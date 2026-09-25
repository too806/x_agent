# Phase 5A 设计审计：Agent Brain V1

> 本文是实施前的设计审计（对应工作单第二十一节），记录**当前架构 → 新架构**的映射、
> 新增数据模型、Graph 节点、Planner schema、Agent loop、与现有 HITL / Publish 的连接方式，
> 以及测试策略。审计完成后直接实施，不再等待二次确认。

北极星：**这是一个真正的 AI Agent 项目，不是一个 X 运营后台项目。**

---

## A. 当前架构（实施前实测）

### A.1 执行链路

```
POST /api/runs {topic}
  └─ create_agent_run(topic, status="running")
  └─ graph.ainvoke({run_id, topic}, config=thread_id=run_id)
       START → research → generate → guardrail → human_review → END
  └─ "__interrupt__" in final_state ? mark_run_waiting_for_review
                                     : mark_run_completed

POST /api/runs/{id}/review {action: approve|edit|reject}
  └─ claim_run_for_review（CAS：waiting_for_review → approved/edited/rejected）
  └─ graph.ainvoke(Command(resume=...), 同一个 thread_id)
  └─ human_review 节点**从头重放**，interrupt() 这次直接返回 resume value
       → create_review（写 review 表）→ 设置 final_content
  └─ 失败时 release_run_review_claim 把状态放回 waiting_for_review

POST /api/runs/{id}/publish
  └─ PublishService.publish(run_id)（**不是 Graph 节点**，刻意如此）
       读 review 决定 → 从 checkpoint 读 final_content 做交叉校验
       → claim_publish（CAS，先落库再调 X）→ 调 X → mark_published / failed / unknown
```

### A.2 五个必须尊重的既有事实

1. **`thread_id` 恒等于 `agent_run.id`**（`app/main.py:_thread_config`）。
   invoke / interrupt / resume 全程一致。
2. **`interrupt()` 会让节点从头重放**，所以 interrupt 之前不能有任何不可重复的副作用
   （`app/agent/nodes/human_review.py` 的文件头把这条写成了硬约束）。
3. **Guardrail 读的是 `draft_content`，不是 `final_content`**（`guardrail.py`）。
   `final_content` 只有 `human_review` 一个写入点。
4. **checkpoint 承载 `guardrail_*` / `human_action` / `final_content`**，数据库里没有这些列；
   `GET /api/runs/{id}` 和 Admin 的 Run 详情都靠 `graph.aget_state()` 补齐。
5. **Draft 表按 `version` 取最新**（`get_draft_for_run` / `drafts_by_run`），
   Review 挂在 Draft 上、PublishedPost 也挂在 Draft 上。改草稿的写法会同时惊动审核与发布。

### A.3 当前 AgentState

`app/agent/state.py::RunState`（TypedDict, total=False）：
`run_id, topic, research_results, research_item_ids, draft_content, draft_id,
guardrail_passed, guardrail_reasons, human_action, final_content`。

### A.4 关键边界

- Node 不碰 SQL、不碰 httpx：一律 Node → Service → Repository。
- Admin API（Phase 4b）**只有 GET**，写操作复用 Phase 3/4a 端点。
- 发布状态机是权威（`published_post.status`），`agent_run.status` 是镜像。

---

## B. Phase 5A 新架构

### B.1 执行链路

```
POST /api/runs {goal}
  └─ create_agent_run(goal=G, topic=NULL)
  └─ agent_graph.ainvoke({run_id, goal}, config=thread_id=run_id)
       START → planner ─┬─(AgentDecision.action)→ discover_topics    ─→ planner
                        ├─→ research_topic                          ─→ planner
                        ├─→ deepen_research                         ─→ planner
                        ├─→ extract_evidence                        ─→ planner
                        ├─→ generate_angles                         ─→ planner
                        ├─→ select_angle                            ─→ planner
                        ├─→ generate_candidates → criticize          ─→ planner
                        ├─→ revise                                  ─→ planner
                        └─(finish)→ finalize → guardrail → human_review → END

POST /api/runs/{id}/review   ← 完全不变
POST /api/runs/{id}/publish  ← 完全不变
```

### B.2 两条拓扑共存，用 `goal` 判别

`agent_run.goal IS NULL` → 现有 `topic` 线性链路（Phase 1–4a 行为，逐字不变）。
`agent_run.goal IS NOT NULL` → 新 Agent 链路。

**刻意不再新增判别列**：`goal` 自己就是判别字段，多一列就多一个可能自相矛盾的状态源。
这让现有 186 条测试与真实产品行为全部保持原样，新能力走新入口。

### B.3 唯一的兼容接缝：`finalize`

Agent loop 结束时不直接产出 API 响应，而是**把选定的候选稿落成 Draft**，
写回 `draft_content` / `draft_id`，然后走**与今天完全相同**的
`guardrail → human_review`。

这是整个设计的核心决策：

> Agent 的自由度全部锁在 `planner … finalize` 这一段里；
> `finalize` 之后的每一个节点、每一张表、每一个端点，都不知道 Agent 存在。

因此 Review 的 approve/edit/reject 语义、Publish 的四个状态与 CAS、
`final_content` 的交叉校验、控制台的一切，**一行都不用改**。

### B.4 Planner 不写内容

职责分离（工作单第五节）：

| 角色 | 负责 | 不负责 |
|---|---|---|
| Planner | 决定下一步 action | 写任何内容 |
| Tools | 执行（检索/抽取/生成/评价） | 决定顺序 |
| Researcher | 取回原始材料 | 形成观点 |
| Strategist | 形成 Angle | 写稿 |
| Writer | 写候选 | 评价自己 |
| Critic | 结构化评价 | 改稿（工作单第十二节：不让 Critic 直接改稿） |

Planner 只调 `llm_service.complete()` 拿一个 `AgentDecision`，**不碰 SQL、不碰 httpx**。

---

## C. 新增数据模型

### C.1 改 `agent_run`（2 处，均为放宽而非改写）

| 列 | 变更 | 理由 |
|---|---|---|
| `goal` | **新增** `TEXT NULL` | Agent 的输入是目标，不是题目 |
| `topic` | `NOT NULL` → **允许 NULL** | 主题由 Agent 自己发现；创建 Run 时还不知道 |

旧行不受影响（`topic` 全部有值、`goal` 全为 NULL → 全部走旧链路）。
`goal` 与 `topic` 都对外暴露为 `str | None`：**不拿 goal 顶替 topic 显示**，
前端显式回退（`topic ?? goal`），避免"把目标当题目"这种静默失真。

### C.2 新表 `agent_decision`（Agent Decision Trace）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | |
| `agent_run_id` | UUID FK → agent_run ON DELETE CASCADE | |
| `step` | INT | 第几步决策（从 1 开始） |
| `action` | VARCHAR(32) | `AgentDecision.action` |
| `reason` | TEXT | 为什么这么做（LLM 给的） |
| `target` | VARCHAR(255) NULL | 作用于哪个对象（topic / angle id / candidate id） |
| `input_summary` | TEXT | **摘要**，不是完整 prompt |
| `result_summary` | TEXT | 这一步产出了什么 |
| `created_at` | TIMESTAMPTZ | |

唯一约束 `UNIQUE(agent_run_id, step)`：step 单调，且让"同一步被写两次"在 schema 层就不可能。

**刻意不存完整 prompt**：工作单第十五节明确要求；prompt 里含研究正文，
体积大且没有审计价值，而"摘要 + 结果"才是人要看的东西。

### C.3 不新增 Evidence 表

`EvidencePack` / `Angle` / `Candidate` / `CriticResult` 只活在 Graph state（checkpoint）里，
经 `aget_state` 暴露 —— **与 `guardrail_*` / `final_content` 完全同一套既有机制**。
理由：它们是一次运行的中间产物，没有跨 Run 查询需求；建表就等于把 checkpoint
已经承担的事再抄一份，两处必然漂移。

**结论：本阶段只有 1 张新表 + 1 个新列 + 1 个放宽。**

---

## D. 新增 Graph 节点

| 节点 | 类型 | 调用的 Tool | 副作用 |
|---|---|---|---|
| `planner` | 决策 | —（只调 LLM） | 写 `agent_decision` 一行 |
| `discover_topics` | 执行 | `discover_topics` | 搜索落 `research_item` |
| `research_topic` | 执行 | `research_topic` | 搜索落 `research_item` |
| `deepen_research` | 执行 | `deepen_research` | 搜索落 `research_item` |
| `extract_evidence` | 执行 | `extract_evidence` | 无（纯 state） |
| `generate_angles` | 执行 | `generate_angles` | 无（纯 state） |
| `select_angle` | 执行 | `select_angle` | 无（纯 state，确定性：按 target 选） |
| `generate_candidates` | 执行 | `generate_candidates` | 无（纯 state） |
| `criticize` | 执行 | `criticize_candidate` | 无（纯 state） |
| `revise` | 执行 | `revise_candidate` | 无（纯 state） |
| `finalize` | 收口 | — | **写 `draft` 一行**，设置 `draft_content`/`draft_id`/`topic` |
| `guardrail` | **复用** | — | 无 |
| `human_review` | **复用** | — | `interrupt()` + `review` 表 |

### D.1 固定边 vs 条件边

- `planner` 用 `add_conditional_edges`：按 `AgentDecision.action` 路由到 10 个目标之一。
- **`generate_candidates → criticize` 是固定边**：候选生成后必须评价，这一步不该交给
  LLM 决定（工作单第十七节的图也是这么画的）。Critic 之后回到 `planner`，由 Planner
  决定 accept / revise / 再来一轮 —— Critic 自己不做路由。
- 其余执行节点一律固定边回 `planner`。
- `planner --finish--> finalize`，`finalize → guardrail → human_review → END`（与今天一致）。

---

## E. Planner decision schema

```python
class AgentAction(str, Enum):
    DISCOVER_TOPICS = "discover_topics"
    RESEARCH_TOPIC = "research_topic"
    DEEPEN_RESEARCH = "deepen_research"
    EXTRACT_EVIDENCE = "extract_evidence"
    GENERATE_ANGLES = "generate_angles"
    SELECT_ANGLE = "select_angle"
    GENERATE_CANDIDATES = "generate_candidates"
    CRITICIZE = "criticize"
    REVISE = "revise"
    FINISH = "finish"


class AgentDecision(BaseModel):
    action: AgentAction
    reason: str = Field(min_length=1)      # 必填：没有理由的决策无法审计
    target: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
```

**必须用 Pydantic structured output，不让 LLM 返回自由文本决定 next node**（工作单第四节）。
`action` 是 `Literal`/`Enum`，非法值在 `model_validate_json` 阶段就被拒 —— 与
`GeneratedDraft` 的处理方式完全一致（`ValidationError` → `LLMError`）。

`reason` 最小长度 1：`decision_trace` 的价值全在"为什么"，允许空串等于允许没有理由。

---

## F. Agent loop 与预算

### F.1 预算配置（`config/agent.yaml`，可配置）

```yaml
max_steps: 12
max_research_calls: 4
max_candidates: 3
max_revisions: 2
candidates_per_round: 3
```

### F.2 预算必须由 Python 强制，不能信任 LLM

工作单第三节："绝不能无限循环"。因此：

1. Planner 节点**先**算 `allowed_actions(state, budget, counters)` —— 纯函数，
   把前置条件不满足、计数已用尽的 action 全部剔除。
2. `step_count >= max_steps` → **不调用 LLM，直接返回 `finish`**。
3. 调 LLM 拿到 decision 后，用 `allowed` 过滤：
   - 选中的 action 不在 `allowed` 里 → 按**确定性回退顺序**取第一个 allowed；
   - `allowed` 为空 → `finish`。
4. 回退也要留下 trace（`reason` 写明"预算不足，回退到 X"），否则审计会看到
   Agent"莫名其妙"做了别的事。

回退顺序（从"最接近完成"到"最接近重来"）：
`finish → revise → generate_candidates → select_angle → generate_angles →
extract_evidence → deepen_research → research_topic → discover_topics`

### F.3 预算计数的口径

| 计数 | 何时 +1 | 上限 |
|---|---|---|
| `step_count` | 每次 planner 决策 | `max_steps` = 12 |
| `research_count` | `discover_topics` / `research_topic` / `deepen_research` 各 +1 | `max_research_calls` = 4 |
| `candidate_count` | 候选池里的候选**总数** | `max_candidates` = 3 |
| `revision_count` | 每次 `revise` | `max_revisions` = 2 |

`max_candidates` 的口径取"候选池大小上限"（不是"轮次上限"）：一轮 `generate_candidates`
产出 `candidates_per_round` = 3 个候选即填满池子，之后 Planner 只能 `revise` 或 `finish`。
这样这个参数是一个**真正会触发的预算**，而不是一个永远够用的摆设。
（`revise` 是**替换**池中的候选，不是新增，所以池子大小恒 ≤ 3。）

### F.4 前置条件表

| action | 前置 |
|---|---|
| `discover_topics` | 无 |
| `research_topic` | `selected_topic` 已定 |
| `deepen_research` | `selected_topic` 已定 且 已有 research |
| `extract_evidence` | 已有 research |
| `generate_angles` | `evidence_pack` 非空 |
| `select_angle` | `angles` 非空 |
| `generate_candidates` | `selected_angle` 已定 且 候选池未满 |
| `criticize` | `candidates` 非空 且 尚无 critic_result |
| `revise` | `critic_result.decision == revise` 且 未超 max_revisions |
| `finish` | 永远允许（终局出口） |

### F.5 安全兜底（safe fallback）

`finish` 时：

- 有 `critic_result.decision == accept` 的候选 → 用它；
- 否则有候选 → 取 critic score 最高的（**明确记为 fallback**，trace 里写明）；
- 一个候选都没有 → 落一条**空正文** Draft。

空正文不是偷懒：它让 `guardrail` 给出 `empty_content`，Run 照常停在
`waiting_for_review`，人看到的是"这次 Agent 没产出内容"这个**事实**，
而不是一个假装成功的占位文本。**绝不用占位文案冒充产出。**

---

## G. 与现有 HITL / Publish 的连接

| 现有机制 | 是否改动 | 说明 |
|---|---|---|
| `human_review` 节点 | **不改** | 仍读 `draft_content` / `draft_id`，行为逐字不变 |
| `guardrail` 节点 | **不改** | 仍读 `draft_content` |
| `POST /api/runs/{id}/review` | **不改** | CAS + resume 逻辑原样 |
| `PublishService` | **不改** | 仍交叉校验 checkpoint 的 `final_content` |
| `published_post` 四状态 | **不改** | |
| `canPublish` 规则 | **不改** | |
| `review` 表 | **不改** | |
| `AdminService` 只读边界 | **不改** | 只新增一个读 `agent_decision` 的方法 |

`finalize` 是唯一的新写入点，它写的是**已经存在**的 `draft` 表，用的是**已经存在**的
`create_draft()`。没有新的写路径，就没有新的漂移面。

`PublishService` 与 `AdminService` 继续拿 Runtime 上的 graph 做 `aget_state`：
两个 Graph 共用**同一个 `RunState`**（同一套 channel schema），
因此无论 Run 由哪条拓扑执行，`aget_state` 读到的字段映射都一致。

---

## H. 测试策略

全部 mock，**不消耗真实 X credits、不调真实 LLM**（工作单第二十二节）。

| 层次 | 测什么 | 需要 DB？ |
|---|---|---|
| 纯函数 | `allowed_actions` / 回退顺序 / 路由映射 / evidence 可追溯性校验 | 否 |
| Schema | `AgentDecision` 合法值、非法 action、缺 reason、arguments 默认值 | 否 |
| Tool | 每个 tool 的输出结构 + evidence 的 `source_ids` 必须落在已落库的 research_item id 集合内 | 部分 |
| Graph | 用**脚本化 FakeLLM**（按 prompt 特征返回不同 JSON）驱动完整 loop，真实 PostgreSQL checkpointer | 是 |
| 回归 | `finish → waiting_for_review`、interrupt/resume、review 三态 | 是 |
| 预算 | 每项预算触顶后必然收敛到 `finish`；构造"LLM 反复要 research"的死循环输入，断言在 `max_steps` 内停 | 是 |

**Evidence 可追溯性**是本阶段最容易做成恒真测试的一项，因此断言写成
"每个 fact 的每个 source_id 都必须在本次运行真实落库的 research_item id 集合里"，
并用**变异验证**（把校验去掉后测试必须失败）确认它不是空转。

FakeLLM 的脚本化：现有 `FakeLLMService` 是"永远返回同一段文本"。
Agent loop 需要按步返回不同 JSON，所以新增一个
`ScriptedLLMService`（按调用序号 / prompt 关键词返回预设 JSON），
放在 `tests/fakes.py`，不改动现有 `FakeLLMService`（186 条测试继续用它）。

---

## 实施顺序（小 commit）

1. `feat: add agent decision model` —— schemas + budget + agent_decision 表 + migration
2. `feat: add planner and agent tools` —— planner（预算强制）+ 7 个 tool
3. `feat: add autonomous research loop` —— build_agent_graph + 条件路由 + finalize
4. `feat: persist agent decision trace` —— Admin API + 控制台最小时间线
5. `test: cover agentic loop`
6. `docs: document phase-5a agent brain`

---

## 明确不做（工作单第十九节）

Style Distillation、高粉账号样本库、Trend Intelligence DB、Fine-tuning、Embedding、
Vector DB、pgvector、Redis、Celery、Kafka、Scheduler、Autonomous Publish、
X metrics feedback、Multi-agent、多账号、RAG。
