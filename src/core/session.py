"""一次录制会话的输出管理（闪退安全版）。

设计原则：
- 所有数据在写入时立即落盘，进程崩溃不丢失已录制内容；
- 视频用 ffmpeg 子进程写 MKV（MKV 容器无需 finalize 即可播放）；
- 音频每收到一块就 append 到 raw PCM 文件；
- 转写每生成一条就 append 到 JSONL 文件；
- 幻灯片元数据同样实时 append。
- finalize() 仅做收尾：PCM→WAV 转换、notes.md 生成、session.json 写入。
"""

from __future__ import annotations

import json
import subprocess
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
import numpy as np


class Session:
    """一次录制会话（闪退安全）。"""

    def __init__(
        self,
        base_dir: str | Path,
        video_source: Dict[str, Any],
        audio_source: Dict[str, Any],
        record_raw_video: bool = True,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.video_source = dict(video_source)
        self.audio_source = dict(audio_source)
        self.record_raw_video = bool(record_raw_video)

        self.name = self._generate_name()
        self.dir = self.base_dir / self.name
        self.slides_dir = self.dir / "slides"

        self._created_at = datetime.now(timezone.utc)
        self._video_size: Optional[tuple] = None
        self._fps: float = 30.0
        self._finalized = False

        # ffmpeg 子进程（MKV 流式写入）
        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        # 增量写入的文件句柄
        self._audio_fp: Optional[object] = None
        self._transcript_fp: Optional[object] = None
        self._slides_meta_fp: Optional[object] = None

        # 内存计数（用于 session.json）
        self._audio_bytes_written = 0
        self._transcript_count = 0
        self._slides: List[Dict[str, Any]] = []

        # 分段录制管理
        self._video_segments: List[Path] = []  # 已完成的视频分段文件路径
        self._audio_segments: List[Path] = []  # 已完成的音频分段文件路径
        self._segment_index: int = 0  # 当前分段索引

    def _generate_name(self) -> str:
        now = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return f"session_{now}"

    def create(self) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.slides_dir.mkdir(parents=True, exist_ok=True)
        return str(self.dir)

    # ── 视频：ffmpeg 子进程写 MKV ──────────────────────────────
    def init_video_writer(self, frame: np.ndarray, fps: float) -> bool:
        """启动 ffmpeg 子进程，将原始帧通过 pipe 写入 MKV 文件。"""
        if not self.record_raw_video or frame is None:
            return False
        self._video_size = (frame.shape[1], frame.shape[0])
        self._fps = float(fps)
        w, h = self._video_size
        path = self.dir / "raw_video.mkv"
        try:
            self._ffmpeg_proc = subprocess.Popen(
                [
                    "ffmpeg", "-y",
                    "-f", "rawvideo",
                    "-pix_fmt", "bgr24",
                    "-s", f"{w}x{h}",
                    "-r", str(fps),
                    "-i", "-",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-crf", "18",
                    "-pix_fmt", "yuv420p",
                    str(path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception as exc:
            print(f"[Session] ffmpeg 启动失败: {exc}")
            self._ffmpeg_proc = None
            return False

    def write_video_frame(self, frame: np.ndarray) -> None:
        if self._ffmpeg_proc is not None and self._ffmpeg_proc.stdin is not None and frame is not None:
            if (frame.shape[1], frame.shape[0]) == self._video_size:
                try:
                    self._ffmpeg_proc.stdin.write(frame.tobytes())
                except Exception as exc:
                    print(f"[Session] 写入视频帧失败: {exc}")
                    self._ffmpeg_proc = None

    def pause_video_writer(self) -> None:
        """暂停时关闭当前视频分段。"""
        if self._ffmpeg_proc is not None:
            try:
                if self._ffmpeg_proc.stdin:
                    self._ffmpeg_proc.stdin.close()
                self._ffmpeg_proc.wait(timeout=30)
            except Exception as exc:
                print(f"[Session] ffmpeg 关闭异常: {exc}")
                try:
                    self._ffmpeg_proc.kill()
                except Exception:
                    pass
            # 记录分段文件路径
            segment_path = self.dir / f"raw_video_{self._segment_index:03d}.mkv"
            if (self.dir / "raw_video.mkv").exists():
                # 重命名当前视频为分段文件
                (self.dir / "raw_video.mkv").rename(segment_path)
                self._video_segments.append(segment_path)
            self._ffmpeg_proc = None
            self._segment_index += 1

    def resume_video_writer(self, frame: np.ndarray, fps: float) -> bool:
        """恢复时启动新的视频分段。"""
        if not self.record_raw_video or frame is None:
            return False
        self._video_size = (frame.shape[1], frame.shape[0])
        self._fps = float(fps)
        w, h = self._video_size
        # 新分段使用临时文件名 raw_video.mkv
        path = self.dir / "raw_video.mkv"
        try:
            self._ffmpeg_proc = subprocess.Popen(
                [
                    "ffmpeg", "-y",
                    "-f", "rawvideo",
                    "-pix_fmt", "bgr24",
                    "-s", f"{w}x{h}",
                    "-r", str(fps),
                    "-i", "-",
                    "-c:v", "libx264",
                    "-preset", "ultrafast",
                    "-crf", "18",
                    "-pix_fmt", "yuv420p",
                    str(path),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception as exc:
            print(f"[Session] ffmpeg 启动失败: {exc}")
            self._ffmpeg_proc = None
            return False

    # ── 音频：增量写入 raw PCM 文件 ───────────────────────────
    def write_audio(self, pcm_bytes: bytes) -> None:
        if not pcm_bytes:
            return
        if self._audio_fp is None:
            path = self.dir / "audio.raw"
            self._audio_fp = open(str(path), "ab")
        try:
            self._audio_fp.write(pcm_bytes)
            self._audio_fp.flush()
            self._audio_bytes_written += len(pcm_bytes)
        except Exception as exc:
            print(f"[Session] 写入音频失败: {exc}")

    def pause_audio(self) -> None:
        """暂停音频写入，保存当前音频分段。"""
        if self._audio_fp is not None:
            self._audio_fp.close()
            self._audio_fp = None
        # 记录当前音频分段
        audio_path = self.dir / f"audio_{self._segment_index - 1:03d}.raw"
        if (self.dir / "audio.raw").exists():
            (self.dir / "audio.raw").rename(audio_path)
            self._audio_segments.append(audio_path)

    def resume_audio(self) -> None:
        """恢复音频写入。"""
        # 新的音频会自动写入 audio.raw（由 write_audio 方法处理）
        pass

    # ── 转写：增量 append JSONL ────────────────────────────────
    def write_transcript(self, segment: Dict[str, Any]) -> None:
        if self._transcript_fp is None:
            path = self.dir / "transcript.jsonl"
            self._transcript_fp = open(str(path), "a", encoding="utf-8")
        try:
            self._transcript_fp.write(json.dumps(segment, ensure_ascii=False) + "\n")
            self._transcript_fp.flush()
            self._transcript_count += 1
        except Exception as exc:
            print(f"[Session] 写入转写失败: {exc}")

    # ── 幻灯片：增量 append 元数据 ─────────────────────────────
    def write_slide(self, info: Dict[str, Any]) -> None:
        self._slides.append(info)
        if self._slides_meta_fp is None:
            path = self.dir / "slides.jsonl"
            self._slides_meta_fp = open(str(path), "a", encoding="utf-8")
        try:
            self._slides_meta_fp.write(json.dumps(info, ensure_ascii=False) + "\n")
            self._slides_meta_fp.flush()
        except Exception as exc:
            print(f"[Session] 写入幻灯片元数据失败: {exc}")

    # ── 收尾 ───────────────────────────────────────────────────
    def _close_handles(self) -> None:
        """关闭所有打开的文件句柄和子进程。"""
        if self._ffmpeg_proc is not None:
            try:
                if self._ffmpeg_proc.stdin:
                    self._ffmpeg_proc.stdin.close()
                self._ffmpeg_proc.wait(timeout=30)
            except Exception as exc:
                print(f"[Session] ffmpeg 关闭异常: {exc}")
                try:
                    self._ffmpeg_proc.kill()
                except Exception:
                    pass
            # 记录最后一个视频分段
            segment_path = self.dir / f"raw_video_{self._segment_index:03d}.mkv"
            if (self.dir / "raw_video.mkv").exists():
                (self.dir / "raw_video.mkv").rename(segment_path)
                self._video_segments.append(segment_path)
            self._ffmpeg_proc = None

        # 记录最后一个音频分段
        if self._audio_fp is not None:
            self._audio_fp.close()
            self._audio_fp = None
        audio_path = self.dir / f"audio_{self._segment_index:03d}.raw"
        if (self.dir / "audio.raw").exists():
            (self.dir / "audio.raw").rename(audio_path)
            self._audio_segments.append(audio_path)

        for fp_attr in ("_transcript_fp", "_slides_meta_fp"):
            fp = getattr(self, fp_attr, None)
            if fp is not None:
                try:
                    fp.close()
                except Exception:
                    pass
                setattr(self, fp_attr, None)

    def _concat_video_segments(self) -> None:
        """拼接所有视频分段为一个完整视频。"""
        if len(self._video_segments) == 0:
            return

        # 如果只有一个分段，直接重命名
        if len(self._video_segments) == 1:
            final_path = self.dir / "raw_video.mkv"
            self._video_segments[0].rename(final_path)
            return

        # 创建 concat 文件列表
        concat_file = self.dir / "concat_list.txt"
        with open(concat_file, "w", encoding="utf-8") as f:
            for segment in self._video_segments:
                # 使用 ffmpeg concat 协议，需要转义单引号
                escaped_path = str(segment).replace("'", "'\\''")
                f.write(f"file '{escaped_path}'\n")

        # 使用 ffmpeg 拼接
        final_path = self.dir / "raw_video.mkv"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", str(concat_file),
                    "-c", "copy",
                    str(final_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # 拼接成功后删除分段文件和 concat 文件
            for segment in self._video_segments:
                segment.unlink(missing_ok=True)
            concat_file.unlink(missing_ok=True)
        except Exception as exc:
            print(f"[Session] 视频拼接失败: {exc}")
            # 拼接失败时保留分段文件

    def _concat_audio_segments(self) -> None:
        """拼接所有音频分段。"""
        if len(self._audio_segments) == 0:
            return

        # 如果只有一个分段，直接重命名
        if len(self._audio_segments) == 1:
            final_path = self.dir / "audio.raw"
            self._audio_segments[0].rename(final_path)
            return

        # 拼接所有音频分段
        final_path = self.dir / "audio.raw"
        with open(final_path, "wb") as outfile:
            for segment in self._audio_segments:
                if segment.exists():
                    with open(segment, "rb") as infile:
                        outfile.write(infile.read())
                    segment.unlink(missing_ok=True)

    def _convert_audio_to_wav(self) -> None:
        """将 raw PCM 转换为 WAV。"""
        raw_path = self.dir / "audio.raw"
        if not raw_path.exists() or raw_path.stat().st_size == 0:
            return
        wav_path = self.dir / "audio.wav"
        try:
            with open(str(raw_path), "rb") as rf, wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                while True:
                    chunk = rf.read(65536)
                    if not chunk:
                        break
                    wf.writeframes(chunk)
            # WAV 写入成功后删除 raw 文件
            raw_path.unlink()
        except Exception as exc:
            print(f"[Session] PCM→WAV 转换失败: {exc}")

    def _save_session_json(self) -> None:
        path = self.dir / "session.json"
        # 统计视频文件
        video_path = self.dir / "raw_video.mkv"
        has_video = video_path.exists() and video_path.stat().st_size > 0
        data = {
            "session_name": self.name,
            "created_at": self._created_at.isoformat(),
            "video_source": self.video_source,
            "audio_source": self.audio_source,
            "slide_count": len(self._slides),
            "transcript_count": self._transcript_count,
            "raw_video": has_video,
            "crash_safe": True,
        }
        with path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    def _load_transcript_from_disk(self) -> List[Dict[str, Any]]:
        """从 JSONL 文件加载转写数据（finalize 后重建 notes 用）。"""
        path = self.dir / "transcript.jsonl"
        segments: List[Dict[str, Any]] = []
        if not path.exists():
            return segments
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        segments.append(json.loads(line))
        except Exception as exc:
            print(f"[Session] 加载转写失败: {exc}")
        return segments

    def _load_slides_from_disk(self) -> List[Dict[str, Any]]:
        """从 slides.jsonl 加载幻灯片元数据。"""
        path = self.dir / "slides.jsonl"
        slides: List[Dict[str, Any]] = []
        if not path.exists():
            return slides
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        slides.append(json.loads(line))
        except Exception as exc:
            print(f"[Session] 加载幻灯片元数据失败: {exc}")
        return slides

    def _build_notes(self, slides: List[Dict], transcript: List[Dict], include_translation: bool = False) -> str:
        """生成 Markdown 课堂笔记。

        Args:
            slides: 幻灯片元数据列表
            transcript: 转写段落列表
            include_translation: 是否包含翻译（中英文对照）

        Returns:
            Markdown 格式的笔记内容
        """
        lines: List[str] = []
        title = f"# 课堂笔记 - {self.name}"
        if include_translation:
            title += " (中英对照)"
        lines.append(title)
        lines.append("")
        lines.append(f"- 录制时间：{self._created_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"- 视频源：{self._video_source_desc()}")
        lines.append(f"- 音频源：{self._audio_source_desc()}")
        lines.append(f"- 幻灯片数：{len(slides)}")
        lines.append(f"- 转写句数：{len(transcript)}")
        lines.append("")

        if not slides:
            lines.append("> 未检测到幻灯片。")
            lines.append("")
            if transcript:
                lines.append("## 转写内容")
                lines.append("")
                for seg in transcript:
                    text = seg['text']
                    translated = seg.get('translated', '')
                    if include_translation and translated:
                        lines.append(f"- `[{seg['start']:.1f}s]` {text}")
                        lines.append(f"  - [翻译] {translated}")
                    else:
                        lines.append(f"- `[{seg['start']:.1f}s]` {text}")
            return "\n".join(lines)

        slides = sorted(slides, key=lambda s: s.get("timestamp", 0))
        max_ts = max((t.get("end", 0) for t in transcript), default=0)
        for i, slide in enumerate(slides):
            start = slide.get("timestamp", 0)
            end = slides[i + 1].get("timestamp", max_ts) if i + 1 < len(slides) else max_ts
            fname = slide.get("filename", "")
            lines.append(f"## 幻灯片 {i + 1} ({start:.1f}s - {end:.1f}s)")
            lines.append("")
            rel_path = f"slides/{fname}" if fname else ""
            if rel_path:
                lines.append(f"![幻灯片 {i + 1}]({rel_path})")
            lines.append("")
            found = False
            for seg in transcript:
                if start <= seg.get("start", 0) < end:
                    text = seg['text']
                    translated = seg.get('translated', '')
                    if include_translation and translated:
                        # 中英文对照格式
                        lines.append(f"- `[{seg['start']:.1f}s]` {text}")
                        lines.append(f"  - [翻译] {translated}")
                    else:
                        lines.append(f"- `[{seg['start']:.1f}s]` {text}")
                    found = True
            if not found:
                lines.append("- （该时段无转写内容）")
            lines.append("")
        return "\n".join(lines)

    def _video_source_desc(self) -> str:
        v = self.video_source
        if v.get("type") == "file":
            return f"文件: {v.get('path', '')}"
        return f"摄像头 #{v.get('index', 0)}"

    def _audio_source_desc(self) -> str:
        a = self.audio_source
        if a.get("type") == "file":
            return "视频文件音轨"
        return f"{a.get('type', 'microphone')}: {a.get('name', '')}"

    def finalize(self) -> str:
        if self._finalized:
            return str(self.dir)

        # 1. 关闭所有句柄（ffmpeg、文件）
        self._close_handles()

        # 2. 拼接视频分段
        self._concat_video_segments()

        # 3. 拼接音频分段
        self._concat_audio_segments()

        # 4. PCM → WAV
        self._convert_audio_to_wav()

        # 5. 从磁盘加载转写和幻灯片元数据（它们已在磁盘上）
        transcript = self._load_transcript_from_disk()
        slides = self._load_slides_from_disk()

        # 6. session.json
        self._save_session_json()

        # 7. notes.md（原始版本）
        notes_path = self.dir / "notes.md"
        notes_path.write_text(self._build_notes(slides, transcript), encoding="utf-8")

        # 8. notes_translated.md（中英对照版本）
        has_translations = any(seg.get('translated') for seg in transcript)
        if has_translations:
            notes_translated_path = self.dir / "notes_translated.md"
            notes_translated_path.write_text(
                self._build_notes(slides, transcript, include_translation=True),
                encoding="utf-8",
            )

        self._finalized = True
        return str(self.dir)

    @property
    def slides_count(self) -> int:
        return len(self._slides)

    @property
    def transcript_count(self) -> int:
        return self._transcript_count
