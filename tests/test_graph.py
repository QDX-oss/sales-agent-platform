# -*- coding: utf-8 -*-
"""
W1-02 验收：任务图。

跑法（项目根目录）：
    python tests/test_graph.py

不联网、不碰 CSV、不碰模型。这一组只测图这一层：条件边、并行、断点续跑、非法定义。

要回答的三个问题，每个都对应一条台账验收口径：

    1. 删掉那条回边，重试是不是真的没有了？
       —— 不是"代码里注释掉了"，是同一条执行路径只出稿一次。
    2. 并行节点的耗时区间是不是真的重叠？
       —— 拿真实时间戳比，并留一个 parallel=False 的对照，证明这不是度量错觉。
    3. 中途崩了，能不能从断点接着跑、且不重复花钱？
       —— 重启后 work 节点只执行过一次。
"""

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.graph import (ERROR, FAIL, OK, SKIP, Graph, NodeFailed, Runner)  # noqa: E402
from core.team import MAX_DRAFT_RETRY, SALES_PIPELINE, pipeline           # noqa: E402
from core.trace import Trace, load_run                                    # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  → " + detail) if detail else ""))


def spec(with_edge=True):
    """一张迷你图：干活 -> 检查 -> 合议 ->(回边) 干活 / 发布。"""
    nodes = [
        {"id": "work", "agent": "A", "action": "work"},
        {"id": "check", "agent": "V", "action": "check", "needs": ["work"]},
        {"id": "gate", "agent": "G", "action": "gate", "needs": ["check"],
         "run_if": "always", "kind": "gate"},
        {"id": "publish", "agent": "C", "action": "publish", "needs": ["gate"]},
    ]
    edges = []
    if with_edge:
        edges.append({"from": "gate", "to": "work", "when": "fail",
                      "max_loops": 1, "label": "打回重做"})
    return {"name": "mini", "nodes": nodes, "edges": edges}


def counting_executors(calls, good_after=2):
    """work 计数；gate 在第 good_after 次之后才放行。"""
    def work(ctx):
        calls["work"] = calls.get("work", 0) + 1
        return OK, {"n": calls["work"]}

    def check_fn(ctx):
        return OK, {}

    def gate(ctx):
        passed = calls.get("work", 0) >= good_after
        return (OK if passed else FAIL), {"action": "pass" if passed else "retry"}

    def publish(ctx):
        calls["publish"] = calls.get("publish", 0) + 1
        return OK, {}

    return {"work": work, "check": check_fn, "gate": gate, "publish": publish}


def main():
    # ------------------------------------------------------------ 1
    print("\n[1] 回边是数据：删掉它，重试就真的没有了")

    calls = {}
    r = Runner(Graph.from_dict(spec(True)), counting_executors(calls))
    plan = r.run({})
    check("有回边：work 执行 2 次（初稿 + 打回重做）", calls.get("work") == 2, str(calls))
    check("有回边：publish 执行 1 次（第二次合议放行）", calls.get("publish") == 1, str(calls))
    check("有回边：loops 记录 1 次", plan["loops"] == {"gate->work": 1}, str(plan["loops"]))
    check("有回边：work 的 visit 递增", plan["nodes"][0]["visit"] == 1,
          str(plan["nodes"][0]["visit"]))
    check("有回边：全部节点都跑过", plan["completed"] is True)

    calls = {}
    r = Runner(Graph.from_dict(spec(False)), counting_executors(calls))
    plan = r.run({})
    check("删掉回边：work 只执行 1 次", calls.get("work") == 1, str(calls))
    check("删掉回边：publish 被跳过（合议未通过）",
          [n["status"] for n in plan["nodes"] if n["id"] == "publish"] == [SKIP])
    check("删掉回边：loops 为空", plan["loops"] == {}, str(plan["loops"]))
    check("同一份执行器 + 两份图 = 两种流程（代码没动）", True)

    # ------------------------------------------------------------ 2
    print("\n[2] 并行：同时开跑的节点，时间区间真的重叠")

    def slow(sleep):
        def fn(ctx):
            time.sleep(sleep)
            return OK, {}
        return fn

    fan = {"name": "fan", "nodes": [
        {"id": "start", "action": "slow"},
        {"id": "left", "action": "slow", "needs": ["start"]},
        {"id": "right", "action": "slow", "needs": ["start"]},
        {"id": "join", "action": "slow", "needs": ["left", "right"]},
    ], "edges": []}

    def run_fan(parallel):
        trace = Trace(question="并行测试", source="selftest")
        r = Runner(Graph.from_dict(fan), {"slow": slow(0.15)}, trace=trace,
                   parallel=parallel)
        t0 = time.perf_counter()
        plan = r.run({})
        wall = (time.perf_counter() - t0) * 1000
        trace.finish()
        return plan, wall, trace

    # 同一张图跑两遍：一遍并行、一遍串行。用两者的比值判断，不用绝对毫秒 ——
    # 绝对阈值会随机器负载漂移，比值是本进程内自己跟自己比，稳定得多。
    p_ser, wall_ser, _ = run_fan(parallel=False)
    p_par, wall_par, trace = run_fan(parallel=True)

    left = [n for n in p_par["nodes"] if n["id"] == "left"][0]
    right = [n for n in p_par["nodes"] if n["id"] == "right"][0]
    overlap = left["started_ms"] < right["ended_ms"] and right["started_ms"] < left["ended_ms"]
    check("left / right 区间重叠", overlap,
          "left=[%s,%s] right=[%s,%s]" % (left["started_ms"], left["ended_ms"],
                                          right["started_ms"], right["ended_ms"]))
    # 判据从"整张图 并行/串行 的墙钟比"换成"这一批并行节点自己的跨度"。
    # 原因是前者里掺了两个单节点批次的开销（sleep 粒度、trace 落盘、checkpoint），
    # 跟并行没关系，跑几轮就会在 85% 上下抖；后者理想值 50%、门槛 60%，
    # 比原来更严，也更贴近"并行"这个词本身。
    two_span = max(left["ended_ms"], right["ended_ms"]) - min(left["started_ms"],
                                                              right["started_ms"])
    two_sum = left["duration_ms"] + right["duration_ms"]
    check("left/right 的墙钟跨度 < 两者耗时之和的 60%",
          two_span < two_sum * 0.6,
          "跨度 %.0fms / 之和 %.0fms / 占比 %.0f%%"
          % (two_span, two_sum, two_span / two_sum * 100))
    check("对照：并行整图墙钟短于串行",
          wall_par < wall_ser,
          "并行 %.0fms / 串行 %.0fms = %.0f%%"
          % (wall_par, wall_ser, wall_par / wall_ser * 100))
    check("join 在两个并行节点都结束之后才开始",
          [n for n in p_par["nodes"] if n["id"] == "join"][0]["started_ms"]
          >= max(left["ended_ms"], right["ended_ms"]))

    saved = load_run(trace.run_id)
    waves = [e for e in saved["events"] if e["event"] == "graph_wave"]
    check("轨迹里记下了「哪几个节点同批并发」",
          any(set(w["detail"]["nodes"]) == {"left", "right"} for w in waves),
          str([w["detail"]["nodes"] for w in waves]))
    seqs = [e["seq"] for e in saved["events"]]
    check("两个线程同时写轨迹，seq 仍然无重复（并发分配序号已加锁）",
          len(seqs) == len(set(seqs)) and seqs == sorted(seqs),
          "seq=%s" % seqs)

    # 三支并行：台账 W1-04 的口径是"总耗时 < 三者之和的 60%"
    tri = {"name": "tri", "nodes": [
        {"id": "root", "action": "slow"},
        {"id": "a", "action": "slow", "needs": ["root"]},
        {"id": "b", "action": "slow", "needs": ["root"]},
        {"id": "c", "action": "slow", "needs": ["root"]},
        {"id": "end", "action": "slow", "needs": ["a", "b", "c"]},
    ], "edges": []}
    r3 = Runner(Graph.from_dict(tri), {"slow": slow(0.15)})
    p3 = r3.run({})
    branches = [n for n in p3["nodes"] if n["id"] in ("a", "b", "c")]
    branch_sum = sum(n["duration_ms"] for n in branches)
    # 量的是"这三支占用的墙钟跨度"，不含它们共同的上游 root 和下游 end ——
    # 把 root/end 也算进去，测出来的就是整条流程，不是并行本身。
    span = max(n["ended_ms"] for n in branches) - min(n["started_ms"] for n in branches)
    check("三支并行：墙钟跨度 < 三支之和的 60%",
          span < branch_sum * 0.6,
          "跨度 %.0fms / 三支之和 %.0fms / 占比 %.0f%%"
          % (span, branch_sum, span / branch_sum * 100))
    # "同时开始"不用 ±30ms 这种阈值，直接用交叠判断：三支存在共同重叠时刻
    check("三支存在共同重叠区间（真正同时在做）",
          max(n["started_ms"] for n in branches) < min(n["ended_ms"] for n in branches),
          "共同重叠 %.1fms"
          % (min(n["ended_ms"] for n in branches) - max(n["started_ms"] for n in branches)))

    left2 = [n for n in p_ser["nodes"] if n["id"] == "left"][0]
    right2 = [n for n in p_ser["nodes"] if n["id"] == "right"][0]
    check("对照：parallel=False 时区间不重叠",
          left2["ended_ms"] <= right2["started_ms"],
          "left.end=%s right.start=%s" % (left2["ended_ms"], right2["started_ms"]))

    # ------------------------------------------------------------ 3
    print("\n[3] 断点续跑：崩在第 3 个节点，重启后从第 3 个继续")

    # 台账 W1-05 的口径：手动在第 3 节点抛异常，重启后从第 3 节点继续，前 2 节点不重复计费。
    # 所以这里用一条四段直线图，崩溃点正好是第三个节点。
    linear = {"name": "linear", "nodes": [
        {"id": "n1", "action": "step"},
        {"id": "n2", "action": "step", "needs": ["n1"]},
        {"id": "n3", "action": "step", "needs": ["n2"]},
        {"id": "n4", "action": "step", "needs": ["n3"]},
    ], "edges": []}

    tmp = Path(tempfile.mkdtemp(prefix="graph_ckpt_"))
    calls = {}
    crash_once = {"armed": True}

    def step(ctx):
        nid = ctx.node.id
        calls[nid] = calls.get(nid, 0) + 1
        if nid == "n3" and crash_once["armed"]:
            crash_once["armed"] = False
            raise ConnectionError("模拟：跑到第 3 个节点时进程被杀")
        return OK, {"n": calls[nid]}

    rid = "resume-demo"
    runner = Runner(Graph.from_dict(linear), {"step": step},
                    checkpoint_dir=tmp, run_id=rid)
    raised = None
    try:
        runner.run({})
    except NodeFailed as exc:
        raised = exc
    check("失败被包装成 NodeFailed 抛出", isinstance(raised, NodeFailed),
          str(raised)[:46] if raised else "无异常")
    check("崩溃前第 1、2 节点各跑过 1 次（没有为了跑到第 3 步而重跑前面的）",
          calls == {"n1": 1, "n2": 1, "n3": 1}, str(calls))

    ckpt = json.loads((tmp / (rid + ".json")).read_text(encoding="utf-8"))
    check("checkpoint 里有已完成的 n1 / n2",
          set(ckpt["results"]) == {"n1", "n2"}, str(sorted(ckpt["results"])))
    check("崩溃的 n3 没有写进 results（否则续跑会跳过它）",
          "n3" not in ckpt["results"])

    redo = {}
    runner2, state = Runner.resume(Graph.from_dict(linear), {"step": step},
                                   rid, checkpoint_dir=tmp)
    plan = runner2.run(state or {})
    # calls 是跨两次运行共用的计数器：n1/n2 仍为 1 表示没有重算
    check("续跑：n1、n2 没有重算（计数仍是 1）",
          calls.get("n1") == 1 and calls.get("n2") == 1, str(calls))
    check("续跑：从 n3 继续，并跑完 n4", calls.get("n3") == 2 and calls.get("n4") == 1, str(calls))
    check("续跑后四个节点齐全", set(plan["order"]) == {"n1", "n2", "n3", "n4"},
          str(plan["order"]))
    check("续跑后整体完成", plan["completed"] is True)

    # ------------------------------------------------------------ 4
    print("\n[4] 非法图在构造时就被拒绝，不留到跑到一半")

    bad_cases = [
        ("依赖不存在的节点",
         {"nodes": [{"id": "a", "action": "x", "needs": ["ghost"]}]}),
        ("needs 指向更晚的节点",
         {"nodes": [{"id": "a", "action": "x", "needs": ["b"]},
                    {"id": "b", "action": "x"}]}),
        ("回边指向前方",
         {"nodes": [{"id": "a", "action": "x"}, {"id": "b", "action": "x"}],
          "edges": [{"from": "a", "to": "b", "when": "fail", "max_loops": 1}]}),
        ("节点 id 重复",
         {"nodes": [{"id": "a", "action": "x"}, {"id": "a", "action": "x"}]}),
        ("when 拼错",
         {"nodes": [{"id": "a", "action": "x"}, {"id": "b", "action": "x"}],
          "edges": [{"from": "b", "to": "a", "when": "fail!!", "max_loops": 1}]}),
        ("空图",
         {"nodes": []}),
    ]
    for name, bad in bad_cases:
        try:
            Graph.from_dict(bad)
            ok = False
        except ValueError:
            ok = True
        check("拒绝：" + name, ok)

    try:
        Runner(Graph.from_dict({"nodes": [{"id": "a", "action": "no_such"}]}), {})
        ok = False
    except ValueError:
        ok = True
    check("拒绝：动作没有对应执行器", ok)

    # ------------------------------------------------------------ 5
    print("\n[5] 销售流水线：图就是流程，流程就是数据")

    g = Graph.from_dict(pipeline(True))
    check("默认六步、顺序固定",
          g.order() == ["draft", "verify", "critique", "gate", "curate", "final_check"],
          str(g.order()))
    check("只有一条回边，预算是 MAX_DRAFT_RETRY",
          [(e.key, e.max_loops) for e in g.edges] == [("gate->draft", MAX_DRAFT_RETRY)])
    check("回边作废范围 = 打回重做的那一圈",
          g.loop_body(g.edges[0]) == ["draft", "verify", "critique", "gate"],
          str(g.loop_body(g.edges[0])))

    g2 = Graph.from_dict(pipeline(False))
    check("关掉成文：图里直接没有 curate / final_check",
          g2.order() == ["draft", "verify", "critique", "gate"], str(g2.order()))

    verify_node = [n for n in g.nodes if n.id == "verify"][0]
    critique_node = [n for n in g.nodes if n.id == "critique"][0]
    check("verify 与 critique 互不依赖（这就是并行的依据）",
          verify_node.needs == critique_node.needs == ("draft",))
    gate_node = [n for n in g.nodes if n.id == "gate"][0]
    check("合议节点 run_if=always（否则回查一失败就没人合议了）",
          gate_node.run_if == "always")

    check("SALES_PIPELINE 可以被 json 序列化（改流程 = 改这份 dict）",
          json.dumps(SALES_PIPELINE, ensure_ascii=False) != "")

    # ------------------------------------------------------------ 汇总
    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print("\n" + "=" * 62)
    print("结果：%d / %d 通过" % (passed, total))
    if passed < total:
        print("  失败： " + "、".join(n for n, ok in results if not ok))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
