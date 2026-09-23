"""cutplan 模块单元测试：层级顺序、就近入刀与决胜规则。"""

import math

import pytest

from app.cutplan import compute_cut_plan
from app.svggeo import analyze_contours


def _plan(d, x, y, fill_rule="nonzero"):
    contours, _warnings = analyze_contours(d, fill_rule)
    return compute_cut_plan(contours, x, y)


def test_outer_with_hole_must_cut_hole_first():
    # 外方 0..10 含内孔 2..8：无论起点在哪，孔（后代）先于外环
    plan = _plan("M0,0h10v10h-10Z M2,2h6v6h-6Z", 0, 0)
    assert plan["contour_count"] == 2
    assert [s["original_index"] for s in plan["steps"]] == [1, 0]
    assert [(s["role"], s["depth"]) for s in plan["steps"]] == [
        ("hole", 1), ("outer", 0)]


def test_three_level_nesting_inside_out():
    # 外-孔-岛：岛（最内层后代）最先，孔其次，外环最后
    d = "M0,0h10v10h-10Z M1,1h8v8h-8Z M6,6h2v2h-2Z"
    plan = _plan(d, 0, 0)
    assert [s["original_index"] for s in plan["steps"]] == [2, 1, 0]
    assert [s["depth"] for s in plan["steps"]] == [2, 1, 0]


def test_entry_is_nearest_subdivision_vertex():
    # 单环：顶点 (0,0)(10,0)(10,10)(0,10)，起点 (-1,0) 最近 (0,0)
    plan = _plan("M0,0h10v10h-10Z", -1, 0)
    step = plan["steps"][0]
    assert step["entry_vertex"] == 0
    assert step["entry_point"] == {"x": 0.0, "y": 0.0}
    assert step["air_distance"] == pytest.approx(1.0)

    # 起点 (-1,10) 时最近顶点为 (0,10)（顶点序号 3）
    plan = _plan("M0,0h10v10h-10Z", -1, 10)
    step = plan["steps"][0]
    assert step["entry_vertex"] == 3
    assert step["entry_point"] == {"x": 0.0, "y": 10.0}


def test_tie_breaks_by_original_index_then_vertex():
    # 两个相同的 10x10 外环：idx0 在 x=0..10，idx1 在 x=20..30，
    # 起点 (15,0) 到两侧各为 5 -> 选原始索引较小的轮廓 0
    plan = _plan("M0,0h10v10h-10Z M20,0h10v10h-10Z", 15, 0)
    assert [s["original_index"] for s in plan["steps"]] == [0, 1]

    # 同一轮廓上距起点等距的两个顶点：正方形起点在中心 (5,5)，
    # 四个顶点距离相同 -> 顶点序号 0 决胜
    plan = _plan("M0,0h10v10h-10Z", 5, 5)
    step = plan["steps"][0]
    assert step["entry_vertex"] == 0
    assert step["entry_point"] == {"x": 0.0, "y": 0.0}


def test_head_stays_at_entry_after_closed_cut():
    # 两独立外环：先切近的右侧轮廓（起点 30,0），加工完停在其入刀点
    # (30,0)；再切左侧时空走距离应从 (30,0) 起算
    plan = _plan("M0,0h10v10h-10Z M20,0h10v10h-10Z", 30, 0)
    assert [s["original_index"] for s in plan["steps"]] == [1, 0]
    s1, s2 = plan["steps"]
    assert s1["entry_point"] == {"x": 30.0, "y": 0.0}
    assert s1["air_distance"] == pytest.approx(0.0)
    # 从 (30,0) 到左环最近顶点 (10,0)
    assert s2["entry_point"] == {"x": 10.0, "y": 0.0}
    assert s2["air_distance"] == pytest.approx(20.0)


def test_air_distances_sum_to_total():
    plan = _plan("M0,0h10v10h-10Z M20,0h10v10h-10Z", -5, -5)
    total = sum(s["air_distance"] for s in plan["steps"])
    assert plan["total_air_distance"] == pytest.approx(total, abs=1e-6)
    # 确定性：同输入同结果
    again = _plan("M0,0h10v10h-10Z M20,0h10v10h-10Z", -5, -5)
    assert again == plan


def test_toolpath_is_closed_ring_starting_at_entry():
    plan = _plan("M0,0h10v10h-10Z", 10, 0)  # 入刀点为顶点 1：(10,0)
    step = plan["steps"][0]
    assert step["entry_vertex"] == 1
    tp = step["toolpath"]
    # 首尾都是入刀点（闭合一周回到入刀点）
    assert tp[0] == tp[-1] == {"x": 10.0, "y": 0.0}
    # 路径为顶点数 + 1 个点，中间各点恰好是环的细分顶点
    assert len(tp) == 5
    assert tp[1] == {"x": 10.0, "y": 10.0}
    assert tp[2] == {"x": 0.0, "y": 10.0}
    assert tp[3] == {"x": 0.0, "y": 0.0}


def test_curve_flattened_vertices_are_candidates():
    # 圆（两段半圆弧细分）：从 (2,0) 入刀，最近顶点是圆上最右点 (1,0)
    plan = _plan("M-1,0 A1,1 0 1,0 1,0 A1,1 0 1,0 -1,0 Z", 2, 0)
    assert len(plan["steps"]) == 1
    step = plan["steps"][0]
    assert step["entry_point"] == {"x": 1.0, "y": 0.0}
    assert step["air_distance"] == pytest.approx(1.0)
    # toolpath 为细分后的闭合环
    assert step["toolpath"][0] == step["toolpath"][-1]


def test_open_and_degenerate_are_skipped_and_not_planned():
    d = "M0,0h10v10h-10Z M5,5 L8,5 L5,5 Z M20,0 L30,0 L30,10"
    plan = _plan(d, 0, 0)
    assert plan["contour_count"] == 1
    assert [s["original_index"] for s in plan["steps"]] == [0]
    assert plan["skipped_contours"] == [
        {"original_index": 1, "role": "degenerate", "reason": "degenerate"},
        {"original_index": 2, "role": "open", "reason": "open"},
    ]


def test_no_valid_contours_returns_empty_plan():
    # 仅开放轮廓
    plan = _plan("M0,0 L10,0 L10,10", 3, 3)
    assert plan["contour_count"] == 0
    assert plan["plan_count"] == 0
    assert plan["steps"] == []
    assert plan["total_air_distance"] == 0.0
    assert plan["start"] == {"x": 3.0, "y": 3.0}
    assert plan["skipped_contours"] == [
        {"original_index": 0, "role": "open", "reason": "open"}]

    # 完全空路径
    plan = _plan("", 0, 0)
    assert plan["steps"] == []
    assert plan["total_air_distance"] == 0.0
    assert plan["skipped_contours"] == []


def test_sibling_holes_both_cut_before_parent():
    # 一个外环含两个孔：两个孔都先于外环；孔之间按就近贪心决定先后
    d = ("M0,0h100v100h-100Z "
         "M10,10h10v10h-10Z "
         "M80,80h10v10h-10Z")
    plan = _plan(d, 5, 10)
    order = [s["original_index"] for s in plan["steps"]]
    assert order[-1] == 0            # 外环最后
    assert set(order[:2]) == {1, 2}  # 两孔在先
    assert order[0] == 1             # 起点离孔 1 最近


def test_invalid_start_raises_value_error():
    contours, _ = analyze_contours("M0,0h10v10h-10Z", "nonzero")
    with pytest.raises(ValueError):
        compute_cut_plan(contours, float("nan"), 0)
    with pytest.raises(ValueError):
        compute_cut_plan(contours, 0, float("inf"))
