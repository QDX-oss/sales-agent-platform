# -*- coding: utf-8 -*-
"""编排屏的数据组装（台账 W3-01 起）。

编排屏要展示的东西，在这个文件里由两份材料拼成一份：

  1. **图定义** —— core/team.py 的 pipeline()，就是运行时真正跑的那张图。
     不在前端另抄一份节点表：抄一份就意味着"画的"和"跑的"迟早不是同一件事，
     而这张屏存在的全部意义就是证明它们是同一件事。
  2. **运行轨迹** —— runs/<run_id>.jsonl。节点状态、批次、调度账、降级、
     诊断，全在里面。前端只念结果，不重算。

W3-01 只要求骨架，所以这里先给「节点 + 状态 + 调度账 + 时间线」；
W3-02 画 DAG 时直接吃同一份 nodes / edges，不用改接口。
"""
import json
import re
from datetime import datetime
from pathlib import Path

from core.team import pipeline

RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"

# 节点存档（core/graph.py 的 checkpoint）。payload 只在这里，trace 里没有 ——
# 抽屉要显示"这个节点产出了什么"，就得读它。
GRAPH_RUNS_DIR = Path(__file__).resolve().parent.parent / "graph_runs"

# run_id 是 "<日期>T<时间>-<随机>"，只允许这些字符。
# 接口收 run_id 参数，不加白名单就等于把 runs 目录交给路径穿越。
RUN_ID_RE = re.compile(r"^[0-9A-Za-z_-]{4,64}$")

# 节点状态归一：图里的状态词和屏上的颜色一一对应，中间不再多做一层翻译。
PENDING = "pending"
RUNNING = "running"

# 与 core/fallback.py 的 fallback_events() 保持同一份清单。
# 屏上少认一个事件，现场就会少看见一次"偷偷换了模型"。
FALLBACK_EVENTS = ("fallback", "fallback_exhausted", "fallback_probe",
                   "channel_fail", "channel_recovered", "channel_reopen",
                   "channel_unavailable", "channel_error_fatal")

# 同一份 jsonl 在一秒内的重复整读缓存，键带 mtime（见 _read_events）
_CACHE = {}


def _run_time_key(path):
    """run 文件的排序键：优先取文件名内嵌的时间戳（20261110T170248）。

    目录里除了正常 run，还有演示样本（FAILSAMPLE-...）这种前缀不规范
    的文件——纯按文件名倒序会把 11-10 的样本顶到最新 run 前面，列表
    看起来就乱了。没有内嵌时间戳的退回文件 mtime。
    """
    m = re.search(r"(\d{8}T\d{6})", path.stem)
    if m:
        return m.group(1)
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%dT%H%M%S")
    except OSError:
        return ""


def list_runs(limit=12):
    """最近的若干次 run，新的在前，严格按日期倒序。

    正常 run_id 本身就是时间戳（20261105T205553），按名字排就是按时间排；
    但 FAILSAMPLE 等演示样本不以日期开头，所以统一用 _run_time_key
    抽内嵌时间戳来排，不用去 stat 每个文件。
    """
    if not RUNS_DIR.exists():
        return []
    files = sorted((p for p in RUNS_DIR.glob("*.jsonl")),
                   key=_run_time_key, reverse=True)
    out = []
    for path in files[:max(1, int(limit))]:
        head = _read_head(path)
        if not head:
            continue
        out.append({
            "run_id": path.stem,
            "question": head.get("question", ""),
            "started_at": head.get("started_at"),
        })
    return out


def _ts(value):
    """把 trace 里的时间串换成可比大小的数；坏的当"最晚"，宁可不点亮也不误报。"""
    if not value:
        return float("inf")
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return float("inf")


def _read_head(path):
    """只读第一行拿问题与起始时间 —— 选运行记录不该把整个 trace 读一遍。"""
    try:
        with path.open("r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                return rec if rec.get("type") == "run" else None
    except (OSError, ValueError):
        return None
    return None


def _read_events(run_id):
    """读一次完整的 run。W3-03 起带 1 秒内的读缓存。

    编排屏每 1 秒轮询一次，同一份 jsonl 在一秒内被反复整读既浪费也容易
    读到"读到一半的文件"。缓存键带文件 mtime：文件一有新增立刻失效，
    不会把旧内容当成新状态。
    """
    path = RUNS_DIR / (run_id + ".jsonl")
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = None
    key = (run_id, mtime)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit

    head, tail, events = {}, {}, []
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            kind = rec.get("type")
            if kind == "run":
                head = rec
            elif kind == "run_end":
                tail = rec
            elif kind == "event":
                events.append(rec)
    out = (head, tail, events)
    _CACHE.clear()          # 只留这一次的：轮询下一轮必换文件，留着旧的没意义
    _CACHE[key] = out
    return out


def _node_states(events):
    """把 graph_node 事件按节点归拢。

    一个节点可能跑好几轮（打回重做），所以留整串 visits：
    屏上要能说出"这个节点跑了第 2 次才过"，只说"通过"就把重试藏了。
    """
    by_node = {}
    for ev in events:
        if ev.get("event") != "graph_node":
            continue
        d = ev.get("detail") or {}
        by_node.setdefault(d.get("node"), []).append({
            "status": d.get("status") or PENDING,
            "visit": d.get("visit", 0),
            "started_ms": d.get("started_ms"),
            "ended_ms": d.get("ended_ms"),
            "duration_ms": d.get("duration_ms"),
            "agent": d.get("agent"),
            "note": d.get("note"),
            "at": ev.get("at"),
        })
    return by_node


def _schedule_ledger(events):
    """调度账：一次 start 配一次 done。

    重试会让同一个 job 出现多轮，所以按出现顺序配对，不按 job 名去重 ——
    去重就等于把"它排了两次队"这件事抹掉。
    """
    rows, open_rows = [], {}
    for ev in events:
        name, d = ev.get("event"), ev.get("detail") or {}
        if name == "scheduler_start":
            job = d.get("job")
            row = {
                "job": job,
                "priority": d.get("priority"),
                "waited_ms": d.get("waited_ms"),
                "deadline_ms": d.get("deadline_ms"),
                "slot": d.get("slot"),
                "status": RUNNING,
                "duration_ms": None,
                "at": ev.get("at"),
            }
            open_rows.setdefault(job, []).append(row)
            rows.append(row)
        elif name == "scheduler_done":
            job = d.get("job")
            pending = [r for r in open_rows.get(job, []) if r["status"] == RUNNING]
            if pending:
                pending[0]["status"] = d.get("status") or "ok"
                pending[0]["duration_ms"] = d.get("duration_ms")
    return rows


def _layers(spec_nodes):
    """按 needs 求每个节点的层号，最长路径算法。

    屏上"按依赖分层"就是这个数：层号 = 从入口走到它要走几步。
    回边不参与分层 —— 它是往回走的路，算进去就成环了，图上永远画不出来。
    """
    idx = {n.get("id"): n for n in spec_nodes}
    memo = {}

    def depth(nid, stack=()):
        if nid in memo:
            return memo[nid]
        if nid in stack:                     # 兜底：真出环了也别递归到栈溢出
            return 0
        node = idx.get(nid)
        if not node:
            return 0
        needs = [x for x in (node.get("needs") or []) if x in idx]
        d = 0 if not needs else 1 + max(depth(x, stack + (nid,)) for x in needs)
        memo[nid] = d
        return d

    for n in spec_nodes:
        depth(n.get("id"))
    return memo


def _split_last(events, tail):
    """把「这一遍」的事件切出来（含跨运行的开跑痕迹）。

    同一 run_id 可以被追加再跑一遍，run_end 就是分界。上一遍的
    node_begin / scheduler_start 没有任何意义地留在账上，切不干净
    就永远有两个节点亮着（实测：追加提问之后 draft 与 verify 同时亮）。

    但 run_end 本身不进 events（Trace 写的是 type=run_end），所以
    「这一遍开没开跑」还得看时间：比它更晚的 node_begin 说明新的一遍
    已经在跑了，那种痕迹不能切掉 —— 否则刚点完提问，屏上反而整屏变灰。
    """
    if not events:
        return events
    cut = getattr(tail, "start", None)
    if cut is None:
        return events
    ended = _ts(tail.get("at") or tail.get("ended_at"))
    late = [ev for ev in events[cut:]
            if ev.get("event") == "node_begin" and _ts(ev.get("at")) > ended]
    return late if late else events[:cut]


def _open_jobs(events):
    """已经开跑、还没出结果的节点 —— 也就是"真正在跑的那几个"。

    两条通道分开记，因为同一个节点会被两边各写一条，混在一起记就会
    "开始记两次、结束减一次"，跑完的节点永远停在被点亮的状态：
      node 通道       node_begin → node_end（图自己的视角，W3-03 加）
      scheduler 通道  scheduler_start → done / late / timeout

    两条都要认：draft 走 execute() 根本不进调度器，只有 node_begin；
    超时判死之后线程还在跑，调度器那边已经 done 了，只有 node_end 说得清。

    run_end 是"这一遍跑完了"的分界（同一 run_id 可以被追加再跑一遍）。
    跨过分界还挂在账上的旧 start，是上一遍留下的，必须一起清掉 ——
    不清就永远有两个节点亮着，而实际上新的那一遍还没开跑。
    """
    node_open, sched_open = {}, {}
    for ev in events:
        name, d = ev.get("event"), ev.get("detail") or {}
        if name == "run_end":
            node_open.clear()       # 上一遍留下的未闭合痕迹，不带进下一遍
            sched_open.clear()
            continue
        job = d.get("node") or d.get("job")
        if not job:
            continue
        if name == "node_begin":
            node_open[job] = node_open.get(job, 0) + 1
        elif name == "node_end":
            if node_open.get(job):
                node_open[job] -= 1
        elif name == "scheduler_start":
            sched_open[job] = sched_open.get(job, 0) + 1
        elif name in ("scheduler_done", "scheduler_late", "scheduler_timeout"):
            if sched_open.get(job):
                sched_open[job] -= 1
    jobs = set(j for j, n in node_open.items() if n > 0)
    jobs |= set(j for j, n in sched_open.items() if n > 0)
    return sorted(jobs)


def _running_jobs(events, nodes, finished, retry_node=None, written_ids=None):
    """推断"现在谁在跑"。四级依据，从硬到软：

    1. 进了槽、还没出结果                      → 铁证（见 _open_jobs 的三种痕迹）
    2. run 还没结束、节点一个都没开过           → 取入口层（第一个真正跑起来的人）
    3. 打回重跑中                              → 回边目标在跑（它第一轮已写过
                                                 graph_node，靠"没写过"推不出来）
    4. 已结束                                  → 空

    written_ids：这一遍已经写过结果的节点。追加写会让节点在第一遍就写过，
    拿全文件的 attempts 当"写过"，第二遍就一路推到最后一层去。
    """
    opened = _open_jobs(events)
    if opened:
        return opened
    if finished:
        return []
    if written_ids is None:
        written_ids = {n["id"] for n in nodes if n["attempts"]}
    waiting = [n for n in nodes if n["id"] not in written_ids]
    if not waiting:
        return []
    if not written_ids:
        # 一个都还没跑过：入口节点正要起跑，屏上把它点亮。
        low = min(n["layer"] for n in nodes)
        return [n["id"] for n in nodes if n["layer"] == low]
    if retry_node:
        return [retry_node]
    low = min(n["layer"] for n in waiting)
    return [n["id"] for n in waiting if n["layer"] == low]


def _retry_target(events):
    """最近一次打回停在哪个节点上，以及这是第几轮。

    诊断事件是打回的"意图"，graph_loop 才是真的作废重跑；两个都看，
    谁后出现算谁 —— 只看 diagnosis 会把"诊断了但没重跑"也说成在跑。

    轮次只认「这一次运行」的：同一 run_id 可以被追加写（第二次提问会
    再写一套诊断事件），跨运行取 max 会让屏上永远挂着"第 2 轮"。
    """
    target, attempt = None, 0
    for ev in events:
        name, d = ev.get("event"), ev.get("detail") or {}
        if name == "diagnosis":
            # 新一轮运行以 attempt=0 的诊断开头，先清零再取。
            attempt = max(0 if int(d.get("attempt") or 0) == 0 else attempt,
                          int(d.get("attempt") or 0))
        elif name == "graph_loop":
            attempt = max(attempt, int(d.get("attempt") or 0))
            dst = str(d.get("edge") or "").split("->")
            if len(dst) == 2:
                target = dst[1].strip()
    return target, attempt


def live(run_id=None):
    """实时视图：在 build() 之上补「现在谁在跑」与「跑到第几轮」。

    只读同一份 jsonl、不碰模型、不重算任何账。请求一次就是一次快照，
    没有后台线程 —— 轮询由前端发起，接口本身无状态，重启即恢复。
    """
    d = build(run_id)
    if not d.get("ok"):
        return d

    head, tail, events = _read_events(d["run"]["run_id"])
    # 「跑完没」只能由 build() 一处说了算：live() 自己再判一次，
    # 两份判据迟早会走岔（实测就岔过 —— build 说在跑、live 说已结束）。
    finished = not d["run"].get("running")
    # 只看「这一遍」的痕迹：上一遍留下的 start / 结果不能拿来判这一遍。
    scope = _split_last(events, tail)
    retry_node, attempt = _retry_target(scope)
    written = {ev.get("detail", {}).get("node") for ev in scope
               if ev.get("event") == "graph_node"}
    running = _running_jobs(scope, d["nodes"], finished, retry_node, written)

    for n in d["nodes"]:
        n["round"] = attempt + 1 if attempt else 1
        # 以 live 的判定为准：build() 里没看 "run 是否结束"，
        # 跑完后残留的 scheduler_start 在这里必须被清掉。
        if finished:
            if n["status"] in (RUNNING, "retry"):
                n["status"] = PENDING
            continue
        if n["id"] in running:
            # 重跑态优先于进行中：回边目标已经在跑第二轮了，
            # 屏上该说的是"被打回了、正在重做"，不是一个干净的在跑。
            n["status"] = "retry" if retry_node == n["id"] else RUNNING
        elif n["status"] == RUNNING:
            n["status"] = PENDING

    # DAG 读的是 graph.nodes 这份快照，分层列表读 nodes —— 不回填就会
    # 出现"列表里在跑、图上还是灰的"。
    by_id = {n["id"]: n for n in d["nodes"]}
    for gn in d["graph"]["nodes"]:
        n = by_id.get(gn["id"])
        if n:
            gn["status"] = n["status"]
            gn["round"] = n["round"]

    d["run"]["running"] = not finished
    d["run"]["event_count"] = len(events)
    d["summary"]["running"] = [n["id"] for n in d["nodes"] if n["status"] in (RUNNING, "retry")]
    d["summary"]["pending"] = [n["id"] for n in d["nodes"] if n["status"] == PENDING]
    d["summary"]["retried"] = [n["id"] for n in d["nodes"] if n["attempts"] > 1]
    if retry_node:
        d["summary"]["retry_node"] = retry_node
        d["summary"]["round"] = attempt + 1
    return d


def build(run_id=None):
    """编排屏的整份数据。run_id 给空就取最近一次。"""
    if run_id and not RUN_ID_RE.match(run_id):
        return {"ok": False, "error": "run_id 不合法"}
    if not run_id:
        recent = list_runs(limit=1)
        if not recent:
            return {"ok": False, "error": "还没有任何一次运行记录"}
        run_id = recent[0]["run_id"]

    path = RUNS_DIR / (run_id + ".jsonl")
    if not path.exists():
        return {"ok": False, "error": "找不到这次运行：%s" % run_id}

    head, tail, events = _read_events(run_id)
    # 「还在跑」的判据不是"没有 run_end"，而是"最后一条事件之后没人写 run_end"。
    # 同一个 run_id 可以被追加写（W3-03 允许外部指定 run_id 重跑，
    # 接口靠这个让屏跟着走），于是文件里会出现多条 run_end 和新事件；
    # 只认 tail 存不存在，第二次提问追加的事件就永远看不见。
    last_event_at = events[-1].get("at") if events else None
    ended_at = tail.get("at") or tail.get("ended_at")
    # 毫秒级的「谁更晚」必须是比时间，不能比字符串（run_end 不带 at 时会退到
    # ended_at，而同一毫秒里 ended_at 可能比事件时间早一点点 —— 字符串比较
    # 在这两种格式上会给出相反的答案）。
    # 缺时间的 run_end（测试夹具、旧版本 trace 都可能没有）不能当成
    # "永远最晚" —— 那只会在下一次追加写之后把整份记录判成已结束。
    _end = _ts(ended_at) if ended_at else float("-inf")
    running = not (tail and (not last_event_at
                             or _end >= _ts(last_event_at)))
    spec = pipeline()
    states = _node_states(events)
    ledger = _schedule_ledger(events)
    layers = _layers(spec.get("nodes") or [])

    # 已经开始、但还没结束的节点：图里跑在另一条线程上，状态只能从
    # scheduler_start 看出来 —— graph_node 是跑完才写的。不补这一步，
    # 屏上就会一直显示"待跑"，直到那一格突然变绿。
    in_flight = {r["job"] for r in ledger if r["status"] == RUNNING}
    retry_node, attempt = _retry_target(events) if running else (None, 0)

    nodes = []
    for item in spec.get("nodes") or []:
        nid = item.get("id")
        visits = states.get(nid) or []
        last = visits[-1] if visits else None
        status = (last or {}).get("status")
        if not status:
            if nid in in_flight:
                status = "retry" if nid == retry_node else RUNNING
            else:
                status = PENDING
        nodes.append({
            "id": nid,
            "label": item.get("label") or nid,
            "agent": item.get("agent"),
            "action": item.get("action"),
            "kind": item.get("kind"),
            "priority": item.get("priority"),
            "needs": list(item.get("needs") or []),
            "layer": layers.get(nid, 0),
            "status": status,
            "round": (attempt + 1) if attempt else 1,
            "attempts": len(visits),
            "duration_ms": (last or {}).get("duration_ms"),
            "started_ms": (last or {}).get("started_ms"),
            "ended_ms": (last or {}).get("ended_ms"),
            "note": (last or {}).get("note"),
        })

    waves = [{
        "nodes": (ev.get("detail") or {}).get("nodes") or [],
        "order": (ev.get("detail") or {}).get("order") or [],
        "slots": (ev.get("detail") or {}).get("slots"),
        "at": ev.get("at"),
    } for ev in events if ev.get("event") == "graph_wave"]

    timeline = [_timeline_item(ev) for ev in events]
    timeline = [x for x in timeline if x]

    return {
        "ok": True,
        "run": {
            "run_id": run_id,
            "question": head.get("question", ""),
            "started_at": head.get("started_at"),
            "ended_at": tail.get("ended_at"),
            "status": tail.get("status") or RUNNING,
            # 屏上据此判断"还要不要继续轮询"：没写 run_end 就是还在跑
            "running": running,
            "duration_ms": tail.get("duration_ms"),
            "tokens": tail.get("tokens") or {},
            "step_count": tail.get("step_count"),
            "retry_count": tail.get("retry_count"),
            "event_count": len(events),
        },
        "graph": {
            "name": spec.get("name"),
            "version": spec.get("version"),
            "nodes": [{"id": n["id"], "label": n["label"], "needs": n["needs"],
                       "layer": n["layer"], "status": n["status"]}
                      for n in nodes],
            # 前进边不写在配置里：前进靠 needs，回退才靠 edges（这是 core/graph.py
            # 的硬规矩）。这里由 needs 派生出来给屏上画，屏上一条都不用自己推 ——
            # 自己推就等于把「图的形状」复制了一份到前端，换图时两边都要改。
            "forward_edges": [
                {"from": dep, "to": n["id"], "kind": "dependency"}
                for n in nodes for dep in n["needs"]
            ],
            "back_edges": [{
                "from": e.get("from"), "to": e.get("to"),
                "when": e.get("when"), "max_loops": e.get("max_loops"),
                "label": e.get("label"), "kind": "loop",
            } for e in (spec.get("edges") or [])],
            # 兼容 W3-01 已用的字段名：edges 仍指「显式写出来的边」。
            "edges": [{
                "from": e.get("from"), "to": e.get("to"),
                "when": e.get("when"), "max_loops": e.get("max_loops"),
                "label": e.get("label"),
            } for e in (spec.get("edges") or [])],
            "layer_count": (max([n["layer"] for n in nodes]) + 1) if nodes else 0,
        },
        "nodes": nodes,
        "waves": waves,
        "schedule": ledger,
        # 降级账与诊断账：屏上不该只说"失败了"，要说"为什么、换过谁"。
        # 事件名照 core/fallback.py 与 core/team.py 实际写下的那几个，
        # 不另起一套叫法 —— 屏上认的名字必须就是 jsonl 里的名字。
        "fallbacks": [_detail(ev) for ev in events if ev.get("event") in FALLBACK_EVENTS],
        "diagnoses": [_detail(ev) for ev in events
                      if ev.get("event") in ("diagnosis", "handoff")],
        "timeline": timeline,
        "summary": {
            "total": len(nodes),
            "passed": sum(1 for n in nodes if n["status"] == "ok"),
            "failed": sum(1 for n in nodes if n["status"] in ("fail", "error")),
            "running": [n["id"] for n in nodes if n["status"] == RUNNING],
            "timed_out": [n["id"] for n in nodes if n["status"] == "timeout"],
            "skipped": [n["id"] for n in nodes if n["status"] == "skip"],
            "pending": [n["id"] for n in nodes if n["status"] == PENDING],
            "retried": [n["id"] for n in nodes if n["attempts"] > 1],
            "degraded": any(ev.get("event") == "fallback" for ev in events),
        },
        "runs": list_runs(limit=12),
    }


def _detail(ev):
    d = dict(ev.get("detail") or {})
    d["event"] = ev.get("event")
    d["at"] = ev.get("at")
    return d


# 时间线上的每一行都要能自己说清发生了什么，屏上不再二次解释。
def _fallback_text(d):
    code = d.get("status_code")
    why = "%s %s" % (d.get("error_type") or "错误", code if code else "")
    return "主通道 %s 判定不可用 → 改走 %s（%s）" % (
        d.get("from"), d.get("to"), why.strip())


_TIMELINE_TEXT = {
    "graph_wave": lambda d: "同批 %d 个节点进队列（%d 个槽位）"
                            % (len(d.get("nodes") or []), d.get("slots") or 0),
    "scheduler_start": lambda d: "%s 取到槽位 %s，优先级 %s，等了 %.0f ms"
                                 % (d.get("job"), d.get("slot"),
                                    d.get("priority"), d.get("waited_ms") or 0),
    "scheduler_done": lambda d: "%s 结束：%s，耗时 %.0f ms"
                                % (d.get("job"), d.get("status"),
                                   d.get("duration_ms") or 0),
    "graph_node": lambda d: "%s（%s）→ %s，%.0f ms"
                            % (d.get("node"), d.get("agent"), d.get("status"),
                               d.get("duration_ms") or 0),
    "node_timeout": lambda d: "%s 超时（deadline %.0f ms），槽位已让出"
                              % (d.get("node"), d.get("deadline_ms") or 0),
    "fallback": _fallback_text,
    "fallback_exhausted": lambda d: "全部通道失败（试过 %d 条），本次请求放弃"
                                    % len(d.get("attempts") or []),
    "fallback_probe": lambda d: "全链冷却中，按原顺序半开探测",
    "channel_fail": lambda d: "通道 %s 失败：%s %s"
                              % (d.get("channel"), d.get("error_type") or "",
                                 d.get("status_code") or ""),
    "channel_recovered": lambda d: "通道 %s 恢复（此前连续失败 %s 次）"
                                   % (d.get("channel"), d.get("after_fails")),
    "channel_reopen": lambda d: "通道 %s 冷却结束，重新允许尝试" % (d.get("channel"),),
    "channel_unavailable": lambda d: "通道 %s 未启用：%s"
                                     % (d.get("name"), d.get("reason")),
    "channel_error_fatal": lambda d: "通道 %s 报非通道类错误，不降级：%s"
                                     % (d.get("channel"), d.get("error_type")),
    "diagnosis": lambda d: "第 %s 轮打回判定：%s → 策略 %s"
                           % (d.get("attempt"), d.get("label") or d.get("kind"),
                              d.get("strategy")),
    "handoff": lambda d: "重试预算用尽（%s 轮），转人工复核" % (d.get("attempts"),),
    "draft_prompt": lambda d: "注入提示 %s 字" % (d.get("injected_chars"),),
    "verify": lambda d: "数字回查：%s（核对 %s 处，无出处 %s 处）"
                        % ("通过" if d.get("verdict") == "pass" else "未通过",
                           d.get("checked") or 0, len(d.get("unsupported") or [])),
    "critic": lambda d: "语义质检：%s（严重问题 %s 条）"
                        % ("通过" if d.get("verdict") == "pass" else "打回",
                           d.get("high") or 0),
}

# 这几类事件在时间线上各自一个颜色，屏上扫一眼就知道哪段是"在花钱"。
_KIND = {
    "graph_wave": "wave",
    "scheduler_start": "queue",
    "scheduler_done": "queue",
    "graph_node": "node",
    "node_timeout": "bad",
    "fallback": "warn",
    "fallback_exhausted": "bad",
    "fallback_probe": "warn",
    "channel_fail": "dim",
    "channel_recovered": "good",
    "channel_reopen": "good",
    "channel_unavailable": "dim",
    "channel_error_fatal": "bad",
    "diagnosis": "warn",
    "handoff": "bad",
    "verify": "node",
    "critic": "node",
    "draft_prompt": "dim",
}


def _timeline_item(ev):
    name = ev.get("event")
    fn = _TIMELINE_TEXT.get(name)
    if not fn:
        return None
    d = ev.get("detail") or {}
    try:
        text = fn(d)
    except (TypeError, KeyError, ValueError):
        text = name
    return {
        "seq": ev.get("seq"),
        "at": ev.get("at"),
        "event": name,
        "kind": _KIND.get(name, "dim"),
        "text": text,
    }


# ---------------------------------------------------------------- W3-04 节点抽屉

# 每个节点"它的输入是什么"在图定义里是隐含的（靠 needs 推），
# 但这句话是要说给评委听的，必须能一句说清，所以在这里写死一份。
_NODE_INPUT = {
    "draft": "用户问题 + 本轮注入的修正策略（首轮为空）",
    "verify": "初稿全文 + 本轮取数工具返回的证据",
    "critique": "用户问题 + 初稿全文 + 工具返回值清单",
    "gate": "数字回查结论 + 语义质检结论",
    "curate": "通过质检的初稿 + 用户问题",
    "final_check": "成文后的最终答复 + 取数证据",
}

# 节点产出落到 payload 里的键，翻成人话给抽屉用。
_PAYLOAD_TEXT = (
    ("chars", lambda v: "产出 %s 字" % v),
    ("tools", lambda v: "调了 %s" % ("、".join(v) if v else "无工具")),
    ("checked", lambda v: "回查 %s 个数字" % v),
    ("unsupported", lambda v: ("无据数字 %s 个" % len(v)) if v else "全部有据"),
    ("high", lambda v: "高严重问题 %s 条" % v),
    ("issues", lambda v: "问题：%s" % ("、".join(v) if v else "无")),
    ("action", lambda v: "处置：%s" % ("退回重做" if v == "retry" else "转人工")),
    ("attempt", lambda v: "这是第 %s 轮" % v),
    ("kind", lambda v: "定性：%s" % v),
    ("strategy", lambda v: "下一轮策略：%s" % (v or "无")),
    ("error", lambda v: "异常：%s" % v),
)


def _graph_run(run_id):
    """读节点存档：payload 只在这里有，trace 里只有结论。"""
    path = GRAPH_RUNS_DIR / (run_id + ".json")
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _payload_lines(payload):
    """把 payload 翻成几条人话；认不出的键原样列出来，不偷偷丢。"""
    out, seen = [], set()
    for key, fmt in _PAYLOAD_TEXT:
        if key in (payload or {}):
            seen.add(key)
            try:
                out.append(fmt(payload[key]))
            except (TypeError, ValueError):
                out.append("%s：%s" % (key, payload[key]))
    for key, val in (payload or {}).items():
        if key not in seen and not isinstance(val, (dict, list)):
            out.append("%s：%s" % (key, val))
    return out


def _node_events(events, node_id, node_name=None):
    """这个节点自己的事件流：谁写的都算，只要 detail 里点了它的名。

    不止 node_begin/graph_node：verify、critic、diagnosis、draft_prompt
    都带 node 或由节点产出，抽屉里要能看到"这一步到底判了什么"。
    """
    names = {"node"}
    if node_name:
        names.add(node_name)
    out = []
    for ev in events:
        d = ev.get("detail") or {}
        if (d.get("node") in (None, node_id) and
                (d.get("node") == node_id or d.get("job") == node_id)):
            out.append({"seq": ev.get("seq"), "at": ev.get("at"),
                        "event": ev.get("event"), "detail": d})
    return out


def _run_tokens(run_id, agent):
    """按 step 行把 token 归到「哪个 Agent、第几次尝试」上。

    为什么不按"这步正在跑的时候产生了 token"去推：模型调用在回调里
    异步落地，跟 node_begin/node_end 的时间窗对不齐，推出来会串。
    step 行自带 agent 与 retry，是唯一不会串的依据。
    """
    path = RUNS_DIR / (run_id + ".jsonl")
    total = {"in": 0, "out": 0}
    mine = {"in": 0, "out": 0, "calls": 0, "by_retry": {}}
    if not path.exists():
        return total, mine
    try:
        with path.open("r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("type") != "step":
                    continue
                tok = rec.get("tokens") or {}
                tin, tout = int(tok.get("in") or 0), int(tok.get("out") or 0)
                total["in"] += tin
                total["out"] += tout
                if rec.get("agent") != agent or not (tin or tout):
                    continue
                mine["in"] += tin
                mine["out"] += tout
                mine["calls"] += 1
                slot = mine["by_retry"].setdefault(int(rec.get("retry") or 0),
                                                   {"in": 0, "out": 0, "calls": 0})
                slot["in"] += tin
                slot["out"] += tout
                slot["calls"] += 1
    except OSError:
        return total, mine
    return total, mine


def node_detail(run_id, node_id):
    """一个节点的全部证据：输入、输出、耗时、token、重试原因、它自己的事件流。

    验收口径（台账 W3-04）：能当面读出「第 3 个 Agent 在第 2 次重试时
    花了 1.8 秒」，且界面上有对应数字。所以这一份里必须有：
    order（第几个跑的）、visit（第几轮）、duration_ms、以及重试原因原文。
    """
    if not run_id or not RUN_ID_RE.match(str(run_id)):
        return {"ok": False, "error": "run_id 不合法"}
    path = RUNS_DIR / (run_id + ".jsonl")
    if not path.exists():
        return {"ok": False, "error": "找不到这次运行：%s" % run_id}

    head, tail, events = _read_events(run_id)
    spec = pipeline()
    item = None
    for it in spec.get("nodes") or []:
        if it.get("id") == node_id:
            item = it
            break
    if item is None:
        return {"ok": False, "error": "这张图里没有节点：%s" % node_id}

    spec_index = [it.get("id") for it in (spec.get("nodes") or [])]
    order = spec_index.index(node_id) + 1

    states = _node_states(events).get(node_id) or []
    last = states[-1] if states else {}
    visits = len(states)
    payload = (_graph_run(run_id).get("results") or {}).get(node_id, {}).get("payload") or {}

    # 重试原因：这一节点被重跑，一定是上一圈 gate 判了 fail，
    # 所以诊断事件的原文就是它的"为什么又被叫回来"。
    scope = _split_last(events, tail)
    diagnoses = [{"at": ev.get("at"), **(ev.get("detail") or {})}
                 for ev in scope if ev.get("event") == "diagnosis"]
    loops = [{"at": ev.get("at"), **(ev.get("detail") or {})}
             for ev in scope if ev.get("event") == "graph_loop"]

    evs = _node_events(scope, node_id, node_name=item.get("label"))
    tok_total, mine = _run_tokens(run_id, item.get("agent") or "")

    return {
        "ok": True,
        "run_id": run_id,
        "node": {
            "id": node_id,
            "label": item.get("label") or node_id,
            "agent": item.get("agent"),
            "action": item.get("action"),
            "priority": item.get("priority"),
            "needs": item.get("needs") or [],
            "order": order,
            "order_text": "第 %d 个 Agent" % order,
            "input": _NODE_INPUT.get(item.get("action")) or
                     ("依赖已完成的：" + "、".join(item.get("needs") or [])
                      if item.get("needs") else "入口节点，无前置输入"),
            "status": last.get("status") or PENDING,
            "started_ms": last.get("started_ms"),
            "ended_ms": last.get("ended_ms"),
            "duration_ms": last.get("duration_ms"),
            "visits": visits,
            "visit_text": "第 %d 次执行" % visits if visits else "尚未执行",
            "output": _payload_lines(payload),
            "payload_raw": payload,
            "tokens": {"in": mine["in"], "out": mine["out"], "calls": mine["calls"],
                       "by_retry": mine["by_retry"]},
            "run_tokens": tok_total,
        },
        "retry": {
            "count": max(0, visits - 1),
            "diagnoses": diagnoses,
            "loops": loops,
            "reason": (diagnoses[-1].get("why") if diagnoses else None),
            "signals": (diagnoses[-1].get("signals") if diagnoses else []),
            "strategy": (diagnoses[-1].get("strategy") if diagnoses else None),
            "kind": (diagnoses[-1].get("label") if diagnoses else None),
        },
        "events": evs,
        "run": {
            "question": head.get("question", ""),
            "running": not bool(tail),
            "status": tail.get("status") or RUNNING,
        },
    }


# ============================ W3-05 trace 回放 ============================
# 回放要的不是"再来一遍请求"，而是"这份记录本身"。所以接口一次把整条事件
# 脚本交出去，屏上按 t_ms 自己走帧：
#   - 断网可回放：拿到脚本之后不再联网，验收口径要的就是这条
#   - 拖时间轴不卡：拖到哪一帧是本地计算，不用等接口
# 一次运行的事件只有几十条（实测 26~67 条），整份交出去比"每帧问一次"便宜得多。

def _rel_ms(value, base):
    """事件时间换成相对开跑的毫秒。算不出来给 None —— 屏上就不给它排帧，
    而不是塞个 0 把它硬挤到第一帧去。"""
    if not value or base is None:
        return None
    try:
        return round((datetime.fromisoformat(str(value)) - base).total_seconds() * 1000, 1)
    except ValueError:
        return None


def _status_at(events, node_id):
    """走到这一刻，这个节点是什么状态。

    和 build() 用的是同一份 graph_node 事件与同一套状态词 —— 回放若自己
    另定一套，屏上就会出现"实时说 pass、回放说 fail"这种对不上的场面。
    """
    last = None
    for ev in events:
        if ev.get("event") != "graph_node":
            continue
        d = ev.get("detail") or {}
        if d.get("node") == node_id:
            last = d
    return (last or {}).get("status") or PENDING


def replay(run_id):
    """一份运行记录的完整回放脚本。

    返回体刻意保持"自带自足"：frames 里有屏上要画的一切，前端不需要
    再回头问任何接口；这也让验收可以在断网后继续拖时间轴。
    """
    if not run_id or not RUN_ID_RE.match(str(run_id)):
        return {"ok": False, "error": "run_id 不合法"}
    path = RUNS_DIR / (run_id + ".jsonl")
    if not path.exists():
        return {"ok": False, "error": "找不到这次运行：%s" % run_id}

    head, tail, events = _read_events(run_id)
    spec = pipeline()
    spec_nodes = spec.get("nodes") or []
    order = {n.get("id"): i for i, n in enumerate(spec_nodes)}

    base = None
    started = head.get("started_at")
    if started:
        try:
            base = datetime.fromisoformat(str(started))
        except ValueError:
            base = None

    # 帧 = 事件 + 在这一刻各节点的状态快照。
    # 快照每帧都算一遍：图上点一个节点要立刻显示"此刻它是什么颜色"，
    # 让前端攒状态就得在前端复制一套判定，迟早和 build() 走岔。
    frames, done, prev = [], [], {}
    for ev in events:
        name = ev.get("event")
        t = _rel_ms(ev.get("at"), base)
        done.append(ev)
        snap = {n.get("id"): _status_at(done, n.get("id")) for n in spec_nodes}
        # 只带变化的那几个：几十帧 × 六个节点全量塞进去，屏上每次都要重画。
        # 首帧不算变化 —— 一上来六个节点都还是"待跑"，那不是这次回放演出来的事。
        diff = ({} if not prev else
                {k: v for k, v in snap.items() if prev.get(k) != v})
        prev = snap
        frames.append({
            "i": len(frames),
            "t_ms": t,
            "event": name,
            "node": (ev.get("detail") or {}).get("node"),
            "detail": ev.get("detail") or {},
            "text": _detail(ev).get("text") or "",
            "changed": diff,
        })

    # 时间轴总长取"最后一帧的时间"，不取 run_end 的 duration_ms：
    # duration 是墙钟（含跑模型的时间），而帧是按事件时间排的，
    # 两者混用会让播放条走到头时还有帧没放完。
    total = max([f["t_ms"] for f in frames if f["t_ms"] is not None] or [0])
    if tail and tail.get("duration_ms") and total <= 0:
        total = tail.get("duration_ms")

    # 事件间隔太挤就按"每帧至少 120ms"拉长播放轴：真实运行往往几百毫秒
    # 就跑完（替身样本整段 282ms），原速播放一眨眼就没了，拖都没处拖。
    # 拉长的是播放节奏，不是数据 —— 帧上仍是真实的 t_ms，刻度位置按比例换算。
    play_ms = total
    MIN_FRAME_MS = 120.0
    if frames and total < len(frames) * MIN_FRAME_MS:
        play_ms = round(len(frames) * MIN_FRAME_MS, 1)

    # 进度条要有刻度：起止时间、以及每次打回的位置（现场最想反复看的就是这几处）
    marks = [{"t_ms": f["t_ms"], "kind": "loop",
              "text": "打回 %s（第 %s 轮）" % (
                  (f["detail"].get("edge") or ""), (f["detail"].get("attempt") or ""))}
             for f in frames if f["event"] == "graph_loop"]
    marks += [{"t_ms": f["t_ms"], "kind": "handoff",
               "text": "重试预算用尽，转人工复核"}
              for f in frames if f["event"] == "handoff"]

    return {
        "ok": True,
        "run": {
            "run_id": run_id,
            "question": head.get("question", ""),
            "started_at": started,
            "ended_at": (tail or {}).get("ended_at"),
            "status": (tail or {}).get("status") or RUNNING,
            "duration_ms": (tail or {}).get("duration_ms"),
            "event_count": len(events),
            "frame_count": len(frames),
            "finished": bool(tail),
        },
        "nodes": [{"id": n.get("id"), "label": n.get("label") or n.get("id"),
                   "agent": n.get("agent"), "index": order.get(n.get("id"), 0) + 1}
                  for n in spec_nodes],
        "total_ms": total,
        "play_ms": play_ms,
        "marks": marks,
        "frames": frames,
    }
