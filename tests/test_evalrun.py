# -*- coding: utf-8 -*-
"""一键回归（台账 W4-02）的测试。

两件事分开测：
- **回归机制**（报告字段、跑挂不静默、连跑波动、与上次对比、落盘、命令行）
  用 canned 答案喂，不碰模型 —— 这些是纯逻辑，跑一百次也应该一个结果。
- **接线**（ask 真的调起班组、trace 真的记下耗时与 token）用假模型 + 临时用例集，
  走真实的 Graph / 回查 / 质检 / 成文，只把模型换成按剧本回话的。

为什么值得单测：回归报告是要拿来下结论的（"这一版没退化"），
它自己算错了没人会发现 —— 比 bug 更贵。
"""
import json
import sys
from pathlib import Path

import pytest
import yaml

from core.evalset import load_cases
from evals import runner
from evals import run as run_mod
from tests.fake_llm import ScriptedChatModel, two_turn_script


# ---------------------------------------------------------------- 夹具

@pytest.fixture(scope="module")
def suite():
    doc, cases = load_cases()
    return doc, cases


def perfect_answers(cases):
    """按用例的预期造一份"满分答案"：只要判定器没错，这些就该全过。

    数字按原值写（不四舍五入）：E07 的 38.92 若写成 38.9，判挂的是这份答案
    自己，不是被测的 Agent —— 造答案的手和判卷的手必须一样细。
    """
    out = {}
    for c in cases:
        exp = c.get("expect") or {}
        t = exp.get("type")
        if t == "exact":
            v = ("%.2f" % float(exp["value"])).rstrip("0").rstrip(".")
            out[c["question"]] = "答案是 %s 元。" % v
        elif t == "must_contain":
            out[c["question"]] = "结论：%s。" % "，".join(exp["keywords"])
        elif t == "must_refuse":
            out[c["question"]] = "数据里没有这项，无法回答。"
        elif t == "either":
            first = (exp.get("options") or [{}])[0]
            if first.get("type") == "must_contain":
                out[c["question"]] = "结论：%s。" % "，".join(first["keywords"])
            else:
                out[c["question"]] = "数据里没有这项，无法回答。"
        elif t == "pending":
            # 回流题没有口径，这里给一句占位（它跑但不判分）
            out[c["question"]] = "（回流题占位答案）"
    return out


# ---------------------------------------------------------------- 回归机制

def test_一键回归出报告含四项指标(suite):
    """验收口径点名的四项：通过率、平均耗时、平均 token、失败清单。"""
    _, cases = suite
    scored = [c for c in cases if (c.get("expect") or {}).get("type") != "pending"]
    ans = perfect_answers(cases)
    ans[scored[0]["question"]] = "（故意答错）"          # 制造一挂，看失败清单
    rep = runner.run_suite(cases, runner.make_canned_ask(ans), meta={"cases": "t"})

    s = rep["summary"]
    assert s["cases_total"] == len(cases)               # 卷上题数（含待定）
    assert s["total"] == len(scored)                    # 参与判分的题数
    assert s["passed"] == len(scored) - 1
    assert s["rate"] == round((len(scored) - 1) / len(scored) * 100, 1)
    assert s["avg_ms"] > 0 and s["avg_tokens"] > 0
    assert rep["failed_ids"] == [scored[0]["id"]]
    # 判不过的题要带理由，不能只有一个 ✗
    bad = [r for r in rep["rows"] if not r["ok"] and not r.get("pending")][0]
    assert bad["reason"] and bad["answer"] == "（故意答错）"


def test_全对时通过率一百(suite):
    _, cases = suite
    rep = runner.run_suite(cases, runner.make_canned_ask(perfect_answers(cases)))
    assert rep["summary"]["rate"] == 100.0
    assert rep["failed_ids"] == []


def test_跑挂的题不会被静默跳过(suite):
    """模型通道断了 / 抛异常时，这一题必须留在报告里并写明原因。"""
    _, cases = suite

    def boom(question):
        raise RuntimeError("模型通道断了")

    rep = runner.run_suite(cases[:3], boom)
    assert rep["summary"]["total"] == 3
    assert rep["summary"]["passed"] == 0
    assert all("跑挂了" in r["reason"] for r in rep["rows"])
    assert all(r["error"] for r in rep["rows"])


def test_返回体里带error时同样算挂(suite):
    _, cases = suite

    def half(question):
        return {"answer": "", "error": "http 502", "duration_ms": 5,
                "tokens": {"in": 1, "out": 0}}

    rep = runner.run_suite(cases[:2], half)
    assert rep["summary"]["passed"] == 0
    assert "跑挂了" in rep["rows"][0]["reason"]


def test_连跑三次通过率波动为零(suite):
    """同一份答案连跑三次，报告里的通过率必须一模一样（波动 0 ≤ 10%）。"""
    _, cases = suite
    ask = runner.make_canned_ask(perfect_answers(cases))
    reps = [runner.run_suite(cases, ask) for _ in range(3)]
    rates = [r["summary"]["rate"] for r in reps]
    assert len(set(rates)) == 1
    assert runner.spread_of(rates) == 0.0


def test_波动算法按百分点算(suite):
    assert runner.spread_of([80.0, 80.0, 90.0]) == 10.0
    assert runner.spread_of([80.0, 90.0, 70.0]) == 20.0
    assert runner.spread_of([90.0]) is None      # 一次谈不上波动


def test_与上一次报告对比能指出新挂和修好(suite):
    _, cases = suite
    ans = perfect_answers(cases)
    cur = runner.run_suite(cases, runner.make_canned_ask(ans))

    ans2 = dict(ans)
    ans2[cases[0]["question"]] = "（答错）"     # 让第一题新挂
    prev = runner.run_suite(cases, runner.make_canned_ask(ans2))

    d = runner.diff_reports(prev, cur)
    assert d["regressed"] == []
    assert d["fixed"] == [cases[0]["id"]]
    assert d["rate_delta"] > 0

    # 反过来：这次挂了、上次是过的 → regressed
    d2 = runner.diff_reports(cur, prev)
    assert d2["regressed"] == [cases[0]["id"]]
    assert d2["fixed"] == []
    assert d2["rate_delta"] < 0


def test_没有上一次时对比不报错(suite):
    _, cases = suite
    rep = runner.run_suite(cases, runner.make_canned_ask(perfect_answers(cases)))
    assert runner.diff_reports(None, rep) == {"regressed": [], "fixed": [],
                                              "rate_delta": None}


def test_报告落盘与读回(tmp_path, suite):
    _, cases = suite
    rep = runner.run_suite(cases, runner.make_canned_ask(perfect_answers(cases)))
    p = runner.save_report(rep, tmp_path)
    assert p.exists()
    back = runner.load_latest(tmp_path)
    assert back["summary"]["rate"] == rep["summary"]["rate"]
    assert len(back["rows"]) == len(cases)
    # 单个报告也要能落（连跑时每次一份）
    assert json.loads(p.read_text(encoding="utf-8"))["stamp"] == rep["stamp"]


def test_打印的报告里带各项指标(suite):
    _, cases = suite
    rep = runner.run_suite(cases, runner.make_canned_ask(perfect_answers(cases)))
    txt = runner.format_report(rep)
    assert "通过率" in txt and "平均耗时" in txt and "平均 token" in txt
    assert "失败清单" in txt


# ---------------------------------------------------------------- 命令行

def _canned_file(tmp_path, answers):
    p = tmp_path / "canned.json"
    p.write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_命令行一条命令出报告(tmp_path, suite):
    _, cases = suite
    out = tmp_path / "reports"
    rc = run_mod.main(["--canned", _canned_file(tmp_path, perfect_answers(cases)),
                       "--out-dir", str(out), "--no-compare",
                       "--label", "自检"])
    assert rc == 0
    latest = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    assert latest["meta"]["label"] == "自检"
    assert latest["summary"]["cases_total"] == len(cases)


def test_命令行只跑指定题(tmp_path, suite):
    _, cases = suite
    out = tmp_path / "r"
    rc = run_mod.main(["--canned", _canned_file(tmp_path, perfect_answers(cases)),
                       "--only", "T02", "--out-dir", str(out), "--no-compare"])
    assert rc == 0
    latest = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    assert latest["summary"]["total"] == 1
    assert latest["rows"][0]["id"] == "T02"


def test_命令行连跑三次会给出波动(tmp_path, suite, capsys):
    _, cases = suite
    out = tmp_path / "r3"
    rc = run_mod.main(["--canned", _canned_file(tmp_path, perfect_answers(cases)),
                       "--repeat", "3", "--out-dir", str(out), "--no-compare"])
    assert rc == 0
    txt = capsys.readouterr().out
    assert "连跑 3 次" in txt and "波动" in txt
    # 三次各留一份 + latest，验收要能翻回第 1 次
    assert len(list(out.glob("*.json"))) >= 4


def test_命令行读不到用例集返回2(tmp_path):
    assert run_mod.main(["--cases", str(tmp_path / "nope.yaml")]) == 2


def test_命令行题号不存在返回2(tmp_path, suite):
    _, cases = suite
    rc = run_mod.main(["--canned", _canned_file(tmp_path, perfect_answers(cases)),
                       "--only", "不存在的题", "--no-save"])
    assert rc == 2


def test_全题跑挂时退出码非零并提示查通道(tmp_path, suite, capsys):
    """全 0% 通常不是"模型答得差"，是通道断了 —— 不能混成一份普通报告。"""
    _, cases = suite
    rc = run_mod.main(["--canned", _canned_file(tmp_path, {}),
                       "--out-dir", str(tmp_path / "r0"), "--no-compare"])
    assert rc == 1
    assert "先查模型通道" in capsys.readouterr().err


def test_通过率门槛不达标退出码1(tmp_path, suite):
    _, cases = suite
    rc = run_mod.main(["--canned", _canned_file(tmp_path, {}),
                       "--out-dir", str(tmp_path / "rg"), "--no-compare",
                       "--fail-under", "80"])
    assert rc == 1


# ---------------------------------------------------------------- 接线（假模型跑真班组）

def test_真跑一次班组并记下耗时与token(tmp_path, suite):
    """走真实 Graph / 回查 / 质检 / 成文，只把模型换成按剧本回话的假模型。

    这里验的是接线：ask 能不能真的把一次班组跑起来，并把耗时与 token 带回来。
    """
    doc, _ = suite
    cases = [
        {"id": "A1", "question": "广东销售额是多少？", "difficulty": "easy",
         "expect": {"type": "must_contain", "keywords": ["占位答案"], "mode": "all"}},
        {"id": "A2", "question": "随便问个数？", "difficulty": "easy",
         "expect": {"type": "exact", "value": 999.0}},
    ]
    runs_dir = tmp_path / "runs"
    ask = runner.make_team_ask(max_retry=0, runs_dir=runs_dir,
                               llm_factory=lambda: ScriptedChatModel(
                                   script=two_turn_script(province="广东")))
    rep = runner.run_suite(cases, ask, meta={"cases": "临时"})

    assert rep["summary"]["total"] == 2
    first = rep["rows"][0]
    assert first["ok"] is True                       # 剧本里的答案就是"占位答案"
    assert first["tokens"]["total"] > 0              # token 从 trace 里汇出来的
    assert first["duration_ms"] is not None
    assert first["run_id"]                        # 每题一个 run，能回看
    assert rep["rows"][1]["ok"] is False             # 999 没出现，不该判过
    # 回归的 run 落在自己的目录里，不混进演示用的 runs/
    assert len(list(runs_dir.glob("*.jsonl"))) == 2


# ---------------------------------------------------------------- 第二份卷子（客服行业，台账 W4-04）

SERVICE_SUITE = Path(__file__).resolve().parents[1] / "evals" / "cases_service.yaml"


def answers_for(cases):
    """按用例的 expect 现场造答案，不写死题号 —— 卷子加题这里不用改。"""
    out = {}
    for c in cases:
        exp = c.get("expect") or {}
        t = exp.get("type")
        if t == "exact":
            out[c["question"]] = "答案是 %s。" % exp["value"]
        elif t == "must_contain":
            out[c["question"]] = "；".join(str(k) for k in exp["keywords"])
        elif t == "must_refuse":
            out[c["question"]] = "这个我答不了：数据里没有该字段，不能编一个数出来。"
        elif t == "either":
            first = (exp.get("options") or [{}])[0]
            if first.get("type") == "must_contain":
                out[c["question"]] = "；".join(str(k) for k in first["keywords"])
            else:
                out[c["question"]] = "这个我答不了。"
        else:
            out[c["question"]] = "占位"
    return out


def test_客服卷的结构与销售卷同构():
    """第二份卷子不能是"随手写的几道题"：判定方式、came_from、易错题数量都要跟销售卷一个标准。"""
    doc, cases = load_cases(str(SERVICE_SUITE))
    assert doc.get("data_file") == "data/service.csv"
    assert len(cases) == 9
    assert sum(1 for c in cases if c.get("difficulty") == "trap") == 3
    for c in cases:
        assert c.get("came_from"), "%s 缺 came_from" % c["id"]
        assert (c.get("expect") or {}).get("type") in (
            "exact", "must_contain", "must_refuse", "either"), c["id"]
    # 题号不跟销售卷撞：两份卷子混着看时能一眼分清是哪边的
    _, sale = load_cases()
    assert not ({c["id"] for c in cases} & {c["id"] for c in sale})


def test_客服卷全部答对时应判满分(tmp_path):
    """判定器不认识行业：把客服卷的标准答案喂进去，必须 100% —— 否则是判定器有行业偏见。"""
    _, cases = load_cases(str(SERVICE_SUITE))
    out = tmp_path / "svc"
    rc = run_mod.main(["--cases", str(SERVICE_SUITE), "--canned",
                       _canned_file(tmp_path, answers_for(cases)),
                       "--out-dir", str(out), "--no-compare"])
    assert rc == 0
    latest = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    assert latest["summary"]["rate"] == 100.0


def test_指定行业后报告里写明是哪个行业(tmp_path):
    """不写行业，两份卷子的报告混在一个目录里就认不出谁是谁。"""
    from core import industry as ind
    _, cases = load_cases(str(SERVICE_SUITE))
    out = tmp_path / "svc2"
    try:
        rc = run_mod.main(["--cases", str(SERVICE_SUITE), "--industry", "service",
                           "--canned", _canned_file(tmp_path, answers_for(cases)),
                           "--out-dir", str(out), "--no-compare"])
    finally:
        ind.select("sales")          # 进程内状态：测完必须切回来，不污染后面的用例
    assert rc == 0
    latest = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    assert latest["meta"]["industry"] == "service"


def test_行业名不存在返回2并说清可选(tmp_path, capsys):
    rc = run_mod.main(["--industry", "不存在的行业", "--no-save"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "没有这个行业" in err and "sales" in err


def test_跨卷子不比逐题(tmp_path, capsys):
    """拿客服卷跟上一次的销售卷比，题号根本对不上，比出来全是噪声 —— 直接不比。"""
    out = tmp_path / "cross"
    out.mkdir()
    prev = {"at": "2026-11-16T00:00:00", "meta": {"cases": str(runner.EVALS_DIR / "cases.yaml")},
            "summary": {"rates": [100.0], "rate": 100.0},
            "rows": [{"id": "E01", "ok": True}]}
    (out / "latest.json").write_text(json.dumps(prev, ensure_ascii=False), encoding="utf-8")

    _, cases = load_cases(str(SERVICE_SUITE))
    rc = run_mod.main(["--cases", str(SERVICE_SUITE), "--canned",
                       _canned_file(tmp_path, answers_for(cases)),
                       "--out-dir", str(out)])
    assert rc == 0
    assert "另一份卷子" in capsys.readouterr().err
