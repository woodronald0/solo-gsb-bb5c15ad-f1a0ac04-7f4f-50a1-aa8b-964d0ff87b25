"""闭合轮廓加工刀路规划（cut-plan，只读即时分析）。

对一份已存 SVG path 即时解析并完成轮廓包含层级分类后（与
metrics / clearance / scanlines 同一份解析与曲线细分折线，见
:func:`app.svggeo.analyze_contours` 与
:func:`app.svggeo.flatten_subpath`），按“**后代先于祖先**”的
包含层级约束与**贪心就近入刀**生成闭合轮廓加工顺序：

- 仅闭合且非退化轮廓（``outer`` / ``hole``）参与；开放轮廓
  （未以 ``Z`` 闭合）与退化轮廓（面积为 0 或顶点不足）跳过并在
  ``skipped_contours`` 中列出原始索引与原因；
- 每个轮廓必须在其**所有后代**（直接/间接子轮廓）加工完后才能
  加工——即先切最内层的孔/岛，逐层向外；
- 每一轮从当前“可加工”轮廓（后代均已完成）中，挑选距当前刀头
  位置最近的**细分顶点**作为入刀点；距离相同时依次按
  **轮廓原始索引、顶点序号**决胜；
- 加工一个闭合轮廓时，刀头从入刀点（空走，计空走距离）移动到该
  顶点，随后沿闭合环切一周回到入刀点；加工完后刀头停在入刀点，
  继续计算下一步；
- ``total_air_distance`` 为各步入刀空走距离之和。

整个计算只读、即时完成：不依赖已保存的规范化结果，也不写库。
没有任何有效闭合轮廓（空路径、仅开放/退化轮廓）时返回空计划。
"""

from __future__ import annotations

import math
from typing import Any

from .svggeo import Contour

# 输出量化位数（仅影响 JSON 展示，不改变选点判定）
_DECIMALS = 9


def compute_cut_plan(contours: list[Contour],
                     start_x: float,
                     start_y: float) -> dict[str, Any]:
    """对已分类的轮廓列表生成闭合轮廓加工计划。

    参数:
        contours: :func:`app.svggeo.analyze_contours` 返回的轮廓
            （已带 ``role`` / ``depth`` / ``parent_original_index``）
        start_x, start_y: 刀头起点坐标，必须为有限数

    返回:
        ``{start, contour_count, skipped_contours, plan_count, steps,
        total_air_distance}``；``steps`` 每项含轮廓标识、入刀顶点
        序号/坐标、本步空走距离与从入刀点起闭合一周的 ``toolpath``。
        无有效闭合轮廓时 ``steps`` 为空列表、总空走距离为 0。

    抛出:
        ValueError: 起点坐标非有限数值。
    """
    if not (isinstance(start_x, (int, float))
            and isinstance(start_y, (int, float))
            and math.isfinite(start_x) and math.isfinite(start_y)):
        raise ValueError("起点 x/y 必须是有限数值")

    start = (float(start_x), float(start_y))

    valid = [c for c in contours if c.role in ("outer", "hole")]
    valid.sort(key=lambda c: c.original_index)

    skipped = [
        {"original_index": c.original_index, "role": c.role,
         "reason": ("open" if c.role == "open" else "degenerate")}
        for c in sorted(contours, key=lambda c: c.original_index)
        if c.role not in ("outer", "hole")
    ]

    if not valid:
        return {
            "start": {"x": _num(start[0]), "y": _num(start[1])},
            "contour_count": 0,
            "skipped_contours": skipped,
            "plan_count": 0,
            "steps": [],
            "total_air_distance": 0.0,
        }

    by_index = {c.original_index: c for c in valid}
    children: dict[int, list[int]] = {c.original_index: [] for c in valid}
    roots: list[int] = []
    for c in valid:
        p = c.parent_original_index
        if p is not None and p in by_index:
            children[p].append(c.original_index)
        else:
            roots.append(c.original_index)
    for ids in children.values():
        ids.sort()

    # 后代闭包：轮廓的全部直接/间接子轮廓（拓扑 DAG，简单 DFS 即可）
    def descendants(idx: int) -> list[int]:
        out: list[int] = []
        stack = list(children[idx])
        while stack:
            cur = stack.pop()
            out.append(cur)
            stack.extend(children[cur])
        return out

    family = {c.original_index: set(descendants(c.original_index))
              for c in valid}
    remaining = set(by_index)

    steps: list[dict[str, Any]] = []
    head = start
    total_air = 0.0

    while remaining:
        # 可加工：所有后代均已完成（即后代集合与剩余集合不相交）
        ready = [by_index[i] for i in sorted(remaining)
                 if not (family[i] & remaining)]

        # 在可加工轮廓的全部细分顶点中，按
        # (到刀头的平方距离, 轮廓原始索引, 顶点序号) 贪心决胜
        best: tuple[float, int, int] | None = None
        for c in ready:
            for vi, (px, py) in enumerate(c.points):
                key = ((px - head[0]) ** 2 + (py - head[1]) ** 2,
                       c.original_index, vi)
                if best is None or key < best:
                    best = key
        assert best is not None  # 层级为森林，必有可加工轮廓
        _, chosen_index, entry_index = best
        chosen = by_index[chosen_index]
        ex, ey = chosen.points[entry_index]

        air = math.hypot(ex - head[0], ey - head[1])
        total_air += air

        # 从入刀顶点起沿细分闭合环一周，回到入刀点
        n = len(chosen.points)
        ring = [chosen.points[(entry_index + k) % n] for k in range(n)]
        ring.append(ring[0])

        steps.append({
            "order": len(steps) + 1,
            "original_index": chosen.original_index,
            "role": chosen.role,
            "depth": chosen.depth,
            "entry_vertex": entry_index,
            "entry_point": {"x": _num(ex), "y": _num(ey)},
            "air_distance": _num(air),
            "toolpath": [{"x": _num(px), "y": _num(py)} for px, py in ring],
        })

        remaining.remove(chosen_index)
        head = (ex, ey)  # 闭合轮廓加工完停在入刀点

    return {
        "start": {"x": _num(start[0]), "y": _num(start[1])},
        "contour_count": len(valid),
        "skipped_contours": skipped,
        "plan_count": len(steps),
        "steps": steps,
        "total_air_distance": _num(total_air),
    }


def _num(v: float) -> float:
    """量化展示值并消除 -0.0。"""
    r = round(v, _DECIMALS)
    return r + 0.0 if r == 0 else r
