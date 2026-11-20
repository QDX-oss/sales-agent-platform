# -*- coding: utf-8 -*-
"""生成一份演示用的拦截轨迹（不联网：假模型 + 真实工具 + 真实 CSV）。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai_analyst
from ai_analyst import ask_verified, query_stats
from core.trace import Trace
from langchain.agents import create_agent
from langchain_core.messages import AIMessage
from tests.fake_llm import ScriptedChatModel


def ai(content, tokens=None, **kw):
    return AIMessage(content=content,
                     usage_metadata={"input_tokens": tokens[0], "output_tokens": tokens[1],
                                     "total_tokens": tokens[0] + tokens[1]},
                     **kw)


gd = json.loads(query_stats.invoke({"province": "广东"}))["统计"]
right = "广东省销售额 %s 元，利润 %s 元，利润率 %s%%。" % (gd["销售额"], gd["利润"], gd["利润率"])
wrong = "广东省销售额 999 万元，利润率 45%。"

script = [
    ai("", (512, 34), tool_calls=[{"name": "query_stats", "args": {"province": "广东"},
                                   "id": "c1", "type": "tool_call"}]),
    ai(wrong, (742, 61)),
    ai(right, (815, 92)),
]
ai_analyst._agent = create_agent(
    ScriptedChatModel(script=script),
    tools=[query_stats, ai_analyst.rank, ai_analyst.trend],
    system_prompt=ai_analyst.SYSTEM_PROMPT)

trace = Trace(question="广东省的销售额和利润率是多少？", source="demo-nokey")
out = ask_verified("广东省的销售额和利润率是多少？", trace=trace)
summary = trace.finish()

print("答案:", out["answer"])
print("verified=%s retries=%d" % (out["verified"], out["retries"]))
print("文件:", summary["path"])
print()
for line in open(summary["path"], encoding="utf-8"):
    rec = json.loads(line)
    kind = rec["type"]
    if kind == "step":
        print("%d [step]  retry=%d %-11s %-6s %6.1fms tokens=%d/%d" % (
            rec["seq"], rec["retry"], rec["name"], rec["status"],
            rec["duration_ms"], rec["tokens"]["in"], rec["tokens"]["out"]))
    elif kind == "event":
        print("%d [event] %s %s" % (rec["seq"], rec["event"], json.dumps(rec["detail"], ensure_ascii=False)))
    elif kind == "run":
        print("  [run]     %s" % rec["question"])
    else:
        print("  [run_end] status=%s steps=%s retry_count=%s tokens=%s" % (
            rec["status"], rec["step_count"], rec["retry_count"], rec["tokens"]))
