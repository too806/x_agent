# Phase 5B.4 — Feedback Loop Foundation

**范围**：让 Agent 第一次**看见自己过去真实做过什么、结果如何**。只建上半环：

```
真实发布结果 → 数据采集 → 结果归因 → 可解释反馈信号 → Agent 可读取
```

**基线**：开工时 `master` HEAD `a6f67b7`（Phase 5B.3 COMPLETE：backend 555 通过 /
frontend 61 通过）。**封版 HEAD**：`2a73341`——Final Integration & Baseline
已以 `--ff-only` 并入 `master`（T 节）。

**分支**：`feat/phase-5b4-feedback-loop`，**独立 worktree** `X_Agent-phase-5b4`
（工作单第四十三节）。**开发期间**`master` 保持 `a6f67b7` 不动；收口时才快进合并。

**状态**：**Complete**（Foundation = Complete，Real-world Validation = Pass）。
Foundation 部分见 A–R 节；**Real-world Validation**（一次真实、人工确认的 X 发布，
把整条链在真实数据上跑通）见 `docs/phase-5b4-real-validation.json` 与 S 节；
合并与最终验证见 T 节。

> 说明：I 节的 Smoke 1/2/4 在**当时**记 `blocked`——那时库里确实一条已发布帖子都没有。
> 那块缺口后来由 Real-world Validation 补上了（S 节）。两节都保留，因为"当时没跑"
> 与"后来用真实数据跑了"是两件事，不能拿后者覆写前者。

**结论**：完成，并且**只完成上半环**。三层认知能力（Trend / Style / Viewpoint）之后
第一次出现"自己行为的结果"这一层，但本阶段**不许它改变自己**：

```
Trend      = 当前外部世界
Style      = 外部高质量语料
Viewpoint  = 当前证据下的判断
Feedback   = 自己过去行为的真实结果        ← 本阶段新增，且只到这里
```

**Foundation 阶段没有真实发布**：那时 `POST /2/tweets = 0`，反馈采集只有
`GET /2/tweets?ids=…`，且真机上一次都没有发出（库里没有可采集的帖子）。
这块缺口由后来的 Real-world Validation 补上——**本阶段总计真实
`POST /2/tweets = 1`**（S 节），此后没有再发过第二条。

---

## A. 交付概览

**新增**

```
app/models/post_metrics_snapshot.py            一张新表，只追加
alembic/versions/e5f1a9c4d20b_phase_5b4_….py   手写迁移（revises c3d8a45e7f21）
app/feedback/__init__.py
app/feedback/schema.py                         FeedbackSignal / FeedbackReport / FeedbackContext / SnapshotObservation
app/feedback/metrics.py                        确定性派生度量（纯函数，无 IO）
app/feedback/signals.py                        封闭信号枚举 + 因果/指令措辞守卫
app/feedback/report.py                         报告与上下文的组装
app/feedback/profile.py                        结果归因（读 checkpoint，不写库）
app/feedback/runtime.py                        resolve_feedback_context
scripts/refresh_post_metrics.py                人工采集 CLI（GET-only，无调度）
scripts/smoke_5b4.py                           工作单第四十一节的五个 smoke
evals/feedback/{__init__,cases,selftest}.py    六类 case + 12 项检查
evals/fixtures/feedback_observed.json          干净 fixture（RULE-024 pass）
evals/fixtures/feedback_causal_language.json   因果 fixture（RULE-024 fail）
tests/test_feedback_metrics.py
tests/test_feedback_signals.py
tests/test_feedback_attribution.py
tests/test_feedback_repository.py
tests/test_feedback_writer_integration.py
tests/test_admin_feedback_api.py
tests/test_x_api_metrics.py
frontend/src/utils/feedback.ts                 纯渲染层
frontend/tests/feedback.test.ts
docs/phase-5b4-smoke-runs.json                 五个 smoke 的运行记录
```

**改动**：`app/agent/{prompts,state}.py`、`app/agent/tools/candidates.py`、
`app/models/__init__.py`、`app/repositories.py`、`app/schemas.py`、
`app/services/{admin,x_api,x_service}.py`、`app/api/admin.py`、
`app/db/checkpointer.py`、`prompts/agent/candidates.txt`、
`evals/{behavior/rules,model,selftest}.py`、`evals/datasets/goals.json`、
`tests/{conftest,fakes,support,test_admin_api}.py`、
`frontend/src/{api/client.ts,components/RunDetailDrawer.tsx,pages/Analytics.tsx,pages/Published.tsx,types/api.ts}`。

**只新增一张表。没有新增 AgentAction，没有新增图节点，预算一处未动。**

---

## B. 只读审计结论（写代码之前先指认问题）

工作单假设的链路是 `PublishedPost → X metrics snapshot`。先查系统里到底有什么：

```
agent_run            137
draft                 72
review                 1
published_post         0        ← 一条都没有
post_metrics_snapshot  不存在
```

三个结论直接决定了本阶段的形状：

1. **`published_post` 是空的。** 从来没有发布过任何内容。所以工作单第三十四节的
   "用历史真实帖子验证"**没有对象**——这不是"没跑到"，是没有可跑的东西。
   如实记进 K 节，不拿合成数据顶替（§35）。
2. **X 客户端读不到我们自己帖子的指标。** 改之前只有
   `GET /2/tweets/search/recent`（`x_api.py:176`，app-only）和
   `POST /2/tweets`（`:213`，OAuth 1.0a）。**没有按 id 查的路径**，
   `impression_count` / `bookmark_count` 在全仓库不存在。采集要从零加一条 GET。
3. **归因要的那些输入全是 checkpoint-only。** `selected_angle`（5B.3）、
   `style_recipe`（5B.2，当时明确决定"刻意不落库"）、`critic_result`、
   话题的 trend 分——都不在数据库里。所以归因只能在**分析时读 checkpoint**
   （`viewpoint_summary(state)` 的先例，`admin.py:288-361`），
   **不能靠加列**：那会把前一个阶段特意做过的决定反过来。

---

## C. 数据模型：`post_metrics_snapshot`

一条帖子一行，只追加：

| 列 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `id` | UUID PK | 否 | `UUIDPrimaryKeyMixin` |
| `published_post_id` | UUID FK → `published_post.id` CASCADE | 否 | 归因的接点 |
| `x_post_id` | String(255) | 否 | 从父行拷来，查询键 |
| `observed_at` | timestamptz | 否 | **我们的**抓取时刻，一个批次共用一个 |
| `hours_since_publish` | Float | 否 | `observed_at − published_post.published_at`，写入时算好 |
| `likes`/`replies`/`reposts`/`quotes` | Integer | **是** | NULL = X 没返回这一项（§10） |
| `impressions`/`bookmarks` | Integer | **是** | **v1 从不写入** |
| `created_at` | timestamptz | 否 | `CreatedAtMixin`（仅 DB 层） |

- `UniqueConstraint(x_post_id, observed_at)`——**不是** `observed_at` 单独唯一：
  一个批次里多条帖子合理地共享同一个时刻。插入走 `ON CONFLICT DO NOTHING`，
  重复跑幂等（`test_the_same_instant_is_idempotent`）。
- `x_post_id` 与 `published_post_id` 各一个索引。
- **只存原始事实。** 派生指标逐位可复算，落成列就是造第二个会漂移的真相来源
  （同 `style_sample` 的理由，迁移 `c3d8a45e7f21:43-45`）。唯一例外是
  `hours_since_publish`：它**不能**从本行重算（那要 join 回 `published_post`）。
- 采集脚本**跳过** `x_post_id` 或 `published_at` 为 NULL 的帖子，而不是给一个
  "年龄未知"的快照（`test_only_confirmed_published_posts_with_an_id_are_listed`）。
- 模型上**没有** `updated_at`，测试直接断言这一点
  （`test_snapshot_model_has_no_update_path`）：有了它，将来一定会有人 UPDATE 掉
  "当时的观察"，而快照的价值恰恰在于它永不被改写。

**为什么配得上进数据库**（迁移 docstring 里逐条论证）：checkpoint 装的是
"这次运行决定了什么"，Run 结束就只是历史；本表装的是**外部世界对我们过去行为的
事实**，读它的可能是三个月后的另一次 Run。checkpoint 会被 GC，这个问题不会。

---

## D. 时间语义：`observed_at` / `published_at` / 分档（§4/§5）

三个时间刻意分开，全仓库同一条纪律（`app/schemas.py:10-31`）：

| 时间 | 谁的 | 在哪 |
|---|---|---|
| `published_at` | **来源给的**发布时刻 | `published_post`（不复制进快照） |
| `observed_at` | **我们的**抓取时刻 | `post_metrics_snapshot` |
| `hours_since_publish` | 两者之差 | 写入时算好，存下来 |

**为什么不请求 `created_at`**：`published_at` 是来源给定的事实，已经在
`published_post` 上；`GET /2/tweets` 再取一次 `created_at` 等于让同一个事实有
两个可能不一致的来源。所以 v1 的 `tweet.fields` **只有** `public_metrics`
（`x_api.py:244`）。

**观察窗口是纯函数**（`app/feedback/metrics.py:31-36`）：

```
WINDOW_BOUNDARY_HOURS = (1, 6, 24, 72)
WINDOW_LABELS         = ("0-1h", "1-6h", "6-24h", "24-72h", ">72h")
```

只要 `hours_since_publish` 存下来了，任何时候都能分档——**不需要一个有状态的
调度器按时去"采样第 1 小时"**。工作单第五节的 Day 0/1/3 因此是数据形状，
不是基础设施要求。

---

## E. 派生指标（§6）

`app/feedback/metrics.py` 是纯的：没有 IO、没有随机、没有 `datetime.now()`、
不读配置（`app/agent/viewpoint.py` 的先例）。**不落库**。

| 量 | 定义 | v1 |
|---|---|---|
| `engagement_total` | 四类计数之和 | **任一项未知 → `None`** |
| `engagement_rate` | `total / impressions` | **恒 `None`**（v1 没有曝光） |
| `reply_rate` / `repost_rate` | `replies|reposts / impressions` | 恒 `None` |
| `attention_share` / `conversation_share` / `amplification_share` | 各占 `total` 的比例 | 有 total 就能算 |
| `window_bucket(hours)` | 见 D 节 | 只要小时数就能算 |

两条硬规则，写进模块 docstring 并各配测试：

1. **求和是"全有才求和"**。把已知的三项加起来，会把"未知"变成一个更小的、
   看起来真实的数字——那是本阶段口径上最严重的错误类型。
2. **比率的分母缺失或 ≤ 0 → `None`**，永远不是 `0.0`。分母为 0 时"比例是多少"
   没有被定义，而 `0.0` 会被读成"测出来是零"。

`engagement_rate` 明知恒为 `None` 也留着，是给将来的来源一个明显的落点。

---

## F. 信号与报告（§9/§10）

`app/feedback/signals.py` + `report.py`。

**信号是封闭枚举**（`FeedbackSignalKind`）：

```
attention           点赞的分布
conversation        回复 + 引用的分布
amplification       转发的分布
share               三类互动占互动总量的比例
insufficient_sample 样本量低于比较下限：只说 n
missing_metric      有观察缺某个计数：如实记"读不到"
```

这里**没有** `QUALITY` / `ENGAGEMENT_SCORE` / `RECOMMENDATION`，而且不该有。
工作单第十二节拆开的三类互动（注意力 / 对话 / 扩散）在代码里就是三个独立信号：
点赞永远不会被折算成一个"好帖"的判断，也不存在把四类计数合成单一分数的路径。

**小样本保护**（§17）：

```
MIN_SAMPLES_FOR_COMPARISON = 5
```

低于它 → **恰好一条** `insufficient_sample`，只带原始 `n`，没有任何比较。
v1 自己没有基线，所以**所有**信号的 `comparison_group` 都是 `None`，
全部只描述分布。`confidence` 也恒为 `None`——不存在校准过的概率，
凭空造一个正是 §10 禁止的编造。

**因果守卫**（§3/§19/§31）：每条信号的 `statement` 都是代码用观察性模板生成的，
返回报告之前过一次 `assert_no_causal_claim`，对封闭的中英双语标记表
（`因为…所以` / `导致` / `证明了` / `带来了` / `caused` / `proves` / `leads to` /
`drives` …）扫描。命中的唯一解释是有人改了模板。

**可追溯性**（§25）：每条信号的 `evidence` 都是非空的快照 id 元组，
且这些 id 必须真的出现在报告的 observations 里。**没有依据就没有结论**。

`FeedbackReport(sample_count, observations, signals, limitations)` 的
`limitations` 把"没算的东西"逐条列出来，包括"曝光量未知，因此互动率无法计算
（不是零互动率）"。

---

## G. 结果归因：Post Performance Profile（§13/§14）

`app/feedback/profile.py::build_post_performance_profile` 是薄的 async，
**只读 checkpoint，不写 SQL**。它把

```
快照序列 → published_post（正文长度 = 实际交付长度）
        → agent_run（话题）
        → checkpoint per-row（graph.aget_state(thread_id=run_id)）
            ├ selected_topic + candidate_topics 的 trend 分
            ├ selected_angle（复用 viewpoint_summary）
            ├ style_recipe（结构 / hook / 目标长度）
            └ critic_result
```

接起来，**任何一个字段取不到就是 `None`，不是猜**。checkpoint 读失败时
降级成"只有数据库字段 + 一条 limitation"，**不返回 500**
（`admin.py:376-390` 的 `_guardrail` 姿态）。读取上限
`FEEDBACK_PROFILE_READ_LIMIT = 50`，超过记 `None`（"这次没读"），不是猜。

**没有任何东西被反规范化进快照表。** trend 分是**读出来的记录值**——
反馈永远不会给当前这次运行的 trend 打分（§27）。

---

## H. Agent 如何消费（§11/§42）

**只有一个入口：Writer。** 具体是 `generate_candidates`，与 `style_recipe` 并列。

- `app/agent/state.py`：一个 checkpoint-only 键 `feedback_context`
  （pydantic `BaseModel`，同 `StyleRecipe`，保证 checkpoint 的 serde 仍是
  JSON 可序列化的）。
- `app/feedback/runtime.py::resolve_feedback_context`：结构与
  `resolve_recipe`（`app/style/runtime.py:32-86`）一致。一次运行内后续轮次
  直接返回已有值（运行内稳定）；任何 DB / checkpoint 失败都记日志并返回
  **空的中性上下文**——反馈是增强，不是前置条件（§38）。
- `app/agent/tools/candidates.py`：在 recipe 旁边解析，并在**所有**
  `ToolResult` 返回路径上带 `feedback_delta`，与 `style_delta` 完全一样。
- `app/agent/prompts.py::format_feedback_context`：**永远返回一段文本**。
  无数据时返回"还没有采集到历史结果"这句交代，而不是空串（`render()`
  不会因缺键报错，漏传会让字面量 `{{FEEDBACK}}` 进入 prompt）。
  渲染完再过一遍 `assert_observational`；**守卫失败不炸掉运行**——
  记一条 error、丢掉摘要，而不是让一次措辞失误带走整个 Run。
- `prompts/agent/candidates.txt`：`{{FEEDBACK}}` 块放在 `{{VIEWPOINT}}` 之后，
  标注为 *本账号过去已发布内容被观察到的结果——背景，不是指令*。

**Writer 拿不到的东西（§42 逐条）**：

```
✗ 历史几十篇完整帖子        —— FeedbackContext 里没有任何正文
✗ 「这类帖子以前有 300 likes」 —— 没有单帖计数，没有"这类"的归类
✗ 任何绝对指令              —— 文案自称"背景参考"，且有指令性措辞守卫
```

**别的地方一处都没动**：没有新节点、没有新 `AgentAction`
（`graph.py:64-75` 的 `TOOL_ACTIONS` 未触碰）、预算 / `step_count` 未变（§39）、
Critic 的 prompt 里没有反馈、没有任何路径用反馈写 `trend_score`、
没有与 StyleRecipe 合并。

---

## I. 真实 Smoke（工作单第四十一节）

工作单列了五个。**本阶段只跑得动两个**，另外三个跑不了（§35：不编）。

| Smoke | 看什么 | 本阶段 | 结果 |
|---|---|---|---|
| 1 | 历史帖子 `PublishedPost → X metrics snapshot` 的数据链 | **跑不了**（`published_post` 0 行） | `blocked` |
| 2 | 多快照（Day 0 / 1 / 3）的时间序列 | **跑不了**（同上） | `blocked` |
| 3 | 小样本 `n < threshold` → `insufficient_sample` | 跑（合成观察） | ✅ 通过 |
| 4 | 样本足够 → 生成 `FeedbackReport` | **跑不了**（同上） | `blocked` |
| 5 | Agent 读 Feedback Context；没有也照常跑 | 跑（合成上下文） | ✅ 5a / 5b 都通过 |

**Smoke 3**（4 条观察，`threshold=5`）：信号只有一条 `insufficient_sample`，
比较类信号 **0** 条，`comparison_group` 与 `confidence` 都是 `None`，
evidence 4 条，因果标记命中 **0**。Writer 看到的块 398 字符，里面唯一一句反馈
就是守卫原句「当前可用的观察只有 4 条，低于比较下限 5，本期不产出任何分组比较
或比率结论。这个下限是采集量的门槛，不是统计显著性判断。」
——**低于下限时上下文里没有任何可被当成结论的分布。**

**Smoke 5a**（有反馈，`run_id=614b57ac…`，9 步，停在人工评审）：
8 条观察 / 1 条已发布内容 / 4 条信号（`attention` / `conversation` /
`amplification` / `share`），`comparison_group` 全 `None`、`confidence` 全 `None`、
evidence 全非空。Writer 的块 562 字符，只有分布与局限。Run 正常走到
`human_review` 并挂起——**反馈没有让它改变动作、也没有增加步数预算**。

**Smoke 5b**（无反馈，`run_id=ca4d4d72…`，11 步）：`{{FEEDBACK}}` 渲染成
53 字符的一段说明（「本次没有可用的历史结果观察……按上面的目标与证据自行写作。」），
**不是空白、也不是未替换的占位符**，Agent 照常跑完并挂起。

**X 请求账**：本次 smoke 共 6 个请求，**全部是 GET**，
`POST /2/tweets = 0`。6 个都是 `GET /2/tweets/search/recent`（Agent 自己的研究检索）。
`GET /2/tweets?ids=…`（本阶段新增的采集路径）真机一次都没发出——
库里没有可查的帖子。

**1 / 2 / 4 为什么是 `blocked` 而不是 `skipped`**：它们问的都是"我们自己发出去的
东西后来怎么样"，而这需要库里真的有已发布帖子。一条都没有——不是"没跑到"，
是**没有可跑的对象**。Smoke 2 / 4 想验证的**性质**（时间序列分档、样本足够时
产出报告）在 `evals/feedback/selftest.py` 里有确定性的离线对照
（`multiple_windows` / `healthy_sample` 两个 case）。那份对照证明的是**算法**，
不是**数据链**——两者的区别正是 1/2/4 仍然算"没跑"的原因。

**Smoke 5 用合成上下文，且不写数据库**：刻意**不往库里塞假的 `published_post`**。
那张表是"我们到底发出去了什么"的审计记录，往里写一条没发过的帖子，
就是把审计记录变成一份假的。合成的 `FeedbackContext` 直接注入初始 state。

**只读守卫**：与 `scripts/smoke_5b1/5b2/5b3.py` 同一套——`RealXApiClient._request`
被替换，非 GET **在发出去之前**就抛。报告末尾给出请求总数、按方法分组，
以及 `POST /2/tweets` 的计数。

运行记录见 `docs/phase-5b4-smoke-runs.json`（每次运行追加进 `runs`）。

---

## J. 只读面：Admin API 与前端

**`GET /api/admin/feedback`**（`app/api/admin.py:153`）：

- `FeedbackOverview`：`available` / `reason` / `sample_count` / `snapshot_count` /
  `latest_observed_at` / `signals` / `limitations` / `posts`。
- 0 条快照时返回 `available=false` + `reason`，**不是一堆 0**
  （`MetricAvailability` 的纪律，`admin.py:720-724`）。
- **只有 GET**：`POST /api/admin/feedback` → 405，测试里直接断言。
  admin API 的既有边界（`app/api/admin.py:1-20`）一个字没改。
- 报告是**读时现算**的，不是读 CLI 写出的 JSON 证据文件——
  那份文件是证据，不是 API 的数据源。

`analytics()` 的 `x_content_performance`：只在 `snapshot_count == 0` 时保持
`available=false`；有快照后转为可用并附口径说明（只有公开计数、人工刷新）。
这去掉了一句现在已经不成立的"未实现"，而没有引入任何聚合性能计算。

**前端**（最小、只读）：

- `frontend/src/utils/feedback.ts`（纯函数，可被 `node --test` 直接测）：
  `metricLabel(null) → '暂无数据'`，**永不** `'0'`；
  `hoursSinceLabel`、`observationLine`、`signalHeadline`、
  `limitationLines`、`FEEDBACK_DISCLAIMER`。
- `RunDetailDrawer.tsx`：`FeedbackSection`（这次运行看到了什么）与
  `PerformanceSection`（这条帖子被观察到了什么）**分成两块**，按时间先后排，
  对应后端两个独立字段。
- `Published.tsx`：新增「最新观察」列——这是这张表上**唯一会随时间变化**的列。
- `Analytics.tsx`：`FeedbackBlock` 区分三种"没有反馈"：请求出错 /
  `available=false` 带原因 / 有数据但为空。

---

## K. 已知限制（§35）

1. ~~**库里没有任何已发布帖子**~~ —— **已被 Real-world Validation 补上**（S 节）：
   2026-10-02 真实发布了 1 条（`x_post_id=2105998097302028593`），并采集了 2 条快照。
   在此之前，Smoke 1/2/4 记的是 `blocked` 而不是 `skipped`（当时确实没有可跑的对象）。
   仍然成立的残余部分：**样本只有 n=2**，证明的是数据链，不是任何内容规律。
2. **`impressions` / `bookmarks` 恒为 NULL**。v1 只读 `public_metrics` 里的公开
   计数；`engagement_rate` 因此恒为 `None`——公式在，分母那半没有。
   **真机验证为此修掉了一个缺陷**：X 的响应里其实**带着**这两个键（真机上是 `0`），
   原来的映射把它们读走并落成 0，反而让报告不再声明"曝光量未知"（见 S 节 D1）。
3. **只有自然产生的数据，没有实验设计**，所以任何因果关系在构造上就不可能
   得出。这不是"本阶段没做"，是"这样做不到"。
4. ~~**`GET /2/tweets?ids=…` 只在 mock transport 上验证过**~~ —— **已被真机验证**：
   该路径在 2026-10-02 真实发出并返回 200（S 节）。仍然成立的部分：只验证过
   **1 条 id、3 次调用**，多 id 分批（`MAX_LOOKUP_IDS=100`）仍只有 mock 覆盖。
5. **Agent 侧的反馈仍是 checkpoint-only**（与 `guardrail_*` / `style_recipe`
   一致）。checkpoint 被 GC 掉的运行拿不到归因的那一半字段——profile 会
   降级并附一条 limitation，不会崩。
6. **429 / 5xx / 传输失败没有退避**。CLI 每批只做一次有界尝试；出错就打印状态、
   **那一批不写任何行**、非零退出（全批中止）。阶段内没有可复用的退避实现，
   也不打算为它引入一个调度层。
7. **`MIN_SAMPLES_FOR_COMPARISON = 5` 是本阶段定的地板**（§17 要求一个明确下限
   但没给数值）。它是一个**采集量的门槛，不是统计显著性判断**——报告里
   逐字这么写。

---

## L. 明确没有做什么（工作单 §3/§28/§39/§42/§45）

```
✗ 自动修改 Prompt          ✗ 自动修改 StyleRecipe
✗ 自动修改 Budget          ✗ 自动修改策略
✗ 自动发布                 ✗ Fine-tuning
✗ Multi-agent              ✗ Vector DB / embedding
✗ A/B testing 基础设施     ✗ Celery / Redis / Kafka / cron
✗ 分布式调度器             ✗ 任何形式的自主发布
```

具体到代码：

- **没有**任何写路径把反馈写回 prompt 模板、`StyleRecipe`、预算或阈值。
- **没有**把反馈合并进 StyleRecipe（§28）。两者在 Writer 的 prompt 里是两个
  独立的块，来源也不同（外部语料 vs 自己的结果）。
- **没有**让反馈影响当前运行的 trend 分（§27）。
- **没有**综合分 / 排名 / "这条帖子更好"的判断（§12）。
- **没有**新增 AgentAction、图节点、预算（§39）。
- **没有**发布任何内容：`POST /2/tweets = 0`（§33）。
- **没有**调度器：采集是人工跑的 CLI，admin API 保持只读。

---

## M. 本阶段修掉的真实缺陷

1. **`metrics.get("like_count", 0)` 没有照抄。** 既有 `_map`
   （`x_service.py:111-115`）把缺失的计数补成 0——那是**别人发的**搜索结果的读法。
   新的 `fetch_public_metrics` 里，缺失的键映射为 `None`，X 没返回的 id 进
   `ids_not_returned` 而**不合成一行零计数**。补 0 会让"没观测到"变成
   "观测到零互动"，而这两句话含义相反。
2. **`PublishedRow` 原本没有指标字段。** 计划要求列表上有一列"最新观察"，
   逐行查会变成 N+1。改为一次批量两查的仓库函数
   `latest_metrics_by_post`，用 Postgres `DISTINCT ON` 取每帖最新一条 +
   分组计数；顺手修掉 SQLAlchemy 2.1 的
   `SADeprecationWarning: Passing expression to distinct …`（改用 `.ext(distinct_on(...))`）。
   这个函数**不返回**没有快照的帖子——返回一个计数为 0 的条目会让每一张
   没采过的帖子显示成"赞 0 回 0"，那是另一句话。
3. **`feedback context` 的守卫失败不炸运行。** 最初打算让
   `assert_observational` 抛出——那意味着模板里一个措辞失误会带走整个 Run。
   改为记 error + 丢掉摘要 + 返回一句中性交代。
4. **`kpis` 这个 CSS 类不存在**（真实的类名是 `kpi-grid`），`FeedbackBlock` 里改掉。
5. **前端测试里一条断言写错了对象**：断言的是"没有窗口时不出现 `·`"，
   而 `·` 是种类标签与 `n=` 之间**恒定存在**的分隔符。改成断言完整字符串
   `'注意力（点赞） · n=5'`。
6. **Smoke 3 的期望修过一次**：原本断言渲染结果里出现"还没有被采集过"，
   但 4 条观察的上下文**不是空的**，它带着 `insufficient_sample` 信号。
   改成断言守卫原句与"样本：4 条观察"出现在 Writer 看到的块里——
   这才是真正想要的行为（Writer 看到的是"数据不够"，不是一份分布）。

---

## N. 四层边界（§45）

```
Trend = 当前外部世界     —— 由 5B.1 提供，本阶段一个字没改
Style = 外部高质量语料   —— 由 5B.2 提供，feedback 不与之合并（§28）
Viewpoint = 当前证据下的判断 —— 由 5B.3 提供，feedback 不改当前 run 的 trend（§27）
Feedback = 自己过去行为的真实结果 —— 本阶段新增，输出是**观察**，不是指令
```

代码里的边界是硬的，不靠约定：

- `app/feedback/` 是**独立的包**，不在 `app/agent/` 里——与
  `viewpoint.py` / `style/` 放在一起等于邀请 §27/§28 的合并。它没有 IO、
  没有 `datetime.now()`、不读配置。
- 反馈进 Writer 的通道是**独立的 prompt 块**，不是 StyleRecipe 的字段。
- `comparison_group` 恒为 `None`：v1 没有任何基线，所以也没有"比上次好"这种
  跨时间的比较结论。
- 唯一的写路径是 `post_metrics_snapshot` 的 INSERT，而它拷进来的
  `hours_since_publish` 之外没有任何派生物。

---

## O. 逐条回答工作单第四十四节的 23 个问题

**1. 为什么需要 Feedback Loop**
前三层建的是 Agent 的**认知**：现在什么值得说（Trend）、怎么说（Style）、
说什么判断（Viewpoint）。它对自己做过的事一无所知——不知道发出去以后发生了什么，
也就没有任何依据在将来做得不同。5B.4 是第一次出现闭环的上半环：
`Agent 做过什么 → 发生了什么 → 它以后可以参考什么`。
**刻意只做上半环**：先让反馈数据本身成为可靠事实，否则下一步就会是
"一次爆款 → 当成规律 → 自动改策略 → 下次失败 → 再次自动改"，
从一个可审计的 Agent 变成一个不可解释的自动调参器。

**2. X metrics 当前能获得哪些**
`GET /2/tweets?ids=…&tweet.fields=public_metrics`（app-only Bearer）能给
**公开计数**：`like_count` / `reply_count` / `retweet_count` / `quote_count`。
**拿不到** `impression_count` 与 `bookmark_count`——它们需要 user-context 授权，
本项目没有那条路径。因此快照表里这两列先建（可空），v1 恒为 NULL，
**绝不写 0**。

**3. Snapshot 数据模型**
`post_metrics_snapshot`：`id` / `published_post_id`（FK，CASCADE）/
`x_post_id` / `observed_at` / `hours_since_publish` / 四个公开计数（可空）/
`impressions`·`bookmarks`（可空，v1 不写）/ `created_at`。
`UniqueConstraint(x_post_id, observed_at)`，`ON CONFLICT DO NOTHING`，
只追加，模型上没有 `updated_at`。详见 C 节。

**4. observed_at / published_at**
两个不同来源的时间，全程不混：`published_at` 是来源给的发布时刻，留在
`published_post` 上；`observed_at` 是我们的抓取时刻，一个批次共用一个。
`hours_since_publish` 是两者之差，**唯一**存下来的派生值——因为它不能从
快照行自身重算。`GET /2/tweets` 刻意**不请求 `created_at`**：不让同一个事实
有两个可能不一致的来源。

**5. observation windows**
纯函数分档，边界 `(1, 6, 24, 72)` 小时 → `0-1h | 1-6h | 6-24h | 24-72h | >72h`。
只要 `hours_since_publish` 存下来了，读的时候随时能分——**不需要调度器按时采样**。

**6. derived metrics**
`engagement_total`（四类之和，任一未知即 `None`）、`engagement_rate` /
`reply_rate` / `repost_rate`（需要曝光，v1 恒 `None`）、
`attention_share` / `conversation_share` / `amplification_share`、
`window_bucket`。全部**不落库**：逐位可复算，存一份只会多一个会漂移的真相来源。

**7. small sample protection**
`MIN_SAMPLES_FOR_COMPARISON = 5`。低于它 → 恰好一条 `insufficient_sample`，
只带原始 `n`，零比较结论，`comparison_group` 与 `confidence` 都是 `None`。
报告里逐字写明这是**采集量门槛，不是统计显著性判断**。
Smoke 3 验证的就是这条（4 条观察 → 1 条 `insufficient_sample`，
比较类信号 0 条，Writer 看到的唯一一句反馈就是"不多于 4 条，低于下限"）。

**8. correlation vs causation**
v1 的每一句话都是观察性模板生成的，返回前过 `assert_no_causal_claim`
（封闭的中英双语标记表）。`comparison_group` 恒为 `None`：没有对照组、
没有实验设计、没有基线，所以**构造上就不可能**得出因果结论。
`limitations` 里逐字写着"同时出现不等于谁导致了谁"。

**9. FeedbackSignal**
封闭枚举：`attention` / `conversation` / `amplification` / `share` /
`insufficient_sample` / `missing_metric`。没有 `quality` / `score` /
`recommendation`。每条带 `sample_count`、`window`、`evidence`（非空快照 id 元组）、
`comparison_group`（恒 `None`）、`confidence`（恒 `None`）。
**没有依据就不产出信号。**

**10. FeedbackReport**
`FeedbackReport(sample_count, observations, signals, limitations)`。
读时现算（CLI 与 Admin API 各算一次），**不落库**——它是一次读的产物，不是资产。
`limitations` 把没算的东西逐条列出。

**11. Agent 如何消费**
只进 **Writer**（`generate_candidates`），作为 checkpoint-only 的
RunState 键 `feedback_context`。没有新节点、没有新 AgentAction、预算未变。
入口是 `prompts/agent/candidates.txt` 的 `{{FEEDBACK}}` 块，内容只有样本量、
几类互动的分布、以及局限——**没有任何帖子正文，也没有单帖计数**。
Critic 的 prompt 里没有反馈。

**12. Style / Trend / Viewpoint / Feedback 如何保持边界**
Trend 是外部世界、Style 是外部语料、Viewpoint 是当前证据下的判断、
Feedback 是自己过去行为的结果。代码里 `app/feedback/` 是独立包（不在
`app/agent/` 里），pure、无 IO；反馈在 prompt 里是独立块，不是 StyleRecipe 的字段；
没有任何路径用反馈写 `trend_score`；反馈不合并进 StyleRecipe；不增加预算。
详见 N 节。

**13. 是否新增 DB 表**
**新增一张**：`post_metrics_snapshot`。**没有**在任何既有表上加列。
没有 `feedback_signal` / `feedback_report` 表——它们是读取时的确定性派生视图。

**14. Migration**
手写 `alembic/versions/e5f1a9c4d20b_phase_5b4_feedback_loop_foundation.py`，
`revises c3d8a45e7f21`，**刻意不用 `--autogenerate`**（LangGraph 的 4 张
checkpoint 表不在 `Base.metadata` 里）。docstring 逐条论证这张表为什么属于
数据库而不属于 checkpoint，以及什么**没有**建。

**15. Evaluation**
`evals/feedback/{__init__,cases,selftest}.py`，`python -m evals.feedback.selftest`，
离线可跑（无 X、无 LLM、无 DB、无时钟）。6 类 case × 反向扰动 + 12 项检查，
覆盖 §30 点名的六项：`snapshot_time_semantics`、`null_metrics_not_zero`、
`derived_metric_requirements`、`small_sample_guard`、`causal_language_guard`、
`feedback_traceability`（另加确定性、上下文只带摘要、每个 case 都配反向扰动）。

**16. Offline benchmark**
**恰好一条新 gate：RULE-024 `feedback_context_is_observational`**。
没有 `feedback_context` → pass（不适用）；有 → 不允许因果标记、样本量必须出现、
低于下限不得给出比较结论、不允许出现帖子正文或把单帖点赞写成指令。
六个同步点全部登记（规则函数 / `RULES` 元组 / `GATE_RULE_IDS` /
fail 构造 + `CHECKS` / `G-022` + fixture / `evals/model.py` 的
`feedback_context` 字段）。fixture 两个：`feedback_observed`（pass）与
`feedback_causal_language`（fail）。基准 `22/22` 通过，含 `G-022`。

**17. 历史真实帖子验证**
**没有做，因为没有对象。** `published_post` 0 行——从来没有发布过任何内容。
如实记成 `blocked`（K 节第 1 条），不拿合成的帖子冒充。

**18. 5 次 Smoke**
Smoke 3 与 5 跑了（合成数据），Smoke 1/2/4 **跑不了**并写明原因。
运行记录见 `docs/phase-5b4-smoke-runs.json`。详见 I 节。

**19. X GET**
新增 `XApiClient.get_posts_by_ids(ids)` → `GET /2/tweets?ids=…&tweet.fields=public_metrics`，
走既有的 `_request`，错误分类（`XApiConfigError` / `XApiResponseError` /
`XApiTransportError`）原样复用。`MAX_LOOKUP_IDS = 100`，调用方分批
（`x_service.fetch_public_metrics`）。真机一次都没有发出——没有可查的帖子；
只在 mock transport 上验证过。

**20. X POST**
**0。** 本阶段没有任何 POST 路径：采集是 GET，`scripts/smoke_5b4.py` 带只读守卫，
非 GET 在发出去之前就抛。工作单第三十三节的红线。

**21. 测试结果**
完整性见 P 节。backend `pytest -q` 644 通过；`evals.selftest` 15；
`run_benchmark` 22（含 `G-022`）；`evals.style.selftest` 22；
`evals.viewpoint.selftest` 17；`evals.feedback.selftest` 12；
前端 `node --test` 75；`tsc --noEmit` clean；`npm run build` 成功。

**22. 已知限制**
见 K 节，7 条。最要紧的两条：**没有真实已发布帖子**（所以数据链只在合成
fixture 上走过）、**没有实验设计**（所以因果在构造上不可能得出）。

**23. 明确没有做什么**
见 L 节：不自动改 Prompt / StyleRecipe / Budget / 策略，不自动发布，
不做 fine-tuning、embedding、vector DB、multi-agent、A/B 基础设施、
Celery/Redis/Kafka/cron、分布式调度。采集是人工 CLI，API 只读。

---

## P. 验证命令与结果（全部串行）

```bash
PY=/c/Users/Too_Far/miniconda3/envs/2026-agent/python.exe

$PY -c "import app, evals; print(app.__file__, evals.__file__)"   # 必须在 worktree 内
$PY -m alembic upgrade head
$PY -m pytest -q
$PY -m evals.selftest
$PY -m evals.run_benchmark --dataset goals.json
$PY -m evals.style.selftest
$PY -m evals.viewpoint.selftest
$PY -m evals.feedback.selftest
$PY scripts/refresh_post_metrics.py --dry-run
$PY scripts/smoke_5b4.py

cd frontend && node --test "tests/**/*.test.ts"
cd frontend && npx tsc --noEmit
cd frontend && npm run build
```

**只增不减**（5B.3 文档记的基线 → 本阶段；本阶段没有删除或放松任何一条既有测试）：

| 项 | 5B.3 基线 | 5B.4 | 结果 |
|---|---|---|---|
| `pytest -q` | 555 | 644 | ✅ 全通过 |
| `evals.selftest` | 14 | 15 | ✅ 全通过 |
| `evals.style.selftest` | 22 | 22 | ✅ 无回归 |
| `run_benchmark` | 21 | 22 | ✅ 全通过（含 `G-022` / RULE-024） |
| `evals.viewpoint.selftest` | 17 | 17 | ✅ 无回归 |
| `evals.feedback.selftest` | — | 12 | ✅ 新增 |
| 前端 `node --test` | 61 | 75 | ✅ 全通过 |
| `tsc --noEmit` | clean | clean | ✅ |
| `npm run build` | ✅ | ✅ | ✅ |

**注意**：在仓库根跑 `evals` 必须用 `python -m evals.…`（本机 editable 安装的
`.pth` 指向 master 检出，按路径直跑会 import 到 master 的 `evals` 包，见
`docs/phase-5b1-content-intelligence.md:333-336`）；`run_benchmark` 的
`--dataset` 取的是**数据集名字**（`goals.json`），给路径会被 `DATASET_DIR`
二次拼接后报 `FileNotFoundError`。worktree 里 `node_modules` 需要
`npm ci --prefer-offline` 先装（本机 npm cache 目录不可写，`npm install` 会
静默什么都不装，退出码却是 0）。

---

## Q. 结论

Feedback Loop 决定的是**"我过去做的事后来怎么样"**，而它在本阶段**只能描述、
不能指挥**。这条分界在代码里是硬的：

- **未知就是未知**：NULL 全程没有被补成 0——从 X 响应映射、到快照列、
  到 `metricLabel`，每一层都有测试钉住 `None` / `0` / 数字是三件不同的事；
- **样本不够就闭嘴**：低于下限只报 `n`，且逐字声明这是采集量门槛，
  不是统计显著性；
- **观察不是结论**：因果措辞在生成侧被模板排除、在返回前被守卫扫一遍、
  在 eval 侧被 RULE-024 判失败；
- **没有依据就没有信号**：每条信号的 evidence 都指向真实存在的快照行；
- **反馈是背景不是指令**：Writer 拿到的是"你过去发的东西被观察到这样"，
  而不是"你下次该这么写"，也没有任何正文或单帖计数；
- **它改不了自己**：没有写路径从反馈回到 prompt / StyleRecipe / 预算 / 阈值，
  当前运行的 trend 分不受影响。

系统的终点因此从"基于当前证据形成判断"变成
**"并且在若干次运行之后，能说出自己过去做过的事实际发生了什么——只说不做"**。
**这是本阶段的成功判据。**

**Phase 5B.4 — Feedback Loop Foundation 完成，立即停止（§45）。**

---

## R. 收口记录

本节只记录"把已经写好的 5B.4 落进历史"这件事，**不新增任何业务能力**。

**分支与 worktree（工作单第四十三节）**：

```
worktree : C:\xuexi\py\2026_agent\X_Agent-phase-5b4
branch   : feat/phase-5b4-feedback-loop
基线     : a6f67b7（Phase 5B.3 的 finalize 提交）
特性提交 : 11f5492  feat: add feedback loop foundation (Phase 5B.4)
master   : a6f67b7  未动
```

**44 条改动**（25 改 + 19 增），全部属于本阶段；没有一条是别的会话留下的。

**收口时的实际状态**（不是按计划假设的状态）：5B.4 的代码此前以工作区改动的形式
躺在 worktree 上，**没有提交过**。收口做的是三件事：把 44 条改动一次性提交、
补齐本文档（含逐条回答 §44 的 23 问）、把 smoke 的真实运行记录
（`docs/phase-5b4-smoke-runs.json`）纳入版本。

**import 落点自查**（必须做，否则"全绿"可能是假的）：本机 editable 安装的 `.pth`
指向 master 检出，在 worktree 里 `import app` 可能静默拿到 master 的代码。
开工与收口各验一次：

```bash
$ python -c "import app, evals; print(app.__file__, evals.__file__)"
C:\xuexi\py\2026_agent\X_Agent-phase-5b4\app\__init__.py
C:\xuexi\py\2026_agent\X_Agent-phase-5b4\evals\__init__.py
```

两次都在 worktree 内，测试跑的是本阶段的代码。

**与 5B.3 的基线对照**（P 节那张表的来源）：`pytest` 555 → 644（+89），
`evals.selftest` 14 → 15（+1，RULE-024 的 fail 构造）、
`run_benchmark` 21 → 22（+1，`G-022`）、`evals.feedback.selftest` 0 → 12、
前端 61 → 75（+14）。`evals.style.selftest`（22）与
`evals.viewpoint.selftest`（17）**一条没动、一条没少**——这是"没有回归"的证据，
不是"没跑"。

**Foundation 收口时明确没有做的动作**：没有合并回 `master`、没有 push、
没有删除 worktree、没有打 tag。工作单第四十三节只要求"在独立分支与 worktree 上做、
`master` 不动"，这三点都满足。

> 后来由 **Final Integration & Baseline**（T 节）改变了其中一点：应当合并且已用
> `--ff-only` 快进并入 `master`。R 节保留的是**当时**的事实，不覆写——
> "开发期间 `master` 不动"与"收口时合并"是先后两件事。**至今仍然成立**的：
> 没有 push、没有删除 worktree、没有打 tag。

---

## S. Real-world Validation（2026-10-02）

Foundation 交付之后，整条链在真实数据上**没有跑过**——`published_post` 是 0 行。
本节记录把这块缺口补上的那一次：一次真实、人工确认的 X 发布。
完整证据在 `docs/phase-5b4-real-validation.json`。

### S1. 做了什么

```
Goal → Agent → waiting_for_review          （run 426edc13…，Agent 自选话题）
  → 人工审核：edit（原稿 144 字符超过 persona.max_length=140，approve 会被 422 拦下）
  → PublishService → POST /2/tweets        （1 次）
  → published_post + agent_run = published
  → GET /2/tweets?ids=…  ×2                （两次快照，observed_at 不同）
  → GET /api/admin/feedback                （真实数据上生成报告）
  → 下一次 Agent Run                        （读到了结构化的 Feedback Context）
```

**真实 X 请求账：`POST /2/tweets = 1`、`POST /2/media = 0`、**
`GET /2/tweets?ids= = 3`、`GET /2/tweets/search/recent = 5`、`GET /2/users/me = 1`。
没有重试、没有循环、没有自动发布。帖子保持公开，没有删除。

### S2. 结果

| 判据 | 结果 |
|---|---|
| 真实帖子发布成功 | ✅ `x_post_id=2105998097302028593` |
| 数据库有 `published_post` | ✅ 1 行，`status=published` |
| X GET 读到 metrics | ✅ 200，四个公开计数真实返回 |
| 至少 2 条 snapshot | ✅ 2 条，同 `x_post_id` 不同 `observed_at` 共存 |
| `FeedbackReport` 生成 | ✅ `available=true` |
| n 被正确标成 `insufficient_sample` | ✅ 唯一信号就是它，无比较、无建议 |
| 下一次 Agent 读到 Feedback Context | ✅ 真实快照 id 已进 checkpoint |
| 没有自动调参 / 自动发布 | ✅ 见 S4 |

### S3. 真机验证发现并修掉的缺陷（D1）

`app/services/x_service.py::fetch_public_metrics` 原本写的是
`impressions=metrics.get("impression_count")`，注释断言"v1 从未请求这两项，
恒为 None"。**真机证明这个假设是错的**：`GET /2/tweets?ids=&tweet.fields=public_metrics`
照样返回 `impression_count: 0` / `bookmark_count: 0`，于是快照里落下两个 0。

后果不是"多读了两个数"：`report.py` 只有在**全部**观察的 impressions 都是 None 时
才写"曝光量为未知，因此互动率无法计算（不是零互动率）"。落成 0 会让这句最该说的话
消失，报告转而暗示"测出来是 0 曝光"——正是工作单第十节禁的东西。

修法是 v1 不读这两项；补了 3 条测试，其中
`test_impressions_and_bookmarks_are_ignored_even_when_x_returns_them` 补上的正是
原来漏掉的情形（原测试只覆盖"键不存在"，真机是"键存在且为 0"）。
另外加了一条
`test_the_real_chain_from_published_post_to_feedback_signal`
（`published_post → snapshot → FeedbackSignal`，不依赖 X，CI 可跑）——
缺陷正是从"每一段都有单测、但没人把它们串起来"这个缝里漏过去的。

### S4. 四层仍然独立（真机上再验一次）

- `{{FEEDBACK}}` 只出现在 `prompts/agent/candidates.txt`（Writer）一个模板里，
  angles / critic / revise 都没有；
- `feedback_context` 在 `app/` 里的出现点只有：state 的键声明、prompt 渲染、
  `candidates.py` 的解析与传入、checkpointer 的 serde 登记、admin 的只读视图——
  **没有任何写 `trend_score` / `style_recipe` / `StyleProfile` / 观点分 / 预算 /
  Critic 阈值的路径**；
- Writer 拿到的 379 字符里没有帖子正文、没有单帖计数、没有指令；
  指令性标记命中 0。唯一出现的"因果"字样是局限句里那句用来**否认**因果的
  "同时出现不等于谁导致了谁"；
- 没有新节点、没有新 AgentAction、预算未动。

### S5. 这次验证**不能**说明什么

n=2，而且两次观察都落在 0-1h 档、计数全是 0。它证明的是
**真实发布结果能够安全进入系统、并被 Agent 看见**，不是任何内容规律。
因此本轮**没有**因为指标低而动过任何算法：`MIN_SAMPLES_FOR_COMPARISON` 没变，
`persona.max_length` 没变，Writer 没动——尽管本轮两次运行的原稿都触发了 guardrail。
按工作单第二十节，这是一次样本，只配证明数据链。

### S6. 这次的验证后测试

`pytest` **647 通过**（644 → 647，+3 条新测试）；`evals.selftest` 15；
`evals.style.selftest` 22；`evals.viewpoint.selftest` 17；
`evals.feedback.selftest` 12；`run_benchmark` 22/22（含 `G-022`）。
前端本轮未改，未重跑。

---

## T. Final Integration & Baseline（2026-10-02）

本节只做收口：Git 合并、最终验证、文档封版、final bundle。**不新增任何业务能力**。

### T1. 合并

```
merge 前 master        : a6f67b7  docs: finalize phase 5b.3
merge 前 5B.4 branch   : 2a73341  feat: real-world validation of the feedback loop
merge-base 检查        : git merge-base --is-ancestor master <branch> → 通过
                          （merge-base = a6f67b7，即 master 自身；分支 3 ahead / 0 behind）
合并方式               : git switch master && git merge --ff-only feat/phase-5b4-feedback-loop
final HEAD             : 2a73341a0c0a7c7a521c79c100e54af9b1279b07
```

没有用 `rebase` / `reset` / `squash` / `amend` / `force push`。是纯快进，
所以三个提交（`11f5492` 特性 → `6a1974f` finalize → `2a73341` 真机验证）原样进历史。

合并前做过一次全量备份：`X_Agent-2026-10-02-5b4-premerge.bundle`（`git bundle verify`
通过，10 个 ref，含 `refs/heads/master` 与 `refs/heads/feat/phase-5b4-feedback-loop`），
放在仓库之外，不入版本。

### T2. 在最终 master 上重新验证（全部串行，重新跑，不引用分支上的历史数字）

开工前的环境前置：docker daemon 在跑（`xagent-postgres` healthy）、
`import app` 落在 `C:\xuexi\py\2026_agent\X_Agent\app\__init__.py`（**主检出**，
不是 worktree）、`alembic current` = `e5f1a9c4d20b (head)`。

| 项目 | 结果 |
|---|---|
| `pytest -q` | **647 通过**（171.35s） |
| `python -m evals.selftest` | **15 通过 / 0 失败** |
| `python -m evals.run_benchmark --dataset` | **22 通过 / 0 未通过（共 22 条）**，含 `G-022` |
| `python -m evals.style.selftest` | **22 通过 / 0 失败** |
| `python -m evals.viewpoint.selftest` | **17 通过 / 0 失败** |
| `python -m evals.feedback.selftest` | **12 通过 / 0 失败** |
| `node --test "tests/**/*.test.ts"` | **75 pass / 0 fail** |
| `npx tsc --noEmit` | exit 0，无输出 |
| `npm run build` | exit 0（870 modules，`dist/` 产物只有那条既有的 >500 kB chunk 提醒） |

与 5B.3 基线对照：`pytest` 555 → 647，`evals.selftest` 14 → 15，
`run_benchmark` 21 → 22，`evals.feedback.selftest` 0 → 12，前端 61 → 75。
`evals.style.selftest`（22）与 `evals.viewpoint.selftest`（17）**数值未变**——
这是"没有回归"的证据。

### T3. 真实数据在测试之后仍然完好

测试跑在**另一个库** `xagent_test` 上（`tests/support.py:21`
`TEST_URL = f"{_DB_BASE}/xagent_test"`），生产库是 `xagent`（同实例、端口 5433）。
跑完全部测试后再查生产库：

```
published_post         = 1   （x_post_id=2105998097302028593, status=published）
post_metrics_snapshot  = 2
agent_run              = 141
```

两条快照**逐字段未变**：

| # | observed_at | likes | replies | reposts | quotes | impressions | bookmarks |
|---|---|---|---|---|---|---|---|
| 1 | 2026-10-02T12:27:35Z | 0 | 0 | 0 | 0 | **0** | **0** |
| 2 | 2026-10-02T12:29:12Z | 0 | 0 | 0 | 0 | NULL | NULL |

**快照 1 被刻意保持原样**：那两个 0 是缺陷 D1 修复**之前**真实落库的审计事实。
快照表只追加、没有更新路径，所以不回填——历史价值恰恰在于它永远不会被改写。
修复之后写的快照 2 用的是新的 nullable 语义。于是同一张表里同时存在
"修复前的物证"与"修复后的正确行为"，这正是我们要的。

### T4. 没有再发第二条

本次合并**没有**触发任何真实发布：没有调用 `POST /2/tweets`，
没有 `POST /2/media`，没有重跑 `scripts/refresh_post_metrics.py` 去抓新 metrics。
本阶段真实 X 请求总量仍停在 S1 记的那一笔：`POST /2/tweets = 1`。

### T5. Metrics 语义（封版口径）

```
真实 API 没有这个字段        → NULL
真实 API 返回了、但当前策略决定不读 → NULL（impressions / bookmarks 属此类）
真实读取的公开计数为 0       → 就是 0（likes / replies / reposts / quotes）
```

`NULL ≠ 0`。任何一处 `or 0` 都会把"没观测到"变成"观测到零"，这是本阶段口径上
最严重的一类错误。代码上的落点是 `app/services/x_service.py::fetch_public_metrics`
（逐键 `.get()`、默认 `None`；`impressions` / `bookmarks` 直接写 `None`），
测试上的落点是 `tests/test_feedback_metrics.py` 与 `tests/test_feedback_repository.py`。

### T6. Feedback 的统计边界（封版口径）

`MIN_SAMPLES_FOR_COMPARISON = 5`（`app/feedback/signals.py:50`）。
当前真实数据是 `1 post / 2 snapshots`，`n=2 < 5` → 唯一信号
`insufficient_sample`，`comparison_group` 与 `confidence` 都是 `None`。
**没有**生成 style / topic / viewpoint 任何一类推荐，也**没有**产出
"这种风格表现最好"这类比较结论。

### T7. Agent 不会自动学习（封版再确认）

`feedback_context` 只能作为**只读观察**进入 Writer 的 prompt：

- `{{FEEDBACK}}` 只出现在 `prompts/agent/candidates.txt`（`grep -rl` 全 `prompts/` 只有这一个文件命中），angles / critic / revise 都没有；
- `app/feedback/` 里对 `trend_score` / `style_recipe` 的引用全部是**读**
  （`profile.py` 做归因时要记下"当时是怎么决定的"），没有一处写；
- 不写 Trend / Style / Viewpoint / Budget / Critic 阈值 / Prompt；
- 没有新节点、没有新 AgentAction、没有新的 `step_count` 消耗。

四层定义没有合并：

```
Trend     = 当前外部世界
Style     = 外部高质量语料
Viewpoint = 当前证据下的判断
Feedback  = 自己过去行为的真实结果
```

### T8. 真实 X 依赖不进 CI

`pytest` 与 `evals` 都不需要网络：涉及 X 的测试用
`httpx.MockTransport`（`tests/test_x_api_metrics.py` / `test_x_api_client.py` /
`test_api.py`）或 `tests/fakes.py::FakeXApiClient`，token 是字面量 `"test-token"`，
`tests/` 与 `evals/` 下没有任何 API key。真实发布与真实采集由
`docs/phase-5b4-real-validation.json` 单独记录，不进 CI。

### T9. working tree

`git status --porcelain -uall` 为空。`.env` / `logs` / `dist` / `node_modules` /
bundle / DB dump / scratch 均未被追踪。final bundle 放在仓库之外，不入版本。

### T10. 本次**不能**说明什么（封版时仍然成立）

- **n=2**：两次观察、1 条帖子。证明的是**数据链通了**，不是任何内容规律。
- **没有内容规律结论**：一条帖子 + 两个 0 计数，推导不出"什么内容更好"。
- **没有因果实验**：只有自然产生的观察数据，没有对照组、没有 A/B，所以因果在
  构造上就不可能得出。
- **真实历史帖子数量有限**：库里只有 1 条已发布内容，反馈层目前几乎是空的。
  要让它有统计意义，需要长期、多次真实发布——那是下一个阶段的事，不是本阶段的。

---

> 本阶段核心：先让 Agent 看见自己过去真实做过什么、结果如何，
> 而不是马上让它学会自动改变自己。
>
> ```
> Trend    = 当前外部世界
> Style    = 外部高质量语料
> Viewpoint = 当前证据下的判断
> Feedback = 自己过去行为的真实结果
> ```
>
> 这四层必须保持独立。
