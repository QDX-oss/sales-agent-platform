# -*- coding: utf-8 -*-
"""
运行轨迹（trace）记录器 —— 台账 W0-03

一次用户请求 = 一次 run。run 里的每一次 Agent 调用 = 一个 step。
所有记录追加写入 runs/{run_id}.jsonl，一行一条：

    {"type": "run",      ...}   第一行：这次跑的是什么、什么时候开始
    {"type": "step",     ...}   每个步骤一行：谁、做了什么、多久、多少 token、成没成
    {"type": "run_end",  ...}   最后一行：整体结果与总耗时

为什么是 jsonl 而不是一个 JSON 或数据库：
- 追加写：程序中途崩溃，已经写出的步骤仍然可读，不会整份作废。
- 一行一条：现场排查时可以直接 tail / grep，不需要先写查询代码。
- 零依赖：只用标准库。将来换存储只改这一个文件，上层不用动。

为什么需要它：
系统的可解释性不取决于界面多漂亮，而取决于「刚才那一步为什么这么走、
花了多久、第几次才成功」这几件事能不能被回答。没有这份记录，
所有关于编排、调度、自我修正的说法都无法被验证。

本模块不依赖 flask / langchain。
"""

import json
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# 默认存放目录：项目根目录下的 runs/
RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"

# 步骤状态
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_TIMEOUT = "timeout"


def _now_iso():
    return datetime.now().isoformat(timespec="milliseconds")


def new_run_id():
    """生成可按时间排序的 run_id，形如 20261103T210512-3f9a2c。"""
    return datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


class Step:
    """单个步骤的句柄。

    在 with 块内可以直接改字段：

        with trace.step(agent="Analyst", name="query_stats") as st:
            st.input = {"date": "2025-11"}
            result = query_stats(...)
            st.output = result
            st.tokens_in, st.tokens_out = 512, 128
    """

    def __init__(self, seq, agent, name, input_data=None, retry=0):
        self.seq = seq
        self.agent = agent
        self.name = name
        self.input = input_data
        self.output = None
        self.status = STATUS_OK
        self.error = None
        self.retry = retry          # 0 = 首次执行，1 = 第一次重试，以此类推
        self.tokens_in = 0
        self.tokens_out = 0
        self.meta = {}
        self.started_at = _now_iso()
        self.duration_ms = None
        self._t0 = time.perf_counter()

    def stop(self):
        """停止计时并返回自身，便于链式调用。"""
        self.duration_ms = round((time.perf_counter() - self._t0) * 1000, 1)
        return self

    def to_dict(self):
        return {
            "type": "step",
            "seq": self.seq,
            "agent": self.agent,
            "name": self.name,
            "status": self.status,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "retry": self.retry,
            "input": self.input,
            "output": self.output,
            "tokens": {"in": self.tokens_in, "out": self.tokens_out},
            "error": self.error,
            "meta": self.meta,
        }


class Trace:
    """一次运行的轨迹。

        trace = Trace(question="本月销售额多少")
        with trace.step("Analyst", "query_stats") as st:
            st.output = "..."
        trace.finish()

    异常不会被吞掉：with 块内抛异常时步骤记为 failed 后原样抛出，
    由调用方决定是重试、降级还是终止。
    """

    def __init__(self, question="", source="api", runs_dir=None, tags=None,
                 run_id=None):
        self.run_id = run_id or new_run_id()
        self.question = question
        self.source = source            # api / selftest / scheduler ...
        self.tags = tags or {}
        self.runs_dir = Path(runs_dir) if runs_dir else RUNS_DIR
        self.path = self.runs_dir / (self.run_id + ".jsonl")

        self.status = "running"
        self.started_at = _now_iso()
        self.ended_at = None
        self.duration_ms = None
        self.aborted = None             # 预算护栏（W2-04）中止标记
        self.abort_detail = None

        self._t0 = time.perf_counter()
        self._seq = 0
        self._lock = threading.Lock()
        self._closed = False
        self._final_summary = None
        self._steps = []                # 内存副本，供 summary() 使用

        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._write({
            "type": "run",
            "run_id": self.run_id,
            "question": question,
            "source": source,
            "tags": self.tags,
            "started_at": self.started_at,
            "trace_version": 1,
        })

    # ---------- 内部 ----------

    def _next_seq(self):
        """取下一个序号。

        必须加锁：W1-02 起节点可以并行执行，两个线程同时 `self._seq += 1`
        会拿到同一个序号，轨迹里就会出现两条 seq 相同的记录 —— 排序失效，
        回放时看不出谁先谁后。序号是这份文件的唯一时间轴，不能有一次重复。
        """
        with self._lock:
            self._seq += 1
            return self._seq

    def _write(self, record):
        """追加一行。每次独立打开文件，避免多线程共享句柄出问题。"""
        if self._closed:
            return
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fp:
                fp.write(line + "\n")
                fp.flush()

    def _emit(self, step):
        step.stop()
        record = step.to_dict()
        self._steps.append(record)
        self._write(record)

    # ---------- 对外 ----------

    @contextmanager
    def step(self, agent, name, input_data=None, retry=0):
        """记录一个步骤。异常自动记 failed 并向上抛出。"""
        st = Step(self._next_seq(), agent, name, input_data, retry)
        try:
            yield st
        except Exception as exc:
            st.status = STATUS_FAILED
            st.error = {"type": type(exc).__name__, "message": str(exc)}
            self._emit(st)
            raise
        else:
            self._emit(st)

    def begin(self, agent, name, input_data=None, retry=0):
        """手动开启一个步骤（不落盘），配合 end() 使用。

        with 语句适合"我控制流程"的场景；但 LangChain 的回调是
        「开始时通知一次、结束时再通知一次」，中间由框架控制，
        没法包进 with。所以额外提供这一对方法，落盘时机与 with 一致。
        """
        return Step(self._next_seq(), agent, name, input_data, retry)

    def end(self, step, status=STATUS_OK, output=None, error=None):
        """收尾一个 begin() 出来的步骤并落盘。重复调用只有第一次生效。"""
        if step is None or getattr(step, "_emitted", False):
            return None
        step.status = status
        if output is not None:
            step.output = output
        if error is not None:
            step.error = error if isinstance(error, dict) else {
                "type": type(error).__name__, "message": str(error)}
        step._emitted = True
        self._emit(step)
        return step

    def log(self, event, **detail):
        """记录一条不构成步骤的事件（例如调度决策、缓存命中、降级切换）。

            trace.log("scheduler", note="Critic 超时，触发第 1 次重试", policy="timeout->retry")
        """
        seq = self._next_seq()
        record = {
            "type": "event",
            "seq": seq,
            "at": _now_iso(),
            "event": event,
            "detail": detail,
        }
        self._steps.append(record)
        self._write(record)

    def events(self, name=None):
        """取本次 run 里的事件记录（可按事件名过滤，不落盘）。

            trace.events("fallback")   # 这次请求换过通道没有

        为什么要有这个方法：降级、调度这类"非步骤"的事实只记在 event 里，
        上层（班组结果、接口、编排屏）不该自己扒 _steps —— 那是私有字段。
        """
        out = [s for s in self._steps if s.get("type") == "event"]
        if name:
            out = [e for e in out if e.get("event") == name]
        return [dict({"event": e.get("event"), "seq": e.get("seq")},
                     **(e.get("detail") or {})) for e in out]

    def summary(self):
        """当前进度摘要，供接口 / 页面轮询使用（不落盘）。"""
        steps = [s for s in self._steps if s.get("type") == "step"]
        return {
            "run_id": self.run_id,
            "status": self.status,
            "question": self.question,
            "step_count": len(steps),
            "failed_steps": sum(1 for s in steps if s.get("status") == STATUS_FAILED),
            "total_tokens": sum(s["tokens"]["in"] + s["tokens"]["out"] for s in steps),
            "elapsed_ms": round((time.perf_counter() - self._t0) * 1000, 1),
            "aborted": getattr(self, "aborted", None),
            "path": str(self.path),
        }

    # ---------- 预算护栏读数（台账 W2-04） ----------

    def tokens_used(self):
        """已烧掉的 token 总数（实时，不等收尾）。"""
        return sum(s["tokens"]["in"] + s["tokens"]["out"]
                   for s in self._steps if s.get("type") == "step")

    def calls_made(self):
        """已发生的模型调用次数。

        只数真正花过 token 的 step：工具查询、校验这类纯本地步骤不该占"调用"名额，
        否则一道题查三次数就把预算耗光了，护栏会变成误伤。
        """
        return sum(1 for s in self._steps
                   if s.get("type") == "step" and
                   (s["tokens"]["in"] + s["tokens"]["out"]) > 0)

    def abort(self, kind, detail=None):
        """标记本次 run 被主动中止（如 kind="budget"）。收尾时写进 run_end.aborted。

        刻意不用异常实现：中止不是故障，是预算到了该停。用异常会走到
        failed 分支，报告里看起来像"跑挂了"，排障时白查一圈。
        """
        self.aborted = str(kind)
        self.abort_detail = detail or {}

    def finish(self, status=STATUS_OK, error=None):
        """收尾：写入 run_end 并关闭。重复调用返回同一份结果（幂等）。"""
        if self._closed:
            return self._final_summary

        self.status = status
        self.ended_at = _now_iso()
        self.duration_ms = round((time.perf_counter() - self._t0) * 1000, 1)

        steps = [s for s in self._steps if s.get("type") == "step"]
        self._write({
            "type": "run_end",
            "run_id": self.run_id,
            "status": status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "step_count": len(steps),
            "failed_steps": sum(1 for s in steps if s.get("status") == STATUS_FAILED),
            "retry_count": sum(1 for s in steps if (s.get("retry") or 0) > 0),
            "tokens": {
                "in": sum(s["tokens"]["in"] for s in steps),
                "out": sum(s["tokens"]["out"] for s in steps),
            },
            "error": error,
            # 预算护栏（W2-04）：被主动中止时这里是 "budget"，正常跑完是 None。
            # 放在 run_end 里而不是单独事件：这是这次 run 的结局属性，
            # 编排屏 / 回归报告 / 排障 grep 一个字段就能筛出来。
            "aborted": getattr(self, "aborted", None),
            "abort_detail": getattr(self, "abort_detail", None),
        })
        self._closed = True
        # 收尾后耗时固定为总耗时，不随时间继续增长，保证重复调用结果一致
        final = self.summary()
        final["elapsed_ms"] = self.duration_ms
        final["status"] = status
        self._final_summary = final
        return final


# ---------- 读取侧：调试、回放、页面列表都要用 ----------

def _resolve(run_id, runs_dir=None):
    return Path(runs_dir or RUNS_DIR) / (str(run_id) + ".jsonl")


def load_run(run_id, runs_dir=None):
    """读回一次完整运行，返回 {meta, steps, events, end}；不存在返回 None。"""
    path = _resolve(run_id, runs_dir)
    if not path.exists():
        return None

    meta, end = None, None
    steps, events = [], []
    with open(path, "r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue          # 崩在写一半的那一行，跳过即可
            kind = rec.get("type")
            if kind == "run":
                meta = rec
            elif kind == "step":
                steps.append(rec)
            elif kind == "event":
                events.append(rec)
            elif kind == "run_end":
                end = rec

    return {"meta": meta, "steps": steps, "events": events, "end": end, "path": str(path)}


def list_runs(limit=20, runs_dir=None):
    """按修改时间倒序列出最近的运行，返回概要（不含完整步骤）。"""
    root = Path(runs_dir or RUNS_DIR)
    if not root.exists():
        return []

    files = sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for path in files[:limit]:
        meta, end = None, None
        step_count = 0
        try:
            with open(path, "r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    kind = rec.get("type")
                    if kind == "run":
                        meta = rec
                    elif kind == "step":
                        step_count += 1
                    elif kind == "run_end":
                        end = rec
        except OSError:
            continue

        out.append({
            "run_id": path.stem,
            "question": (meta or {}).get("question", ""),
            "source": (meta or {}).get("source", ""),
            "started_at": (meta or {}).get("started_at"),
            "status": (end or {}).get("status", "running"),
            "duration_ms": (end or {}).get("duration_ms"),
            "step_count": step_count,
            "path": str(path),
        })
    return out
