# -*- coding: utf-8 -*-
"""编排屏数据组装的测试（台账 W3-01）。

这层最容易出的错是「假数据能画出来，真运行画不出来」—— 事件名拼错、
字段名对不上，屏上就是一片空白，还查不出是哪儿空。所以这里不测 JS，
只测「喂一份 trace，能不能拧出屏上要的东西」，而且喂的是**真事件名**。

跑法（项目根目录）：
    python -m pytest tests/test_orchestrate.py -q
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import orchestrate                                   # noqa: E402


def _write_run(tmp_path, run_id, head, events, tail=None):
    """按 Trace 的真实写法造一份 jsonl：run 头 + event 流 + run_end。"""
    lines = [dict(type="run", run_id=run_id, question=head, source="test")]
    for i, (name, detail) in enumerate(events, start=1):
        lines.append(dict(type="event", seq=i, at="2026-11-07T10:00:00.000",
                          event=name, detail=detail))
    if tail:
        lines.append(dict(type="run_end", run_id=run_id, status="ok",
                          at="2026-11-07T10:00:01.000",
                          ended_at="2026-11-07T10:00:01.000",
                          duration_ms=1234.5, tokens={"in": 100, "out": 20},
                          step_count=2, retry_count=0))
    path = tmp_path / (run_id + ".jsonl")
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines),
                    encoding="utf-8")
    return path


@pytest.fixture
def runs_dir(tmp_path, monkeypatch):
    """把 runs 目录指到临时目录 —— 测试不许碰真实运行记录。"""
    monkeypatch.setattr(orchestrate, "RUNS_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------- 正常一次

def test_正常一次运行(runs_dir):
    _write_run(runs_dir, "20261107T100000-aaaaaa", "哪个省最高", [
        ("graph_wave", {"nodes": ["verify", "critique"], "slots": 2,
                        "order": ["verify", "critique"]}),
        ("scheduler_start", {"job": "verify", "priority": 90, "waited_ms": 0.0,
                             "deadline_ms": 120000.0, "slot": "1/2"}),
        ("scheduler_start", {"job": "critique", "priority": 80, "waited_ms": 6.8,
                             "deadline_ms": 120000.0, "slot": "2/2"}),
        ("scheduler_done", {"job": "verify", "status": "ok", "duration_ms": 13.7}),
        ("scheduler_done", {"job": "critique", "status": "ok", "duration_ms": 754.7}),
        ("graph_node", {"node": "draft", "agent": "Analyst", "status": "ok",
                        "visit": 0, "duration_ms": 3418.4}),
        ("graph_node", {"node": "verify", "agent": "verifier", "status": "ok",
                        "visit": 0, "duration_ms": 13.7}),
        ("graph_node", {"node": "critique", "agent": "Critic", "status": "ok",
                        "visit": 0, "duration_ms": 754.7}),
        ("graph_node", {"node": "gate", "agent": "gate", "status": "ok",
                        "visit": 0, "duration_ms": 0.0}),
        ("graph_node", {"node": "curate", "agent": "Curator", "status": "ok",
                        "visit": 0, "duration_ms": 1040.5}),
        ("graph_node", {"node": "final_check", "agent": "verifier", "status": "ok",
                        "visit": 0, "duration_ms": 0.1}),
    ], tail=True)

    d = orchestrate.build()
    assert d["ok"] is True
    assert d["run"]["run_id"] == "20261107T100000-aaaaaa"
    assert d["run"]["question"] == "哪个省最高"

    # 图来自 pipeline()，不是屏上抄的：节点数、回边都要对得上
    assert len(d["nodes"]) == 6
    assert [e["from"] for e in d["graph"]["edges"]] == ["gate"]
    assert [e["to"] for e in d["graph"]["edges"]] == ["draft"]

    assert d["summary"]["total"] == 6
    assert d["summary"]["passed"] == 6
    assert d["summary"]["degraded"] is False

    # 调度账：两条 start 各配一条 done，排队时长不被合并掉
    rows = {r["job"]: r for r in d["schedule"]}
    assert rows["verify"]["slot"] == "1/2"
    assert rows["critique"]["waited_ms"] == 6.8
    assert rows["critique"]["status"] == "ok"
    assert rows["critique"]["duration_ms"] == 754.7


def test_分层的层号按最长依赖算(runs_dir):
    _write_run(runs_dir, "20261107T100001-bbbbbb", "q", [])
    d = orchestrate.build()
    layer = {n["id"]: n["layer"] for n in d["nodes"]}
    assert layer["draft"] == 0
    assert layer["verify"] == 1
    assert layer["critique"] == 1          # 同层 = 可并发，屏上同一行
    assert layer["gate"] == 2
    assert layer["curate"] == 3
    assert layer["final_check"] == 4
    assert d["graph"]["layer_count"] == 5


# ---------------------------------------------------------------- W3-02 图

def test_前进边由needs派生而不是前端猜(runs_dir):
    """W3-02 的硬口径：屏上一条边都不用自己推，换图不改前端。"""
    _write_run(runs_dir, "20261107T100010-gggggg", "q", [])
    d = orchestrate.build()
    fwd = d["graph"]["forward_edges"]
    # needs 总数：draft0 + verify1 + critique1 + gate2 + curate1 + final_check1 = 6
    assert len(fwd) == 6
    assert {"from": "draft", "to": "verify", "kind": "dependency"} in fwd
    assert {"from": "verify", "to": "gate", "kind": "dependency"} in fwd
    assert {"from": "critique", "to": "gate", "kind": "dependency"} in fwd
    # 回边只认配置里写出来的那一条，且带 kind=loop
    back = d["graph"]["back_edges"]
    assert len(back) == 1
    assert back[0]["from"] == "gate" and back[0]["kind"] == "loop"
    # 只走 needs 不会把回边误画成前进边
    assert not any(e["from"] == "gate" and e["to"] == "draft" for e in fwd)


def test_换一份图定义图上数据全变_前端不用改(runs_dir, monkeypatch):
    """台账 W3-02 验收口径：换一份 graph 定义，节点与连线随之变化。"""
    _write_run(runs_dir, "20261107T100011-hhhhhh", "q", [])
    compact = {
        "name": "sales.compact", "version": 9,
        "nodes": [
            {"id": "draft", "label": "取数", "agent": "Analyst", "needs": []},
            {"id": "verify", "label": "回查", "agent": "verifier", "needs": ["draft"]},
            {"id": "gate", "label": "合议", "agent": "gate", "needs": ["verify"]},
            {"id": "curate", "label": "成文", "agent": "Curator", "needs": ["gate"]},
        ],
        "edges": [],
    }
    monkeypatch.setattr(orchestrate, "pipeline", lambda: compact)
    d = orchestrate.build()

    assert d["graph"]["name"] == "sales.compact"
    assert d["graph"]["version"] == 9
    assert [n["id"] for n in d["graph"]["nodes"]] == ["draft", "verify", "gate", "curate"]
    assert d["graph"]["layer_count"] == 4
    assert len(d["graph"]["forward_edges"]) == 3
    assert d["graph"]["back_edges"] == []
    # 状态仍从同一份 trace 读，图换了也不影响"哪些节点跑过"
    assert set(d["summary"]["pending"]) == {"draft", "verify", "gate", "curate"}


# ---------------------------------------------------------------- 重试与降级

def test_重试过的节点留下次数(runs_dir):
    """一个节点跑两轮，attempts 必须是 2 —— 只说"通过"就把重试藏了。"""
    _write_run(runs_dir, "20261107T100002-cccccc", "q", [
        ("graph_node", {"node": "draft", "status": "ok", "visit": 0,
                        "duration_ms": 100.0}),
        ("graph_node", {"node": "draft", "status": "ok", "visit": 1,
                        "duration_ms": 200.0}),
        ("diagnosis", {"attempt": 1, "kind": "fetch", "label": "取数错",
                       "strategy": "refetch", "why": "数字对不上",
                       "signals": ["verify 未通过"]}),
    ])
    d = orchestrate.build()
    draft = [n for n in d["nodes"] if n["id"] == "draft"][0]
    assert draft["attempts"] == 2
    assert draft["duration_ms"] == 200.0     # 取最后一遍，不是第一遍
    assert len(d["diagnoses"]) == 1
    assert d["diagnoses"][0]["strategy"] == "refetch"
    # 诊断也要落在时间线上，否则屏上只有节点、没有"为什么重来"
    assert any(t["event"] == "diagnosis" for t in d["timeline"])


def test_真的换过通道的事件名认得出来(runs_dir):
    """用 core/fallback.py 会写的那个事件名 fallback（不是 channel_fallback）。"""
    _write_run(runs_dir, "20261107T100003-dddddd", "q", [
        ("channel_unavailable", {"name": "siliconflow", "reason": "缺密钥"}),
        ("channel_fail", {"channel": "deepseek",
                          "error_type": "OpenAIAuthenticationError",
                          "status_code": 401, "consecutive": 1}),
        ("fallback", {"from": "deepseek", "to": "backup-demo",
                      "reason": "consecutive_fail", "consecutive": 1,
                      "threshold": 1, "status_code": 401, "cooldown_s": 300.0}),
    ])
    d = orchestrate.build()
    assert d["summary"]["degraded"] is True
    names = [x["event"] for x in d["fallbacks"]]
    assert "fallback" in names
    assert "channel_fail" in names
    text = [t["text"] for t in d["timeline"] if t["event"] == "fallback"]
    assert text and "deepseek" in text[0] and "backup-demo" in text[0]
    assert "401" in text[0]                  # 现场要能看出是 401 还是超时


def test_超时节点单独计数(runs_dir):
    _write_run(runs_dir, "20261107T100004-eeeeee", "q", [
        ("node_timeout", {"node": "verify", "deadline_ms": 120000.0,
                          "waited_ms": 400.0}),
        ("graph_node", {"node": "verify", "status": "timeout", "visit": 0,
                        "duration_ms": 400.0}),
    ])
    d = orchestrate.build()
    assert d["summary"]["timed_out"] == ["verify"]
    assert d["summary"]["passed"] == 0


def test_没跑的节点算待跑不是失败(runs_dir):
    """只跑到一半：没出现在事件里的节点是 pending，不能算成 0 分。"""
    _write_run(runs_dir, "20261107T100005-ffffff", "q", [
        ("graph_node", {"node": "draft", "status": "ok", "visit": 0,
                        "duration_ms": 10.0}),
    ])
    d = orchestrate.build()
    assert d["summary"]["passed"] == 1
    assert set(d["summary"]["pending"]) == {"verify", "critique", "gate",
                                            "curate", "final_check"}
    assert d["summary"]["failed"] == 0


# ---------------------------------------------------------------- 入口防呆

def test_找不到运行要说人话(runs_dir):
    d = orchestrate.build("20260101T000000-nothing")
    assert d["ok"] is False
    assert "找不到" in d["error"]


def test_run_id_不许路径穿越(runs_dir):
    d = orchestrate.build("../../app")
    assert d["ok"] is False
    d = orchestrate.build("a/b")
    assert d["ok"] is False


def test_一次都没有时说清楚(runs_dir):
    d = orchestrate.build()
    assert d["ok"] is False
    assert "没有" in d["error"]


def test_列表只给最近的(runs_dir):
    for i in range(15):
        _write_run(runs_dir, "20261107T10%04d-xxxxxx" % i, "q%d" % i, [])
    runs = orchestrate.list_runs(limit=12)
    assert len(runs) == 12
    assert runs[0]["run_id"] == "20261107T100014-xxxxxx"   # 新的在前
    assert runs[0]["question"] == "q14"


# ---------------------------------------------------------------- 实时视图（W3-03）
#
# 这一段守的是「屏上要能看见过程」。它最怕两种假绿：
#   1. 节点明明在跑，屏上还是灰的 → 观众以为卡死了
#   2. 节点早跑完了，屏上还留一个蓝的在这儿呼吸 → 观众以为还在跑
# 两种都只能靠「同一份 jsonl、不同时刻」的断言抓出来。

_HALF = [
    ("graph_node", {"node": "draft", "agent": "Analyst", "status": "ok",
                    "visit": 0, "duration_ms": 3418.4}),
    ("graph_wave", {"nodes": ["verify", "critique"], "slots": 2,
                    "order": ["verify", "critique"]}),
    ("scheduler_start", {"job": "verify", "priority": 90, "waited_ms": 0.0,
                         "deadline_ms": 120000.0, "slot": "1/2"}),
        ("scheduler_start", {"job": "critique", "priority": 80, "waited_ms": 1.2,
                             "deadline_ms": 120000.0, "slot": "2/2"}),
        # 先跑两下再收尾：两个节点同时排队却一个都没落地的记录，
        # 真实 trace 里不会出现（同一层真跑过必有 graph_node）。
        ("scheduler_done", {"job": "verify", "status": "ok", "duration_ms": 820.0}),
        ("scheduler_done", {"job": "critique", "status": "ok", "duration_ms": 910.0}),
]


def test_跑到一半时占着槽位的节点算进行中(runs_dir):
    _write_run(runs_dir, "20261107T110000-live01", "q", _HALF)     # 没有 run_end
    d = orchestrate.live()
    assert d["run"]["running"] is True
    st = {n["id"]: n["status"] for n in d["nodes"]}
    assert st["draft"] == "ok"
    assert st["verify"] == "running"
    assert st["critique"] == "running"
    assert st["gate"] == "pending"          # 还没轮到，不能跟着变蓝
    assert sorted(d["summary"]["running"]) == ["critique", "verify"]


def test_图上的快照也必须是新状态(runs_dir):
    # DAG 读 graph.nodes、分层列表读 nodes：只改一边，屏上就自相矛盾
    _write_run(runs_dir, "20261107T110001-live02", "q", _HALF)
    d = orchestrate.live()
    gs = {n["id"]: n["status"] for n in d["graph"]["nodes"]}
    assert gs["verify"] == "running"
    assert gs["gate"] == "pending"
    assert [n["id"] for n in d["graph"]["nodes"] if n["status"] == "running"] == \
           [n["id"] for n in d["nodes"] if n["status"] == "running"]


def test_跑完之后一个进行中都不许剩(runs_dir):
    _write_run(runs_dir, "20261107T110002-live03", "q", _HALF, tail=True)
    d = orchestrate.live()
    assert d["run"]["running"] is False
    assert d["summary"]["running"] == []
    assert all(n["status"] != "running" for n in d["nodes"])


def test_打回重跑认回边目标而不是推下一层(runs_dir):
    # 回边目标第一轮已经写过 graph_node，靠"没写过"推不出来，
    # 必须靠 diagnosis + back_edges 才认得出它正在重做。
    _write_run(runs_dir, "20261107T110003-live04", "q", _HALF + [
        ("scheduler_done", {"job": "verify", "status": "ok", "duration_ms": 13.7}),
        ("scheduler_done", {"job": "critique", "status": "fail", "duration_ms": 900.0}),
        ("graph_node", {"node": "verify", "agent": "verifier", "status": "ok",
                        "visit": 0, "duration_ms": 13.7}),
        ("graph_node", {"node": "critique", "agent": "Critic", "status": "fail",
                        "visit": 0, "duration_ms": 900.0}),
        ("diagnosis", {"attempt": 1, "kind": "semantic", "label": "语义质检打回",
                       "strategy": "rewrite", "signals": ["口径不一致"]}),
        ("graph_loop", {"edge": "gate->draft", "attempt": 1, "max_loops": 2,
                        "cleared": ["draft", "verify", "critique", "gate"],
                        "reason": "fail"}),
        ("scheduler_start", {"job": "draft", "priority": 60, "waited_ms": 0.0,
                             "deadline_ms": 120000.0, "slot": "1/2"}),
    ])
    d = orchestrate.live()
    st = {n["id"]: n["status"] for n in d["nodes"]}
    assert st["draft"] == "retry"           # 不是 running，也不能是 pending
    assert st["gate"] == "pending"          # 不许把下一层也说成在跑
    assert d["summary"]["running"] == ["draft"]
    assert d["summary"]["retry_node"] == "draft"
    assert d["summary"]["round"] == 2


def test_第二轮每个节点都带轮次(runs_dir):
    _write_run(runs_dir, "20261107T110004-live05", "q", [
        ("diagnosis", {"attempt": 1, "kind": "semantic", "label": "打回",
                       "strategy": "rewrite", "signals": []}),
        ("graph_loop", {"edge": "gate->draft", "attempt": 1, "max_loops": 2,
                        "cleared": ["draft"], "reason": "fail"}),
    ])
    d = orchestrate.live()
    assert {n["round"] for n in d["nodes"]} == {2}


def test_实时口与静态口是同一份图(runs_dir):
    _write_run(runs_dir, "20261107T110005-live06", "q", _HALF)
    a = orchestrate.build()
    b = orchestrate.live()
    assert a["graph"]["name"] == b["graph"]["name"]
    assert a["graph"]["forward_edges"] == b["graph"]["forward_edges"]
    assert a["graph"]["back_edges"] == b["graph"]["back_edges"]
    assert [n["id"] for n in a["nodes"]] == [n["id"] for n in b["nodes"]]


def test_只写了run头时点亮入口节点而不是整屏抢跑(runs_dir):
    # 只写了 run 头：谁都还没跑。屏上该点亮的是入口层（draft），
    # 而不是把六个节点都当成在跑 —— 那样一进页面就"全都完了"。
    _write_run(runs_dir, "20261107T110006-live07", "q", [])
    d = orchestrate.live()
    assert d["run"]["running"] is True
    assert d["summary"]["running"] == ["draft"]
    assert [n["status"] for n in d["nodes"] if n["id"] != "draft"] == \
           ["pending"] * 5


def test_draft不进调度器也要算在跑(runs_dir):
    # draft 走 execute()，不经过 Scheduler，所以没有 scheduler_start，
    # 只有 node_begin。不认这条，屏上要等它跑完才亮 —— 恰好是最长的一步。
    _write_run(runs_dir, "20261107T110007-live08", "q", [
        ("draft_prompt", {"injected_chars": 2100}),
        ("node_begin", {"node": "draft", "agent": "Analyst", "priority": 60}),
    ])
    d = orchestrate.live()
    assert d["summary"]["running"] == ["draft"]
    st = {n["id"]: n["status"] for n in d["nodes"]}
    assert st["draft"] == "running" and st["verify"] == "pending"


def test_节点收尾之后就不再算在跑(runs_dir):
    _write_run(runs_dir, "20261107T110008-live09", "q", [
        ("node_begin", {"node": "draft", "agent": "Analyst"}),
        ("node_end", {"node": "draft", "status": "ok", "duration_ms": 2400.0}),
        ("graph_node", {"node": "draft", "agent": "Analyst", "status": "ok",
                        "visit": 0, "duration_ms": 2400.0}),
    ])
    d = orchestrate.live()
    # draft 收了，就轮到下一层（verify / critique 同层）——
    # 屏上不能停在"draft 还在跑"，也不能整屏空着没人亮
    assert sorted(d["summary"]["running"]) == ["critique", "verify"]
    assert {n["status"] for n in d["nodes"] if n["attempts"]} == {"ok"}


def test_实时口找不到运行也要说人话(runs_dir):
    d = orchestrate.live("../../app")
    assert d["ok"] is False and "不合法" in d["error"]


def test_同一个run被追加第二遍时屏要接着跟(runs_dir):
    # 真实踩到的坑：同一个 run_id 先跑完（文件里已有 run_end），
    # 再拿同一个 id 提第二个问题，事件是往后追加的。
    # 只要认"有 run_end 就算结束"，第二个问题在屏上就完全看不见。
    path = _write_run(runs_dir, "20261107T110009-live10", "q", _HALF, tail=True)
    d = orchestrate.live()
    assert d["run"]["running"] is False        # 第一遍确实结束了

    # 追加第二遍：run 头 + 事件（时间比上面那条 run_end 晚）
    with path.open("a", encoding="utf-8") as fp:
        fp.write("\n" + json.dumps(dict(type="run", run_id="20261107T110009-live10",
                                        question="q2", source="test"),
                                   ensure_ascii=False))
        fp.write("\n" + json.dumps(dict(type="event", seq=1,
                                        at="2026-11-07T10:00:09.000",
                                        event="node_begin",
                                        detail={"node": "draft", "agent": "Analyst"}),
                                   ensure_ascii=False))
    d = orchestrate.live()
    assert d["run"]["running"] is True
    assert d["summary"]["running"] == ["draft"]


def test_追加写之后又结束的run不许假装还在跑(runs_dir):
    path = _write_run(runs_dir, "20261107T110010-live11", "q", _HALF, tail=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write("\n" + json.dumps(dict(type="event", seq=1,
                                        at="2026-11-07T10:00:09.000",
                                        event="node_begin",
                                        detail={"node": "draft", "agent": "Analyst"}),
                                   ensure_ascii=False))
        fp.write("\n" + json.dumps(dict(type="run_end", run_id="20261107T110010-live11",
                                        status="ok",
                                        ended_at="2026-11-07T10:00:20.000"),
                                   ensure_ascii=False))
    d = orchestrate.live()
    assert d["run"]["running"] is False
    assert d["summary"]["running"] == []


# ---------------------------------------------------------------- W3-04 节点抽屉

def _write_graph_run(tmp_path, run_id, node, payload):
    """按 core/graph.py checkpoint 的真实写法造一份节点存档。"""
    path = tmp_path.parent / "graph_runs"
    path.mkdir(exist_ok=True)
    fp = path / (run_id + ".json")
    data = {"run_id": run_id, "graph": "sales.answer", "version": 3,
            "updated_at": "2026-11-07T10:00:02.000",
            "results": {node: {"id": node, "agent": "Analyst", "action": node,
                               "label": node, "status": "ok", "visit": 1,
                               "started_ms": 0.0, "ended_ms": 1800.0,
                               "duration_ms": 1800.0, "payload": payload}},
            "visits": {node: 1}}
    fp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return fp


@pytest.fixture
def graph_dir(tmp_path, monkeypatch):
    d = tmp_path.parent / "graph_runs"
    monkeypatch.setattr(orchestrate, "GRAPH_RUNS_DIR", d)
    return d


def test_抽屉能当面读出第几个agent第几次执行花了多少毫秒(runs_dir, graph_dir):
    # 验收口径（W3-04）：能当面读出「第 3 个 Agent 在第 2 次重试时
    # 花了 1.8 秒」，且界面上有对应数字 —— 数据必须在接口里就齐。
    _write_graph_run(runs_dir, "20261107T110020-dw01", "draft",
                     {"chars": 38, "tools": ["query_stats"]})
    _write_run(runs_dir, "20261107T110020-dw01", "q", [
        ("node_begin", {"node": "draft", "agent": "Analyst", "visit": 1}),
        ("node_end", {"node": "draft"}),
        ("graph_node", {"node": "draft", "agent": "Analyst", "status": "ok",
                        "visit": 1, "started_ms": 0.0, "ended_ms": 1800.0,
                        "duration_ms": 1800.0}),
        ("diagnosis", {"attempt": 1, "kind": "metric", "label": "口径错",
                       "strategy": "recheck_caliber",
                       "why": "质检员指出了口径/单位问题"}),
        ("graph_loop", {"edge": "gate->draft", "attempt": 1, "max_loops": 2}),
        ("node_begin", {"node": "draft", "agent": "Analyst", "visit": 2}),
        ("node_end", {"node": "draft"}),
        ("graph_node", {"node": "draft", "agent": "Analyst", "status": "ok",
                        "visit": 2, "started_ms": 2000.0, "ended_ms": 3800.0,
                        "duration_ms": 1800.0}),
    ], tail=True)
    d = orchestrate.node_detail("20261107T110020-dw01", "draft")
    assert d["ok"] is True
    n = d["node"]
    assert n["order"] == 1 and "第 1 个" in n["order_text"]
    assert n["visits"] == 2 and "第 2 次" in n["visit_text"]
    assert n["duration_ms"] == 1800.0
    assert d["retry"]["count"] == 1
    assert "口径/单位问题" in d["retry"]["reason"]
    assert d["retry"]["strategy"] == "recheck_caliber"
    assert "产出 38 字" in " ".join(n["output"])


def test_抽屉的token按agent归集并按轮拆开(runs_dir, graph_dir):
    # step 行里 agent=Analyst 的两条：第 0 轮两次调用、第 1 轮一次。
    # 归到 draft 头上必须是 3 次调用，且能按轮拆 —— 抽屉的表格靠它。
    _write_graph_run(runs_dir, "20261107T110021-dw02", "draft", {})
    path = _write_run(runs_dir, "20261107T110021-dw02", "q", [], tail=True)
    with path.open("a", encoding="utf-8") as fp:
        for rec in [
            dict(type="step", seq=1, agent="Analyst", name="chat_model",
                 status="ok", started_at="2026-11-07T10:00:00.000",
                 duration_ms=0.1, retry=0, input="", output="",
                 tokens={"in": 500, "out": 83}, error=None, meta={}),
            dict(type="step", seq=2, agent="Critic", name="chat_model",
                 status="ok", started_at="2026-11-07T10:00:01.000",
                 duration_ms=0.2, retry=0, input="", output="",
                 tokens={"in": 320, "out": 53}, error=None, meta={}),
            dict(type="step", seq=3, agent="Analyst", name="chat_model",
                 status="ok", started_at="2026-11-07T10:00:02.000",
                 duration_ms=0.1, retry=1, input="", output="",
                 tokens={"in": 800, "out": 133}, error=None, meta={}),
            dict(type="run_end", run_id="20261107T110021-dw02", status="ok",
                 at="2026-11-07T10:00:03.000",
                 ended_at="2026-11-07T10:00:03.000",
                 duration_ms=1, step_count=3, retry_count=0),
        ]:
            fp.write("\n" + json.dumps(rec, ensure_ascii=False))
    d = orchestrate.node_detail("20261107T110021-dw02", "draft")
    n = d["node"]
    assert n["tokens"]["calls"] == 2
    assert n["tokens"]["in"] == 1300            # 500 + 800，Critic 的不算
    assert n["tokens"]["by_retry"][0]["in"] == 500
    assert n["tokens"]["by_retry"][1]["in"] == 800
    assert n["run_tokens"]["in"] == 1620        # 全次运行含 Critic


def test_抽屉拒绝非法run_id与未知节点(runs_dir, graph_dir):
    assert orchestrate.node_detail("../app", "draft")["ok"] is False
    _write_run(runs_dir, "20261107T110022-dw03", "q", [], tail=True)
    d = orchestrate.node_detail("20261107T110022-dw03", "nope")
    assert d["ok"] is False and "没有节点" in d["error"]


def test_抽屉的事件流只含这一节点的痕迹(runs_dir, graph_dir):
    _write_graph_run(runs_dir, "20261107T110023-dw04", "draft", {})
    _write_run(runs_dir, "20261107T110023-dw04", "q", [
        ("node_begin", {"node": "draft"}),
        ("graph_node", {"node": "draft", "status": "ok"}),
        ("node_begin", {"node": "verify"}),
        ("graph_node", {"node": "verify", "status": "ok"}),
    ], tail=True)
    d = orchestrate.node_detail("20261107T110023-dw04", "draft")
    evs = d["events"]
    assert evs and all((e["detail"] or {}).get("node") == "draft" for e in evs)


# ---------------------------------------------------------------- W3-05 回放

def _write_run_t(runs_dir, run_id, frames, tail=None, started="2026-11-07T10:00:00.000"):
    """带递增时间的事件流：回放靠 t_ms 排帧，同一个时刻就排不出先后。"""
    from datetime import datetime, timedelta
    base = datetime.fromisoformat(started)
    lines = [dict(type="run", run_id=run_id, question="q", source="test",
                  started_at=started)]
    for i, (name, detail) in enumerate(frames, start=1):
        at = (base + timedelta(milliseconds=i * 10)).isoformat()
        lines.append(dict(type="event", seq=i, at=at, event=name, detail=detail))
    if tail:
        lines.append(dict(type="run_end", run_id=run_id, status="ok",
                          at=tail, ended_at=tail, duration_ms=100.0))
    path = runs_dir / (run_id + ".jsonl")
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines),
                    encoding="utf-8")
    return path


def test_回放一次给全且帧带时间(runs_dir):
    _write_run_t(runs_dir, "20261110T000001-rp01", [
        ("node_begin", {"node": "draft"}),
        ("graph_node", {"node": "draft", "status": "ok"}),
        ("graph_loop", {"edge": "gate->draft", "attempt": 1}),
        ("node_begin", {"node": "draft"}),
        ("graph_node", {"node": "draft", "status": "ok"}),
    ], tail="2026-11-07T10:00:00.060")
    d = orchestrate.replay("20261110T000001-rp01")
    assert d["ok"] is True
    assert d["run"]["frame_count"] == 5
    assert d["run"]["status"] == "ok"
    # 帧时间递增：10ms、20ms、30ms…
    ts = [f["t_ms"] for f in d["frames"]]
    assert ts == [10.0, 20.0, 30.0, 40.0, 50.0]
    # 刻度把打回点标出来，现场要反复看的就是这里
    assert any(m["kind"] == "loop" and "gate->draft" in m["text"]
               for m in d["marks"])


def test_回放帧快照反映节点状态推进(runs_dir):
    _write_run_t(runs_dir, "20261110T000002-rp02", [
        ("graph_node", {"node": "draft", "status": "fail"}),
        ("graph_loop", {"edge": "gate->draft", "attempt": 1}),
        ("graph_node", {"node": "draft", "status": "ok"}),
        ("graph_node", {"node": "curate", "status": "skip"}),
    ], tail="2026-11-07T10:00:00.050")
    d = orchestrate.replay("20261110T000002-rp02")
    # 第 0 帧不算变化（一上来全 pending 不是演出来的事）
    assert d["frames"][0]["changed"] == {}
    # 第 2 帧（index=2）draft 变 ok
    assert d["frames"][2]["changed"] == {"draft": "ok"}
    # 末帧 curate 变 skip
    assert d["frames"][3]["changed"] == {"curate": "skip"}


def test_回放拒绝非法run_id与未知run(runs_dir):
    assert orchestrate.replay("../app")["ok"] is False
    assert orchestrate.replay("")["ok"] is False
    assert orchestrate.replay("20261110T000003-no40")["ok"] is False


def test_回放对没写run_end的run也能给脚本(runs_dir):
    """追加重跑的文件里有多条 run_end / 没收尾都能出帧：回放不挑记录的完整性。"""
    _write_run_t(runs_dir, "20261110T000004-rp04", [
        ("graph_node", {"node": "draft", "status": "ok"}),
    ])
    d = orchestrate.replay("20261110T000004-rp04")
    assert d["ok"] is True and d["run"]["finished"] is False
    assert d["run"]["status"] == "running"       # 没收尾就如实说还在跑
