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
                        ├─→ generate_candidates                     ─→ planner
                        ├─→ criticize                               ─→ planner
                        ├─→ revise                                  ─→ planner
                        └─(finish)→ finalize → guardrail → human_review → END

POST /api/runs/{id}/review   ← 完全不变
POST /api/runs/{id}/publish  ← 完全不变
```

同一张编译好的 Graph 在 START 处分岔：`goal` 为 None 时走既有的
`START → research → generate → guardrail → …`（Phase 1–4a 行为，逐字不变），
非 None 时走上面的 planner 环。

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

Planner 只调 `tools/base.py::complete_json(llm_service, prompt, AgentDecision)` 拿一个
`AgentDecision`，**不碰 SQL、不碰 httpx**：那条 helper 顺带把 `ValidationError` 折成
`LLMError`（上游给了非法输出 → 502，与 `GeneratedDraft` 同一套处理），失败路径也就只有
一条。落库走 `create_decision()`（仓储层），节点里一行 SQL 都没有。

---

## C. 新增数据模型

### C.1 改 `agent_run`（2 处，均为放宽而非改写）

| 列 | 变更 | 理由 |
|---|---|---|
| `goal` | **新增** `TEXT NULL` | Agent 的输入是目标，不是题目 |
| `topic` | `NOT NULL` → **允许 NULL** | 主题由 Agent 自己发现；创建 Run 时还不知道 |

旧行不受影响（`topic` 全部有值、`goal` 全为 NULL → 全部走旧链路）。
`goal` 与 `topic` 都对外暴露为 `str | None`，且**是两个字段、两处显示，从不互相顶替**：
题目没选出来时表里显示 `—`、抽屉标题退回中性的「Run 详情」，goal 另起一行标为「目标」。
回退成 `topic ?? goal` 会把"人的输入"渲染成"Agent 的选择"，看着像 Agent 已经选好题了 ——
那是静默失真，比一个 `—` 危险得多。

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
- **所有执行节点一律固定边回 `planner`，一个固定边都不加。** 这一段在设计审计里本来
  写的是 `generate_candidates → criticize` 固定边，实施时改掉了，理由见下面的 I.1：
  工作单第四节把 `criticize` 列为 Planner 的动作，固定边会让它永远轮不到 Planner 选，
  schema 也就成了一句空话。
- "生成之后必须评价"这条约束并没有丢，只是换了实现：它变成了 `finish` 的**前置条件**
  （见 F.4 的 `ready_to_finish`），由纯函数把关，而不是由图的形状把关。
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

1. Planner 节点**先**算 `allowed_actions(state, budget)` —— 纯函数，
   把前置条件不满足、计数已用尽的 action 全部剔除。
2. `step_count >= max_steps` → **不调用 LLM**，按回退顺序收尾（见下面的 WRAPUP）。
3. 调 LLM 拿到 decision 后，用 `allowed` 过滤：
   - 选中的 action 不在 `allowed` 里 → 按**确定性回退顺序**取第一个 allowed，
     并**丢弃 target / arguments**（它们是针对原动作的，换动作后含义就变了）；
   - `allowed` 为空 → `finish`。
4. 回退也要留下 trace（`reason` 写明"原决策 X 不可行，改为 Y"），否则审计会看到
   Agent"莫名其妙"做了别的事。

回退顺序（从"最接近成品"到"最接近从头再来"）：
`revise → criticize → generate_candidates → select_angle → generate_angles →
extract_evidence → deepen_research → research_topic → discover_topics → finish`

顺序以 `finish` **结尾**而不是开头：`finish` 是唯一永远可行的动作，放最后保证回退一定
终止。设计审计里原本把 `finish` 排在第一位，实施时改掉了 —— 那样"研究预算用尽但手上
有材料"会直接结束这一轮，而正确做法是拿现有材料继续往下走（`extract_evidence`）。

**WRAPUP：预算用尽不等于立刻结束。** 工作单第三节要求的是"预算耗尽 → 停止 Agent →
安全兜底 / 人工审核"，而不是"什么都没有就结束"。所以 `step_count >= max_steps` 之后仍
允许沿回退顺序走完收尾（`WRAPUP_ALLOWANCE = 6` 步）。它不是无限循环，因为有三重上界：
收尾不调用 LLM（纯函数）、收尾动作沿 `FALLBACK_ORDER` 单调推进、以及一个最笨的硬停
`steps_hard_exhausted`（`step_count >= max_steps + 6`）。硬停那一步的强制 `finish`
会绕开可行性过滤 —— 那一步的意图恰恰是"没有动作可行了，出去"。

### F.3 预算计数的口径

| 计数 | 何时 +1 | 上限 |
|---|---|---|
| `step_count` | 每次 planner 决策 | `max_steps` = 12（收尾另有 6 步余量） |
| `research_count` | `discover_topics` / `research_topic` / `deepen_research` 各 +1 | `max_research_calls` = 4 |
| 候选池大小 | 不另设计数器，直接取 `len(state["candidates"])` | `max_candidates` = 3 |
| `revision_count` | 每次 `revise` | `max_revisions` = 2 |

候选池没有独立的计数器字段：`candidates` 列表本身就是要看的那份数据，多一个计数器就
多一个可能与它对不上的状态源。`writer_epoch` / `critic_epoch` 同理（见 F.4）。

`max_candidates` 的口径取"候选池大小上限"（不是"轮次上限"）：一轮 `generate_candidates`
产出 `candidates_per_round` = 3 个候选即填满池子，之后 Planner 只能 `revise` 或 `finish`。
这样这个参数是一个**真正会触发的预算**，而不是一个永远够用的摆设。
（`revise` 是**替换**池中的候选，不是新增，所以池子大小恒 ≤ 3。）

### F.4 前置条件表

| action | 前置（`app/agent/budget.py::allowed_actions`） |
|---|---|
| `discover_topics` | 还没发现过候选选题 且 研究预算未用尽（发现也计入研究预算，否则"发现不出东西"可以无限重试） |
| `research_topic` | `candidate_topics` 非空 且 研究预算未用尽 |
| `deepen_research` | `selected_topic` 已定 且 已有 research 且 研究预算未用尽 |
| `extract_evidence` | 已有 research（预算用尽不影响它 —— 手上的材料照样能往证据推进） |
| `generate_angles` | `evidence_pack` 里**至少有一条事实**（抽出全部事实被丢弃时不算有证据） |
| `select_angle` | `angles` 非空 且 **还没有候选**（候选池满时重选角度只会让角度与候选对不上） |
| `generate_candidates` | `selected_angle` 已定 且 候选池未满 |
| `criticize` | 候选池非空 且 `writer_epoch > critic_epoch`（**还没评价过这一版**，否则同一批候选可以被反复评价） |
| `revise` | `critic_result.decision == "revise"` 且 未超 `max_revisions` |
| `finish` | `ready_to_finish`：有候选 **且** `writer_epoch <= critic_epoch` 且未被改稿推翻 |

`ready_to_finish` 就是"写出来的东西必须被评价过才能交付"这条链路的落点：`revise` 会推进
`writer_epoch`，于是改完必须重新评价，"改完就算过"做不到。而评审次数不受 `max_revisions`
限制 —— 预算管的是改稿，不是检查。

只有 `finish` 不是"永远允许"：一个没有正文的 `finish` 必然失败（`finalize` 会抛
`AgentIncompleteError`），把它摆在选项里等于给 Planner 一次注定失败的选择。

### F.5 收尾时交哪一条（`pick_final_candidate`）

排序规则（`app/agent/nodes/finalize.py`）：

1. Critic 评价过、且不是 `reject` 的，**分数高的优先**；
2. 没被评价过的（分数未知）次之；
3. 被 `reject` 的垫底。

被 `reject` 的排最后而不是直接丢掉：真到只剩它可选时，交一条明确被否过的稿子并**保留
那条否决记录**（它还在 `critic_results` 里，控制台与审核工作台看得到），比让整个 Run
失败更有用 —— 人能直接看到"Agent 努力过，但评价说不行"。

**一个候选都没有 → Run 明确失败**（`AgentIncompleteError`，错误信息带上步数/研究次数/
材料条数）。这条在设计审计里原本写的是"落一条空正文 Draft"，实施时改掉了：下游需要的
是一条真正文才能进人工审核，而"给人工审核一条空内容"比"明确失败"更糟 —— 审核者会以为
流程是好的。失败比假装成功诚实，也说明预算或 prompt 需要调整（错误信息正是为此带的）。
**绝不用占位文案冒充产出。**

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
| `AdminService` 只读边界 | **不改** | `run_detail` 多读一处 `agent_decision`（`list_decisions` 早已存在）。仍然一个写端点都没有 |

`finalize` 是唯一的新写入点，它写的是**已经存在**的 `draft` 表，用的是**已经存在**的
`create_draft()`。没有新的写路径，就没有新的漂移面。

`PublishService` 与 `AdminService` 继续拿 Runtime 上的 graph 做 `aget_state`。
**两条拓扑编译成同一个 Graph**（START 处按 `goal` 是否为 None 分岔），而不是两个
Graph：Runtime 上只有一个 graph 引用，`PublishService` 就不需要知道"这次该用哪个"。
两个 Graph 的写法要求每个持有 graph 的地方都做一次同样的判断，而判断错的那一次恰好
落在最不能出错的地方 —— 发布前对 checkpoint 的交叉校验。

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

## I. 实施记录：与这份审计稿的偏离

审计完成后直接实施（工作单第二十一节），过程中改掉了审计稿里的五处设计。记在这里
是因为"为什么不是最初那个设计"比设计本身更容易丢：

| # | 审计稿写的 | 实际实现 | 为什么改 |
|---|---|---|---|
| I.1 | `generate_candidates → criticize` 固定边 | 无边，改为 `finish` 的前置条件 `ready_to_finish` | 固定边会让 `criticize` 永远轮不到 Planner 选，而工作单第四节把它列为 Planner 的动作 —— schema 会变成一句空话。约束改由纯函数把关，图的形状更简单 |
| I.2 | 回退顺序 `finish` 排第一 | `finish` 排最后 | 研究预算用尽但手上有材料时，正确答案是继续往下走，不是结束这一轮 |
| I.3 | 预算用尽 → 直接 `finish` | 预算用尽 → 沿回退顺序收尾（`WRAPUP_ALLOWANCE = 6`），另有硬停 | 工作单第三节要的是"安全兜底 / 人工审核"，不是"什么都没有就结束" |
| I.4 | 无候选 → 落一条空正文 Draft | 无候选 → `AgentIncompleteError`，Run 失败 | 空正文会让审核者以为流程是好的 |
| I.5 | 前端 `topic ?? goal` | 两个字段分列显示，题目缺失显示 `—` | 顶替会把"人的输入"渲染成"Agent 的选择" |

另外两个字段级的口径变化（候选池不设计数器、`critic_epoch` 直接取 `writer_epoch`）
已分别写进 F.3 / F.4。

## 实际提交（工作单第二十四节的小 commit 建议）

| commit | 内容 |
|---|---|
| `5114a41` | `feat: add agent decision model` —— schemas + budget + `agent_decision` 表 + migration + 仓储层 |
| `3901fc8` | `feat: add the autonomous planner loop and its tools` —— planner（预算强制）+ 9 个动作工具 + Graph 分岔 + finalize + 63 条测试 |
| `e05ea11` | `feat: surface the agent decision trace in the console` —— Admin API 暴露 `decision_trace` / `goal` + 控制台最小时间线 |
| 本次 | `docs: document phase-5a agent brain` |

审计稿原本建议拆成六条，实际是三条：工具的注册表与 Graph 的节点一一对应、缺任何一组
都会让测试红灯，所以 planner + tools + graph 合成一条可运行、可验证的提交；
"persist agent decision trace" 的**写入**部分也已经在 `3901fc8` 里（Planner 落库是它
的一部分），`e05ea11` 只负责**读出来**。

测试：186（Phase 4b 结束时）→ 252 后端 + 9 前端。关键用例做了变异验证 —— 去掉证据
可追溯性校验、`ready_to_finish` 闸门、研究预算、改稿预算、可行性过滤、决策轨迹排序中
的任意一条，都有对应用例立刻变红。

---

## 明确不做（工作单第十九节）

Style Distillation、高粉账号样本库、Trend Intelligence DB、Fine-tuning、Embedding、
Vector DB、pgvector、Redis、Celery、Kafka、Scheduler、Autonomous Publish、
X metrics feedback、Multi-agent、多账号、RAG。
