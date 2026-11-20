# -*- coding: utf-8 -*-
"""W2-04 预算护栏测试。

验收口径（台账原文）：把上限设为极低值，run 在超限处停下，trace 记录 aborted=budget。
这里分三层测：Budget 判定本身 → Runner 在波次间拦住 → 班组停下后"话说得像话"。
"""

import json

import pytest

from core import budget as B
from core.budget import Budget
from core.graph import Graph, Runner
from core.team import run_team
from core.trace import Trace, load_run
from ai_analyst import query_stats, rank, trend
from tests.fake_llm import ScriptedChatModel, two_turn_script


# ---------------- 判定本身 ----------------

def test_没设上限时永远放行():
    b = Budget(0, None)
    assert not b.enabled()
    assert b.check(None) is None


def test_token触顶报用量与上限():
    class T:
        def tokens_used(self):
            return 5000

        def calls_made(self):
            return 1

    hit = Budget(max_tokens=4000).check(T())
    assert hit and hit["limit"] == "tokens"
    assert hit["used"] == 5000 and hit["cap"] == 4000
    assert "5,000" in hit["why"], "提示里要带千分位用量，人一眼能读"


def test_调用次数触顶报calls():
    class T:
        def tokens_used(self):
            return 100

        def calls_made(self):
            return 9

    hit = Budget(max_calls=8).check(T())
    assert hit and hit["limit"] == "calls"
    assert hit["used"] == 9


def test_读配置且0视为不设限(tmp_path):
    p = tmp_path / "team.json"
    p.write_text(json.dumps({"budget": {"max_tokens": 300, "max_calls": 0}}),
                 encoding="utf-8")
    b = B.load_budget(p)
    assert b.max_tokens == 300
    assert b.max_calls == 0, '写 0 是"这条不设限"，不是"上限为零"'


def test_配置缺失退回默认不打死链路(tmp_path):
    b = B.load_budget(tmp_path / "nope.json")
    assert b.max_tokens == B.DEFAULT_MAX_TOKENS
    assert b.note, "退回默认要留下原因，否则配置写错没人知道"


def test_真实配置文件能被读出来():
    b = B.load_budget()
    assert b.enabled(), "config/team.json 里应有 budget 段"
    assert b.note == ""


# ---------------- Runner：波次之间拦住 ----------------

def _graph3():
    return Graph.from_dict({
        "name": "t", "nodes": [
            {"id": "a", "action": "x"},
            {"id": "b", "action": "x", "needs": ["a"]},
            {"id": "c", "action": "x", "needs": ["b"]},
        ], "edges": []})


def _burn(trace, tokens, calls=1):
    """执行器：每跑一个节点就记一笔 token，模拟模型调用。"""
    def run(state):
        if trace is not None:
            with trace.step(agent="Analyst", name="fake") as st:
                st.tokens_in = tokens
                st.tokens_out = 0
        return "ok", {}
    return run


def test_极低token上限在第二个节点后停下(tmp_path):
    tr = Trace(question="预算测试", runs_dir=tmp_path)
    g = _graph3()
    runner = Runner(g, {"x": _burn(tr, 1000)}, trace=tr, parallel=False,
                    budget=Budget(max_tokens=1500))
    state = {}
    plan = runner.run(state)

    assert plan["aborted"]["limit"] == "tokens"
    assert [n["id"] for n in plan["nodes"]] == ["a", "b"], "第三个节点不该再开跑"
    assert plan["completed"] is False
    assert tr.aborted == "budget"
    # 落盘也必须带上：这是验收口径的硬要求
    sm = tr.finish()
    assert sm["aborted"] == "budget"
    data = load_run(tr.run_id, runs_dir=tmp_path)
    assert data["end"]["aborted"] == "budget"
    assert data["end"]["abort_detail"]["used"] == 2000


def test_调用次数上限同样生效(tmp_path):
    tr = Trace(question="预算测试", runs_dir=tmp_path)
    runner = Runner(_graph3(), {"x": _burn(tr, 10)}, trace=tr, parallel=False,
                    budget=Budget(max_calls=1))
    plan = runner.run({})
    assert plan["aborted"]["limit"] == "calls"
    assert [n["id"] for n in plan["nodes"]] == ["a"]


def test_不设预算时照旧跑完(tmp_path):
    tr = Trace(question="预算测试", runs_dir=tmp_path)
    runner = Runner(_graph3(), {"x": _burn(tr, 10)}, trace=tr, parallel=False)
    plan = runner.run({})
    assert plan["aborted"] is None
    assert plan["completed"] is True


# ---------------- 班组层：停下之后话说得像话 ----------------

def _ask(trace, budget, max_retry=0):
    script = two_turn_script(province="广东", answer="广东省销售额 1235074 元。")
    return run_team("广东省的销售额是多少？", ScriptedChatModel(script=script),
                    [query_stats, rank, trend], trace=trace, max_retry=max_retry,
                    budget=budget)


def test_触顶后返回已有结论并说明原因(tmp_path):
    tr = Trace(question="广东省的销售额是多少？", source="test", runs_dir=tmp_path)
    # 低到连一轮 Analyst 都跑不起：第一个有 token 的节点之后就停
    out = _ask(tr, Budget(max_tokens=100))

    assert out["aborted"]["kind"] == "budget"
    assert "预算上限" in out["answer"], "用户必须看到为什么停：%r" % out["answer"][:80]
    assert out["passed"] is False
    # 预算中止不是故障：degraded 仍可能为真（没结论时），但绝不该写成 failed
    sm = tr.finish()
    assert sm["aborted"] == "budget"
    assert sm["status"] != "failed"


def test_默认预算下正常跑完且不误伤(tmp_path):
    tr = Trace(question="广东省的销售额是多少？", source="test", runs_dir=tmp_path)
    out = _ask(tr, None)          # None = 读 config/team.json 的默认值
    assert out["aborted"] is None
    assert out["passed"] is True
    tr.finish()
