/* =========================================================
   运行编排屏（台账 W3-01）
   数据全部来自 /api/orchestration/run —— 图来自 core/team.py 的
   pipeline()，状态来自 runs/<run_id>.jsonl，屏上不重算任何账。
   ========================================================= */

/* ---------- 0. 与主大屏同一套缩放：1920 定版，等比居中 ---------- */
function fitScreen() {
    var screen = document.getElementById("screen");
    var scale = Math.min(window.innerWidth / 1920, window.innerHeight / 1080);
    screen.style.transform = "translate(-50%, -50%) scale(" + scale + ")";
}
window.addEventListener("resize", fitScreen);
fitScreen();

/* ---------- 1. 工具 ---------- */
function esc(s) {
    return String(s === null || s === undefined ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
}
function ms(v) {
    if (v === null || v === undefined) return "—";
    if (v < 1) return "<1ms";
    if (v < 1000) return Math.round(v) + "ms";
    return (v / 1000).toFixed(2) + "s";
}
function num(v) {
    if (v === null || v === undefined) return "—";
    return Number(v).toLocaleString();
}
function clock() {
    var now = new Date();
    var p = function (n) { return n < 10 ? "0" + n : n; };
    document.getElementById("orch-time").textContent =
        p(now.getHours()) + ":" + p(now.getMinutes()) + ":" + p(now.getSeconds());
    document.getElementById("orch-date").textContent =
        now.getFullYear() + "年" + (now.getMonth() + 1) + "月" + now.getDate() + "日" +
        "  星期" + "日一二三四五六"[now.getDay()];
}
setInterval(clock, 1000);
clock();

/* ---------- 2. 状态词 ---------- */
var STATUS_TEXT = {
    ok: "通过", fail: "打回", error: "异常", timeout: "超时",
    skip: "跳过", pending: "待跑", running: "进行中", retry: "重跑中"
};
function stText(s) { return STATUS_TEXT[s] || s || "待跑"; }

/* ---------- 3. 中栏：任务图（DAG / 分层列表，同一份数据） ---------- */

var STATUS_COLOR = {
    ok: "#2eb872", fail: "#e2574c", error: "#a33327",
    timeout: "#e8963c", skip: "#9aa9bd",
    pending: "#c3d2e6", running: "#1e6fff", retry: "#e8963c"
};

var DAG = null;

function ensureChart() {
    if (typeof echarts === "undefined") return null;
    if (!DAG) {
        DAG = echarts.init(document.getElementById("dag-canvas"), null,
                           { renderer: "svg" });
        DAG.on("click", function (p) {
            if (p.dataType === "node" && p.data && p.data.id) {
                showNodeDetail(p.data.id);
            }
        });
        window.addEventListener("resize", function () { DAG.resize(); });
        // 演示与验收时可以直接读 window.__orchChart.getOption() 看画了什么
        window.__orchChart = DAG;
    }
    return DAG;
}

/* 坐标由后端的 layer 决定：换一份图定义，节点自动重排，前端一行都不用改。 */
function dagOption(d) {
    var el = document.getElementById("dag-canvas");
    var W = el.clientWidth || 900;
    var H = el.clientHeight || 420;
    var g = d.graph || {};
    var gnodes = g.nodes || [];
    var full = {};
    (d.nodes || []).forEach(function (n) { full[n.id] = n; });

    var byLayer = {};
    gnodes.forEach(function (n) {
        (byLayer[n.layer] = byLayer[n.layer] || []).push(n);
    });
    var L = Math.max(1, g.layer_count || Object.keys(byLayer).length);
    var padX = 76, padY = 58;
    var spanX = L > 1 ? (W - padX * 2) / (L - 1) : 0;
    var pos = {};
    gnodes.forEach(function (n) {
        var same = byLayer[n.layer] || [n];
        var i = same.indexOf(n);
        var spanY = (H - padY * 2) / (same.length + 1);
        var y = same.length === 1 ? H / 2 : padY + spanY * (i + 1);
        pos[n.id] = [L > 1 ? padX + spanX * n.layer : W / 2, y];
    });

    var data = gnodes.map(function (n) {
        var f = full[n.id] || {};
        var retry = (f.attempts || 0) > 1 ? " ×" + f.attempts : "";
        return {
            id: n.id,
            name: n.id,
            x: pos[n.id][0], y: pos[n.id][1],
            // cartesian2d 下 ECharts 读 value 当坐标；symbol 按像素画，
            // 不会像 layout:none 那样被坐标映射一起拉成椭圆。
            value: [pos[n.id][0], pos[n.id][1]],
            symbolSize: n.id === "gate" ? 64 : 56,
            duration_ms: f.duration_ms || 0,
            itemStyle: nodeStyle(f.status),
            label: { formatter: (n.label || n.id) + retry },
            _node: f
        };
    });

    // 前进边从 graph.forward_edges 来（后端按 needs 派生），回边从 back_edges 来。
    // 前端不自己推依赖 —— 推一遍就等于把图的形状复制到前端，换图时两边都得改。
    var links = (g.forward_edges || []).map(function (e) {
        return {
            source: e.from, target: e.to,
            lineStyle: { color: "#9fb2c9", width: 2, opacity: .9 }
        };
    }).concat((g.back_edges || []).map(function (e) {
        return {
            source: e.from, target: e.to,
            lineStyle: { color: "#e2574c", width: 2, type: "dashed", curveness: .3 },
            label: { show: true, formatter: e.label || "回边",
                     color: "#a33327", fontSize: 11 }
        };
    }));

    return {
        animationDuration: 420,
        tooltip: {
            trigger: "item",
            formatter: function (p) {
                if (p.dataType === "edge") {
                    return esc((p.data && p.data.source) || "") + " → " +
                           esc((p.data && p.data.target) || "");
                }
                var n = (p.data && p.data._node) || {};
                return nodeTip(n, p.name);
            }
        },
        grid: { left: 0, top: 0, right: 0, bottom: 0, containLabel: false },
        xAxis: { type: "value", min: 0, max: W, show: false },
        yAxis: { type: "value", min: 0, max: H, inverse: true, show: false },
        series: [{
            // 固定 id 是实时刷新的前提：paintDag() 靠 id "dag" 找这个系列并合并，
            // 没有 id 时 setOption 找不到目标，整次调用被当成新系列丢掉 ——
            // 接口一直在报"谁在跑"，屏上却一帧都不变色（踩过）。
            id: "dag",
            type: "graph",
            coordinateSystem: "cartesian2d",
            layout: "none",
            roam: false,
            draggable: false,
            data: data, links: links,
            edgeSymbol: ["none", "arrow"],
            edgeSymbolSize: 8,
            label: {
                show: true, position: "bottom", distance: 7,
                color: "#1d3a5f", fontSize: 12, fontWeight: 600
            },
            lineStyle: { color: "#9fb2c9", width: 2, curveness: 0, opacity: .9 },
            emphasis: { focus: "adjacency", lineStyle: { width: 3 } }
        }]
    };
}

/* 节点样式集中在一处：初始渲染和"只改颜色"都调它，避免两套色值。 */
function nodeStyle(status) {
    var hot = status === "running" || status === "retry";
    return {
        color: STATUS_COLOR[status] || STATUS_COLOR.pending,
        borderColor: "#fff", borderWidth: 2,
        // 正在跑的节点自带光晕，扫一眼就知道卡在哪一格
        shadowBlur: hot ? 22 : 12,
        shadowColor: status === "retry"
            ? "rgba(232,150,60,.60)"
            : (hot ? "rgba(30,111,255,.55)" : "rgba(30,111,255,.28)")
    };
}

function nodeTip(n, fallbackName) {
    return '<b>' + esc(n.label || fallbackName) + '</b>　' + esc(stText(n.status)) +
        '<br>优先级 ' + esc(n.priority) + ' · 耗时 ' + esc(ms(n.duration_ms)) +
        ' · 第 ' + esc(n.attempts || 0) + ' 轮' +
        (n.round > 1 ? '（当前整体第 ' + esc(n.round) + ' 轮）' : '') +
        '<br>依赖：' + esc((n.needs || []).join(" · ") || "无（入口节点）");
}

function renderDag(d) {
    var chart = ensureChart();
    if (!chart) {
        document.getElementById("dag-wrap").innerHTML =
            '<div class="credit-empty">echarts 未加载，DAG 图不可用；请切到分层列表。</div>';
        return;
    }
    chart.setOption(dagOption(d), true);
}

function showNodeDetail(id) {
    // W3-04：点节点弹出抽屉，读 /api/orchestration/node 取这一节点的全部证据。
    // 底部的单行摘要照旧更新 —— 投屏时不一定有空点开抽屉，
    // 但"第 3 个 Agent、第 2 次重试、1.8 秒"这句话必须随时能念出来。
    var box = document.getElementById("node-detail");
    var n = (CURRENT && CURRENT.nodes || []).filter(function (x) {
        return x.id === id;
    })[0];
    if (!n) return;
    if (box) {
        box.innerHTML = '<span class="nd-tag">选中节点</span> <b>' + esc(n.label) +
            '</b>（' + esc(n.id) + '）· ' + esc(stText(n.status)) +
            ' · 优先级 ' + esc(n.priority) +
            ' · 耗时 ' + esc(ms(n.duration_ms)) +
            ' · 第 ' + esc(n.attempts || 0) + ' 轮' +
            ' · 依赖 ' + esc((n.needs || []).join(" · ") || "无") +
            ' · <a href="#" id="nd-open">打开详情 →</a>';
        var open = document.getElementById("nd-open");
        if (open) open.addEventListener("click", function (e) {
            e.preventDefault();
            openDrawer(id);
        });
    }
    openDrawer(id);
}

/* ---------- 3c. 节点抽屉（W3-04） ---------- */

var DRAWER_NODE = null;

function openDrawer(id) {
    var wrap = document.getElementById("drawer");
    if (!wrap) return;
    DRAWER_NODE = id;
    wrap.classList.add("on");
    wrap.setAttribute("aria-hidden", "false");
    document.body.classList.add("drawer-open");
    var body = document.getElementById("drawer-body");
    body.innerHTML = '<div class="dw-loading">正在取 ' + esc(id) + ' 的证据…</div>';
    var rid = CURRENT && CURRENT.run && CURRENT.run.run_id;
    if (!rid) {
        body.innerHTML = '<div class="dw-empty">还没有选中任何一次运行。</div>';
        return;
    }
    fetch("/api/orchestration/node?run_id=" + encodeURIComponent(rid) +
          "&node=" + encodeURIComponent(id), { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            if (DRAWER_NODE !== id) return;      // 期间又点了别的节点，丢掉旧响应
            renderDrawer(d);
        })
        .catch(function (e) {
            body.innerHTML = '<div class="dw-empty">取不到：' + esc(e.message) + '</div>';
        });
}

function closeDrawer() {
    var wrap = document.getElementById("drawer");
    if (!wrap) return;
    DRAWER_NODE = null;
    wrap.classList.remove("on");
    wrap.setAttribute("aria-hidden", "true");
    document.body.classList.remove("drawer-open");
}

function dwRow(k, v) {
    return '<div class="dw-row"><span class="dw-k">' + esc(k) + '</span>' +
           '<span class="dw-v">' + v + '</span></div>';
}

function renderDrawer(d) {
    var body = document.getElementById("drawer-body");
    if (!d || !d.ok) {
        body.innerHTML = '<div class="dw-empty">' + esc((d && d.error) || "取不到这个节点") + '</div>';
        return;
    }
    var n = d.node, r = d.retry || {};
    // 「第 3 个 Agent 在第 2 次重试时花了 1.8 秒」—— 这句话要能照着念，
    // 所以这几个数字放在最上面一行，且用同一套单位。
    var sentence = n.order_text + ' ' + n.label + '，' +
        (n.visits > 1 ? '第 ' + n.visits + ' 次执行（重试 ' + r.count + ' 次）' : '第 1 次执行') +
        '，耗时 ' + ms(n.duration_ms);

    var html = '';
    html += '<div class="dw-hero">' +
        '<div class="dw-hero-top"><b>' + esc(n.label) + '</b>' +
        '<span class="dw-st" data-status="' + esc(n.status) + '">' +
        esc(stText(n.status)) + '</span></div>' +
        '<div class="dw-sentence">' + esc(sentence) + '</div>' +
        '<div class="dw-hero-meta">' +
        '<span>' + esc(n.order_text) + '</span>' +
        '<span>' + esc(n.visit_text) + '</span>' +
        '<span>' + esc(n.agent) + '</span>' +
        '<span>P' + esc(n.priority) + '</span></div></div>';

    html += '<div class="dw-sec"><h4>输入</h4><div class="dw-text">' +
        esc(n.input) + '</div></div>';

    html += '<div class="dw-sec"><h4>输出</h4>';
    html += n.output.length
        ? '<ul class="dw-list">' + n.output.map(function (x) {
            return '<li>' + esc(x) + '</li>';
        }).join('') + '</ul>'
        : '<div class="dw-text dim">这一步没有产出字段（只做了决定，见下方事件流）</div>';
    html += '</div>';

    html += '<div class="dw-sec"><h4>耗时与 token</h4>' +
        dwRow('开始 / 结束', esc(ms(n.started_ms)) + ' → ' + esc(ms(n.ended_ms))) +
        dwRow('本节点耗时', '<b>' + esc(ms(n.duration_ms)) + '</b>') +
        dwRow('token', esc(n.tokens['in']) + ' in / ' + esc(n.tokens['out']) + ' out' +
              (n.tokens.calls ? '（' + esc(n.tokens.calls) + ' 次模型调用）' : '')) +
        dwRow('全次运行 token', esc(n.run_tokens['in']) + ' in / ' +
              esc(n.run_tokens['out']) + ' out') + '</div>';

    if (n.tokens.by_retry && Object.keys(n.tokens.by_retry).length) {
        html += '<div class="dw-sec"><h4>token 按轮拆开</h4><table class="dw-tbl">' +
            '<tr><th>第几轮</th><th>in</th><th>out</th><th>调用</th></tr>' +
            Object.keys(n.tokens.by_retry).sort().map(function (k) {
                var v = n.tokens.by_retry[k];
                return '<tr><td>第 ' + (parseInt(k, 10) + 1) + ' 轮</td><td>' + v['in'] +
                    '</td><td>' + v.out + '</td><td>' + v.calls + '</td></tr>';
            }).join('') + '</table></div>';
    }

    if (r.count > 0) {
        html += '<div class="dw-sec dw-retry"><h4>为什么重试了 ' + r.count + ' 次</h4>';
        html += '<div class="dw-text"><b>' + esc(r.kind || '—') + '</b>　' +
            esc(r.reason || '（没有留下诊断原文）') + '</div>';
        if (r.signals && r.signals.length) {
            html += '<ul class="dw-list sig">' + r.signals.map(function (s) {
                return '<li>' + esc(s) + '</li>';
            }).join('') + '</ul>';
        }
        if (r.strategy) {
            html += '<div class="dw-text dim">下一轮策略：<code>' +
                esc(r.strategy) + '</code></div>';
        }
        (r.loops || []).forEach(function (lp) {
            html += '<div class="dw-text dim">打回边 <code>' + esc(lp.edge) +
                '</code>，作废 ' + esc((lp.cleared || []).join('、')) +
                '，上限 ' + esc(lp.max_loops) + ' 次</div>';
        });
        html += '</div>';
    }

    html += '<div class="dw-sec"><h4>这个节点的日志（' + d.events.length + ' 条）</h4>' +
        '<div class="dw-log">' + (d.events.length ? d.events.map(function (e) {
            return '<div class="dw-log-row" data-ev="' + esc(e.event) + '">' +
                '<span class="dw-log-t">' + esc((e.at || '').slice(11)) + '</span>' +
                '<span class="dw-log-e">' + esc(e.event) + '</span>' +
                '<span class="dw-log-x">' + esc(evText(e)) + '</span></div>';
        }).join('') : '<div class="dw-text dim">这一节点没有留下事件</div>') + '</div></div>';

    body.innerHTML = html;
}

/* 事件原文翻成人话：抽屉里给的是"判定结论"，不是字段名。 */
function evText(e) {
    var d = e.detail || {};
    switch (e.event) {
    case 'node_begin': return '开始执行（第 ' + (d.visit || 1) + ' 次）';
    case 'node_end':   return '执行结束，结果见下一条';
    case 'graph_node': return '结论 ' + (d.status || '') + '，耗时 ' + ms(d.duration_ms);
    case 'verify':     return (d.verdict === 'pass' ? '通过' : '不通过') +
                              '，回查 ' + d.checked + ' 个数字' +
                              ((d.unsupported || []).length ? '，无据：' + d.unsupported.join('、') : '');
    case 'critic':     return (d.verdict === 'pass' ? '通过' : '不通过') +
                              '，高严重 ' + d.high + ' 条' +
                              ((d.issues || []).length ? '：' + d.issues.map(function (i) {
                                  return i.type; }).join('、') : '');
    case 'draft_prompt': return d.note || '注入提示';
    case 'diagnosis':  return (d.label || '') + ' · ' + (d.strategy || '') + ' · ' + (d.why || '');
    case 'graph_loop': return '打回 ' + (d.edge || '') + '（第 ' + d.attempt + ' 轮）';
    case 'scheduler_start': return '取到槽位 ' + (d.slot || '') + '，等了 ' + ms(d.waited_ms);
    case 'scheduler_done':  return '让出槽位，耗时 ' + ms(d.duration_ms);
    case 'curator_reject':  return '成文引入无据数字，退回初稿';
    default:
        var keys = Object.keys(d).filter(function (k) { return k !== 'node'; });
        return keys.slice(0, 3).map(function (k) { return k + '=' + d[k]; }).join(' · ');
    }
}

function renderGraph(d) {
    var nodes = d.nodes || [];
    var laneWrap = document.getElementById("lane-wrap");
    var meta = document.getElementById("graph-meta");
    var foot = document.getElementById("graph-foot");

    meta.innerHTML =
        '<span>图名 <b>' + esc(d.graph.name || "—") + '</b></span>' +
        '<span>版本 <b>v' + esc(d.graph.version || "?") + '</b></span>' +
        '<span>节点 <b>' + nodes.length + '</b></span>' +
        '<span>层数 <b>' + esc(d.graph.layer_count || 0) + '</b></span>' +
        '<span>前进边 <b>' + (d.graph.forward_edges || []).length + '</b></span>' +
        '<span>回边 <b>' + (d.graph.back_edges || []).length + '</b></span>' +
        '<span>来源 <b>' + esc(INDUSTRY_SRC) + '</b></span>';

    if (!nodes.length) {
        laneWrap.innerHTML = '<div class="credit-empty">这次运行没有任务图。</div>';
        foot.innerHTML = "";
        return;
    }

    // 视图二：分层列表，读数值更清楚（W3-01 的产物继续有用）
    laneWrap.innerHTML = laneHtml(nodes);

    // 视图一：DAG 图
    renderDag(d);

    var edges = d.graph.edges || [];
    foot.innerHTML = (edges.length
        ? edges.map(function (e) {
            return '<b>' + esc(e.from) + '</b> <span style="color:#e2574c">⇠</span> <b>' +
                   esc(e.to) + '</b>（' + esc(e.when) + '，上限 ' + esc(e.max_loops) +
                   ' 次）<span class="loop-tag">' + esc(e.label || "回边") + '</span>';
        }).join("") + '<br>'
        : "") +
        '实线箭头 = 前进（靠 needs）· 红色虚线 = 打回（靠 edges）。点节点看详情。' +
        '<div class="node-detail" id="node-detail"></div>';
}

function laneHtml(nodes) {
    var byLayer = {};
    nodes.forEach(function (n) {
        (byLayer[n.layer || 0] = byLayer[n.layer || 0] || []).push(n);
    });
    var html = "";
    Object.keys(byLayer).sort(function (a, b) { return a - b; }).forEach(function (L) {
        html += '<div class="lane"><div class="lane-no">L' + esc(L) + '</div>' +
                '<div class="lane-items">';
        byLayer[L].forEach(function (n) {
            var dep = (n.needs && n.needs.length) ? "↑ " + n.needs.join(" · ") : "入口节点";
            var multi = n.attempts > 1 ? ' <span class="node-multi">×' + n.attempts + '</span>' : "";
            // 第几轮只在重试过之后才出现：一路顺风跑完还挂个"第 1 轮"是噪音
            var round = n.round > 1
                ? '<span class="node-round">第 ' + esc(n.round) + ' 轮</span>' : "";
            html +=
                '<div class="node" data-status="' + esc(n.status) +
                    '" data-node="' + esc(n.id) + '">' + round +
                '<div class="node-pri">P' + esc(n.priority !== null && n.priority !== undefined ? n.priority : "—") + '</div>' +
                '<div class="node-name">' + esc(n.label) + multi + '</div>' +
                '<div class="node-sub">' + esc(n.id) + ' · ' + esc(n.agent) +
                ' · ' + esc(ms(n.duration_ms)) + '</div>' +
                '<div class="node-dep">' + esc(dep) + '</div>' +
                '</div>';
        });
        html += '</div></div>';
    });
    return html;
}

/* ---------- 3b. 实时刷新：只改颜色，不重建图（W3-03） ---------- */

/* 上一次的节点快照，用来看"谁刚从待跑变成在跑、谁刚重试" */
var LAST = {};

function paintDag(nodes) {
    var chart = ensureChart();
    if (!chart) return;
    // graph 系列的 data 是**整组替换**，不按 id 做合并：只传一个节点，
    // 图上就只剩那一个（实测 6 个变 1 个）。所以每次都要把 6 个一起送，
    // 只把这一轮变色的那个节点换成新样式，坐标与连线全部沿用现有值。
    var cur = ((chart.getOption().series || [])[0] || {}).data || [];
    if (!cur.length) return;
    var byId = {};
    nodes.forEach(function (n) { byId[n.id] = n; });
    var data = cur.map(function (d) {
        var n = byId[d.id];
        if (!n) return d;
        var retry = n.attempts > 1 ? " ×" + n.attempts : "";
        // 浅拷贝：cur 是 ECharts 内部对象，就地改会让"对比前后"永远相等
        return Object.assign({}, d, {
            itemStyle: nodeStyle(n.status),
            label: Object.assign({}, d.label, {
                formatter: (n.label || d.id) + retry
            }),
            _node: n
        });
    });
    // 一次 setOption 送整组：id 固定为 "dag" 才会合并进同一个系列，
    // 不固定就会被当成新系列追加，屏上出现两张图叠在一起。
    chart.setOption({ series: [{ id: "dag", data: data }] }, { lazyUpdate: false });
}

function flashRetry(id, times) {
    // 重试闪烁：同层节点可能不止一个，找 data-node 而不是按位置取。
    var el = document.querySelector('.node[data-node="' + id + '"]');
    if (!el) return;
    el.classList.add("node-retry-flash");
    setTimeout(function () { el.classList.remove("node-retry-flash"); }, 1400);
    if (times) showNodeDetail(id);
}

function applySnapshot(d) {
    var nodes = d.nodes || [];
    var changed = [];
    var hadAny = Object.keys(LAST).length > 0;

    nodes.forEach(function (n) {
        var prev = LAST[n.id];
        // 轮次涨了才算"重试"：只按 attempts 涨判断会漏掉"同一轮里又跑了一遍"
        var retried = prev && ((n.attempts || 0) > (prev.attempts || 0) ||
                               (n.round || 1) > (prev.round || 1));
        if (!prev || prev.status !== n.status || retried) {
            changed.push({ id: n.id, from: prev && prev.status, to: n.status, retried: !!retried });
        }
    });
    nodes.forEach(function (n) { LAST[n.id] = n; });

    // 首帧必须整图染色：LAST 为空时每个节点都是"新来的"，但图上
    // 可能已经把上一遍的绿画好了（切 run、刷新页面后的第一次轮询）。
    // 只给"变化的"上色，屏上就一直停在上一遍的颜色上。
    var firstSnapshot = !hadAny;
    hadAny = true;

    if (!changed.length && !firstSnapshot) return changed;

    // 图：只改颜色与标签
    paintDag(nodes);

    // 列表：整块重画更省事，且它不在主视觉上，闪一下不刺眼
    var laneWrap = document.getElementById("lane-wrap");
    if (laneWrap) laneWrap.innerHTML = laneHtml(nodes);

    // 重试的节点闪一下，并说明第几次
    changed.filter(function (c) { return c.retried; }).forEach(function (c) {
        var n = nodes.filter(function (x) { return x.id === c.id; })[0];
        flashRetry(c.id, n && n.attempts);
        pushLive("重试", n ? (n.label + " 第 " + (n.round || n.attempts) + " 轮") : c.id, "warn");
    });
    changed.filter(function (c) {
        return !c.retried && c.to === "running";
    }).forEach(function (c) {
        pushLive("开始", c.id, "run");
    });
    changed.filter(function (c) {
        return !c.retried && c.to === "retry";
    }).forEach(function (c) {
        pushLive("打回", c.id + " 正在重做", "warn");
    });
    changed.filter(function (c) {
        return !c.retried && c.to && c.to !== "running" && c.to !== "retry" && c.to !== "pending";
    }).forEach(function (c) {
        pushLive("结束", c.id + " → " + stText(c.to),
                 c.to === "ok" ? "ok" : (c.to === "timeout" ? "warn" : "bad"));
    });

    // 队列与时间线跟着刷新：它们是"谁在排队"的直接证据
    renderQueue(d);
    renderTimeline(d);
    return changed;
}

function pushLive(kind, text, cls) {
    var box = document.getElementById("live-strip");
    if (!box) return;
    var row = document.createElement("span");
    row.className = "live-item live-" + (cls || "dim");
    row.innerHTML = '<b>' + esc(kind) + '</b> ' + esc(text);
    box.appendChild(row);
    while (box.children.length > 4) box.removeChild(box.firstChild);
}

/* ---------- 4. 左栏：调度队列 ---------- */
function renderQueue(d) {
    var body = document.getElementById("q-body");
    var note = document.getElementById("q-note");
    var rows = d.schedule || [];

    if (!rows.length) {
        body.innerHTML = '<div class="credit-empty">这次运行没有争槽记录。</div>';
        note.textContent = "单节点批次不进队列，直接执行 —— 只有同一批有多个节点时才需要排。";
        return;
    }
    body.innerHTML = rows.map(function (r) {
        var hi = (r.priority || 0) >= 90 ? " pri-hi" : "";
        return '<div class="q-row">' +
            '<span class="q-c-job">' + esc(r.job) + '</span>' +
            '<span class="q-c-pri"><i class="pri-badge' + hi + '">' +
                esc(r.priority !== null && r.priority !== undefined ? r.priority : "—") + '</i></span>' +
            '<span class="q-c-slot">' + esc(r.slot || "—") + '</span>' +
            '<span class="q-c-wait">' + esc(ms(r.waited_ms)) + '</span>' +
            '<span class="q-c-cost">' + esc(ms(r.duration_ms)) + '</span>' +
            '<span class="q-c-st"><i class="st-dot st-' + esc(r.status) + '"></i>' +
                esc(stText(r.status)) + '</span>' +
            '</div>';
    }).join("");

    note.textContent = "排队时间是等槽位的时间，不含执行；槽位只有 "
        + (rows[0].slot ? String(rows[0].slot).split("/")[1] : "n")
        + " 个，优先级高的先拿。";
}

/* ---------- 5. 左栏：批次 ---------- */
function renderWaves(d) {
    var body = document.getElementById("wave-body");
    var waves = d.waves || [];
    if (!waves.length) {
        body.innerHTML = '<div class="credit-empty">这次运行每个节点都是单独一批，没有并发。</div>';
        return;
    }
    body.innerHTML = waves.map(function (w, i) {
        var lead = (w.order || [])[0];
        var chips = (w.nodes || []).map(function (id) {
            return '<span class="wave-chip' + (id === lead ? " lead" : "") + '">' + esc(id) + '</span>';
        }).join("");
        return '<div class="wave-item">' +
            '<div class="wave-top"><span>第 ' + (i + 1) + ' 批 · 先跑 <b>' + esc(lead || "—") +
            '</b></span><span>' + esc(w.slots || 0) + ' 槽位</span></div>' +
            '<div class="wave-nodes">' + chips + '</div>' +
            '</div>';
    }).join("");
}

/* ---------- 6. 中栏：时间线 ---------- */
function renderTimeline(d) {
    var body = document.getElementById("tl-body");
    var rows = d.timeline || [];
    if (!rows.length) {
        body.innerHTML = '<div class="credit-empty">这次运行没有可读的事件。</div>';
        return;
    }
    body.innerHTML = rows.map(function (t) {
        return '<div class="tl-row tl-' + esc(t.kind) + '">' +
            '<span class="tl-seq">' + esc(t.seq) + '</span>' +
            '<span class="tl-bar"></span>' +
            '<span class="tl-text">' + esc(t.text) + '</span>' +
            '</div>';
    }).join("");
}

/* ---------- 7. 右栏：降级与诊断 ---------- */
function renderCredit(d) {
    var body = document.getElementById("credit-body");
    var html = "";

    var fb = d.fallbacks || [];
    var switched = fb.filter(function (x) { return x.event === "fallback"; });
    if (switched.length) {
        html += switched.map(function (x) {
            return '<div class="credit-item credit-bad"><b>换过模型</b>：' +
                esc(x.from) + ' → ' + esc(x.to) + '<br>' +
                esc(x.error_type || "") + ' ' + esc(x.status_code || "") +
                '，连续失败 ' + esc(x.consecutive) + ' 次（阈值 ' + esc(x.threshold) +
                '），冷却 ' + esc(x.cooldown_s) + 's</div>';
        }).join("");
    } else if (d.summary.degraded) {
        html += '<div class="credit-item credit-warn">' +
            '<b>发生过降级</b>，但没有留下换人记录 —— 需要查 trace。</div>';
    } else {
        html += '<div class="credit-item credit-good">' +
            '<b>本次未降级</b>：请求始终走主通道，没有换人。</div>';
    }

    // 通道层的失败/恢复也摆出来：只报"换没换"，会漏掉"试了但被跳过的通道"
    var others = fb.filter(function (x) {
        return ["channel_fail", "channel_recovered", "channel_reopen",
                "channel_unavailable", "fallback_exhausted",
                "channel_error_fatal", "fallback_probe"].indexOf(x.event) >= 0;
    });
    if (others.length) {
        html += others.slice(-6).map(function (x) {
            var cls = x.event === "channel_recovered" || x.event === "channel_reopen"
                ? "credit-good"
                : (x.event === "fallback_exhausted" || x.event === "channel_error_fatal"
                    ? "credit-bad" : "");
            var text = {
                channel_fail: "通道 " + esc(x.channel) + " 失败：" +
                    esc(x.error_type || "") + " " + esc(x.status_code || "") +
                    "（连续 " + esc(x.consecutive) + " 次）",
                channel_recovered: "通道 " + esc(x.channel) + " 恢复，连续失败计数清零",
                channel_reopen: "通道 " + esc(x.channel) + " 冷却结束，重新允许尝试",
                channel_unavailable: "通道 " + esc(x.name) + " 未启用：" + esc(x.reason),
                fallback_exhausted: "全部通道失败，本次请求放弃（试过 " +
                    (x.attempts || []).length + " 条）",
                channel_error_fatal: "通道 " + esc(x.channel) + " 报非通道类错误：" +
                    esc(x.error_type) + "，不降级",
                fallback_probe: "全链冷却中，按原顺序半开探测"
            }[x.event];
            return '<div class="credit-item ' + cls + '">' + (text || esc(x.event)) + '</div>';
        }).join("");
    }

    var diag = d.diagnoses || [];
    if (diag.length) {
        html += diag.map(function (x) {
            if (x.event === "handoff") {
                return '<div class="credit-item credit-bad"><b>转人工</b>：跑了 ' +
                    esc(x.attempts) + ' 轮仍不通过，已停止自动修改并交出交接稿</div>';
            }
            return '<div class="credit-item credit-warn">' +
                '<b>第 ' + esc(x.attempt) + ' 轮诊断</b>：' + esc(x.label || x.kind) +
                '<br>策略 ' + esc(x.strategy) + '<br>' +
                esc((x.signals || []).join(" · ") || x.why || "") + '</div>';
        }).join("");
    } else {
        html += '<div class="credit-item credit-good">' +
            '<b>没有打回</b>：两道关卡一次通过，诊断链路未启动。</div>';
    }

    if (!html) {
        html = '<div class="credit-empty">这次运行没有账可看。</div>';
    }
    body.innerHTML = html;
}

/* ---------- 8. 右栏：运行信息 ---------- */
function renderRun(d) {
    var r = d.run || {};
    var tk = r.tokens || {};
    var rows = [
        ["问题", r.question],
        ["run_id", r.run_id, "mono"],
        ["开始", (r.started_at || "").replace("T", " ")],
        ["结束", (r.ended_at || "").replace("T", " ")],
        ["总耗时", ms(r.duration_ms)],
        ["结果", stText(r.status === "ok" ? "ok" : r.status)],
        ["模型步数", r.step_count],
        ["重试轮数", r.retry_count],
        ["Token", "in " + num(tk.in) + " / out " + num(tk.out)],
        ["节点", d.summary.passed + " 通过 / " + d.summary.total + " 总数"]
    ];
    document.getElementById("run-body").innerHTML = rows.map(function (kv) {
        return '<div class="run-row"><span class="run-k">' + esc(kv[0]) +
            '</span><span class="run-v ' + (kv[2] || "") + '">' +
            esc(kv[1] === null || kv[1] === undefined ? "—" : kv[1]) + '</span></div>';
    }).join("");

    var sel = document.getElementById("run-select");
    var runs = d.runs || [];
    sel.innerHTML = runs.map(function (x) {
        return '<option value="' + esc(x.run_id) + '"' +
            (x.run_id === r.run_id ? " selected" : "") + '>' +
            esc((x.started_at || "").replace("T", " ")) + " · " +
            esc((x.question || "").slice(0, 16)) + '</option>';
    }).join("");
}

/* ---------- 9. 底部口径条 ---------- */
function renderFoot(d) {
    var s = d.summary || {};
    var r = d.run || {};
    var tk = r.tokens || {};
    var parts = [
        '<span>节点 <b>' + s.passed + "/" + s.total + '</b> 通过</span>',
        '<span>在跑 <b>' + (s.running || []).length + '</b></span>',
        '<span>打回 <b>' + (s.failed || 0) + '</b></span>',
        '<span>超时 <b>' + (s.timed_out || []).length + '</b></span>',
        '<span>重试 <b>' + (s.retried || []).length + '</b></span>',
        '<span>总耗时 <b>' + ms(r.duration_ms) + '</b></span>',
        '<span>Token <b>' + num(tk.in) + "</b> in / <b>" + num(tk.out) + '</b> out</span>'
    ];
    document.getElementById("orch-foot").innerHTML =
        parts.join('<span class="foot-sep">|</span>') +
        '<span class="foot-right">图：' + esc(INDUSTRY_SRC) + ' · 数据：runs/' +
        esc(r.run_id || "—") + '.jsonl · 屏上不重算</span>';
}

/* ---------- 10. 取数与实时轮询（W3-03） ---------- */
var CURRENT = null;
var TIMER = null;
var POLL_MS = 1000;          // 台账口径：状态变化延迟 < 2s，留一半余量
var LAST_TICK = 0;

function showError(msg) {
    document.getElementById("lane-wrap").innerHTML =
        '<div class="credit-empty">' + esc(msg) + '</div>';
    document.getElementById("graph-meta").textContent = "";
    setView("lane");
}

function setView(view) {
    var dagWrap = document.getElementById("dag-wrap");
    var laneWrap = document.getElementById("lane-wrap");
    dagWrap.style.display = view === "dag" ? "block" : "none";
    laneWrap.style.display = view === "lane" ? "flex" : "none";
    var btns = document.querySelectorAll("#view-toggle .vt");
    for (var i = 0; i < btns.length; i++) {
        btns[i].classList.toggle("active", btns[i].getAttribute("data-view") === view);
    }
    // 从隐藏状态切回来时，echarts 可能是在 0 尺寸下初始化的，重新量一次
    if (view === "dag" && DAG) DAG.resize();
}

function renderLive(d) {
    var el = document.getElementById("live-strip");
    if (!el) return;
    var r = d.run || {};
    var s = d.summary || {};
    var state = r.running
        ? '<span class="live-dot on"></span>实时跟随中 · 每 ' + (POLL_MS / 1000) + 's 刷新'
        : '<span class="live-dot"></span>本次运行已结束 · 已停止轮询';
    var mid = r.running
        ? '<b>' + (s.running || []).length + '</b> 个节点在跑 · <b>' +
          (s.pending || []).length + '</b> 个待跑'
        : '<b>' + s.passed + '/' + s.total + '</b> 通过';
    // 事件驱动的瞬时提示（开始/结束/重试）挂在同一行右边
    var box = el.querySelector(".live-msgs");
    if (!box) {
        el.innerHTML = '<span class="live-state">' + state + '</span>' +
                       '<span class="live-count">' + mid + '</span>' +
                       '<span class="live-msgs"></span>';
    } else {
        el.querySelector(".live-state").innerHTML = state;
        el.querySelector(".live-count").innerHTML = mid;
    }
}

function schedulePoll(runId, running) {
    // 不因为"这一遍结束了"就停：同一个 run_id 还能被追加再跑一遍
    // （投屏的用法就是先开屏、随口再问一个问题）。停了的代价是屏上
    // 永远停在上一遍的全绿，等于实时跟随在第二轮就失效了。
    // 所以只在页面不可见时停 —— 那是唯一"没人看"的情形。
    if (TIMER) { clearTimeout(TIMER); TIMER = null; }
    if (!runId) return;
    if (document.hidden) return;
    TIMER = setTimeout(function () { load(runId, true); }, POLL_MS);
}

// 切回这个标签页时立刻补一次，不用等下一个 1s
document.addEventListener("visibilitychange", function () {
    if (!document.hidden && CURRENT && CURRENT.run && CURRENT.run.run_id) {
        load(CURRENT.run.run_id, true);
    }
});

function load(runId, isPoll) {
    // 轮询走 /live（带"现在谁在跑"的推断），首次整屏走 /run。
    // 两个口读的是同一份 jsonl、同一张图，分别只为读少量/整屏。
    var url = (isPoll ? "/api/orchestration/live" : "/api/orchestration/run") +
              (runId ? "?run_id=" + encodeURIComponent(runId) : "");
    var t0 = Date.now();
    fetch(url, { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (d) {
        if (!d.ok) {
            showError(d.error || "读取失败");
            schedulePoll(runId, false);
            return;
        }
        var prevRunId = CURRENT && CURRENT.run && CURRENT.run.run_id;
        CURRENT = d;
        LAST_TICK = Date.now() - t0;

        if (isPoll && prevRunId === d.run.run_id) {
            // 轮询只改变化的部分，不重建图 —— 重建会让用户眼前的节点位置跳一下
            applySnapshot(d);
            renderWaves(d);
            renderCredit(d);
            renderRun(d);
        } else {
            // 首次加载或切换了 run：整屏重建，节点快照清零
            LAST = {};
            renderGraph(d);
            renderQueue(d);
            renderWaves(d);
            renderTimeline(d);
            renderCredit(d);
            renderRun(d);
        }
        renderLive(d);
        renderFoot(d);
        schedulePoll(d.run.run_id, d.run.running);
    }).catch(function (e) {
        showError("接口不通：" + e.message);
        schedulePoll(runId, false);
    });
}

document.getElementById("view-toggle").addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest(".vt") : null;
    if (btn) setView(btn.getAttribute("data-view"));
});
document.getElementById("run-select").addEventListener("change", function (e) {
    load(e.target.value);
});
document.getElementById("run-refresh").addEventListener("click", function () {
    load(CURRENT ? CURRENT.run.run_id : null);
});

// 抽屉的关闭：× / 遮罩 / Esc 三条路都要能关，少一条现场就会卡在打开状态
(function () {
    var close = document.getElementById("drawer-close");
    var mask = document.getElementById("drawer-mask");
    if (close) close.addEventListener("click", closeDrawer);
    if (mask) mask.addEventListener("click", closeDrawer);
    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") closeDrawer();
    });
})();

// 分层列表里的卡片也能点开抽屉 —— 投影时 DAG 节点小，卡片大得多
document.addEventListener("click", function (e) {
    var card = e.target.closest ? e.target.closest(".node[data-node]") : null;
    if (card) showNodeDetail(card.getAttribute("data-node"));
});

// 地址栏 ?run_id=xxx 可以直接钉住某一次运行（演示脚本会打印这个链接）
(function () {
    var m = /[?&]run_id=([^&]+)/.exec(location.search);
    load(m ? decodeURIComponent(m[1]) : null);
})();


/* ==================== trace 回放（W3-05） ====================
   拿到整份脚本后本地走帧，不再联网：
     - 断网也能拖完一次失败 run（验收口径）
     - 拖时间轴是本地计算，不会每拖一格都等一次接口
   帧里已经带好"这一刻变了哪几个节点"，这里不重算状态判定 ——
   判定只该有一处（core/orchestrate.py 的 _status_at）。 */
var RP = null;        // 回放脚本
var RP_I = 0;         // 当前帧下标
var RP_T = 0;         // 当前毫秒
var RP_ON = false;    // 是否处于回放模式
var RP_PLAY = false;
var RP_SPEED = 1;
var RP_RAF = null;
var RP_LAST = 0;
var RP_SNAP = {};     // 回放到此刻，各节点是什么状态

function rpFmt(v) {
    return ((v || 0) / 1000).toFixed(1) + "s";
}

/* 把回放的帧翻译成 paintDag 认得的那种 nodes */
function rpNodes() {
    var base = (CURRENT && CURRENT.nodes) || [];
    return base.map(function (n) {
        return Object.assign({}, n, {
            status: RP_SNAP[n.id] || "pending",
            // 回放只重演"过没过"，重跑了几遍要看节点抽屉。
            // 这里编个次数出来比留空更糟，所以显式归零。
            attempts: 0
        });
    });
}

function rpPaint() {
    var nodes = rpNodes();
    paintDag(nodes);
    var laneWrap = document.getElementById("lane-wrap");
    if (laneWrap) laneWrap.innerHTML = laneHtml(nodes);
}

function rpSeek(t, keepPlay) {
    if (!RP) return;
    var total = RP.play_ms || RP.total_ms || 0;
    t = Math.max(0, Math.min(total, t));
    RP_T = t;
    // 播放轴可能被拉长过（play_ms > total_ms）：先换算回真实时间再定位帧，
    // 帧永远按真实 t_ms 排 —— 拉长的是节奏，不是数据。
    var real = total > 0 ? t * ((RP.total_ms || 0) / total) : 0;
    // t_ms 为 null 的帧（时间串坏掉的那种）不参与定位 —— 不判 null 的话
    // "null <= t" 恒为真，会把所有坏时间的帧都算成"已经发生"。
    var i = -1;
    for (var k = 0; k < RP.frames.length; k++) {
        var ft = RP.frames[k].t_ms;
        if (ft === null || ft === undefined) continue;
        if (ft <= real) i = k; else break;
    }
    RP_SNAP = {};
    for (var j = 0; j <= i; j++) {
        var f = RP.frames[j];
        if (f.changed) Object.keys(f.changed).forEach(function (k2) {
            RP_SNAP[k2] = f.changed[k2];
        });
    }
    RP_I = i;
    rpPaint();
    // 进度、时间、当前在讲什么
    var pct = total > 0 ? (t / total * 100) : 0;
    document.getElementById("rp-fill").style.width = pct.toFixed(2) + "%";
    document.getElementById("rp-now").textContent = rpFmt(t);
    var cur = i >= 0 ? RP.frames[i] : null;
    document.getElementById("rp-state").textContent = cur
        ? (cur.node ? cur.node + " · " : "") + (cur.event || "") +
          (cur.text ? " · " + cur.text : "")
        : "未开始";
}

function rpStop() {
    RP_PLAY = false;
    if (RP_RAF) { cancelAnimationFrame(RP_RAF); RP_RAF = null; }
    var b = document.getElementById("rp-play");
    if (b) { b.textContent = "▶ 继续"; b.classList.remove("on"); }
}

function rpExit() {
    rpStop();
    RP_ON = false;
    RP = null;
    RP_SNAP = {};
    document.getElementById("replay-bar").classList.remove("on");
    document.getElementById("rp-play").textContent = "▶ 回放";
    document.getElementById("rp-state").textContent = "未回放";
    document.getElementById("rp-fill").style.width = "0%";
    document.getElementById("rp-now").textContent = "0.0s";
    document.getElementById("rp-marks").innerHTML = "";
    // 退出回放要回到"这一遍真实的样子"，不然屏上留着回放最后一帧的颜色
    LAST = {};
    if (CURRENT) applySnapshot(CURRENT);
}

function rpTick(ts) {
    if (!RP_PLAY || !RP) return;
    if (!RP_LAST) RP_LAST = ts;
    var dt = (ts - RP_LAST) * RP_SPEED;
    RP_LAST = ts;
    var next = RP_T + dt;
    var RP_END = RP.play_ms || RP.total_ms || 0;
    if (next >= RP_END) {
        rpSeek(RP_END, true);
        rpStop();
        document.getElementById("rp-play").textContent = "↺ 重放";
        return;
    }
    rpSeek(next, true);
    RP_RAF = requestAnimationFrame(rpTick);
}

function rpPlay() {
    if (!RP) return;
    if (RP_PLAY) { rpStop(); return; }
    // 已经放到头：从头再放一遍
    if (RP_T >= (RP.play_ms || RP.total_ms || 0) - 0.5) rpSeek(0, true);
    RP_PLAY = true;
    RP_LAST = 0;
    var b = document.getElementById("rp-play");
    b.textContent = "❚❚ 暂停";
    b.classList.add("on");
    RP_RAF = requestAnimationFrame(rpTick);
}

function rpLoad(runId) {
    var bar = document.getElementById("replay-bar");
    document.getElementById("rp-state").textContent = "正在取回放脚本…";
    return fetch("/api/orchestration/replay?run_id=" + encodeURIComponent(runId))
        .then(function (r) { return r.json(); })
        .then(function (d) {
            if (!d.ok) {
                document.getElementById("rp-state").textContent = d.error || "取不到回放脚本";
                return;
            }
            RP = d;
            RP_ON = true;
            // 回放期间停掉轮询：一边追实时一边放历史，屏上会自己打自己
            if (TIMER) { clearTimeout(TIMER); TIMER = null; }
            bar.classList.add("on");
            document.getElementById("rp-total").textContent = rpFmt(d.play_ms || d.total_ms);
            document.getElementById("rp-play").textContent = "▶ 回放";
            // 刻度：打回与转人工
            var marks = document.getElementById("rp-marks");
            marks.innerHTML = (d.marks || []).map(function (m) {
                var p = d.total_ms > 0 ? (m.t_ms / d.total_ms * 100) : 0;
                return '<i class="rp-mark ' + (m.kind === "handoff" ? "handoff" : "") +
                       '" style="left:' + p.toFixed(2) + '%" title="' + esc(m.text) + '"></i>';
            }).join("");
            document.getElementById("rp-state").textContent =
                "就绪 · " + d.run.frame_count + " 帧 / " + rpFmt(d.play_ms || d.total_ms) +
                " · " + (d.run.status === "failed" ? "这次失败了" : "这次通过");
            rpSeek(0, true);
            rpPlay();
        });
}

/* ---- 交互绑定 ---- */
(function () {
    var play = document.getElementById("rp-play");
    var track = document.getElementById("rp-track");
    if (play) play.addEventListener("click", function () {
        if (!RP) { rpLoad(pickRunId()); return; }
        rpPlay();
    });
    if (track) {
        var drag = false;
        function at(ev) {
            var r = track.getBoundingClientRect();
            var x = Math.max(0, Math.min(r.width, ev.clientX - r.left));
            // 进度条画的是播放轴（play_ms），不是真实时长 —— 拖到 60% 就是
            // 播放到 60%，换算成真实毫秒是 rpSeek 内部的事
            return (r.width > 0 ? x / r.width : 0) *
                   (RP ? (RP.play_ms || RP.total_ms || 0) : 0);
        }
        track.addEventListener("mousedown", function (ev) {
            if (!RP) return;
            rpStop();
            drag = true;
            rpSeek(at(ev), true);
        });
        window.addEventListener("mousemove", function (ev) {
            if (drag && RP) rpSeek(at(ev), true);
        });
        window.addEventListener("mouseup", function () { drag = false; });
    }
    Array.prototype.forEach.call(document.querySelectorAll(".rp-sp"), function (b) {
        b.addEventListener("click", function () {
            Array.prototype.forEach.call(document.querySelectorAll(".rp-sp"),
                function (x) { x.classList.remove("active"); });
            b.classList.add("active");
            RP_SPEED = Number(b.getAttribute("data-speed")) || 1;
        });
    });
    var runReplay = document.getElementById("run-replay");
    if (runReplay) runReplay.addEventListener("click", function () {
        rpLoad(pickRunId());
    });
})();

function pickRunId() {
    var sel = document.getElementById("run-select");
    if (sel && sel.value) return sel.value;
    if (CURRENT && CURRENT.run) return CURRENT.run.run_id;
    return "";
}

/* ================= 行业切换器（台账 W4-04） ================= */
var INDUSTRY_ID = "sales";
var INDUSTRY_SRC = "configs/sales.yaml";     // 图来源文案跟着行业走

function renderIndustryRoles(roles) {
    var el = document.getElementById("ind-roles");
    if (!el) return;
    // 只摆"谁在班上"：名字 + 职务。全量角色卡在节点抽屉里点开看。
    el.innerHTML = Object.keys(roles || {}).map(function (k) {
        var r = roles[k];
        return '<span class="ind-role"><b>' + esc(r.name) + '</b>' + esc(r.title) + '</span>';
    }).join("");
}

function refreshRoles() {
    fetch("/api/team/roles", { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d.ok) renderIndustryRoles(d.roles); });
}

function industryInit() {
    var sel = document.getElementById("industry-select");
    if (!sel) return;
    fetch("/api/industry", { cache: "no-store" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            if (!d.ok) return;
            INDUSTRY_ID = d.current || "sales";
            INDUSTRY_SRC = "configs/" + INDUSTRY_ID + ".yaml";
            sel.innerHTML = (d.industries || []).map(function (i) {
                return '<option value="' + esc(i.id) + '">' + esc(i.display_name) + '</option>';
            }).join("");
            sel.value = INDUSTRY_ID;
            refreshRoles();
        });
    sel.addEventListener("change", function (e) {
        var target = e.target.value;
        if (!target || target === INDUSTRY_ID) return;
        sel.disabled = true;
        fetch("/api/industry/select", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ industry: target })
        }).then(function (r) { return r.json(); }).then(function (d) {
            sel.disabled = false;
            if (!d.ok) {
                sel.value = INDUSTRY_ID;
                showError(d.error || "切换失败");
                return;
            }
            INDUSTRY_ID = d.industry;
            INDUSTRY_SRC = "configs/" + INDUSTRY_ID + ".yaml";
            // 三件事一起换：角色卡、整屏（含流程图与 run 列表，都在
            // load() 的整屏重建里）—— 切了行业还留着旧图，屏幕会自己骗自己
            refreshRoles();
            load(null);
        }).catch(function () {
            sel.disabled = false;
            sel.value = INDUSTRY_ID;
        });
    });
}

industryInit();
