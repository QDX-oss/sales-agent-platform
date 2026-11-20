# -*- coding: utf-8 -*-
"""
Flask 项目入口（销售数据大屏）
运行: python app.py
访问: http://127.0.0.1:5000
"""
import json
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (Flask, jsonify, redirect, render_template, request,
                   session, url_for)

from core.trace import STATUS_FAILED, Trace
from data_source import REQUIRED_COLS, clear_cache, load_csv

# 创建 Flask 应用实例
app = Flask(__name__)

# session 签名密钥（演示用）
app.secret_key = "dashboard-demo-secret"

# 数据文件路径
DATA_FILE = Path(__file__).parent / "data" / "data.csv"

# 访问记录文件（存放每次登录时间）
VISITS_FILE = Path(__file__).parent / "data" / "visits.json"

# 密码文件（首次使用时自动写入默认密码）
PASSWORD_FILE = Path(__file__).parent / "data" / "password.json"
DEFAULT_PASSWORD = "12345"

# 上传 CSV 必须包含的列：统一定义在 data_source，避免两处各写一份

# 省份简称 -> 地图全名 映射（用于中国地图名称匹配）
PROVINCE_FULL = {
    "北京": "北京市", "上海": "上海市", "天津": "天津市", "重庆": "重庆市",
    "广东": "广东省", "江苏": "江苏省", "浙江": "浙江省", "山东": "山东省",
    "四川": "四川省", "湖北": "湖北省", "河南": "河南省", "湖南": "湖南省",
    "河北": "河北省", "福建": "福建省", "安徽": "安徽省", "陕西": "陕西省",
    "辽宁": "辽宁省", "江西": "江西省", "云南": "云南省", "广西": "广西壮族自治区",
    "山西": "山西省", "贵州": "贵州省", "黑龙江": "黑龙江省", "吉林": "吉林省",
}


# 首页路由：落到一体化外壳（大屏/编排屏/后台/设置同页切换，不新开窗口）
@app.route("/")
def index():
    return _shell("dashboard")


# 内层视图路由：shell.html 的 iframe 用。页面本身不含数据，
# 数据仍来自受保护的 /api/*；未登录时打开外壳会被拦到 /login。
@app.route("/view/dashboard")
def view_dashboard():
    return render_template("index.html")


# ================= 登录与访问记录 =================

def read_visits():
    """读取访问记录列表"""
    if VISITS_FILE.exists():
        try:
            return json.loads(VISITS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def append_visit():
    """记录一次登录时间"""
    visits = read_visits()
    visits.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    VISITS_FILE.parent.mkdir(exist_ok=True)
    VISITS_FILE.write_text(json.dumps(visits, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def read_password():
    """读取当前密码（不存在则初始化为默认密码）"""
    if PASSWORD_FILE.exists():
        try:
            return json.loads(PASSWORD_FILE.read_text(encoding="utf-8"))["password"]
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    write_password(DEFAULT_PASSWORD)
    return DEFAULT_PASSWORD


def write_password(pwd):
    VISITS_FILE.parent.mkdir(exist_ok=True)
    PASSWORD_FILE.write_text(json.dumps({"password": pwd}, ensure_ascii=False),
                             encoding="utf-8")


# 登录拦截：未登录时页面跳转到 /login，接口返回 401
# orchestration_live 必须免登录：投屏时没人会去点登录，
# 而 1s 轮询只要被 401 打断一次，整屏就停在首帧不动了。
# /view/* 是外壳(shell.html)里的 iframe 内层页：页面本身不含数据，
# 数据全部来自受保护的 /api/*，所以与 orchestration 同等待遇免登录。
ALLOWED_ENDPOINTS = {"login", "api_login", "static", "orchestration",
                     "orchestration_run", "orchestration_live",
                     "orchestration_node", "orchestration_replay",
                     "industry_list", "industry_select", "team_roles",
                     "view_dashboard", "view_orchestration",
                     "view_admin", "view_settings"}


@app.before_request
def require_login():
    if request.endpoint in ALLOWED_ENDPOINTS:
        return None
    if session.get("logged_in"):
        return None
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error="未登录，请先登录"), 401
    return redirect(url_for("login"))


# 登录页
@app.route("/login")
def login():
    return render_template("login.html")


# 登录接口：校验密码，通过后记录登录时间
@app.route("/api/login", methods=["POST"])
def api_login():
    password = (request.json or {}).get("password", "")
    if password != read_password():
        return jsonify(ok=False, error="密码错误，请重试")
    session["logged_in"] = True
    append_visit()
    return jsonify(ok=True)


# 退出登录
@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify(ok=True)


# 访问记录接口
@app.route("/api/admin/visits")
def admin_visits():
    return jsonify(visits=read_visits())


# 设置页面：老地址落外壳；内层页走 /view/settings
@app.route("/settings")
def settings():
    return _shell("settings")


@app.route("/view/settings")
def view_settings():
    return render_template("settings.html")


# 修改密码：必须先输入当前密码
@app.route("/api/admin/password", methods=["POST"])
def admin_password():
    body = request.json or {}
    current = body.get("current", "")
    new = body.get("new", "")
    if read_password() != current:
        return jsonify(ok=False, error="当前密码不正确")
    if not new.strip():
        return jsonify(ok=False, error="新密码不能为空")
    if len(new) < 5:
        return jsonify(ok=False, error="新密码至少 5 位")
    write_password(new)
    return jsonify(ok=True, message="密码修改成功，请牢记新密码")


# 管理后台页面：老地址落外壳；内层页走 /view/admin
@app.route("/admin")
def admin():
    return _shell("admin")


@app.route("/view/admin")
def view_admin():
    return render_template("admin.html")


# 数据接口：返回大屏所需的全部汇总数据（一次请求拿全）
@app.route("/api/dashboard")
def dashboard():
    # 单一数据源：与 ai_analyst 的查询工具共用 data_source.load_csv()，
    # 同一份 CSV、同一套编码兜底、同一份缓存，杜绝两处数字不一致。
    # （台账 W0-02）
    df, data_error = load_csv(DATA_FILE)
    if df is None:
        # 文件缺失 / 编码不可识别 / 缺列：统一走空态，并把原因带给前端
        return jsonify(empty=True, error=data_error, kpi={}, monthly=[],
                       province=[], product=[], channel=[], details=[])

    # ---- 顶部核心指标 ----
    kpi = {
        "total_sales": int(df["销售额"].sum()),   # 总销售额
        "total_profit": int(df["利润"].sum()),    # 总利润
        "total_volume": int(df["销量"].sum()),    # 总销量
        "order_count": int(len(df)),              # 订单数
        "avg_order": int(df["销售额"].mean()),    # 平均客单价
        "profit_rate": round(df["利润"].sum() / df["销售额"].sum() * 100, 1),  # 利润率 %
    }

    # ---- 月度趋势（销售额 / 利润）----
    df["月份"] = df["日期"].astype(str).str[:7]
    monthly = (
        df.groupby("月份")[["销售额", "利润"]].sum().reset_index()
        .rename(columns={"月份": "month"})
        .to_dict("records")
    )

    # ---- 省份汇总（地图 + 地区占比 + 地区热榜）----
    prov = df.groupby("省份")[["销售额", "利润"]].sum().reset_index()
    prov["地图名"] = prov["省份"].map(PROVINCE_FULL)
    province = prov.sort_values("销售额", ascending=False).to_dict("records")

    # ---- 产品排行（按销售额）----
    product = (
        df.groupby("产品")["销售额"].sum().sort_values(ascending=False)
        .reset_index().to_dict("records")
    )

    # ---- 渠道占比（按销售额）----
    channel = (
        df.groupby("渠道")["销售额"].sum().sort_values(ascending=False)
        .reset_index().to_dict("records")
    )

    # ---- 销售明细（按日期倒序，取最近 30 条）----
    details = (
        df.sort_values("日期", ascending=False).head(30)
        .drop(columns=["月份"]).to_dict("records")
    )

    return jsonify(kpi=kpi, monthly=monthly, province=province,
                   product=product, channel=channel, details=details)


# ================= 管理后台接口 =================

def read_csv_safe(path):
    """兼容旧调用点：返回 (DataFrame 或 None, 错误信息)。

    列名校验由调用方自行完成（上传流程需要给出「必须包含……」的完整提示），
    因此这里传 require_columns=False。实际读取统一走 data_source.load_csv。
    """
    return load_csv(path, require_columns=False)


# 文件信息：是否存在、大小、行数、列名等
@app.route("/api/admin/info")
def admin_info():
    if not DATA_FILE.exists():
        return jsonify(exists=False, size=0, rows=0, columns=[], mtime=None)

    df, err = read_csv_safe(DATA_FILE)
    if df is None:
        return jsonify(exists=True, error=err, size=DATA_FILE.stat().st_size,
                       rows=0, columns=[], mtime=DATA_FILE.stat().st_mtime)
    return jsonify(exists=True,
                   size=DATA_FILE.stat().st_size,
                   rows=int(len(df)),
                   columns=list(df.columns),
                   mtime=DATA_FILE.stat().st_mtime)


# 数据预览：返回前 N 行（默认 20）
@app.route("/api/admin/preview")
def admin_preview():
    if not DATA_FILE.exists():
        return jsonify(rows=[], columns=[])
    limit = request.args.get("limit", default=20, type=int)
    limit = max(1, min(limit, 200))
    df, err = read_csv_safe(DATA_FILE)
    if df is None:
        return jsonify(rows=[], columns=[], error=err)
    return jsonify(columns=list(df.columns),
                   rows=df.head(limit).fillna("").to_dict("records"))


# 上传新 CSV：校验必须包含全部必需列，通过后覆盖保存为 data.csv
@app.route("/api/admin/upload", methods=["POST"])
def admin_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, error="请选择要上传的 CSV 文件")
    if not f.filename.lower().endswith(".csv"):
        return jsonify(ok=False, error="只支持 .csv 文件")

    tmp_path = DATA_FILE.parent / "upload_tmp.csv"
    f.save(tmp_path)

    df, err = read_csv_safe(tmp_path)
    if df is None:
        tmp_path.unlink(missing_ok=True)
        return jsonify(ok=False, error=err)

    # 校验列名（忽略首尾空格）
    actual = [c.strip() for c in df.columns]
    missing = [c for c in REQUIRED_COLS if c not in actual]
    if missing:
        tmp_path.unlink(missing_ok=True)
        return jsonify(ok=False,
                       error="缺少必需列: " + "、".join(missing) +
                             "（必须包含: " + "、".join(REQUIRED_COLS) + "）")

    # 校验通过，覆盖保存
    tmp_path.replace(DATA_FILE)
    clear_cache()  # 文件被整体替换，强制下次重新解析
    return jsonify(ok=True, rows=int(len(df)), columns=actual)


# 删除数据文件：必须显式确认（confirm=true）才执行
@app.route("/api/admin/delete", methods=["DELETE"])
def admin_delete():
    if not DATA_FILE.exists():
        return jsonify(ok=False, error="当前没有数据文件")
    if request.args.get("confirm") != "true":
        return jsonify(ok=False, error="未确认，删除操作已取消")
    DATA_FILE.unlink()
    clear_cache()  # 文件已删除，清掉缓存避免返回旧数据
    return jsonify(ok=True)


# ================= AI 数据分析师"小张"（LangChain Agent） =================

# 模型通道与密钥不再在这里读：统一由 core/fallback.py 按 config/models.json
# 组装成一条链（台账 W2-02）。这个文件曾经直接读 ai_key.txt 判断"能不能用"，
# 那样一来主 key 坏了就整个拒服务，备用通道等于白配。


@app.route("/api/team/roles")
def team_roles():
    """班组花名册：每个角色会什么、不能干什么、输出长什么样。

    这份数据就是角色卡的唯一真源（core/roles.py），界面上展示的和
    实际运行时用的必须是同一份，否则"花名册"只是宣传画。
    """
    from core.roles import squad
    return jsonify(ok=True, roles=squad())


@app.route("/api/industry")
def industry_list():
    """行业清单与当前选择（台账 W4-04）。

    下拉框的选项和当前值都从这里来 —— 界面上出现的行业必须是配置目录里
    真实存在的，不靠前端写死。
    """
    from core.industry import available, current
    cur_id, cfg = current()
    return jsonify(ok=True, current=cur_id,
                   display_name=cfg["display_name"],
                   industries=available())


@app.route("/api/industry/select", methods=["POST"])
def industry_select():
    """切换行业（台账 W4-04）。

    切换 = core.industry.select()：重载数据、工具、角色卡、流程图，
    全部来自新配置，后端代码零改动。切到不存在的行业如实报错。
    """
    industry = ((request.json or {}).get("industry") or "").strip()
    if not industry:
        return jsonify(ok=False, error="缺少 industry 参数")
    from core.industry import IndustryError, select
    try:
        cfg = select(industry)
    except IndustryError as exc:
        return jsonify(ok=False, error=str(exc))
    return jsonify(ok=True, industry=cfg["industry"],
                   display_name=cfg["display_name"],
                   screen_title=cfg.get("screen_title") or "")


@app.route("/api/team/models")
def team_models():
    """模型通道链现状（台账 W2-02）。

    哪个通道跳闸了、连续失败几次、还有多久冷却结束 —— 这些是降级
    有没有真的发生的直接证据，不该只能去翻 jsonl。
    reset=1 清空跳闸记忆（演示时把主 key 改回来之后用）。
    """
    from ai_analyst import channel_report
    from core import fallback
    if request.args.get("reset") == "1":
        fallback.reset_shared_state()
    return jsonify(ok=True, **channel_report())


@app.route("/api/ai/ask", methods=["POST"])
def ai_ask():
    """调用 AI 数据分析师小张回答关于当前数据的问题"""
    question = (request.json or {}).get("question", "").strip()
    if not question:
        return jsonify(ok=False, error="请输入问题")

    from ai_analyst import ask_team, has_model_channel

    # 判断标准是"有没有任意一条通道可用"，不是"主 key 文件在不在"：
    # 主通道坏了但备用配好了，本来就该照常服务（台账 W2-02）。
    if not has_model_channel():
        return jsonify(ok=False,
                       error="没有可用的模型通道。请在 config/models.json 配置通道，"
                             "并把密钥写入对应文件（如 ai_key.txt）。")

    # 一次提问 = 一次 run。班组里每个角色的每次模型/工具动作都落进同一个文件，
    # 靠 step.agent 区分是谁干的，前端凭返回的 run_id 可回看。
    #
    # W3-03 起可以指定 run_id：编排屏要实时看节点变色，就得先拿到 id 再开跑
    # （不然屏上只知道"有一个新 run 出现了"，追不上它前面的几毫秒）。
    # 只接受合法的 run_id 形状，写不出 runs 目录之外的文件。
    from core.orchestrate import RUN_ID_RE
    req_run_id = (request.json or {}).get("run_id")
    if req_run_id and not RUN_ID_RE.match(str(req_run_id)):
        return jsonify(ok=False, error="run_id 不合法")
    trace = Trace(question=question, source="web", run_id=req_run_id)
    try:
        outcome = ask_team(question, trace=trace)
    except Exception as e:
        trace.finish(status=STATUS_FAILED,
                     error={"type": type(e).__name__, "message": str(e)})
        return jsonify(ok=False, error="AI 分析师调用失败: " + str(e),
                       run_id=trace.run_id,
                       fallbacks=outcome_fallbacks(trace))

    summary = trace.finish()
    return jsonify(
        ok=True,
        answer=outcome["answer"],
        run_id=trace.run_id,
        trace=summary,
        verified=outcome["passed"],
        retries=outcome["retries"],
        curated=outcome["curated"],
        verification=outcome["verdict"],
        critic=outcome.get("critic") or {},
        # 调度账（台账 W2-01）：每个节点拿到了什么优先级、排了多久、
        # 有没有被 deadline 判超时。编排屏将来直接画这份，不另算一遍。
        schedule=outcome.get("schedule") or [],
        timed_out=outcome.get("timed_out") or [],
        degraded=outcome.get("degraded") or False,
        # 预算护栏（台账 W2-04）：中止时是 {"kind":"budget","limit":...,"used":...,"cap":...}。
        # 答案正文里已经说了为什么停，这里再给一份结构化字段，排障与前端都能用。
        aborted=outcome.get("aborted"),
        # 降级账（台账 W2-02）：换过通道没有、换到谁、最后是谁答的。
        # 悄悄降级然后当作一切正常，比直接失败更糟。
        fallbacks=outcome.get("fallbacks") or [],
        fallback_used=outcome.get("fallback_used") or False,
        model=outcome.get("model"),
        # 诊断账（台账 W2-03）：每一轮打回判成哪一类、下一轮换什么做法。
        # 前端只念结果，不重新判 —— 判在 core/diagnose.py，一处。
        diagnoses=outcome.get("diagnoses") or [],
        retry_strategies=outcome.get("retry_strategies") or [],
        handoff=outcome.get("handoff"),
    )


def outcome_fallbacks(trace):
    """失败收尾时也把降级账带出去 —— 失败原因可能正是"全链都挂了"。"""
    from core.fallback import fallback_events
    return fallback_events(trace)


# ================= 运行编排屏（台账 W3-01 起） =================

@app.route("/orchestration")
@app.route("/orchestration/")
def orchestration():
    """编排屏：把一次 run 的图、队列、降级、诊断摆在同一屏里。

    只读页面 —— 演示时可以左边开数据大屏提问、右边开这一屏看它是怎么跑的，
    两件事都发生在同一次 run 上。老地址落一体化外壳，与数据大屏同页切换；
    外壳里的 iframe 内层页是 /view/orchestration。
    """
    return _shell("orchestration")


@app.route("/view/orchestration")
def view_orchestration():
    return render_template("orchestration.html")


def _shell(view):
    """四个界面共用的外壳：一条顶部导航 + 惰性 iframe 容器。"""
    return render_template("shell.html", view=view)


@app.route("/api/orchestration/run")
def orchestration_run():
    """编排屏的数据口。run_id 省略就取最近一次运行。

    数据全部来自两份材料：图定义（core/team.py 的 pipeline()）与
    runs/<run_id>.jsonl。屏上不重算任何账 —— 重算就有第二个真源，
    第二个真源迟早和第一个对不上。
    """
    from core import orchestrate
    return jsonify(**orchestrate.build(request.args.get("run_id") or None))


@app.route("/api/orchestration/live")
def orchestration_live():
    """编排屏的实时口（W3-03）。前端每 1s 打一次。

    与 /run 是同一份数据 + 「现在谁在跑」的推断。分成两个口而不是给 /run
    加个 ?live=1：首次整屏渲染和轮询的读取量不同，混在一个口里以后
    加缓存策略会互相拖累。
    """
    from core import orchestrate
    return jsonify(**orchestrate.live(request.args.get("run_id") or None))


@app.route("/api/orchestration/node")
def orchestration_node():
    """单个节点的全部证据（W3-04）：点节点时按需取，不塞进 1s 轮询里。

    为什么单独开一个口：抽屉里的 payload / 事件流 / token 归集都要读
    第二份文件（graph_runs 与 step 行），每次轮询都带着它会把 1s 的
    刷新拖慢，而用户一次只看一个节点。
    """
    from core import orchestrate
    return jsonify(**orchestrate.node_detail(
        request.args.get("run_id") or "",
        request.args.get("node") or ""))


@app.route("/api/orchestration/replay")
def orchestration_replay():
    """一次运行的回放脚本（W3-05）：整条事件流一次交出，屏上自己走帧。

    为什么不按"播放到第几毫秒"每次回后端问一帧：断网也得能回放，
    而且拖时间轴时本地算比走网络稳 —— 一次运行只有几十帧，整份发过去最省事。
    """
    from core import orchestrate
    return jsonify(**orchestrate.replay(request.args.get("run_id") or ""))


# 启动开发服务器
if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
