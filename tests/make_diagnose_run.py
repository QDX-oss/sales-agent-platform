# -*- coding: utf-8 -*-
"""W2-03 演示：两轮失败归成了不同的类，注入的指令是两段不同的话。

跑法（项目根目录）：
    python tests/make_diagnose_run.py

剧本照旧是假的（ScriptedChatModel），图、关卡、回查、轨迹全部真实。
演示要回答的问题只有一个：
**第二轮的提示词，跟第一轮到底哪里不一样？**
把两段注入原文并排打出来，读者自己判断。
"""

import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai_analyst                                              # noqa: E402
from ai_analyst import query_stats                             # noqa: E402
from core.team import Team                                     # noqa: E402
from core.trace import Trace, load_run                         # noqa: E402
from langchain_core.messages import AIMessage                  # noqa: E402
from tests.fake_llm import ScriptedChatModel                   # noqa: E402

TOOLS = [query_stats, ai_analyst.rank, ai_analyst.trend]
GD = json.loads(query_stats.invoke({"province": "广东"}))["统计"]
QUESTION = "广东省的销售额和利润率是多少？"
RIGHT = "广东省销售额 %s 元，利润率 %s%%。" % (GD["销售额"], GD["利润率"])
WRONG_NUM = "广东省销售额 999 万元，利润率 45%，建议尽快降价冲量。"
WRONG_TOPIC = "广东省销量 %s 件，整体表现不错。" % GD["销量"]


def ai(content, tokens=0, **kw):
    kw.setdefault("usage_metadata", {
        "input_tokens": tokens, "output_tokens": tokens // 6, "total_tokens": tokens})
    return AIMessage(content=content, **kw)


def call_tool():
    return ai("", tokens=480, tool_calls=[{
        "name": "query_stats", "args": {"province": "广东"}, "id": "c1", "type": "tool_call"}])


def critic(issues=None, fix=""):
    return ai(json.dumps({"pass": not issues, "issues": issues or [], "fix": fix},
                         ensure_ascii=False), tokens=300)


def issue(kind, detail):
    return {"type": kind, "detail": detail, "severity": "high"}


def rule(title):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def main():
    rule("场景一：取数错 → 表达错 → 通过（两次诊断不同类）")

    script = [
        call_tool(), ai(WRONG_NUM, 700),
        critic([issue("无据数字", "999 万不在本次工具返回里")], "改用工具返回的销售额"),
        call_tool(), ai(WRONG_TOPIC, 700),
        critic([issue("答非所问", "问的是销售额和利润率，答的是销量")], "直接回答销售额与利润率"),
        ai(RIGHT, 700), critic(), ai(RIGHT, 400),
    ]
    trace = Trace(question=QUESTION, source="demo-diagnose")
    out = Team(ScriptedChatModel(script=script), TOOLS, trace=trace).run(QUESTION)
    trace.finish()

    print("\n问： %s" % QUESTION)
    print("答： %s" % out["answer"])
    print("重做 %d 次 · 最终通过=%s · run_id=%s"
          % (out["retries"], out["passed"], trace.run_id))

    print("\n诊断序列：")
    for i, d in enumerate(out["diagnoses"], 1):
        print("  第 %d 轮  %-6s  策略=%-15s" % (i, d["label"], d["strategy"]))
        print("           依据：%s" % d["why"])
        for s in d["signals"][:3]:
            print("             · %s" % s)

    data = load_run(trace.run_id)
    blocks = [e["detail"]["injected"] for e in data["events"]
              if e.get("event") == "draft_prompt" and e["detail"].get("injected")]
    if len(blocks) >= 2:
        print("\n两轮注入给 Analyst 的原文差异（- 第一轮 / + 第二轮）：")
        diff = list(difflib.unified_diff(
            blocks[0].splitlines(), blocks[1].splitlines(),
            fromfile="第 1 次重做", tofile="第 2 次重做", lineterm="", n=0))
        for line in diff:
            print("  " + line)
        same = len(set(blocks[0].splitlines()) & set(blocks[1].splitlines()))
        print("\n  两段重合 %d 行 / 各 %d、%d 行" % (same, len(blocks[0].splitlines()),
                                                len(blocks[1].splitlines())))

    rule("场景二：一直改不好 → 2 次用完，转人工并交代试过什么")

    script2 = []
    for _ in range(3):
        script2 += [call_tool(), ai(WRONG_NUM, 700),
                    critic([issue("无据数字", "999 万不在本次工具返回里")], "改成有出处的数")]
    trace2 = Trace(question="广东省的销售额是多少？", source="demo-handoff")
    out2 = Team(ScriptedChatModel(script=script2), TOOLS, trace=trace2).run(
        "广东省的销售额是多少？")
    trace2.finish()

    print("\n重做 %d 次后停下 · run_id=%s" % (out2["retries"], trace2.run_id))
    print("交接信息：%s" % json.dumps(
        {k: v for k, v in (out2["handoff"] or {}).items() if k != "last"},
        ensure_ascii=False))
    print("\n交给用户的原文：")
    for line in out2["answer"].splitlines():
        print("  " + line)

    print("\n轨迹： %s" % (ROOT / "runs" / (trace.run_id + ".jsonl")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
