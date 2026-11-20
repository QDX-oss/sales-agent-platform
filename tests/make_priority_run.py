# -*- coding: utf-8 -*-
"""
W2-01 演示：跑一次真的带优先级与 deadline 的调度，把轨迹留在 runs/ 里。

跑法（项目根目录）：
    python tests/make_priority_run.py

它不联网、不碰模型：节点动作是 sleep，但**调度、轨迹、checkpoint、计时**
全部是真的。这条演示想让人看到三件事：

    1. 两个槽位：verify 与 critique 同时开跑，时间区间重叠
    2. verify 卡住 → 到 120ms 判超时、让出槽位；gate 不等它那 400ms
    3. 排队顺序按 priority 写进轨迹，不靠线程运气

输出的 gantt 是给人看的，runs/{run_id}.jsonl 是给机器看的。变更单里
引用的每个数字都从这一次运行来。
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.graph import OK, Graph, Runner                       # noqa: E402
from core.trace import Trace, load_run                          # noqa: E402

# 与真实流水线同构的一份图：取数 -> (回查 || 质检) -> 合议
SPEC = {
    "name": "sales.answer.demo",
    "version": 1,
    "nodes": [
        {"id": "draft", "agent": "Analyst", "action": "draft", "label": "取数出稿",
         "priority": 100},
        {"id": "verify", "agent": "verifier", "action": "verify", "needs": ["draft"],
         "label": "数字回查", "priority": 90, "deadline_ms": 120},
        {"id": "critique", "agent": "Critic", "action": "critique", "needs": ["draft"],
         "label": "语义质检", "priority": 80},
        {"id": "gate", "agent": "gate", "action": "gate", "kind": "gate",
         "needs": ["verify", "critique"], "run_if": "always", "label": "合议",
         "priority": 70},
    ],
    "edges": [],
}


def sleepy(ms, status=OK, payload=None):
    def fn(ctx):
        time.sleep(ms / 1000.0)
        return status, (payload or {})
    return fn


def gantt(nodes, width=64):
    """把每个节点的时间区间画成一行。"""
    span = max(n["ended_ms"] for n in nodes) or 1
    lines = []
    for n in sorted(nodes, key=lambda x: (x["started_ms"], x["id"])):
        a = int(n["started_ms"] / span * width)
        b = max(a + 1, int(n["ended_ms"] / span * width))
        bar = " " * a + "#" * (b - a)
        lines.append("  %-9s %s %s  %s" % (
            n["id"], bar.ljust(width),
            ("%6.0f–%-6.0f ms" % (n["started_ms"], n["ended_ms"])).rjust(18),
            n["status"]))
    return "\n".join(lines)


def main():
    trace = Trace(question="W2-01 演示：优先级与超时让位", source="demo")
    execs = {
        "draft": sleepy(60, payload={"chars": 120}),
        "verify": sleepy(400, payload={"checked": 6}),   # 故意卡住，120ms 时会被判超时
        "critique": sleepy(100, payload={"high": 0}),
        "gate": sleepy(10, payload={"action": "give_up"}),
    }

    runner = Runner(Graph.from_dict(SPEC), execs, trace=trace,
                    checkpoint_dir=ROOT / "graph_runs", slots=2)

    t0 = time.perf_counter()
    plan = runner.run({"question": "W2-01 演示"})
    wall = (time.perf_counter() - t0) * 1000
    summary = trace.finish()

    print("run_id   :", trace.run_id)
    print("轨迹文件 :", summary["path"])
    print("总墙钟   : %.0f ms（若串行执行：60+400+100+10 = 570 ms）" % wall)
    print("槽位数   : 2")
    print("\n甘特（# = 正在执行；verify 的条到 120ms 就断了 —— 那是被判超时让位）")
    print(gantt(plan["nodes"]))

    print("\n调度账（按提交顺序 = 优先级顺序）：")
    for s in plan["schedule"]:
        print("  %-9s pri=%-4s pos=%s  waited=%6.1fms  ran=%6.1fms  %-8s %s"
              % (s["id"], s["priority"], s["queue_pos"], s["waited_ms"],
                 s["duration_ms"], s["status"],
                 ("reason=" + s["reason"]) if s["reason"] else ""))

    print("\n结论：")
    print("  超时节点      :", plan["timed_out"])
    print("  未通过         :", plan["not_passed"])
    print("  下游被跳过     :", plan["skipped"])

    saved = load_run(trace.run_id)
    print("\n轨迹里的调度事件（按发生顺序）：")
    for e in saved["events"]:
        if e["event"].startswith("scheduler_") or e["event"] in ("graph_wave", "node_timeout"):
            print("  seq=%-3s %-18s %s" % (e["seq"], e["event"],
                                           json.dumps(e["detail"], ensure_ascii=False)[:120]))

    ckpt = ROOT / "graph_runs" / (trace.run_id + ".json")
    print("\ncheckpoint:", ckpt, "存在" if ckpt.exists() else "缺失")
    if ckpt.exists():
        data = json.loads(ckpt.read_text(encoding="utf-8"))
        print("  results 里的状态：",
              {k: v["status"] for k, v in data["results"].items()})

    print("\n给变更单用的数据：")
    print(json.dumps({
        "run_id": trace.run_id,
        "wall_ms": round(wall, 1),
        "serial_ms": 570,
        "nodes": [{k: n[k] for k in ("id", "status", "priority", "started_ms",
                                     "ended_ms", "duration_ms")}
                  for n in plan["nodes"]],
        "timed_out": plan["timed_out"],
        "skipped": plan["skipped"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
