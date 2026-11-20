# -*- coding: utf-8 -*-
"""模型降级 —— 台账 W2-02

一条链上放多个模型通道（主 → 备 → 再备）。调用时按顺序试，
前一个通道"坏了"就换下一个，并把切换这件事写进 trace 的 fallback 事件。

--------------------------------------------------------------------
先说清楚这个模块**不做什么**，否则很容易被说成一个它做不到的东西。

1. 它不修"话说错了"。
   模型答非所问、编数字、越界承诺 —— 这些换一个模型只会换一种错法。
   那些归 Critic 和数字回查管（W1-01 / W0-05）。降级只治"通道坏了"：
   连不上、超时、401/403、429 限流、5xx。这个边界写在 is_channel_error()
   里，不是靠注释约束 —— 非通道类异常会原样向上抛，不会触发切换。

2. 它不掩盖。
   切换过就必须留痕：trace 里有一条 event=fallback，接口返回里有
   fallbacks 列表。悄悄降级然后当作一切正常，比直接失败更糟 ——
   用户以为这是主力模型的水平，实际已经是备胎在答。

3. 它不能让备用模型变成主力。
   通道跳闸后有冷却期（默认 300s）。冷却结束会重新允许尝试主通道
   （半开探测）。一次网络抖动不该把主模型封印一整天。

--------------------------------------------------------------------
为什么"连续失败 N 次"而不是"失败一次就切"：
    单次 429 或一次瞬时超时是很常见的抖动。立刻永久切换会让主力模型
    因为一次抖动就长期闲置，而且每次抖动都写一条 fallback，账本全是噪声。
    N 可配，默认 1 —— 对本项目更合适：一次用户提问只值一次等待，
    与其在坏通道上重试三次（3 × 20s 超时 = 一分钟），不如立刻换人干。

为什么内层调用不传 config：
    config 里挂着 TraceCallbackHandler。如果把 config 透传给子模型，
    一次失败调用 + 一次成功调用会在轨迹里记成两条 llm step（其中一条
    属于那个已经作废的通道），token 账也会重复计。所以只在外层
    记一次，通道层面的失败用 event 记 —— 两者职责不同，不该混。

本模块不依赖 flask。
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "models.json"

__all__ = [
    "Channel", "FallbackChatModel", "AllChannelsFailed",
    "is_channel_error", "load_config", "load_channels", "build_model",
    "channel_status", "fallback_events", "last_model_of",
]

# HTTP 状态码分类。两个集合都不在里面的（例如 400/404 之外的 2xx 变体）
# 一律按"通道问题"保守处理 —— 判断不了的时候，宁可切一次也不要把
# 一次可以降级解决的故障报成系统崩溃。
CHANNEL_STATUS = {401, 403, 404, 408, 409, 425, 429}

# 这几类明确不降级：请求本身构造有问题（格式错、参数错、上下文超长）。
# 换模型只是把同一个错误换个地方报，还会把"代码 bug"伪装成"通道故障"。
FATAL_STATUS = {400, 413, 422}

assert not (CHANNEL_STATUS & FATAL_STATUS)


# ================================================================ 通道

@dataclass
class Channel:
    """一个模型通道：名字（写进 trace 用）+ 实际模型对象 + 原始配置。"""

    name: str
    model: Any
    spec: Dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict:
        return {
            "name": self.name,
            "model": self.spec.get("model") or getattr(self.model, "model_name", "") or "",
            "provider": self.spec.get("provider", "openai"),
            "base_url": self.spec.get("base_url") or "",
            "kind": type(self.model).__name__,
        }


class AllChannelsFailed(RuntimeError):
    """整条链都失败了。带每个通道的失败原因，方便直接贴到界面上。"""

    def __init__(self, attempts: List[dict]):
        self.attempts = list(attempts or [])
        if self.attempts:
            parts = ["%s（%s）" % (a["channel"], a["error_type"]) for a in self.attempts]
            msg = "所有模型通道均不可用：" + "；".join(parts) + "。本次请求无法完成。"
        else:
            msg = "没有可用的模型通道（配置为空或全部未启用）。"
        super().__init__(msg)


def is_channel_error(exc: BaseException) -> bool:
    """这个异常值不值得换一个模型再试？

    判断顺序有意为之：先看 HTTP 状态码（最确定），再看内置网络异常，
    最后才退到类名匹配（不同 SDK 的类名不一样，但都带这些词根）。
    匹配不上的一律返回 False —— 不认识的错误不该被当成"通道故障"糊过去。
    """
    code = getattr(exc, "status_code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        if code in FATAL_STATUS:
            return False
        return code in CHANNEL_STATUS or code >= 500

    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True

    name = type(exc).__name__.lower()
    if name in ("badrequesterror", "unprocessableentityerror",
                "valueerror", "typeerror", "keyerror", "attributeerror"):
        return False
    for token in ("timeout", "connection", "ratelimit", "authentication",
                  "permissiondenied", "serviceunavailable", "internalserver",
                  "apiconnection", "apierror", "apitimeout"):
        if token in name:
            return True
    return False


# ================================================================ 运行时状态

class _State:
    """通道健康度。挂在模型对象上，同一个进程内共享。

    明确局限：这是进程内内存状态。多进程部署（gunicorn 多 worker）时
    每个 worker 各记各的，重启即清零。要跨进程共享得换 Redis 之类，
    本项目单进程演示，不值得为它引入依赖 —— 但这是真局限，写在这里。

    这里**不存 trace**：健康度要跨请求共享（跳闸记忆才有意义），
    而 trace 是每个请求一个。两者生命周期不同，混在一个对象里就会
    出现"降级事件写进了上一个请求的轨迹"。所以 log() 显式收 trace。
    """

    def __init__(self, trace=None, threshold: int = 1, cooldown_s: float = 300.0):
        self.threshold = max(1, int(threshold))
        self.cooldown_s = max(0.0, float(cooldown_s))
        self.fails: Dict[str, int] = {}
        self.tripped_at: Dict[str, float] = {}
        # 曾经出过故障、还没成功过的通道。和 fails 分开记：
        # 冷却到期（reopen）会把 fails 清零，但"它坏过"这件事对
        # channel_recovered 仍然有意义 —— 否则恢复日志永远等不到。
        self.bad: set = set()
        self.ok_name: Optional[str] = None
        self.calls = 0
        self.events: List[dict] = []

    # ---- 记账 ----

    def log(self, trace, event: str, **detail):
        rec = {"event": event, "at": round(time.monotonic(), 3)}
        rec.update(detail)
        self.events.append(rec)
        if trace is not None:
            try:
                trace.log(event, **detail)
            except Exception:                     # noqa: BLE001
                # 埋点不能把主流程搞挂，这条原则与 instrument 一致
                pass

    def note_fail(self, trace, name: str, exc: BaseException,
                  duration_ms: float) -> bool:
        """记一次失败。返回 True 表示这一次刚好达到阈值（该跳闸了）。"""
        n = self.fails.get(name, 0) + 1
        self.fails[name] = n
        self.bad.add(name)
        code = getattr(exc, "status_code", None)
        self.log(trace, "channel_fail",
                 channel=name,
                 error_type=type(exc).__name__,
                 # 状态码单独记：只留类名的话，现场看不出"是 401 还是超时"，
                 # 而这两种的处理方式完全不同（一个要换 key，一个只要重试）。
                 status_code=(code if isinstance(code, int) else None),
                 error=str(exc)[:200],
                 consecutive=n,
                 threshold=self.threshold,
                 duration_ms=round(duration_ms, 1))
        if n >= self.threshold and name not in self.tripped_at:
            self.tripped_at[name] = time.monotonic() + self.cooldown_s
            return True
        return False

    def note_ok(self, trace, name: str):
        if name in self.bad:
            self.log(trace, "channel_recovered", channel=name,
                     after_fails=self.fails.get(name, 0),
                     note="该通道本次成功，连续失败计数清零")
            self.bad.discard(name)
        self.fails[name] = 0
        self.tripped_at.pop(name, None)
        self.ok_name = name

    def order(self, trace, names: List[str]) -> tuple:
        """当前该按什么顺序试。返回 (顺序, 是否全部处于冷却期)。"""
        now = time.monotonic()
        for name in list(self.tripped_at):
            if self.tripped_at[name] <= now:
                self.tripped_at.pop(name, None)
                self.fails[name] = 0
                self.log(trace, "channel_reopen", channel=name,
                         note="冷却结束，重新允许尝试该通道")

        alive = [n for n in names if n not in self.tripped_at]
        if alive:
            return alive, False
        # 全都跳闸了还按原顺序硬试一遍：不试就只能直接失败，
        # 而"全部冷却"往往是因为整段时间都没人成功过，值得再探一次。
        return list(names), True


# ================================================================ 包装模型

class FallbackChatModel(BaseChatModel):
    """把一条通道链包装成一个模型。

    对上层（create_agent / llm.invoke）来说它就是一个普通聊天模型，
    降级完全发生在里面。这也是为什么 Team 一行都不用改。
    """

    chain: List[Any] = []                 # list[Channel]，用 Any 是为了不触发校验
    fail_threshold: int = 1
    cooldown_s: float = 300.0
    state: Any = None                     # _State，跨请求共享
    trace: Any = None                     # 本次 run 的 Trace，每个请求一个

    @property
    def _llm_type(self) -> str:
        return "fallback-chain"

    # ---------- 内部 ----------

    def _st(self) -> _State:
        if self.state is None:
            self.state = _State(threshold=self.fail_threshold,
                                cooldown_s=self.cooldown_s)
        return self.state

    @property
    def names(self) -> List[str]:
        return [c.name for c in self.chain]

    def _replica(self, channels: List[Channel]) -> "FallbackChatModel":
        new = FallbackChatModel(chain=channels,
                                fail_threshold=self.fail_threshold,
                                cooldown_s=self.cooldown_s)
        new.state = self._st()            # 共享同一份健康度，别让 bind 把账本拆开
        new.trace = self.trace            # trace 跟着这次请求走
        return new

    # ---------- 对上层暴露的接口 ----------

    def bind_tools(self, tools, **kwargs):
        """create_agent 会给每个模型绑工具。这里逐个绑，然后把链重建一份。"""
        channels = []
        for c in self.chain:
            model = c.model
            if hasattr(model, "bind_tools"):
                try:
                    model = model.bind_tools(tools, **kwargs)
                except Exception:             # noqa: BLE001
                    # 假模型 / 不支持工具绑定的通道：保持原样，失败在调用时暴露
                    model = c.model
            channels.append(Channel(c.name, model, c.spec))
        return self._replica(channels)

    def bind(self, **kwargs):
        # Runnable 的 bind 会包一层；直接返回自身副本即可，
        # 因为 stop / kwargs 已经在 _generate 里处理。
        return self._replica(list(self.chain))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        st = self._st()
        st.calls += 1
        trace = self.trace

        names, probing = st.order(trace, self.names)
        if probing:
            st.log(trace, "fallback_probe", channels=names,
                   note="全部通道处于冷却期，按原顺序再试一遍（半开探测）")

        by_name = {c.name: c for c in self.chain}
        attempts: List[dict] = []

        for idx, name in enumerate(names):
            ch = by_name[name]
            t0 = time.monotonic()
            try:
                # _skip_trace 让埋点跳过这一次内层调用（详见 instrument.py）。
                # LangChain 的回调经 contextvars 向下继承，不显式传 config
                # 也会触发外层那套回调 —— 不标记的话一次调用会记两条。
                cfg = {"metadata": {"_skip_trace": True}}
                message = (ch.model.invoke(messages, config=cfg, stop=stop) if stop
                           else ch.model.invoke(messages, config=cfg))
            except Exception as exc:              # noqa: BLE001
                ms = (time.monotonic() - t0) * 1000
                if not is_channel_error(exc):
                    # 非通道类错误：换模型修不好，原样抛出去，由上层处理
                    st.log(trace, "channel_error_fatal",
                           channel=name,
                           error_type=type(exc).__name__,
                           error=str(exc)[:200],
                           note="非通道类错误，不降级：换模型修不了请求本身的毛病")
                    raise

                attempts.append({
                    "channel": name,
                    "error_type": type(exc).__name__,
                    "status_code": (getattr(exc, "status_code", None)
                                    if isinstance(getattr(exc, "status_code", None), int)
                                    else None),
                    "error": str(exc)[:200],
                    "duration_ms": round(ms, 1),
                })
                tripped = st.note_fail(trace, name, exc, ms)
                if tripped:
                    nxt = names[idx + 1] if idx + 1 < len(names) else None
                    code = getattr(exc, "status_code", None)
                    st.log(trace, "fallback",
                           **{"from": name,
                              "to": nxt or "(无可用备用)",
                              "reason": "consecutive_fail",
                              "consecutive": st.fails[name],
                              "threshold": st.threshold,
                              "error_type": type(exc).__name__,
                              "status_code": (code if isinstance(code, int) else None),
                              "cooldown_s": st.cooldown_s,
                              "note": ("主通道判定为不可用，本进程内后续调用直接走备用"
                                       if nxt else "没有可用的备用通道")})
                continue

            st.note_ok(trace, name)
            return ChatResult(generations=[ChatGeneration(message=message)])

        st.log(trace, "fallback_exhausted", channels=names, attempts=attempts,
               note="全部通道失败，本次请求无法完成")
        raise AllChannelsFailed(attempts)

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        return {"chain": self.names, "fail_threshold": self.fail_threshold}


# ================================================================ 配置

def load_config(path=None) -> dict:
    """读 config/models.json。文件不存在时返回一个空壳，由调用方决定怎么报错。"""
    p = Path(path) if path else CONFIG_PATH
    if not p.exists():
        return {"channels": [], "fail_threshold": 1, "cooldown_s": 300,
                "_missing": str(p)}
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return {"channels": [], "fail_threshold": 1, "cooldown_s": 300,
                "_error": "%s: %s" % (type(exc).__name__, exc)}
    if not isinstance(cfg, dict):
        return {"channels": [], "fail_threshold": 1, "cooldown_s": 300,
                "_error": "配置根节点必须是对象"}
    return cfg


def _read_key(spec: dict) -> Optional[str]:
    raw = spec.get("key_file")
    if not raw:
        return spec.get("api_key") or None
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return None
    key = p.read_text(encoding="utf-8").strip()
    return key or None


def _make_model(spec: dict):
    """按配置造一个模型。返回 (model, None) 或 (None, 不可用原因)。"""
    provider = (spec.get("provider") or "openai").lower()
    model_name = spec.get("model")

    if provider in ("openai", "deepseek", "openai-compatible", "compat"):
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as exc:
            return None, "缺依赖 langchain_openai：%s" % exc

        key = _read_key(spec)
        if not key:
            return None, "缺少密钥文件 %s" % (spec.get("key_file") or "(未配置)")
        if not model_name:
            return None, "缺少 model 字段"

        kwargs: Dict[str, Any] = {
            "model": model_name,
            "api_key": key,
            "temperature": float(spec.get("temperature", 0.2)),
            "timeout": float(spec.get("timeout_s", 20)),
            # 关键：SDK 自己重试会让降级判断失真 —— 三次内部重试把一次
            # 明确的 401 拖成 30 秒，还让我们以为"试过一次就失败了"。
            # 重试策略归我方（W2-03），SDK 不许自己决定。
            "max_retries": 0,
        }
        if spec.get("base_url"):
            kwargs["base_url"] = spec["base_url"]
        try:
            return ChatOpenAI(**kwargs), None
        except Exception as exc:                  # noqa: BLE001
            return None, "构造失败 %s: %s" % (type(exc).__name__, str(exc)[:120])

    return None, "不认识的 provider：%s" % provider


def load_channels(cfg: dict) -> tuple:
    """把配置变成通道列表。返回 (channels, skipped)。

    skipped 不是错误 —— 它表示"这个槽位因为没配 key 而不能用"。
    这件事必须记进 trace：备用通道没配，等于没有备用，
    真出事的时候才发现就晚了。
    """
    channels: List[Channel] = []
    skipped: List[dict] = []
    for i, spec in enumerate(cfg.get("channels") or []):
        if not isinstance(spec, dict):
            skipped.append({"name": "channels[%d]" % i, "reason": "配置项不是对象"})
            continue
        name = spec.get("name") or "channel-%d" % i
        if spec.get("enabled") is False:
            skipped.append({"name": name, "reason": "enabled=false"})
            continue
        model, why = _make_model(spec)
        if model is None:
            skipped.append({"name": name, "reason": why})
            continue
        channels.append(Channel(name, model, spec))
    return channels, skipped


def build_model(trace=None, config_path=None, channels: Optional[List[Channel]] = None,
                cfg: Optional[dict] = None, state: Any = None) -> FallbackChatModel:
    """按配置造一个带降级能力的模型。上层拿到的就是一个普通模型。

    state 可以外部传进来 —— 进程里所有请求共用一份通道健康度，
    跳闸记忆才能跨请求生效。不传就自己新建（脚本、单测用）。
    """
    cfg = cfg if cfg is not None else load_config(config_path)
    if channels is None:
        channels, skipped = load_channels(cfg)
        for s in skipped:
            if trace is not None:
                try:
                    trace.log("channel_unavailable", **s)
                except Exception:                 # noqa: BLE001
                    pass
    else:
        skipped = []

    if not channels:
        detail = "；".join("%s: %s" % (s["name"], s["reason"]) for s in skipped) or "配置里没有通道"
        raise RuntimeError("没有可用的模型通道（%s）。请在 config/models.json 配置，"
                           "并把密钥写进对应文件。" % detail)

    threshold = int(cfg.get("fail_threshold", 1) or 1)
    cooldown = float(cfg.get("cooldown_s", 300) or 0)
    return FallbackChatModel(
        chain=list(channels),
        fail_threshold=threshold,
        cooldown_s=cooldown,
        state=state if state is not None else _State(threshold=threshold,
                                                     cooldown_s=cooldown),
        trace=trace,
    )


# ================================================================ 观测

_SHARED_STATE: Optional["_State"] = None


def shared_state(cfg: Optional[dict] = None) -> "_State":
    """进程级共享的通道健康度。

    跳闸记忆必须跨请求：如果每次请求都新建一份状态，那"连续失败"永远
    归零，降级就变成了"每次都在坏通道上白撞一次再换人"——
    既慢又费，等于没做。
    """
    global _SHARED_STATE
    if _SHARED_STATE is None:
        cfg = cfg if cfg is not None else load_config()
        _SHARED_STATE = _State(threshold=int(cfg.get("fail_threshold", 1) or 1),
                               cooldown_s=float(cfg.get("cooldown_s", 300) or 0))
    return _SHARED_STATE


def reset_shared_state():
    """清空跳闸记忆。给测试和 /api/team/models?reset=1 用。"""
    global _SHARED_STATE
    _SHARED_STATE = None


def channel_status(llm) -> dict:
    """给接口/编排屏看的通道健康度快照。"""
    chain = getattr(llm, "chain", None) or []
    st = getattr(llm, "state", None)
    now = time.monotonic()
    out = []
    for c in chain:
        name = c.name
        tripped = bool(st and name in st.tripped_at)
        out.append(dict(
            c.describe(),
            consecutive_fails=(st.fails.get(name, 0) if st else 0),
            tripped=tripped,
            cooldown_left_s=(round(max(0.0, st.tripped_at[name] - now), 1)
                             if tripped else 0.0),
        ))
    return {
        "channels": out,
        "calls": (st.calls if st else 0),
        "last_ok": (st.ok_name if st else None),
        "fail_threshold": (st.threshold if st else None),
        "cooldown_s": (st.cooldown_s if st else None),
    }


def fallback_events(trace) -> List[dict]:
    """从 trace 里取出所有降级相关事件。

    trace 是唯一真源：模型对象可能被 bind 出副本、进程内状态可能被重置，
    但写进 jsonl 的那一行不会变。
    """
    if trace is None:
        return []
    kind = ("fallback", "fallback_exhausted", "fallback_probe",
            "channel_unavailable", "channel_reopen")
    try:
        raw = trace.events()
    except Exception:                             # noqa: BLE001
        return []
    return [e for e in raw if e.get("event") in kind]


def last_model_of(llm) -> Optional[str]:
    st = getattr(llm, "state", None)
    return getattr(st, "ok_name", None) if st else None
