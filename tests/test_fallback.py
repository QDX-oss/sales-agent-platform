# -*- coding: utf-8 -*-
"""W2-02 验收：模型降级。

台账验收原文：「把主模型 key 改错，系统仍能出结果，trace 里有一条 fallback 记录」

这里不去真连 DeepSeek（本机没网、没 key），而是构造三种"主通道坏掉"的
等价情形：401（key 错）、连接失败（网络断）、超时。这三种在
is_channel_error() 里必须都判成"可降级"，而 ValueError 这类必须判成"不降级"。

跑法（项目根目录）：
    python tests/test_fallback.py
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_core.language_models.chat_models import BaseChatModel   # noqa: E402
from langchain_core.messages import AIMessage                          # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult          # noqa: E402

from core.fallback import (AllChannelsFailed, Channel, FallbackChatModel,  # noqa: E402
                           _State, build_model, channel_status,
                           fallback_events, is_channel_error, load_channels)
from core.trace import Trace, load_run                                 # noqa: E402
from tests.fake_llm import ScriptedChatModel                           # noqa: E402

PASS, FAIL = 0, 0


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [OK]   %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s   %s" % (name, extra))


# ---------------------------------------------------------------- 假模型

class FakeStatusError(Exception):
    """带 HTTP 状态码的异常，模拟 openai SDK 的 AuthenticationError 一类。"""

    def __init__(self, code, msg="invalid api key"):
        self.status_code = code
        super().__init__(msg)


class FlakyChatModel(BaseChatModel):
    """前 fail_times 次抛错，之后正常回话。calls 用于验证"有没有真的被调用"。"""

    fail_times: int = 0
    error_kind: str = "connection"
    content: str = "ok"
    calls: int = 0

    @property
    def _llm_type(self):
        return "flaky"

    def _raise(self):
        if self.error_kind == "connection":
            raise ConnectionError("模拟：无法连接到模型服务")
        if self.error_kind == "timeout":
            raise TimeoutError("模拟：请求超时")
        if self.error_kind == "value":
            raise ValueError("模拟：消息格式不合法")
        code = int(self.error_kind)
        raise FakeStatusError(code)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            self._raise()
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content=self.content,
            usage_metadata={"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}))])


def chain_of(primary, backup, threshold=1, cooldown=300.0, trace=None):
    st = _State(threshold=threshold, cooldown_s=cooldown)
    return FallbackChatModel(chain=[Channel("primary", primary, {"model": "primary"}),
                                    Channel("backup", backup, {"model": "backup"})],
                             fail_threshold=threshold, cooldown_s=cooldown,
                             state=st, trace=trace)


# ---------------------------------------------------------------- 1 分类

def test_classify():
    print("\n[1] 什么算通道故障 —— 这条界线决定了降级会不会掩盖真 bug")
    check("401 坏 key 可降级", is_channel_error(FakeStatusError(401)))
    check("403 无权限可降级", is_channel_error(FakeStatusError(403)))
    check("429 限流可降级", is_channel_error(FakeStatusError(429)))
    check("500 服务端错误可降级", is_channel_error(FakeStatusError(500)))
    check("503 可降级", is_channel_error(FakeStatusError(503)))
    check("ConnectionError 可降级", is_channel_error(ConnectionError("x")))
    check("TimeoutError 可降级", is_channel_error(TimeoutError("x")))
    check("400 请求构造错 不降级", not is_channel_error(FakeStatusError(400)))
    check("422 不降级", not is_channel_error(FakeStatusError(422)))
    check("ValueError 不降级", not is_channel_error(ValueError("x")))
    check("KeyError 不降级", not is_channel_error(KeyError("x")))


# ---------------------------------------------------------------- 2 基本切换

def test_basic_switch():
    print("\n[2] 主通道 401，备用接手 —— 台账验收的最小版本")
    primary = FlakyChatModel(fail_times=999, error_kind="401")
    backup = FlakyChatModel(content="备用通道的答案")
    llm = chain_of(primary, backup)

    out = llm.invoke("问题")
    check("仍然拿到了答案", out.content == "备用通道的答案", out.content)
    check("主通道确实被试过", primary.calls == 1, "calls=%d" % primary.calls)
    check("备用通道接手", backup.calls == 1, "calls=%d" % backup.calls)

    events = [e["event"] for e in llm.state.events]
    check("有一条 fallback 事件", events.count("fallback") == 1, str(events))
    fb = [e for e in llm.state.events if e["event"] == "fallback"][0]
    check("fallback 记了 from=primary / to=backup",
          fb["from"] == "primary" and fb["to"] == "backup", str(fb))
    check("fallback 记了原因与 HTTP 状态码",
          fb["reason"] == "consecutive_fail" and fb.get("status_code") == 401,
          str(fb))


# ---------------------------------------------------------------- 3 跳闸记忆

def test_trip_memory():
    print("\n[3] 跳闸之后不再白撞 —— 否则每次提问都要先等一次失败")
    primary = FlakyChatModel(fail_times=999, error_kind="connection")
    backup = FlakyChatModel(content="答案")
    llm = chain_of(primary, backup, cooldown=3600)

    llm.invoke("第一次")
    check("第一次调用后主通道跳闸", "primary" in llm.state.tripped_at)
    first_calls = primary.calls

    llm.invoke("第二次")
    check("第二次调用完全没有碰主通道", primary.calls == first_calls,
          "calls %d -> %d" % (first_calls, primary.calls))
    check("第二次仍然出结果", backup.calls == 2, "backup.calls=%d" % backup.calls)

    st = channel_status(llm)
    trip = [c for c in st["channels"] if c["name"] == "primary"][0]
    check("channel_status 里主通道 tripped=true", trip["tripped"] is True, str(trip))
    check("channel_status 里有剩余冷却秒数", trip["cooldown_left_s"] > 0,
          str(trip["cooldown_left_s"]))


# ---------------------------------------------------------------- 4 阈值

def test_threshold():
    print("\n[4] 连续失败 N 次才跳闸 —— N 是配置，不是硬编码")
    primary = FlakyChatModel(fail_times=999, error_kind="429")
    backup = FlakyChatModel(content="答案")
    llm = chain_of(primary, backup, threshold=3, cooldown=3600)

    for i in range(3):
        llm.invoke("第 %d 次" % (i + 1))
        n_fb = sum(1 for e in llm.state.events if e["event"] == "fallback")
        if i < 2:
            check("第 %d 次失败还没到阈值，不记 fallback" % (i + 1), n_fb == 0,
                  "fallback=%d" % n_fb)
        else:
            check("第 3 次失败刚好到阈值，记一条 fallback", n_fb == 1,
                  "fallback=%d" % n_fb)

    fails = sum(1 for e in llm.state.events if e["event"] == "channel_fail")
    check("每次失败都留了 channel_fail（3 条）", fails == 3, "channel_fail=%d" % fails)
    check("三次都出了结果（降级不等于失败）", backup.calls == 3, "backup.calls=%d" % backup.calls)


# ---------------------------------------------------------------- 5 不降级

def test_non_channel_error():
    print("\n[5] 不是通道的毛病，不许偷偷换人 —— 换模型修不了代码 bug")
    primary = FlakyChatModel(fail_times=999, error_kind="value")
    backup = FlakyChatModel(content="不该出现")
    llm = chain_of(primary, backup)

    raised = None
    try:
        llm.invoke("问题")
    except ValueError as exc:
        raised = exc
    check("ValueError 原样抛出去", isinstance(raised, ValueError), repr(raised))
    check("备用通道一次都没被调用", backup.calls == 0, "calls=%d" % backup.calls)
    events = [e["event"] for e in llm.state.events]
    check("没有 fallback 事件", "fallback" not in events, str(events))
    check("留了一条 channel_error_fatal 说明为什么不降级",
          "channel_error_fatal" in events, str(events))


# ---------------------------------------------------------------- 6 全挂

def test_all_failed():
    print("\n[6] 整条链都挂了 —— 必须明确失败，不能假装有答案")
    a = FlakyChatModel(fail_times=999, error_kind="401")
    b = FlakyChatModel(fail_times=999, error_kind="503")
    st = _State(threshold=1, cooldown_s=300)
    llm = FallbackChatModel(chain=[Channel("a", a, {}), Channel("b", b, {})],
                            state=st)

    err = None
    try:
        llm.invoke("问题")
    except AllChannelsFailed as exc:
        err = exc
    check("抛 AllChannelsFailed", err is not None)
    check("消息里点名了两个通道", err and "a（" in str(err) and "b（" in str(err),
          str(err)[:120])
    check("attempts 里两条失败明细", err and len(err.attempts) == 2,
          str(err.attempts if err else None))
    check("留了 fallback_exhausted 事件",
          any(e["event"] == "fallback_exhausted" for e in st.events))


# ---------------------------------------------------------------- 7 冷却

def test_cooldown():
    print("\n[7] 冷却到期会重新允许试主通道 —— 一次抖动不该封印主力")
    primary = FlakyChatModel(fail_times=0, error_kind="connection")
    primary.fail_times = 1        # 只坏第一次
    backup = FlakyChatModel(content="答案")
    llm = chain_of(primary, backup, cooldown=0)

    llm.invoke("第一次")
    check("第一次主通道失败后跳闸", "primary" in llm.state.tripped_at)
    llm.invoke("第二次")
    check("冷却为 0 时第二次会重新试主通道", primary.calls == 2, "calls=%d" % primary.calls)
    check("主通道这次成功了，跳闸状态被清掉", "primary" not in llm.state.tripped_at)
    check("并且记了 channel_reopen 与 channel_recovered",
          {"channel_reopen", "channel_recovered"} <= {e["event"] for e in llm.state.events},
          str([e["event"] for e in llm.state.events]))

    # 反过来：冷却很长时不该重试
    p2 = FlakyChatModel(fail_times=999, error_kind="connection")
    b2 = FlakyChatModel(content="答案")
    llm2 = chain_of(p2, b2, cooldown=3600)
    llm2.invoke("一")
    llm2.invoke("二")
    check("冷却期内第二次不试主通道", p2.calls == 1, "calls=%d" % p2.calls)


# ---------------------------------------------------------------- 8 trace

def test_trace_record():
    print("\n[8] 台账验收原文：trace 里有一条 fallback 记录")
    tmp = tempfile.mkdtemp(prefix="fb-run-")
    trace = Trace(question="广东销售额", source="test-fallback", runs_dir=tmp)

    primary = FlakyChatModel(fail_times=999, error_kind="401")
    backup = FlakyChatModel(content="已核对")
    llm = chain_of(primary, backup, trace=trace)

    llm.invoke("问题")
    trace.finish()

    data = load_run(trace.run_id, runs_dir=tmp)
    events = [e for e in data["events"] if e["event"] == "fallback"]
    check("jsonl 里真的有一条 event=fallback", len(events) == 1, str(len(events)))
    if events:
        d = events[0]["detail"]
        check("落盘内容含 from / to / reason",
              d.get("from") == "primary" and d.get("to") == "backup"
              and d.get("reason") == "consecutive_fail", str(d))
    check("fallback_events() 能从 trace 里读回来",
          len(fallback_events(trace)) == 1, str(fallback_events(trace)))


# ---------------------------------------------------------------- 9 不重复记账

def test_no_double_llm_step():
    print("\n[9] 一次问答只记一条 llm step —— 失败的那次不该混进 token 账")
    tmp = tempfile.mkdtemp(prefix="fb-run2-")
    trace = Trace(question="q", source="test", runs_dir=tmp)
    primary = FlakyChatModel(fail_times=999, error_kind="connection")
    backup = FlakyChatModel(content="答案")
    llm = chain_of(primary, backup, trace=trace)

    from core.instrument import TraceCallbackHandler
    llm.invoke("问题", config={"callbacks": [TraceCallbackHandler(trace)]})
    trace.finish()

    data = load_run(trace.run_id, runs_dir=tmp)
    check("llm step 只有 1 条（内层不透传 config，回调不会重复触发）",
          len(data["steps"]) == 1, "steps=%d" % len(data["steps"]))


# ---------------------------------------------------------------- 10 bind

def test_bind_shares_state():
    print("\n[10] bind_tools 复制出的副本必须共享同一份健康度")
    primary = FlakyChatModel(fail_times=999, error_kind="401")
    backup = ScriptedChatModel(script=[AIMessage(content="工具绑定后的答案")])
    llm = chain_of(primary, backup)

    bound = llm.bind_tools([])
    check("bind 后是另一个对象", bound is not llm)
    check("但共享同一份 state", bound.state is llm.state)
    out = bound.invoke("问题")
    check("在副本上发生的降级，原对象也看得到",
          out.content == "工具绑定后的答案"
          and any(e["event"] == "fallback" for e in llm.state.events),
          str([e["event"] for e in llm.state.events]))


# ---------------------------------------------------------------- 11 配置

def test_config():
    print("\n[11] 配置：没配 key 的槽位要被跳过，并且留下原因")
    tmpdir = Path(tempfile.mkdtemp(prefix="fb-cfg-"))
    cfg_path = tmpdir / "models.json"
    cfg_path.write_text(json.dumps({
        "fail_threshold": 2,
        "cooldown_s": 60,
        "channels": [
            {"name": "nokey", "provider": "openai", "model": "m1",
             "key_file": str(tmpdir / "nope.txt")},
            {"name": "off", "provider": "openai", "model": "m2",
             "key_file": str(tmpdir / "nope.txt"), "enabled": False},
            {"name": "good", "provider": "openai", "model": "m3",
             "api_key": "sk-test-fake", "base_url": "http://127.0.0.1:9/v1"},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    channels, skipped = load_channels(cfg)
    check("只留下配好的那一个通道", [c.name for c in channels] == ["good"],
          str([c.name for c in channels]))
    check("没 key 的被跳过且给了原因",
          any(s["name"] == "nokey" and "密钥" in s["reason"] for s in skipped),
          str(skipped))
    check("enabled=false 的被跳过",
          any(s["name"] == "off" and "enabled" in s["reason"] for s in skipped),
          str(skipped))

    model = build_model(cfg=cfg)
    check("build_model 读到了 fail_threshold", model.fail_threshold == 2,
          str(model.fail_threshold))
    check("build_model 读到了 cooldown_s", model.cooldown_s == 60.0,
          str(model.cooldown_s))

    empty = {"channels": [], "fail_threshold": 1, "cooldown_s": 1}
    err = None
    try:
        build_model(cfg=empty)
    except RuntimeError as exc:
        err = exc
    check("一个通道都没有时明确报错", err is not None and "没有可用的模型通道" in str(err),
          str(err)[:80])

    real = json.loads((ROOT / "config" / "models.json").read_text(encoding="utf-8"))
    check("项目里真实的 config/models.json 能解析且 channels 是数组",
          isinstance(real.get("channels"), list) and len(real["channels"]) >= 1)


# ---------------------------------------------------------------- 12 端到端

def test_team_end_to_end():
    print("\n[12] 端到端：主通道 401，六个节点的班组照样跑完并给出答案")
    import ai_analyst
    from core.team import run_team

    gd = json.loads(ai_analyst.query_stats.invoke({"province": "广东"}))["统计"]
    question = "广东省的销售额和利润率是多少？"
    draft_ok = "广东省销售额 %s 元，利润 %s 元，利润率 %s%%。" % (
        gd["销售额"], gd["利润"], gd["利润率"])
    critic_pass = json.dumps({"pass": True, "issues": [], "fix": ""}, ensure_ascii=False)
    curated = "%s 元销售额，利润率 %s%%。" % (gd["销售额"], gd["利润率"])

    script = [
        AIMessage(content="", tool_calls=[{"name": "query_stats",
                                           "args": {"province": "广东"},
                                           "id": "c1", "type": "tool_call"}],
                  usage_metadata={"input_tokens": 400, "output_tokens": 20, "total_tokens": 420}),
        AIMessage(content=draft_ok,
                  usage_metadata={"input_tokens": 500, "output_tokens": 40, "total_tokens": 540}),
        AIMessage(content=critic_pass,
                  usage_metadata={"input_tokens": 800, "output_tokens": 20, "total_tokens": 820}),
        AIMessage(content=curated,
                  usage_metadata={"input_tokens": 700, "output_tokens": 30, "total_tokens": 730}),
    ]

    primary = FlakyChatModel(fail_times=999, error_kind="401")
    backup = ScriptedChatModel(script=script)

    tmp = tempfile.mkdtemp(prefix="fb-team-")
    trace = Trace(question=question, source="test-fallback-team", runs_dir=tmp)
    llm = chain_of(primary, backup, trace=trace)
    out = run_team(question, llm, [ai_analyst.query_stats, ai_analyst.rank,
                                   ai_analyst.trend], trace=trace)
    trace.finish()

    check("答案非空", bool(out["answer"].strip()), out["answer"][:60])
    check("答案出自广东的真实数字", str(int(gd["销售额"])) in out["answer"],
          out["answer"][:80])
    check("六个节点都跑到了（图完成）", out["graph"]["completed"] is True)
    check("result 里带 fallbacks", len(out["fallbacks"]) >= 1, str(out["fallbacks"]))
    check("fallback_used=true", out["fallback_used"] is True)
    check("result 里写明最终是备用通道答的", out["model"] == "backup", str(out["model"]))

    data = load_run(trace.run_id, runs_dir=tmp)
    fb = [e for e in data["events"] if e["event"] == "fallback"]
    check("轨迹文件里那条 fallback 是真的落盘了", len(fb) == 1, str(len(fb)))
    llm_steps = [s for s in data["steps"] if s["agent"] in ("Analyst", "Critic", "Curator")]
    check("三次角色模型调用都记进了轨迹（4 次里含 1 次工具轮）",
          len(llm_steps) >= 3, "llm_steps=%d" % len(llm_steps))


# ---------------------------------------------------------------- main

def main():
    print("=" * 66)
    print("W2-02 验收：模型降级（主通道坏了，班组仍能出结果）")
    print("=" * 66)
    test_classify()
    test_basic_switch()
    test_trip_memory()
    test_threshold()
    test_non_channel_error()
    test_all_failed()
    test_cooldown()
    test_trace_record()
    test_no_double_llm_step()
    test_bind_shares_state()
    test_config()
    test_team_end_to_end()
    print("\n" + "=" * 66)
    print("合计：%d 项通过，%d 项失败" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
