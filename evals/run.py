# -*- coding: utf-8 -*-
"""`python -m evals.run` —— 一条命令出回归报告（台账 W4-02）。

用法：
    python -m evals.run                 # 跑全部 10 题，出报告，存 evals/reports/
    python -m evals.run --repeat 3      # 连跑三次，看通过率波动（验收口径 ≤10%）
    python -m evals.run --only T02      # 只跑一题（改完某题想单验）
    python -m evals.run --no-save       # 只打印不落盘（试跑）
    python -m evals.run --fail-under 80 # 通过率低于 80% 时退出码 1（挂 CI 用）

退出码：0 跑完；1 跑挂（用例集读不了 / 全题都跑挂）；--fail-under 未达标；2 参数错。
"""
import argparse
import sys

from core.evalset import load_cases
from evals import runner


def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m evals.run",
        description="Eval Set 一键回归：跑全部用例，出通过率 / 耗时 / token / 失败清单")
    p.add_argument("--cases", default=None, help="用例集路径（默认 evals/cases.yaml）")
    p.add_argument("--industry", default=None,
                   help="跑之前切到指定行业（如 service），默认用当前行业")
    p.add_argument("--only", default=None,
                   help="只跑指定题号，逗号分隔（例：T02 或 E01,T03）")
    p.add_argument("--repeat", type=int, default=1, help="连跑几次（默认 1，验收用 3）")
    p.add_argument("--max-retry", type=int, default=1,
                   help="班组重试预算（默认 1，与线上 /api/ai/ask 一致）")
    p.add_argument("--label", default="", help="给这次回归起个名字（写进报告）")
    p.add_argument("--out-dir", default=None, help="报告目录（默认 evals/reports）")
    p.add_argument("--no-save", action="store_true", help="只打印，不写报告文件")
    p.add_argument("--no-compare", action="store_true", help="不与上一次报告对比")
    p.add_argument("--fail-under", type=float, default=None,
                   help="通过率低于该值时退出码 1")
    p.add_argument("--canned", default=None,
                   help="离线自检：按 JSON 文件里的答案查表作答，不调模型")
    p.add_argument("--reflow", action="store_true",
                   help="跑完后把判挂的题追加进用例集的 regression 分组（W4-03）")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        doc, cases = load_cases(args.cases)
    except (FileNotFoundError, ValueError) as exc:
        print("读不到用例集：%s" % exc, file=sys.stderr)
        return 2

    if args.only:
        want = {x.strip().upper() for x in args.only.split(",") if x.strip()}
        cases = [c for c in cases if str(c.get("id", "")).upper() in want]
        if not cases:
            print("--only 指定的题号不在用例集里：%s" % args.only, file=sys.stderr)
            return 2

    if args.repeat < 1:
        print("--repeat 至少是 1", file=sys.stderr)
        return 2

    # 换行业跑卷子（台账 W4-04）：回归机器不认识行业，只认识"当前这行配置"。
    # 切换必须发生在建 ask 之前 —— 工具、数据缓存、角色卡都在 select 里重置。
    if args.industry:
        from core import industry as ind
        try:
            ids = [x["id"] for x in ind.available()]
        except ind.IndustryError as exc:
            print("行业配置坏了：%s" % exc, file=sys.stderr)
            return 2
        if args.industry not in ids:
            print("没有这个行业：%s（可选：%s）" % (args.industry, "、".join(ids)),
                  file=sys.stderr)
            return 2
        ind.select(args.industry)

    if args.canned:
        import json
        from pathlib import Path
        try:
            answers = json.loads(Path(args.canned).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print("读不到 canned 答案：%s" % exc, file=sys.stderr)
            return 2
        ask = runner.make_canned_ask(answers)
    else:
        ask = runner.make_team_ask(max_retry=args.max_retry,
                                   runs_dir=runner.EVAL_RUNS_DIR)

    from core.industry import current as _industry_current
    meta = {
        "cases": args.cases or str(runner.EVALS_DIR / "cases.yaml"),
        "label": args.label,
        "max_retry": args.max_retry,
        "suite_name": doc.get("name") or "",
        "suite_version": doc.get("version"),
        "industry": _industry_current()[0],
        "canned": bool(args.canned),
        "model": None,
    }

    prev = None if args.no_compare or args.no_save else runner.load_latest(args.out_dir)
    if prev:
        # 两份卷子之间不比（台账 W4-04）：题号都不一样，比出来的"新增/消失"
        # 全是噪声。只跟同一份卷子的上一次比。
        from pathlib import Path
        prev_cases = (prev.get("meta") or {}).get("cases")
        if prev_cases and Path(prev_cases).name != Path(meta["cases"]).name:
            print("上一份报告用的是另一份卷子（%s），本次不做逐题对比。"
                  % Path(prev_cases).name, file=sys.stderr)
            prev = None

    reports = []
    for i in range(args.repeat):
        if args.repeat > 1:
            print("\n—— 第 %d/%d 次 ——" % (i + 1, args.repeat))
        rep = runner.run_suite(cases, ask, meta=dict(meta))
        models = {r.get("model") for r in rep["rows"] if r.get("model")}
        rep["meta"]["model"] = "、".join(sorted(models)) if models else (
            "canned" if args.canned else "未知")
        reports.append(rep)
        # 多次运行时只在最后一次跟历史比 —— 中间几次比出来的是噪声
        show_prev = prev if i == args.repeat - 1 else None
        print(runner.format_report(
            rep, prev=show_prev,
            repeats=reports if (args.repeat > 1 and i == args.repeat - 1) else None))

    final = reports[-1]
    if not args.no_save:
        # 连跑时每次一份（r1/r2/r3）：验收要能翻回第 1 次，不能只留最后一次
        paths = [runner.save_report(rep, args.out_dir,
                                    tag=("r%d" % (i + 1)) if args.repeat > 1 else "")
                 for i, rep in enumerate(reports)]
        print("\n报告已存 %s" % paths[-1])

    # 失败回流（W4-03）：把这次判挂的题收进卷子，下次必测。
    # 只在显式要求时写卷子 —— 回归顺手改数据文件，是最容易被忽略的副作用。
    if args.reflow:
        from evals import reflow
        try:
            _, cases_now = load_cases(args.cases)
            entries = reflow.collect_from_report(final, cases_now)
            if not entries:
                print("\n回流：本次没有可回流的题（无失败，或失败的全是待定口径）")
            else:
                res = reflow.append_entries(entries, args.cases)
                print("\n回流 %d 题 → regression 分组（下次必测）：" % len(res["added"]))
                print(res["text"])
                print("用例数 %d → %d" % (len(cases_now), len(cases_now) + len(res["added"])))
                for s in res["skipped"]:
                    print("  跳过 %s —— %s" % (s["question"][:30], s["why"]))
                if res["skipped"] and not res["added"]:
                    # 说明白为什么"回流 0 题"不是故障：回归只跑卷里的题，
                    # 挂了的题本来就在卷子里。要加新题，得从线上失败的 run 回流。
                    print("  （回归只跑卷子里的题，挂了的题已在卷中。"
                          "要把线上挂掉的新问题收进来：python -m evals.reflow --from-run <run_id>）")
        except (OSError, ValueError) as exc:
            print("\n回流失败（报告仍然有效）：%s" % exc, file=sys.stderr)

    if args.fail_under is not None and final["summary"]["rate"] < args.fail_under:
        print("\n通过率 %.1f%% 低于门槛 %.1f%%" % (
            final["summary"]["rate"], args.fail_under), file=sys.stderr)
        return 1

    # 全题跑挂通常意味着模型通道断了，不该被当成"通过率 0%"含糊过去
    if final["summary"]["passed"] == 0 and final["summary"]["total"] > 0:
        print("\n全部用例都没通过 —— 先查模型通道（/api/ai/models）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
