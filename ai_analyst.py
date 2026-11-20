# -*- coding: utf-8 -*-
"""
AI 数据分析师班组的数据工具层（DeepSeek + LangChain create_agent）

W4-04 起这里不再认识任何行业：三个查询工具（条件统计 / 排名 / 趋势）
由 build_tools() 按当前行业配置动态构造 —— 参数名、列名、指标枚举、
docstring 全部来自 configs/<行业>.yaml。换行业 = 换配置，本文件零改动。

工具的形状刻意保持三个不变（结构比实现更值得稳定）：
    query_stats  条件查询统计   —— 回答"是多少"
    rank         按维度排名     —— 回答"谁最多"
    trend        按月时间趋势   —— 回答"怎么变"
行业可以换字段、换指标，但一个班组用这三种问法就能覆盖绝大多数数据问题。
"""
import json
import time
from pathlib import Path

import pandas as pd
from langchain.agents import create_agent
from langchain_core.tools import StructuredTool
from pydantic import create_model, Field

from data_source import load_csv
from core import roles

# 销售行业的数据文件（W4-04 之前的唯一数据口）。
# 现在仍被两处绑死销售的地方引用：大屏 dashboard / 管理后台（见 app.py）。
# Agent 层的数据文件改由行业配置给出，见 load_df()。
DATA_FILE = Path(__file__).parent / "data" / "data.csv"

# 小张的系统提示词。真本在 core/roles.py 的角色卡里，这里只是引用，
# 避免提示词出现两份、改了一处忘了另一处。（台账 W1-01）
# 注意：行业切换后此变量会随角色卡一起刷新（见 rebind_tools），别在
# 模块顶层缓存第二份 —— 引用同一真源。
SYSTEM_PROMPT = roles.ANALYST.system_prompt


def load_df():
    """加载当前行业的数据文件：统一走 data_source，共用同一份缓存与编码兜底。

    W4-04 前这里读死 data/data.csv；现在按行业配置取文件与必需列。
    文件不存在 / 编码不可识别 / 缺列时返回 None。
    """
    from core.industry import current
    cfg = current()[1]
    d = cfg["data"]
    need = [d["date_col"]] + [dim["column"] for dim in d["dims"]] + list(d["metrics"])
    df, _ = load_csv(Path(__file__).parent / d["file"], require_columns=need)
    return df


def _empty_result():
    return json.dumps({"提示": "当前没有数据文件，数据不足，无法回答"}, ensure_ascii=False)


def _filter(df, date, d, filters):
    """按条件过滤数据，返回 (过滤后 DataFrame, 是否命中)。

    date 是月份（2025-11）或具体日期（2025-11-05），统一按前缀匹配；
    其余条件是行业 dims，param → column 的翻译全在这一行字典推导里。
    """
    if date:
        df = df[df[d["date_col"]].astype(str).str.startswith(str(date))]
    for dim in d["dims"]:
        v = (filters or {}).get(dim["param"])
        if v:
            df = df[df[dim["column"]] == v]
    return df, len(df) > 0


def _stats(df, d):
    """求和指标 + 派生比率。哪些列是指标、哪个比率怎么算，配置说了算。"""
    out = {"记录数": int(len(df))}
    sums = {}
    for m in d["metrics"]:
        v = round(float(df[m].sum()), 2)
        sums[m] = v
        out[m] = int(v) if float(v).is_integer() else v
    for name, r in (d.get("derived") or {}).items():
        num, den = sums.get(r["num"], 0), sums.get(r["den"], 0)
        out[name] = round(num / den * 100, 2) if den else None
    return out


def _metric_names(d):
    return list(d["metrics"]) + list(d.get("derived") or {})


def build_tools(cfg=None):
    """按行业配置构造三个数据工具。

    参数签名用 pydantic create_model 现场生成 —— 行业的维度叫"省份"还是
    "问题类型"，参数就叫 province 还是 issue，模型的工具调用 schema 直接
    对上中文列名，不需要中间翻译层。三个工具的实现是全行业共用的：
    过滤 → 求和 → 派生比率，谁都不认识具体的行业词。
    """
    from core.industry import current as _cur
    cfg = cfg or _cur()[1]
    d = cfg["data"]
    date_col = d["date_col"]
    dims = d["dims"]
    default_metric = d.get("default_metric") or (d["metrics"] or [""])[0]

    def _pick(df, date, filters):
        df, hit = _filter(df, date, d, filters)
        return df, hit

    # ---------------- 工具 1：条件查询统计 ----------------
    q_fields = {"date": (str, Field(default="", description="月份（如 2025-11）或具体日期（如 2025-11-05），可留空"))}
    for dim in dims:
        q_fields[dim["param"]] = (str, Field(default="", description=dim.get("desc") or dim["column"]))
    QueryArgs = create_model("QueryStatsArgs", **q_fields)

    def _query_stats_impl(date: str = "", **filters) -> str:
        df = load_df()
        if df is None:
            return _empty_result()
        df, hit = _pick(df, date, filters)
        if not hit:
            return json.dumps({"提示": "没有符合条件的数据，数据不足"}, ensure_ascii=False)
        return json.dumps({"条件": dict(date=date or "", **{k: v for k, v in filters.items() if v}),
                           "统计": _stats(df, d)}, ensure_ascii=False)

    metric_desc = "、".join(_metric_names(d))
    dim_desc = "、".join(dim["column"] for dim in dims)
    query_stats = StructuredTool.from_function(
        func=_query_stats_impl, name="query_stats",
        description="按条件查询并统计：%s。所有条件可选，不填则统计全部数据。" % metric_desc,
        args_schema=QueryArgs)

    # ---------------- 工具 2：排名 ----------------
    RankArgs = create_model(
        "RankArgs",
        by=(str, Field(description="按哪个维度排名，只能是：%s 或 %s（按月）" % (dim_desc, date_col))),
        metric=(str, Field(default=default_metric, description="按哪个指标排名，只能是：%s" % metric_desc)),
        top_n=(int, Field(default=10, description="取前几名，默认 10")))

    def _rank_impl(by: str, metric: str = None, top_n: int = 10) -> str:
        metric = metric or default_metric
        df = load_df()
        if df is None:
            return _empty_result()
        cols = [date_col] + [dim["column"] for dim in dims]
        if by not in cols or metric not in _metric_names(d):
            return json.dumps({"提示": "参数错误：by 须为 %s，metric 须为 %s" % (dim_desc, metric_desc)},
                              ensure_ascii=False)
        dim = "月份" if by == date_col else by
        if dim == "月份":
            df = df.assign(月份=df[date_col].astype(str).str[:7])

        if metric in (d.get("derived") or {}):
            r = d["derived"][metric]
            g = df.groupby(dim).agg(**{r["num"]: (r["num"], "sum"), r["den"]: (r["den"], "sum")}).reset_index()
            g[metric] = (g[r["num"]] / g[r["den"]] * 100).round(2)
            g = g[[dim, metric]]
        else:
            g = df.groupby(dim)[metric].sum().round(2).reset_index()

        result = g.sort_values(metric, ascending=False).head(max(1, top_n))
        return json.dumps({"按%s排名（%s）" % (metric, by): result.to_dict("records")}, ensure_ascii=False)

    rank = StructuredTool.from_function(
        func=_rank_impl, name="rank",
        description="按维度排名，返回前 N 名。可用于对比谁多谁少。",
        args_schema=RankArgs)

    # ---------------- 工具 3：趋势分析 ----------------
    TrendArgs = create_model("TrendArgs", **q_fields)

    def _trend_impl(date: str = "", **filters) -> str:
        df = load_df()
        if df is None:
            return _empty_result()
        df, hit = _pick(df, date, filters)
        if not hit:
            return json.dumps({"提示": "没有符合条件的数据，数据不足"}, ensure_ascii=False)
        df = df.assign(月份=df[date_col].astype(str).str[:7])
        rows = []
        for m, g in df.groupby("月份"):
            r = {"月份": m}
            r.update(_stats(g, d))
            rows.append(r)
        return json.dumps({"月度趋势": rows}, ensure_ascii=False)

    trend = StructuredTool.from_function(
        func=_trend_impl, name="trend",
        description="按月查看 %s 的时间趋势，可用于对比和趋势分析。条件可选，含义同 query_stats。" % metric_desc,
        args_schema=TrendArgs)

    return [query_stats, rank, trend]


# 模块级默认工具 = 默认行业（sales）的一份。行业切换时由 rebind_tools()
# 整体替换这三个名字 —— 旧引用（tests、evals 的 from-import）拿到的是
# sales 工具，Agent 运行时则始终经 tools_now() 取当前行业。
query_stats, rank, trend = build_tools()


def tools_now():
    """当前行业的工具三件套。运行时一律走这里，别用模块级旧引用。"""
    return [query_stats, rank, trend]


def rebind_tools():
    """行业切换后重造工具并刷新模块级绑定（industry.select 回调）。"""
    global query_stats, rank, trend, SYSTEM_PROMPT
    query_stats, rank, trend = build_tools()
    SYSTEM_PROMPT = roles.ANALYST.system_prompt


# ---------------- 创建 Agent ----------------
def get_llm(trace=None):
    """带降级的模型（台账 W2-02）。

    返回的不是某一个模型，而是「一条通道链」：主模型坏了自动换备用，
    换过就写进 trace。上层（Team / create_agent）拿到的仍是一个普通模型对象。

    每次调用新建模型对象，但共享进程级的通道健康度：
    降级记忆要跨请求才有意义，而 trace 必须每个请求一个 ——
    这两件事生命周期不同，所以分开持有。
    """
    from core import fallback
    return fallback.build_model(trace=trace, state=fallback.shared_state())


def has_model_channel() -> bool:
    """有没有至少一条可用通道。没有就该给用户明确提示，而不是等到调用时炸。"""
    from core import fallback
    cfg = fallback.load_config()
    channels, _ = fallback.load_channels(cfg)
    return bool(channels)


def channel_report() -> dict:
    """通道链现状（不含密钥），给管理接口和编排屏看。"""
    from core import fallback
    cfg = fallback.load_config()
    channels, skipped = fallback.load_channels(cfg)
    st = fallback.shared_state()
    now = time.monotonic()
    rows = []
    for c in channels:
        tripped = c.name in st.tripped_at
        rows.append(dict(
            c.describe(),
            consecutive_fails=st.fails.get(c.name, 0),
            tripped=tripped,
            cooldown_left_s=(round(max(0.0, st.tripped_at[c.name] - now), 1)
                             if tripped else 0.0),
        ))
    return {
        "channels": rows,
        "unavailable": skipped,
        "fail_threshold": st.threshold,
        "cooldown_s": st.cooldown_s,
        "calls": st.calls,
        "last_ok": st.ok_name,
        "config": str(fallback.CONFIG_PATH),
    }


def get_analyst(trace=None):
    """创建 AI 数据分析师小张。

    不再缓存：agent 里包着模型对象，而模型对象持有本次请求的 trace。
    缓存 agent 会让第二次提问把降级事件写进第一次的轨迹里。
    构造开销只是几个对象，不值得为它冒串账的风险。
    """
    return create_agent(get_llm(trace), tools=tools_now(),
                        system_prompt=roles.ANALYST.system_prompt)


def ask_analyst(question: str, trace=None) -> str:
    """向小张提问，返回最终回答文本。

    传入 trace 时，本次调用中每一次模型/工具动作都会落进 runs/{run_id}.jsonl。
    trace 只影响"记录"，不影响"回答"——不传 trace 与改动前行为完全一致。
    """
    config = {}
    if trace is not None:
        from core.instrument import TraceCallbackHandler
        config["callbacks"] = [TraceCallbackHandler(trace)]

    result = get_analyst(trace).invoke(
        {"messages": [{"role": "user", "content": question}]}, config=config)
    return result["messages"][-1].content


# 回查不通过时最多重答几次。2 次是经验值：第一次多半能改对，
# 第三次还改不对，说明问题不在措辞而在取数，继续重试只是烧钱。
MAX_VERIFY_RETRY = 2


def ask_verified(question: str, trace=None, max_retry: int = MAX_VERIFY_RETRY) -> dict:
    """提问 → 数字回查 → 不合规就带着具体纠错意见重答。

    返回 {answer, verified, retries, verdict}。
    与 ask_analyst 的区别不是"更聪明"，而是"说错话会被发现"：
    回答里每个数字都要能在本次工具返回值里找到出处，找不到就重来。
    """
    from core import verifier
    from core.instrument import TraceCallbackHandler

    agent = get_analyst(trace)
    messages = [{"role": "user", "content": question}]
    evidence, answer, verdict, attempt = [], "", None, 0

    while True:
        handler = TraceCallbackHandler(trace, retry=attempt) if trace else None
        config = {"callbacks": [handler]} if handler else {}
        result = agent.invoke({"messages": messages}, config=config)
        answer = result["messages"][-1].content

        if handler:
            evidence.extend(verifier.evidence_from(handler.tool_outputs))
        verdict = verifier.verify(answer, evidence)

        if trace:
            trace.log(
                "verify",
                attempt=attempt,
                verdict="pass" if verdict.passed else "fail",
                checked=verdict.checked,
                unsupported=[c["raw"] for c in verdict.unsupported],
                evidence_count=verdict.evidence_count,
            )

        if verdict.passed or attempt >= max_retry:
            break

        # 把"哪几个数没出处"直接告诉它，比让它自己反省有效得多
        attempt += 1
        messages = messages + [
            {"role": "assistant", "content": answer},
            {"role": "user", "content": verdict.feedback()},
        ]

    return {
        "answer": answer,
        "verified": verdict.passed,
        "retries": attempt,
        "verdict": verdict,
    }


def ask_team(question: str, trace=None, max_retry: int = 1) -> dict:
    """三人班组作答：Analyst 取数出稿 → Critic 独立质检 → Curator 成文。

    与 ask_verified 的区别：ask_verified 只有一道规则回查（只认数字），
    这里多了一道 LLM 质检（能判断答非所问、越界承诺、口径混用），
    并把最终成文交给一个只负责"说人话 + 存档"的角色。
    """
    from core.team import run_team
    return run_team(question, get_llm(trace), tools_now(),
                    trace=trace, max_retry=max_retry)
