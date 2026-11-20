# -*- coding: utf-8 -*-
"""
生成模拟销售数据（电子产品）-> data/data.csv
运行一次即可，重复运行会覆盖旧数据
"""
import csv
import random
from datetime import date, timedelta
from pathlib import Path

# ---------- 基础配置 ----------

# 省份列表（尽量多，覆盖全国主要省份）
PROVINCES = [
    "北京", "上海", "广东", "江苏", "浙江", "山东", "四川", "湖北",
    "河南", "湖南", "河北", "福建", "安徽", "陕西", "辽宁", "江西",
    "云南", "广西", "山西", "重庆", "贵州", "天津", "黑龙江", "吉林",
]

# 电子产品及单价区间（元）：(产品名, 最低单价, 最高单价)
PRODUCTS = [
    ("笔记本电脑", 3500, 12000),
    ("智能手机", 1500, 8000),
    ("平板电脑", 1200, 6000),
    ("蓝牙耳机", 150, 1800),
    ("智能手表", 400, 3000),
    ("显示器", 700, 4000),
    ("机械键盘", 200, 1200),
    ("移动电源", 60, 400),
]

# 销售渠道
CHANNELS = ["天猫", "京东", "抖音", "微信小程序", "线下专柜"]

# 日期范围：2025年全年
START_DATE = date(2025, 1, 1)
DAYS = 365

N_ROWS = 300  # 总条数

# ---------- 生成数据 ----------

random.seed(2025)  # 固定随机种子，保证每次生成结果一致

# 保证日期分散：随机抽300个不重复的天
dates = random.sample(range(DAYS), N_ROWS)

rows = []
for offset in dates:
    d = START_DATE + timedelta(days=offset)
    name, lo, hi = random.choice(PRODUCTS)          # 随机产品
    unit_price = random.randint(lo, hi)             # 该次成交的单价
    volume = random.randint(5, 200)                 # 销量（件）：电子产品单价高，销量比美妆低
    revenue = volume * unit_price                   # 销售额 = 销量 x 单价
    profit = int(revenue * random.uniform(0.08, 0.3))  # 利润率 8%~30%
    rows.append([
        d.isoformat(),                              # 日期
        random.choice(PROVINCES),                   # 省份
        name,                                       # 产品
        random.choice(CHANNELS),                    # 渠道
        volume,                                     # 销量
        revenue,                                    # 销售额
        profit,                                     # 利润
    ])

# ---------- 写入 CSV ----------

out_dir = Path(__file__).parent / "data"
out_dir.mkdir(exist_ok=True)  # data 目录不存在则创建
out_file = out_dir / "data.csv"

# utf-8-sig 编码：带 BOM，Excel 打开不乱码
with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.writer(f)
    writer.writerow(["日期", "省份", "产品", "渠道", "销量", "销售额", "利润"])
    writer.writerows(rows)

print(f"已生成 {len(rows)} 条数据 -> {out_file}")
