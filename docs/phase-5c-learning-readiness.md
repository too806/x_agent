# Phase 5C — Learning Readiness

**范围**：在 Trend / Style / Viewpoint / Feedback 之后新增**第五层**，回答且只回答一句：

```text
我过去发出去的东西、后来被观察到的结果，够不够可靠，可以拿去学习？
```

**基线**：开工时 `master` HEAD `e854b03`（Phase 5B.4 COMPLETE，backend 694 通过 /
frontend 93 通过）。

**封版 HEAD**：**本文件所在的 `master` 提交**（`git log --oneline -1` 即得）。
此处刻意不写字面哈希——文档每改一次哈希就变，写死必然过期。

**状态**：**Complete**。五条 smoke 全部跑通（`docs/phase-5c-smoke-runs.json`），
真机验证与离线验证分别给出预期结果（见 H、I 节）。

**结论**：完成，而且**只是"判断能不能学"**。这一层不写正文、不改 prompt、不调权重、
不自动发布。它唯一的功能是把"反馈够不够可靠"从一个印象变成一次**代码判定**：

```text
Trend              = 当前外部世界在发生什么
Style              = 外部语料是怎么写的
Viewpoint          = 在现有证据下一个判断站不站得住
Feedback           = 我们自己过去的行为，实际带来了什么可观察的结果
Learning Readiness = Feedback 是否已经足够可靠，可以进入未来学习     ← 本阶段新增，且只到这里
```

---

## A. 交付概览

**新增**

```text
app/learning/__init__.py                       包说明与边界
app/learning/schema.py                         PostPerformanceRecord / PerformanceVector /
                                               CohortDefinition / ObservedComparison /
                                               LearningEvidence / ReadinessCheck /
                                               LearningReadiness / LearningReport
app/learning/performance.py                    窗口取值、分布、离群标记（纯函数）
app/learning/cohort.py                         分组与同窗口同维度比较（纯函数）
app/learning/evidence.py                       LearningEvidence + 确定性 learning gate
app/learning/report.py                         组装与出口校验（可追溯 / 因果 / 指令 / confidence）
app/learning/runtime.py                        只读装配（唯一碰 DB 与 checkpoint 的模块）
evals/learning/{__init__,fixtures,selftest}.py 九个离线 case + 16 项自检
scripts/smoke_5c.py                            工作单第三十八节的五个 smoke（GET-only）
tests/test_learning_core.py                    纯层单测（38 条）
tests/test_learning_api.py                     只读 API 集成测试（9 条）
frontend/src/utils/learning.ts                 措辞纯函数
frontend/tests/learning.test.ts                前端措辞测试（18 条）
docs/phase-5c-smoke-runs.json                  smoke 记录
docs/phase-5c-learning-readiness.md            本文件
```

**改动（全部是追加式，不改既有语义）**

```text
app/feedback/schema.py       PostPerformanceProfile 追加 viewpoint_stance /
                             counterpoint_awareness / scope_exceeds_evidence 三个字段
app/feedback/profile.py      把 Angle 的 stance / counterpoints / scope 投影进上面三个字段
app/schemas.py               LearningCohortRow / LearningComparisonRow / LearningEvidenceRow /
                             LearningReadinessRow / LearningOverview（对外模型）
app/services/admin.py        AdminService.learning()（只读）
app/api/admin.py             GET /api/admin/learning（唯一新增路由）
app/agent/prompts.py         format_learning_context()（纯函数；**不接入 Graph**）
prompts/agent/candidates.txt 反馈块补一句"反馈是观察性证据，不是事实 / 指令 / 保证"
frontend/src/types/api.ts    学习层类型
frontend/src/api/client.ts   getLearning()
frontend/src/pages/Analytics.tsx          LearningBlock（最小展示）
frontend/src/components/RunDetailDrawer.tsx 一行紧凑 readiness
```

**没有的**（这是本阶段最重要的一张清单）：新数据库表、新列、迁移、调度器、Celery、
Redis、Kafka、embedding、向量库、多智能体、A/B 框架、微调、`apply_learning()`。

---

## B. 只读审计结论（写代码之前先指认问题）

工作单第三节要求先审。七问逐一回答：

1. **一条已发布帖子能关联到哪些生成时信息？**
   `published_post`（`body` / `x_post_id` / `published_at`）+ `post_metrics_snapshot`
   （多次观察）+ `agent_run.topic` + checkpoint（`selected_topic` + `candidate_topics`
   → 当时的 trend 分、`selected_angle`、`style_recipe`、`critic_result`）。
   这条归因链在 5B.4 已经实现于 `app/feedback/profile.py::build_post_performance_profile`，
   本阶段**复用而不是重写**。

2. **Trend / Style / Viewpoint / Critic 能否从历史 Run 重建？**
   能读回"当时记下来的值"，**不能重算**。重算需要一个"现在"（trend 分要算 recency、
   观点评估要当下语境），得到的会是另一个数。所以本层只搬运 checkpoint 里的历史值；
   读不到就是 `None` 并在 `limitations` 里说明。

3. **哪些字段在 publish 时已经成为不可变事实？**
   `published_post.body` / `x_post_id` / `published_at`，以及每一行快照
   （只追加、没有 update 路径）。这些是持久事实，直接读。

4. **哪些字段以后可能被重新计算？**
   trend 分、critic 分、观点分——它们**刻意不落库**。本阶段沿用这个边界：
   **不建** `style_recipe_history` / `viewpoint_history` / `critic_history`，
   也不新增任何列或表。一旦复制成列，就出现第二个真相来源，然后某天悄悄漂掉。

5. **metrics snapshot 如何形成时间序列？**
   靠写库那一刻算好的 `hours_since_publish`（唯一基础时间轴）+ 纯函数分档
   `app/feedback/metrics.py::window_bucket`（`0-1h | 1-6h | 6-24h | 24-72h | >72h`）。
   分档是纯函数，所以**不需要调度器**按时去"采样第 1 小时"。

6. **当前哪些内容可以构成 cohort？**
   `structure_type` / `hook_type`（StyleRecipe）、`stance` / 反方意识 / scope
   （Viewpoint Angle）、`length_bucket`（实际正文长度）。都是确定性取值。
   真机上 n=1，**任何 cohort 都不成立**。

7. **如何定义"比较组"而不制造伪因果？**
   只在**同一观察窗口、同一维度**的两个取值之间比较；每个组必须带
   `sample_count` / `time_window` / `selection_rule`；两组 topic 组成不同时写进
   `limitations`（本仓库没有确定性选题分类器，**无法**把选题控制掉）；结论只用
   观察性措辞并过 `assert_no_causal_claim`。`topic class` 维度**明确不做**。

---

## C. PostPerformanceRecord（§4）

`PostPerformanceRecord` 是**只读派生视图**，三块并排、不合并、不加权、不排序：

```text
决策（checkpoint 的当时值）  ‖  交付（published_post.body）  ‖  结果（快照表，按窗口取值）
```

- **不复制整份 Run**（工作单第四节），也不落库；
- 每个 checkpoint 读不到的字段是 `None`，**不用默认值顶替**；
- 三块之间没有一行代码说"因为选了 X，所以点赞多"。

`app/learning/runtime.py::record_from_profile` 是唯一的转换点；它调用 5B.4 的
`build_post_performance_profile`，不重写归因。

## D. 分析单位是帖子，不是快照（本阶段最容易写错的一条）

同一条帖子的 1h / 6h / 24h 三条快照是**同一条帖子的重复观察**，不是三个样本。
把三条当三份证据，等于把同一句话数了三遍，是小样本上造"显著差异"最快的一条路。

因此：每个帖子在每个窗口只贡献**一个**值（该窗口内**最新**那条观察）；窗口内无观察
→ `None`，**不补 0**；`sample_count` 一律指**帖子数**。

真机数据 `1 post / 2 snapshots` 因此正确地算作 **n=1**，够不上比较下限。
`tests/test_learning_api.py` 通过 HTTP 边界把这条钉住。

## E. Observation windows（§7）

`hours_since_publish` 是**唯一**的基础时间轴，写入时算好，事后无法从快照行自身重算。
分档沿用 5B.4 的纯函数：`0-1h | 1-6h | 6-24h | 24-72h | >72h`，负小时数与 `None`
都返回 `None`（数据不自洽时不硬塞进 `0-1h`）。

不指定窗口时取**读数最多**的那一档（`dominant_window`，并列时按固定标签顺序
tie-break）。真机数据只有 `0-1h` 的观察，所以默认就是 `0-1h`；若默认成别的档，
报告会因为"没人有那个窗口的读数"而只说一句没有数据——那不是数据的问题，是默认值的问题。

## F. PerformanceVector（§8）与为什么没有总分（§9）

映射**沿用 5B.4 的定义**，全库只有一个定义：

```text
attention     = likes
conversation  = replies + quotes
amplification = reposts
```

三个分量独立、不合成。这里**没有** `score` / `engagement` / `quality` 这类成员，
而且不该有：一个综合分正是工作单第九节禁止的"用一个数代表好坏"。两个分量各可空，
`None` = 该窗口内没读到，**不是 0**。

## G. CohortDefinition（§10/§11）与比较（§12/§13）

`CohortDefinition` 必带三样，不是可选备注：

```text
sample_count    这一组有多少条帖子
time_window     看的是发布后多久
selection_rule  按什么分组、窗口固定在哪、分析单位是什么
```

`MIN_SAMPLES_FOR_COMPARISON = 5` **是采集量的门槛，不是统计显著性**——这句话写进了
代码注释、报告陈述与前端文案，三处都在说同一件事。

`compare_cohorts` 对**每一对**都产出对象，但只有成立的那些带结论；不成立时
`valid=False` + 具体原因（两组重叠 / 取值相同 / 窗口不同 / 样本不足 / 读数不足）。
一个被静默丢弃的比较，读的人无从知道"这里本来想比什么、为什么没比"。

成立的比较，措辞是固定的：

```text
在 24-72h 观察窗口下，按开头方式分组，question（n=5）的点赞（注意力）中位数为 …
；在当前样本中观察到「statement」更高。这是观察到的差异，不构成任何因果结论。
```

**绝不**是 `question hook 更好`。中位数相等时方向句换成"两组中位数相同，没有观察到
方向上的差异"——说某一方"更高"是在编一个不存在的差异。

## H. median / distribution（§14）与 outlier（§15）

主口径是 `median` / `p25` / `p75`；`mean` 只作**辅助**保留（让"为什么不用均值"
在报告里可核对：一个异常高值会把均值带走，中位数不会）。

离群点用 IQR 栅栏（`[p25 − 1.5·IQR, p75 + 1.5·IQR]`）**只标记、不删除**——
真实爆款本身就是数据。`distribution` 永远在**全部**读数上计算，没有任何一条代码路径
会把标记为离群的点剔除。读数少于 4 条时不判离群（栅栏几乎完全由端点决定），
但**不判不等于删**。

## I. missing metrics（§17）

`NULL ≠ 0` 是硬性的：一个缺失的分量被补成 0，会把它压到分布下沿，
然后变成一条"这一类表现更差"的假结论。所以：

- 求和是"全有才求和"（`conversation = replies + quotes`，任一未知即 `None`）；
- 缺读数的帖子不进该指标的分布，**不补 0**；
- 报告里出现的是"读数不足"，不是"读数是 0"。

`app/learning/schema.py` 里**没有一处默认值写成 0**；前端 `medianLabel(null)` 返回
"暂无数据"而不是 `'0'`，并有测试钉住。

## J. LearningEvidence（§19）与可追溯（§18）

每条证据带着 `id` / `type` / `statement` / `sample_count` / `comparison_group` /
`observation_window` / `metric` / `dimension` / `source_snapshot_ids` /
`source_post_ids` / `source_x_post_ids` / `confidence` / `limitations`。

- `id` 由比较键**确定性派生**（同一次比较每次同一个 id）；
- `confidence` **恒为 `None`**：没有经过校准的概率模型，编一个数字就是捏造；
- 可追溯是硬要求，`report.py` 在出口处核验每条证据引用的 snapshot / post id 真的
  出现在报告里，对不上就抛 `LearningReportError`。

**证据永远是观察，不是规则**（§20）：`LearningEvidenceKind` 里正向的只有
`cohort_difference`；其余四项（`insufficient_sample` / `missing_metric` /
`window_mismatch` / `invalid_comparison`）都是"什么也没法说"的诚实出口。
没有 `RECOMMENDATION` / `RULE` / `ACTION`。

## K. LearningGate（§24）——确定性，不由 LLM 决定

四条判据**同时**成立才放行，**顺序即优先级**：

```text
sample_count       窗口内有多少条帖子真有读数        >= 5
window_consistent  所有记录是不是同一个观察窗口
metrics_present    至少一类计数在该窗口有 >= 5 条读数
comparison_valid   有没有至少一组 cohort 比较成立
```

理由取第一条不成立的：`insufficient_sample` → `inconsistent_window` →
`missing_metrics` → `invalid_comparison` → `ready`。

`learning_ready = True` **只**表示"可以作为未来学习材料"（§25），不表示"立即改变
Agent"。所以：

- `LearningReadiness.automatic_modification` **恒为 `False`**，而且是**字段**而不是
  调用方的约定——一个恒假的字段会被测试钉住，而一句约定不会；
- 整个 `app/learning/` **没有** `apply_learning()`（§36）。`evals/learning/selftest.py`
  用一条 **AST 扫描**把它的缺失钉死，将来有人加回来，测试会先失败。

## L. Feedback Context（§21-23）

三条纪律：每一条进入 Agent 的 observation 必须带 `sample_count` /
`observation_window` / `comparison_group` / `limitations`；Feedback 不是 fact /
instruction / guarantee；Agent 读到的措辞是"过去观察到"而不是"以后必须"。

本阶段的落地范围（已确认的设计决定）：**只留接口，不接入 Graph**。
`app/agent/prompts.py::format_learning_context()` 是纯函数，有守卫、有测试，
但**没有任何调用点**——`prompts/agent/*.txt` 里没有 `{{LEARNING}}` 占位符
（测试断言），`app/agent/tools/candidates.py` 里也不出现 learning。

Writer prompt 只按 §23 补了一句：反馈是**观察性证据**，不是事实、不是指令、不是保证。
输入优先级不变：

```text
Evidence  >  Viewpoint  >  Style  >  Feedback observation
```

## M. 只读面：Admin API 与前端（§33）

`GET /api/admin/learning?window=<label>` 是**唯一**新增路由。

- 它在后端只有 GET：`tests/test_learning_api.py` 断言 POST / PUT / PATCH / DELETE
  四个写方法都返回 405；
- 它不触发任何采集；
- 它是控制台里**最重**的一次读（按条读 checkpoint），所以有读取上界
  `LEARNING_POST_READ_LIMIT = 200`，到达上界时如实写进 `limitations`；
- 它被刻意做成**独立端点**，而不是塞进 `/api/admin/analytics` 或
  `/api/admin/feedback`——那会把一次重读变成每次打开首页都要付的代价。

前端只做最小展示，**没有** Learning Dashboard、没有图表：

- `Analytics.tsx` 的 `LearningBlock`：状态 / n / 窗口 / 原因 / 闸门四条判据 /
  观察到的差异 / 证据与出处 / limitations / 口径说明；
- `RunDetailDrawer.tsx` 的一行紧凑 readiness（自己单独取一次接口，**不进**
  Run Detail 热路径，也不进任何聚合）。

措辞纪律全在 `utils/learning.ts` 这类纯函数里，且几乎全是否定式断言：
`null` 不变成 `0`；未成立的比较**一个数字都不给**（只说不成立的原因——并排摆出两个
中位数，读的人一定会自己去比，而那条比较之所以不成立恰恰是因为不该比）；
观察不写成评价（不出现"更好 / 更差 / 优秀 / 应该"）。

## N. X GET / X POST（§30）

本阶段对 X 的调用只有一条：`GET /2/tweets?ids=…&tweet.fields=public_metrics`。

```text
POST /2/tweets = 0
```

这不是一句承诺，是机器保证：`scripts/smoke_5c.py` 把 `RealXApiClient._request`
换成一层守卫，**非 GET 在发出去之前就抛**，报告末尾给出按方法分组的请求计数。
本次运行的总账是 `{GET: 1}`，`post_tweets = 0`。

**本次 GET 拿到的数字没有写进数据库**。它只是"此刻只读这条路通不通"的证据；
写进去就会多一条不是按采集节奏采的观察，让 `hours_since_publish` 的序列多出一个
没人打算要的点。真机快照数在 smoke 前后都是 2。

---

## O. 逐条回答工作单第三十九节的 21 个问题

**1. PostPerformanceRecord**
只读派生视图，三块并排（决策 ‖ 交付 ‖ 结果），不落库、不复制整份 Run、不加权不排序。
见 C 节；实现于 `app/learning/schema.py` + `runtime.py::record_from_profile`。

**2. PerformanceVector**
三个独立分量，沿用 5B.4 的映射：`attention=likes`、`conversation=replies+quotes`、
`amplification=reposts`。**没有综合分**（§9）。见 F 节。

**3. Observation windows**
`0-1h | 1-6h | 6-24h | 24-72h | >72h`，唯一时间轴是 `hours_since_publish`，
纯函数分档，无需调度器。默认窗口取读数最多的那一档。见 E 节。

**4. CohortDefinition**
同维度、同取值、同窗口的一批帖子；必带 `sample_count` / `time_window` /
`selection_rule`。取值 `unclassified` 不参与比较。见 G 节。

**5. small sample**
`MIN_SAMPLES_FOR_COMPARISON = 5`，**是采集量门槛，不是统计显著性**（三处文案都说）。
低于下限时**一个比较型结论都不产出**，只有一条带 n 的状态证据。见 G 节。

**6. comparison**
只在同窗口、同维度、互不重叠的两组之间；成立才给方向，措辞是"在当前样本中观察到
更高"，不是"更好"；中位数相等时不给方向。见 G 节。

**7. median / distribution**
主口径 median / p25 / p75，mean 只作辅助保留以便核对。空序列是"全部 `None`"，
不是 `(0,0,0)`。见 H 节。

**8. outlier**
IQR 栅栏**只标记、不删除**；读数 < 4 时不判；`distribution` 永远在全部读数上计算。
见 H 节。

**9. missing metrics**
`NULL ≠ 0`：缺失不补 0、不进分布、不进求和；闸门给 `missing_metrics`，
证据类型是 `missing_metric`。见 I 节。

**10. LearningEvidence**
种类封闭、id 确定性推导、`confidence` 恒 `None`、按 `comparison → post_ids →
snapshot_ids → x_post_id` 可追溯，出口处再核验一遍。见 J 节。

**11. LearningGate**
确定性四条件（`sample_count` / `window_consistent` / `metrics_present` /
`comparison_valid`），**不由 LLM 决定**；`automatic_modification` 恒 `False`；
`apply_learning()` 不存在。见 K 节。

**12. Feedback Context**
接口已备（`format_learning_context()` 纯函数 + 守卫 + 测试），**本阶段不接入 Graph**；
每条 observation 必带 sample_count / observation_window / comparison_group /
limitations；Writer prompt 按 §23 补了一句"观察性证据，不是事实 / 指令 / 保证"。
见 L 节。

**13. Agent 如何消费**
本阶段 Agent **不消费**学习层的任何东西。`format_learning_context()` 没有调用点，
`{{LEARNING}}` 占位符不存在。唯一进入 Writer 的还是 5B.4 的 Feedback Context，
优先级 `Evidence > Viewpoint > Style > Feedback observation` 未变。见 L 节。

**14. Evaluation**
`evals/learning/selftest.py`：16 项检查全通过，其中包含 §28 要求的五条防伪
（`n=1→recommendation`、不同窗口→直接比较、`NULL→0`、correlation→causation、
小样本→`learning_ready`）都必须被**拒绝**，以及 AST 扫描断言无 `apply_learning`。
见 Q 节验证记录。

**15. Offline replay**
`evals/learning/fixtures.py` 九个 case（`one_post` / `small_sample` /
`balanced_cohorts` / `unequal_cohorts` / `missing_metrics` / `different_windows` /
`outlier` / `causal_language` / `invalid_comparison`），全合成、不碰 DB / X / LLM、
不取挂钟时间；检查确定性（同输入同输出）。见 Q 节。

**16. 真实数据验证**
`scripts/smoke_5c.py` Smoke 1 / 2 读真机库：链路连通，闸门给出 `insufficient_sample`
+ `learning_ready=false`。见 I 节（下方）与 `docs/phase-5c-smoke-runs.json`。

**17. X GET**
允许，而且是唯一允许的方向。本次运行发出 1 次 GET（`/2/tweets`），HTTP 200。见 N 节。

**18. X POST**
`POST /2/tweets = 0`。守卫在发出前就拒绝非 GET，报告里给出按方法分组的计数。
见 N 节。

**19. 测试**
backend `694 passed`；frontend `93 passed` + `tsc --noEmit` 退出码 0；各层自检
learning 16 / feedback 12 / style 22 / viewpoint 17 / selftest 15 全通过。见 Q 节。

**20. 当前真实数据限制**
1 条已发布帖子、2 次观察（发布后 0.0019h / 0.0288h，都在 `0-1h`），四项计数全 0。
n=1，任何 cohort 都不成立，因此**没有任何比较结论**。这不是本阶段的失败，
是它给出的第一条正确结论。见 P 节。

**21. 明确没有做什么**
Automatic Learning / Automatic Parameter Tuning / Automatic Prompt Optimization /
Strategy Modification / A/B Testing / Fine-tuning / LoRA / Embedding / Vector DB /
Multi-agent / Scheduler / Celery / Redis / Kafka / Autonomous Publish —— **全部未做**。
没有改 PublishService，没有改 Writer 的行为，没有新增表 / 列 / 迁移。见 R 节。

---

## P. 当前真实数据限制（§31/§37）

真机库当前状态（smoke 前后一致，本阶段没有写入任何一行）：

```text
published_post       1 行
post_metrics_snapshot 2 行
x_post_id            2105998097302028593
hours_since_publish  0.0019 / 0.0288   →  都在 0-1h
计数                  likes/replies/reposts/quotes 全为 0
```

因此：

```text
observation_window  = 0-1h（读数最多的那一档）
sample_count        = 1（帖子数，不是快照数）
reason              = insufficient_sample
learning_ready      = false
automatic_modification = false
valid_comparison_count = 0
```

报告里出现的是**恰好一条**状态证据，它说得出口依据：

```text
source_snapshot_ids = [cc1fb657-59fe-4861-b8d9-b8195c6e0d4b]
source_post_ids     = [46b5b3a1-7fb0-4c27-bcc7-bab83a893f76]
source_x_post_ids   = [2105998097302028593]
```

**这份数据不能说明任何关于"怎么写更有效"的事**：一条帖子既没有对照组，也没有
第二个样本点，四项计数还全是 0。工作单第三十一节要求的正是这个结果——
数据不足时输出"不足"，而不是拉一条看似中性的空报告。

---

## Q. 验证命令与结果（全部串行）

```text
python -m pytest -q                       694 passed in 136.49s
python -m evals.learning.selftest         Learning 自检：16 通过 / 0 失败（共 16 项）
python -m evals.feedback.selftest         Feedback 自检：12 通过 / 0 失败（共 12 项）
python -m evals.style.selftest            Style 自检：22 通过 / 0 失败（共 22 项）
python -m evals.viewpoint.selftest        Viewpoint 自检：17 通过 / 0 失败（共 17 项）
python -m evals.selftest                  自检：15 通过 / 0 失败（共 15 项）
python -m evals.run_benchmark --selftest  自检：15 通过 / 0 失败（共 15 项）
python scripts/smoke_5c.py                五条 smoke 全部通过；POST /2/tweets = 0
cd frontend && npx tsc --noEmit           退出码 0
cd frontend && node --test …              ℹ tests 93 / pass 93 / fail 0
```

说明：

- 跑 pytest 前 **Docker 必须在跑**，否则会出现几百个 `OperationalError` 的假回归；
- 测试**串行**跑：`xagent_test` 是共享库，`session_factory` 会 `TRUNCATE … CASCADE`，
  两个并发进程会互相清表，失败集会漂移，看起来像回归；
- 真实 smoke 不依赖 X 可达：只读 GET 失败时如实记 `blocked`，不让本地四条断言的结果
  一起丢掉。本次运行 GET 是通的。

## R. 明确没有做什么（§2/§40）

```text
Automatic Learning              未做
Automatic Parameter Tuning      未做
Automatic Prompt Optimization   未做
Strategy Modification           未做
A/B Testing                     未做
Fine-tuning / LoRA              未做
Embedding / Vector DB           未做
Multi-agent                     未做
Scheduler / Celery / Redis / Kafka  未做
Autonomous Publish              未做
```

具体到代码：

- **没有** `apply_learning()`（AST 扫描钉死）；
- **没有**任何依据反馈修改系统的代码路径：prompt、StyleRecipe、Viewpoint 规则、
  Trend 权重、Critic threshold、预算、Planner 一处未动；
- **没有**改 `PublishService`、`ReviewService`、`Writer` 的行为（Writer 只多了一句
  说明性文字，见 L 节）；
- **没有**新增表、列或迁移，`tests/conftest.py` 未动；
- **没有**第二个审核入口、第二个发布入口；
- **没有** Learning Dashboard 或图表；
- **没有**污染真实数据：不新增 / 不修改 / 不复制 snapshot，不伪造已发布帖子，
  不改 `published_at`。所有测试用 `xagent_test`，真机库只读。

**这一层停在"判断能不能学"。** 下一步（真的拿反馈去改点什么）需要单独一个阶段，
需要实验设计而不是本阶段这套观察口径，而且必须重新讨论——不能靠本阶段的这段代码
顺手接过去。
