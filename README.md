# 销售多智能体编排平台（SaleAgent Orchestrator）

> 第五届"未来杯"创新大赛 · 赛题一「Agent 编排创新应用」参赛作品
> 大模型 Agent 工具编排调度平台 —— 面向多智能体协作的编排、调度、执行与验证

一个围绕「销售数据分析」真实业务场景构建的多智能体（Multi-Agent）编排调度平台：
三个角色（Analyst / Critic / Curator）在一张可配置的 DAG 上协作完成任务流，
全程 trace 落盘、节点级可观测、断网可回放、结果可验证、回归可复现。

---

## 一、与赛题六大核心能力的对应

| 赛题要求能力 | 本平台实现 | 代码位置 |
|---|---|---|
| Agent 团队定义与管理 | 三角色分工（Analyst 只取数 / Critic 只挑错不改数 / Curator 写结论）；角色卡外置可热改（改配置重启即生效，代码零改动）；行业级团队整体切换 | `core/roles.py`、`config/roles.json`、`configs/sales.yaml`、`configs/service.yaml` |
| 工作流编排 | 可配置 DAG：节点 = {id, agent, deps, retry, timeout}；条件边 `when=ok/fail/always`；回边循环上限 `max_loops`；拓扑排序执行；换一份图定义流程随之改变、执行器零改动 | `core/graph.py`、`core/team.py` |
| 智能调度引擎 | 优先级队列（priority 大者先行，同级按提交顺序稳定排序）；任务级 deadline（从真正开跑起算，超时判 timeout 立即释放槽位）；Blackboard 快照断点续跑（失败后从最后成功节点继续，不重复计费） | `core/scheduler.py` |
| 自我循环（Self-Loop） | 诊断式重试：失败按**确定性规则**归类为 无数据/取数错/口径错/表达错 四类 → 按类给不同修正策略（重取 / 退回核口径 / 只改表达不动数字）→ 重试上限 2 次，超限转人工兜底 | `core/diagnose.py` |
| 工具与外部系统集成 | LangChain Function Calling（bind_tools）注册结构化查询工具（query_stats / rank / trend 等），数据源为 CSV；提示词与证据分离注入 | `core/team.py`、`data_source.py` |
| 执行监控与结果验证 | 每步 trace 落盘 `runs/{run_id}.jsonl`（agent / 输入输出 / 耗时 / token / 重试 / 状态）；编排屏实时 DAG 染色（pending/running/pass/retry/fail，1s 轮询）；节点详情抽屉；历史 run 断网回放；verifier 数字回查（回答中的数字必须能在工具返回中找到出处，否则打回） | `core/trace.py`、`core/orchestrate.py`、`core/verifier.py`、`templates/orchestration.html` |
| 结果验证与反馈优化 | 11 题固定 Eval Set（含 3 道故意易错题），判定方式分 精确值/必须包含/必须拒答；`python -m evals.run` 一键回归出报告；失败样本自动回流 regression 分组 | `evals/` |

## 二、快速开始（评审复现指引）

### 1. 环境要求

- Python 3.12+（开发版本 3.13）
- 一个 DeepSeek API Key（用于大模型推理；也可改为任何 OpenAI 兼容端点）

### 2. 安装与启动

```bash
pip install -r requirements.txt

# 配置 API Key（二选一）
cp ai_key.example.txt ai_key.txt    # 然后把你的 Key 填进去
# 或设置环境变量 DEEPSEEK_API_KEY=sk-xxxx

python app.py                        # 启动，监听 http://127.0.0.1:5000
```

浏览器打开 <http://127.0.0.1:5000>，输入登录密码（`data/password.json`，默认 `114514`）。

登录后进入**一体化控制台**，顶部导航在四个界面间切换（不新开窗口）：
**📊 数据大屏**（KPI 与图表）· **🔀 运行编排**（编排调度可视化）· **⚙ 管理后台**（CSV 数据管理）· **🛠 系统设置**。

### 3. 三分钟看到全部核心能力

1. **数据大屏**：按 F5 连刷三次，总销售额恒为 16,848,875（与 data.csv 真值逐位一致——大屏与 AI 共用 `data_source.py` 唯一数据入口）。
2. **AI 提问**：点左上角「✨ AI 分析」，问"广东省的销售额是多少？"，同时切到「🔀 运行编排」——这次提问作为一次 run 实时展示：三角色 DAG 节点按 pending → running → pass 变色。
3. **Self-Loop 演示**：故意问"广东省的毛利率是多少？"（口径陷阱：数据中无成本字段）——Critic 判 fail → 诊断归类"口径错" → 退回核口径重答 → 诚实拒答而非编数。
4. **节点详情**：编排屏点任意节点，抽屉内可读出该次调用的输入、输出、耗时、token、重试原因。
5. **断网回放**：编排屏右侧选中历史 run，点「回放这一遍」——整条事件流本地重演，不依赖模型在线。

### 4. 一键回归与测试

```bash
python -m evals.run          # 11 题回归：通过率 / 平均耗时 / 平均 token / 失败清单
python -m pytest tests/ -q   # 129 个单元测试
```

当前版本回归成绩：**11/11 = 100% 通过**（报告存于 `evals/reports/`，含与上次的自动对比）。

## 三、总体架构

```
                 ┌──────────────────────────────────────────────┐
                 │                 一体化外壳 shell.html          │
                 │   数据大屏 │ 运行编排 │ 管理后台 │ 系统设置     │
                 └───────────────┬──────────────────────────────┘
                                 │ /api/*
   ┌─────────────────────────────┴──────────────────────────────┐
   │                        Flask app.py                         │
   │   /api/ai/ask ── 一条 run：Trace 上下文 + run_id 先行下发     │
   └──────┬─────────────────────────────────────────────────────┘
          │
   ┌──────┴──────────────────────────────────────────────────────┐
   │              核心内核 core/（与界面完全解耦）                  │
   │                                                              │
   │  graph.py  ── 可配置 DAG：节点/条件边/回边/拓扑执行            │
   │      │          图定义来自 configs/<industry>.yaml            │
   │      ▼                                                       │
   │  scheduler.py ─ 优先级队列 + deadline + 超时让位 + 断点续跑     │
   │      │                                                       │
   │      ▼                                                       │
   │  team.py ──── Analyst(工具取数) → Critic(挑错) → Curator(结论) │
   │      │        Function Calling（bind_tools）                  │
   │      ▼                                                       │
   │  verifier.py ── 数字回查：结论中的数必须有工具返回出处          │
   │      │  fail ──▶ diagnose.py ── 确定性四类归因                 │
   │      │              │        （无数据/取数/口径/表达）          │
   │      │              └── 分类修正策略 → 重试（上限 2）→ 人工兜底 │
   │      ▼                                                       │
   │  fallback.py ── 主模型连续失败自动切备用，切换写入 trace        │
   │  budget.py ─── token/调用次数上限，触顶写 aborted=budget       │
   │      │                                                       │
   │      ▼                                                       │
   │  trace.py ─── 全量事件流落盘 runs/{run_id}.jsonl（唯一真源）   │
   └──────┬──────────────────────────────────────────────────────┘
          │
   ┌──────┴──────────────────────────────────────────────────────┐
   │  orchestrate.py ── 编排屏数据口（run / live / node / replay）│
   │  evals/ ── 固定用例集 + 一键回归 + 失败样本回流               │
   └─────────────────────────────────────────────────────────────┘
```

## 四、自定义改进与创新点（相对开源框架）

本平台基于 LangChain（Apache 2.0）作为 LLM 接入层，**调度、验证、Self-Loop 均为自研**：

1. **确定性失败归因**：不问模型"你错哪了"（模型自归因不可靠），用确定性规则从工具调用记录、质检意见、数字回查结果中归类失败原因，按类给不同的修正策略——这是"改策"而不是"重发"。
2. **单一真源原则**：大屏 KPI、AI 问答、编排屏、回归评测共用 `data_source.py` 一个数据入口与 `runs/*.jsonl` 一份 trace；编排屏"不重算任何账"，从根上杜绝两个真源对不上的问题。
3. **降级必须留痕**：模型通道连续失败自动切换备用，但切换事件强制写入 trace 的 `fallback` 字段——"悄悄降级然后当作一切正常，比直接失败更糟"。
4. **预算护栏**：单次 run 的 token 与调用次数上限外置在 `config/team.json`，触顶即中止、已有结论照常交付、`aborted=budget` 写入 trace。
5. **断网可回放**：整条事件流一次交付，时间轴本地重演——现场演示不赌网络。

## 五、目录结构

```
├── app.py               Flask 入口（一体化外壳 + 大屏 + 后台 + 设置 + API）
├── ai_analyst.py        AI 问答入口（/api/ai/ask，run_id 先行下发）
├── data_source.py       唯一数据入口（大屏 / AI / 评测三处共用）
├── core/                编排调度内核（与界面解耦）
│   ├── graph.py         可配置 DAG 状态机
│   ├── scheduler.py     优先级调度 + deadline + 断点续跑
│   ├── team.py          三角色班组（Analyst / Critic / Curator）
│   ├── roles.py         角色卡（外置可热改）
│   ├── verifier.py      数字回查
│   ├── diagnose.py      诊断式 Self-Loop（确定性四类归因）
│   ├── fallback.py      模型通道降级
│   ├── budget.py        预算护栏
│   ├── trace.py         trace 结构与落盘
│   ├── orchestrate.py   编排屏数据口
│   ├── industry.py      行业配置切换（sales / service）
│   └── evalset.py       用例集加载
├── configs/             行业级配置（角色 + 工具 + 图定义 + 口径知识）
├── config/              角色卡外部覆盖 / 模型通道 / 调度参数 / 预算
├── evals/               固定用例集 + 一键回归 + 失败样本回流 + 报告存档
├── runs/                每次 run 的全量 trace（jsonl，编排屏回放数据源）
├── graph_runs/          图执行事件流
├── templates/           一体化外壳 + 四个界面
├── static/              前端资源（ECharts DAG 渲染、实时染色）
├── tests/               129 个单元测试（含各内核模块与评测链路）
└── docs/                七分钟演示台词 · 答辩五问
```

## 六、开源合规说明

- 本项目基于 **LangChain / LangChain-OpenAI**（Apache License 2.0）接入大模型，前端图表使用 **ECharts**（Apache License 2.0）。
- 按赛题要求声明改动点与创新点：工作流编排引擎（graph/scheduler）、验证机制（verifier + 评测闭环）、自我循环机制（diagnose）均为参赛团队原创实现，未套用任何编排框架的默认示例。
- `runs/`、`graph_runs/` 中的运行记录为演示数据（模拟销售数据），不含敏感信息；`ai_key.txt` 已从仓库移除，请参照 `ai_key.example.txt` 自行配置。

## 七、核心指标速览

| 指标 | 数值 |
|---|---|
| 单元测试 | 129 个，全部通过 |
| Eval Set 回归 | 11/11 = 100%（含 3 道易错题），平均 4.1s / 3,247 token |
| 数据一致性 | 大屏 KPI 与 AI 回答逐位一致（连调恒定 16,848,875） |
| 内核规模 | core/ 十余模块约 5,300 行，全部可独立单测 |
