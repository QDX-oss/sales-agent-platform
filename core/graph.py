# -*- coding: utf-8 -*-
"""任务图 —— 把流程从代码里搬到数据里（台账 W1-02）

改造前，一次问答的顺序写在 core/team.py 的 while 循环里：

    draft -> 回查 -> Critic -> 没过就再来一遍

这意味着「流程长什么样」只存在于 Python 的缩进里。想删掉重试，得改代码、
重启服务；想看出哪两步其实能同时做，得通读整段循环；想崩了接着跑，无从谈起。

这个文件把那几行缩进换成一份数据：

    nodes  有谁参与、各自等谁
    edges  什么条件下打回重来

改流程 = 改这份 dict。代码不动。

三个能力是这次改造的全部理由：

1. 条件边
   `gate --fail--> draft` 是一条回边。把这条边从数据里删掉，重试就真的没有了 ——
   不是"被注释掉的代码"，是图里根本没有这条路。

2. 并行
   verify 和 critique 都只依赖 draft，于是它们在同一批里同时开跑。
   规则回查不花模型调用，Critic 花一次，两者互不依赖也互不通信，
   串行跑纯粹是历史写法留下的。并行后时间区间真的重叠，不是"看起来并行"。

3. 断点续跑
   每完成一个节点写一次 checkpoint。崩了之后用同一个 run_id 接着跑，
   已完成的节点不重算 —— 模型调用是花钱的，重算就是重复付费。

设计上的一条硬规矩：**前进靠 needs，回退靠 edges。**
needs 只允许指向更早的节点，回边也只允许指向更早的节点。
这样图里不会出现"环形依赖"这种说不清的顺序，拓扑序就是声明顺序。

本模块只依赖标准库，不认识 Flask / LangChain / 销售数据。
"""

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.scheduler import ERROR as SCHED_ERROR
from core.scheduler import TIMEOUT as SCHED_TIMEOUT
from core.scheduler import Scheduler
from core.trace import new_run_id

__all__ = [
    "OK", "FAIL", "SKIP", "ERROR", "TIMEOUT",
    "Node", "Edge", "Graph", "Runner", "Ctx", "NodeResult", "NodeFailed",
    "GRAPH_RUNS",
]

# 节点状态
OK = "ok"            # 做完了，且通过
FAIL = "fail"        # 做完了，但结论是"不通过"（这是正常结果，不是异常）
SKIP = "skip"        # 上游没通过，这一步没走
ERROR = SCHED_ERROR  # 执行过程中抛了异常
TIMEOUT = SCHED_TIMEOUT  # 超过 deadline 没做完，已被放弃等待（台账 W2-01）

# 超时与"不通过"不是一回事：
#   FAIL     有结论，结论是坏的  → 回边可以打回重做
#   TIMEOUT  没有结论            → 打回重做没有意义，只能交已有结论或降级
# 所以回边的 when=fail 不会匹配 timeout。

NODE_KINDS = ("task", "gate")
RUN_IF = ("all_ok", "always")
WHEN = ("ok", "fail", "always")

# checkpoint 默认目录：项目根目录下的 graph_runs/
GRAPH_RUNS = Path(__file__).resolve().parent.parent / "graph_runs"


def _now_iso():
    return datetime.now().isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Node:
    """一个执行单元。

    id          图内唯一名，也是 checkpoint 里的键
    agent       谁负责（Analyst / Critic / Curator / verifier）—— 编排屏按这个染色
    action      交给哪个执行器（对应 Team._executors() 里的键）
    needs       等谁做完。只能指向更早的节点
    kind        task = 干活；gate = 合议（只做判断，不产出内容）
    run_if      all_ok = 上游全部通过才做；always = 上游跑过就做（合议节点必须用它，
                否则回查一失败它就被跳过，两道关卡的意见就凑不齐了）
    priority    同时能做几件事时谁先做，数字大的先（台账 W2-01）
    deadline_ms 从进队列那一刻起算的时限，超了判 timeout 并让出槽位
    """

    id: str
    agent: str
    action: str
    needs: Tuple[str, ...] = ()
    kind: str = "task"
    run_if: str = "all_ok"
    label: str = ""
    priority: int = 0
    deadline_ms: Optional[float] = None

    def to_dict(self):
        return {
            "id": self.id, "agent": self.agent, "action": self.action,
            "needs": list(self.needs), "kind": self.kind,
            "run_if": self.run_if, "label": self.label,
            "priority": self.priority, "deadline_ms": self.deadline_ms,
        }


@dataclass(frozen=True)
class Edge:
    """条件边。目前只用于回边（打回重做）。

    when       fail = 源节点判不通过才走；ok / always 同理
    max_loops  这条边最多走几次。0 表示禁用（保留形状但不生效）
    """

    src: str
    dst: str
    when: str = "fail"
    max_loops: int = 0
    label: str = ""

    @property
    def key(self):
        return "%s->%s" % (self.src, self.dst)

    def to_dict(self):
        return {"from": self.src, "to": self.dst, "when": self.when,
                "max_loops": self.max_loops, "label": self.label}


@dataclass
class NodeResult:
    id: str
    agent: str
    action: str
    status: str
    visit: int = 0
    started_ms: float = 0.0
    ended_ms: float = 0.0
    label: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self):
        return round(self.ended_ms - self.started_ms, 1)

    def to_dict(self):
        return {
            "id": self.id, "agent": self.agent, "action": self.action,
            "label": self.label, "status": self.status, "visit": self.visit,
            "started_ms": self.started_ms, "ended_ms": self.ended_ms,
            "duration_ms": self.duration_ms, "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            id=d["id"], agent=d.get("agent", ""), action=d.get("action", ""),
            status=d.get("status", OK), visit=int(d.get("visit", 0)),
            started_ms=float(d.get("started_ms", 0.0)),
            ended_ms=float(d.get("ended_ms", 0.0)),
            label=d.get("label", ""), payload=d.get("payload") or {},
        )


class NodeFailed(RuntimeError):
    """节点执行抛异常。包装后往上抛，让调用方能把 trace 标成失败。"""

    def __init__(self, node_id, cause):
        super().__init__("节点 %s 执行失败：%s: %s"
                         % (node_id, type(cause).__name__, cause))
        self.node_id = node_id
        self.cause = cause


class Ctx:
    """执行一个节点时能看到的全部东西。

    刻意只给这些：state（共享黑板）、自己的 node、已经完成的 results。
    不给全局 runner 之外的入口 —— 执行器不该自己决定下一步是谁，
    那是图的事。gate 需要知道"还有没有重试预算"，所以给 can_loop()。
    """

    def __init__(self, state, node, results, runner):
        self.state = state
        self.node = node
        self.results = results
        self.runner = runner

    # ---- 读别人的结果 ----
    def status(self, node_id):
        r = self.results.get(node_id)
        return r.status if r else None

    def payload(self, node_id):
        r = self.results.get(node_id)
        return (r.payload if r else {}) or {}

    # ---- 看图 ----
    def can_loop(self, node_id=None):
        """当前节点还有没有重试预算。gate 靠这个决定"打回"还是"交卷"。"""
        return self.runner.can_loop(node_id or self.node.id)

    def loop_used(self, node_id=None):
        return self.runner.loop_used(node_id or self.node.id)

    # ---- 记录 ----
    def log(self, event, **detail):
        if self.runner.trace is not None:
            self.runner.trace.log(event, **detail)


class Graph:
    """节点 + 边。构造时就校验，不合法直接抛，不留到运行到一半才炸。"""

    def __init__(self, nodes, edges, name="graph", version=1):
        self.nodes: List[Node] = list(nodes)
        self.edges: List[Edge] = list(edges)
        self.name = name
        self.version = version
        self._index = {n.id: i for i, n in enumerate(self.nodes)}
        self.check()

    # ---------- 构造 ----------

    @classmethod
    def from_dict(cls, spec):
        nodes = []
        for item in spec.get("nodes") or []:
            nodes.append(Node(
                id=item["id"],
                agent=item.get("agent", ""),
                action=item.get("action") or item["id"],
                needs=tuple(item.get("needs") or ()),
                kind=item.get("kind", "task"),
                run_if=item.get("run_if", "all_ok"),
                label=item.get("label", ""),
                priority=int(item.get("priority", 0) or 0),
                deadline_ms=(None if item.get("deadline_ms") is None
                             else float(item["deadline_ms"])),
            ))
        edges = []
        for item in spec.get("edges") or []:
            edges.append(Edge(
                src=item.get("from") or item["src"],
                dst=item.get("to") or item["dst"],
                when=item.get("when", "fail"),
                max_loops=int(item.get("max_loops", 0)),
                label=item.get("label", ""),
            ))
        return cls(nodes, edges, spec.get("name", "graph"),
                   int(spec.get("version", 1)))

    def to_dict(self):
        return {
            "name": self.name, "version": self.version,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }

    # ---------- 校验 ----------

    def check(self):
        errors = []
        if not self.nodes:
            errors.append("图里没有任何节点")

        ids = [n.id for n in self.nodes]
        dup = {x for x in ids if ids.count(x) > 1}
        if dup:
            errors.append("节点 id 重复：%s" % "、".join(sorted(dup)))

        for n in self.nodes:
            if n.kind not in NODE_KINDS:
                errors.append("节点 %s 的 kind 不合法：%s" % (n.id, n.kind))
            if n.run_if not in RUN_IF:
                errors.append("节点 %s 的 run_if 不合法：%s" % (n.id, n.run_if))
            if not isinstance(n.priority, int):
                errors.append("节点 %s 的 priority 必须是整数：%r" % (n.id, n.priority))
            if n.deadline_ms is not None and n.deadline_ms <= 0:
                errors.append("节点 %s 的 deadline_ms 必须大于 0：%r"
                              % (n.id, n.deadline_ms))
            for need in n.needs:
                if need not in self._index:
                    errors.append("节点 %s 依赖了不存在的节点 %s" % (n.id, need))
                elif self._index[need] >= self._index[n.id]:
                    errors.append("节点 %s 的 needs 指向了不更早的节点 %s" % (n.id, need))

        for e in self.edges:
            if e.src not in self._index:
                errors.append("边 %s 的起点不存在" % e.key)
                continue
            if e.dst not in self._index:
                errors.append("边 %s 的终点不存在" % e.key)
                continue
            if e.when not in WHEN:
                errors.append("边 %s 的 when 不合法：%s" % (e.key, e.when))
            if e.max_loops < 0:
                errors.append("边 %s 的 max_loops 不能为负" % e.key)
            if self._index[e.dst] >= self._index[e.src]:
                errors.append("边 %s 必须指回更早的节点（前进用 needs，回退用 edges）" % e.key)

        if errors:
            raise ValueError("任务图不合法：\n- " + "\n- ".join(errors))

    # ---------- 查询 ----------

    def back_edges(self, node_id):
        return [e for e in self.edges if e.src == node_id]

    def loop_body(self, edge):
        """回边触发时要作废的范围（含起点和终点）。

        gate 打回 draft 时，这一圈里 draft / verify / critique / gate 的结论
        全部过期，必须重算。上游节点不受影响。
        """
        return [n.id for n in self.nodes[self._index[edge.dst]:self._index[edge.src] + 1]]

    def order(self):
        return [n.id for n in self.nodes]


class Runner:
    """按图执行。一次 run 一个实例，状态不跨 run 复用。

    slots                同批最多同时做几件。None = 该批有几个就几个（旧行为）
    default_deadline_ms  节点没单独写 deadline 时用的时限（台账 W2-01）
    """

    def __init__(self, graph: Graph, executors: Dict[str, Callable],
                 trace=None, checkpoint_dir=None, run_id=None, parallel=True,
                 slots: Optional[int] = None, default_deadline_ms=None,
                 budget=None):
        self.graph = graph
        self.executors = dict(executors)
        self.trace = trace
        # 预算护栏（W2-04）：每批开跑前问一句还能不能跑。None = 不设限。
        self.budget = budget
        self.aborted = None
        self.run_id = run_id or (getattr(trace, "run_id", None) or new_run_id())
        self.checkpoint_dir = Path(checkpoint_dir) if checkpoint_dir else None
        self.parallel = parallel
        self.slots = (max(1, int(slots)) if slots is not None else None)
        self.default_deadline_ms = default_deadline_ms

        self.results: Dict[str, NodeResult] = {}
        self.visits: Dict[str, int] = {}
        self.loops: Dict[str, int] = {}
        self.order: List[str] = []
        self.schedule: List[dict] = []      # 每批调度留下的账，summary 里给编排屏看
        self._lock = threading.Lock()
        self._t0 = None

        missing = {n.action for n in graph.nodes} - set(self.executors)
        if missing:
            raise ValueError("这些动作没有执行器：%s" % "、".join(sorted(missing)))

    # ---------- 对外 ----------

    @classmethod
    def resume(cls, graph, executors, run_id, trace=None, checkpoint_dir=None):
        """从 checkpoint 恢复。返回 (runner, state)，调用 run(state) 继续跑。"""
        runner = cls(graph, executors, trace=trace,
                     checkpoint_dir=checkpoint_dir, run_id=run_id)
        saved = runner.read_checkpoint()
        if not saved:
            raise FileNotFoundError("找不到 run %s 的 checkpoint" % run_id)
        runner.results = {k: NodeResult.from_dict(v)
                          for k, v in (saved.get("results") or {}).items()}
        runner.visits = {k: int(v) for k, v in (saved.get("visits") or {}).items()}
        runner.loops = {k: int(v) for k, v in (saved.get("loops") or {}).items()}
        runner.order = list(saved.get("order") or [])
        return runner, saved.get("state") or {}

    def run(self, state: dict) -> dict:
        """跑到没有可执行的节点为止。返回摘要，答案在 state 里。"""
        self._t0 = time.perf_counter()

        while True:
            wave = self._ready()
            if not wave:
                break

            # 预算先于调度（W2-04）：下一个节点开跑前就拦住，不等这一轮跑完。
            # 停在这里说明"已经产出的部分就是全部"，上层据此如实交付 + 提示。
            if self.budget is not None:
                hit = self.budget.check(self.trace)
                if hit:
                    self.aborted = hit
                    if self.trace is not None:
                        self.trace.abort(hit["kind"], hit)
                        try:
                            self.trace.log("budget_abort", limit=hit["limit"],
                                           used=hit["used"], cap=hit["cap"],
                                           note=hit["why"])
                        except Exception:               # noqa: BLE001
                            pass                        # 留痕失败不该反过来打断中止
                    break

            if len(wave) == 1 or not self.parallel:
                done = [self._exec(n, state) for n in wave]
            else:
                done = self._run_wave(wave, state)

            if self._apply_loops(done, state):
                continue

        self._checkpoint(state)
        return self.summary(state)

    def summary(self, state=None):
        # 把图上的调度参数一并附在节点上：编排屏要画"谁先谁后、谁有时间限制"，
        # 只靠 NodeResult 里的时间戳是画不出优先级的。
        nodes = []
        for n in self.graph.nodes:
            if n.id not in self.results:
                continue
            item = self.results[n.id].to_dict()
            item["priority"] = n.priority
            item["deadline_ms"] = self._deadline_for(n)
            nodes.append(item)
        return {
            "graph": self.graph.name,
            "version": self.graph.version,
            "run_id": self.run_id,
            "nodes": nodes,
            "edges": [e.to_dict() for e in self.graph.edges],
            "loops": dict(self.loops),
            "order": list(self.order),
            "completed": len(self.results) == len(self.graph.nodes),
            "not_passed": [x["id"] for x in nodes
                           if x["status"] in (FAIL, ERROR, TIMEOUT)],
            "timed_out": [x["id"] for x in nodes if x["status"] == TIMEOUT],
            "skipped": [x["id"] for x in nodes if x["status"] == SKIP],
            "schedule": list(self.schedule),
            # 预算中止（W2-04）：不是失败，是主动停车 —— 上层要区别对待
            "aborted": self.aborted,
        }

    # ---------- 调度 ----------

    def _ready(self):
        """这一批可以执行的节点。

        条件：自己没跑过、依赖全部跑过（不论成败，成败由 run_if 判）。
        因为 needs 只指向更早的节点，同一时刻的就绪集天然构成一层，
        层内的节点互不依赖 —— 这就是并行的依据。
        """
        return [n for n in self.graph.nodes
                if n.id not in self.results
                and all(need in self.results for need in n.needs)]

    def _exec(self, node: Node, state: dict) -> NodeResult:
        visit = self.visits.get(node.id, 0)
        start_ms = self._since()

        blocked = self._blocked(node)
        if node.run_if == "all_ok" and blocked:
            return self._finish(NodeResult(
                node.id, node.agent, node.action, SKIP, visit,
                start_ms, start_ms, node.label, {"blocked_by": blocked}), state)

        try:
            status, payload = self._invoke(node, state)
        except Exception as exc:                        # noqa: BLE001
            # 故意不写进 results：这个节点必须能在 resume 时重跑
            self._trace_node(NodeResult(
                node.id, node.agent, node.action, ERROR, visit, start_ms,
                self._since(), node.label,
                {"error": {"type": type(exc).__name__, "message": str(exc)[:200]}}))
            raise NodeFailed(node.id, exc)

        return self._finish(NodeResult(
            node.id, node.agent, node.action, status, visit,
            start_ms, self._since(), node.label, payload or {}), state)

    def _since(self):
        return round((time.perf_counter() - self._t0) * 1000, 1)

    def _blocked(self, node):
        return [need for need in node.needs
                if self.results[need].status != OK]

    def _invoke(self, node, state):
        """只执行、不落账。异常原样抛给调用方。

        与"记账"分开，是因为调度器可能在我们不等之后才拿到结果（台账 W2-01）：
        超时被放弃的线程绝不能有写 results / 写 checkpoint 的机会，
        它只能拿到这个入口，也就是只能算，不能定。

        W3-03：进出各写一条 node_begin / node_end。编排屏靠它们回答
        "现在是哪个节点在跑"——
          · draft 走 execute() 根本不进调度器，没有 scheduler_start 可读；
          · 超时判死之后线程还在跑，调度器那边已经 done 了，只有这里说得清。
        这两条只是**观测量**，不参与任何判定：写失败也不能影响节点本身。
        """
        trace = self.trace
        if trace is not None:
            trace.log("node_begin", node=node.id, agent=node.agent,
                      priority=node.priority,
                      visit=self.visits.get(node.id, 0) + 1,
                      note="节点开始执行")
        try:
            return self.executors[node.action](Ctx(state, node, self.results, self))
        finally:
            if trace is not None:
                trace.log("node_end", node=node.id, agent=node.agent,
                          note="节点执行结束（结果如何见 graph_node）")

    def _finish(self, res: NodeResult, state: dict):
        """落账 + 记轨迹 + 存档。三件事一起做，避免有人漏掉其中一步。"""
        self._commit(res)
        self._trace_node(res)
        self._checkpoint(state)
        return res

    def _node(self, node_id):
        return self.graph.nodes[self.graph._index[node_id]]

    # ---------- 同批调度（台账 W2-01） ----------

    def _deadline_for(self, node):
        return node.deadline_ms if node.deadline_ms is not None else self.default_deadline_ms

    def _by_priority(self, wave):
        """同批的执行顺序：优先级高的先，同优先级按图的声明顺序（稳定，不靠线程运气）。"""
        pos = {n.id: i for i, n in enumerate(self.graph.nodes)}
        return sorted(wave, key=lambda n: (-n.priority, pos[n.id]))

    def _run_wave(self, wave, state):
        """一批互不依赖的节点，交给调度器排队执行。

        改造前这里是 ThreadPoolExecutor 一把 submit 全部：没有队列，
        谁先跑取决于线程调度；也没有 deadline，一个卡住的节点能把整批拖死。
        """
        ordered = self._by_priority(wave)
        slots = min(self.slots or len(wave), len(wave))
        if self.trace is not None:
            self.trace.log("graph_wave", nodes=[n.id for n in wave], slots=slots,
                           order=[n.id for n in ordered],
                           note="互不依赖，同批进队列；按 priority 取槽位")

        sched = Scheduler(slots=slots, trace=self.trace)
        for node in ordered:
            sched.submit(node.id, _job_fn(self, node, state),
                         priority=node.priority,
                         deadline_ms=self._deadline_for(node))

        done, failure = [], None
        outcomes = sched.run()
        # 调度器的时间相对它自己开跑那一刻，图的时间相对整次 run 的起点。
        # 换算一次，后面所有节点都在同一条时间轴上（不然甘特图会错位）。
        offset = sched.offset_ms(self._t0)
        for out in outcomes:
            node = self._node(out.id)
            visit = self.visits.get(node.id, 0)
            started = _shift(out.started_ms, offset)
            ended = _shift(out.ended_ms, offset)
            rec = dict(out.to_dict(), agent=node.agent, label=node.label,
                       started_ms=started, ended_ms=ended)
            self.schedule.append(rec)

            if out.status == SCHED_TIMEOUT:
                begin = started if started is not None else out.waited_ms
                res = NodeResult(
                    node.id, node.agent, node.action, TIMEOUT, visit, begin,
                    ended if ended is not None else begin,
                    node.label,
                    {"reason": out.reason, "waited_ms": out.waited_ms,
                     "deadline_ms": out.deadline_ms, "abandoned": True})
                self._finish(res, state)
                if self.trace is not None:
                    self.trace.log("node_timeout", node=node.id, reason=out.reason,
                                   deadline_ms=out.deadline_ms,
                                   waited_ms=out.waited_ms, abandoned=True,
                                   note="节点超时，槽位已让出，后续节点不等它")
                done.append(res)
                continue

            if out.status == SCHED_ERROR:
                # 同批其它节点可能已经跑完并落了 checkpoint，这里不写任何东西，
                # 直接抛出去 —— 恢复时那一个节点会被跳过，没跑的那个会重跑。
                self._trace_node(NodeResult(
                    node.id, node.agent, node.action, ERROR, visit,
                    started or 0.0, ended or 0.0, node.label,
                    {"error": out.error or {}}))
                failure = failure or NodeFailed(
                    node.id, out.exc or RuntimeError("节点未返回结果"))
                continue

            status, payload = out.value
            res = self._finish(NodeResult(
                node.id, node.agent, node.action, status, visit,
                started or 0.0, ended or 0.0,
                node.label, payload or {}), state)
            done.append(res)

        if failure is not None:
            raise failure
        return done

    def _commit(self, res):
        with self._lock:
            self.results[res.id] = res
            self.order.append(res.id)

    def _trace_node(self, res):
        if self.trace is None:
            return
        self.trace.log(
            "graph_node",
            node=res.id, agent=res.agent, status=res.status,
            visit=res.visit, started_ms=res.started_ms, ended_ms=res.ended_ms,
            duration_ms=res.duration_ms,
            note=res.label,
        )

    def _apply_loops(self, done: List[NodeResult], state: dict) -> bool:
        """检查刚跑完的节点有没有触发回边。触发就作废这一圈，返回 True。"""
        for res in done:
            for edge in self.graph.back_edges(res.id):
                if not self._edge_match(edge, res.status):
                    continue
                used = self.loops.get(edge.key, 0)
                if used >= edge.max_loops:
                    continue

                body = self.graph.loop_body(edge)
                with self._lock:
                    for nid in body:
                        self.results.pop(nid, None)
                    self.loops[edge.key] = used + 1
                    self.visits[edge.dst] = self.visits.get(edge.dst, 0) + 1
                    self.order = [x for x in self.order if x not in body]

                if self.trace is not None:
                    self.trace.log(
                        "graph_loop", edge=edge.key, attempt=used + 1,
                        max_loops=edge.max_loops, cleared=body,
                        reason=res.status, note=edge.label or "打回重做")
                self._checkpoint(state)
                return True
        return False

    @staticmethod
    def _edge_match(edge, status):
        if edge.when == "always":
            return True
        if edge.when == "ok":
            return status == OK
        return status == FAIL

    def can_loop(self, node_id):
        return any(self.loops.get(e.key, 0) < e.max_loops
                   for e in self.graph.back_edges(node_id))

    def loop_used(self, node_id):
        return sum(self.loops.get(e.key, 0) for e in self.graph.back_edges(node_id))

    # ---------- checkpoint ----------

    def _path(self):
        return self.checkpoint_dir / (self.run_id + ".json")

    def read_checkpoint(self):
        if self.checkpoint_dir is None:
            return None
        path = self._path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _checkpoint(self, state):
        if self.checkpoint_dir is None:
            return
        try:
            self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "run_id": self.run_id,
                "graph": self.graph.name,
                "version": self.graph.version,
                "updated_at": _now_iso(),
                "results": {k: v.to_dict() for k, v in self.results.items()},
                "visits": dict(self.visits),
                "loops": dict(self.loops),
                "order": list(self.order),
                "state": _safe(state),
            }
            tmp = self._path().with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2,
                                      default=str), encoding="utf-8")
            tmp.replace(self._path())
        except OSError:
            # 存档失败不该弄崩业务：丢了 checkpoint 最多是重算一次
            pass


def _shift(value, offset):
    """把调度器时间轴上的毫秒数搬到 run 的时间轴上。None 原样返回。"""
    return None if value is None else round(value + offset, 1)


def _job_fn(runner, node, state):
    """把"执行一个节点"包成调度器要的无参函数。

    SKIP 的判断放在这里而不是在图里，是因为它只依赖已完成的节点、
    进队列那一刻就能定；让它在槽位里判，等于白占一个槽位。
    """
    def run_one():
        blocked = runner._blocked(node)
        if node.run_if == "all_ok" and blocked:
            return SKIP, {"blocked_by": blocked}
        return runner._invoke(node, state)
    return run_one


def _safe(obj):
    """转成能落盘的形状。不可序列化的对象降级成字符串，不抛异常。"""
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {}
