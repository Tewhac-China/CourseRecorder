"""幻灯片变化检测与保存（Round-2：稳定态检测）。

Round-1 的做法是"当前帧与上一张差异超阈值即保存"，导致演讲者走动、
投影切换动画、相机抖动都会触发保存（实测 34 秒生成 17 张）。

Round-2 引入 :class:`SlideStateTracker`：

1. 检测到"显著变化"时不立即保存，只登记一个**候选**；
2. 候选必须连续稳定 ``slide_stable_sec`` 秒、且整体持续 ``slide_min_persist_sec``
   秒以上，才确认翻页并保存；
3. 在稳定期内若画面变回上一张，则丢弃候选（说明只是短暂遮挡/闪烁）。

"显著变化"由三个指标投票决定（phash / HSV 直方图 / 边缘结构相似度），
并额外要求**变化面积占比**足够大，用来过滤演讲者走动造成的局部遮挡。

Round-3 性能优化：
- 全部特征在缩略图上计算（WORK_W x WORK_H），避免在 1920x1080 上做 Laplacian / phash。
- 用 struct_signature 差异做快速预筛，无变化时直接跳过昂贵的 phash / 直方图。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import imagehash
import numpy as np
from PIL import Image

# 工作分辨率：所有特征在此尺寸上计算（从 dewarp 输出缩放）
_WORK_W = 480
_WORK_H = 270
# 结构/边缘比较用的固定缩略尺寸：与输入分辨率无关，保证阈值稳定
_STRUCT_SIZE = (64, 36)   # (w, h) 网格级差异比较用的灰度缩略图
_EDGE_SIZE = (160, 90)    # (w, h) 边缘结构签名
_GRID_COLS = 4            # 局部遮挡判定的网格列数
_GRID_ROWS = 3            # 局部遮挡判定的网格行数
# 单格相对峰值差异的下限（避免几乎相同的两帧被噪声放大误判）
_CELL_REL_RATIO = 0.3
_CELL_ABS_FLOOR = 0.02    # 归一化灰度差（0~1）的绝对下限
# 峰值差异低于该值时直接认为没有变化
_CELL_PEAK_FLOOR = 0.02


@dataclass
class SlideFeatures:
    """一帧矫正画面的多指标特征。"""

    phash: "imagehash.ImageHash"
    hist: np.ndarray            # 归一化 HSV 直方图（float32, 1-D）
    edge: np.ndarray            # 边缘结构签名（float32, 1-D）
    struct: np.ndarray          # 归一化灰度缩略图（float32, HxW, 0~1）

    @staticmethod
    def build(bgr_small: np.ndarray, gray_small: np.ndarray) -> "SlideFeatures":
        """在已缩放的缩略图上计算全部特征（调用者负责缩放）。"""
        return SlideFeatures(
            phash=_phash(bgr_small),
            hist=_hist(bgr_small),
            edge=_edge_signature(gray_small),
            struct=_struct_signature(gray_small),
        )


class SlideStateTracker:
    """幻灯片"稳定态"状态机。

    状态：
        ``IDLE``      当前画面 == 已保存的参考帧（上一张幻灯片）
        ``CANDIDATE`` 检测到显著变化，等待稳定确认

    :meth:`update` 的返回值：
        ``"none"``    无变化，继续观察
        ``"pending"`` 候选未满足稳定/持续条件，继续等待
        ``"revert"``  候选期内画面变回上一张 —— 丢弃候选
        ``"confirm"`` 候选已稳定且持续足够久 —— 确认翻页
    """

    IDLE = "idle"
    CANDIDATE = "candidate"

    def __init__(self, stable_sec: float = 0.6, min_persist_sec: float = 1.0) -> None:
        self.stable_sec = float(stable_sec)
        self.min_persist_sec = float(min_persist_sec)
        self._state = self.IDLE
        self._start_ts = 0.0      # 候选首次出现的时间（用作幻灯片时间戳）
        self._stable_since = 0.0  # 当前这段"与候选一致"的起始时间
        self._last_ts = 0.0

    # ------------------------------------------------------------ 属性
    @property
    def state(self) -> str:
        return self._state

    @property
    def candidate_start_ts(self) -> float:
        return self._start_ts

    @property
    def candidate_age_sec(self) -> float:
        return 0.0 if self._state != self.CANDIDATE else self._last_ts - self._start_ts

    @property
    def candidate_stable_sec(self) -> float:
        return 0.0 if self._state != self.CANDIDATE else self._last_ts - self._stable_since

    # ------------------------------------------------------------ 状态推进
    def update(
        self,
        matches_reference: bool,
        matches_candidate: bool,
        timestamp: float,
        allow_confirm: bool = True,
    ) -> str:
        """推进状态机。

        :param matches_reference: 当前帧与"上一张已保存幻灯片"相似
        :param matches_candidate: 当前帧与"当前候选"相似
        :param timestamp: 当前帧时间戳（秒，媒体时间轴）
        :param allow_confirm: 为 False 时即使满足稳定条件也只返回 ``pending``
                              （用于冷却时间未到的情况）
        """
        ts = float(timestamp)
        self._last_ts = ts

        if self._state == self.IDLE:
            if matches_reference:
                return "none"
            # 进入候选态
            self._state = self.CANDIDATE
            self._start_ts = ts
            self._stable_since = ts
            return "pending"

        # -------- CANDIDATE --------
        if matches_reference:
            # 变回上一张：说明只是短暂遮挡/闪烁
            self.reset()
            return "revert"

        if not matches_candidate:
            # 又变成另一种状态：重新开始计时（保留 start_ts 用于时间戳对齐）
            self._stable_since = ts
            return "pending"

        stable = (ts - self._stable_since) >= self.stable_sec
        persisted = (ts - self._start_ts) >= self.min_persist_sec
        if stable and persisted:
            if not allow_confirm:
                # 冷却时间未到：再等一个稳定周期后重试
                self._stable_since = ts
                return "pending"
            self.reset()
            return "confirm"

        return "pending"

    def reset(self) -> None:
        self._state = self.IDLE
        self._start_ts = 0.0
        self._stable_since = 0.0


class SlideExtractor:
    """幻灯片提取器（稳定态检测版）。"""

    def __init__(
        self,
        output_dir: str | Path,
        sensitivity: int = 60,
        cooldown_sec: float = 2.0,
        blur_threshold: float = 80.0,
        min_area_ratio: float = 0.15,
        stable_sec: float = 0.6,
        min_persist_sec: float = 1.0,
        min_changed_area: float = 0.7,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.sensitivity = max(0, min(100, int(sensitivity)))
        self.cooldown_sec = float(cooldown_sec)
        self.blur_threshold = float(blur_threshold)
        self.min_area_ratio = float(min_area_ratio)
        self.min_persist_sec = float(min_persist_sec)
        self.min_changed_area = float(min_changed_area)

        self.tracker = SlideStateTracker(stable_sec=stable_sec, min_persist_sec=min_persist_sec)

        self._index = 0
        self._last_saved_ts = -1e9
        self._ref: Optional[SlideFeatures] = None
        self._candidate: Optional[SlideFeatures] = None
        self._candidate_frame: Optional[np.ndarray] = None
        self._candidate_sharpness = -1.0

        # 灵敏度 -> 各指标阈值；sensitivity 越高阈值越低（越容易触发保存）
        self._apply_thresholds()

    def _apply_thresholds(self):
        """根据 self.sensitivity 重新计算各项检测阈值。"""
        s = self.sensitivity / 100.0
        self._phash_threshold = int(14 - s * 12)          # 2 ~ 14
        self._hist_threshold = float(0.25 - s * 0.20)     # 0.05 ~ 0.25
        self._edge_threshold = float(0.30 - s * 0.15)     # 0.15 ~ 0.30
        self.min_changed_area = float(0.90 - s * 0.40)    # 0.50 ~ 0.90

    def set_sensitivity(self, value: int):
        """动态更新灵敏度并重算阈值（录制中可调）。"""
        self.sensitivity = max(0, min(100, int(value)))
        self._apply_thresholds()

    # ------------------------------------------------------------ 属性
    @property
    def stable_sec(self) -> float:
        return self.tracker.stable_sec

    @property
    def slide_count(self) -> int:
        return self._index

    # ------------------------------------------------------------ 主流程
    def process(self, dewarped: np.ndarray, timestamp: float) -> Optional[dict]:
        """处理一帧矫正后的幻灯片。返回保存信息或 None。"""
        if dewarped is None or dewarped.size == 0:
            return None
        h, w = dewarped.shape[:2]
        if h < 100 or w < 100:
            return None
        if h * w / (1920 * 1080 + 1) < self.min_area_ratio:
            return None

        # ---- 缩放到工作分辨率，后续全部在缩略图上计算 ----
        small = cv2.resize(dewarped, (_WORK_W, _WORK_H), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        # 模糊检测（在 480x270 上，从 17ms 降到 <1ms）
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        if sharpness < self.blur_threshold:
            return None

        # ---- 快速预筛：仅用 struct_signature（~0.2ms）判断是否有变化 ----
        struct = _struct_signature(gray)
        if self._ref is not None:
            ref_struct = self._ref.struct
            area_ratio = _changed_area_ratio(ref_struct, struct)
            # 预筛阈值极低，只过滤"完全没变化"的帧（噪声级）
            # 真正的判定交给 _differs 的多指标投票
            if area_ratio < 0.05:
                return None

        # ---- 完整特征计算（在缩略图上，phash 从 15ms 降到 ~5ms） ----
        feats = SlideFeatures.build(small, gray)

        # 第一帧：直接作为第一张幻灯片
        if self._ref is None:
            return self._save(dewarped, float(timestamp), feats)

        changed = self._differs(self._ref, feats)

        event = self.tracker.update(
            matches_reference=not changed,
            # 候选稳定判断：只要仍与参考帧不同就算"稳定"
            # 不与候选帧比较，避免摄像头噪声打断候选状态
            matches_candidate=changed,
            timestamp=float(timestamp),
            allow_confirm=self._cooldown_ok(float(timestamp)),
        )

        if event == "revert":
            self._clear_candidate()
            return None

        if event == "confirm":
            frame = self._candidate_frame if self._candidate_frame is not None else dewarped
            saved_ts = self.tracker.candidate_start_ts or float(timestamp)
            self._clear_candidate()
            return self._save(frame, saved_ts, feats)

        # pending / none
        if changed:
            if self._candidate is None:
                self._candidate = feats
                self._candidate_frame = dewarped.copy()
                self._candidate_sharpness = sharpness
            elif sharpness > self._candidate_sharpness:
                self._candidate_frame = dewarped.copy()
                self._candidate_sharpness = sharpness
        return None

    def force_save(self, frame: np.ndarray, timestamp: float) -> Optional[dict]:
        """手动强制保存当前画面为幻灯片。

        用于自动检测漏掉某一页时由用户手动补拍。
        保存后同时把该画面设为新的参考帧，并丢弃进行中的候选，
        避免紧接着又自动保存一页重复的。
        """
        if frame is None or frame.size == 0:
            return None
        h, w = frame.shape[:2]
        if h < 100 or w < 100:
            return None

        small = cv2.resize(frame, (_WORK_W, _WORK_H), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        feats = SlideFeatures.build(small, gray)

        self._clear_candidate()
        self.tracker.reset()

        return self._save(frame, float(timestamp), feats)

    def reset(self) -> None:
        """重置到初始状态（重新开始一次录制时调用）。"""
        self._index = 0
        self._last_saved_ts = -1e9
        self._ref = None
        self._clear_candidate()
        self.tracker.reset()

    # ------------------------------------------------------------ 判定
    def _cooldown_ok(self, timestamp: float) -> bool:
        gap = max(self.cooldown_sec, self.min_persist_sec)
        return (timestamp - self._last_saved_ts) >= gap

    def _differs(self, ref: SlideFeatures, cur: SlideFeatures, factor: float = 1.0) -> bool:
        """多指标投票：多数指标认为变化大、且变化面积足够大时返回 True。"""
        phash_dist = int(ref.phash - cur.phash)
        hist_diff = 1.0 - float(
            cv2.compareHist(ref.hist, cur.hist, cv2.HISTCMP_CORREL)
        )
        edge_diff = _cosine_distance(ref.edge, cur.edge)
        area_ratio = _changed_area_ratio(ref.struct, cur.struct)

        votes = (
            int(phash_dist > self._phash_threshold * factor)
            + int(hist_diff > self._hist_threshold * factor)
            + int(edge_diff > self._edge_threshold * factor)
        )
        return votes >= 2 and area_ratio >= self.min_changed_area

    def _clear_candidate(self) -> None:
        self._candidate = None
        self._candidate_frame = None
        self._candidate_sharpness = -1.0

    def _save(self, frame: np.ndarray, timestamp: float, feats: SlideFeatures) -> dict:
        self._index += 1
        filename = f"slide_{self._index:03d}_{int(max(0.0, timestamp) * 1000):08d}.jpg"
        path = self.output_dir / filename
        cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 92])

        self._last_saved_ts = float(timestamp)
        self._ref = feats
        return {
            "index": self._index,
            "timestamp": float(timestamp),
            "filename": filename,
            "path": str(path),
        }


# --------------------------------------------------------------------- 特征
def _phash(img: np.ndarray) -> "imagehash.ImageHash":
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    return imagehash.phash(pil, hash_size=16)


def _hist(img: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist.flatten().astype(np.float32)


def _edge_signature(gray: np.ndarray) -> np.ndarray:
    """边缘结构签名：高斯模糊 -> Canny -> 再模糊，最后按均值归一。

    演讲者遮挡只会改变局部边缘，整页切换会让整张图的边缘结构完全不同，
    因此这个指标对遮挡远不如 phash / 直方图敏感。
    """
    small = cv2.resize(gray, _EDGE_SIZE, interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (5, 5), 0)
    med = float(np.median(small))
    lo = int(max(0.0, 0.66 * med))
    hi = int(min(255.0, 1.33 * med))
    if hi <= lo:
        lo, hi = 50, 150
    edges = cv2.Canny(small, lo, hi)
    edges = cv2.GaussianBlur(edges.astype(np.float32), (5, 5), 0)
    mean = float(edges.mean())
    if mean > 1e-6:
        edges = edges / mean
    return edges.astype(np.float32)


def _struct_signature(gray: np.ndarray) -> np.ndarray:
    """归一化灰度缩略图，用于网格级"变化面积占比"比较。"""
    small = cv2.resize(gray, _STRUCT_SIZE, interpolation=cv2.INTER_AREA)
    return (small.astype(np.float32) / 255.0)


def _cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=np.float32).ravel()
    b = np.asarray(b, dtype=np.float32).ravel()
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na < 1e-6 or nb < 1e-6:
        return 1.0
    return float(max(0.0, 1.0 - float(np.dot(a, b)) / (na * nb)))


def _changed_area_ratio(a: np.ndarray, b: np.ndarray) -> float:
    """按 4x3 网格统计"明显变化"的格子占比。

    阈值相对峰值自适应：整页切换时几乎所有格子都超过 0.3×峰值；
    局部遮挡只有少数格子超过，占比自然很低。
    """
    diff = np.abs(a - b)
    gh, gw = diff.shape[:2]
    cell_h = gh // _GRID_ROWS
    cell_w = gw // _GRID_COLS
    if cell_h <= 0 or cell_w <= 0:
        return 1.0

    cell_means = []
    for r in range(_GRID_ROWS):
        for c in range(_GRID_COLS):
            y0, x0 = r * cell_h, c * cell_w
            cell = diff[y0:y0 + cell_h, x0:x0 + cell_w]
            if cell.size:
                cell_means.append(float(cell.mean()))
    if not cell_means:
        return 1.0

    cm = np.asarray(cell_means, dtype=np.float32)
    peak = float(cm.max())
    if peak < _CELL_PEAK_FLOOR:
        return 0.0
    thr = max(_CELL_ABS_FLOOR, _CELL_REL_RATIO * peak)
    return float((cm > thr).mean())
