/* =========================================================
   AI 数据分析师（DeepSeek）：面板开关 / 发送问题 / 展示回答
   ========================================================= */

var aiPanel = document.getElementById("ai-panel");
var aiMsgs = document.getElementById("ai-messages");
var aiInput = document.getElementById("ai-input");
var aiSend = document.getElementById("ai-send");

// 打开 / 关闭面板
document.getElementById("ai-entry").onclick = function () {
    var showing = aiPanel.style.display !== "none";
    aiPanel.style.display = showing ? "none" : "flex";
    if (!showing) aiInput.focus();
};
document.getElementById("ai-close").onclick = function () {
    aiPanel.style.display = "none";
};

// 追加一条消息，返回消息元素
function addMsg(text, cls) {
    var div = document.createElement("div");
    div.className = "ai-msg " + cls;
    div.textContent = text;
    aiMsgs.appendChild(div);
    aiMsgs.scrollTop = aiMsgs.scrollHeight;
    return div;
}

// 回答下方的一行核对结果：哪些数字有出处、哪些没有
function addVerify(d) {
    var v = d.verification || {};
    if (!v.passed) {
        var bad = (v.unsupported || []).map(function (x) { return x.raw; }).join("、");
        var n = (v.unsupported || []).length;
        addMsg("⚠ " + n + " 个数字无法核对：" + (bad || "—") +
               "（已纠正 " + (d.retries || 0) + " 次仍未通过）",
               "ai-verify ai-verify-fail");
    } else if (v.checked > 0) {
        addMsg("✓ " + v.checked + " 个数字均已核对，全部来自本次查询结果" +
               (d.retries ? "（第 " + (d.retries + 1) + " 次回答才通过）" : ""),
               "ai-verify ai-verify-ok");
    }
}

// 角色链路：这一问经过了谁。多智能体最容易变成"嘴上说说"，
// 把它压成一行 chip，用户不用翻轨迹也知道刚才有几个角色在干活。
function addCrew(d) {
    var c = d.critic || {};
    var row = document.createElement("div");
    row.className = "ai-msg ai-crew";

    function chip(text, cls) {
        var s = document.createElement("span");
        s.className = "ai-chip " + cls;
        s.textContent = text;
        row.appendChild(s);
    }
    function arrow() {
        var a = document.createElement("span");
        a.className = "ai-arrow";
        a.textContent = "→";
        row.appendChild(a);
    }

    chip("小张 取数出稿", "chip-analyst");

    if (d.retries > 0) {
        var why = (c.issues || [])
            .filter(function (i) { return i.severity === "high"; })
            .map(function (i) { return i.type; })
            .filter(Boolean).join("、");
        arrow();
        chip("老李 打回" + (why ? "：" + why : ""), "chip-bad");
        arrow();
        chip("小张 重做" + (d.retries > 1 ? " ×" + d.retries : ""), "chip-analyst");
    }

    arrow();
    chip(c.pass ? "老李 通过" : "老李 未通过", c.pass ? "chip-critic" : "chip-bad");

    if (d.curated) {
        arrow();
        chip("小王 成文", "chip-curator");
    }

    aiMsgs.appendChild(row);
    aiMsgs.scrollTop = aiMsgs.scrollHeight;
}

// 发送问题
// 降级要让人看得见（台账 W2-02）。悄悄换成备用通道、界面上还装作
// 一切正常，比直接报错更坏 —— 用户会以为自己的主模型一直好着。
function addFallback(d) {
    var list = d.fallbacks || [];
    var switched = list.filter(function (e) { return e.event === "fallback"; });
    if (!d.fallback_used && !switched.length) return;

    var last = switched.length ? switched[switched.length - 1] : {};
    var bits = [];
    if (last.from) {
        var why = last.status_code ? "HTTP " + last.status_code : (last.error_type || "失败");
        bits.push(last.from + "（" + why + "）");
    }
    if (last.to && last.to.indexOf("无可用") !== 0) bits.push("→ " + last.to);
    if (d.model) bits.push("本次由 " + d.model + " 作答");

    var div = document.createElement("div");
    div.className = "ai-fallback";
    div.textContent = "⚠ 已切换备用通道：" + bits.join(" ");
    aiMsgs.appendChild(div);
    aiMsgs.scrollTop = aiMsgs.scrollHeight;
}

// 重做过程要让人看得见（台账 W2-03）：别让用户以为这答案一次就对。
// 只念诊断结果，不在这里重新判断 —— 判定在 core/diagnose.py，一处。
var RETRY_LABEL = {
    fetch: "重新取数",
    metric: "退回核口径",
    no_data: "说明查询范围",
    expression: "只改表达",
    unknown: "逐条重做"
};

function addRetry(d) {
    var list = d.diagnoses || [];
    if (!list.length && !d.handoff) return;

    var parts = list.map(function (x) {
        return (x.label || x.kind) + "（" + (RETRY_LABEL[x.kind] || x.strategy) + "）";
    });
    var text = "↻ 自动重做 " + (d.retries || list.length) + " 次：" + parts.join(" → ");
    if (d.handoff && d.handoff.needed) text += " · 已转人工复核";

    var div = document.createElement("div");
    div.className = "ai-retry" + (d.handoff && d.handoff.needed ? " ai-retry-hand" : "");
    div.textContent = text;
    aiMsgs.appendChild(div);
    aiMsgs.scrollTop = aiMsgs.scrollHeight;
}

function ask() {
    var q = aiInput.value.trim();
    if (!q) return;
    aiInput.value = "";
    addMsg(q, "ai-user");

    // 占位的"思考中"提示
    var loading = addMsg("正在分析数据，请稍候…", "ai-bot ai-loading");
    aiSend.disabled = true;

    fetch("/api/ai/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q })
    })
        .then(function (r) { return r.json(); })
        .then(function (d) {
            loading.remove();
            aiSend.disabled = false;
            if (d.ok) {
                addMsg(d.answer, "ai-bot");
                addCrew(d);
                addRetry(d);
                addVerify(d);
                addFallback(d);
            } else {
                addMsg("出错了：" + d.error, "ai-bot");
            }
            aiInput.focus();
        })
        .catch(function () {
            loading.remove();
            aiSend.disabled = false;
            addMsg("网络请求失败，请稍后重试。", "ai-bot");
        });
}

aiSend.onclick = ask;
aiInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") ask();
});
