# -*- coding: utf-8 -*-
"""
预算护栏 —— 台账 W2-04

一次 run 烧掉多少 token、叫了几次模型，必须有上限。理由不是省钱好看，
而是**成本失控时的行为要可预期**：没有上限时，一个打回循环卡在质检上能
把预算烧到任意大，而且中途没有任何一句话告诉用户"它已经不划算了"。

三条口径（与台账验收对齐）：
  1. 上限外置在 config/team.json 的 budget 段，改配置即生效，不改代码；
  2. 触顶即停 —— 不是等这一轮跑完再判，是下一个节点开跑前就拦住；
  3. 停下来要说人话：已有结论照常交付，另附一句"因预算上限中止"，
     并把 aborted=budget 写进 trace 的 run_end —— 排障与回归都能筛出来。

为什么不把它做成异常：中止不是故障。抛异常会流进 NodeFailed / failed 分支，
报告里看着像"跑挂了"，实际是"主动停车"，两种事的处置完全不同。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

# 与 max_retry 同住一份团队策略：都是"这个班组怎么干活"的参数
TEAM_POLICY = Path(__file__).resolve().parent.parent / "config" / "team.json"

# 默认值刻意宽于正常用量（一道题实测 2,700~3,400 token / 4~6 次调用）：
# 护栏是防失控的，不是掐着脖子算账。真要收紧就改配置。
DEFAULT_MAX_TOKENS = 120000
DEFAULT_MAX_CALLS = 24

__all__ = ["Budget", "load_budget", "DEFAULT_MAX_TOKENS", "DEFAULT_MAX_CALLS"]


class Budget:
    """一次 run 的预算。check() 返回 None 表示还能跑，返回 dict 表示该停了。"""

    def __init__(self, max_tokens: Optional[int] = None,
                 max_calls: Optional[int] = None):
        # None / 0 / 负数都当"不设上限"，免得配置写错就把系统锁死
        self.max_tokens = int(max_tokens) if max_tokens and int(max_tokens) > 0 else 0
        self.max_calls = int(max_calls) if max_calls and int(max_calls) > 0 else 0

    def enabled(self) -> bool:
        return bool(self.max_tokens or self.max_calls)

    def describe(self) -> str:
        parts = []
        if self.max_tokens:
            parts.append("%s token" % "{:,}".format(self.max_tokens))
        if self.max_calls:
            parts.append("%d 次模型调用" % self.max_calls)
        return " · ".join(parts) if parts else "未设上限"

    def check(self, trace) -> Optional[Dict]:
        """下一个节点开跑前问一句：还能不能跑。

        优先判 token（更贵），再判调用次数。两个都超了也只报最先撞上的那个，
        理由写清楚 —— 报告里"超了 token"和"叫太多次"是两种不同的病。
        """
        if trace is None or not self.enabled():
            return None

        if self.max_tokens:
            used = trace.tokens_used()
            if used >= self.max_tokens:
                return {"kind": "budget", "limit": "tokens", "used": used,
                        "cap": self.max_tokens,
                        "why": "token 已用 %s，达到上限 %s" % (
                            "{:,}".format(used), "{:,}".format(self.max_tokens))}

        if self.max_calls:
            calls = trace.calls_made()
            if calls >= self.max_calls:
                return {"kind": "budget", "limit": "calls", "used": calls,
                        "cap": self.max_calls,
                        "why": "模型调用已 %d 次，达到上限 %d 次" % (calls, self.max_calls)}
        return None


def load_budget(path=None) -> Budget:
    """读 config/team.json 的 budget 段。

    文件不存在 / 段缺失 / 写坏了：一律退回默认上限，不让配置瑕疵把整条链路打死。
    但会把原因留在 Budget.note 里，自检脚本能指出"你这段配错了"。
    """
    p = Path(path) if path else TEAM_POLICY
    note = ""
    cfg = {}
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:                        # noqa: BLE001
            note = "team.json 解析失败（%s），已退回默认预算" % type(exc).__name__
    else:
        note = "找不到 %s，已退回默认预算" % p.name

    b = (cfg.get("budget") or {}) if isinstance(cfg, dict) else {}
    # 用"键是否存在"而不是 `or 默认值`：写 0 是「这条不设限」，
    # 而 0 是 falsy —— 用 or 会把人的明确意图悄悄替换成默认上限。
    budget = Budget(b["max_tokens"] if "max_tokens" in b else DEFAULT_MAX_TOKENS,
                    b["max_calls"] if "max_calls" in b else DEFAULT_MAX_CALLS)
    budget.note = note
    return budget
