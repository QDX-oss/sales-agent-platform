# -*- coding: utf-8 -*-
"""三人班组 —— 把 Analyst / Critic / Curator 串成一条可回放的流水线。

    question
       |
       v
    Analyst  ←──────────────┐  取数 + 出初稿（唯一能调工具的角色）
       |                    │
       v                    │
    规则回查(verifier)  fail ─┤  数字找不到出处
       |                    │
       v                    │
    Critic   ---------- fail ┘  答非所问 / 越界承诺 / 口径混用
       |  pass
       v
    Curator      成文 + 存档（成文后再回查一次，脏了就退回初稿）

两道关卡各有分工，不是重复：
    规则回查  便宜、精确、只认数字。50 行能拦住 80% 的编造。
    Critic    贵、模糊、能判断"这句话该不该说"。规则拦不住的部分归它。

设计上真正要紧的一条：**Critic 拿不到工具**。
它没有 query_stats，就不能"再查一遍确认一下"。一旦它能查，它就会
用自己的查询结果给自己的判断背书，退回成自查自纠——那和在一个 prompt 里
写"请检查一下"没有区别，只是多花一次 token。
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import diagnose, roles, verifier
from core.fallback import fallback_events, last_model_of
from core.graph import FAIL, GRAPH_RUNS, OK, SKIP, Graph, Runner
from core.instrument import TraceCallbackHandler
from core.scheduler import apply_policy, load_policy
from core.budget import load_budget
from core.trace import STATUS_FAILED, Trace

__all__ = ["Team", "run_team", "build_analyst", "BRIEF_DIR",
           "SALES_PIPELINE", "pipeline"]

BRIEF_DIR = Path(__file__).resolve().parent.parent / "memory"

# 初稿被质检打回后最多重做几次（台账 W2-03 定为 2）。
# 为什么是 2 而不是 1：诊断式重试的两次分别对应两类不同的修法 ——
# 第一次多半是取数/口径（要换做法），第二次才是表达（只改措辞）。
# 只给 1 次，等于把"口径错"和"表达错"挤在同一轮里改，改不动。
# 为什么不是 3：第三次还不过，通常说明问题不在模型这一侧（数据里就没有，
# 或者问题本身有歧义），再重试只是烧钱。到点转人工兜底，把已经试过的方向写清楚。
MAX_DRAFT_RETRY = 2

TEAM_CFG = Path(__file__).resolve().parent.parent / "config" / "team.json"


def load_team_policy(path=None) -> dict:
    """重试预算从配置读（config/team.json），与角色卡、调度参数同一套做法。"""
    p = Path(path) if path else TEAM_CFG
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"max_retry": MAX_DRAFT_RETRY}
    try:
        n = max(0, int(cfg.get("max_retry", MAX_DRAFT_RETRY)))
    except (TypeError, ValueError):
        n = MAX_DRAFT_RETRY
    return {"max_retry": n}


# ---------------------------------------------------------------- 流程定义
# 这张图就是"工作流长什么样"的答案。以前它藏在 run() 的 while 循环缩进里，
# 现在它是一份可以直接看的 JSON：改流程改这里，不改代码逻辑。
SALES_PIPELINE = {
    "name": "sales.answer",
    "version": 3,
    "nodes": [
        # priority 是"同时能做几件事时谁先做"（台账 W2-01），不是流程顺序：
        # 顺序由 needs 决定，优先级只在同批争槽位时起作用。
        # 取数是一切的前提，给最高；两道关卡并级；成文可以等。
        {"id": "draft", "agent": "Analyst", "action": "draft", "needs": [],
         "run_if": "always", "label": "取数出稿", "priority": 100},
        # verify 与 critique 都只依赖 draft，互不依赖 —— 所以同批进队列。
        # 规则回查不花模型调用，Critic 花一次，两者本来就不该排队。
        {"id": "verify", "agent": "verifier", "action": "verify",
         "needs": ["draft"], "label": "数字回查", "priority": 90},
        # verify 先于 critique：槽位只有 1 个时，先把不花钱的回查做完，
        # 它有结论了再让 Critic 花那一次调用 —— 早失败早省钱。
        {"id": "critique", "agent": "Critic", "action": "critique",
         "needs": ["draft"], "label": "语义质检", "priority": 80},
        # 合议必须 always：回查失败时它照样要跑，否则两道关卡的意见凑不齐，
        # 打回去重做时只带着一条意见。
        {"id": "gate", "agent": "gate", "action": "gate", "kind": "gate",
         "needs": ["verify", "critique"], "run_if": "always", "label": "合议",
         "priority": 70},
        {"id": "curate", "agent": "Curator", "action": "curate",
         "needs": ["gate"], "label": "成文", "priority": 60},
        {"id": "final_check", "agent": "verifier", "action": "final_check",
         "needs": ["curate"], "label": "成文复检", "priority": 60},
    ],
    "edges": [
        # 唯一的一条回边。删掉它，重试就真的没有了 —— 不是被注释掉，是图里没这条路。
        {"from": "gate", "to": "draft", "when": "fail",
         "max_loops": MAX_DRAFT_RETRY, "label": "打回重做"},
    ],
}


def pipeline(with_curator: bool = True) -> dict:
    """返回一份图定义。关掉成文就把那两步从图里摘掉，而不是在代码里 if 一下。

    W4-04 起图定义从当前行业配置读取（configs/<行业>.yaml 的 pipeline 段，
    配置加载时已通过 Graph.check() 验过拓扑）；SALES_PIPELINE 常量保留为
    兜底 —— 配置缺失时拿它跑，行为与 W4-04 之前完全一致。
    """
    spec = None
    try:
        from core.industry import current
        p = current()[1].get("pipeline") or {}
        if p.get("nodes"):
            spec = json.loads(json.dumps(p))       # 深拷贝，别让调用方改到配置
    except Exception:                              # noqa: BLE001 —— 配置坏了别炸运行时
        spec = None
    if spec is None:
        spec = json.loads(json.dumps(SALES_PIPELINE))
    if not with_curator:
        spec["nodes"] = [n for n in spec["nodes"]
                         if n["id"] not in ("curate", "final_check")]
    return spec


# ---------------------------------------------------------------- 工具函数

def _loads_json(text: str) -> Optional[dict]:
    """从模型输出里硬抠一个 JSON 对象。

    真模型会把 JSON 包在 ```json 里、会在前面加一句"好的，我的判断是"，
    所以不能直接 json.loads。先剥代码块，再退化为"取第一个 { 到最后一个 }"。
    """
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (ValueError, TypeError):
        pass
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            obj = json.loads(s[i:j + 1])
            return obj if isinstance(obj, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def _normalize_critic(obj: Optional[dict]) -> dict:
    """把 Critic 的 JSON 归一成固定结构。

    一处刻意的不信任：模型可能写 pass=true 却同时列了一条 high 问题。
    这种自相矛盾时以"有 high 问题"为准 —— 宁可多打回一次，不要放过一稿。
    """
    if not isinstance(obj, dict):
        return {"pass": True, "issues": [], "fix": "", "high_count": 0,
                "unparsed": True}

    issues = []
    for item in obj.get("issues") or []:
        if not isinstance(item, dict):
            continue
        issues.append({
            "type": str(item.get("type") or "")[:24],
            "detail": str(item.get("detail") or "")[:200],
            "severity": "high" if str(item.get("severity") or "").lower() == "high" else "low",
        })
    high = [x for x in issues if x["severity"] == "high"]
    passed = bool(obj.get("pass") is True) and not high
    return {
        "pass": passed,
        "issues": issues,
        "fix": str(obj.get("fix") or "")[:300],
        "high_count": len(high),
        "unparsed": False,
    }


def _evidence_text(records: List[dict], limit_tools: int = 4, width: int = 600) -> str:
    """给 Critic 看的证据摘要：哪个工具、返回了什么。

    不把全量 JSON 塞进去，是因为 Critic 的活是"对照"，不是"重算"。
    太长反而会淹掉真正要看的那几个数。
    """
    if not records:
        return "（本次没有任何工具调用 —— 这本身就是问题：回答里的数字没有任何出处）"
    out = []
    for i, rec in enumerate(records[:limit_tools], 1):
        raw = rec.get("output")
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False, default=str)
        out.append("[%d] %s 返回：\n%s" % (i, rec.get("name") or "tool", raw[:width]))
    if len(records) > limit_tools:
        out.append("（另有 %d 次调用未展开）" % (len(records) - limit_tools))
    return "\n\n".join(out)


def _brief(question: str, draft: str, records: List[dict], attempt: int) -> str:
    """Critic 的输入：问题 + 初稿 + 证据。三样齐全才审得出东西。"""
    return (
        "【用户原问题】\n%s\n\n"
        "【分析师初稿】%s\n\n"
        "【本次工具真实返回】\n%s\n\n"
        "这是第 %d 稿。请按 rubric 输出裁决 JSON。"
        % (question, draft or "（空）", _evidence_text(records), attempt + 1)
    )


def _record_brief(question: str, answer: str, meta: dict) -> Optional[str]:
    """结论存档：Curator 的第二个职责。

    每次问答留一条，供以后的问题参考（"上周问过广东，结论是什么"）。
    追加写，不建索引 —— 现在量小，能用 cat 看完。
    """
    try:
        BRIEF_DIR.mkdir(exist_ok=True)
        path = BRIEF_DIR / "briefs.jsonl"
        rec = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "question": question,
            "answer": answer,
        }
        rec.update(meta)
        with open(path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return str(path)
    except OSError:
        return None             # 存档失败不影响答复


# ---------------------------------------------------------------- 班组

class Team:
    """一次提问的执行体。每次 run 新建一个，不复用状态。"""

    def __init__(self, llm, tools, trace: Optional[Trace] = None,
                 max_retry: Optional[int] = None, curator: bool = True,
                 checkpoint_dir=None, policy: Optional[dict] = None,
                 budget=None):
        from langchain.agents import create_agent

        self.llm = llm
        self.tools = list(tools)
        self.trace = trace
        # 重试预算默认读 config/team.json（台账 W2-03）；显式传值则听调用方的，
        # 测试要压到 0 或 1 次时不必改配置文件。
        if max_retry is None:
            max_retry = load_team_policy().get("max_retry", MAX_DRAFT_RETRY)
        self.max_retry = max(0, int(max_retry))
        self.use_curator = curator
        # 预算护栏（W2-04）：token / 模型调用上限，默认读 config/team.json，
        # 显式传值则听调用方 —— 验收要把它压到极低值，不能去改生产配置。
        self.budget = budget if budget is not None else load_budget()
        # 调度策略外置（台账 W2-01）：slots / deadline 来自 config/scheduler.json。
        # 没有这个文件就退回「该批几个就几个、不设时限」，与改造前行为一致。
        self.policy = dict(policy) if policy is not None else load_policy()
        self.slots = self.policy.get("slots")
        self.default_deadline_ms = self.policy.get("default_deadline_ms")
        # 有 trace 才写 checkpoint：两者用同一个 run_id，编排屏和断点续跑才对得上。
        # 没 trace 的场景（单测、一次性脚本）不落盘，免得堆一堆没人看的状态文件。
        self.checkpoint_dir = checkpoint_dir if checkpoint_dir is not None else (
            GRAPH_RUNS if trace is not None else None)
        # Analyst 是唯一带工具的 agent。Critic / Curator 直接 llm.invoke，
        # 不经过 create_agent —— 没有工具可 bind，也就没有机会自己查数。
        self.analyst = create_agent(llm, tools=self.tools,
                                    system_prompt=roles.ANALYST.system_prompt)

    # ---------- 内部 ----------

    def _config(self, agent: str, retry: int):
        if self.trace is None:
            return {}, None
        handler = TraceCallbackHandler(self.trace, agent=agent, retry=retry)
        return {"callbacks": [handler]}, handler

    def _draft(self, messages, attempt):
        config, handler = self._config("Analyst", attempt)
        result = self.analyst.invoke({"messages": messages}, config=config)
        return result["messages"][-1].content or "", handler

    def _critique(self, question, draft, records, attempt):
        config, _ = self._config("Critic", attempt)
        msg = [{"role": "system", "content": roles.CRITIC.system_prompt},
               {"role": "user", "content": _brief(question, draft, records, attempt)}]
        resp = self.llm.invoke(msg, config=config)
        text = getattr(resp, "content", resp) or ""
        verdict = _normalize_critic(_loads_json(text))
        verdict["raw"] = text[:400]
        return verdict

    def _curate(self, question, draft):
        # retry 固定 0：Curator 一轮只成文一次，它没有"第几次重试"这回事。
        # 若跟着初稿的 attempt 走，轨迹里会显示成"成文重试了 1 次"，是错的。
        config, _ = self._config("Curator", 0)
        msg = [{"role": "system", "content": roles.CURATOR.system_prompt},
               {"role": "user", "content": "【用户原问题】\n%s\n\n【已通过质检的初稿】\n%s"
                                           % (question, draft)}]
        resp = self.llm.invoke(msg, config=config)
        return (getattr(resp, "content", resp) or "").strip()

    # ---------- 主流程 ----------

    def _executors(self):
        """节点动作 -> 具体怎么做。图只管顺序，这里管干活。

        每个执行器接收 Ctx，返回 (status, payload)。
        status 是结论（通过/不通过），不是异常 —— 回查不通过是正常业务结果。
        """
        return {
            "draft": self._ex_draft,
            "verify": self._ex_verify,
            "critique": self._ex_critique,
            "gate": self._ex_gate,
            "curate": self._ex_curate,
            "final_check": self._ex_final_check,
        }

    # ---- 各节点 ----

    def _ex_draft(self, ctx):
        st = ctx.state
        block = st.get("retry_block") or ""
        # 把"本轮多了什么"落进轨迹。台账 W2-03 的验收要求是
        # 「第二轮 prompt 与第一轮有实质差异」，那就得让差异在文件里看得见，
        # 而不是只存在于内存里的一句话。
        ctx.log("draft_prompt", attempt=st["attempt"],
                strategy=(st["diagnoses"][-1]["strategy"] if st["diagnoses"] else ""),
                injected_chars=len(block),
                injected=block[:300],
                messages=len(st["messages"]),
                note=("本轮注入修正策略" if block else "首稿，无修正策略"))

        draft, handler = self._draft(st["messages"], st["attempt"])
        records = handler.tool_records if handler else []
        if handler:
            st["evidence"].extend(verifier.evidence_from(handler.tool_outputs))
        st["draft"] = draft
        st["records"] = records
        return OK, {"chars": len(draft), "tools": [r["name"] for r in records],
                    "strategy": (st["diagnoses"][-1]["strategy"] if st["diagnoses"] else "")}

    def _ex_verify(self, ctx):
        st = ctx.state
        v = verifier.verify(st["draft"], st["evidence"])
        st["verdict"] = v.to_dict()
        st["verify_feedback"] = "" if v.passed else v.feedback()
        ctx.log("verify", attempt=st["attempt"],
                verdict="pass" if v.passed else "fail",
                checked=v.checked,
                unsupported=[c["raw"] for c in v.unsupported],
                evidence_count=v.evidence_count)
        return (OK if v.passed else FAIL), {
            "checked": v.checked,
            "unsupported": [c["raw"] for c in v.unsupported],
        }

    def _ex_critique(self, ctx):
        st = ctx.state
        crit = self._critique(st["question"], st["draft"], st["records"], st["attempt"])
        st["critique"] = crit
        ctx.log("critic", attempt=st["attempt"],
                verdict="pass" if crit["pass"] else "fail",
                high=crit["high_count"],
                issues=[{"type": i["type"], "severity": i["severity"]}
                        for i in crit["issues"]],
                unparsed=crit["unparsed"])
        return (OK if crit["pass"] else FAIL), {
            "high": crit["high_count"],
            "issues": [i["type"] for i in crit["issues"]],
            "unparsed": crit["unparsed"],
        }

    def _ex_gate(self, ctx):
        """合议：两道关卡都过才算过；不过就先诊断，再决定这一轮怎么改。

        它不产生内容，只做决定，所以 kind=gate。
        重试预算从图上问（ctx.can_loop）—— 预算写在边的 max_loops 里，
        改图就能改"最多重做几次"，不用碰这个方法。
        """
        st = ctx.state
        v_ok = ctx.status("verify") == OK
        c_ok = ctx.status("critique") == OK
        passed = v_ok and c_ok
        st["passed"] = passed

        if passed:
            st["retry_block"] = ""
            return OK, {"verify": OK, "critic": OK}

        # 诊断（台账 W2-03）：先给这一稿定性，再决定下一轮怎么改。
        # 定性用的是关卡留下的痕迹，不问模型"你觉得自己错在哪" ——
        # 模型对自己的失败归因极不可靠，而规则可以写测试。
        diag = diagnose.classify(
            draft=st.get("draft", ""),
            verdict=st.get("verdict") or {},
            critique=st.get("critique") or {},
            records=st.get("records") or [],
            evidence=st.get("evidence") or [],
            attempt=int(st.get("attempt") or 0) + 1,
        )
        st["diagnoses"].append(diag.to_dict())

        if ctx.can_loop("gate"):
            st["attempt"] += 1
            block = diag.block()
            st["retry_block"] = block
            st["messages"] = st["messages"] + [
                {"role": "assistant", "content": st["draft"]},
                {"role": "user", "content": block + "\n\n" + self._feedback(
                    st.get("verify_feedback", ""), st["critique"])},
            ]
            ctx.log("diagnosis", attempt=st["attempt"], kind=diag.kind,
                    label=diag.label, strategy=diag.strategy, why=diag.why,
                    signals=diag.signals[:4], action="retry",
                    verify=ctx.status("verify"), critic=ctx.status("critique"))
            return FAIL, {"action": "retry", "attempt": st["attempt"],
                          "kind": diag.kind, "strategy": diag.strategy,
                          "verify": ctx.status("verify"),
                          "critic": ctx.status("critique")}

        # 预算用尽：不再自动改，转人工兜底，并把"试过哪些方向"一起交出去。
        # 只丢一句"失败了"等于让接手的人从头再走一遍。
        st["handoff"] = {
            "needed": True,
            "attempts": int(st.get("attempt") or 0),
            "kinds": [d["kind"] for d in st["diagnoses"]],
            "labels": [d["label"] for d in st["diagnoses"]],
            "last": diag.to_dict(),
        }
        st["final"] = diagnose.handoff_text(
            st["diagnoses"], int(st.get("attempt") or 0), diag.kind) + "\n\n" + st["draft"]
        ctx.log("handoff", attempts=int(st.get("attempt") or 0),
                kinds=st["handoff"]["kinds"], last_kind=diag.kind,
                note="重试预算用尽，停止自动修改，转人工复核")
        return FAIL, {"action": "give_up", "attempt": st.get("attempt"),
                      "kind": diag.kind,
                      "verify": ctx.status("verify"),
                      "critic": ctx.status("critique")}

    def _ex_curate(self, ctx):
        st = ctx.state
        try:
            text = self._curate(st["question"], st["draft"])
        except Exception as exc:                        # noqa: BLE001
            # 成文失败不该让整次问答崩掉：退回初稿，但要留下证据
            ctx.log("curator_error", error=type(exc).__name__, note=str(exc)[:200])
            st["final"] = st["draft"]
            return SKIP, {"error": type(exc).__name__}
        st["curated_text"] = text
        return OK, {"chars": len(text)}

    def _ex_final_check(self, ctx):
        """成文复检：Curator 也不免税。

        这一步是"不能改数字"这条规则唯一的执行方式 —— 靠提示词没用。
        脏了就退回初稿原文，并记下改了什么。
        """
        st = ctx.state
        text = st.get("curated_text") or ""
        check = verifier.verify(text, st["evidence"])
        if text and check.passed:
            st["final"] = text
            st["curated"] = True
            return OK, {"checked": check.checked}

        ctx.log("curator_reject",
                unsupported=[c["raw"] for c in check.unsupported],
                note="成文引入无据数字，退回初稿原文")
        st["final"] = st["draft"]
        st["curated"] = False
        return FAIL, {"unsupported": [c["raw"] for c in check.unsupported]}

    # ---------- 一次作业 ----------

    def run(self, question: str) -> Dict[str, Any]:
        started = time.perf_counter()
        state = {
            "question": question,
            "messages": [{"role": "user", "content": question}],
            "attempt": 0,
            "evidence": [],
            "draft": "",
            "records": [],
            "verdict": {},
            "verify_feedback": "",
            "critique": {},
            "passed": False,
            "diagnoses": [],        # 每次打回一条诊断（W2-03），按轮次排列
            "retry_block": "",      # 最近一轮注入给 Analyst 的策略段
            "handoff": None,        # 预算用尽时转人工的交接信息
            "curated_text": "",
            "final": "",
            "curated": False,
        }

        # 重试预算是图里一条边的属性。调用方的 max_retry 直接改那份定义，
        # 而不是在代码里另开一个计数器跟图上的数字打架。
        spec = pipeline(self.use_curator)
        for edge in spec["edges"]:
            if edge["from"] == "gate" and edge["to"] == "draft":
                edge["max_loops"] = self.max_retry
        # 优先级 / 时限写进同一份定义：图还是那张图，调度是另一层（台账 W2-01）
        apply_policy(spec, self.policy)

        graph = Graph.from_dict(spec)
        runner = Runner(graph, self._executors(), trace=self.trace,
                        checkpoint_dir=self.checkpoint_dir,
                        slots=self.slots,
                        default_deadline_ms=self.default_deadline_ms,
                        budget=self.budget)
        plan = runner.run(state)

        timed_out = list(plan.get("timed_out") or [])
        final = state.get("final") or state.get("draft") or ""
        degraded = False
        if not final:
            # 什么都没产出来（多半是取数节点超时）：如实说，不要拿空字符串糊过去
            degraded = True
            why = "、".join(timed_out) if timed_out else "没有产出内容"
            final = ("本次请求未能完成（%s），已中止，没有可交付的结论。"
                     "可以缩小问题范围后重试。" % why)

        # 预算中止（W2-04）：停下来了，但要停得像话 —— 已有的结论照常交，
        # 另说清楚"为什么停"和"这段结论没走完最后一步"。不把它算 failed：
        # 这不是系统出错，是护栏按设计生效。
        aborted = plan.get("aborted") or None
        if aborted:
            note = ("本次回答因预算上限提前中止（%s）。" % aborted.get("why", "预算到顶"))
            if state.get("final"):
                final = final + "\n\n—— " + note + "以上是中止前已完成的结论。"
            elif state.get("draft"):
                final = final + "\n\n—— " + note + "以上是中止前的初稿，尚未通过质检，仅供参考。"
            else:
                degraded = True
                final = note + "没有产出可交付的结论。"

        elapsed = round((time.perf_counter() - started) * 1000, 1)

        # 降级账（台账 W2-02）。从 trace 读而不是问模型对象：
        # trace 是唯一真源 —— 模型对象会被 create_agent 的 bind_tools 复制出
        # 副本，进程级健康度会被重启清空，只有写进 jsonl 的那一行不会变。
        if self.trace is not None:
            fallbacks = fallback_events(self.trace)
        else:
            raw = list(getattr(getattr(self.llm, "state", None), "events", []) or [])
            fallbacks = [e for e in raw if e.get("event") in
                         ("fallback", "fallback_exhausted", "fallback_probe")]

        result = {
            "answer": final,
            "draft": state.get("draft", ""),
            "curated": bool(state.get("curated")),
            "passed": bool(state.get("passed")),
            "retries": int(state.get("attempt") or 0),
            "critic": state.get("critique") or {},
            "verdict": state.get("verdict") or {},
            "duration_ms": elapsed,
            "timed_out": timed_out,
            "degraded": degraded,
            "aborted": aborted,
            "schedule": plan.get("schedule") or [],
            "graph": plan,               # 编排屏要画的就是这份
            # 模型降级（W2-02）：换过通道没有、换到谁、最后是谁答的
            "fallbacks": fallbacks,
            "fallback_used": any(e.get("event") == "fallback" for e in fallbacks),
            "model": last_model_of(self.llm),
            # 诊断账（W2-03）：每一轮打回判成了哪一类、下一轮换了什么做法
            "diagnoses": state.get("diagnoses") or [],
            "retry_strategies": [d.get("strategy") for d in (state.get("diagnoses") or [])],
            "handoff": state.get("handoff"),
        }

        _record_brief(question, final, {
            "passed": result["passed"],
            "curated": result["curated"],
            "retries": result["retries"],
            "timed_out": timed_out,
            "degraded": degraded,
            "aborted": (aborted or {}).get("kind") if aborted else None,
            "fallback_used": result["fallback_used"],
            "model": result["model"],
            "critic_issues": [i["type"] for i in (result["critic"] or {}).get("issues", [])],
            "diagnoses": [d.get("kind") for d in result["diagnoses"]],
            "handoff": bool(result["handoff"]),
            "run_id": getattr(self.trace, "run_id", None),
        })
        result["brief_path"] = str(BRIEF_DIR / "briefs.jsonl")
        return result

    @staticmethod
    def _feedback(verify_note: str, crit) -> str:
        """把两道关卡的意见合成一条给 Analyst 的具体要求。

        必须具体到数字/句子。只说"请再检查一下"，模型会原样再答一遍。
        """
        parts = []
        if verify_note:
            parts.append(verify_note)
        if crit and not crit["pass"]:
            detail = "；".join(
                "%s（%s）" % (i["detail"] or i["type"], i["type"])
                for i in crit["issues"] if i["severity"] == "high"
            )
            if crit.get("fix"):
                detail = (detail + "。" if detail else "") + "改法：" + crit["fix"]
            if detail:
                parts.append("质检员打回了这一稿：" + detail)
        parts.append("请按上述意见重新作答，不要重复上一稿的问题。")
        return "\n".join(parts)


def build_analyst(llm, tools):
    """单独构造 Analyst（供测试和外部脚本使用）。"""
    from langchain.agents import create_agent
    return create_agent(llm, tools=list(tools),
                        system_prompt=roles.ANALYST.system_prompt)


def run_team(question: str, llm, tools, trace: Optional[Trace] = None,
             max_retry: Optional[int] = None, curator: bool = True,
             budget=None) -> Dict[str, Any]:
    """一问一答一次班组作业。返回体可直接塞进 HTTP 响应。"""
    team = Team(llm, tools, trace=trace, max_retry=max_retry, curator=curator,
                budget=budget)
    try:
        return team.run(question)
    except Exception as exc:
        if trace is not None:
            trace.finish(status=STATUS_FAILED,
                         error={"type": type(exc).__name__, "message": str(exc)})
        raise
