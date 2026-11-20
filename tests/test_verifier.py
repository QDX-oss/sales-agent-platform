# -*- coding: utf-8 -*-
"""
W0-05 验收：数字回查。

跑法（项目根目录）：
    python tests/test_verifier.py

不联网：用假模型 + 真实 CSV 工具。重点验证"错的能不能被拦住"，
以及"对的不会被误伤"——后者同样重要，一个天天误报的校验器最后会被关掉。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai_analyst                                            # noqa: E402
from ai_analyst import ask_verified, query_stats              # noqa: E402
from core import verifier                                     # noqa: E402
from core.trace import Trace, load_run                        # noqa: E402
from langchain.agents import create_agent                     # noqa: E402
from langchain_core.messages import AIMessage                 # noqa: E402
from tests.fake_llm import ScriptedChatModel                  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  → " + detail) if detail else ""))


GD = json.loads(query_stats.invoke({"province": "广东"}))["统计"]
EV = [GD["记录数"], GD["销量"], GD["销售额"], GD["利润"], GD["利润率"]]


def unit_cases():
    print("\n[1] 单元：能不能认出对的、拦住错的")

    ok = "广东省销售额合计 %s 元，利润 %s 元，利润率 %s%%，共 %s 单、%s 件。" % (
        GD["销售额"], GD["利润"], GD["利润率"], GD["记录数"], GD["销量"])
    v = verifier.verify(ok, EV)
    check("全对 → 通过", v.passed and v.checked >= 5, "checked=%d" % v.checked)

    v = verifier.verify("广东省销售额约 123.5 万元。", EV)
    check("万元换算（123.5 万）→ 通过", v.passed, str(v.unsupported))

    v = verifier.verify("全国销售额 16,848,875 元。", [16848875])
    check("千分位（16,848,875）→ 通过", v.passed, str(v.unsupported))

    v = verifier.verify("广东省销售额 999 万元。", EV)
    check("编造 999 万 → 拦下", not v.passed, "unsupported=%s" % [c["raw"] for c in v.unsupported])
    if v.unsupported:
        check("拦下时给出最近的真实值", v.unsupported[0].get("nearest") == GD["销售额"],
              str(v.unsupported[0].get("nearest")))

    v = verifier.verify("共 42 单品，销售额 1235074 元。", EV)
    check("小数字说错（42 单，真值 17）→ 拦下", not v.passed,
          "unsupported=%s" % [c["raw"] for c in v.unsupported])

    v = verifier.verify("广东省销售额 123 万元。", EV)
    check("把 123.5 万写成 123 万 → 拦下（丢了 5000 的信息）", not v.passed,
          "unsupported=%s" % [c["raw"] for c in v.unsupported])

    v = verifier.verify("利润率 20.88%。", [GD["利润"], GD["销售额"]])
    check("派生值（利润率由利润/销售额算出）→ 通过",
          v.passed and v.supported and "derived" in v.supported[0],
          str(v.supported[0].get("derived")) if v.supported else "")

    v = verifier.verify("2025 年 11 月，第 3 季度，销售额前 10 名。\n1. 先看总量。", EV)
    check("时间/序号/列表号 → 全部忽略，不算错",
          v.passed and v.checked == 0 and len(v.ignored) >= 4,
          "checked=%d ignored=%d" % (v.checked, len(v.ignored)))

    v = verifier.verify("销售额 1235074 元。", [])
    check("工具没返回任何数据时说数字 → 拦下", not v.passed, "evidence=0")

    v = verifier.verify("", EV)
    check("空回答 → 通过（无数字可观）", v.passed and v.checked == 0)

    v = verifier.verify("广东省的数据如下。", EV)
    check("纯文字无数字 → 通过", v.passed and v.checked == 0)

    print("\n[2] 纠错意见是否可操作")
    v = verifier.verify("广东省销售额 999 万元。", EV)
    fb = v.feedback()
    check("反馈里点名了那个数字", "999" in fb, fb[:60] + "…")
    check("反馈里给了最近的真实值", "1,235,074" in fb, "")


def build_agent(script):
    return create_agent(ScriptedChatModel(script=script),
                        tools=[query_stats, ai_analyst.rank, ai_analyst.trend],
                        system_prompt=ai_analyst.SYSTEM_PROMPT)


def e2e_cases():
    print("\n[3] 端到端：说错 → 被打回 → 改对")
    right = "广东省销售额 %s 元，利润 %s 元。" % (GD["销售额"], GD["利润"])
    wrong = "广东省销售额 999 万元，利润 88 万元。"
    script = [
        AIMessage(content="", tool_calls=[{"name": "query_stats", "args": {"province": "广东"},
                                           "id": "c1", "type": "tool_call"}]),
        AIMessage(content=wrong),
        AIMessage(content=right),
    ]
    ai_analyst.get_analyst = lambda trace=None: build_agent(script)
    trace = Trace(question="广东省销售额是多少？", source="test")
    out = ask_verified("广东省销售额是多少？", trace=trace)
    trace.finish()

    check("第一次说错被拦下，重答后通过", out["verified"] and out["retries"] == 1,
          "verified=%s retries=%d" % (out["verified"], out["retries"]))
    check("最终答案是改对的那一版", out["answer"] == right, out["answer"])

    data = load_run(trace.run_id)
    events = [e for e in data["events"] if e["event"] == "verify"]
    steps = data["steps"]
    print("     轨迹：%d 步 / %d 条 verify 事件" % (len(steps), len(events)))
    for s in steps:
        print("       [step] retry=%d %-11s %-6s %6.1fms" % (
            s["retry"], s["name"], s["status"], s["duration_ms"]))
    for e in events:
        print("       [event] attempt=%s verdict=%s unsupported=%s" % (
            e["detail"].get("attempt"), e["detail"].get("verdict"),
            e["detail"].get("unsupported")))

    check("轨迹里有 2 条 verify 事件（先 fail 后 pass）", len(events) == 2,
          str([e["detail"]["verdict"] for e in events]))
    check("第一次 verify 记录了被拦下的数字",
          "999 万" in str(events[0]["detail"].get("unsupported")),
          str(events[0]["detail"].get("unsupported")))
    check("重试的步骤标了 retry=1", any(s["retry"] == 1 for s in steps),
          str(sorted({s["retry"] for s in steps})))

    print("\n[4] 一直改不对：重试到上限就停，仍然给出答案")
    script2 = [
        AIMessage(content="", tool_calls=[{"name": "query_stats", "args": {"province": "广东"},
                                           "id": "c1", "type": "tool_call"}]),
        AIMessage(content=wrong),
    ]
    ai_analyst.get_analyst = lambda trace=None: build_agent(script2)
    t2 = Trace(question="广东省销售额是多少？", source="test")
    out2 = ask_verified("广东省销售额是多少？", trace=t2)
    t2.finish()
    check("重试次数到达上限（2 次）", out2["retries"] == 2, "retries=%d" % out2["retries"])
    check("仍然返回答案，不会因为校验失败而什么都不给", bool(out2["answer"]))
    check("verdict 标明未通过", out2["verified"] is False)
    check("未通过时列出无法溯源的数字",
          len(out2["verdict"].unsupported) >= 1,
          str([c["raw"] for c in out2["verdict"].unsupported]))

    print("\n[5] 一次就对：不该多花一次调用")
    script3 = [
        AIMessage(content="", tool_calls=[{"name": "query_stats", "args": {"province": "广东"},
                                           "id": "c1", "type": "tool_call"}]),
        AIMessage(content=right),
    ]
    ai_analyst.get_analyst = lambda trace=None: build_agent(script3)
    t3 = Trace(question="广东省销售额是多少？", source="test")
    out3 = ask_verified("广东省销售额是多少？", trace=t3)
    t3.finish()
    check("不重试", out3["retries"] == 0 and out3["verified"], "retries=%d" % out3["retries"])
    d3 = load_run(t3.run_id)
    check("轨迹里只有 3 步（模型/工具/模型）", len(d3["steps"]) == 3, str(len(d3["steps"])))


def main():
    print("=" * 74)
    print("W0-05 验收：数字回查")
    print("=" * 74)
    unit_cases()
    e2e_cases()

    bad = [n for n, ok in results if not ok]
    print("\n" + "-" * 74)
    print("合计 %d 项，通过 %d 项，失败 %d 项" % (len(results), len(results) - len(bad), len(bad)))
    if bad:
        print("失败项：" + "、".join(bad))
    print("-" * 74)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
