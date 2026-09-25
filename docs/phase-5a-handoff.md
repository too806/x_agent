# Phase 5A 交接文档：Agent Brain V1

> 生成于 2026-09-26，**完成当日即写入**（真机 smoke test 的记录在第 6 节）。
> 基线：`b8a5268`（Phase 4b / 4b.1 收口，186 tests 绿）。
> 本阶段：50 files changed, +4651 / −60；测试基线 **252 后端 + 9 前端**（186 项原样通过）。
>
> 设计审计稿见 [`phase-5a-agent-brain-design.md`](phase-5a-agent-brain-design.md)，
> 其中第 I 节记录了实施过程中对审计稿的 5 处偏离。

北极星（工作单原话）：

> 这是一个真正的 AI Agent 项目，不是一个 X 运营后台项目。
> 最重要的一条能力：**Agent 自己决定下一步做什么。不是简单增加 LangGraph 节点。**

---

## 1. 本阶段做了什么

新增一条**由目标驱动**的链路，与既有的 topic 链路并存：

```
POST /api/runs {goal:"帮我找一个最近值得在中文科技圈讨论的 AI Agent 话题"}
  → Agent 自己发现选题 → 自己决定研究什么 → 抽证据 → 出观点 → 选角度
  → 写 3 条候选 → 自己评审 → 自己决定改稿 → 自己决定收尾
  → guardrail → waiting_for_review（人手动审核、手动发布）
```

既有链路 `POST /api/runs {topic}` 与 Review / Publish / 状态机 / 控制台**一行未改**。

新增文件（30 个）分四类：

| 类别 | 文件 |
|---|---|
| 决策与预算 | `app/agent/schemas.py`、`app/agent/budget.py`、`app/agent/prompts.py`、`prompts/agent/*.txt`（7 个） |
| 工具 | `app/agent/tools/`（9 个动作 → 8 个文件） |
| 节点 | `app/agent/nodes/{planner,execute,finalize}.py` |
| 记录与配置 | `app/models/agent_decision.py`、`alembic/versions/a1f0c7d3b5e2_phase5a_agent_brain.py`、`config/agent.yaml` |

---

## 2. 已锁定的设计决策

| # | 决策 | 理由 |
|---|---|---|
| D1 | **一个 Graph，START 处按 `goal` 分岔** | `PublishService` 拿 Runtime 上的 graph 做 checkpoint 交叉校验；两个 Graph 要求每处持有者都判断"该用哪个"，判断错的那次正好落在最不能出错的地方 |
| D2 | **`agent_run.goal IS NOT NULL` 就是拓扑判别字段** | 不新增 `graph_kind` 列：多一列就多一个可能与 `goal` 自相矛盾的状态源 |
| D3 | **Planner 只决策，不写内容**（工作单第五节） | 同一处既决策又写作，出问题时无法区分"判断错了"还是"笔头错了" |
| D4 | **决策是 Pydantic 结构化输出，`action` 是枚举** | 不让 LLM 用自由文本决定 next node（工作单第四节）。非法值在 `model_validate_json` 阶段被拒 → `LLMError` |
| D5 | **预算由纯 Python 强制，不信模型自觉** | 模型认为"研究还不够"时一定会继续研究；预算的意义恰恰是它判断失误时仍然能停 |
| D6 | **`finalize` 是唯一的兼容接缝** | Agent 的自由度全锁在 `planner … finalize`；之后每个节点、每张表、每个端点都不知道 Agent 存在 |
| D7 | **只有 1 张新表（`agent_decision`）** | `EvidencePack` / `Angle` / `Candidate` 只活在 checkpoint，与 `guardrail_*` / `final_content` 同一套既有机制 |
| D8 | **`topic` 与 `goal` 是两个字段、两处显示** | 绝不用 `topic ?? goal` 顶替：那会把"人的输入"渲染成"Agent 的选择" |
| D9 | **没有候选就明确失败，不给空正文** | 空正文会让审核者以为流程是好的 |
| D10 | **控制台只加最小时间线** | 工作单第十五节：后端先提供数据，不把 Operation Console 大改一遍 |

---

## 3. 不变量（后续阶段不许破坏）

1. **`thread_id` 恒等于 `agent_run.id`**（invoke / interrupt / resume 全程一致）。
2. **`interrupt()` 会让节点从头重放** → interrupt 之前不能有不可重复的副作用。
3. **Publish 状态的权威是 `published_post.status`**，`agent_run.status` 只是镜像。
4. **`final_content` 只有 `human_review` 一个写入点**；Agent 链路写的是 `draft_content`。
5. **`goal IS NULL` 的 Run 行为逐字不变** —— 186 条既有测试是这条的守门人。
6. **不存完整 prompt / secrets**：`agent_decision.input_summary` 存的是"当时手上有什么、
   允许做什么"（`allowed=…; step=2/12; materials=25; facts=20; …`），已用真机数据核对过
   不含 goal 原文，更不含研究正文。
7. **Agent 不自动发布**：`finish → guardrail → human_review` 就停住，Publish 仍然只由人触发。

---

## 4. 预算与终止性

`config/agent.yaml`（工作单第三节的四个数字，可配置）：

| 预算 | 值 | 触发后 |
|---|---|---|
| `max_steps` | 12 | 不再调用 LLM，沿回退顺序收尾（另有 `WRAPUP_ALLOWANCE = 6` 步余量与硬停） |
| `max_research_calls` | 4 | `research_topic` / `deepen_research` / `discover_topics` 全部关闭 |
| `max_candidates` | 3 | 候选池满，只剩 `revise` / `finish` |
| `max_revisions` | 2 | 停止改稿，带着现有最佳候选交给人工审核 |

终止性是**三重**的：`finish` 在 `FALLBACK_ORDER` 结尾（回退一定收敛）、收尾不调用 LLM、
`steps_hard_exhausted` 最笨的硬停。真机上这套机制**真的触发了**（见第 6 节 O5）。

---

## 5. 怎么跑（真机）

```bash
# 1) 主检出合并（4b.1 与 5A 都在这条分支上）
git merge --ff-only worktree-phase-4b1-i18n

# 2) 迁移（DB 上已经是 a1f0c7d3b5e2，这一步是幂等的确认）
alembic upgrade head

# 3) 后端（需要 .env + HTTPS_PROXY，见项目 README 的既有约定）
python run.py

# 4) 前端
cd frontend && npm run dev
```

发一个目标给 Agent（同步阻塞，真机约 2–4 分钟，取决于步数）：

```bash
curl -X POST http://127.0.0.1:8000/api/runs \
  -H "Content-Type: application/json" \
  -d '{"goal":"帮我找一个最近值得在中文科技圈讨论的 AI Agent 话题"}'
```

控制台里看：**运行记录 → 该 Run → 抽屉里的「Agent 决策轨迹」**（步号 / 动作 /
理由 / 当时手里有什么 / 结果），以及它停在「待审核」。

---

## 6. 真机 smoke test 记录（2026-09-26）

一次真实运行：真实 DeepSeek（`deepseek-flash`）+ 真实 X 搜索 + 共享 PostgreSQL，
`run_id = 08a5b915-a9a6-461a-bfbd-995276588d47`，耗时约 2–3 分钟。
**全程没有真实 Publish**（工作单第二十三节）。

十个观察点：

| # | 观察 | 结果 |
|---|---|---|
| O1 | 只给目标，题目由 Agent 发现 | ✅ goal = "帮我找一个最近值得在中文科技圈讨论的 AI Agent 话题"；topic = "Meta 给自家 AI 助手 Muse 上线「应用商店」后，个人 Agent 会不会成为下一个 App Store 级别的平台机会？" |
| O2 | 每一步决策都留下可查的记录 | ✅ 12 步，步号 1..12 单调；每步都有 reason / input 摘要 / result 摘要；input 摘要**不含**完整 prompt |
| O3 | 顺序**不是**固定的 | ✅ 实际是 `discover_topics → extract_evidence → research_topic → generate_angles → select_angle → generate_candidates → criticize → revise → criticize → revise → criticize → finish` —— 它在**还没有选定题目时就先抽了证据**，选定之后又补了一次研究。这条如果换成固定链，图根本跑不出来 |
| O4 | 可选动作随状态收缩 | ✅ 第 2 步没有 `generate_angles`（还没有事实）；第 7 步没有 `select_angle` / `generate_candidates`（候选池满）；第 11 步没有 `revise`（预算用尽）；第 12 步有 `finish` |
| O5 | 预算真的会触发 | ✅ 收尾时 `research=2/4`、`revisions=2/2`、`candidates=3`、`step=11/12` |
| O6 | 回退过滤器是保险，不是拐杖 | ✅ 12 步**一次回退都没发生**（真实模型的每个决策都可行）；它只在模型判断失误时兜底 |
| O7 | 证据可追溯 | ✅ 抽到 20 条事实、`dropped=0`（每条事实的来源都落在本次真实落库的 research_item id 上） |
| O8 | 观点 → 候选 → 评审 → 改稿 | ✅ 5 个观点、选定 a1、3 条候选（直接观点 / 信息密集 / 反共识）、3 轮评审、2 次改稿，最后交付 c1（131 字） |
| O9 | 停在人工审核、没有自动发布 | ✅ `guardrail_passed=True`、`run_status=waiting_for_review`、`review` 表与 `published_post` 表**都没有行** |
| O10 | 控制台看得到 | ✅ `/api/admin/runs/{id}` 返回 12 步轨迹 + goal；待审队列里能看到它，题目非空；`/api/runs/{id}` 正常返回 |

其他实测数据：`research_item` 28 行（首次检索 25 条 + 补充检索 3 条，27 个不重复的
`platform_post_id` —— 两次检索有重叠，与既有行为一致，不去重）；`draft.prompt_version = "5a"`。

**一个值得记下来的现象**：三轮评审里 Critic 对三条候选**全部**给了 `revise`，Agent
于是一路改到 `max_revisions` 用尽才收尾。这不是缺陷（工作单第十三节要的就是"超过预算
就停下来交给人工"），但说明 Critic 的 prompt 偏严 —— 5B 若调，调这里，不要动预算。

---

## 7. 测试与变异验证

| 文件 | 条数 | 覆盖 |
|---|---|---|
| `tests/test_agent_planner.py` | 29 | 决策 schema、预算判定、回退顺序、prompt 与枚举一致性 |
| `tests/test_agent_loop.py` | 15 | 端到端 Agent 循环（脚本化 FakeLLM + 真 PostgreSQL checkpointer） |
| `tests/test_agent_tools.py` | 19 | 每个工具的判定逻辑（证据可追溯、候选绑定、终选排序） |
| `tests/test_admin_api.py` | +3 | 轨迹按 step 排序、goal 与 topic 分列、列表带 goal |
| `frontend/tests/agent-action-label.test.ts` | 3 | 动作名中文化 + "没翻译的动作原样显示" |

关键用例都做了**变异验证**（改坏实现 → 对应用例必须变红，然后还原）：
证据可追溯性校验、`ready_to_finish` 闸门、研究预算、改稿预算、不可行决策的替换、
决策轨迹的排序口径。

所有测试默认 mock：**不消耗真实 X credits、不调真实 LLM**（工作单第二十二节）。

---

## 8. 已知限制 / 本阶段没做

- **前端时间线只过了 typecheck + build + API 形状核对，没有人工浏览器验收**。
  它是最小版本（没有折叠、没有单步耗时、没有从步骤跳到产物）。
- **证据包 / 观点 / 候选不入库**，只在 checkpoint。checkpoint 被清理后，控制台只剩
  决策轨迹，看不到"当时抽出了哪些事实、选了哪个角度"。
- **本阶段没有真机验证 review → resume → publish 的 Agent 版本**（smoke test 按要求停在
  `waiting_for_review`）。这条路径由 `tests/test_agent_loop.py::test_agent_run_resumes_through_review_and_publish`
  覆盖。
- **4b.1 遗留的那条视觉复现仍未验收**：单条待审队列里做完决定后保留当前 Run。
- **4b.1 与 5A 都还没并进 `master`**，仓库无 remote，备份仍靠仓库外的 bundle。

## 9. 明确不做（工作单第十九节）

Style Distillation、高粉账号样本库、Trend Intelligence DB、Fine-tuning、Embedding、
Vector DB、pgvector、Redis、Celery、Kafka、Scheduler、Autonomous Publish、
X metrics feedback、Multi-agent、多账号、RAG。

工作单第二十五节：**完成后停止，不进入 Phase 5B。**

---

## 10. 提交

| commit | 内容 |
|---|---|
| `5114a41` | `feat: add agent decision model` —— schemas + budget + `agent_decision` 表 + migration + 仓储层 |
| `3901fc8` | `feat: add the autonomous planner loop and its tools` —— planner + 9 个动作工具 + Graph 分岔 + finalize |
| `e05ea11` | `feat: surface the agent decision trace in the console` —— Admin API 暴露轨迹与 goal + 控制台最小时间线 |
| `948fb39` | `docs: document phase-5a agent brain` —— 设计审计稿同步为实施记录 |
| 本次 | `docs: record phase-5a handoff and the real-world run` |
