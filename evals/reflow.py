# -*- coding: utf-8 -*-
"""失败样本回流（台账 W4-03）。

把「挂过的题」收进 `evals/cases.yaml` 的 `regression` 分组，下次回归必测。

三个来源：
    --from-report  上一次回归报告里判挂的题
    --from-run     某一次失败的 run（线上/演示现场挂掉的那一问）
    --scan N       最近 N 个 run 里状态是 failed 的全部收进来

两个刻意的边界：

1. **不猜预期答案。** 失败只能证明"这一答不对"，不能证明"什么才对"。
   所以回流的题写 `expect: {type: pending}`：下次照样跑、答案照样存档，
   但不判分（不进通过率分母）。等有人把预期值定下来，它就是普通题。
   自动编一个 expect 出来，等于让回归自己给自己出标准答案 —— 那套卷子废了。

2. **同一问法不重复收。** 卷子里已有这个问法（不管在 cases 还是 regression），
   就只报告"已有"，不再加一条。否则跑几次回归，同一道题能把卷子撑满。

写文件用的是**文本追加 + 解析校验**，不是 yaml 重写：cases.yaml 顶上的注释
（为什么钉死在这份数据上、三种判定怎么用）是给人看的，重写一次就没了。
追加后立刻 yaml.safe_load 验一遍，坏就回滚。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import yaml

from core.evalset import EVALS_DIR, load_cases
from core.trace import list_runs, load_run

CASES_PATH = EVALS_DIR / "cases.yaml"

REGRESSION_HEADER = """regression:
  # 失败回流（台账 W4-03）：回归/线上挂过的题，下次必测。
  # expect.type = pending 表示"答案还没定口径"：跑但不判分，补上 expect 后参与通过率。
"""


# ---------------------------------------------------------------- 生成条目

def _next_id(existing):
    """R01 起排，取现有最大值 +1（不复用空号，免得报告里的历史 id 指错题）。"""
    return _next_free({str(c.get("id") or "") for c in existing})


def _next_free(used):
    nums = [int(x[1:]) for x in used if x.startswith("R") and x[1:].isdigit()]
    return "R%02d" % (max(nums) + 1 if nums else 1)


def make_entry(question, cases, reason="", source="", run_id=None):
    """造一条回流用例。预期答案故意留空（pending），只记"怎么挂的"。"""
    question = (question or "").strip()
    date = datetime.now().strftime("%Y-%m-%d")
    where = "、".join(x for x in [("run %s" % run_id) if run_id else "", source, date] if x)
    return {
        "id": _next_id(cases),
        "question": question,
        "difficulty": "regression",
        "group": "regression",
        "trap": ("失败回流：%s。这条题当初真的挂过，收进卷子是为了下次不再挂"
                 % where),
        "expect": {"type": "pending"},
        "came_from": "；".join([x for x in
                                [("run %s" % run_id) if run_id else "", source, date,
                                 ("失败原因：%s" % reason[:120]) if reason else ""] if x]),
    }


# ---------------------------------------------------------------- 写回文件

def _entry_text(e):
    """一条用例的 YAML 文本（两空格缩进，与文件里现有条目一致）。"""
    lines = [
        "  - id: %s" % e["id"],
        "    question: %s" % _yaml_scalar(e["question"]),
        "    difficulty: %s" % e["difficulty"],
        "    group: regression",
        "    trap: %s" % _yaml_scalar(e["trap"]),
        "    expect:",
        "      type: pending",
        "    came_from: %s" % _yaml_scalar(e["came_from"]),
    ]
    return "\n".join(lines) + "\n"


def _yaml_scalar(s):
    """能裸写就裸写，不能被 YAML 原样读回来的才加引号。

    不用"看有没有特殊字符"这种猜法：真正的坑是"看着普通、其实是别的类型"——
    例如 came_from 以 2026-11-16 开头，YAML 会把它解析成日期对象。
    所以这里让 YAML 自己读一遍：读回来不等于原文，就老老实实加引号。
    """
    s = str(s)
    if not s:
        return '""'
    try:
        same = yaml.safe_load(s) == s
    except yaml.YAMLError:
        same = False
    return s if same else json.dumps(s, ensure_ascii=False)


def _insert_point(text):
    """regression 段的插入位置。返回 (字符下标, 是否需要先补段头)。"""
    lines = text.splitlines(keepends=True)
    offsets, acc = [], 0
    for ln in lines:
        offsets.append(acc)
        acc += len(ln)
    for i, ln in enumerate(lines):
        if ln.startswith("regression:"):
            j = i + 1
            # 段内继续走：空行、缩进条目、注释都算；遇到下一个顶层键就停
            while j < len(lines):
                if lines[j].strip() and not lines[j].startswith((" ", "\t", "#")):
                    break
                j += 1
            return (offsets[j] if j < len(lines) else acc), False
    return acc, True


def append_entries(entries, path=None, dry_run=False):
    """把条目追加进 regression 分组。返回 {added, skipped, path, text}。

    文件写坏了就回滚（原样恢复），并把异常抛出去 —— 用例集是回归的地基，
    不能因为一次回流把它改成读不出来的东西。
    """
    p = Path(path) if path else CASES_PATH
    raw = p.read_text(encoding="utf-8")
    doc, existing = load_cases(p)                     # 顺带校验原文件是好的
    seen_q = {(c.get("question") or "").strip() for c in existing}

    added, skipped = [], []
    for e in entries:
        q = (e.get("question") or "").strip()
        if not q:
            skipped.append({"question": q, "why": "空问题，跳过"})
            continue
        if q in seen_q:
            hit = next((c for c in existing if (c.get("question") or "").strip() == q), {})
            skipped.append({"question": q, "why": "卷里已有（%s）" % hit.get("id", "?")})
            continue
        seen_q.add(q)
        added.append(e)

    # 同一批里的 id 可能撞车（各自按现有卷子算出来的都是 R01）——
    # 落盘前统一重编号，保证文件里 id 唯一。
    used = {str(c.get("id") or "") for c in existing}
    fixed = []
    for e in added:
        e = dict(e)
        if not e.get("id") or str(e["id"]) in used:
            e["id"] = _next_free(used)
        used.add(str(e["id"]))
        fixed.append(e)
    added = fixed

    out = {"added": added, "skipped": skipped, "path": str(p), "text": ""}
    if not added or dry_run:
        out["text"] = "\n".join("  + %s %s" % (e["id"], e["question"]) for e in added)
        return out

    at, need_header = _insert_point(raw)
    body = "".join(_entry_text(e) for e in added)
    if need_header:
        new_text = raw + ("" if raw.endswith("\n") else "\n") + "\n" + REGRESSION_HEADER + body
    else:
        new_text = raw[:at] + body + raw[at:]

    try:
        parsed = yaml.safe_load(new_text)
        if not isinstance(parsed, dict) or not parsed.get("regression"):
            raise ValueError("追加后 regression 分组是空的")
    except (yaml.YAMLError, ValueError) as exc:
        raise ValueError("追加后用例集解析失败，已放弃写入：%s" % exc) from exc

    p.write_text(new_text, encoding="utf-8")
    out["text"] = "\n".join("  + %s %s" % (e["id"], e["question"]) for e in added)
    return out


# ---------------------------------------------------------------- 三个来源

def collect_from_report(report, cases):
    """回归报告里判挂的题（待定口径的不算 —— 它没被判过）。"""
    out = []
    for r in report.get("rows") or []:
        if r.get("ok") or r.get("pending"):
            continue
        out.append(make_entry(r.get("question"), cases,
                              reason=r.get("reason") or "",
                              source="回归报告 %s" % (report.get("stamp") or ""),
                              run_id=r.get("run_id")))
        cases = cases + [out[-1]]
    return out


def collect_from_run(run_id, cases, runs_dir=None):
    """从一次 run 回流：问题从 trace 的 run 头取，原因从 handoff/run_end 取。"""
    data = load_run(run_id, runs_dir)
    if not data:
        raise FileNotFoundError("找不到 run：%s" % run_id)
    q = (data.get("meta") or {}).get("question") or ""
    end = data.get("end") or {}

    # 原因优先取"转人工/打回"这类决策事件：事件里每条节点开始也带 note，
    # 顺手取第一条会取到"节点开始执行"这种废话，对复盘毫无用处。
    events = data.get("events") or []
    reason = ""
    for want in ("handoff", "curator_reject", "graph_loop", "diagnosis"):
        for ev in reversed(events):
            if ev.get("event") == want and (ev.get("detail") or {}).get("note"):
                reason = str(ev["detail"]["note"])[:120]
                break
        if reason:
            break
    if not reason:
        err = end.get("error") or {}
        reason = str(err.get("message") or "")[:120]
    if not reason:
        for ev in reversed(events):
            note = (ev.get("detail") or {}).get("note")
            if note:
                reason = str(note)[:120]
                break

    return [make_entry(q, cases, reason=reason,
                       source="run_end=%s" % (end.get("status") or "?"),
                       run_id=run_id)]


def collect_scan(limit, cases, runs_dir=None):
    """扫最近 limit 个 run，把 failed 的全收进来（转人工的也算）。"""
    out, cur = [], list(cases)
    for row in list_runs(limit=limit, runs_dir=runs_dir):
        if row.get("status") != "failed":
            continue
        try:
            made = collect_from_run(row["run_id"], cur, runs_dir)
        except FileNotFoundError:
            continue
        out.extend(made)
        cur = cur + made
    return out


# ---------------------------------------------------------------- 命令行

def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m evals.reflow",
        description="失败样本回流：把挂过的题收进 cases.yaml 的 regression 分组")
    ap.add_argument("--from-report", default=None, help="上一次回归报告 json")
    ap.add_argument("--from-run", default=None, help="某个 run_id（runs/*.jsonl）")
    ap.add_argument("--scan", type=int, default=None, help="扫最近 N 个 run，收所有 failed")
    ap.add_argument("--runs-dir", default=None, help="run 所在目录（默认 runs/）")
    ap.add_argument("--cases", default=None, help="用例集路径（默认 evals/cases.yaml）")
    ap.add_argument("--dry-run", action="store_true", help="只说不写")
    args = ap.parse_args(argv)

    if not (args.from_report or args.from_run or args.scan):
        ap.print_help()
        return 2

    try:
        _, cases = load_cases(args.cases)
    except (FileNotFoundError, ValueError) as exc:
        print("读不到用例集：%s" % exc, file=sys.stderr)
        return 2

    try:
        if args.from_report:
            rep = json.loads(Path(args.from_report).read_text(encoding="utf-8"))
            entries = collect_from_report(rep, cases)
            src = "报告 %s" % args.from_report
        elif args.from_run:
            entries = collect_from_run(args.from_run, cases, args.runs_dir)
            src = "run %s" % args.from_run
        else:
            entries = collect_scan(args.scan, cases, args.runs_dir)
            src = "最近 %d 个 run" % args.scan
    except (OSError, ValueError, FileNotFoundError) as exc:
        print("回流失败：%s" % exc, file=sys.stderr)
        return 2

    print("来源 %s，候选 %d 条" % (src, len(entries)))
    if not entries:
        print("没有可回流的题。")
        return 0

    try:
        res = append_entries(entries, args.cases, dry_run=args.dry_run)
    except ValueError as exc:
        print("写入失败：%s" % exc, file=sys.stderr)
        return 1

    if res["added"]:
        print(("将追加（dry-run）：" if args.dry_run else "已追加：") + "\n" + res["text"])
        print("用例数 %d → %d" % (len(cases), len(cases) + len(res["added"])))
        print("这些题 expect.type=pending：下次回归会跑，但先不判分。"
              "把预期答案补进 cases.yaml 的 regression 分组后，它们参与通过率。")
    for s in res["skipped"]:
        print("  跳过 %s —— %s" % (s["question"][:30], s["why"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
