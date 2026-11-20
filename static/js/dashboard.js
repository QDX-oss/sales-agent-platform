/* =========================================================
   大屏主脚本：数据请求 / 图表渲染 / 各类动画 / 定时刷新
   ========================================================= */

/* ---------- 1. 大屏缩放适配：按 1920x1080 设计稿等比缩放并居中 ---------- */
function fitScreen() {
    var screen = document.getElementById("screen");
    // 取宽高缩放比较小的一个，保证内容完整显示不变形
    var scale = Math.min(window.innerWidth / 1920, window.innerHeight / 1080);
    // CSS 已让容器居中(left:50% top:50%)，这里只需叠加缩放
    screen.style.transform = "translate(-50%, -50%) scale(" + scale + ")";
}
window.addEventListener("resize", fitScreen);
fitScreen();

/* ---------- 2. 顶部时间 ---------- */
function updateClock() {
    var now = new Date();
    var p = function (n) { return n < 10 ? "0" + n : n; };
    document.getElementById("header-time").textContent =
        p(now.getHours()) + ":" + p(now.getMinutes()) + ":" + p(now.getSeconds());
    document.getElementById("header-date").textContent =
        now.getFullYear() + "年" + (now.getMonth() + 1) + "月" + now.getDate() + "日" +
        "  星期" + "日一二三四五六"[now.getDay()];
}
setInterval(updateClock, 1000);
updateClock();

/* ---------- 3. 数字滚动动画（从当前值滚动到目标值） ---------- */
function animateNumber(el, target, suffix) {
    var from = parseFloat(el.dataset.value || 0);   // 上一次的值
    var start = null;
    var duration = 1200;
    function step(ts) {
        if (!start) start = ts;
        var t = Math.min((ts - start) / duration, 1);
        var eased = 1 - Math.pow(1 - t, 3);          // 缓出曲线
        var val = from + (target - from) * eased;
        el.textContent = Math.round(val).toLocaleString() + (suffix || "");
        if (t < 1) requestAnimationFrame(step);
        else el.dataset.value = target;              // 动画结束记录当前值
    }
    requestAnimationFrame(step);
}

/* ---------- 4. ECharts 公共配置 ---------- */
var BLUE = "#1e6fff";
var charts = {};                                    // 存放所有图表实例

// 通用文字颜色
var TEXT = { color: "#35618f", fontSize: 14 };

// 初始化一个图表
function initChart(id) {
    var c = echarts.init(document.getElementById(id));
    charts[id] = c;
    return c;
}
// 窗口缩放时重绘所有图表
window.addEventListener("resize", function () {
    Object.values(charts).forEach(function (c) { c.resize(); });
});

/* ---------- 5. 各图表渲染函数（data 为接口返回的数据） ---------- */

// 5.1 地区占比（饼图 <-> 玫瑰图 动态切换）
var regionType = "ring";                            // 当前图表类型
function renderRegion(province) {
    var data = province.slice(0, 10).map(function (p) {
        return { name: p["省份"], value: p["销售额"] };
    });
    var rose = regionType === "rose";
    charts["chart-region"].setOption({
        tooltip: { trigger: "item", formatter: "{b}<br/>销售额: {c}元 ({d}%)" },
        // 环形模式切片上已带省份名，图例是重复信息还挤在底部；玫瑰图模式下
        // 标签关闭，图例才回来（翻页式，10 省放得下）
        legend: rose ? { bottom: 0, type: "scroll", textStyle: TEXT, itemWidth: 14, itemHeight: 14 } : { show: false },
        color: ["#1e6fff", "#38bdf8", "#6fc3ff", "#34c3a2", "#ffb74d", "#7986cb", "#4dd0e1", "#9575cd", "#4fc3f7", "#81c784"],
        series: [{
            type: "pie",
            radius: rose ? ["12%", "68%"] : ["42%", "68%"],   // 玫瑰图/环形图
            roseType: rose ? "radius" : false,
            center: ["50%", "44%"],
            itemStyle: { borderColor: "#fff", borderWidth: 2 },
            label: { show: !rose, color: "#35618f", fontSize: 12, formatter: "{b}" },
            animationDurationUpdate: 800,
            data: data
        }]
    }, true);
}

// 5.2 产品排行（竖向柱状 <-> 横向条形 动态切换）
var productType = "bar";                            // 当前图表类型
function renderProduct(product) {
    var names = product.map(function (p) { return p["产品"]; });
    var values = product.map(function (p) { return p["销售额"]; });
    var horizontal = productType === "hbar";
    charts["chart-product"].setOption({
        tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, valueFormatter: function (v) { return v.toLocaleString() + "元"; } },
        grid: { left: 8, right: 16, top: 28, bottom: 0, containLabel: true },
        xAxis: horizontal
            ? { type: "value", axisLabel: TEXT, splitLine: { lineStyle: { color: "rgba(30,111,255,.12)" } } }
            : { type: "category", data: names, axisLabel: { color: "#35618f", fontSize: 13, interval: 0, rotate: 30 } },
        yAxis: horizontal
            ? { type: "category", data: names, axisLabel: { color: "#35618f", fontSize: 13 } }
            : { type: "value", axisLabel: TEXT, splitLine: { lineStyle: { color: "rgba(30,111,255,.12)" } } },
        series: [{
            type: "bar",
            data: values,
            barWidth: horizontal ? 14 : 22,
            // 蓝色渐变柱体
            itemStyle: {
                borderRadius: horizontal ? [0, 7, 7, 0] : [7, 7, 0, 0],
                color: new echarts.graphic.LinearGradient(0, 0, horizontal ? 1 : 0, horizontal ? 0 : 1,
                    [{ offset: 0, color: "#38bdf8" }, { offset: 1, color: "#1e6fff" }])
            },
            animationDurationUpdate: 800
        }]
    }, true);
}

// 5.3 渠道占比（环形图）
function renderChannel(channel) {
    charts["chart-channel"].setOption({
        tooltip: { trigger: "item", formatter: "{b}<br/>销售额: {c}元 ({d}%)" },
        // 图例去掉：切片标签已带渠道名与百分比，底部图例在窄卡里必溢出
        color: ["#1e6fff", "#38bdf8", "#34c3a2", "#ffb74d", "#9575cd"],
        series: [{
            type: "pie",
            radius: ["32%", "54%"],
            center: ["50%", "46%"],
            itemStyle: { borderColor: "#fff", borderWidth: 3 },
            label: { color: "#35618f", fontSize: 13, edgeDistance: 8, formatter: "{b}\n{d}%" },
            labelLine: { length: 8, length2: 6 },
            data: channel.map(function (c) { return { name: c["渠道"], value: c["销售额"] }; })
        }]
    }, true);
}

// 5.4 中国销售地图 + 涟漪特效（需要先加载地图数据）
var mapCenters = {};                                // 省份名 -> 中心坐标
function loadChinaMap(cb) {
    fetch("/static/map/china.json").then(function (r) { return r.json(); }).then(function (geo) {
        echarts.registerMap("china", geo);
        // 从地图数据中提取各省份中心坐标，用于涟漪散点
        geo.features.forEach(function (f) {
            if (f.properties && f.properties.cp) mapCenters[f.properties.name] = f.properties.cp;
        });
        cb();
    });
}
function renderMap(province) {
    // 地图区域数据
    var mapData = province.map(function (p) { return { name: p["地图名"], value: p["销售额"] }; });
    // 涟漪散点：取销售额前 10 的省份
    var rippleData = province.slice(0, 10).map(function (p) {
        return { name: p["地图名"], value: (mapCenters[p["地图名"]] || []).concat([p["销售额"]]) };
    });
    charts["chart-map"].setOption({
        tooltip: {
            trigger: "item",
            formatter: function (params) {
                if (!params.value) return params.name + "<br/>暂无数据";
                return params.name + "<br/>销售额: " + Number(params.value).toLocaleString() + "元";
            }
        },
        visualMap: {                                 // 销售额高低 -> 颜色深浅
            min: 0, max: mapData.length ? mapData[0].value : 1,
            left: 16, bottom: 10,
            text: ["高", "低"],
            textStyle: TEXT,
            inRange: { color: ["#cfe6ff", "#6fc3ff", "#1e6fff"] },
            calculable: false
        },
        geo: {                                       // 地图坐标系（供地图和涟漪散点共用）
            map: "china", roam: false,
            zoom: 1.12, top: 46,
            itemStyle: { borderColor: "#fff", borderWidth: 1, areaColor: "#dbeeff" },
            emphasis: { label: { color: "#0d47a1" }, itemStyle: { areaColor: "#38bdf8" } },
            select: { disabled: true }
        },
        series: [
            {   // 地图系列绑定 geo 组件，配合 visualMap 按销售额上色
                type: "map", geoIndex: 0,
                data: mapData
            },
            {   // 涟漪特效散点
                type: "effectScatter", coordinateSystem: "geo",
                geoIndex: 0, zlevel: 2,
                symbolSize: function (val) { return Math.max(8, val[2] / mapData[0].value * 26); },
                rippleEffect: { brushType: "stroke", scale: 3 },   // 涟漪
                itemStyle: { color: "#ff9f43", shadowBlur: 10, shadowColor: "#ff9f43" },
                label: { show: true, position: "right", formatter: "{b}", color: "#1554ad", fontSize: 12 },
                data: rippleData
            }
        ]
    }, true);
}

// 5.5 月度销售趋势（面积折线图）
function renderMonth(monthly) {
    var months = monthly.map(function (m) { return m.month; });
    charts["chart-month"].setOption({
        tooltip: { trigger: "axis", valueFormatter: function (v) { return Number(v).toLocaleString() + "元"; } },
        legend: { top: 0, textStyle: TEXT },
        grid: { left: 8, right: 20, top: 34, bottom: 0, containLabel: true },
        xAxis: { type: "category", boundaryGap: false, data: months, axisLabel: { color: "#35618f", fontSize: 12 } },
        yAxis: { type: "value", axisLabel: TEXT, splitLine: { lineStyle: { color: "rgba(30,111,255,.12)" } } },
        series: [
            {
                name: "销售额", type: "line", smooth: true, data: monthly.map(function (m) { return m["销售额"]; }),
                symbolSize: 7,
                itemStyle: { color: "#1e6fff" },
                lineStyle: { width: 3, color: "#1e6fff" },
                areaStyle: {  // 蓝色渐变面积
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1,
                        [{ offset: 0, color: "rgba(30,111,255,.35)" }, { offset: 1, color: "rgba(30,111,255,0)" }])
                }
            },
            {
                name: "利润", type: "line", smooth: true, data: monthly.map(function (m) { return m["利润"]; }),
                symbolSize: 7,
                itemStyle: { color: "#34c3a2" },
                lineStyle: { width: 3, color: "#34c3a2" },
                areaStyle: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1,
                        [{ offset: 0, color: "rgba(52,195,162,.3)" }, { offset: 1, color: "rgba(52,195,162,0)" }])
                }
            }
        ]
    }, true);
}

/* ---------- 6. 销售明细轮播列表 ---------- */
function renderDetails(details) {
    var box = document.getElementById("detail-list");
    var rows = details.map(function (d) {
        return '<div class="detail-row">' +
            '<span class="c-date">' + d["日期"] + '</span>' +
            '<span class="c-prov">' + d["省份"] + '</span>' +
            '<span class="c-prod">' + d["产品"] + '</span>' +
            '<span class="c-ch">' + d["渠道"] + '</span>' +
            '<span class="c-num">' + d["销量"].toLocaleString() + '</span>' +
            '<span class="c-num hl">' + d["销售额"].toLocaleString() + '</span>' +
            '</div>';
    }).join("");
    // 内容复制一份，实现无缝循环滚动（CSS 动画位移 50%）
    box.innerHTML = rows + rows;
    box.classList.remove("animate");
    void box.offsetWidth;                            // 强制重绘以重启动画
    box.classList.add("animate");
}

/* ---------- 7. 利润进度条 + 地区热榜 ---------- */
function renderProfit(province) {
    var top5 = province.slice(0, 5);
    var maxProfit = top5[0]["利润"];
    document.getElementById("profit-list").innerHTML = top5.map(function (p) {
        return '<div class="progress-item">' +
            '<div class="progress-top"><b>' + p["省份"] + '</b><span>利润 ' + p["利润"].toLocaleString() + '元</span></div>' +
            '<div class="progress-track"><div class="progress-bar" data-w="' + (p["利润"] / maxProfit * 100) + '"></div></div>' +
            '</div>';
    }).join("");
    animateBars(".progress-bar");                    // 触发进度动画
}

function renderHot(province) {
    var top10 = province.slice(0, 10);
    var maxSales = top10[0]["销售额"];
    document.getElementById("hot-list").innerHTML = top10.map(function (p, i) {
        return '<div class="hot-item">' +
            '<span class="hot-rank">' + (i + 1) + '</span>' +
            '<span class="hot-name">' + p["省份"] + '</span>' +
            '<div class="hot-bar-track"><div class="hot-bar" data-w="' + (p["销售额"] / maxSales * 100) + '"></div></div>' +
            '<span class="hot-val">' + (p["销售额"] / 10000).toFixed(1) + '万</span>' +
            '</div>';
    }).join("");
    animateBars(".hot-bar");
}

// 进度条动画：先归零，下一帧再设置目标宽度，触发 CSS transition
function animateBars(selector) {
    var bars = document.querySelectorAll(selector);
    bars.forEach(function (b) { b.style.width = "0"; });
    requestAnimationFrame(function () {
        requestAnimationFrame(function () {
            bars.forEach(function (b) { b.style.width = b.dataset.w + "%"; });
        });
    });
}

/* ---------- 8. 图表类型切换按钮 ---------- */
document.getElementById("toggle-region").onclick = function () {
    regionType = regionType === "ring" ? "rose" : "ring";
    this.textContent = regionType === "ring" ? "环形图" : "玫瑰图";
};
document.getElementById("toggle-product").onclick = function () {
    productType = productType === "bar" ? "hbar" : "bar";
    this.textContent = productType === "bar" ? "横向条形" : "纵向柱状";
};

/* ---------- 9. 拉取数据并刷新全屏 ---------- */
// 显示 / 隐藏"暂无数据"遮罩
function setEmpty(show) {
    document.getElementById("empty-overlay").style.display = show ? "flex" : "none";
}

function refresh() {
    fetch("/api/dashboard").then(function (r) {
        // 未登录 / 会话过期：接口返回 401，直接跳登录页，避免整屏空白
        if (r.status === 401) {
            window.location.href = "/login";
            return null;
        }
        return r.json();
    }).then(function (data) {
        if (!data) return;
        // 数据文件被删除：显示"暂无数据"遮罩，清空各图表
        if (data.empty || !data.kpi || !data.kpi.total_sales) {
            setEmpty(true);
            renderRegion([]); renderProduct([]); renderChannel([]);
            renderMap([]); renderMonth([]); renderDetails([]);
            ["kpi-sales", "kpi-profit", "kpi-volume", "kpi-orders", "kpi-avg", "kpi-rate"]
                .forEach(function (id) { animateNumber(document.getElementById(id), 0); });
            document.getElementById("profit-list").innerHTML = "";
            document.getElementById("hot-list").innerHTML = "";
            return;
        }
        setEmpty(false);

        // 顶部核心指标数字滚动
        animateNumber(document.getElementById("kpi-sales"), data.kpi.total_sales);
        animateNumber(document.getElementById("kpi-profit"), data.kpi.total_profit);
        animateNumber(document.getElementById("kpi-volume"), data.kpi.total_volume);
        animateNumber(document.getElementById("kpi-orders"), data.kpi.order_count);
        animateNumber(document.getElementById("kpi-avg"), data.kpi.avg_order);
        animateNumber(document.getElementById("kpi-rate"), data.kpi.profit_rate);

        renderRegion(data.province);
        renderProduct(data.product);
        renderChannel(data.channel);
        renderMap(data.province);
        renderMonth(data.monthly);
        renderDetails(data.details);
        renderProfit(data.province);
        renderHot(data.province);
    });
}

/* ---------- 10. 启动 ---------- */
// 先初始化所有图表容器（含地图，等地图数据加载完）
["chart-region", "chart-product", "chart-channel", "chart-month", "chart-map"].forEach(initChart);
loadChinaMap(refresh);                               // 地图加载完成后首次渲染

// 定时刷新：每 30 秒拉取一次最新数据（后端有 ±3% 模拟波动）
setInterval(refresh, 30000);
