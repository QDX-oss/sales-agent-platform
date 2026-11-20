# -*- coding: utf-8 -*-
"""生成一份"打回重做"的演示轨迹，供变更单和答辩引用。

跑法（项目根目录）：
    python tests/make_demo_team_run.py

用的是假模型（本机没有 ai_key.txt），但工具、CSV、回调、trace、verifier、
briefs 存档全是真跑的。剧本刻意让第一稿说错话，好让轨迹里留下：
    Analyst 失败 → Critic 判 fail → Analyst 重做 → Critic 判 pass → Curator 成文
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai_analyst                                              # noqa: E402
from ai_analyst import query_stats                             # noqa: E402
from core.team import run_team                                 # noqa: E402
from core.trace import Trace, load_run                         # noqa: E402
from langchain_core.messages import AIMessage                  # noqa: E402
from tests.fake_llm import ScriptedChatModel                   # noqa: E402


def ai(content, tin=0, tout=0, **kw):
    kw.setdefault("usage_metadata", {
        "input_tokens": tin, "output_tokens": tout, "total_tokens": tin + tout})
    return AIMessage(content=content, **kw)


GD = json.loads(query_stats.invoke({"province": "广东"}))["统计"]
QUESTION = "广东省这个月的销售额和利润率是多少？有没有降价空间？"

DRAFT_BAD = "广东省销售额 999 万元，利润率 45%，建议立刻降价 10% 冲量。"
DRAFT_OK = "广东省销售额 %s 元，利润 %s 元，利润率 %s%%。" % (
    GD["销售额"], GD["利润"], GD["利润率"])
CRITIC_FAIL = json.dumps({
    "pass": False,
    "issues": [
        {"type": "无据数字", "detail": "999 万与 45%% 在工具返回中均无出处，实际为 %s 与 %s%%"
                                      % (GD["销售额"], GD["利润率"]), "severity": "high"},
        {"type": "越界表述", "detail": "「建议立刻降价 10%」属商务承诺，超出分析职责",
         "severity": "high"},
    ],
    "fix": "改用工具返回的真实数字，删掉降价建议，如需建议请说明缺少竞品价格数据",
}, ensure_ascii=False)
CRITIC_PASS = json.dumps({"pass": True, "issues": [], "fix": ""}, ensure_ascii=False)
CURATED = "%s 元销售额，利润率 %s%%。降价空间暂无法判断：缺竞品价格数据。" % (
    GD["销售额"], GD["利润率"])

SCRIPT = [
    ai("", 431, 28, tool_calls=[{"name": "query_stats", "args": {"province": "广东"},
                                 "id": "c1", "type": "tool_call"}]),
    ai(DRAFT_BAD, 512, 61),
    ai(CRITIC_FAIL, 903, 128),
    ai(DRAFT_OK, 688, 47),
    ai(CRITIC_PASS, 861, 24),
    ai(CURATED, 742, 52),
]


def main():
    trace = Trace(question=QUESTION, source="demo-team")
    out = run_team(QUESTION, ScriptedChatModel(script=SCRIPT),
                   [query_stats, ai_analyst.rank, ai_analyst.trend], trace=trace)
    summary = trace.finish()

    print("最终答复：", out["answer"])
    print("通过=%s  重试=%d  成文=%s" % (out["passed"], out["retries"], out["curated"]))
    print("轨迹文件：", summary["path"])
    print()

    data = load_run(trace.run_id)
    print("%-4s %-8s %-11s %-6s %-9s %s" % ("seq", "agent", "name", "status", "耗时", "retry"))
    print("-" * 62)
    for s in data["steps"]:
        print("%-4s %-8s %-11s %-6s %7.1fms %d" % (
            s["seq"], s["agent"], s["name"], s["status"], s["duration_ms"], s["retry"]))
    print("-" * 62)
    for e in data["events"]:
        d = e["detail"]
        note = ""
        if e["event"] == "verify":
            note = "第%d稿 %s，核对%d个数字，无出处=%s" % (
                d["attempt"] + 1, d["verdict"], d["checked"], d["unsupported"])
        elif e["event"] == "critic":
            note = "第%d稿 %s，high=%d %s" % (
                d["attempt"] + 1, d["verdict"], d["high"],
                [i["type"] for i in d["issues"]])
        else:
            note = str(d)
        print("%-4s [event] %-8s %s" % (e["seq"], e["event"], note))
    end = data["end"]
    print("-" * 62)
    print("run_end status=%s steps=%s failed=%s retry_count=%s tokens=%s" % (
        end["status"], end["step_count"], end["failed_steps"],
        end.get("retry_count"), end.get("tokens")))
    print()
    print("轨迹：", summary["path"])
    print("存档：", out.get("brief_path"))


if __name__ == "__main__":
    main()
