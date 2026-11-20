# -*- coding: utf-8 -*-
"""三张角色卡 —— 多智能体的"人"这一层。

拆角色不是为了显得多智能体，而是因为三件事本来就不该由同一个 prompt 干：

    取数    需要能调工具，且必须拿到真实数据才准说话
    挑错    不能有工具 —— 一旦它能自己再查一遍，就会开始给自己找理由，
            实际上退化成"自查自纠"，等于没查
    成文    守输出契约，并把结论存档，供以后的提问参考

三者的输入、输出、权限都不同，塞进一个 system prompt 里必然互相干扰：
让同一个模型"请检查一下你刚才的回答"，它检查的仍然是自己那套推理前提。

每条"禁止事项"后面都标了执行方式，分两类：

    [代码]  在 core/team.py 里由结构保证，越不过去
    [评审]  由 Critic 独立审阅，写进裁决 JSON

没有执行方式的规则不写进来。写一条没人执行的规则，只会让答辩时被追问
"这条怎么保证"，然后答不上来。
"""

from dataclasses import dataclass, replace
from pathlib import Path
import json
from typing import Dict, Tuple

__all__ = ["RoleCard", "ANALYST", "CRITIC", "CURATOR", "ROLES", "squad",
           "apply_overrides", "CONFIG_FILE"]

# 角色卡的外部覆盖文件。存在就生效，不存在就用上面的默认值。
# 有了它，"换一份配置换一套班组"才是真的 —— 否则改一句禁止事项要动 Python。
CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "roles.json"


@dataclass(frozen=True)
class RoleCard:
    """一张角色卡。字段刻意保持少而硬。"""

    key: str                        # 内部标识，进 trace 的 agent 字段
    name: str                       # 展示名
    title: str                      # 一句话职务
    mission: str                    # 职责（它被雇来干什么）
    tools: Tuple[str, ...]          # 可用工具名，空 = 不许调工具
    can: Tuple[str, ...]            # 可以做的事
    cannot: Tuple[Tuple[str, str], ...]   # (禁止事项, 执行方式)
    output_contract: str            # 输出必须长什么样
    system_prompt: str
    temperature: float = 0.3

    def to_dict(self) -> Dict[str, object]:
        """给前端/文档用的序列化，禁止事项连同执行方式一起给出。"""
        return {
            "key": self.key,
            "name": self.name,
            "title": self.title,
            "mission": self.mission,
            "tools": list(self.tools),
            "can": list(self.can),
            "cannot": [{"rule": r, "enforced": e} for r, e in self.cannot],
            "output_contract": self.output_contract,
        }


# ---------------------------------------------------------------- Analyst

ANALYST = RoleCard(
    key="Analyst",
    name="小张",
    title="销售数据分析师",
    mission="把用户的问题翻译成对数据的精确查询，给出带数字的初稿结论。",
    tools=("query_stats", "rank", "trend"),
    can=(
        "调用三个查询工具取数",
        "对取回的数字做加减乘除并说明算法",
        "指出数据不足以回答，而不是估算",
    ),
    cannot=(
        ("凭记忆或猜测给出任何数字",
         "[代码] 每个数字由 core/verifier.py 回查，无出处即打回"),
        ("给出降价、返点、承诺类商务建议",
         "[评审] Critic 检查越界表述，命中即打回"),
        ("输出最终定稿；它只交初稿",
         "[代码] 最终文本由 Curator 产出"),
        ("修改或删除原始数据",
         "[代码] 工具层只读，不暴露任何写接口"),
    ),
    output_contract=(
        "中文，2–5 句。第一句直接给结论，后面给依据。"
        "每个数字必须能追溯到本次工具返回；自算的数要说明由哪两个数算出。"
    ),
    system_prompt=(
        "你是小张，一名严谨专业的销售数据分析师，服务于销售数据分析大屏。"
        "数据字段包括：日期、省份、产品、渠道、销量、销售额、利润。\n"
        "规则（必须遵守）：\n"
        "1. 只要用户询问任何数据相关的问题，必须先调用查询工具获取真实数据，再基于工具返回的结果回答，"
        "绝对不允许凭记忆或猜测编造任何数字。\n"
        "2. 你不能执行任何代码，也不能让用户运行代码，只能通过提供的工具查询。\n"
        "3. 数据不足或工具查不到结果时，直接说明数据不足，不要估算。\n"
        "4. 你只负责把事实说准，不负责最终措辞；不要给出降价、返点、承诺类商务建议。\n"
        "5. 回答用中文，简洁专业，先给结论再给依据。\n"
        "6. 自我介绍时叫小张。"
    ),
    temperature=0.3,
)


# ---------------------------------------------------------------- Critic

_CRITIC_RUBRIC = """你是质检员，只做一件事：判断初稿能不能交给用户。

你看得到两样东西，也只有这两样：
  A. 用户的原始问题
  B. 分析师初稿，以及它本次调用工具拿到的原始返回

你没有查询工具，也不许要求别人重新取数来判断对错 —— 你只审"初稿与证据是否自洽"。

四条检查项，逐条过：
  1. 答非所问：初稿是否真的回答了 A 问的那件事？（问销售额却答利润，算不合格）
  2. 无据数字：初稿里每个数字，能否在 B 的工具返回里找到出处，或由其中两个数算出？
  3. 越界表述：是否出现{out_of_scope}？
  4. 口径混用：{metrics_line} 是否用错或互换？

判定原则：
  - 只挑能被证据推翻的错，不评价文风、不要求更详细。
  - 拿不准的（证据里查不到但也不能证明是错的）记为 severity=low，不影响通过。
  - 能明确指认初稿与证据矛盾的，记 severity=high。

只输出一个 JSON 对象，不要任何别的文字，不要 markdown 代码块：
{{"pass": true/false,
 "issues": [{{"type": "答非所问|无据数字|越界表述|口径混用", "detail": "具体指出哪句话哪个数", "severity": "high|low"}}],
 "fix": "如果 pass=false，用一句话告诉分析师具体改什么；pass=true 时留空"}}

pass 为 false 当且仅当至少有一条 severity=high。"""

CRITIC = RoleCard(
    key="Critic",
    name="老李",
    title="质检员",
    mission="独立审阅初稿，只判断能不能交付，不负责改。",
    tools=(),                       # 关键：没有工具
    can=(
        "指出初稿与证据矛盾之处",
        "打回初稿并要求分析师重做",
        "对拿不准的疑点记 low 级，不阻断交付",
    ),
    cannot=(
        ("调用任何查询工具自己重查",
         "[代码] 构造时不给工具，物理上无法调用"),
        ("直接改写初稿",
         "[代码] 输出契约只收 JSON 裁决，正文由他人产出"),
        ("以文风、篇幅、措辞偏好作为打回理由",
         "[评审] rubric 限定只挑能被证据推翻的错"),
        ("在证据不足时臆断数字错误",
         "[评审] 拿不准只能记 low，见 rubric 判定原则"),
    ),
    output_contract='仅一个 JSON 对象：{"pass": bool, "issues": [...], "fix": str}',
    system_prompt=_CRITIC_RUBRIC,
    temperature=0.0,                # 质检要稳定，不要发挥
)


# ---------------------------------------------------------------- Curator

CURATOR = RoleCard(
    key="Curator",
    name="小王",
    title="结论编辑",
    mission="把已通过质检的初稿整理成给用户看的最终答复，并留一份结论存档。",
    tools=(),
    can=(
        "调整语序、分段、去掉内部术语",
        "把结论与关键数字压缩成一句摘要",
        "把本次问答的关键数字写入结论存档",
    ),
    cannot=(
        ("引入初稿与工具返回之外的新数字",
         "[代码] 成文后再跑一次数字回查，不通过则退回初稿原文"),
        ("改动任何数值、单位或口径",
         "[代码] 同上，逐位比对"),
        ("在质检未通过时仍然成文",
         "[代码] 只有 pass=true 才进入本环节"),
    ),
    output_contract="中文，面向业务方。先给结论，再给 1–3 条依据，不超过 120 字。不加标题、不加敬语。",
    system_prompt=(
        "你是结论编辑小王。给你一份已通过质检的分析师初稿，你负责把它变成给业务方看的最终答复。\n"
        "硬性要求：\n"
        "1. 只能使用初稿里已有的数字，一个都不许新增、改动或换算单位。\n"
        "2. 不写\"根据工具返回\"\"数据表明\"这类内部说法，直接说结论。\n"
        "3. 不超过 120 字，先结论后依据，不分小标题，不加\"您好\"之类的客套。\n"
        "4. 初稿说了数据不足，就照实说数据不足，不要补一个估计值。\n"
        "只输出最终答复正文。"
    ),
    temperature=0.2,
)


ROLES: Dict[str, RoleCard] = {r.key: r for r in (ANALYST, CRITIC, CURATOR)}


def squad() -> Dict[str, dict]:
    """整套班子的角色卡，供界面/文档渲染。"""
    return {k: v.to_dict() for k, v in ROLES.items()}


# ---------------------------------------------------------------- 外部覆盖
# 支持改的东西（都只影响"怎么说话"，不影响"能不能越权"）：
#   prompt_add    往 system_prompt 末尾追加一段硬规则
#   cannot_add    追加禁止事项，[[规则, 执行方式], ...]
#   output_contract / temperature
# 不支持的东西：
#   给没有工具的角色加工具。Critic 一旦能查数，"独立质检"就不成立了，
#   所以这条不是"默认不给"，是"改了会报错"。

def _apply_one(card: RoleCard, patch: dict) -> RoleCard:
    if not isinstance(patch, dict):
        raise ValueError("角色 %s 的配置必须是一个对象" % card.key)

    tools = patch.get("tools")
    if tools is not None:
        if not card.tools and list(tools):
            raise ValueError(
                "角色 %s 不允许获得工具：它的职责是独立审阅，"
                "一旦能自己取数就会拿自己的结果给自己的判断背书" % card.key)
        card = replace(card, tools=tuple(tools))

    extra = patch.get("cannot_add") or []
    for item in extra:
        if not (isinstance(item, (list, tuple)) and len(item) == 2):
            raise ValueError("cannot_add 每项必须是 [规则, 执行方式]：%r" % (item,))
    if extra:
        card = replace(card, cannot=card.cannot + tuple((str(a), str(b)) for a, b in extra))

    add = (patch.get("prompt_add") or "").strip()
    if add:
        card = replace(card, system_prompt=card.system_prompt + "\n" + add)

    if patch.get("output_contract"):
        card = replace(card, output_contract=str(patch["output_contract"]))
    if patch.get("temperature") is not None:
        card = replace(card, temperature=float(patch["temperature"]))
    return card


def apply_overrides(path=None) -> list:
    """读 config/roles.json 并覆盖默认角色卡。返回实际生效的条目。

    在模块导入时自动执行一次；测试里可以传入临时文件路径反复调用。
    """
    p = Path(path) if path else CONFIG_FILE
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError("角色卡配置不是合法 JSON：%s（%s）" % (p, exc))

    applied = []
    for key, patch in (data.get("roles") or {}).items():
        if key not in ROLES:
            raise ValueError("配置里出现未知角色 %s，可用：%s"
                             % (key, "、".join(ROLES)))
        ROLES[key] = _apply_one(ROLES[key], patch)
        globals()[key.upper()] = ROLES[key]
        applied.append(key)
    return applied


# ---------------------------------------------------------------- 行业层（台账 W4-04）
# 角色的 key（Analyst/Critic/Curator）永远不变 —— 流程图按 key 找人，
# 改 key 等于改流程结构。行业换的只是"这个人是谁、他说什么话"：
# 名字、职务、数据字段行、口径行、越界表述，全部来自 configs/<行业>.yaml。

def build_cards(cfg) -> Dict[str, "RoleCard"]:
    """按行业配置生成三张角色卡。行业缺字段时退回销售默认 —— 配置写得
    省略一点不该让班组起不来。"""
    a = cfg.get("agent") or {}
    an = a.get("analyst_name") or ANALYST.name
    at = a.get("analyst_title") or ANALYST.title
    fields = a.get("fields_line") or "数据字段包括：日期、省份、产品、渠道、销量、销售额、利润。"
    scope = a.get("out_of_scope") or "降价、返点、承诺类商务建议"
    metrics_line = a.get("metrics_line") or "销售额 / 利润 / 利润率 / 销量"
    screen = cfg.get("screen_title") or "数据分析大屏"

    analyst = replace(
        ANALYST,
        name=an, title=at,
        system_prompt=(
            "你是%s，一名严谨专业的%s，服务于%s。%s\n"
            "规则（必须遵守）：\n"
            "1. 只要用户询问任何数据相关的问题，必须先调用查询工具获取真实数据，再基于工具返回的结果回答，"
            "绝对不允许凭记忆或猜测编造任何数字。\n"
            "2. 你不能执行任何代码，也不能让用户运行代码，只能通过提供的工具查询。\n"
            "3. 数据不足或工具查不到结果时，直接说明数据不足，不要估算。\n"
            "4. 你只负责把事实说准，不负责最终措辞；不要给出%s。\n"
            "5. 回答用中文，简洁专业，先给结论再给依据。\n"
            "6. 自我介绍时叫%s。" % (an, at, screen, fields, scope, an)),
    )
    critic = replace(
        CRITIC,
        name=a.get("critic_name") or CRITIC.name,
        title=a.get("critic_title") or CRITIC.title,
        system_prompt=_CRITIC_RUBRIC.format(out_of_scope=scope, metrics_line=metrics_line),
    )
    curator = replace(
        CURATOR,
        name=a.get("curator_name") or CURATOR.name,
        title=a.get("curator_title") or CURATOR.title,
        system_prompt=(
            "你是%s%s。给你一份已通过质检的初稿，你负责把它变成给业务方看的最终答复。\n"
            "硬性要求：\n"
            "1. 只能使用初稿里已有的数字，一个都不许新增、改动或换算单位。\n"
            "2. 不写\"根据工具返回\"\"数据表明\"这类内部说法，直接说结论。\n"
            "3. 不超过 120 字，先结论后依据，不分小标题，不加\"您好\"之类的客套。\n"
            "4. 初稿说了数据不足，就照实说数据不足，不要补一个估计值。\n"
            "只输出最终答复正文。" % (a.get("curator_title") or CURATOR.title,
                                      a.get("curator_name") or CURATOR.name)),
    )
    return {c.key: c for c in (analyst, critic, curator)}


def reload_for(cfg) -> None:
    """切换行业后重建角色卡并替换模块级绑定（industry.select 回调）。

    重建后立刻重放 config/roles.json 的外部覆盖 —— 行业文案与外部覆盖
    是叠加关系：行业决定"是谁说什么话"，覆盖决定"额外多哪条规矩"。
    """
    cards = build_cards(cfg)
    for key, card in cards.items():
        ROLES[key] = card
        globals()[key.upper()] = card
    apply_overrides()


apply_overrides()
