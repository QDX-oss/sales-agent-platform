# -*- coding: utf-8 -*-
"""失败样本回流（台账 W4-03）的测试。

验收口径：制造一次失败后，下次运行该题出现在用例集中，且用例数 +1。

这里死守两条：
- 回流**不许猜预期答案**（写 pending），否则回归等于自己给自己出标准答案；
- 同一问法**不许重复收**，否则跑几次回归就把卷子撑满。
"""
import json
from pathlib import Path

import pytest
import yaml

from core.evalset import check_case, load_cases
from evals import reflow, runner
from evals import run as run_mod


BASE = """# 顶部注释：这是给人看的，回流不许把它弄丢
version: 1
cases:
  - id: A1
    question: 甲省销售额是多少？
    difficulty: normal
    expect:
      type: exact
      value: 100
    came_from: 手工算的

  - id: A2
    question: 乙省销售额是多少？
    difficulty: trap
    expect:
      type: must_refuse
    came_from: 该省在数据里不存在
"""


@pytest.fixture
def suite_file(tmp_path):
    p = tmp_path / "cases.yaml"
    p.write_text(BASE, encoding="utf-8")
    return p


def _entry(q, cases, reason="", source="", run_id=None):
    return reflow.make_entry(q, cases, reason=reason, source=source, run_id=run_id)


# ---------------------------------------------------------------- 追加

def test_追加一题用例数加一且进regression组(suite_file):
    _, cases = load_cases(suite_file)
    before = len(cases)
    res = reflow.append_entries([_entry("丙省销售额是多少？", cases)], suite_file)
    assert len(res["added"]) == 1

    _, cases2 = load_cases(suite_file)
    assert len(cases2) == before + 1
    new = cases2[-1]
    assert new["group"] == "regression"
    assert new["question"] == "丙省销售额是多少？"
    # 关键：不猜答案，标成待定口径
    assert new["expect"]["type"] == "pending"
    assert "run" in new["came_from"] or "回流" in new["trap"]


def test_追加不破坏原有注释与主卷(suite_file):
    _, cases = load_cases(suite_file)
    reflow.append_entries([_entry("丙省销售额是多少？", cases)], suite_file)
    raw = suite_file.read_text(encoding="utf-8")
    assert "# 顶部注释：这是给人看的" in raw
    assert raw.count("regression:") == 1
    doc = yaml.safe_load(raw)
    assert len(doc["cases"]) == 2                  # 主卷没被动
    assert len(doc["regression"]) == 1


def test_同一问法不重复收(suite_file):
    _, cases = load_cases(suite_file)
    first = reflow.append_entries([_entry("丙省销售额是多少？", cases)], suite_file)
    _, cases2 = load_cases(suite_file)
    again = reflow.append_entries([_entry("丙省销售额是多少？", cases2)], suite_file)

    assert len(first["added"]) == 1
    assert again["added"] == []
    assert again["skipped"] and "已有" in again["skipped"][0]["why"]
    _, cases3 = load_cases(suite_file)
    assert len(cases3) == len(cases2)             # 一条都没多


def test_卷里已有的问法也不重复收(suite_file):
    _, cases = load_cases(suite_file)
    res = reflow.append_entries([_entry("甲省销售额是多少？", cases)], suite_file)
    assert res["added"] == []
    assert "A1" in res["skipped"][0]["why"]


def test_第二次追加插进同一个regression段(suite_file):
    _, cases = load_cases(suite_file)
    reflow.append_entries([_entry("丙省销售额是多少？", cases)], suite_file)
    _, cases2 = load_cases(suite_file)
    reflow.append_entries([_entry("丁省销售额是多少？", cases2)], suite_file)

    raw = suite_file.read_text(encoding="utf-8")
    assert raw.count("regression:") == 1          # 不能出现两个同名段
    _, cases3 = load_cases(suite_file)
    assert [c["id"] for c in cases3[-2:]] == ["R01", "R02"]
    assert cases3[-1]["question"] == "丁省销售额是多少？"


def test_编号从R01起且不复用空号(suite_file):
    _, cases = load_cases(suite_file)
    ents = [_entry("丙%d多少？" % i, cases) for i in range(3)]
    res = reflow.append_entries(ents, suite_file)
    assert [e["id"] for e in res["added"]] == ["R01", "R02", "R03"]


def test_干跑不写文件(suite_file):
    before = suite_file.read_text(encoding="utf-8")
    _, cases = load_cases(suite_file)
    res = reflow.append_entries([_entry("丙省销售额是多少？", cases)],
                                suite_file, dry_run=True)
    assert len(res["added"]) == 1
    assert suite_file.read_text(encoding="utf-8") == before


def test_空问题跳过(suite_file):
    _, cases = load_cases(suite_file)
    res = reflow.append_entries([_entry("   ", cases)], suite_file)
    assert res["added"] == [] and res["skipped"]


def test_带冒号井号的问法不会被写坏(suite_file):
    _, cases = load_cases(suite_file)
    q = '问：#1 渠道：退货率多少？ "双引号" 也要能存'
    reflow.append_entries([_entry(q, cases)], suite_file)
    _, cases2 = load_cases(suite_file)
    assert cases2[-1]["question"] == q


# ---------------------------------------------------------------- 待定口径

def test_待定口径跑但不判分(suite_file):
    _, cases = load_cases(suite_file)
    reflow.append_entries([_entry("丙省销售额是多少？", cases)], suite_file)
    _, cases2 = load_cases(suite_file)

    # 这一题没有 canned 答案 → 会记"跑挂了"，但它仍然是待定口径，不进分母
    answers = {"甲省销售额是多少？": "100", "乙省销售额是多少？": "数据里没有"}
    rep = runner.run_suite(cases2, runner.make_canned_ask(answers))

    s = rep["summary"]
    assert s["cases_total"] == 3
    assert s["pending"] == 1
    assert s["total"] == 2                       # 分母只算能判的
    assert s["passed"] == 2 and s["rate"] == 100.0
    assert rep["pending_ids"] == ["R01"]
    assert rep["failed_ids"] == []               # 待定不算失败


def test_待定题有答案也不判过():
    case = {"id": "R99", "question": "x？", "expect": {"type": "pending"}}
    r = check_case(case, "随便一个答案 123456")
    assert r["ok"] is False and r.get("pending") is True
    assert "待定口径" in r["reason"]


def test_待定题不进前后对比(suite_file):
    _, cases = load_cases(suite_file)
    reflow.append_entries([_entry("丙省销售额是多少？", cases)], suite_file)
    _, cases2 = load_cases(suite_file)
    answers = {"甲省销售额是多少？": "100", "乙省销售额是多少？": "数据里没有"}
    rep = runner.run_suite(cases2, runner.make_canned_ask(answers))
    d = runner.diff_reports(rep, rep)
    assert d["regressed"] == [] and d["fixed"] == []


# ---------------------------------------------------------------- 三个来源

def test_从报告回流判挂的题(suite_file):
    _, cases = load_cases(suite_file)
    rep = {"stamp": "20261116-090000-000",
           "rows": [
               {"id": "A1", "question": "甲省销售额是多少？", "ok": True, "run_id": "R-a"},
               {"id": "A2", "question": "乙省销售额是多少？", "ok": False,
                "reason": "没发现拒答信号", "run_id": "R-b"},
               {"id": "R01", "question": "丙？", "ok": False, "pending": True},
           ]}
    ents = reflow.collect_from_report(rep, cases)
    assert len(ents) == 1                       # 待定口径那行不算"挂"
    assert ents[0]["question"] == "乙省销售额是多少？"
    assert "没发现拒答信号" in ents[0]["came_from"]
    assert "R-b" in ents[0]["came_from"]


def test_从run回流带run_id与问题(tmp_path):
    rundir = tmp_path / "runs"
    rundir.mkdir()
    rid = "R-20261116"
    rows = [
        {"type": "run", "run_id": rid, "question": "哪个渠道退货率最高？"},
        {"type": "event", "detail": {"note": "重试预算用尽，停止自动修改，转人工复核"}},
        {"type": "run_end", "run_id": rid, "status": "failed"},
    ]
    (rundir / (rid + ".jsonl")).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    _, cases = load_cases()
    ents = reflow.collect_from_run(rid, cases, rundir)
    assert len(ents) == 1
    assert ents[0]["question"] == "哪个渠道退货率最高？"
    assert rid in ents[0]["came_from"]
    assert "转人工" in ents[0]["came_from"]


def test_从run回流找不到run时报错(tmp_path):
    with pytest.raises(FileNotFoundError):
        reflow.collect_from_run("NOT-EXIST", [], tmp_path)


def test_扫描只收failed(tmp_path):
    rundir = tmp_path / "runs"
    rundir.mkdir()
    for rid, status, q in [("R-ok", "ok", "甲省销售额是多少？"),
                           ("R-bad", "failed", "丙省销售额是多少？"),
                           ("R-bad2", "failed", "丁省销售额是多少？")]:
        rows = [{"type": "run", "run_id": rid, "question": q},
                {"type": "run_end", "run_id": rid, "status": status}]
        (rundir / (rid + ".jsonl")).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")

    _, cases = load_cases()
    ents = reflow.collect_scan(10, cases, rundir)
    qs = {e["question"] for e in ents}
    assert "丙省销售额是多少？" in qs and "丁省销售额是多少？" in qs
    assert "甲省销售额是多少？" not in qs


# ---------------------------------------------------------------- 和回归串起来

def test_线上失败回流后下次必测且用例数加一(suite_file, tmp_path, capsys):
    """验收口径全链：制造一次失败 → 回流 → 下次运行该题在卷里、用例数 +1。

    失败样本走真实路径：线上挂掉的一个 run（转人工），问题不在卷子里 ——
    这才是回流真正能加题的情况。卷里已有的题挂了，回流只会说"已有"，不重复收。
    """
    rundir = tmp_path / "runs"
    rundir.mkdir()
    rid = "R-live-001"
    rows = [{"type": "run", "run_id": rid, "question": "哪个渠道的退货率最高？"},
            {"type": "event", "detail": {"note": "重试预算用尽，停止自动修改，转人工复核"}},
            {"type": "run_end", "run_id": rid, "status": "failed"}]
    (rundir / (rid + ".jsonl")).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    rc = reflow.main(["--from-run", rid, "--cases", str(suite_file),
                      "--runs-dir", str(rundir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "用例数 2 → 3" in out

    _, cases = load_cases(suite_file)
    assert len(cases) == 3
    assert cases[-1]["question"] == "哪个渠道的退货率最高？"
    assert cases[-1]["expect"]["type"] == "pending"

    # 下次回归：这题真的被跑了（出现在报告里），并标成待定口径
    answers = {"甲省销售额是多少？": "100", "乙省销售额是多少？": "数据里没有"}
    rc2 = run_mod.main(["--cases", str(suite_file),
                        "--canned", str(_write(tmp_path, answers)),
                        "--out-dir", str(tmp_path / "rep2"), "--no-compare"])
    assert rc2 == 0
    rep = json.loads((tmp_path / "rep2" / "latest.json").read_text(encoding="utf-8"))
    ids = [r["id"] for r in rep["rows"]]
    assert "R01" in ids
    assert rep["summary"]["pending"] == 1
    assert rep["summary"]["cases_total"] == 3


def test_回归报告里的题都已存在时只提示不重复收(suite_file, tmp_path, capsys):
    """回归只跑卷子里的题：挂了的题本来就在卷里，回流只能是"已有"，
    要加新题得从线上失败的 run 回流 —— 报告里也会这么提示。"""
    answers = {"甲省销售额是多少？": "100",
               "乙省销售额是多少？": "销售额约 120 万"}     # 该拒答却编数 → 挂
    rc = run_mod.main(["--cases", str(suite_file),
                       "--canned", str(_write(tmp_path, answers)),
                       "--out-dir", str(tmp_path / "rep"),
                       "--no-compare", "--reflow"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "回流 0 题" in out and "卷里已有" in out
    _, cases = load_cases(suite_file)
    assert len(cases) == 2                       # 一条都没多
    assert "--from-run" in out                   # 提示正确路径


def _write(tmp_path, answers):
    p = tmp_path / "canned.json"
    p.write_text(json.dumps(answers, ensure_ascii=False), encoding="utf-8")
    return p
