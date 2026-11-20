# -*- coding: utf-8 -*-
"""Eval Set 判定器（台账 W4-01）。

判定只做三件事：精确值 / 必须包含 / 必须拒答，外加 either 组合器给
"口径歧义"这种答对或诚实拒答都算过的题。

为什么不拿模型来当裁判（LLM-as-judge）：裁判自己也会错，而且错的理由
说不清。这里的每一条判定都是规则，错了能对着答案一句句指出来 ——
回归报告里每道题都要写"为什么判不过"，规则才写得出来。
"""
import json
import re
from pathlib import Path

import yaml

EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"

# 拒答信号：出现其中一个就算"拒答了"。
# 刻意挑死板的词，不挑"恐怕""可能"这种软话 —— 软话拒答不算拒答。
REFUSAL_WORDS = ("没有", "无法", "不存在", "无数据", "查不到", "数据不足",
                 "缺少", "不支持", "找不到", "不包含", "无法计算")

# 编造检测：拒答判定里，答案若出现"位数很大的数字"（6 位以上，销售额量级）
# 就大概率是在编 —— 诚实的拒答不会带出一个具体的大金额。
_FABRICATE_RE = re.compile(r"\d{6,}(?:\.\d+)?")
# 数字抽取：容忍千分位逗号与单位后缀（元 / 件 / %）
_NUM_RE = re.compile(r"(\d[\d,]*)(?:\.(\d+))?")


def load_cases(path=None):
    """读用例集。文件不存在或格式坏了都如实报错，不返回空列表装没事。

    两组一起读（台账 W4-03）：
      cases       人工钉死的固定题
      regression  失败回流进来的题（每次回归都必测）

    回流题自带的 group 会被标成 regression，上层据此区分"这题是卷子原本就有的"
    还是"线上/回归里挂过、被收进来的"。顺序保持文件里的顺序 —— 报告要能对着
    卷子一行行看，打乱顺序只会让人对不上账。
    """
    p = Path(path) if path else (EVALS_DIR / "cases.yaml")
    if not p.exists():
        raise FileNotFoundError("找不到用例集：%s" % p)
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    cases = list(doc.get("cases") or [])
    for c in cases:
        c.setdefault("group", "main")
    for c in (doc.get("regression") or []):
        c["group"] = "regression"
        c.setdefault("difficulty", "regression")
    cases.extend(doc.get("regression") or [])
    if not cases:
        raise ValueError("用例集是空的：%s" % p)
    return doc, cases


def _extract_numbers(text):
    """从答案里抽数字：'1,235,074.0 元' → 1235074.0。"""
    out = []
    for whole, frac in _NUM_RE.findall(text or ""):
        try:
            out.append(float(("%s.%s" % (whole.replace(",", ""), frac or "0"))))
        except ValueError:
            continue
    return out


def _check_exact(case, answer):
    """精确值：答案文本里要能找到这个数（容忍逗号与小数末尾差异）。

    用「答案中的数字里有没有它」而不是「字符串里含不含有它」——
    后者会被"1,235,074"这种带逗号的写法漏掉。
    """
    want = float(case["value"])
    got = _extract_numbers(answer)
    hit = any(abs(x - want) < 0.01 for x in got)
    return hit, ("找到 %s" % want if hit
                 else "答案里的数字是 %s，不含预期 %s" % (got[:6], want))


def _check_contain(case, answer):
    """必须包含：关键词全部/任一出现，大小写不敏感。"""
    mode = (case.get("mode") or "all").lower()
    kws = [str(k) for k in (case.get("keywords") or [])]
    if not kws:
        return False, "用例配置错误：keywords 是空的"
    hit = [k for k in kws if k in (answer or "")]
    ok = (len(hit) == len(kws)) if mode == "all" else bool(hit)
    miss = [k for k in kws if k not in hit]
    return ok, ("命中 %s" % hit if ok else
                "缺关键词 %s（%s 模式）" % (miss, mode))


def _check_refuse(case, answer):
    """必须拒答：有拒答信号，且没有编造一个销售额量级的大数出来。"""
    text = answer or ""
    said_no = any(w in text for w in REFUSAL_WORDS)
    if not said_no:
        return False, "没发现拒答信号（%s 任一）" % "、".join(REFUSAL_WORDS[:5])
    fabricated = _FABRICATE_RE.findall(text.replace(",", ""))
    if fabricated:
        # 拒答的话里复述"2026"这种年份没事，6 位以上才是金额量级
        return False, "嘴上说没有，但答案里出现了 %s 这样的大数字，疑似编造" % fabricated[0]
    return True, "明确拒答且未编造数字"


_CHECKERS = {
    "exact": _check_exact,
    "must_contain": _check_contain,
    "must_refuse": _check_refuse,
}


def check_case(case, answer):
    """判定一道题。返回 {ok, reason, expect_type}，判定过程全部写进 reason。"""
    exp = case.get("expect") or {}
    etype = exp.get("type")
    if etype == "pending":
        # 回流题（W4-03）刚收进来时还没有预期答案 —— 不猜、不判分。
        # 判它"挂"是冤枉系统（口径没定），判它"过"是自欺（不知道对错）。
        # 所以单独标出来：跑，但不进分母；补上 expect 之后它就是普通题。
        return {"ok": False, "reason": "待定口径：尚未给预期答案（补上 expect 后参与判分）",
                "expect_type": "pending", "pending": True}
    if etype == "either":
        # 组合器：任一子判定通过即过（给"答对或诚实拒答都算分"的题用）
        reasons = []
        for sub in exp.get("options") or []:
            fn = _CHECKERS.get(sub.get("type"))
            if fn is None:
                reasons.append("未知判定类型 %s" % sub.get("type"))
                continue
            ok, why = fn(sub, answer)
            reasons.append("[%s] %s" % (sub.get("type"), why))
            if ok:
                return {"ok": True, "reason": "；".join(reasons),
                        "expect_type": "either"}
        return {"ok": False, "reason": "；".join(reasons), "expect_type": "either"}
    fn = _CHECKERS.get(etype)
    if fn is None:
        return {"ok": False, "reason": "未知判定类型 %s" % etype, "expect_type": etype}
    ok, why = fn(exp, answer)
    return {"ok": ok, "reason": why, "expect_type": etype}


def run_eval(cases, answers):
    """answers: {case_id: answer_text} → 逐题判定 + 汇总。

    判不过的题把出题理由（trap / came_from）一起带上：报告要能说出
    "这题为什么设计、它挂在哪"，只给一个 ✗ 没法改。

    待定口径的回流题（W4-03）不进分母：它还没有预期答案，判它过或挂都不诚实。
    """
    rows, passed, pending = [], 0, 0
    for c in cases:
        cid = c.get("id")
        # pending 由用例定义决定，不由"这道题恰好有没有作答"决定 ——
        # 否则漏答一条待定题，它就会被算进分母（这是个真出现过的错法）。
        is_pending = (c.get("expect") or {}).get("type") == "pending"
        ans = (answers or {}).get(cid)
        if ans is None:
            rows.append({"id": cid, "ok": False,
                         "reason": "没有作答（漏跑或跑挂）",
                         "expect_type": (c.get("expect") or {}).get("type"),
                         "difficulty": c.get("difficulty"),
                         "group": c.get("group"), "pending": is_pending})
            pending += 1 if is_pending else 0
            continue
        r = check_case(c, ans)
        pending += 1 if is_pending else 0
        rows.append(dict(id=cid, ok=r["ok"], reason=r["reason"],
                         expect_type=r["expect_type"],
                         difficulty=c.get("difficulty"),
                         trap=c.get("trap"), group=c.get("group"),
                         pending=is_pending))
        passed += 1 if r["ok"] else 0
    scored = len(cases) - pending
    return {"rows": rows, "passed": passed, "total": len(cases), "pending": pending,
            "scored": scored,
            "rate": round(passed / scored * 100, 1) if scored else 0.0}


def to_json(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2)
