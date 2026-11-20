# -*- coding: utf-8 -*-
"""
LangChain 回调 → 运行轨迹的桥接器 —— 台账 W0-04

作用：把框架内部发生的事（调了哪个模型、调了哪个工具、多久、多少 token、
成没成）变成 trace 里的 step 记录。

为什么需要它：
在此之前，一次提问在系统里是不留痕的——只能看到"问题进、答案出"。
有了这个桥，才能回答"刚才那步为什么这么决策""第几次才成功"。

设计上的三条自我约束：

1. 绝不影响主流程。所有回调内部都吞掉异常：埋点把业务搞挂了，
   比没有埋点更糟。数据可以少记一条，答案不能少给一次。

2. 只记有信息量的东西。LangChain 内部会触发大量 chain 回调，
   全记下来会得到几百条噪声，反而看不清主干。这里只记
   「模型调用」和「工具调用」两类，它们才是流程的骨架。

3. 超长内容截断但留标记。工具返回的排名可能是几十行 JSON，
   原样写进 jsonl 会让文件难以阅读；截断时打 meta.truncated，
   避免"看到的是全部"这种误解。

本模块不依赖 flask。
"""

import json
import threading

from langchain_core.callbacks import BaseCallbackHandler

from .trace import STATUS_FAILED

# 单条内容写入文件的上限（字符）。超出部分丢弃并打标记。
MAX_CONTENT = 8000

KIND_LLM = "llm"
KIND_TOOL = "tool"


def _short(value, limit=MAX_CONTENT):
    """把任意对象转成可读文本，超长则截断。返回 (文本, 是否被截断)。"""
    if value is None:
        return None, False
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    if len(text) > limit:
        return text[:limit] + "...[truncated]", True
    return text, False


def _msg_text(message):
    """从一条消息里取出可用于展示的文本（兼容 content 为 list 的新格式）。"""
    if message is None:
        return ""
    content = getattr(message, "content", message)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def _parse_input(payload):
    """工具入参尽力解析成 dict；解析不出来就原样保留。

    on_tool_start 给的可能是已经解析好的 dict，也可能是 JSON 字符串，
    甚至可能是模型生成的半截 JSON（工具会因此报错）。三种都别让它炸。
    """
    if isinstance(payload, dict):
        text, cut = _short(payload, 1000)
        return payload if not cut else text
    if isinstance(payload, str) and payload.strip().startswith("{"):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            pass
    shown, cut = _short(payload, 1000)
    return shown


def _usage(response):
    """从 LLMResult 里取 token 用量，取不到就返回 (0, 0)。

    LangChain 的用量位置随版本变化，这里按可靠性从高到低依次尝试：
    消息级 usage_metadata → llm_output.token_usage → 无。
    """
    try:
        generations = getattr(response, "generations", None) or []
        if generations and generations[0]:
            message = getattr(generations[0][0], "message", None)
            usage = getattr(message, "usage_metadata", None)
            if usage:
                return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    except (AttributeError, TypeError, ValueError, IndexError):
        pass

    try:
        raw = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
        return int(raw.get("prompt_tokens") or 0), int(raw.get("completion_tokens") or 0)
    except (AttributeError, TypeError, ValueError):
        return 0, 0


class TraceCallbackHandler(BaseCallbackHandler):
    """挂到 LangChain config.callbacks 上，把每次模型/工具调用写进 trace。

        trace = Trace(question="本月销售额")
        agent.invoke(payload, config={"callbacks": [TraceCallbackHandler(trace)]})
        trace.finish()
    """

    # 回调里抛异常会中断推理，这里显式关掉 LangChain 的异常上抛行为
    raise_error = False

    def __init__(self, trace, agent="Analyst", retry=0):
        self.trace = trace
        self.agent = agent              # 工具调用归属的角色名
        self.retry = retry              # 第几次尝试，供重试样例区分
        self.tool_outputs = []          # 工具原始返回值，供数字回查（W0-05）取证据
        self.tool_records = []          # [{name, output}]，供 Critic 看"哪个工具返回了什么"
        self._open = {}                 # run_id -> Step（已开始未结束）
        self._lock = threading.Lock()

    # ---------- 内部 ----------

    def _agent_of(self, metadata, tags):
        """允许调用方用 tag 'agent:Critic' 或 metadata.agent_name 覆盖角色名。"""
        for tag in tags or []:
            if isinstance(tag, str) and tag.startswith("agent:"):
                return tag.split(":", 1)[1].strip() or self.agent
        if isinstance(metadata, dict) and metadata.get("agent_name"):
            return str(metadata["agent_name"])
        return self.agent

    def _pop(self, run_id):
        with self._lock:
            return self._open.pop(run_id, None)

    def _safe(self, fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception:               # noqa: BLE001 —— 埋点失败绝不冒泡
            pass

    # ---------- 模型调用 ----------

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None,
                            tags=None, metadata=None, **kwargs):
        # 嵌套调用要去重（台账 W2-02）。降级模型内部会挨个试通道，
        # 而 LangChain 的回调是走 contextvars 往下传的 —— 即使内层调用
        # 不显式传 config，它也会继承外层的 callbacks，于是"一次调用"
        # 会记成两条 chat_model（一条失败、一条成功），token 也跟着翻倍。
        # 内层调用会带上 _skip_trace 标记，这里直接不记账。
        if isinstance(metadata, dict) and metadata.get("_skip_trace"):
            return

        def _do():
            batch = messages[0] if messages else []
            last = _msg_text(batch[-1]) if batch else ""
            preview, cut = _short(last, 500)
            step = self.trace.begin(
                self._agent_of(metadata, tags),
                "chat_model",
                input_data={"messages": len(batch), "last": preview},
                retry=self.retry,
            )
            step.meta = {"kind": KIND_LLM, "model": (serialized or {}).get("name") or ""}
            if cut:
                step.meta["input_truncated"] = True
            with self._lock:
                self._open[run_id] = step
        self._safe(_do)

    def on_llm_end(self, response, *, run_id, parent_run_id=None, tags=None, **kwargs):
        def _do():
            step = self._pop(run_id)
            if step is None:
                return
            step.tokens_in, step.tokens_out = _usage(response)
            text = ""
            try:
                text = _msg_text(response.generations[0][0].message)
            except (AttributeError, IndexError, TypeError):
                pass
            shown, cut = _short(text, 500)
            if cut:
                step.meta["output_truncated"] = True
            self.trace.end(step, output=shown)
        self._safe(_do)

    def on_llm_error(self, error, *, run_id, parent_run_id=None, tags=None, **kwargs):
        def _do():
            step = self._pop(run_id)
            if step is None:
                return
            self.trace.end(step, status=STATUS_FAILED, error=error)
        self._safe(_do)

    # ---------- 工具调用 ----------

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None,
                      tags=None, metadata=None, inputs=None, **kwargs):
        def _do():
            name = (serialized or {}).get("name") or "unknown_tool"
            payload = _parse_input(inputs if isinstance(inputs, dict) else input_str)
            step = self.trace.begin(self._agent_of(metadata, tags), name,
                                    input_data=payload, retry=self.retry)
            step.meta = {"kind": KIND_TOOL}
            with self._lock:
                self._open[run_id] = step
        self._safe(_do)

    def on_tool_end(self, output, *, run_id, parent_run_id=None, tags=None, **kwargs):
        def _do():
            step = self._pop(run_id)
            if step is None:
                return
            raw = getattr(output, "content", output)
            # 存原始值（不截断）供数字回查当证据：截断可能刚好切掉要核对的那个数
            self.tool_outputs.append(raw)
            self.tool_records.append({"name": step.name, "output": raw})
            shown, cut = _short(raw)
            if cut:
                step.meta["output_truncated"] = True
            self.trace.end(step, output=shown)
        self._safe(_do)

    def on_tool_error(self, error, *, run_id, parent_run_id=None, tags=None, **kwargs):
        def _do():
            step = self._pop(run_id)
            if step is None:
                return
            self.trace.end(step, status=STATUS_FAILED, error=error)
        self._safe(_do)


def attach(trace, agent="Analyst"):
    """便捷函数：返回可直接放进 config["callbacks"] 的列表。"""
    return [TraceCallbackHandler(trace, agent=agent)]


__all__ = ["TraceCallbackHandler", "attach"]
