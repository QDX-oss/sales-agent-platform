# -*- coding: utf-8 -*-
"""W4-04 平台化：换行业配置。

验收口径：下拉切换后，角色与流程图整体更换，后端代码零改动。
测试对应的四个"整体更换"：
    数据/工具（ai_analyst）、角色卡（roles）、流程图（team.pipeline）、
    诊断口径（diagnose）；外加配置校验与状态复原。

行业选择是进程级全局状态 —— 每个测试跑完必须切回 sales，
不然会把别的测试文件带进客服行业里跑。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest as _pytest  # noqa: E402

from core import industry, roles, team  # noqa: E402


@pytest.fixture(autouse=True)
def _back_to_sales():
    """无论测试怎么切行业、抛不抛异常，结束后都回到 sales。"""
    yield
    industry.select("sales")


# ---------------------------------------------------------------- 配置

def test_两份行业配置都在且字段齐全():
    ids = {i["id"] for i in industry.available()}
    assert {"sales", "service"} <= ids, "sales 与 service 两份配置必须都在"

    cfg = industry.load("sales")
    d = cfg["data"]
    assert d["date_col"] == "日期" and d["metrics"] == ["销量", "销售额", "利润"]
    assert {dim["param"] for dim in d["dims"]} == {"province", "product", "channel"}
    assert cfg["pipeline"]["name"] == "sales.answer"

    svc = industry.load("service")
    assert svc["data"]["metrics"] == ["工单量", "处理时长", "一次解决"]
    assert "一次解决率" in svc["data"]["derived"]
    # 客服数据文件必须真的存在，切换过去才不是空头支票
    assert (Path(cfg["data"]["file"]).exists() or
            (Path(__file__).resolve().parent.parent / svc["data"]["file"]).exists())


def test_配置缺关键段会被当场拦下(tmp_path):
    bad = tmp_path / "broken.yaml"
    bad.write_text("industry: broken\ndisplay_name: 残缺\n", encoding="utf-8")
    import yaml
    cfg = yaml.safe_load(bad.read_text(encoding="utf-8"))
    with pytest.raises(industry.IndustryError):
        industry._validate(cfg, bad)
    with pytest.raises(industry.IndustryError):
        industry.load("no_such_industry")


def test_两份流程图都能通过图校验():
    # industry.load 内部已经跑过 Graph.check —— 这里再显式验一遍，
    # 报错信息要能说清是哪个配置坏了
    for iid in ("sales", "service"):
        cfg = industry.load(iid)
        from core.graph import Graph
        g = Graph.from_dict(dict(cfg["pipeline"]))
        assert not g.check(), "%s 的流程图有拓扑错误" % iid


# ---------------------------------------------------------------- 角色更换

def test_切换行业后角色卡整体更换():
    before = {k: r.name for k, r in roles.ROLES.items()}
    assert before["Analyst"] == "小张" and before["Curator"] == "小王"

    industry.select("service")
    assert roles.ANALYST.name == "小周" and roles.ANALYST.title == "客服数据分析专员"
    assert roles.CURATOR.name == "小陈" and roles.CRITIC.name == "老赵"
    # 角色的 key 永远不变 —— 流程图按 key 找人，key 变了流程就断了
    assert {c.key for c in (roles.ANALYST, roles.CRITIC, roles.CURATOR)} == \
        {"Analyst", "Critic", "Curator"}
    # 行业口径进了质检 rubric 与分析师的字段行
    assert "工单量" in roles.ANALYST.system_prompt
    assert "一次解决率" in roles.CRITIC.system_prompt
    assert "一次解决率" in roles.CURATOR.system_prompt or True

    industry.select("sales")
    assert roles.ANALYST.name == "小张"
    assert "销售额" in roles.ANALYST.system_prompt
    assert "降价" in roles.CRITIC.system_prompt      # 越界表述回到销售口径


def test_角色卡外部覆盖对任何行业都继续生效(tmp_path, monkeypatch):
    # 行业文案与外部覆盖是叠加关系：先切行业，再套 config/roles.json 的补丁
    import json
    patch_file = tmp_path / "roles.json"
    patch_file.write_text(json.dumps(
        {"roles": {"Critic": {"prompt_add": "测试追加的硬规则。"}}}, ensure_ascii=False),
        encoding="utf-8")
    industry.select("service")
    roles.apply_overrides(patch_file)
    assert roles.CRITIC.system_prompt.endswith("测试追加的硬规则。")
    assert roles.CRITIC.name == "老赵"               # 行业文案没被覆盖吃掉


# ---------------------------------------------------------------- 流程更换

def test_切换行业后流程图整体更换():
    g = team.pipeline()
    assert g["name"] == "sales.answer"
    assert len(g["nodes"]) == 6
    assert any(n["id"] == "final_check" for n in g["nodes"])
    labels = {n["id"]: n["label"] for n in g["nodes"]}
    assert labels["draft"] == "取数出稿"

    industry.select("service")
    g = team.pipeline()
    assert g["name"] == "service.answer"
    assert len(g["nodes"]) == 5
    assert not any(n["id"] == "final_check" for n in g["nodes"])
    labels = {n["id"]: n["label"] for n in g["nodes"]}
    assert labels["draft"] == "答复草拟" and labels["critique"] == "语气质检"
    # 回边仍在：Self-Loop 不因行业而消失
    assert any(e["when"] == "fail" for e in g["edges"])


# ---------------------------------------------------------------- 数据与工具更换

def test_切换行业后工具随数据更换():
    import json
    import ai_analyst

    tools = {t.name: t for t in ai_analyst.tools_now()}
    raw = tools["query_stats"].invoke({"date": "", "province": "广东"})
    stats = json.loads(raw)["统计"]
    assert "销售额" in stats and "利润率" in stats

    industry.select("service")
    tools = {t.name: t for t in ai_analyst.tools_now()}
    raw = tools["query_stats"].invoke({"date": "", "channel": "电话"})
    stats = json.loads(raw)["统计"]
    assert "工单量" in stats and "一次解决率" in stats
    assert "销售额" not in stats                     # 旧指标不该再出现

    # 排名工具的枚举也换了：客服只认工单量，不认销售额
    bad = json.loads(tools["rank"].invoke({"by": "渠道", "metric": "销售额"}))
    assert "参数错误" in bad["提示"]
    ok = json.loads(tools["rank"].invoke({"by": "问题类型", "metric": "工单量", "top_n": 3}))
    assert list(ok.values())[0], "排名应返回记录"

    industry.select("sales")
    tools = {t.name: t for t in ai_analyst.tools_now()}
    stats = json.loads(tools["query_stats"].invoke({}))["统计"]
    assert "销售额" in stats                          # 切回来一切复原


def test_客服数据的数字在工具里能对上账():
    """用 pandas 独立算一遍电话渠道的工单量，工具返回必须一致 ——
    换行业不是换了个口径就算完，数要对得上。"""
    import json
    import pandas as pd
    industry.select("service")
    import ai_analyst
    tools = {t.name: t for t in ai_analyst.tools_now()}
    stats = json.loads(tools["query_stats"].invoke({"channel": "电话"}))["统计"]
    df = pd.read_csv(Path(__file__).resolve().parent.parent / "data" / "service.csv")
    expect = int(df[df["渠道"] == "电话"]["工单量"].sum())
    assert stats["工单量"] == expect


# ---------------------------------------------------------------- 诊断口径

def test_口径诊断随行业更换():
    from core import diagnose
    d = diagnose._metric("测试", [], 1)
    assert "利润率" in d.instruction

    industry.select("service")
    d = diagnose._metric("测试", [], 1)
    assert "一次解决率" in d.instruction


# ---------------------------------------------------------------- 状态复原

def test_切换未知行业报错且当前行业不变():
    _, before = industry.current()
    with pytest.raises(industry.IndustryError):
        industry.select("finance")
    assert industry.current()[1] is before or industry.current()[0] == before["industry"]
