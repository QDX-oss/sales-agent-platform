# -*- coding: utf-8 -*-
"""
W2-01 验收：优先级调度 + deadline 超时让位。

跑法（项目根目录）：
    python tests/test_scheduler.py

不联网、不碰 CSV、不碰模型。这一组只回答台账 W2-01 那句验收：

    「同时提交高/低优先两任务，低优先的在高优先完成后才开始，trace 可证」

以及它没说出口、但不做就等于没做的三件事：

    · 超时之后槽位真的还回来了吗？（不是"标记了 timeout 就算"）
    · 被判超时的任务，迟到的结果会不会偷偷写进账本？
    · 优先级相同的时候，顺序稳不稳？（不稳 = 开发机上一套、服务器上另一套）

时间断言一律留足余量：这里量的是"先后关系"和"有没有被某个卡住的节点拖住"，
不是精确耗时。用绝对毫秒卡阈值，机器一忙就会红，那种测试最后只会被关掉。
"""

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.graph import (OK, SKIP, TIMEOUT, Graph, NodeFailed, Runner)  # noqa: E402
from core.scheduler import (ERROR, TIMEOUT as S_TIMEOUT, Scheduler,  # noqa: E402
                            apply_policy, load_policy)
from core.trace import Trace, load_run  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  → " + detail) if detail else ""))


def work(ms, tag=None, calls=None, raises=False):
    """一个会占用 ms 毫秒的任务。calls[tag] 记被真正执行过。"""
    def fn():
        if calls is not None and tag:
            calls[tag] = calls.get(tag, 0) + 1
        time.sleep(ms / 1000.0)
        if raises:
            raise ConnectionError("模拟：取数接口 500")
        return "%s-完成" % (tag or "job")
    return fn


def main():
    # ------------------------------------------------------------ 1
    print("\n[1] 台账原话：同时提交高/低优先两任务，低优先的在高优先完成后才开始")

    # 连跑三轮。一轮通过可能只是线程恰好抢到了，三轮都通过才叫"按优先级"。
    orders, gaps = [], []
    for _ in range(3):
        calls = {}
        s = Scheduler(slots=1)
        # 故意先提交低优先的：如果实现是 FIFO，它就会先跑，这条断言立刻红。
        s.submit("low", work(100, "low", calls), priority=1)
        s.submit("high", work(100, "high", calls), priority=10)
        out = {o.id: o for o in s.run()}
        orders.append([o.id for o in sorted(out.values(),
                                            key=lambda x: x.started_ms)])
        gaps.append(out["low"].started_ms - out["high"].ended_ms)

    check("三轮都是 high 先跑、low 后跑（不是靠线程运气）",
          all(o == ["high", "low"] for o in orders), str(orders))
    check("low 的开始时间不早于 high 的结束时间（槽位是真让出来的）",
          all(g >= -2 for g in gaps), "间隔 %s ms" % [round(g, 1) for g in gaps])
    check("low 的排队时长 >= high 的执行时长",
          out["low"].waited_ms >= out["high"].duration_ms - 2,
          "waited=%.0fms / high=%.0fms" % (out["low"].waited_ms,
                                          out["high"].duration_ms))
    check("提交顺序记在 queue_pos 里（low=1, high=2），执行顺序由 priority 决定",
          (out["low"].queue_pos, out["high"].queue_pos) == (1, 2),
          "pos low=%s high=%s" % (out["low"].queue_pos, out["high"].queue_pos))

    # ------------------------------------------------------------ 2
    print("\n[2] 同优先级：按提交顺序，不随线程调度漂")

    seq = []
    s = Scheduler(slots=1)
    for name in ("a", "b", "c"):
        s.submit(name, work(30, name, {}), priority=5)
    for o in s.run():
        seq.append(o.id)
    check("同优先级 = 先提交先执行", seq == ["a", "b", "c"], str(seq))

    # ------------------------------------------------------------ 3
    print("\n[3] 两个槽位：前两名先上，第三名等有人做完")

    s = Scheduler(slots=2)
    for name, pri in (("p10", 10), ("p9", 9), ("p1", 1)):
        s.submit(name, work(120, name, {}), priority=pri)
    out = {o.id: o for o in s.run()}
    first_two = sorted(["p10", "p9"], key=lambda k: out[k].started_ms)
    check("p10 / p9 都在 p1 之前开始",
          max(out["p10"].started_ms, out["p9"].started_ms) < out["p1"].started_ms,
          "p10=%.0f p9=%.0f p1=%.0f" % (out["p10"].started_ms,
                                        out["p9"].started_ms,
                                        out["p1"].started_ms))
    check("p1 等到前面某个做完才开始",
          out["p1"].started_ms >= min(out["p10"].ended_ms, out["p9"].ended_ms) - 2,
          "p1.start=%.0f / 先完成者=%.0f" % (
              out["p1"].started_ms,
              min(out["p10"].ended_ms, out["p9"].ended_ms)))
    check("两个槽位时前两名确实重叠执行",
          first_two[0] and out["p10"].started_ms < out["p9"].ended_ms
          and out["p9"].started_ms < out["p10"].ended_ms)

    # ------------------------------------------------------------ 4
    print("\n[4] 超时：判 timeout、让出槽位，后面的不用陪它等")

    calls = {}
    s = Scheduler(slots=1)
    s.submit("stuck", work(400, "stuck", calls), priority=10, deadline_ms=120)
    s.submit("quick", work(10, "quick", calls), priority=1)

    t0 = time.perf_counter()
    out = {o.id: o for o in s.run()}
    wall = (time.perf_counter() - t0) * 1000

    check("卡住的被标 timeout，原因是 deadline",
          out["stuck"].status == S_TIMEOUT and out["stuck"].reason == "deadline",
          "%s/%s" % (out["stuck"].status, out["stuck"].reason))
    check("timeout 记为 abandoned（我们不等它了，但它可能还在跑）",
          out["stuck"].abandoned is True)
    check("排在后面的任务照常完成", out["quick"].status == OK,
          out["quick"].status)
    check("后面的任务在 deadline 附近就开始了，不等满 400ms",
          out["quick"].started_ms < 300,
          "quick.start=%.0fms，卡住的任务要跑满 400ms" % out["quick"].started_ms)
    check("整批墙钟 < 卡住任务自己的耗时（槽位真的还回来了）",
          wall < 380, "整批 %.0fms / 卡住任务 400ms" % wall)
    # 等一下那个被放弃的线程，确认它跑完了也不会把结果塞回来
    time.sleep(0.35)
    check("迟到结果被丢弃：超时任务的 value 仍是 None",
          out["stuck"].value is None, repr(out["stuck"].value))
    check("超时任务的状态没有被迟到者改成 ok",
          out["stuck"].status == S_TIMEOUT, out["stuck"].status)

    # ------------------------------------------------------------ 5
    print("\n[5] 排队不吃执行时限：等得久 ≠ 做得慢")

    # 这一条是被一次真实的失败逼出来的：最早把 deadline 从"提交"起算，
    # 结果是排在慢节点后面的快节点，会因为"排了 121ms"而被判超时 ——
    # 它一行代码都没跑过。那是调度器的锅，不该算在它头上。
    calls = {}
    s = Scheduler(slots=1)
    s.submit("busy", work(250, "busy", calls), priority=10)
    s.submit("quick", work(10, "quick", calls), priority=1, deadline_ms=60)
    out = {o.id: o for o in s.run()}
    check("排队 200ms+ 的快任务照样正常完成（排队不计入 deadline）",
          out["quick"].status == OK, out["quick"].status)
    check("它的 waited_ms 确实远大于 deadline（证明它真的等久了）",
          out["quick"].waited_ms > 60,
          "waited=%.0fms / deadline=60ms" % out["quick"].waited_ms)
    check("等待不判死，但看得见：waited_ms 单独记着",
          out["quick"].waited_ms >= out["busy"].duration_ms - 5,
          "waited=%.0f / busy=%.0f" % (out["quick"].waited_ms,
                                      out["busy"].duration_ms))

    # ------------------------------------------------------------ 6
    print("\n[6] 抛异常：记 error、让槽位，不连累同批其他节点")

    s = Scheduler(slots=1)
    s.submit("boom", work(10, "boom", {}, raises=True), priority=10)
    s.submit("fine", work(10, "fine", {}), priority=1)
    out = {o.id: o for o in s.run()}
    check("异常任务记 error", out["boom"].status == ERROR, out["boom"].status)
    check("异常信息里带类型（排障要看得出是连接失败还是别的）",
          (out["boom"].error or {}).get("type") == "ConnectionError",
          str(out["boom"].error))
    check("同批另一个任务照常完成", out["fine"].status == OK)

    # ------------------------------------------------------------ 7
    print("\n[7] 接进任务图：超时节点 → timeout，下游 skip，轨迹可证")

    fan = {"name": "prio", "nodes": [
        {"id": "root", "action": "fast"},
        {"id": "slow", "action": "slow", "needs": ["root"], "priority": 10},
        {"id": "quick", "action": "fast", "needs": ["root"], "priority": 1},
        {"id": "join", "action": "fast", "needs": ["slow", "quick"]},
    ], "edges": []}

    execs = {
        "fast": lambda ctx: (OK, {"who": ctx.node.id}),
        "slow": lambda ctx: (time.sleep(0.40), (OK, {}))[1],
    }

    trace = Trace(question="W2-01 超时让位", source="selftest")
    tmp = Path(tempfile.mkdtemp(prefix="prio_ckpt_"))
    r = Runner(Graph.from_dict(fan), execs, trace=trace,
               checkpoint_dir=tmp, run_id="prio-demo",
               slots=1, default_deadline_ms=120)
    t0 = time.perf_counter()
    plan = r.run({})
    wall = (time.perf_counter() - t0) * 1000
    trace.finish()

    node = {n["id"]: n for n in plan["nodes"]}
    check("slow 在图里被标 timeout", node["slow"]["status"] == TIMEOUT,
          node["slow"]["status"])
    check("quick 没被 slow 拖死，正常完成", node["quick"]["status"] == OK)
    check("下游 join 被跳过（上游没有结论，不能假装有）",
          node["join"]["status"] == SKIP, node["join"]["status"])
    check("plan.timed_out 列出超时节点", plan["timed_out"] == ["slow"],
          str(plan["timed_out"]))
    check("quick 在 slow 的 deadline 附近就开跑了",
          node["quick"]["started_ms"] < 300,
          "quick.start=%.0fms" % node["quick"]["started_ms"])
    # 不拿"总墙钟 < 400ms"当判据：每个节点都要落一次 checkpoint，本机实测
    # 一次原子写 25–40ms（4 个节点就是 100ms+），这项开销跟"有没有等 slow"无关。
    # 真正要证明的是"下游在 slow 本该跑完之前就动了"，所以量 gate 的起点。
    check("下游 join 在 slow 本该跑完（400ms）之前就被处理了",
          node["join"]["started_ms"] < 400,
          "join.start=%.0fms / slow 需要 400ms" % node["join"]["started_ms"])
    check("整次运行仍明显短于「等 slow 跑满」的 400ms + 存档开销",
          wall < 480, "%.0fms" % wall)

    saved = load_run(trace.run_id)
    events = [e["event"] for e in saved["events"]]
    check("轨迹里有 graph_wave（记了槽位数与优先级顺序）",
          "graph_wave" in events
          and any(e["detail"].get("slots") == 1 for e in saved["events"]
                  if e["event"] == "graph_wave"),
          str([e["detail"].get("order") for e in saved["events"]
               if e["event"] == "graph_wave"]))
    to = [e for e in saved["events"] if e["event"] == "scheduler_timeout"]
    check("轨迹里有 scheduler_timeout，写明 reason / abandoned / 槽位是否归还",
          bool(to) and to[0]["detail"].get("reason") == "deadline"
          and to[0]["detail"].get("abandoned") is True
          and to[0]["detail"].get("slot_released") is True,
          json.dumps(to[0]["detail"], ensure_ascii=False)[:110] if to else "无")
    check("轨迹里还有 node_timeout（节点语义的一层，不止调度语义）",
          "node_timeout" in events, str(events))

    ckpt = json.loads((tmp / "prio-demo.json").read_text(encoding="utf-8"))
    check("超时节点照样落进 checkpoint（续跑时不重算，也不重复付费）",
          (ckpt["results"].get("slow") or {}).get("status") == TIMEOUT,
          str((ckpt["results"].get("slow") or {}).get("status")))

    # ------------------------------------------------------------ 8
    print("\n[8] 图里的优先级排序：同批按 priority，不看声明顺序")

    def order_of(slots, prios):
        calls = []
        nodes = [{"id": "seed", "action": "fast"}]
        for i, p in enumerate(prios, 1):
            nodes.append({"id": "w%d" % i, "action": "rec", "needs": ["seed"],
                          "priority": p})
        spec = {"name": "ord", "nodes": nodes, "edges": []}

        def rec(ctx):
            calls.append((ctx.node.id, time.perf_counter()))
            time.sleep(0.05)
            return OK, {}

        rr = Runner(Graph.from_dict(spec), {"fast": lambda c: (OK, {}), "rec": rec},
                    slots=slots)
        rr.run({})
        return [c[0] for c in sorted(calls, key=lambda x: x[1])]

    got = order_of(1, [5, 50, 20])
    check("声明顺序 5,50,20 → 执行顺序 w2,w3,w1（按优先级，不按声明）",
          got == ["w2", "w3", "w1"], str(got))
    got2 = order_of(3, [5, 50, 20])
    check("三个槽位时全部并行，不存在排队这回事",
          set(got2) == {"w1", "w2", "w3"}, str(got2))

    # ------------------------------------------------------------ 9
    print("\n[9] 策略外置：改 JSON 改调度，且拼错要报错")

    spec = {"name": "s", "nodes": [{"id": "a", "action": "x"},
                                   {"id": "b", "action": "x"}]}
    out_spec = apply_policy(json.loads(json.dumps(spec)),
                            {"node_priority": {"a": 7}, "node_deadline_ms": {"b": 50}})
    got = {n["id"]: (n.get("priority", 0), n.get("deadline_ms")) for n in out_spec["nodes"]}
    check("apply_policy 把 priority / deadline 写进图定义",
          got == {"a": (7, None), "b": (0, 50.0)}, str(got))

    try:
        apply_policy(json.loads(json.dumps(spec)), {"node_priority": {"ghost": 1}})
        ok = False
        err = ""
    except ValueError as exc:
        ok, err = True, str(exc)
    check("配了图里没有的节点 → 直接报错（不会静默失效）", ok, err[:60])

    real = load_policy()
    check("读得到 config/scheduler.json", isinstance(real, dict) and real.get("slots"),
          "slots=%s default_deadline=%s" % (real.get("slots"),
                                            real.get("default_deadline_ms")))
    check("策略里的节点名与销售流水线对得上",
          set((real.get("node_priority") or {})) ==
          {"draft", "verify", "critique", "gate", "curate", "final_check"},
          str(sorted((real.get("node_priority") or {}))))

    bad = Path(tempfile.mkdtemp(prefix="pol_")) / "bad.json"
    bad.write_text('{"slots": 0}', encoding="utf-8")
    try:
        load_policy(bad)
        ok = False
    except ValueError:
        ok = True
    check("slots=0 这种配置当场拒绝", ok)

    # ------------------------------------------------------------ 10
    print("\n[10] 图上拒绝不合法的调度参数")

    for name, node in (("deadline 为 0", {"id": "a", "action": "x", "deadline_ms": 0}),
                       ("deadline 为负", {"id": "a", "action": "x", "deadline_ms": -5})):
        try:
            Graph.from_dict({"nodes": [node]})
            ok = False
        except ValueError:
            ok = True
        check("拒绝：" + name, ok)

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
