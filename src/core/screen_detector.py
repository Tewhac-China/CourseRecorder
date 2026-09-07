"""投影屏幕四边形检测 + 透视矫正。

针对“手持相机拍摄投影幕/投影墙”的场景做了两层策略：

1. **亮区策略（优先）**：投影幕通常是画面中最亮、最均匀的大块矩形区域。
   用大津阈值 + 形态学闭运算提取亮块，再找轮廓。
2. **Canny 边缘策略（兜底）**：亮区策略找不到四边形时，改用 Canny 边缘。

两种策略都会：
- 用 ``approxPolyDP`` 把轮廓近似成四边形；
- 按“面积 + 长宽比接近 16:9 / 4:3 + 凸性”打分，取最高分。

另外提供 ``QuadStabilizer`` 做帧间指数平滑，抑制检测抖动造成的
矫正画面闪烁（这会直接影响幻灯片去重效果）。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import cv2
import numpy as np

# 自动检测时把帧缩放到该宽度，兼顾速度与精度
_DETECT_WIDTH = 480
# 候选四边形面积至少占画面的比例
_MIN_AREA_RATIO = 0.06
# 候选四边形面积至多占画面的比例（避免选中整幅画面）
# Round-2：0.98 -> 0.90，自动检测经常把整幅画面当成屏幕
_MAX_AREA_RATIO = 0.90
# 允许的目标长宽比
_TARGET_ASPECTS = (16.0 / 9.0, 16.0 / 10.0, 4.0 / 3.0, 3.0 / 2.0)
# 角点允许超出画面的比例（Round-2：0.5 -> 0.1）
_OUT_OF_FRAME_RATIO = 0.1
# Canny 兜底策略：内部平均亮度相对外部的最低要求
_INTERIOR_MIN_BRIGHT_RATIO = 1.15
_INTERIOR_MIN_BRIGHT_DELTA = 10.0


class ScreenDetector:
    """屏幕四边形检测与透视矫正。"""

    def __init__(
        self,
        target_aspect: float = 16.0 / 9.0,
        output_size: Optional[Tuple[int, int]] = None,
        min_area_ratio: float = _MIN_AREA_RATIO,
    ) -> None:
        self.target_aspect = float(target_aspect)
        # output_size=None 表示「原生尺寸」: 矫正输出沿用源帧分辨率,
        # 不做任何强制缩放(不再固定 1920x1080)。
        self.output_size = (
            (int(output_size[0]), int(output_size[1])) if output_size else None
        )
        self.min_area_ratio = float(min_area_ratio)

    # ------------------------------------------------------------ 排序
    @staticmethod
    def order_points(pts: np.ndarray) -> np.ndarray:
        """把 4 个点排序为 [左上, 右上, 右下, 左下]。

        使用“和/差”法（对近似轴对齐的矩形稳健），再用质心角度法兜底。
        """
        pts = np.asarray(pts, dtype=np.float32).reshape(-1, 2)
        if pts.shape[0] != 4:
            raise ValueError(f"order_points 需要 4 个点，收到 {pts.shape[0]} 个")

        rect = np.zeros((4, 2), dtype=np.float32)
        s = pts.sum(axis=1)
        rect[0] = pts[np.argmin(s)]   # 左上：x+y 最小
        rect[2] = pts[np.argmax(s)]   # 右下：x+y 最大

        rest_idx = [i for i in range(4) if i not in (int(np.argmin(s)), int(np.argmax(s)))]
        rest = pts[rest_idx]
        if len(rest) == 2:
            d = np.diff(rest, axis=1).ravel()
            # 差最小的是右上（x-y 最小），差最大的是左下
            tr, bl = rest[int(np.argmin(d))], rest[int(np.argmax(d))]
            rect[1] = tr
            rect[3] = bl
        return rect

    # ------------------------------------------------------------ 自动检测
    def auto_detect(self, frame: np.ndarray) -> Optional[np.ndarray]:
        """返回 4x2 float32 角点（原始分辨率坐标系），失败返回 None。"""
        if frame is None or frame.size == 0:
            return None

        h, w = frame.shape[:2]
        scale = _DETECT_WIDTH / float(w) if w > _DETECT_WIDTH else 1.0
        if scale < 1.0:
            small = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        else:
            small = frame

        gray_small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        candidates: List[Tuple[float, np.ndarray]] = []
        # 亮区策略优先；Canny 兜底时额外要求“内部比外部亮”，否则容易选中墙面
        for extractor, require_bright_interior in (
            (self._mask_bright, False),
            (self._mask_edges, True),
        ):
            mask = extractor(small)
            if mask is None:
                continue
            for quad in self._quads_from_mask(mask, small.shape):
                if require_bright_interior and not self._interior_brighter(quad, gray_small):
                    continue
                score = self._score_quad(quad, small.shape)
                if score > 0:
                    candidates.append((score, quad))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)
        best = candidates[0][1]
        if scale < 1.0:
            best = best / scale
        return self.order_points(best).astype(np.float32)

    # ------------------------------------------------------------ 掩膜生成
    def _mask_bright(self, small: np.ndarray) -> Optional[np.ndarray]:
        """提取画面中的亮区域（投影幕）。"""
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        # 大津阈值
        _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        # 亮区占比过小时，说明画面整体偏亮/偏暗，用固定高阈值再试一次
        ratio = float((otsu > 0).mean())
        if ratio < 0.05 or ratio > 0.75:
            thr = float(gray.mean() + gray.std())
            _, otsu = cv2.threshold(gray, thr, 255, cv2.THRESH_BINARY)
            ratio = float((otsu > 0).mean())
            if ratio < 0.03 or ratio > 0.9:
                return None

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        mask = cv2.morphologyEx(otsu, cv2.MORPH_CLOSE, kernel, iterations=2)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel, iterations=1)
        return mask

    def _mask_edges(self, small: np.ndarray) -> Optional[np.ndarray]:
        """Canny 边缘 + 膨胀连接，作为兜底掩膜。"""
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        med = float(np.median(gray))
        lo = int(max(0, 0.66 * med))
        hi = int(min(255, 1.33 * med))
        if hi <= lo:
            lo, hi = 50, 150
        edges = cv2.Canny(gray, lo, hi)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        edges = cv2.dilate(edges, kernel, iterations=2)
        edges = cv2.erode(edges, kernel, iterations=1)
        return edges

    # ------------------------------------------------------------ 轮廓 -> 四边形
    def _quads_from_mask(self, mask: np.ndarray, shape) -> List[np.ndarray]:
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return []

        h, w = shape[:2]
        frame_area = float(h * w)
        quads: List[np.ndarray] = []

        contours = sorted(contours, key=cv2.contourArea, reverse=True)[:12]
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < frame_area * self.min_area_ratio:
                continue
            peri = cv2.arcLength(cnt, True)
            if peri <= 0:
                continue
            # 逐步放宽近似精度，直到得到 4 个顶点
            for eps_ratio in (0.02, 0.03, 0.04, 0.05, 0.07):
                approx = cv2.approxPolyDP(cnt, eps_ratio * peri, True)
                if len(approx) == 4:
                    quad = approx.reshape(4, 2).astype(np.float32)
                    if cv2.isContourConvex(quad.reshape(4, 2).astype(np.float32)):
                        quads.append(quad)
                    break
            else:
                # 轮廓形状不规则：退回最小外接矩形作为候选
                if area > frame_area * 0.10:
                    rect = cv2.minAreaRect(cnt)
                    box = cv2.boxPoints(rect).astype(np.float32)
                    quads.append(box)
        return quads

    @staticmethod
    def _interior_brighter(
        quad: np.ndarray,
        gray: np.ndarray,
        min_ratio: float = _INTERIOR_MIN_BRIGHT_RATIO,
        min_delta: float = _INTERIOR_MIN_BRIGHT_DELTA,
    ) -> bool:
        """判断四边形内部是否明显亮于外部（投影幕 vs 墙面/环境）。

        内/外掩膜都做了腐蚀/膨胀，避开边界像素，减少透视误差的影响。
        """
        if gray is None or gray.size == 0:
            return False
        h, w = gray.shape[:2]
        try:
            pts = np.asarray(quad, dtype=np.float32).reshape(4, 2)
            if not np.all(np.isfinite(pts)):
                return False
            pts = np.clip(pts, [0, 0], [w - 1, h - 1])
        except Exception:
            return False

        mask_in = np.zeros((h, w), dtype=np.uint8)
        cv2.fillConvexPoly(mask_in, pts.astype(np.int32), 255)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        inner = cv2.erode(mask_in, kernel, iterations=2)
        outer = cv2.bitwise_not(cv2.dilate(mask_in, kernel, iterations=2))
        if not inner.any() or not outer.any():
            # 几乎占满画面：没有可比较的外部，视为不合格
            return False

        mi = float(gray[inner > 0].mean())
        mo = float(gray[outer > 0].mean())
        return bool(mi >= mo * min_ratio or mi >= mo + min_delta)

    def _score_quad(self, quad: np.ndarray, shape) -> float:
        """给候选四边形打分，<=0 表示不合格。"""
        h, w = shape[:2]
        frame_area = float(h * w)

        q = np.asarray(quad, dtype=np.float32).reshape(4, 2)
        area = abs(cv2.contourArea(q))
        area_ratio = area / frame_area
        if area_ratio < self.min_area_ratio or area_ratio > _MAX_AREA_RATIO:
            return 0.0

        ordered = self.order_points(q)
        tl, tr, br, bl = ordered
        width_top = float(np.linalg.norm(tr - tl))
        width_bottom = float(np.linalg.norm(br - bl))
        height_left = float(np.linalg.norm(bl - tl))
        height_right = float(np.linalg.norm(br - tr))
        width = (width_top + width_bottom) / 2.0
        height = (height_left + height_right) / 2.0
        if width < 8 or height < 8:
            return 0.0

        aspect = width / height
        if aspect < 1.0:  # 统一按横向处理
            aspect = 1.0 / aspect
        # 优先匹配用户设定的目标长宽比，其次再从常见比例中挑选
        _aspects = (
            _TARGET_ASPECTS if self.target_aspect in _TARGET_ASPECTS
            else (self.target_aspect, *_TARGET_ASPECTS)
        )
        aspect_err = min(abs(aspect - a) / a for a in _aspects)

        # 四条边长度比例的一致性（越接近平行四边形/矩形越好）
        sides = np.array([width_top, width_bottom, height_left, height_right], dtype=np.float32)
        side_cv = float(sides.std() / (sides.mean() + 1e-6))

        # 角度接近直角的程度
        angles = []
        for i in range(4):
            a = ordered[i - 1] - ordered[i]
            b = ordered[(i + 1) % 4] - ordered[i]
            na, nb = np.linalg.norm(a), np.linalg.norm(b)
            if na < 1e-6 or nb < 1e-6:
                return 0.0
            cosv = float(np.dot(a, b) / (na * nb))
            angles.append(abs(np.degrees(np.arccos(np.clip(cosv, -1.0, 1.0))) - 90.0))
        angle_err = float(np.mean(angles)) / 90.0

        if aspect_err > 0.45 or angle_err > 0.45:
            return 0.0

        score = (
            area_ratio * 3.0
            - aspect_err * 2.0
            - angle_err * 2.0
            - side_cv * 1.0
        )
        return float(score)

    # ------------------------------------------------------------ 矫正
    def dewarp(
        self,
        frame: np.ndarray,
        corners: np.ndarray,
        output_size: Optional[Tuple[int, int]] = None,
    ) -> Optional[np.ndarray]:
        """透视变换到 ``output_size``，返回 BGR 图像。

        ``output_size`` 为 None 且实例未指定尺寸时，使用**源帧的原始分辨率**
        作为输出尺寸，不做任何强制放大/缩小。
        """
        if frame is None or corners is None:
            return None
        size = output_size or self.output_size
        try:
            pts = self.order_points(np.asarray(corners, dtype=np.float32))
        except Exception:
            return None

        if size is None:
            # 输出比例固定为 target_aspect（默认 16:9），取源帧内该比例的最大矩形。
            # 这样无论摄像头输出是 4:3 还是 16:9，矫正结果都按目标比例铺满，
            # 而不会被压成「随机截取的一块」（例如 4:3 源输出 4:3）。
            fh, fw = frame.shape[:2]
            if fw / fh >= self.target_aspect:
                w = int(round(fh * self.target_aspect))
                h = int(fh)
            else:
                w = int(fw)
                h = int(round(fw / self.target_aspect))
        else:
            w, h = int(size[0]), int(size[1])
        dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
        try:
            matrix = cv2.getPerspectiveTransform(pts, dst)
            warped = cv2.warpPerspective(frame, matrix, (w, h), flags=cv2.INTER_LINEAR)
        except Exception:
            return None
        return warped

    # ------------------------------------------------------------ 工具
    @staticmethod
    def is_valid_corners(corners, frame_shape=None) -> bool:
        """检查角点是否构成一个合理的凸四边形。"""
        if corners is None:
            return False
        try:
            pts = np.asarray(corners, dtype=np.float32).reshape(4, 2)
        except Exception:
            return False
        if not np.all(np.isfinite(pts)):
            return False
        if frame_shape is not None:
            h, w = frame_shape[:2]
            # Round-2：允许超出画面的范围从 ±50% 收紧到 ±10%
            m = _OUT_OF_FRAME_RATIO
            if pts[:, 0].min() < -w * m or pts[:, 0].max() > w * (1.0 + m):
                return False
            if pts[:, 1].min() < -h * m or pts[:, 1].max() > h * (1.0 + m):
                return False
        if abs(cv2.contourArea(pts)) < 1.0:
            return False
        return bool(cv2.isContourConvex(pts))

    @staticmethod
    def draw_overlay(
        frame: np.ndarray,
        corners,
        color=(0, 255, 0),
        thickness=2,
        labels: bool = True,
    ) -> np.ndarray:
        """在副本上绘制四边形与角点序号，供预览使用。"""
        out = frame.copy()
        if corners is None:
            return out
        try:
            pts = ScreenDetector.order_points(np.asarray(corners, dtype=np.float32))
        except Exception:
            return out
        pts_int = pts.astype(int)
        cv2.polylines(out, [pts_int], True, color, thickness, cv2.LINE_AA)
        if labels:
            for i, (x, y) in enumerate(pts_int):
                cv2.circle(out, (int(x), int(y)), max(4, thickness * 3), (0, 0, 255), -1, cv2.LINE_AA)
                cv2.putText(
                    out, str(i + 1), (int(x) + 6, int(y) - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2, cv2.LINE_AA,
                )
        return out


class QuadStabilizer:
    """四边形角点的帧间指数平滑 + 有效性校验。

    自动检测在演讲者走动时会抖动，直接逐帧使用会导致矫正画面晃动，
    进而让感知哈希误判为“幻灯片变化”。这里用 EMA 平滑，并在检测结果
    与平滑值偏离过大时重置。
    """

    def __init__(self, alpha: float = 0.25, max_jump_ratio: float = 0.18) -> None:
        self.alpha = float(alpha)
        self.max_jump_ratio = float(max_jump_ratio)
        self._current: Optional[np.ndarray] = None

    def reset(self) -> None:
        self._current = None

    @property
    def current(self) -> Optional[np.ndarray]:
        return None if self._current is None else self._current.copy()

    def update(self, detected: Optional[np.ndarray], frame_shape=None) -> Optional[np.ndarray]:
        """喂入本帧检测结果，返回平滑后的角点。"""
        if detected is None:
            return self.current

        det = np.asarray(detected, dtype=np.float32).reshape(4, 2)
        if not np.all(np.isfinite(det)):
            return self.current

        if self._current is None:
            self._current = det.copy()
            return self.current

        diag = 1.0
        if frame_shape is not None:
            h, w = frame_shape[:2]
            diag = float(np.hypot(w, h))
        else:
            diag = float(np.linalg.norm(det.max(axis=0) - det.min(axis=0))) + 1e-6

        delta = float(np.linalg.norm(det - self._current, axis=1).max())
        if delta > self.max_jump_ratio * diag:
            # 检测结果跳变过大（可能换了一张 detections 或演讲者遮挡），直接跟随
            self._current = det.copy()
        else:
            self._current = (1.0 - self.alpha) * self._current + self.alpha * det
        return self.current
