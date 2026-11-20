# -*- coding: utf-8 -*-
"""
W2-03 验收：诊断式 Self-Loop。

跑法（项目根目录）：
    python tests/test_diagnose.py

台账的验收原话是：
**「两次失败的原因分类不同，第二轮 prompt 与第一轮有实质差异（diff 可证）」**

所以这一组测试的重点不是"重试了几次"，而是三件事：
  1. 四类失败能不能各自判对；
  2. 判成不同类之后，注入给 Analyst 的那段话是不是真的不一样；
  3. 预算用尽时，交出去的是"我试过哪些方向"而不是一句"失败了"。
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai_analyst                                              # noqa: E402
from ai_analyst import query_stats                             # noqa: E402
from core import diagnose                                      # noqa: E402
from core.team import Team, load_team_policy                   # noqa: E402
from core.trace import Trace, load_run                         # noqa: E402
from langchain_core.messages import AIMessage                  # noqa: E402
from tests.fake_llm import ScriptedChatModel                   # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  → " + detail) if detail else ""))


TOOLS = [query_stats, ai_analyst.rank, ai_analyst.trend]
GD = json.loads(query_stats.invoke({"province": "广东"}))["统计"]
RIGHT = "广东省销售额 %s 元，利润率 %s%%。" % (GD["销售额"], GD["利润率"])
# 数字编的（999 万不在证据里），这类必然被规则回查拦住
WRONG_NUM = "广东省销售额 999 万元，利润率 45%，建议尽快降价冲量。"
# 数字全对，但答的是"销量"——问题问的是销售额。规则回查放行，只有 Critic 看得出来
WRONG_TOPIC = "广东省销量 %s 件，库存周转良好。" % GD["销量"]


def ai(content, tokens=0, **kw):
    kw.setdefault("usage_metadata", {
        "input_tokens": tokens, "output_tokens": tokens // 6, "total_tokens": tokens})
    return AIMessage(content=content, **kw)


def call_tool(province="广东", tokens=500):
    return ai("", tokens=tokens, tool_calls=[{
        "name": "query_stats", "args": {"province": province},
        "id": "c1", "type": "tool_call"}])


def critic(issues=None, fix="", tokens=300):
    issues = issues or []
    return ai(json.dumps({"pass": not issues, "issues": issues, "fix": fix},
                         ensure_ascii=False), tokens=tokens)


def issue(kind, detail, severity="high"):
    return {"type": kind, "detail": detail, "severity": severity}


# ---------------------------------------------------------------- 1 四类判定

def test_classify():
    print("\n[1] 四类失败各判一次：无数据 / 取数错 / 口径错 / 表达错")

    d = diagnose.classify("查不到相关记录，数据不足。", {"passed": True, "unsupported": []},
                          {"pass": False, "issues": [issue("答非所问", "没回答")]},
                          [], [], attempt=1)
    check("拿不到任何证据 → 无数据", d.kind == diagnose.KIND_NO_DATA, d.kind)
    check("无数据的策略是「说明查询范围」", d.strategy == "state_scope", d.strategy)
    check("无数据那轮明令禁止编数", "不许估算" in d.forbid, d.forbid[:30])

    d = diagnose.classify(WRONG_NUM, {"passed": False, "unsupported": [{"raw": "999 万", "value": 9990000}]},
                          {"pass": True, "issues": []},
                          [{"name": "query_stats", "output": "{}"}], [GD["销售额"]], attempt=1)
    check("取过数但结论里的数对不上 → 取数错", d.kind == diagnose.KIND_FETCH, d.kind)
    check("取数错的策略是「重取」", d.strategy == "refetch", d.strategy)
    check("要求换条件重取，不许复用上一轮数字", "重新调用查询工具" in d.instruction, "")

    d = diagnose.classify("利润率 45%", {"passed": True, "unsupported": []},
                          {"pass": False, "issues": [issue("口径混用", "销售额与利润率互换")]},
                          [{"name": "query_stats", "output": "{}"}], [1.0, 2.0], attempt=1)
    check("质检指出口径混用 → 口径错", d.kind == diagnose.KIND_METRIC, d.kind)
    check("口径错的策略是「退回核口径」", d.strategy == "recheck_caliber", d.strategy)
    check("口径错要给定义，而不是只说「再算一遍」", "利润率 = 利润 ÷ 销售额" in d.instruction, "")

    d = diagnose.classify(WRONG_TOPIC, {"passed": True, "unsupported": []},
                          {"pass": False, "issues": [issue("答非所问", "问销售额却答销量")]},
                          [{"name": "query_stats", "output": "{}"}], [GD["销量"]], attempt=1)
    check("数字全对、只是答错题 → 表达错", d.kind == diagnose.KIND_EXPRESSION, d.kind)
    check("表达错的策略是「只改表达」", d.strategy == "rewrite_only", d.strategy)
    check("表达错那轮一个数字都不许动", "一个数字都不许改" in d.forbid, d.forbid[:30])


# ---------------------------------------------------------------- 2 百分数陷阱

def test_unit_shift():
    print("\n[2] 100 倍差：百分数写成小数、元写成万元")

    evidence = [GD["销售额"], GD["利润"], float(GD["利润率"])]
    d = diagnose.classify("利润率是 0.2088。", {"passed": False,
                                            "unsupported": [{"raw": "0.2088", "value": 0.2088}]},
                          {"pass": True, "issues": []},
                          [{"name": "query_stats", "output": "{}"}], evidence, attempt=1)
    check("0.2088 与证据 20.88 相差 100 倍 → 判成口径错", d.kind == diagnose.KIND_METRIC, d.kind)
    check("而且能说出是哪个证据值", any("20.88" in s for s in d.signals), str(d.signals))

    d = diagnose.classify("毛利率 30%。", {"passed": False,
                                       "unsupported": [{"raw": "30%", "value": 30}]},
                          {"pass": True, "issues": []},
                          [{"name": "query_stats", "output": "{}"}], evidence, attempt=1)
    check("30 与证据毫无 100 倍关系 → 不判口径错（不硬套）",
          d.kind != diagnose.KIND_METRIC, d.kind)


# ---------------------------------------------------------------- 3 两轮实质差异

def test_two_rounds_differ():
    print("\n[3] 台账验收原文：两轮失败分类不同，第二轮 prompt 与第一轮有实质差异")

    script = [
        call_tool(), ai(WRONG_NUM, 700),                      # 第 1 稿：数字编的
        critic([issue("无据数字", "999 万不在工具返回里")], "改用工具返回的销售额"),
        call_tool(), ai(WRONG_TOPIC, 700),                    # 第 2 稿：数字对，答错题
        critic([issue("答非所问", "问的是销售额，答的是销量")], "直接回答销售额"),
        ai(RIGHT, 700),                                       # 第 3 稿：对了
        critic(),
        ai(RIGHT, 400),
    ]
    model = ScriptedChatModel(script=script)
    trace = Trace(question="广东省的销售额和利润率是多少？", source="test",
                  runs_dir=tempfile.mkdtemp(prefix="diag-"))
    out = Team(model, TOOLS, trace=trace).run("广东省的销售额和利润率是多少？")
    trace.finish()

    check("发生了 2 次重做", out["retries"] == 2, "retries=%s" % out["retries"])
    kinds = [d["kind"] for d in out["diagnoses"]]
    check("两轮判成了不同的类（取数错 → 表达错）",
          kinds == ["fetch", "expression"], str(kinds))
    check("两轮用的策略也不同（refetch → rewrite_only）",
          out["retry_strategies"] == ["refetch", "rewrite_only"],
          str(out["retry_strategies"]))
    check("最终交付的是第三稿（改对的）", out["answer"] == RIGHT, out["answer"][:40])
    check("没有转人工（最后还是过了）", out["handoff"] is None, str(out["handoff"]))

    # 从模型收到的 messages 里把两段注入取出来，做真正的 diff
    blocks = {}
    for msgs in model.seen:
        text = "\n".join(getattr(m, "content", "") or "" for m in msgs)
        for n in (1, 2):
            tag = "【第 %d 次重做" % n
            if tag in text:
                i = text.index(tag)
                blocks[n] = text[i:i + 400]

    check("第 1 轮的注入块出现在 prompt 里", 1 in blocks)
    check("第 2 轮的注入块出现在 prompt 里", 2 in blocks)
    if 1 in blocks and 2 in blocks:
        check("第 1 轮写明诊断是「取数错」", "取数错" in blocks[1], blocks[1][:50])
        check("第 2 轮写明诊断是「表达错」", "表达错" in blocks[2], blocks[2][:50])
        check("第 1 轮要求重取", "重新调用查询工具" in blocks[1])
        check("第 2 轮要求一个数字都不许改", "一个数字都不许改" in blocks[2])
        # 实质差异：不是只换了序号。逐句比对，至少一半句子不同
        l1 = set(x for x in blocks[1].splitlines() if x.strip())
        l2 = set(x for x in blocks[2].splitlines() if x.strip())
        overlap = len(l1 & l2)
        check("两段指令的句子重合不超过 1 句（不只是换了序号）",
              overlap <= 1, "重合 %d 句 / 共 %d、%d 句" % (overlap, len(l1), len(l2)))
        check("两段文字长度都够，不是空壳",
              min(len(blocks[1]), len(blocks[2])) > 60,
              "%d / %d 字" % (len(blocks[1]), len(blocks[2])))


# ---------------------------------------------------------------- 4 预算用尽

def test_handoff():
    print("\n[4] 重试用尽：停下、转人工，并交代试过哪些方向")

    # 永远不过：每一稿都编数字，质检每次都打回
    script = []
    for _ in range(3):
        script.append(call_tool())
        script.append(ai(WRONG_NUM, 700))
        script.append(critic([issue("无据数字", "999 万不在工具返回里")], "改成有出处的数"))
    model = ScriptedChatModel(script=script)
    tmp = tempfile.mkdtemp(prefix="diag-hand-")
    trace = Trace(question="q", source="test", runs_dir=tmp)
    team = Team(model, TOOLS, trace=trace)          # 默认 2 次，来自 config/team.json
    out = team.run("广东省的销售额是多少？")
    trace.finish()

    check("默认重试上限来自配置，是 2", team.max_retry == 2, str(team.max_retry))
    check("确实只重做了 2 次就停", out["retries"] == 2, "retries=%s" % out["retries"])
    check("三稿各判一次（初稿 + 两次重做）", len(out["diagnoses"]) == 3,
          str(len(out["diagnoses"])))
    check("handoff 被标记", bool(out["handoff"] and out["handoff"]["needed"]))
    check("交出答案时先说「需人工复核」", out["answer"].startswith("【需人工复核】"),
          out["answer"][:24])
    check("交代了试过的方向", "试过的方向" in out["answer"], "")
    check("交接信息里有每次判定的类", len(out["handoff"]["kinds"]) == 3,
          str(out["handoff"])
          )
    check("答案里仍然带着最后一稿，不是空白", WRONG_NUM[:8] in out["answer"], "")

    # 再确认一次真的停了：剧本里的消息没被继续消耗
    check("停下之后没有再发一次模型调用", model.calls == 9, "calls=%d" % model.calls)


# ---------------------------------------------------------------- 5 轨迹留痕

def test_trace():
    print("\n[5] 轨迹里能读出：每轮判成什么、注入了多少字")

    script = [
        call_tool(), ai(WRONG_NUM, 700),
        critic([issue("无据数字", "999 万不在工具返回里")], "改成有出处的数"),
        ai(RIGHT, 700), critic(), ai(RIGHT, 400),
    ]
    tmp = tempfile.mkdtemp(prefix="diag-trace-")
    trace = Trace(question="q", source="test", runs_dir=tmp)
    out = Team(ScriptedChatModel(script=script), TOOLS, trace=trace).run("广东省销售额是多少")
    trace.finish()

    data = load_run(trace.run_id, runs_dir=tmp)
    events = data["events"]
    diag = [e["detail"] for e in events if e.get("event") == "diagnosis"]
    prompts = [e["detail"] for e in events if e.get("event") == "draft_prompt"]

    check("轨迹里有一条 diagnosis", len(diag) == 1, str(len(diag)))
    check("这条 diagnosis 带了类型与策略",
          bool(diag) and diag[0].get("kind") == "fetch" and diag[0].get("strategy") == "refetch",
          str(diag[:1]))
    check("diagnosis 记录了判定依据", bool(diag and diag[0].get("why")), "")
    check("首稿的注入长度为 0（第一轮没有策略可注入）",
          bool(prompts) and (prompts[0].get("injected_chars") or 0) == 0, str(prompts[:1]))
    check("第二稿的注入长度 > 0（prompt 真的变了）",
          len(prompts) > 1 and (prompts[1].get("injected_chars") or 0) > 0,
          str(prompts[1:2])[:120])
    analyst = [s.get("retry") for s in data["steps"]
               if s.get("agent") == "Analyst" and s.get("name") == "chat_model"]
    check("Analyst 两次作业在轨迹里分别记 retry=0 与 retry=1",
          bool(analyst) and analyst[0] == 0 and 1 in analyst, str(analyst))


# ---------------------------------------------------------------- 6 配置

def test_config():
    print("\n[6] 重试预算是配置，不是硬编码")

    cfg = load_team_policy()
    check("config/team.json 读得到 max_retry", cfg.get("max_retry") == 2, str(cfg))

    tmp = Path(tempfile.mkdtemp(prefix="diag-cfg-")) / "team.json"
    tmp.write_text(json.dumps({"max_retry": 0}), encoding="utf-8")
    check("改成 0 就真的一次都不重做", load_team_policy(tmp)["max_retry"] == 0)

    tmp.write_text(json.dumps({"max_retry": "abc"}), encoding="utf-8")
    check("配了非法值退回默认 2，不崩", load_team_policy(tmp)["max_retry"] == 2)

    tmp.write_text("{}", encoding="utf-8")
    check("不写就按默认 2", load_team_policy(tmp)["max_retry"] == 2)

    model = ScriptedChatModel(script=[call_tool(), ai(WRONG_NUM), critic([issue("无据数字", "x")]),
                                      ai(WRONG_NUM), critic([issue("无据数字", "x")])])
    out = Team(model, TOOLS, max_retry=0).run("广东省销售额是多少")
    check("显式传 0 覆盖配置：一次都不重做", out["retries"] == 0, "retries=%s" % out["retries"])
    check("不重做也照样转人工", bool(out["handoff"] and out["handoff"]["needed"]))


def main():
    print("=" * 62)
    print("W2-03 验收：诊断式 Self-Loop")
    print("=" * 62)
    test_classify()
    test_unit_shift()
    test_two_rounds_differ()
    test_handoff()
    test_trace()
    test_config()

    total = len(results)
    passed = sum(1 for _, ok in results if ok)
    print("\n" + "=" * 62)
    print("结果：%d / %d 通过" % (passed, total))
    print("=" * 62)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
