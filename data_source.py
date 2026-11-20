# -*- coding: utf-8 -*-
"""
CSV 唯一读取入口。

全项目对 data.csv 的读取都必须经过 load_csv()。此前同一个文件有三处各写一套
读法（dashboard 直接 pd.read_csv、read_csv_safe、ai_analyst.load_df），
任何一处行为不同都会让大屏展示值与 AI 分析结果分叉。

依赖只有 pandas 与标准库，不引入 flask / langchain，
因此 web 层与 agent 层都可以安全地顶层导入。

台账编号：W0-02
"""
from pathlib import Path

import pandas as pd

# 尝试的编码顺序：带 BOM 的 UTF-8 优先（Excel 导出的常见形态），其次 GBK
ENCODINGS = ("utf-8-sig", "utf-8", "gbk")

# 销售行业的一份有效数据必须包含的列（台账 W0-04 时的口径）。
# W4-04 起行业可换：agent 层按行业配置传 required_columns；这里保留的
# 默认值只服务两处仍绑死销售数据的地方 —— 大屏 dashboard 与管理后台上传。
REQUIRED_COLS = ["日期", "省份", "产品", "渠道", "销量", "销售额", "利润"]

# 单文件缓存：路径与 mtime 都没变时复用已解析的 DataFrame
_cache = {"path": None, "mtime": None, "df": None}


def load_csv(path, require_columns=True):
    """读取 CSV，返回 (DataFrame, None) 或 (None, 错误信息)。

    - 按 ENCODINGS 顺序尝试解码，全部失败才返回错误
    - 路径与 mtime 均未变化时直接返回缓存，避免重复解析
    - require_columns=True 时校验必需列，缺列返回明确错误而不是半成品
    """
    path = Path(path)

    if not path.exists():
        return None, "数据文件不存在"

    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        return None, "无法读取数据文件：%s" % exc

    if _cache["path"] == str(path) and _cache["mtime"] == mtime:
        return _cache["df"], None

    last_error = None
    for encoding in ENCODINGS:
        try:
            df = pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        except Exception as exc:
            return None, "解析 CSV 失败：%s" % exc

        if require_columns:
            # W4-04：传列表 = 行业自定义的必需列；传 True = 用销售默认列
            needed = REQUIRED_COLS if require_columns is True else list(require_columns)
            actual = [str(c).strip() for c in df.columns]
            missing = [c for c in needed if c not in actual]
            if missing:
                return None, "数据缺少必需列：" + "、".join(missing)

        _cache.update(path=str(path), mtime=mtime, df=df)
        return df, None

    return None, "无法识别文件编码（已尝试 UTF-8 与 GBK）：%s" % last_error


def clear_cache():
    """数据文件被整体替换后调用，强制下次重新解析。"""
    _cache.update(path=None, mtime=None, df=None)
