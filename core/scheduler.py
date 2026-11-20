# -*- coding: utf-8 -*-
"""优先级调度 —— 台账 W2-01

改造前，一波节点"同时开跑"的做法是 ThreadPoolExecutor 一次 submit 全部：
没有队列，也就没有"谁先谁后"这件事 —— 谁先抢到线程谁先跑。
节点一多、机器一忙，"哪一步先做"变成随机的；出问题一复现不出来，
就没法解释，也没法保证重要的活先干。

这个文件把"待办"变成一条真正的队列：

    priority   数字大的先做。同优先级按提交顺序（稳定，不随线程调度漂）
    deadline   一个任务自己最多做多久，从它真正开跑那一刻算起。
               到点了就判 timeout、立刻把槽位让出去，后面的任务不用陪它一起等

三条必须说在前面的约束，否则很容易把它说成一个它做不到的东西：

1. Python 线程杀不掉。
   判 timeout ≠ 线程停了，只等于「我们不再等它」。它的结果会被丢弃、
   槽位归还、trace 记 abandoned=true。把这一点写成"超时即中断"就是撒谎。

2. 丢弃的结果不会污染账本。
   调度器只负责跑函数；把结果写进图、写进 checkpoint 是调用方在主线程做的。
   超时线程即使后来跑完了，也无权改任何结论 —— 它连入口都没有。

3. deadline 只管"做"，不管"等"。
   排队时间不计入 deadline。原因很实际：一个槽位只有 1 的批次里，
   前一个节点慢会把后面节点的预算吃光，于是"快的那个"因为"排得久"被判超时 ——
   那是调度器的锅，不是它的锅。排队时长单独记在 waited_ms 里，看得见，但不判死。
   代价是：如果前面的节点没配 deadline 又真的卡死，后面的会一直等。
   所以生产上给所有节点配默认 deadline（config/scheduler.json 的
   default_deadline_ms），让"卡死"这件事总有人在管。

本模块只依赖标准库，不认识 Flask / LangChain / 销售数据，也不知道节点是什么。
"""

import heapq
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "OK", "ERROR", "TIMEOUT", "Job", "Outcome", "Scheduler",
    "POLICY_FILE", "load_policy", "apply_policy",
]

OK = "ok"
ERROR = "error"
TIMEOUT = "timeout"

# 调度策略文件：改它 = 改调度，不动 Python（与 config/roles.json 同一套思路）
POLICY_FILE = Path(__file__).resolve().parent.parent / "config" / "scheduler.json"


@dataclass
class Job:
    """一个待执行的任务。id 在调度器内唯一。"""

    id: str
    fn: Callable[[], Any]
    priority: int = 0
    deadline_ms: Optional[float] = None      # 自己最长做多久（从开跑算起）；None = 不限
    meta: Dict[str, Any] = field(default_factory=dict)

    submitted_ms: float = 0.0                # 由调度器填写（相对它自己的起点）
    queue_pos: int = 0                       # 第几个被提交，用于同优先级排序


@dataclass
class Outcome:
    """一个任务的结局。ok / error / timeout 三选一。"""

    id: str
    status: str = OK
    value: Any = None                        # 任务返回值（ok 才有意义）
    error: Optional[dict] = None             # {"type","message"}，可落盘
    exc: Optional[BaseException] = None      # 原始异常对象，供调用方重新抛出
    priority: int = 0
    deadline_ms: Optional[float] = None
    submitted_ms: float = 0.0
    started_ms: Optional[float] = None       # 真正开跑的时刻（相对调度器起点）
    ended_ms: Optional[float] = None
    waited_ms: float = 0.0                   # 排队等了多久（不判死，只记录）
    abandoned: bool = False                  # True = 我们不再等它了
    reason: str = ""                         # 判超时的原因，目前只有 deadline
    queue_pos: int = 0

    @property
    def duration_ms(self):
        if self.started_ms is None or self.ended_ms is None:
            return 0.0
        return round(self.ended_ms - self.started_ms, 1)

    def to_dict(self):
        return {
            "id": self.id, "status": self.status, "priority": self.priority,
            "deadline_ms": self.deadline_ms, "queue_pos": self.queue_pos,
            "submitted_ms": self.submitted_ms, "started_ms": self.started_ms,
            "ended_ms": self.ended_ms, "waited_ms": self.waited_ms,
            "duration_ms": self.duration_ms, "abandoned": self.abandoned,
            "reason": self.reason,
        }


class _Run:
    """正在跑的任务。thread 是 daemon —— 它绝不会挡住进程退出。"""

    __slots__ = ("job", "started_ms", "waited_ms", "thread", "abandoned", "late")

    def __init__(self, job, started_ms, waited_ms):
        self.job = job
        self.started_ms = started_ms
        self.waited_ms = waited_ms
        self.thread = None
        self.abandoned = False
        self.late = None


class Scheduler:
    """按优先级取任务、按 deadline 放手的执行器。

        s = Scheduler(slots=1, trace=trace)
        s.submit("high", work_a, priority=10)
        s.submit("low",  work_b, priority=1)
        for out in s.run():
            ...

    slots = 同时能做几件事。设成 1 就是"排成一队"，台账验收用的就是它：
    同时提交高/低两任务，低优先的必须等高的做完才开始。
    """

    def __init__(self, slots: int = 1, trace=None, name: str = "schedule"):
        self.slots = max(1, int(slots))
        self.trace = trace
        self.name = name

        self._cv = threading.Condition()
        self._heap: List[tuple] = []
        self._seq = 0
        self._running: Dict[str, _Run] = {}
        self._results: Dict[str, Outcome] = {}
        self._order: List[str] = []
        self._t0: Optional[float] = None
        self._started = False

    # ---------------- 提交 ----------------

    def submit(self, job_id, fn, priority=0, deadline_ms=None, **meta):
        """入队。必须在 run() 之前调用 —— 跑起来之后再追加，语义会变成"插队"，
        那是另一个功能，不在这一条台账里，所以直接拒绝而不是含糊地支持。"""
        if self._started:
            raise RuntimeError("调度已经开始，不能再提交任务：%s" % job_id)
        with self._cv:
            if job_id in self._results or any(j.id == job_id for _, _, j in self._heap):
                raise ValueError("任务 id 重复：%s" % job_id)
            if self._t0 is None:
                self._t0 = time.perf_counter()
            self._seq += 1
            job = Job(id=job_id, fn=fn, priority=int(priority),
                      deadline_ms=None if deadline_ms is None else float(deadline_ms),
                      meta=dict(meta))
            job.submitted_ms = self._ms()
            job.queue_pos = self._seq
            heapq.heappush(self._heap, (-job.priority, self._seq, job))
            self._order.append(job_id)
            self._cv.notify()
        return self

    # ---------------- 执行 ----------------

    def run(self) -> List[Outcome]:
        """跑到队列空、槽位空为止。返回结果，顺序 = 提交顺序。"""
        with self._cv:
            if self._t0 is None:
                self._t0 = time.perf_counter()
        self._started = True
        with self._cv:
            while True:
                self._fill()
                if not self._running and not self._heap:
                    break
                self._cv.wait(self._wait_hint())
                self._expire()
        return [self._results[i] for i in self._order if i in self._results]

    def _fill(self):
        while self._heap and len(self._running) < self.slots:
            _, _, job = heapq.heappop(self._heap)
            self._start(job)

    def _start(self, job: Job):
        now = self._ms()
        waited = round(now - job.submitted_ms, 1)

        rec = _Run(job, now, waited)
        self._running[job.id] = rec
        th = threading.Thread(target=self._work, args=(rec,), daemon=True,
                              name="job-" + job.id)
        rec.thread = th
        th.start()
        self._log("scheduler_start", job=job.id, priority=job.priority,
                  waited_ms=waited, deadline_ms=job.deadline_ms,
                  slot="%d/%d" % (len(self._running), self.slots),
                  note="按优先级取到槽位")

    def _work(self, rec: _Run):
        status, value, exc = OK, None, None
        try:
            value = rec.job.fn()
        except BaseException as e:                       # noqa: BLE001
            status, exc = ERROR, e

        stale = None
        with self._cv:
            if rec.abandoned:
                # 迟到者：只留痕，不入账。它已经不在 _running 里，也改不到结果。
                rec.late = status
                stale = True
            else:
                self._running.pop(rec.job.id, None)
                out = Outcome(
                    id=rec.job.id, status=status, value=value, exc=exc,
                    error=None if exc is None else {
                        "type": type(exc).__name__, "message": str(exc)[:200]},
                    priority=rec.job.priority, deadline_ms=rec.job.deadline_ms,
                    submitted_ms=rec.job.submitted_ms, started_ms=rec.started_ms,
                    ended_ms=self._ms(), waited_ms=rec.waited_ms,
                    queue_pos=rec.job.queue_pos)
                self._results[rec.job.id] = out
            self._cv.notify()

        if stale:
            self._log("scheduler_late", job=rec.job.id, status=status,
                      note="判超时之后才跑完，结果丢弃")
        else:
            self._log("scheduler_done", job=rec.job.id, status=status,
                      duration_ms=out.duration_ms)

    def _expire(self):
        """把跑过 deadline 的任务判超时并让出槽位。

        计时基点是 started_ms（真正开跑），不是 submitted_ms：
        排队等别人不算它的账。

        注意这不是"中断"：线程还在跑，只是没人等了。所以这里既不能
        回收它的内存，也不能假装它停了 —— 只能保证它写不进结果。
        """
        now = self._ms()
        for job_id in list(self._running):
            rec = self._running[job_id]
            d = rec.job.deadline_ms
            if d is None:
                continue
            elapsed = now - rec.started_ms
            if elapsed < d:
                continue
            rec.abandoned = True
            self._running.pop(job_id, None)
            self._results[job_id] = Outcome(
                id=job_id, status=TIMEOUT, priority=rec.job.priority,
                deadline_ms=d, submitted_ms=rec.job.submitted_ms,
                started_ms=rec.started_ms, ended_ms=now,
                waited_ms=rec.waited_ms, abandoned=True, reason="deadline",
                queue_pos=rec.job.queue_pos)
            self._log("scheduler_timeout", job=job_id, reason="deadline",
                      deadline_ms=d, elapsed_ms=round(elapsed, 1),
                      slot_released=True, abandoned=True,
                      note="线程杀不掉，只放弃等待；迟到结果会被丢弃")

    def _wait_hint(self):
        """还要等多久。None = 没有带 deadline 的任务，可以一直等到有人完成。"""
        now = self._ms()
        waits = []
        for rec in self._running.values():
            d = rec.job.deadline_ms
            if d is None:
                continue
            waits.append(max(0.0, rec.started_ms + d - now) / 1000.0)
        return min(waits) if waits else None

    def _ms(self):
        return round((time.perf_counter() - self._t0) * 1000, 3)

    def offset_ms(self, base: float) -> float:
        """把本调度器的时间轴换算到调用方的时间轴。

        调度器的时间都相对它自己第一次 submit 的那一刻。调用方（任务图）
        用的是整次 run 的起点。不做这个换算，同一份结果里就会混进两套时间，
        画甘特图会错位 —— 节点看着像"在上游结束之前就开跑了"。
        """
        if self._t0 is None:
            return 0.0
        return round((self._t0 - base) * 1000, 1)

    def _log(self, event, **detail):
        if self.trace is not None:
            self.trace.log(event, **detail)


# ---------------------------------------------------------------- 策略外置

def load_policy(path=None) -> dict:
    """读 config/scheduler.json。不存在就返回空策略（= 全部用图里的默认值）。"""
    p = Path(path) if path else POLICY_FILE
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError("调度策略不是合法 JSON：%s（%s）" % (p, exc))
    if not isinstance(data, dict):
        raise ValueError("调度策略必须是一个对象：%s" % p)

    slots = data.get("slots")
    if slots is not None and (not isinstance(slots, int) or slots < 1):
        raise ValueError("slots 必须是 >=1 的整数，当前：%r" % (slots,))
    default = data.get("default_deadline_ms")
    if default is not None and (not isinstance(default, (int, float)) or default <= 0):
        raise ValueError("default_deadline_ms 必须是正数，当前：%r" % (default,))
    return data


def apply_policy(spec: dict, policy: dict) -> dict:
    """把策略写进图定义。

    只改 priority / deadline_ms 两个字段 —— 调度策略不该顺手改流程。
    写了图里不存在的节点名要报错，否则"配了但没生效"这种事，
    要等到答辩时被问"你这条配的哪生效了"才发现。
    多行业（W4-04）之后语义变了：策略是跨行业共用的，多出来的条目
    可能是另一个行业的节点（比如销售图没有 final_check，客服图有）——
    这种**忽略**，不报错；真正拼错的节点（哪个行业都没有）照样报错。
    """
    if not policy:
        return spec

    ids = {n["id"] for n in spec.get("nodes") or []}
    # 懒加载：只有配了跨行业节点时才需要查全行业清单
    other_ids = set()
    if not ids.issuperset((policy.get("node_priority") or {}).keys()) or \
       not ids.issuperset((policy.get("node_deadline_ms") or {}).keys()):
        try:
            from core.industry import available, load
            for it in available():
                other_ids |= {n["id"] for n in (load(it["id"]).get("pipeline")
                                                or {}).get("nodes") or []}
        except Exception:                      # noqa: BLE001
            other_ids = set()                  # 清单拿不到就按旧口径严格校验

    for key in ("node_priority", "node_deadline_ms"):
        unknown = set((policy.get(key) or {}).keys()) - ids - other_ids
        if unknown:
            raise ValueError("调度策略里出现图里没有的节点：%s（现有：%s）"
                             % ("、".join(sorted(unknown)), "、".join(sorted(ids))))

    for node in spec.get("nodes") or []:
        nid = node["id"]
        pri = (policy.get("node_priority") or {}).get(nid)
        if pri is not None:
            node["priority"] = int(pri)
        dl = (policy.get("node_deadline_ms") or {}).get(nid)
        if dl is not None:
            node["deadline_ms"] = float(dl)
    return spec
