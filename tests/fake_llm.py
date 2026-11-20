# -*- coding: utf-8 -*-
"""
测试用假模型 —— 不联网、不花钱、结果确定。

为什么需要它：
验证"埋点有没有挂上"这件事，不应该依赖 DeepSeek 的可用性和钱包。
这里用一个按剧本回话的假模型替换真模型，其余环节（create_agent 的图、
真实的 query_stats 工具、真实的 CSV、真实的回调链）全部保持原样。
这样如果 trace 里能看到工具调用，就能确定是回调机制在工作，
而不是碰巧。

剧本里放的 token 用量是编的，只用来验证"能被读出来"。
"""

from typing import Any, List, Optional, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


class ScriptedChatModel(BaseChatModel):
    """按顺序吐出预设消息的聊天模型，用完后一直重复最后一条。"""

    script: List[AIMessage]
    calls: int = 0
    # 每次调用收到的 messages 都留一份：诊断式重试要证明"第二轮的 prompt
    # 与第一轮有实质差异"，光看剧本对不出来 —— 剧本是模型的输出，不是输入。
    seen: List[List[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedChatModel":
        # create_agent 会先 bind_tools 再调用；假模型不需要真的绑定
        return self

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        self.seen.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self.script[index])])


def two_turn_script(province: str = "广东", answer: str = "",
                    tool_name: str = "query_stats") -> List[AIMessage]:
    """标准两轮剧本：先调工具，再给结论。

    第二轮的内容留空时，由调用方按工具真实返回的数字填写，
    保证"答案里说的数"与"工具返回的数"一致 —— 这是后面做数字回查的前提。
    """
    return [
        AIMessage(
            content="",
            tool_calls=[{
                "name": tool_name,
                "args": {"province": province},
                "id": "call_fake_1",
                "type": "tool_call",
            }],
            usage_metadata={"input_tokens": 486, "output_tokens": 31, "total_tokens": 517},
        ),
        AIMessage(
            content=answer or "（占位答案）",
            usage_metadata={"input_tokens": 702, "output_tokens": 88, "total_tokens": 790},
        ),
    ]
