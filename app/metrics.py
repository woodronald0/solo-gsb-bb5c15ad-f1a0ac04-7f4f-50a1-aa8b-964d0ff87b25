"""填充面积与面积质心（只读即时分析）。

对一份已存 SVG path 即时解析并完成轮廓包含层级分类后，按
**层级交替**规则统计填充区域：

- 外环（``outer``，包含深度偶数）面积计入；
- 孔洞（``hole``，深度奇数）面积扣减；
- 孔中“岛”（深度 2）再次计入，岛中孔（深度 3）再次扣减，以此类推。

因此填充总面积为各环面积的交替代数和；面积质心为按各环**带符号
面积矩**加权的质心。开放轮廓（未以 ``Z`` 闭合）与退化轮廓
（面积为 0 或顶点不足）不参与。面积基于曲线细分后的多边形
（与规范化/间隙校验同一份细分折线，见 :func:`app.svggeo.flatten_subpath`），
不使用解析积分，也不写库。

坐标系沿用 SVG 屏幕坐标（y 轴向下），质心即该坐标系下的几何质心，
与轮廓书写方向无关。
"""

from __future__ import annotations

import math
from typing import Any

from .svggeo import Contour, signed_area

# 净面积小于该阈值视为“无有效填充区域”（与退化判定 1e-10*diag² 同量级）
_AREA_EPS = 1e-10
# 输出量化位数（仅影响 JSON 展示，不改变判定）
_DECIMALS = 9


def compute_metrics(contours: list[Contour]) -> dict[str, Any]:
    """对已分类的轮廓列表计算填充总面积与面积质心。

    参数:
        contours: :func:`app.svggeo.analyze_contours` 返回的轮廓
            （已带 ``role`` / ``depth``）。

    返回:
        ``{area, centroid, contributing_contour_count, contours,
        skipped_contours}``；``centroid`` 为 ``{"x", "y"}`` 或
        无有效闭合区域时的 ``None``。
    """
    valid = [c for c in contours if c.role in ("outer", "hole")]
    valid.sort(key=lambda c: c.original_index)

    scale = 1.0
    for c in valid:
        diag = math.hypot(c.bounds[2] - c.bounds[0], c.bounds[3] - c.bounds[1])
        scale = max(scale, diag)
    eps = _AREA_EPS * scale ** 2

    total_area = 0.0
    moment_x = 0.0  # Σ sign·area·cx
    moment_y = 0.0
    items: list[dict[str, Any]] = []

    for c in valid:
        ring_area = abs(c.area)
        cx, cy = polygon_centroid(c.points, c.area)
        sign = 1.0 if c.role == "outer" else -1.0
        signed_contrib = sign * ring_area
        total_area += signed_contrib
        moment_x += signed_contrib * cx
        moment_y += signed_contrib * cy
        items.append({
            "original_index": c.original_index,
            "role": c.role,
            "depth": c.depth,
            "area": _num(ring_area),
            "signed_contribution": _num(signed_contrib),
            "centroid": {"x": _num(cx), "y": _num(cy)},
        })

    skipped = [
        {"original_index": c.original_index, "role": c.role,
         "reason": ("open" if c.role == "open" else "degenerate")}
        for c in sorted(contours, key=lambda c: c.original_index)
        if c.role not in ("outer", "hole")
    ]

    # 净面积被孔洞完全抵消（理论上的等面积异常嵌套）或本就没有闭合区域
    if abs(total_area) <= eps:
        return {
            "area": 0.0,
            "centroid": None,
            "contributing_contour_count": len(valid),
            "contours": items,
            "skipped_contours": skipped,
        }

    return {
        "area": _num(total_area),
        "centroid": {"x": _num(moment_x / total_area),
                     "y": _num(moment_y / total_area)},
        "contributing_contour_count": len(valid),
        "contours": items,
        "skipped_contours": skipped,
    }


def polygon_centroid(points: list[tuple[float, float]],
                     area: float | None = None
                     ) -> tuple[float, float]:
    """细分多边形的几何质心（鞋带公式配套，与顶点环绕方向无关）。

    退化（面积 0）多边形返回顶点均值，调用方不应在填充统计中使用其结果。
    """
    a = signed_area(points) if area is None else area
    if len(points) < 3 or abs(a) <= 0.0:
        if points:
            mx = sum(p[0] for p in points) / len(points)
            my = sum(p[1] for p in points) / len(points)
            return mx, my
        return 0.0, 0.0

    sx = sy = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        cross = x1 * y2 - x2 * y1
        sx += (x1 + x2) * cross
        sy += (y1 + y2) * cross
    return sx / (6.0 * a), sy / (6.0 * a)


def _num(v: float) -> float:
    """量化展示值并消除 -0.0。"""
    r = round(v, _DECIMALS)
    return r + 0.0 if r == 0 else r
