# -*- coding: utf-8 -*-
"""
数字回查（verifier）—— 台账 W0-05

要解决的问题：模型可能把数说错。而"说错了"这件事，靠提示词写
「禁止编造数据」是拦不住的——提示词是请求，不是约束。

做法：回答里出现的每一个有实质含义的数字，都必须能在**本次**运行的
工具返回值里找到出处。找不到就说明它是编的、或是从别处记来的。

三个必须处理的难点（不处理就会天天误报，最后没人信它）：

1. 换算与舍入。工具返回 1235074.0，人会说「123.5 万」。直接比不相等。
   → 按"写出来的最后一位"推算容差：123.5 万 的容差是 500，
     差 74 在容差内，通过；说「123 万」差 5074，容差 5000 外，不通过。

2. 派生值。模型自己算利润率 = 利润/销售额×100，这个数工具没返回，
   但它是从两个真实数字算出来的，不该被判成编造。
   → 允许"由两个证据数字四则运算得到"的数字，并记录它是怎么算出来的。

3. 非数据数字。2025 年 11 月、第 3 季度、前 10 名、列表序号 1.
   这些不是数据声明，比对它们只会制造噪声。
   → 白名单，但每条忽略都要给出理由，便于复核。

本模块不依赖 flask / langchain，可单独测试。
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 中文数量单位 → 倍数
UNITS = {"万亿": 1e12, "亿": 1e8, "万": 1e4, "千": 1e3, "%": 1, "％": 1}

# 数字：支持千分位、小数、正负号、可选中文单位和百分号
NUM_RE = re.compile(
    r"(?<![\d.])"
    r"(?P<num>-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)"
    r"\s*(?P<unit>万亿|亿|万|千|%|％)?"
)

# 数字后面跟这些字，说明它在表达时间而不是数据
DATE_SUFFIX = ("年", "月", "日", "号", "时", "分", "秒", "季度", "周", "星期")

# 数字后面跟这些字，进一步确认是序号（配合前面的 "第" / "前"）
ORDINAL_SUFFIX = ("名", "位", "季度", "周", "月", "页", "条", "章", "节", "步")

# 单次比对允许做两两运算的基数上限；超过就放弃派生判断，避免 O(n²) 拖慢请求
MAX_DERIVED_BASE = 60


@dataclass
class Claim:
    """回答里的一处数字声明。"""

    raw: str                 # 原样文本，例如 "123.5 万"
    value: float             # 归一化后的值，例如 1235000.0
    tolerance: float         # 允许的偏差，由书写精度决定
    start: int = 0
    end: int = 0
    kind: str = "number"     # number / percent
    ignored: str = ""        # 非空表示被白名单忽略，值为理由

    def to_dict(self):
        return {"raw": self.raw, "value": self.value, "kind": self.kind,
                "ignored": self.ignored or None}


@dataclass
class Verdict:
    """回查结论。"""

    passed: bool
    checked: int                              # 参与比对的数字个数
    claims: List[Dict[str, Any]] = field(default_factory=list)
    unsupported: List[Dict[str, Any]] = field(default_factory=list)
    supported: List[Dict[str, Any]] = field(default_factory=list)
    ignored: List[Dict[str, Any]] = field(default_factory=list)
    evidence_count: int = 0
    note: str = ""

    def to_dict(self):
        return {
            "passed": self.passed,
            "checked": self.checked,
            "supported": self.supported,
            "unsupported": self.unsupported,
            "ignored": self.ignored,
            "evidence_count": self.evidence_count,
            "note": self.note,
        }

    def feedback(self):
        """给模型的纠错要求。要具体到数字，否则它不知道改哪。"""
        if self.passed:
            return ""
        parts = []
        for item in self.unsupported[:5]:
            nearest = item.get("nearest")
            hint = ""
            if nearest is not None:
                hint = "（本次工具返回过最接近的是 %s）" % _pretty(nearest)
            parts.append("「%s」%s" % (item["raw"], hint))
        return (
            "你上一条回答里有数字在本次工具返回结果中找不到出处："
            + "；".join(parts)
            + "。请只使用工具真实返回的数值重新作答；"
              "如果某个数是你自己算出来的，请先调用工具拿到原始数据再计算，"
              "并在文中说明它由哪两个数算出。不要保留任何无法溯源的数字。"
        )


def _pretty(value):
    """数字回显：整数不带小数点，大数加千分位。"""
    try:
        if abs(value - round(value)) < 1e-9:
            return "{:,}".format(int(round(value)))
        return "{:,.2f}".format(value)
    except (TypeError, ValueError):
        return str(value)


def _tolerance(digits_frac, unit_mult, value):
    """由书写精度推容差。

    "123.5 万" 精确到 0.1 万，容差取半个最小刻度 = 0.05 万 = 500。
    大数再叠加 0.1% 的松散量，容忍四舍五入到有效数字的写法，
    但不容忍把 123.5 万写成 123 万（那丢了 5000 的信息）。
    """
    step = 10 ** (-digits_frac) if digits_frac else 1
    base = step * unit_mult / 2.0
    loose = abs(value) * 1e-3
    return max(base, loose)


def _whitelist_reason(text, start, end, value, unit):
    """判断这个数字是否与数据无关，返回理由（空字符串表示需要核对）。"""
    before = text[:start]
    prev = before[-1] if before else ""          # 紧邻的前一个字符
    prev_word = before.rstrip()[-1:]             # 跳过空格后的前一个字（"前 10 名"）
    prev_two = before.rstrip()[-2:]
    after = text[end:end + 4]
    after_trim = after.lstrip()

    if unit in ("%", "％"):
        return ""

    # 时间：2025 年、11 月、5 日
    for suffix in DATE_SUFFIX:
        if after_trim.startswith(suffix):
            return "时间"

    # 纯年份
    if not unit and 1900 <= value <= 2100 and abs(value - round(value)) < 1e-9:
        return "年份"

    # 序号：第 3 季度、前 10 名
    if prev_word in ("第", "前"):
        if (not after_trim
                or any(after_trim.startswith(s) for s in ORDINAL_SUFFIX)
                or after_trim[0] in "、，,。；;"):
            return "序号"

    # 列表序号：行首 / 句末 / 顿号之后的 "1." "2、"
    if (prev == "" or prev in "\n。！？；;、，,：:") and after_trim[:1] in (".", "、", ")", "）"):
        return "列表序号"

    # 版本号 / 型号
    if after_trim.startswith("版") or prev_word in ("v", "V") or prev_two.endswith(("v", "V")):
        return "版本号"

    return ""


def extract_claims(text: str) -> List[Claim]:
    """把一段回答拆成数字声明列表（含被白名单忽略的）。"""
    claims = []
    for match in NUM_RE.finditer(text or ""):
        raw_num = match.group("num")
        unit = match.group("unit") or ""
        frac = raw_num.split(".")[1] if "." in raw_num else ""
        try:
            value = float(raw_num.replace(",", ""))
        except ValueError:
            continue

        if unit in UNITS:
            value *= UNITS[unit]

        claim = Claim(
            raw=match.group(0).strip(),
            value=value,
            tolerance=_tolerance(len(frac), UNITS.get(unit, 1), value),
            start=match.start(),
            end=match.end(),
            kind="percent" if unit in ("%", "％") else "number",
        )
        claim.ignored = _whitelist_reason(text, claim.start, claim.end, value, unit)
        claims.append(claim)
    return claims


def collect_numbers(obj) -> List[float]:
    """从工具返回值里递归收集所有数字（含 JSON 字符串里的）。"""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)
        elif isinstance(node, bool):
            return
        elif isinstance(node, (int, float)):
            found.append(float(node))
        elif isinstance(node, str):
            text = node.strip()
            if text[:1] in "{[":
                try:
                    walk(json.loads(text))
                    return
                except (json.JSONDecodeError, ValueError):
                    pass
            # 纯文本里夹带的数字也收进来（工具不一定返回 JSON）
            for match in NUM_RE.finditer(node):
                try:
                    value = float(match.group("num").replace(",", ""))
                except ValueError:
                    continue
                found.append(value * UNITS.get(match.group("unit") or "", 1))

    walk(obj)
    return found


def evidence_from(tool_outputs) -> List[float]:
    """把本次运行的所有工具返回值变成证据数字集合。"""
    numbers = []
    for output in tool_outputs or []:
        numbers.extend(collect_numbers(output))
    return numbers


def _derived(value, evidence, tolerance, limit=MAX_DERIVED_BASE):
    """判断 value 能否由两个证据数字**相除**得到。

    只承认比值与百分比两类派生：它们是叙事里最常出现的自算量
    （利润率、占比、增长率的分母部分）。

    不承认加减法，这是一个用实测换来的决定：
    差值与求和的容差要求与"直接匹配"高度重叠，会放过真正的错误——
    实测中「把 1235074 说成 123 万」就被一次无关的减法蒙混过关
    （1235074 - 4064 = 1231010，落在 123 万的容差内）。
    宁可漏放少数合法的差值表述，也不能让编造的数字混进来。
    """
    base = [n for n in evidence if n not in (0, 1)][:limit]
    for a in base:
        for b in base:
            if a == b:
                continue
            for result, label in (
                (a / b, "%s / %s" % (_pretty(a), _pretty(b))),
                (a / b * 100, "%s / %s x 100" % (_pretty(a), _pretty(b))),
            ):
                if abs(result - value) <= max(tolerance, abs(result) * 1e-3):
                    return label
    return None


def verify(answer: str, evidence, note="") -> Verdict:
    """核心入口：回答 vs 本次工具返回值。"""
    claims = extract_claims(answer)
    pool = [float(n) for n in evidence or []]

    supported, unsupported, ignored = [], [], []
    for claim in claims:
        item = claim.to_dict()
        if claim.ignored:
            ignored.append(item)
            continue

        hit = None
        for number in pool:
            if abs(number - claim.value) <= claim.tolerance:
                hit = number
                break
        if hit is not None:
            item["matched"] = hit
            supported.append(item)
            continue

        formula = _derived(claim.value, pool, claim.tolerance)
        if formula:
            item["derived"] = formula
            supported.append(item)
            continue

        item["nearest"] = min(pool, key=lambda n: abs(n - claim.value)) if pool else None
        unsupported.append(item)

    checked = len(supported) + len(unsupported)
    return Verdict(
        passed=not unsupported,
        checked=checked,
        claims=[c.to_dict() for c in claims],
        supported=supported,
        unsupported=unsupported,
        ignored=ignored,
        evidence_count=len(pool),
        note=note,
    )


__all__ = [
    "Claim", "Verdict", "extract_claims", "collect_numbers",
    "evidence_from", "verify", "MAX_DERIVED_BASE",
]
