# -*- coding: utf-8 -*-
"""一键回归执行器（台账 W4-02）。

一条命令干四件事：读用例集 → 逐题真的问一遍班组 → 按 W4-01 的规则阅卷 →
出报告（通过率 / 平均耗时 / 平均 token / 失败清单）。

三个刻意的设计：

1. **问答（ask）与阅卷分开**。ask 是个可替换的回调：真跑接班组，测试接假模型。
   这样"回归机制本身"能被测，不必每次烧真 token。跑挂的题也照样记一行
   （原因写"跑挂了"，不是悄悄跳过），否则通过率会虚高。

2. **耗时和 token 从 trace 取，不另算**。trace 里每步都有 duration 与
   usage，回归报告直接汇总 —— 和编排屏看到的是同一份数，两处不会打架。

3. **回归的用例 trace 落 runs/eval/**，不落主目录。一次回归几十个 run，
   混进编排屏会把演示用的那几条淹掉；报告里带 run_id，要看单个照样翻得到。

报告为什么要落盘并保留上一份：单看"这次 80%"说明不了什么，跟上一次比
才知道是变好还是变坏（哪题新挂、哪题修好了）。
"""
import json
import time
import unicodedata
from datetime import datetime
from pathlib import Path

from core.evalset import check_case, load_cases

EVALS_DIR = Path(__file__).resolve().parent
REPORT_DIR = EVALS_DIR / "reports"
# 验收用的运行记录单独放一层：主目录留给演示与线上请求（见模块注释第 3 条）
EVAL_RUNS_DIR = EVALS_DIR.parent / "runs" / "eval"

ANSWER_KEEP = 1200          # 报告里每题留多少字答案（够复盘，不至于撑爆）


# ---------------------------------------------------------------- 小工具

def now_stamp():
    # 带毫秒：连跑三次往往落在同一秒里，只到秒会把前两次的报告互相覆盖掉
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]


def _width(s):
    """终端显示宽度：中日韩全角算 2（报告要能对齐，不能靠 len）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in str(s))


def _pad(s, n):
    s = str(s)
    w = _width(s)
    if w <= n:
        return s + " " * (n - w)
    # 超宽就截断（按宽度截，不按字符数）
    out, acc = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if acc + cw > n - 1:
            return out + "…"
        out, acc = out + ch, acc + cw
    return out


def _ljust(s, n):
    """只在不够宽时补空格，绝不截断 —— 标签被截掉比不对齐更难读。"""
    s = str(s)
    w = _width(s)
    return s if w >= n else s + " " * (n - w)


def _ms(ms):
    if ms is None:
        return "—"
    if ms < 1000:
        return "%dms" % round(ms)
    return "%.1fs" % (ms / 1000.0)


def _tok(n):
    return "{:,}".format(int(n or 0))


# ---------------------------------------------------------------- 真跑：接班组

def make_team_ask(max_retry=1, runs_dir=None, llm_factory=None):
    """返回一个 ask(question) 回调：真跑一次三人班组，带回耗时与 token。

    llm_factory 只为测试存在（换成按剧本回话的假模型）；默认走 ai_analyst.get_llm，
    也就是线上同一条模型通道链（含降级）。
    """
    from core.trace import STATUS_FAILED, STATUS_OK, Trace, load_run

    def ask(question):
        # trace 先建：模型要拿它记降级事件，工具要拿它记调用，都不能后补
        tr = Trace(question=question, source="eval", runs_dir=runs_dir,
                   tags={"eval": True})
        if llm_factory is None:
            from ai_analyst import get_llm
            llm = get_llm(tr)
        else:
            llm = llm_factory()
        from ai_analyst import query_stats, rank, trend
        tools = [query_stats, rank, trend]

        from core.team import run_team

        def _tokens():
            """token 从落盘的 run_end 读：summary() 只给总数，报告要分清 in/out。"""
            end = (load_run(tr.run_id, tr.runs_dir) or {}).get("end") or {}
            tk = dict(end.get("tokens") or {})
            tk.setdefault("in", 0)
            tk.setdefault("out", 0)
            tk["total"] = int(tk["in"]) + int(tk["out"])
            return tk

        try:
            out = run_team(question, llm, tools, trace=tr, max_retry=max_retry)
        except Exception as exc:                       # noqa: BLE001
            # 跑挂也要留痕：trace 收尾 + 把原因带回报告，不让这一题静默消失
            tr.finish(status=STATUS_FAILED,
                      error={"type": type(exc).__name__, "message": str(exc)[:400]})
            return {"answer": "", "error": "%s: %s" % (type(exc).__name__, str(exc)[:300]),
                    "duration_ms": tr.duration_ms, "tokens": _tokens(),
                    "run_id": tr.run_id, "passed": False, "retries": 0,
                    "handoff": False, "model": None}

        # 转人工 / 没过质检的 run 记 failed —— 与编排屏同一套状态口径
        status = STATUS_OK if out.get("passed") else STATUS_FAILED
        sm = tr.finish(status=status)
        return {
            "answer": out.get("answer") or "",
            "error": None,
            "duration_ms": out.get("duration_ms") if out.get("duration_ms") is not None
                           else sm.get("elapsed_ms"),
            "tokens": _tokens(),
            "run_id": tr.run_id,
            "passed": bool(out.get("passed")),
            "retries": int(out.get("retries") or 0),
            "handoff": bool(out.get("handoff")),
            "model": out.get("model"),
        }

    return ask


def make_canned_ask(answers):
    """测试 / 离线用：按题目查表给答案，不碰模型。耗时与 token 给固定值。"""
    def ask(question):
        ans = answers.get(question)
        if ans is None:
            return {"answer": "", "error": "canned 里没有这道题", "duration_ms": 0,
                    "tokens": {"in": 0, "out": 0, "total": 0}, "run_id": None,
                    "passed": False, "retries": 0, "handoff": False, "model": "canned"}
        return {"answer": ans, "error": None, "duration_ms": 100.0,
                "tokens": {"in": 60, "out": 40, "total": 100}, "run_id": None,
                "passed": True, "retries": 0, "handoff": False, "model": "canned"}
    return ask


# ---------------------------------------------------------------- 跑一遍

def run_suite(cases, ask, meta=None):
    """逐题问 + 阅卷 + 汇总。返回一份可直接落盘的报告。"""
    rows = []
    started = time.perf_counter()

    for c in cases:
        cid = c.get("id")
        q = c.get("question") or ""
        is_pending = (c.get("expect") or {}).get("type") == "pending"
        t0 = time.perf_counter()
        try:
            res = ask(q) or {}
            err = res.get("error")
        except Exception as exc:                        # noqa: BLE001
            res, err = {}, "%s: %s" % (type(exc).__name__, str(exc)[:300])
        wall = round((time.perf_counter() - t0) * 1000, 1)

        answer = "" if err else (res.get("answer") or "")
        if err:
            verdict = {"ok": False, "reason": "跑挂了：%s" % err,
                       "expect_type": (c.get("expect") or {}).get("type")}
        else:
            verdict = check_case(c, answer)

        tk = res.get("tokens") or {}
        total_tok = tk.get("total")
        if total_tok is None:
            total_tok = int(tk.get("in") or 0) + int(tk.get("out") or 0)

        rows.append({
            "id": cid,
            "question": q,
            "ok": bool(verdict["ok"]),
            "reason": verdict["reason"],
            "expect_type": verdict.get("expect_type"),
            "difficulty": c.get("difficulty"),
            "group": c.get("group") or "main",
            "trap": c.get("trap"),
            # 回流题还没有预期答案：跑（必测），但不判分 —— 详见 check_case
            "pending": bool(is_pending or verdict.get("pending")),
            # 答案留原文（截断）：判不过的时候要能直接读它，而不是重跑一遍
            "answer": answer[:ANSWER_KEEP],
            "duration_ms": res.get("duration_ms", wall),
            "wall_ms": wall,
            "tokens": {"in": int(tk.get("in") or 0), "out": int(tk.get("out") or 0),
                       "total": int(total_tok)},
            "run_id": res.get("run_id"),
            "team_passed": res.get("passed"),
            "retries": res.get("retries"),
            "handoff": res.get("handoff"),
            "model": res.get("model"),
            "error": err,
        })

    # 分母只算"有口径可判"的题：回流题口径未定时判过或判挂都不诚实（W4-03）
    scored_rows = [r for r in rows if not r.get("pending")]
    pending_rows = [r for r in rows if r.get("pending")]
    passed = sum(1 for r in rows if r["ok"])
    total = len(scored_rows)
    ms = [r["duration_ms"] or 0 for r in rows]
    tok = [r["tokens"]["total"] for r in rows]

    report = {
        "task": "W4-02",
        "at": datetime.now().isoformat(timespec="seconds"),
        "stamp": now_stamp(),
        "meta": meta or {},
        "summary": {
            "cases_total": len(rows),        # 卷子上有几题（含待定）
            "total": total,                  # 参与判分的有几题
            "pending": len(pending_rows),
            "passed": passed,
            "failed": sum(1 for r in scored_rows if not r["ok"]),
            "rate": round(passed / total * 100, 1) if total else 0.0,
            "avg_ms": round(sum(ms) / len(rows), 1) if rows else 0.0,
            "max_ms": round(max(ms), 1) if ms else 0.0,
            "avg_tokens": round(sum(tok) / len(rows), 1) if rows else 0.0,
            "total_tokens": sum(tok),
        },
        "failed_ids": [r["id"] for r in scored_rows if not r["ok"]],
        "pending_ids": [r["id"] for r in pending_rows],
        "rows": rows,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }
    return report


def spread_of(rates):
    """连跑多次的通过率波动（百分点）。少于两次无法谈波动，返回 None。"""
    if not rates or len(rates) < 2:
        return None
    return round(max(rates) - min(rates), 1)


# ---------------------------------------------------------------- 落盘

def save_report(report, out_dir=None, tag=""):
    """写 <stamp>[-tag].json + latest.json。latest 覆盖前先读出来做对比。

    tag 给连跑用（r1/r2/r3）：验收要能翻回第 1 次，不能只留最后一次。
    """
    d = Path(out_dir) if out_dir else REPORT_DIR
    d.mkdir(parents=True, exist_ok=True)
    name = "%s%s.json" % (report["stamp"], ("-" + tag) if tag else "")
    p = d / name
    body = json.dumps(report, ensure_ascii=False, indent=2)
    p.write_text(body, encoding="utf-8")
    (d / "latest.json").write_text(body, encoding="utf-8")
    return p


def load_latest(out_dir=None):
    """上一次的报告（没有就 None）。对比用，缺了不报错。"""
    d = Path(out_dir) if out_dir else REPORT_DIR
    p = d / "latest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def diff_reports(prev, cur):
    """两次回归的逐题差异：哪题新挂（regression）、哪题修好了（fixed）。"""
    if not prev:
        return {"regressed": [], "fixed": [], "rate_delta": None}
    # 待定口径的题不参与对比：它今天既不是"过"也不是"挂"
    old = {r["id"]: r["ok"] for r in prev.get("rows") or [] if not r.get("pending")}
    new = {r["id"]: r["ok"] for r in cur.get("rows") or [] if not r.get("pending")}
    regressed = [k for k in new if k in old and old[k] and not new[k]]
    fixed = [k for k in new if k in old and not old[k] and new[k]]
    return {
        "regressed": regressed,
        "fixed": fixed,
        "rate_delta": round(new_rate(cur) - new_rate(prev), 1),
    }


def new_rate(report):
    return float((report.get("summary") or {}).get("rate") or 0.0)


# ---------------------------------------------------------------- 打印

def format_report(report, prev=None, repeats=None):
    """人看的报告。对齐靠显示宽度，不靠 len —— 中文一列就散。"""
    m = report.get("meta") or {}
    s = report.get("summary") or {}
    lines = []
    lines.append("=" * 74)
    lines.append("W4-02 一键回归 · %s" % report.get("at"))
    lines.append("用例集 %s · %d 题%s · 行业 %s · 模型 %s · 重试预算 %s" % (
        m.get("cases") or "evals/cases.yaml", s.get("cases_total") or s.get("total") or 0,
        ("（其中 %d 题待定口径）" % s["pending"]) if s.get("pending") else "",
        m.get("industry") or "sales", m.get("model") or "—", m.get("max_retry", "—")))
    lines.append("=" * 74)

    for r in report.get("rows") or []:
        if r.get("pending"):
            mark = "待"
        else:
            mark = "✔" if r["ok"] else "✗"
        head = "  %s %s %8s %10s  %s" % (
            mark, _pad(r["id"], 5), _ms(r.get("duration_ms") or 0),
            _tok(r["tokens"]["total"]) + " tok", _pad(r["question"], 34))
        lines.append(head.rstrip())
        if not r["ok"] and not r.get("pending"):
            lines.append("       └ %s" % r["reason"])

    lines.append("-" * 74)
    rate_txt = "%d/%d = %.1f%%" % (s.get("passed") or 0, s.get("total") or 0,
                                   s.get("rate") or 0.0)
    if s.get("pending"):
        rate_txt += "（不含待定 %d 题）" % s["pending"]
    tail = ""
    if prev:
        d = diff_reports(prev, report)
        tail = "（上一次 %.1f%%，%+.1f 个点）" % (new_rate(prev), d["rate_delta"] or 0.0)
    lines.append(("通过率   %s %s" % (_ljust(rate_txt, 18), tail)).rstrip())
    lines.append("平均耗时 %s      平均 token %s      总 token %s" % (
        _ljust(_ms(s.get("avg_ms") or 0), 18), _tok(s.get("avg_tokens") or 0),
        _tok(s.get("total_tokens") or 0)))
    fail = report.get("failed_ids") or []
    lines.append("失败清单 %s" % ("、".join(fail) if fail else "无"))
    pend = report.get("pending_ids") or []
    if pend:
        # 回流题（W4-03）：跑了、答案留了，但口径还没定 —— 单独说清，不混进失败
        lines.append("待定口径 %s  （已跑未判分，补上 expect 后参与通过率）" % "、".join(pend))

    if prev:
        d = diff_reports(prev, report)
        if d["regressed"]:
            lines.append("新挂     %s  ← 上一版是过的，这一版挂了（先查这里）" %
                         "、".join(d["regressed"]))
        if d["fixed"]:
            lines.append("修好了   %s" % "、".join(d["fixed"]))

    if repeats:
        rates = [x["summary"]["rate"] for x in repeats]
        sp = spread_of(rates)
        ok = "✔ ≤10%" if (sp is not None and sp <= 10.0) else "✗ >10%"
        lines.append("连跑 %d 次 %s   波动 %.1f%%  %s" % (
            len(rates), " / ".join("%.1f%%" % x for x in rates), sp or 0.0, ok))
    return "\n".join(lines)
