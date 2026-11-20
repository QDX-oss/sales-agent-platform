"""诊断式 Self-Loop（台账 W2-03）。

重试有两种写法，差别不在次数，在**第二轮做了什么**：

  重发式：把同一个问题再问一遍，顶多加一句"请再检查一下"。
          能不能改好，取决于模型这次心情好不好。写不出断言，也没法解释。
  诊断式：先判"上一稿错在哪一类"，再按类换一种做法。
          取数错了就重新取，口径错了就退回上一步核口径，
          表达错了就只改表达、一个数字都不许动。

这个模块只做判断，不执行。它拿到的是三道关卡留下的痕迹：
回查结论（哪些数字没出处）、质检意见（答非所问 / 越界 / 口径混用）、
以及本轮到底调用了几次工具。据此给出一份 Diagnosis ——
里面写清楚：判成哪一类、凭什么这么判、下一轮具体要改什么、什么不许动。

刻意的取舍：分类规则是**确定性**的，不看模型怎么说自己错在哪。
模型对自己的失败归因极不可靠（它会说"我表达不够清晰"，而实际是没取数）。
规则错了可以改规则、可以写测试；模型自我归因错了只能重跑。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

__all__ = [
    "KIND_NO_DATA", "KIND_FETCH", "KIND_METRIC", "KIND_EXPRESSION", "KIND_UNKNOWN",
    "Diagnosis", "classify", "KIND_LABEL",
]

KIND_NO_DATA = "no_data"          # 数据里根本没有 / 一个工具都没调通
KIND_FETCH = "fetch"              # 取数错：取到的数与结论对不上，或该取没取
KIND_METRIC = "metric"            # 口径错：销售额/利润/利润率/销量 用错或单位错
KIND_EXPRESSION = "expression"    # 表达错：数字都对，但答非所问 / 越界 / 说不清楚
KIND_UNKNOWN = "unknown"          # 判断不出来，保守处理

KIND_LABEL = {
    KIND_NO_DATA: "无数据",
    KIND_FETCH: "取数错",
    KIND_METRIC: "口径错",
    KIND_EXPRESSION: "表达错",
    KIND_UNKNOWN: "未能归类",
}

# 判定"这一稿在说没有数据"的措辞。宁可多判一次 no_data（它会要求说明查询范围），
# 也不要把它误判成"取数错"然后催着模型再编一版。
_NO_DATA_WORDS = ("没有找到", "未找到", "没有符合", "无符合", "查不到", "无数据",
                  "暂无数据", "没有相关", "数据不足", "没有记录")


@dataclass
class Diagnosis:
    """一次失败诊断的结论。字段就是编排屏和测试要读的全部内容。"""

    kind: str
    strategy: str
    why: str                          # 一句话：凭什么这么判
    signals: List[str] = field(default_factory=list)   # 具体证据条目
    instruction: str = ""             # 追加给 Analyst 的本轮指令
    forbid: str = ""                  # 本轮明令禁止的事
    attempt: int = 0

    @property
    def label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)

    def block(self) -> str:
        """拼成要塞进 prompt 的那一段。两轮之间"实质差异"就长在这里。"""
        lines = ["【第 %d 次重做 · 诊断：%s】" % (self.attempt, self.label),
                 "判定依据：%s" % self.why]
        for s in self.signals[:4]:
            lines.append("  · %s" % s)
        lines.append("本轮做法：%s" % self.instruction)
        if self.forbid:
            lines.append("本轮禁止：%s" % self.forbid)
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "strategy": self.strategy,
            "why": self.why,
            "signals": list(self.signals),
            "instruction": self.instruction,
            "forbid": self.forbid,
            "attempt": self.attempt,
        }


# ---------------------------------------------------------------- 各类型的处置

def _metric(why: str, signals: List[str], attempt: int) -> Diagnosis:
    # 口径定义行来自行业配置（台账 W4-04）：打回时告诉模型的"按什么定义
    # 重算"必须是这个行业的口径，销售说利润率、客服说一次解决率。
    try:
        from core.industry import current
        glossary = ((current()[1].get("agent") or {}).get("metrics_glossary") or "").strip()
    except Exception:                              # noqa: BLE001
        glossary = ""
    if not glossary:
        glossary = ("销售额 = 金额合计；利润 = 利润合计；"
                    "利润率 = 利润 ÷ 销售额 × 100（百分数，保留两位）；销量 = 件数。")
    return Diagnosis(
        kind=KIND_METRIC, strategy="recheck_caliber", why=why,
        signals=signals, attempt=attempt,
        instruction=(
            "退回口径这一步核对，不要直接换个数糊过去。先确认这些量的定义："
            + glossary +
            " 找出上一稿里被用错或互换的那一个，按定义重算后改写那一句。"
        ),
        forbid="不许改动与口径无关的数字，也不许把百分数写成小数或反过来。",
    )


def _fetch(why: str, signals: List[str], attempt: int) -> Diagnosis:
    return Diagnosis(
        kind=KIND_FETCH, strategy="refetch", why=why,
        signals=signals, attempt=attempt,
        instruction=(
            "上一轮的取数与结论对不上，本轮必须重新调用查询工具："
            "换一个条件重取（例如换维度、换时间范围、换聚合方式），"
            "拿新返回的数字重写结论。不许沿用上一轮的任何数字。"
        ),
        forbid="不许凭记忆复述上一轮的结果；没有工具返回的数字一律不许出现在回答里。",
    )


def _no_data(why: str, signals: List[str], attempt: int) -> Diagnosis:
    return Diagnosis(
        kind=KIND_NO_DATA, strategy="state_scope", why=why,
        signals=signals, attempt=attempt,
        instruction=(
            "不要再尝试编一个数出来。明确说明：在什么范围（时间 / 地区 / 产品 / 渠道）内查询过、"
            "结果为空或不足以回答，并请用户确认要看哪个范围或哪个字段。"
        ),
        forbid="不许估算、不许用「大约」「左右」给出没有出处的数字。",
    )


def _expression(why: str, signals: List[str], attempt: int) -> Diagnosis:
    return Diagnosis(
        kind=KIND_EXPRESSION, strategy="rewrite_only", why=why,
        signals=signals, attempt=attempt,
        instruction=(
            "数字全部有效，本轮只改表达：第一句直接回答用户问的那一件事，"
            "删掉与问题无关的展开，删掉任何承诺、建议、保证类措辞。"
        ),
        forbid="一个数字都不许改，包括单位、小数位和百分比写法。",
    )


def _unknown(why: str, signals: List[str], attempt: int) -> Diagnosis:
    return Diagnosis(
        kind=KIND_UNKNOWN, strategy="redo_with_notes", why=why,
        signals=signals, attempt=attempt,
        instruction="逐条对照下面的回查与质检意见修改，改完自查一遍再交。",
        forbid="不许只改措辞而不改被指出的那处事实。",
    )


# ---------------------------------------------------------------- 判据

def _critic_types(critique: Optional[dict]) -> List[str]:
    return [str(i.get("type") or "") for i in (critique or {}).get("issues") or []]


def _high_issues(critique: Optional[dict]) -> List[dict]:
    return [i for i in (critique or {}).get("issues") or []
            if i.get("severity") == "high"]


def _has_type(critique: Optional[dict], *words: str) -> bool:
    for t in _critic_types(critique):
        for w in words:
            if w in t:
                return True
    return False


def _looks_empty(draft: str) -> bool:
    return any(w in (draft or "") for w in _NO_DATA_WORDS)


def _unit_shift(value: Any, evidence: List[float]) -> Optional[float]:
    """这个数是不是某个证据数字放大/缩小 100 倍得来的（百分数 vs 小数、元 vs 万元）。

    只查 100 倍，不查 10000 倍：万元混淆在这个数据集里没出现过，
    多一条规则就多一种误判，等真出现再加。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v == 0:
        return None
    for e in evidence:
        if e in (0, 1):
            continue
        for scale in (100.0, 0.01):
            target = e * scale
            tol = max(abs(target) * 0.005, 1e-9)
            if abs(v - target) <= tol:
                return e
    return None


def classify(draft: str, verdict: Optional[dict], critique: Optional[dict],
             records: Optional[List[dict]], evidence: Optional[List[float]],
             attempt: int = 1, timed_out: Optional[List[str]] = None) -> Diagnosis:
    """给这一轮失败定性。传入的都是关卡留下的痕迹，不需要再问模型。"""
    verdict = verdict or {}
    records = records or []
    evidence = list(evidence or [])
    unsupported = list(verdict.get("unsupported") or [])
    signals: List[str] = []

    # 1. 质检明确说是口径问题 —— 这是最具体的一条意见，优先采信
    if _has_type(critique, "口径", "单位"):
        for i in _high_issues(critique):
            if "口径" in str(i.get("type")) or "单位" in str(i.get("type")):
                signals.append("质检：%s" % (i.get("detail") or i.get("type")))
        return _metric("质检员指出了口径/单位问题，这一类换个数修不好，必须退回重算",
                       signals, attempt)

    # 2. 通篇在说"没查到"，而且确实没有证据 —— 再问一遍也变不出数据
    if _looks_empty(draft) and not evidence:
        signals.append("初稿写明查不到/数据不足")
        signals.append("本轮工具返回的证据数为 0")
        return _no_data("数据里确实没有符合条件的记录，重复提问不会变出数据", signals, attempt)

    # 3. 数字与证据差 100 倍 —— 典型的百分数/小数、元/万元混用
    for item in unsupported:
        hit = _unit_shift(item.get("value"), evidence)
        if hit is not None:
            signals.append("「%s」与证据里的 %s 相差 100 倍" % (item.get("raw"), _fmt(hit)))
            return _metric("有一个数字恰好是某个证据值的 100 倍/百分之一，是单位或百分数用错了",
                           signals, attempt)

    # 3.5 质检直接指出"无据数字" —— 即使规则回查放过了，也要重新取数。
    # 这种情况真实存在：回查的白名单会放过年份、序号一类，模型可能借着
    # 一个像年份的数字把结论也编进去，只有 Critic 看得出来。
    if _has_type(critique, "无据"):
        for i in _high_issues(critique):
            if "无据" in str(i.get("type")):
                signals.append("质检：%s" % (i.get("detail") or i.get("type")))
        return _fetch("质检指出回答里有找不到出处的数字，这一条只能靠重新取数解决",
                      signals, attempt)

    # 4. 根本没取数，或者取了个空 —— 该做的是重新取
    if not records:
        signals.append("本轮没有任何工具调用记录")
        return _fetch("初稿没有经过任何取数就给出了结论", signals, attempt)
    if not evidence:
        signals.append("调用了 %d 次工具，但没有拿到任何可用数字" % len(records))
        return _fetch("取数动作发生了，但没有拿到能支撑结论的数字", signals, attempt)

    # 5. 取过数了，但结论里的数对不上 —— 取错了切片或算错了
    if unsupported:
        for item in unsupported[:3]:
            signals.append("「%s」不在本次工具返回里" % item.get("raw"))
        signals.append("证据池共 %d 个数字" % len(evidence))
        return _fetch("工具返回的数字与初稿里的数字对不上，说明取的不是同一批数据",
                      signals, attempt)

    # 6. 数字全过、质检仍打回 —— 只剩表达问题
    if not verdict.get("passed", True) or _high_issues(critique):
        for i in _high_issues(critique)[:3]:
            signals.append("质检：%s（%s）" % (i.get("detail") or "", i.get("type")))
        return _expression("所有数字都能对上出处，问题出在答非所问或越界表述",
                           signals, attempt)

    if timed_out:
        signals.append("超时节点：%s" % "、".join(timed_out))
    return _unknown("两类关卡都没有给出可归类的具体问题", signals, attempt)


def _fmt(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return "%d" % round(v)
    return ("%.4f" % v).rstrip("0").rstrip(".")


def handoff_text(diagnoses: List[dict], attempts: int, last_kind: str) -> str:
    """重试用尽时贴在答案前面的那段话。

    不能只说"失败了"：要说明已经试过几种做法、分别卡在哪，
    否则接手的人得从头再走一遍。
    """
    kinds = []
    for d in diagnoses:
        label = d.get("label") or d.get("kind")
        if label and label not in kinds:
            kinds.append(label)
    tried = "、".join(kinds) if kinds else "未能归类"
    return ("【需人工复核】自动重试 %d 次仍未通过质检，已停止自动修改。"
            "试过的方向：%s。以下内容未经最终确认，请人工核对后再使用。"
            % (attempts, tried))
