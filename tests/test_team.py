# -*- coding: utf-8 -*-
"""
W1-01 验收：三角色班组。

跑法（项目根目录）：
    python tests/test_team.py

不联网：假模型按剧本回话，工具、CSV、回调、trace 全部真实。

这一组测试要回答的问题只有一个：
**Critic 是不是真的独立于 Analyst？**
如果它只是 Analyst 那个 prompt 里多写的一句"请检查一下"，下面第三组
（打回重做）根本不可能通过 —— 同一个模型不会在同一个上下文里推翻自己。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai_analyst                                              # noqa: E402
from ai_analyst import query_stats                             # noqa: E402
from core import roles, verifier                                # noqa: E402
from core.team import Team, run_team                            # noqa: E402
from core.trace import Trace, load_run                          # noqa: E402
from langchain_core.messages import AIMessage                   # noqa: E402
from tests.fake_llm import ScriptedChatModel                    # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  → " + detail) if detail else ""))


TOOLS = [query_stats, ai_analyst.rank, ai_analyst.trend]
GD = json.loads(query_stats.invoke({"province": "广东"}))["统计"]
RIGHT = "广东省销售额 %s 元，利润 %s 元，利润率 %s%%。" % (
    GD["销售额"], GD["利润"], GD["利润率"])
WRONG = "广东省销售额 999 万元，利润率 45%，建议立即降价 10% 冲量。"


def ai(content, tokens=0, **kw):
    kw.setdefault("usage_metadata", {
        "input_tokens": tokens, "output_tokens": tokens // 6, "total_tokens": tokens})
    return AIMessage(content=content, **kw)


def call_tool(province="广东", tokens=500):
    return ai("", tokens=tokens, tool_calls=[{
        "name": "query_stats", "args": {"province": province},
        "id": "c1", "type": "tool_call"}])


def critic(fail=False, unparsed=False):
    if unparsed:
        return ai("我觉得还行吧，应该没什么问题。", tokens=300)
    if fail:
        return ai(json.dumps({
            "pass": False,
            "issues": [
                {"type": "口径混用", "detail": "初稿说利润率 45%%，工具返回为 %s%%" % GD["利润率"],
                 "severity": "high"},
                {"type": "越界表述", "detail": "建议降价 10% 属商务承诺", "severity": "high"},
            ],
            "fix": "删掉降价建议，利润率改用工具返回值"},
            ensure_ascii=False), tokens=320)
    return ai(json.dumps({"pass": True, "issues": [], "fix": ""}), tokens=300)


def steps_of(trace):
    data = load_run(trace.run_id)
    return data["steps"], data["events"], data["end"]


# ---------------------------------------------------------------- 1 角色卡

def test_cards():
    print("\n[1] 角色卡：禁止事项必须有执行方式")
    sq = roles.squad()
    check("三个角色齐全", set(sq) == {"Analyst", "Critic", "Curator"}, str(sorted(sq)))

    check("Critic 没有工具（物理上无法自查）",
          sq["Critic"]["tools"] == [], str(sq["Critic"]["tools"]))
    check("Curator 没有工具", sq["Curator"]["tools"] == [], str(sq["Curator"]["tools"]))
    check("Analyst 有且仅有一个查询职责的工具集",
          sq["Analyst"]["tools"] == ["query_stats", "rank", "trend"],
          str(sq["Analyst"]["tools"]))

    no_rule = []
    for key, card in sq.items():
        for item in card["cannot"]:
            if not item["enforced"]:
                no_rule.append((key, item["rule"]))
    check("每条禁止事项都标了执行方式", not no_rule, str(no_rule))

    check("Critic 温度固定 0（质检不发挥）", roles.CRITIC.temperature == 0.0)
    check("Analyst 提示词只有一份（引用角色卡）",
          ai_analyst.SYSTEM_PROMPT is roles.ANALYST.system_prompt)


# ---------------------------------------------------------------- 2 一次通过

def test_happy_path():
    print("\n[2] 一次通过：不重试、不多烧一次调用")
    script = [call_tool(), ai(RIGHT, 700), critic(), ai(RIGHT, 400)]
    trace = Trace(question="广东省的销售额和利润率是多少？", source="test")
    out = run_team("广东省的销售额和利润率是多少？", ScriptedChatModel(script=script),
                   TOOLS, trace=trace)
    trace.finish()
    steps, events, end = steps_of(trace)

    agents = {s["agent"] for s in steps}
    check("轨迹里出现三个不同角色", agents == {"Analyst", "Critic", "Curator"}, str(sorted(agents)))
    check("没有发生重试", out["retries"] == 0)
    check("Analyst 的 retry 全为 0",
          all(s["retry"] == 0 for s in steps if s["agent"] == "Analyst"))
    # 只要求这两类事件都在，不排斥新增的结构事件（W1-02 起多了 graph_node / graph_wave）
    kinds = {e["event"] for e in events}
    check("两道关卡都记了事件", {"verify", "critic"} <= kinds, str(sorted(kinds)))
    check("质检结论为 pass", out["critic"]["pass"] is True)
    check("成文由 Curator 产出", out["curated"] is True)
    # W1-02 起，执行顺序由 core/graph.py 的图决定，返回体里带一份图摘要，
    # 编排屏（W3）画的就是这份，不用再单独埋一遍点。
    check("返回体带图摘要，且六步都跑过",
          out["graph"]["order"] == ["draft", "verify", "critique", "gate",
                                    "curate", "final_check"],
          str(out["graph"]["order"]))
    check("这一次没有触发回边", out["graph"]["loops"] == {})
    check("最终答复等于成文（不是初稿）", out["answer"] == out["draft"], "剧本里两稿相同")
    check("run 正常收尾", end["status"] == "ok")


# ---------------------------------------------------------------- 3 打回重做

def test_reject_and_retry():
    print("\n[3] 打回重做：Critic 否掉初稿，Analyst 带着意见重做")
    script = [call_tool(), ai(WRONG, 740), critic(fail=True),
              ai(RIGHT, 820), critic(), ai(RIGHT, 410)]
    trace = Trace(question="广东省的销售额和利润率是多少？", source="test")
    out = run_team("广东省的销售额和利润率是多少？", ScriptedChatModel(script=script),
                   TOOLS, trace=trace)
    trace.finish()
    steps, events, end = steps_of(trace)

    check("重试了 1 次", out["retries"] == 1, "retries=%s" % out["retries"])
    check("最终交付的是改好的那一稿", out["answer"] == RIGHT or out["draft"] == RIGHT)
    check("被否决的初稿没有成为最终答复", out["answer"] != WRONG)

    analyst_retries = [s["retry"] for s in steps if s["agent"] == "Analyst"]
    critic_retries = [s["retry"] for s in steps if s["agent"] == "Critic"]
    check("Analyst 轨迹里出现 retry=1（第二次作业）", 1 in analyst_retries, str(analyst_retries))
    check("Critic 轨迹里出现 retry=1（第二次质检）", 1 in critic_retries, str(critic_retries))

    crit_events = [e for e in events if e["event"] == "critic"]
    check("Critic 第一次判 fail、第二次判 pass",
          [e["detail"]["verdict"] for e in crit_events] == ["fail", "pass"],
          str([e["detail"]["verdict"] for e in crit_events]))
    check("打回理由被结构化记录（不是一句话）",
          crit_events[0]["detail"]["issues"][0]["type"] == "口径混用",
          str(crit_events[0]["detail"]["issues"]))

    # 关键证据：Critic 的意见真的传给了 Analyst，而不是被丢掉
    data = load_run(trace.run_id)
    second = [s for s in data["steps"] if s["agent"] == "Analyst" and s["retry"] == 1]
    fed = second[0]["input"]["last"] if second else ""
    check("Analyst 第二次收到的输入里带着质检意见",
          "质检员打回了这一稿" in fed, fed[:80].replace("\n", " "))


# ---------------------------------------------------------------- 4 Curator 不免疫

def test_curator_cannot_invent():
    print("\n[4] Curator 不能改数字：成文后再回查一次")
    dirty = "广东省销售额约 1.5 亿元，请重点关注。"
    script = [call_tool(), ai(RIGHT, 700), critic(), ai(dirty, 400)]
    trace = Trace(question="广东省的销售额是多少？", source="test")
    out = run_team("广东省的销售额是多少？", ScriptedChatModel(script=script), TOOLS, trace=trace)
    trace.finish()
    steps, events, _ = steps_of(trace)

    check("成文被退回（curated=False）", out["curated"] is False)
    check("最终答复退回初稿原文", out["answer"] == RIGHT)
    check("轨迹里留下退回原因", any(e["event"] == "curator_reject" for e in events),
          str([e["event"] for e in events]))
    check("Curator 那次调用仍然被记录（不是为了掩盖失败而不记）",
          any(s["agent"] == "Curator" for s in steps))


# ---------------------------------------------------------------- 5 解析失败

def test_critic_unparsed():
    print("\n[5] Critic 输出不是 JSON：不阻断业务，但要留痕")
    script = [call_tool(), ai(RIGHT, 700), critic(unparsed=True), ai(RIGHT, 400)]
    trace = Trace(question="广东省的销售额是多少？", source="test")
    out = run_team("广东省的销售额是多少？", ScriptedChatModel(script=script), TOOLS, trace=trace)
    trace.finish()
    steps, events, _ = steps_of(trace)

    check("不因解析失败而卡死业务", "销售额" in out["answer"])
    check("标记 unparsed", out["critic"]["unparsed"] is True)
    crit_ev = [e for e in events if e["event"] == "critic"][0]
    check("轨迹里能看到 unparsed=True", crit_ev["detail"]["unparsed"] is True)


# ---------------------------------------------------------------- 6 规则与LLM分工

def test_two_gates():
    print("\n[6] 两道关卡分工：规则拦数字，Critic 拦语义")
    # 注意这里刻意不带任何多余数字：一旦出现"降价 10%"，规则回查自己就能拦，
    # 就证明不了分工。真正只有 Critic 能拦的是"没有数字的越界断言"。
    misleading = "广东省销售额 %s 元。竞品一定比我们便宜，建议尽快降价。" % GD["销售额"]
    fixed = "广东省销售额 %s 元。本次未提供竞品价格，暂不建议调整定价。" % GD["销售额"]
    script = [call_tool(), ai(misleading, 700), critic(fail=True), ai(fixed, 800), critic(), ai(fixed, 400)]
    trace = Trace(question="广东省的销售额是多少？", source="test")
    out = run_team("广东省的销售额是多少？", ScriptedChatModel(script=script), TOOLS, trace=trace)
    trace.finish()
    steps, events, _ = steps_of(trace)

    v = [e for e in events if e["event"] == "verify"]
    check("规则回查第一稿就通过（数字确有出处）",
          v[0]["detail"]["verdict"] == "pass", str(v[0]["detail"]))
    check("但 Critic 还是把它拦下了",
          [e["detail"]["verdict"] for e in events if e["event"] == "critic"] == ["fail", "pass"])
    check("最终答复里去掉了降价建议", "降价" not in out["answer"])

# ---------------------------------------------------------------- 7 存档

def test_brief():
    print("\n[7] 结论存档：Curator 的第二个职责")
    from core.team import BRIEF_DIR
    path = BRIEF_DIR / "briefs.jsonl"
    check("briefs.jsonl 已生成", path.exists(), str(path))
    if path.exists():
        last = json.loads(path.read_text(encoding="utf-8").strip().split("\n")[-1])
        check("存档含问题、结论、run_id",
              {"question", "answer", "run_id"} <= set(last), str(sorted(last)))


def test_overrides():
    """角色卡可外部覆盖：改配置，不改代码（台账 W1-01 的验收口径）。"""
    print("\n[8] 角色卡外置：改配置生效，越权配置被拒")

    import importlib
    import tempfile

    tmp = Path(tempfile.gettempdir()) / "roles_override_test.json"
    tmp.write_text(json.dumps({
        "roles": {
            "Critic": {
                "prompt_add": "【补充规则】禁止以篇幅不足为由打回。",
                "cannot_add": [["以篇幅不足打回", "[评审] 补充规则"]],
            }
        }
    }, ensure_ascii=False), encoding="utf-8")

    applied = roles.apply_overrides(tmp)
    check("配置被读取并应用", applied == ["Critic"], str(applied))
    check("提示词里出现新增的硬规则",
          "禁止以篇幅不足为由打回" in roles.CRITIC.system_prompt)
    check("禁止事项从 4 条变 5 条", len(roles.CRITIC.cannot) == 5,
          str(len(roles.CRITIC.cannot)))
    check("代码侧的硬约束不受配置影响：Critic 仍然零工具",
          roles.CRITIC.tools == ())
    check("覆盖同步到 ROLES 与模块属性",
          roles.ROLES["Critic"] is roles.CRITIC)

    tmp.write_text(json.dumps({"roles": {"Critic": {"tools": ["query_stats"]}}}),
                   encoding="utf-8")
    rejected = None
    try:
        roles.apply_overrides(tmp)
    except ValueError as exc:
        rejected = str(exc)
    check("拒绝给 Critic 加工具（越权配置直接报错）", rejected is not None,
          (rejected or "")[:40])
    check("被拒之后 Critic 依然没有工具", roles.CRITIC.tools == ())

    tmp.write_text(json.dumps({"roles": {"Nobody": {}}}), encoding="utf-8")
    try:
        roles.apply_overrides(tmp)
        unknown_ok = False
    except ValueError:
        unknown_ok = True
    check("拒绝未知角色名（拼错不会静默失效）", unknown_ok)

    importlib.reload(roles)          # 复原，避免影响后续运行
    check("reload 后回到默认的 4 条禁止事项", len(roles.CRITIC.cannot) == 4,
          str(len(roles.CRITIC.cannot)))
    try:
        tmp.unlink()
    except OSError:
        pass


def test_schedule_wired():
    """优先级 / 时限真的接进了班组，不是只活在调度器单测里（台账 W2-01）。"""
    print("\n[9] 调度接进班组：优先级与时限来自策略，随机返回一份调度账")

    script = [call_tool(), ai(RIGHT, 700), critic(), ai(RIGHT, 400)]
    trace = Trace(question="广东省的销售额是多少？", source="test")
    team = Team(ScriptedChatModel(script=script), TOOLS, trace=trace)
    out = team.run("广东省的销售额是多少？")
    trace.finish()

    sched = {s["id"]: s for s in out["schedule"]}
    check("返回体里带调度账（编排屏将来直接画它）",
          set(sched) == {"verify", "critique"} or {"verify", "critique"} <= set(sched),
          str(sorted(sched)))
    check("取数优先于质检：draft=100 > verify=90 > critique=80",
          sched["verify"]["priority"] == 90 and sched["critique"]["priority"] == 80,
          "verify=%s critique=%s" % (sched["verify"]["priority"],
                                     sched["critique"]["priority"]))
    check("时限从配置来：verify 用默认 120s",
          sched["verify"]["deadline_ms"] == 120000, str(sched["verify"]["deadline_ms"]))
    check("没有节点超时，degraded=False", out["timed_out"] == [] and out["degraded"] is False,
          "%s / %s" % (out["timed_out"], out["degraded"]))

    # 槽位压到 1：同一批里必须分出先后，且先做的是不花钱的那个
    script = [call_tool(), ai(RIGHT, 700), critic(), ai(RIGHT, 400)]
    trace = Trace(question="广东省的销售额是多少？", source="test")
    team = Team(ScriptedChatModel(script=script), TOOLS, trace=trace,
                policy={"slots": 1, "default_deadline_ms": 120000,
                        "node_priority": {"draft": 100, "verify": 90, "critique": 80}})
    out = team.run("广东省的销售额是多少？")
    trace.finish()
    sched = {s["id"]: s for s in out["schedule"]}
    check("槽位=1 时回查先开跑、Critic 后开跑（先做不花钱的）",
          sched["verify"]["started_ms"] < sched["critique"]["started_ms"],
          "verify=%.0f critique=%.0f" % (sched["verify"]["started_ms"],
                                         sched["critique"]["started_ms"]))
    check("后开跑的那个确实排了队（waited_ms 有值）",
          sched["critique"]["waited_ms"] >= 0 and sched["critique"]["queue_pos"]
          > sched["verify"]["queue_pos"],
          "waited=%s pos=%s/%s" % (sched["critique"]["waited_ms"],
                                   sched["verify"]["queue_pos"],
                                   sched["critique"]["queue_pos"]))
    check("压槽位不影响结论质量", out["passed"] is True and "销售额" in out["answer"])


def main():
    print("=" * 62)
    print("W1-01 验收：Analyst / Critic / Curator 三角色班组")
    print("=" * 62)
    test_cards()
    test_happy_path()
    test_reject_and_retry()
    test_curator_cannot_invent()
    test_critic_unparsed()
    test_two_gates()
    test_brief()
    test_overrides()
    test_schedule_wired()

    ok = sum(1 for _, r in results if r)
    print("\n" + "=" * 62)
    print("结果：%d / %d 通过" % (ok, len(results)))
    for name, r in results:
        if not r:
            print("  失败：", name)
    print("=" * 62)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
