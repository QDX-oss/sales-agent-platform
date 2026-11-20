# -*- coding: utf-8 -*-
"""生成一份"主模型 key 写错、备用通道接手"的真实运行轨迹。

跑法（项目根目录）：
    python tests/make_fallback_run.py

为什么不用"真连 DeepSeek 并把 key 写错"来演示：
    这台机器连不上外网（连 api.deepseek.com 会挂到超时，20 秒后报的是
    APIConnectionError 而不是 401），拿它当证据只能证明"断网会降级"，
    证不了"key 错会降级"。所以在本地起一个只回 401 的 HTTP 服务，
    让真实的 ChatOpenAI 去打它 —— SDK 会抛 AuthenticationError(status=401)，
    与"key 填错"在上层看到的东西完全相同，而且 3 毫秒就返回，稳定可复现。

哪一部分是替代品，必须说清楚：
    备用通道用的是脚本模型（本机没有第二个 provider 的 key）。
    除了"备用通道由谁扮演"这一点，其余全是真的：真的 ChatOpenAI 客户端
    发真的 HTTP 请求、真的 401、真的 Team 六节点、真的 CSV 与工具、
    真的 verifier、真的调度器、真的 trace 落盘。
    现场有第二家 key 时，把 config/models.json 的第二个槽位配上，
    把这条链里的 backup 换成真模型即可，代码一行不用改。
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_core.messages import AIMessage                          # noqa: E402
from langchain_openai import ChatOpenAI                               # noqa: E402

import ai_analyst                                                      # noqa: E402
from core.fallback import Channel, FallbackChatModel, channel_status   # noqa: E402
from core.team import run_team                                         # noqa: E402
from core.trace import Trace, load_run                                 # noqa: E402
from tests.fake_llm import ScriptedChatModel                           # noqa: E402


# ---------------------------------------------------------------- 假 401 服务

class _Unauthorized(BaseHTTPRequestHandler):
    """对任何请求都回 401，模拟"key 填错了"的上游。"""

    def _deny(self):
        body = json.dumps({
            "error": {
                "message": "Incorrect API key provided: sk-wr***ong. "
                           "You can find your API key at https://platform.deepseek.com",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        }).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = _deny
    do_GET = _deny

    def log_message(self, *args):
        pass


def start_401_server():
    srv = HTTPServer(("127.0.0.1", 0), _Unauthorized)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_port


# ---------------------------------------------------------------- 剧本

def ai(content, tin=0, tout=0, **kw):
    kw.setdefault("usage_metadata", {
        "input_tokens": tin, "output_tokens": tout, "total_tokens": tin + tout})
    return AIMessage(content=content, **kw)


GD = json.loads(ai_analyst.query_stats.invoke({"province": "广东"}))["统计"]
QUESTION = "广东省这个月的销售额和利润率是多少？"

DRAFT = "广东省销售额 %s 元，利润 %s 元，利润率 %s%%。" % (
    GD["销售额"], GD["利润"], GD["利润率"])
CRITIC = json.dumps({"pass": True, "issues": [], "fix": ""}, ensure_ascii=False)
CURATED = "%s 元销售额，利润率 %s%%，与上月口径一致。" % (GD["销售额"], GD["利润率"])

SCRIPT = [
    ai("", 431, 28, tool_calls=[{"name": "query_stats", "args": {"province": "广东"},
                                 "id": "c1", "type": "tool_call"}]),
    ai(DRAFT, 512, 61),
    ai(CRITIC, 903, 24),
    ai(CURATED, 742, 52),
]


def main():
    srv, port = start_401_server()
    print("假上游已启动：http://127.0.0.1:%d/v1 （对任何请求回 401）" % port)

    primary = ChatOpenAI(
        model="deepseek-chat",
        api_key="sk-wrong-key-on-purpose",
        base_url="http://127.0.0.1:%d/v1" % port,
        timeout=8,
        max_retries=0,      # 由我方决定重试，不让 SDK 自己悄悄重试三次
    )
    backup = ScriptedChatModel(script=SCRIPT)

    trace = Trace(question=QUESTION, source="demo-fallback")
    llm = FallbackChatModel(
        chain=[Channel("deepseek", primary,
                       {"model": "deepseek-chat", "provider": "openai",
                        "base_url": "http://127.0.0.1:%d/v1" % port}),
               Channel("backup-demo", backup,
                       {"model": "scripted-fake", "provider": "openai",
                        "base_url": "（脚本模型，无网络）"})],
        fail_threshold=1, cooldown_s=300, trace=trace)

    out = run_team(QUESTION, llm, [ai_analyst.query_stats, ai_analyst.rank,
                                   ai_analyst.trend], trace=trace)
    summary = trace.finish()
    srv.shutdown()

    print()
    print("问：", QUESTION)
    print("答：", out["answer"])
    print("通过=%s 重试=%d 成文=%s 耗时=%.0fms" % (
        out["passed"], out["retries"], out["curated"], out["duration_ms"]))
    print("最终作答通道：", out["model"], "｜ fallback_used =", out["fallback_used"])
    print()

    data = load_run(trace.run_id)
    print("%-4s %-8s %-11s %-6s %-9s %s" % ("seq", "agent", "name", "status", "耗时", "retry"))
    print("-" * 66)
    for s in data["steps"]:
        print("%-4s %-8s %-11s %-6s %7.1fms %d" % (
            s["seq"], s["agent"], s["name"], s["status"], s["duration_ms"], s["retry"]))
    print("-" * 66)
    print("事件：")
    for e in data["events"]:
        d = e["detail"]
        if e["event"] in ("channel_fail", "fallback", "fallback_exhausted",
                          "channel_reopen", "channel_recovered"):
            print("  [%d] %-20s %s" % (e["seq"], e["event"],
                                       json.dumps(d, ensure_ascii=False)[:220]))
        else:
            print("  [%d] %-20s %s" % (e["seq"], e["event"],
                                       json.dumps(d, ensure_ascii=False)[:150]))

    end = data["end"]
    print("-" * 66)
    print("run_end status=%s steps=%s failed=%s tokens=%s" % (
        end["status"], end["step_count"], end["failed_steps"], end.get("tokens")))
    print()
    print("通道状态：", json.dumps(channel_status(llm), ensure_ascii=False)[:400])
    print()
    print("轨迹：", summary["path"])
    print("存档：", out.get("brief_path"))


if __name__ == "__main__":
    main()
