# -*- coding: utf-8 -*-
"""
W0-04 验收：一次真实提问是否留下可回放的轨迹。

跑法（项目根目录）：
    python tests/test_instrument.py

不联网、不需要 API Key：用按剧本回话的假模型替换 DeepSeek，
但 create_agent 的图、真实的 query_stats 工具、真实的 CSV 全部保留。
所以轨迹里能看到工具调用，就说明回调机制确实在工作。

覆盖的用例：
    1. 正常两轮：模型 → 工具 → 模型，三条 step 顺序正确，耗时与 token 可读
    2. 工具报错：异常在轨迹里记为 failed，业务照常拿到回答
    3. 不传 trace：与改动前行为一致，不多写文件
    4. 埋点自身出错：不影响回答
    5. 端到端：走 /api/ai/ask，返回体带 run_id，且该 run_id 能读回完整轨迹
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai_analyst                                              # noqa: E402
from ai_analyst import ask_analyst, query_stats                # noqa: E402
from core.instrument import TraceCallbackHandler, attach       # noqa: E402
from core.trace import STATUS_FAILED, Trace, list_runs, load_run  # noqa: E402
from langchain.agents import create_agent                      # noqa: E402
from langchain_core.messages import AIMessage                  # noqa: E402
from langchain_core.tools import tool as make_tool             # noqa: E402
from tests.fake_llm import ScriptedChatModel, two_turn_script  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print("  [%s] %s%s" % (PASS if ok else FAIL, name, ("  → " + detail) if detail else ""))


def show(path):
    """把轨迹文件原样打印成表，用于肉眼核对。"""
    with open(path, encoding="utf-8") as fp:
        lines = [json.loads(l) for l in fp if l.strip()]
    for rec in lines:
        kind = rec["type"]
        if kind == "step":
            print("     [step]    %-9s %-11s %-6s %7.1fms  tokens=%s/%s" % (
                rec["agent"], rec["name"], rec["status"],
                rec["duration_ms"], rec["tokens"]["in"], rec["tokens"]["out"]))
        elif kind == "run":
            print("     [run]     %s" % rec["question"])
        elif kind == "event":
            print("     [event]   %s" % rec["event"])
        else:
            print("     [run_end] status=%s steps=%s failed=%s %sms" % (
                rec["status"], rec["step_count"], rec["failed_steps"], rec["duration_ms"]))


def build_agent(script, tools=None):
    """用假模型搭一个与线上结构一致的 agent（同一个 create_agent、同一批工具）。"""
    return create_agent(
        ScriptedChatModel(script=script),
        tools=tools or [query_stats, ai_analyst.rank, ai_analyst.trend],
        system_prompt=ai_analyst.SYSTEM_PROMPT,
    )


def main():
    print("=" * 74)
    print("W0-04 验收：Agent 执行轨迹埋点")
    print("=" * 74)

    # ---------- 用例 1：正常两轮 ----------
    print("\n[1] 正常两轮：模型 → 工具 → 模型")
    real = json.loads(query_stats.invoke({"province": "广东"}))["统计"]
    answer = "广东省销售额合计 %s 元，利润 %s 元。" % (real["销售额"], real["利润"])
    ai_analyst.get_analyst = lambda trace=None: build_agent(two_turn_script("广东", answer))

    trace = Trace(question="广东省的销售额是多少？", source="test")
    text = ask_analyst("广东省的销售额是多少？", trace=trace)
    summary = trace.finish()

    data = load_run(trace.run_id)
    steps = data["steps"]
    show(summary["path"])

    check("产生了轨迹文件", Path(summary["path"]).exists(), summary["path"])
    check("步骤数为 3（模型/工具/模型）", len(steps) == 3, "实际 %d" % len(steps))
    check("第 1 步是模型决策", steps[0]["name"] == "chat_model" and steps[0]["meta"]["kind"] == "llm")
    check("第 2 步是真实工具调用",
          steps[1]["name"] == "query_stats" and steps[1]["meta"]["kind"] == "tool")
    check("第 3 步是最终回答", steps[2]["name"] == "chat_model")
    check("工具入参被记录", steps[1]["input"] == {"province": "广东"}, str(steps[1]["input"]))
    check("工具返回值来自真实 CSV", str(real["销售额"]) in str(steps[1]["output"]),
          "期望含 %s" % real["销售额"])
    check("耗时是真实值（>0）", all(s["duration_ms"] > 0 for s in steps))
    check("token 用量被读出", steps[0]["tokens"] == {"in": 486, "out": 31}, str(steps[0]["tokens"]))
    check("seq 严格递增", [s["seq"] for s in steps] == sorted(s["seq"] for s in steps))
    check("回答正常返回", text == answer)
    check("最终回答内容进了轨迹", "广东省销售额合计" in str(steps[2]["output"]))
    check("run 摘要统计正确",
          summary["step_count"] == 3 and summary["failed_steps"] == 0, str(summary["failed_steps"]))

    # ---------- 用例 2：工具报错 ----------
    print("\n[2] 工具报错：业务照常，轨迹记 failed")

    def broken_tool(province: str = "") -> str:
        """故意失败的查询工具"""
        raise ConnectionError("数据库连接被重置")

    agent = build_agent(two_turn_script("广东", "抱歉，取数失败。", tool_name="broken_tool"),
                        tools=[make_tool(broken_tool)])
    t2 = Trace(question="故意让工具失败", source="test")
    try:
        agent.invoke({"messages": [{"role": "user", "content": "广东省销售额"}]},
                     config={"callbacks": attach(t2)})
    except Exception as exc:                        # 框架也可能把异常抛回来，记录即可
        print("     框架将异常抛出：%s" % type(exc).__name__)
    s2 = t2.finish()
    d2 = load_run(t2.run_id)
    failed = [s for s in d2["steps"] if s["status"] == STATUS_FAILED]
    show(s2["path"])
    check("失败步骤被记为 failed", len(failed) >= 1, "%d 条" % len(failed))
    check("错误类型被保留", bool(failed) and failed[0]["error"]["type"] == "ConnectionError",
          str(failed[0]["error"]) if failed else "没有失败步骤")

    # ---------- 用例 3：不传 trace ----------
    print("\n[3] 不传 trace：行为与改动前一致")
    before = len(list_runs(limit=500))
    ai_analyst.get_analyst = lambda trace=None: build_agent(two_turn_script("江苏", "江苏省销售额略。"))
    text3 = ask_analyst("江苏省销售额是多少？")
    after = len(list_runs(limit=500))
    check("回答不受影响", bool(text3))
    check("没有多写轨迹文件", after == before, "%d → %d" % (before, after))

    # ---------- 用例 4：埋点自身出错 ----------
    print("\n[4] 埋点自身出错时不影响回答")

    class BrokenTrace(Trace):
        def begin(self, *a, **k):
            raise RuntimeError("埋点内部炸了")

    t4 = BrokenTrace(question="埋点故障", source="test")
    ai_analyst.get_analyst = lambda trace=None: build_agent(two_turn_script("浙江", "浙江省销售额略。"))
    try:
        ok4, err4 = bool(ask_analyst("浙江省销售额是多少？", trace=t4)), ""
    except Exception as exc:                        # noqa: BLE001
        ok4, err4 = False, "%s: %s" % (type(exc).__name__, exc)
    t4.finish()
    check("埋点崩溃，回答照常", ok4, err4)
    check("回调已关闭异常上抛", TraceCallbackHandler.raise_error is False)

    # ---------- 用例 5：端到端 ----------
    # 注意：路由在 W1-01 后改走三人班组，一次提问会依次经过 Analyst / Critic /
    # Curator，所以剧本要备齐四段（工具 → 初稿 → 质检 → 成文）。这里验的仍然
    # 只是"这条链路留不留痕"，不是班组逻辑本身（那在 tests/test_team.py）。
    print("\n[5] 端到端：/api/ai/ask 返回 run_id")
    import app as web
    team_script = [
        AIMessage(content="", tool_calls=[{"name": "query_stats", "args": {"province": "广东"},
                                          "id": "c1", "type": "tool_call"}]),
        AIMessage(content=answer),
        AIMessage(content='{"pass": true, "issues": [], "fix": ""}'),
        AIMessage(content=answer),
    ]
    fake = ScriptedChatModel(script=team_script)
    # agent 不再跨请求缓存（缓存会把本次的降级/trace 串到上一次），
    # 所以注入点改成两个函数：模型工厂与"有没有通道"的判断。
    ai_analyst.get_llm = lambda trace=None: fake
    ai_analyst.has_model_channel = lambda: True
    client = web.app.test_client()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
    resp = client.post("/api/ai/ask", json={"question": "广东省的销售额是多少？"})
    body = resp.get_json()
    print("     返回体：ok=%s run_id=%s steps=%s" % (
        body.get("ok"), body.get("run_id"), (body.get("trace") or {}).get("step_count")))
    check("HTTP 200 且 ok=true", resp.status_code == 200 and body.get("ok") is True,
          str(body.get("error", ""))[:60])
    check("返回体带 run_id", bool(body.get("run_id")), str(body.get("run_id")))
    # 3 步来自 Analyst（模型 → 工具 → 模型），Critic 与 Curator 各 1 步
    check("返回体带 trace 摘要", (body.get("trace") or {}).get("step_count") == 5,
          str((body.get("trace") or {}).get("step_count")))
    e2e = load_run(body.get("run_id"))
    check("前端拿到的 run_id 能读回完整轨迹", e2e is not None and len(e2e["steps"]) == 5)
    check("轨迹里三个角色都留下了痕迹",
          e2e is not None and {s["agent"] for s in e2e["steps"]} == {"Analyst", "Critic", "Curator"},
          str(sorted({s["agent"] for s in e2e["steps"]})) if e2e else "")

    # ---------- 汇总 ----------
    bad = [n for n, ok in results if not ok]
    print("\n" + "-" * 74)
    print("合计 %d 项，通过 %d 项，失败 %d 项" % (len(results), len(results) - len(bad), len(bad)))
    if bad:
        print("失败项：" + "、".join(bad))
    print("-" * 74)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
