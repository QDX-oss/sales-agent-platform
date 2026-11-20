# -*- coding: utf-8 -*-
"""W4-01 Eval Set：用例集结构与判定器。

判定的每条规则都要有测试压着 —— 判定器说谁挂谁，报告就跟着说，
它自己错了没有一道题能平反。
"""
import pytest

from core import evalset


# ---------------- 用例集本身 ----------------

def test_用例集十题且三道易错():
    doc, cases = evalset.load_cases()
    # 主卷是人工钉死的 10 题；regression 是失败回流进来的（W4-03），会越来越多，
    # 所以这里只数主卷 —— 回流题单独在 test_reflow 里验。
    main = [c for c in cases if c.get("group") != "regression"]
    assert len(main) == 10
    traps = [c for c in main if c.get("difficulty") == "trap"]
    assert len(traps) == 3, "台账口径：3 题故意易错，实际 %d" % len(traps)
    # 每题必须有判定方式与预期 —— 没有预期值的题在报告里没法判
    for c in cases:
        exp = c.get("expect") or {}
        assert exp.get("type") in ("exact", "must_contain", "must_refuse", "either",
                                   "pending"), \
            "%s 判定类型不明: %s" % (c.get("id"), exp.get("type"))
        assert c.get("came_from"), "%s 缺 came_from（换数据的人对不上账）" % c.get("id")


def test_易错题覆盖台账点名的三类陷阱():
    doc, cases = evalset.load_cases()
    trap_ids = {c["id"] for c in cases if c.get("difficulty") == "trap"}
    # T01 口径歧义（毛利率）· T02 无数据（2026）· T03 需跨条件组合（三条件）
    assert trap_ids == {"T01", "T02", "T03"}
    by_id = {c["id"]: c for c in cases}
    assert "毛利率" in by_id["T01"]["question"]
    assert "2026" in by_id["T02"]["question"]
    assert by_id["T03"]["expect"]["type"] == "exact"


# ---------------- 三种判定方式 ----------------

def _case(doc_cases, cid):
    return [c for c in doc_cases if c["id"] == cid][0]


def test_精确值判定容忍逗号与单位():
    doc, cases = evalset.load_cases()
    c = _case(cases, "E01")
    for ans in ("1,235,074.0 元", "1235074元", "销售额为 1235074.0"):
        assert evalset.check_case(c, ans)["ok"], ans
    # 差一个量级就是错，别拿"误差容忍"当挡箭牌
    assert not evalset.check_case(c, "约 123,507 元")["ok"]
    assert not evalset.check_case(c, "大约两百万")["ok"]


def test_必须包含判定():
    doc, cases = evalset.load_cases()
    c = _case(cases, "E04")
    assert evalset.check_case(c, "山东省的利润率最高，为 31.24%")["ok"]
    assert not evalset.check_case(c, "安徽省的利润率最高")["ok"]


def test_必须拒答判定含编造检测():
    doc, cases = evalset.load_cases()
    c = _case(cases, "T02")
    assert evalset.check_case(c, "2026 年的数据不存在，数据只覆盖 2025 年")["ok"]
    # 嘴上说没有、手上编数字：这是最危险的错法，必须单独逮住
    r = evalset.check_case(c, "没有 2026 年数据，按趋势估算约 1,200,000 元")
    assert not r["ok"] and "编造" in r["reason"]
    # 复述年份不算编造 —— "2026"只有 4 位
    assert evalset.check_case(c, "没有 2026 年的数据")["ok"]


def test_口径歧义题答对或诚实拒答都算过():
    doc, cases = evalset.load_cases()
    c = _case(cases, "T01")
    # 正确：说明口径并以利润率作答
    assert evalset.check_case(c, "无毛利率（无成本列）；利润率为 20.88%")["ok"]
    # 正确：诚实说算不出来
    assert evalset.check_case(c, "数据中没有毛利率，无法计算")["ok"]
    # 错误：拿利润率的数字冒充毛利率，一字不提口径
    assert not evalset.check_case(c, "广东省的毛利率是 20.88%")["ok"]


def test_三条件组合题只给部分条件算挂():
    doc, cases = evalset.load_cases()
    c = _case(cases, "T03")
    assert evalset.check_case(c, "广东口红在线下专柜的销售额是 10,710.0 元")["ok"]
    # 漏了渠道条件的答案就是台账里点名的那错法
    assert not evalset.check_case(c, "广东的口红卖了 29322 元")["ok"]


# ---------------- 汇总 ----------------

def test_汇总统计与漏答处理():
    doc, cases = evalset.load_cases()
    # 待定口径的回流题（W4-03）不进分母，这里只给有口径的题作答
    scored = [c for c in cases if (c.get("expect") or {}).get("type") != "pending"]
    answers = {c["id"]: "对不上随便说" for c in scored}
    r = evalset.run_eval(cases, answers)
    assert r["scored"] == len(scored) and r["pending"] == len(cases) - len(scored)
    assert r["passed"] == 0 and r["rate"] == 0.0
    # 漏答的题如实报"没有作答"，不许悄悄当它过
    r2 = evalset.run_eval(cases, {})
    assert all((not row["ok"]) and "没有作答" in row["reason"] for row in r2["rows"])
