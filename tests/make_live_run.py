# -*- coding: utf-8 -*-
"""W3-03 演示：跑一次**看得见过程**的运行，让编排屏上的节点一个个变色。

跑法（项目根目录，另开一个终端）：
    python tests/make_live_run.py            # 每步 3 秒，约 20 秒跑完
    python tests/make_live_run.py --fast     # 每步 0.5 秒，约 4 秒

它不联网、不碰模型：节点动作是 sleep，但**调度、轨迹、checkpoint、计时**
全是真的 —— 编排屏读的就是这份 jsonl，和真实提问写的是同一个格式。

为什么要有这个脚本：真实提问 5 秒就跑完了，截图截不到"一半绿一半灰"的
中间态。这个脚本的价值就是把这 5 秒拉长到 20 秒，让「延迟 < 2s」这条
验收口径可以被人眼直接看到，而不是靠我口头保证。
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.graph import OK, Graph, Runner                       # noqa: E402
from core.scheduler import Scheduler                            # noqa: E402
from core.team import pipeline                                  # noqa: E402
from core.trace import Trace, load_run                          # noqa: E402

FAST = "--fast" in sys.argv
STEP_MS = 500 if FAST else 3000


def spec():
    """直接复用真实流水线：演示的必须是那张真的图，不是另画一张。"""
    g = pipeline()
    for n in g["nodes"]:
        n["priority"] = n.get("priority", 50)
    return g


def main():
    trace = Trace(question="【演示】哪个省销售额最高？（慢速可视化运 run）",
                  source="demo-live")
    print("run_id =", trace.run_id)
    print("打开编排屏并选这一次运行：")
    print("    http://127.0.0.1:5000/orchestration?run_id=%s" % trace.run_id)
    print("每步 %.1fs，共 6 个节点，约 %.0fs 跑完。Ctrl+C 可中断。"
          % (STEP_MS / 1000.0, STEP_MS * 6 / 1000.0))
    sys.stdout.flush()

    def make(node):
        def fn(state):
            time.sleep(STEP_MS / 1000.0)
            return OK, {"node": node.id, "note": "演示用：耗时是人为拉长的"}
        return fn

    graph = Graph.from_dict(spec())
    executors = {n.action: make(n) for n in graph.nodes}
    # 两槽位：verify / critique 同批并发，能同时看到两个节点是"在跑"的蓝色；
    # deadline 给足，这次演示不演超时（超时另有 make_priority_run.py）。
    runner = Runner(graph, executors, trace=trace, slots=2,
                    default_deadline_ms=STEP_MS * 5)
    try:
        runner.run({"question": "演示"})
        trace.finish(status="ok")
        print("\n跑完了。看看编排屏上是不是 6 个节点全绿：")
        print("    http://127.0.0.1:5000/orchestration?run_id=%s" % trace.run_id)
        run = load_run(trace.run_id)
        print("jsonl 事件数 =", len(run.get("events") or []))
    except KeyboardInterrupt:
        trace.finish(status="failed", error={"type": "Interrupted",
                                             "message": "演示被手动中断"})
        print("\n已中断。这次运行在屏上会停在「跑了一半」的样子 —— "
              "刚好可以用来讲「中途失败长什么样」。")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
