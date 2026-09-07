"""SlideExtractor 稳定态逻辑单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.slide_extractor import SlideExtractor


def make_slide(text: str, size: tuple = (640, 360), bg: tuple = (240, 240, 240)) -> np.ndarray:
    """生成一张带文字的 BGR 图像，用于模拟幻灯片。"""
    img = np.full((*size[::-1], 3), bg, dtype=np.uint8)
    cv2.putText(
        img, text, (40, 180), cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 0), 3, cv2.LINE_AA
    )
    return img


def add_occluder(img: np.ndarray) -> np.ndarray:
    """在右下角加一个人形遮挡块，模拟演讲者。"""
    out = img.copy()
    h, w = out.shape[:2]
    cv2.rectangle(out, (w - 120, h - 180), (w - 20, h - 20), (80, 80, 80), -1)
    return out


def test_stable_state_filters_brief_occlusion() -> None:
    """短暂遮挡不应产生新幻灯片。"""
    out_dir = Path(__file__).resolve().parents[1] / "_tmp" / "slide_test"
    ext = SlideExtractor(
        out_dir,
        sensitivity=60,
        cooldown_sec=0.0,
        stable_sec=0.5,
        min_persist_sec=0.8,
        min_area_ratio=0.05,
    )

    base = make_slide("Slide A")
    saved = []

    # 1.0 秒稳定显示 Slide A
    ts = 0.0
    for _ in range(30):
        info = ext.process(base, ts)
        if info:
            saved.append(info)
        ts += 0.033

    # 0.3 秒演讲者遮挡
    for _ in range(9):
        info = ext.process(add_occluder(base), ts)
        if info:
            saved.append(info)
        ts += 0.033

    # 恢复 Slide A
    for _ in range(30):
        info = ext.process(base, ts)
        if info:
            saved.append(info)
        ts += 0.033

    assert len(saved) == 1, f"预期只保存 1 张，实际 {len(saved)}"
    assert saved[0]["index"] == 1
    print("test_stable_state_filters_brief_occlusion PASS")


def test_persisted_change_creates_new_slide() -> None:
    """持续显示的新幻灯片应被保存。"""
    out_dir = Path(__file__).resolve().parents[1] / "_tmp" / "slide_test"
    ext = SlideExtractor(
        out_dir,
        sensitivity=60,
        cooldown_sec=0.0,
        stable_sec=0.3,
        min_persist_sec=0.5,
        min_area_ratio=0.05,
    )

    saved = []
    ts = 0.0
    # Slide A 0.8 秒
    for _ in range(25):
        info = ext.process(make_slide("Slide A", bg=(240, 240, 240)), ts)
        if info:
            saved.append(info)
        ts += 0.033

    # Slide B 持续 1.2 秒，背景色也不同
    for _ in range(36):
        info = ext.process(make_slide("Slide B", bg=(200, 220, 240)), ts)
        if info:
            saved.append(info)
        ts += 0.033

    assert len(saved) == 2, f"预期保存 2 张，实际 {len(saved)}"
    assert saved[0]["filename"].startswith("slide_001")
    assert saved[1]["filename"].startswith("slide_002")
    print("test_persisted_change_creates_new_slide PASS")


def test_transition_frames_are_ignored() -> None:
    """快速变化的过渡帧不应被保存。"""
    out_dir = Path(__file__).resolve().parents[1] / "_tmp" / "slide_test"
    ext = SlideExtractor(
        out_dir,
        sensitivity=60,
        cooldown_sec=0.0,
        stable_sec=0.3,
        min_persist_sec=0.5,
        min_area_ratio=0.05,
    )

    saved = []
    ts = 0.0
    # 稳定 Slide A
    for _ in range(20):
        info = ext.process(make_slide("Slide A", bg=(240, 240, 240)), ts)
        if info:
            saved.append(info)
        ts += 0.033

    # 快速闪烁 0.4 秒（模拟切换动画）
    for i in range(12):
        if i % 2 == 0:
            info = ext.process(make_slide("Slide A", bg=(240, 240, 240)), ts)
        else:
            info = ext.process(make_slide("Slide B", bg=(200, 220, 240)), ts)
        if info:
            saved.append(info)
        ts += 0.033

    # 稳定 Slide B
    for _ in range(30):
        info = ext.process(make_slide("Slide B", bg=(200, 220, 240)), ts)
        if info:
            saved.append(info)
        ts += 0.033

    assert len(saved) == 2, f"预期保存 2 张，实际 {len(saved)}"
    print("test_transition_frames_are_ignored PASS")


def main() -> int:
    test_stable_state_filters_brief_occlusion()
    test_persisted_change_creates_new_slide()
    test_transition_frames_are_ignored()
    print("\nAll slide-extractor tests PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
