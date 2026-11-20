# -*- coding: utf-8 -*-
"""行业层 —— 换一份配置换一个行业（台账 W4-04）。

此前"行业"这个概念散落在四处硬编码里：
    数据列        data_source.py REQUIRED_COLS
    工具参数      ai_analyst.py 省份/产品/渠道 三个参数与四个指标
    角色卡文案    core/roles.py "小张 / 销售数据分析师 / 利润率口径"
    流程图        core/team.py SALES_PIPELINE

W4-04 把这四件事抽进 configs/<行业>.yaml。本模块是行业层的唯一入口：
    current()   当前行业的配置 dict
    select()    切换行业，并把散在各处的行业状态重置干净
    available() 界面下拉框的选项

两个刻意的边界：
1. 这里只管"干什么活"。模型通道、调度策略、重试预算是跨行业共用的
   "怎么干活"，仍在 config/ 下，不搬进来 —— 同一份信息只有一个真源。
2. 切换是进程内的（刷新页面、重启服务都回到 sales）。行业是演示层
   的概念，不落库；落库就得回答"两个人同时在用，听谁的"，那不是
   这一条要解决的问题。

依赖方向：industry 不 import ai_analyst / roles（只在 select() 里懒加载
回调它们），否则模块导入会成环。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, List, Tuple

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"
DEFAULT_INDUSTRY = "sales"

_lock = threading.RLock()
_cache: Dict[str, dict] = {}
_current: Dict[str, object] = {"id": None, "cfg": None}


class IndustryError(ValueError):
    """行业不存在或配置坏了。给上层一个明确的类型，别拿裸 ValueError 混过去。"""


# ---------------------------------------------------------------- 校验

def _validate(cfg: dict, source: Path) -> None:
    """配置缺关键段就报错。加载时一次验完，别等到运行到一半才炸。"""
    missing = [k for k in ("industry", "display_name", "data", "agent", "pipeline")
               if k not in cfg]
    if missing:
        raise IndustryError("%s 缺少必需段：%s" % (source.name, "、".join(missing)))

    d = cfg["data"]
    for k in ("file", "date_col", "dims", "metrics"):
        if k not in d:
            raise IndustryError("%s 的 data 段缺少 %s" % (source.name, k))
    if not d["dims"]:
        raise IndustryError("%s 的 data.dims 不能为空：工具没有可过滤的维度" % source.name)
    for dim in d["dims"]:
        if not dim.get("param") or not dim.get("column"):
            raise IndustryError("%s 的维度缺 param 或 column：%r" % (source.name, dim))

    p = cfg["pipeline"]
    if not p.get("nodes"):
        raise IndustryError("%s 的 pipeline.nodes 不能为空" % source.name)

    # 流程图当场验一遍拓扑：依赖指错节点，加载时就报，不留到跑 run 的时候
    from core.graph import Graph
    g = Graph.from_dict(dict(p))
    errors = g.check()
    if errors:
        raise IndustryError("%s 的流程图不合法：%s" % (source.name, "；".join(errors)))


# ---------------------------------------------------------------- 读取

def available() -> List[Dict[str, str]]:
    """configs/ 下所有合法行业，按 id 排序。坏文件如实报，不静默跳过。"""
    out = []
    for p in sorted(CONFIG_DIR.glob("*.yaml")):
        try:
            cfg = load(p.stem)
            out.append({"id": cfg["industry"], "display_name": cfg["display_name"]})
        except IndustryError:
            raise
    return out


def load(industry_id: str) -> dict:
    """读一份行业配置（带缓存）。配置文件改了要重启服务 —— 与 config/ 下
    其他文件同一个口径，不引入热加载的复杂度。"""
    with _lock:
        if industry_id in _cache:
            return _cache[industry_id]
        p = CONFIG_DIR / ("%s.yaml" % industry_id)
        # 行业 id 限定在目录内的文件名形状里，路径穿越进不来
        if not p.exists() or "/" in industry_id or "\\" in industry_id or ".." in industry_id:
            raise IndustryError("没有这个行业配置：%s" % industry_id)
        try:
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise IndustryError("%s 不是合法的 YAML：%s" % (p.name, exc))
        _validate(cfg, p)
        _cache[industry_id] = cfg
        return cfg


def current() -> Tuple[str, dict]:
    """(行业 id, 配置)。未选过就给 sales —— 让所有旧调用方零改动照常工作。"""
    with _lock:
        if _current["cfg"] is None:
            cfg = load(DEFAULT_INDUSTRY)
            _current.update(id=cfg["industry"], cfg=cfg)
        return _current["id"], _current["cfg"]


# ---------------------------------------------------------------- 切换

def select(industry_id: str) -> dict:
    """切换行业，并把散在别处的行业状态重置干净。

    必须重置的三样（少一样就是"切换了但没切干净"）：
      数据缓存   data_source —— service.csv 和 data.csv 是两个文件
      工具       ai_analyst —— 参数签名 / 指标枚举 / docstring 都是行业的
      角色卡     roles —— 名字、职务、口径行都是行业的
    """
    cfg = load(industry_id)
    with _lock:
        _current.update(id=cfg["industry"], cfg=cfg)

    # 懒加载回调，避免模块导入成环
    import data_source
    data_source.clear_cache()

    import ai_analyst
    ai_analyst.rebind_tools()

    from core import roles
    roles.reload_for(cfg)

    return cfg
