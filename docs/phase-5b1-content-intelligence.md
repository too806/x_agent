# Phase 5B.1 — Content Intelligence Foundation（Trend Scout + Evidence Intelligence）

> 生成于 2026-09-29；终验收修复与最终批次于 2026-09-30 补入。
> 基线：`master` 的 `56e327b`（Phase 5A 收口，266 tests 绿）。
> 本阶段：功能与修复 commit + 1 个文档 commit；测试基线 **341 后端 + 46 前端**
> （原 300 项全部通过，本阶段新增 41 项）。
> 分支：`feat/phase-5b1-content-intelligence`（工作树 `../X_Agent-phase-5b1`，**未并回 master**）。
> 第 10 节与第 13 节记录的是**同一批**五次真机 smoke（`generated_at` 见下），
> 批次之前的 4/5 结果不再作为验收依据。

北极星（工作单第三十四节）：

> 让现有的 One Agent 真正开始拥有"发现问题 → 判断价值 → 研究事实"的能力。

而不是把"用户给主题 → Agent 搜主题"换个说法。改的是**上游**：

```text
之前：用户给主题  →  Agent 搜主题
现在：用户给目标  →  Agent 发现什么值得讨论、判断为什么值得、研究事实、比较来源、
                     抽出矛盾与变化、再形成观点
```

本阶段只做到"话题质量 + 证据质量"这两项（工作单第十五节：语言风格、结构质量属于后续阶段），
并顺带为观点生成准备好输入。

---

## 1. 为什么需要 Trend Intelligence

Phase 5A 的 Agent 已经能自主决定"下一步做什么"，但它自主的第一步是
`discover_topics`——而那个动作当时只是**让 LLM 从检索回来的材料里列几个候选**。
三个问题：

1. **候选里没有"为什么值得写"的依据。** 模型列出来的东西没有任何可核对的数据支撑，
   选题质量完全取决于它当时的措辞。
2. **没有"现在"的概念。** 一条三天前的帖子和一小时前的帖子在候选里长得一样，
   而"为什么是现在"恰恰是这个产品唯一的差异化（内容团队不缺话题，缺的是时机判断）。
3. **单条爆款就能冒充一个趋势。** 工作单第十一条点名禁止这件事。

所以本阶段引入的不是"更多数据源"，而是**把已有的只读检索变成一个有定义、可复现、
可解释的判断过程**：划定窗口 → 多作者聚类 → Python 打分 → 分档判定。

---

## 2. Topic 数据模型

候选话题是 `CandidateTopic`（`app/agent/schemas.py`），字段刻意分成三类，
**混在一起就看不清哪些能信**：

| 类别 | 字段 | 由谁产生 | 能不能信 |
|---|---|---|---|
| 模型给的 | `title` `description` `keywords` `why_now` `unknowns` | LLM | 文本，不是判断依据。排序不看它们 |
| 观测到的 | `source_post_ids` `first_seen_at` `latest_seen_at` `observed_at` `discussion_count` `author_count` `engagement_signal` | 本次扫描真实取回的材料 | 是事实 |
| 算出来的 | 五项 sub-score、`trend_score`、`trend_score_raw`、`trend_reasons` | `app/agent/trends.py` | 可复现、可解释 |

三条边界值得单独说：

**`engagement_signal` 是观测到的加权互动量，不是"热度"。** 它的定义写在
`trends.ENGAGEMENT_WEIGHTS` 里（点赞 ×1、转发 ×2、回复 ×2、引用 ×3），
引用最高是因为它意味着有人**带着自己的话**把讨论接下去。

**`source_post_ids` 指向本系统的 `research_item.id`**，与 `EvidenceFact.source_ids`
是同一个 id 空间。两处各引一套 id，追溯链会在中间换一次尺子——那是最难查的一类断链。

**`topic_id`（t1、t2…）由工具赋值，不采信模型给的值**，与 `fact_id` / `angle_id` /
`candidate_id` 同一套做法。

`normalized_topic` / `topic_fingerprint` 只作为设计预留存在：**不落库、不查询、
不参与打分、不影响 Planner**（本阶段不做跨 Run 的话题归一，见第 11 节）。

---

## 3. Trend Score 的计算方法

**不是"X 官方热度排名"，也不是全网热度。** 它是：

> 基于当前能够可靠取得的数据计算的项目内部 ranking signal。

X 的公开 API 只给 like / reply / repost / quote 四个计数。我们看不到曝光、
看不到转发链、看不到窗口之外发生了什么——所以任何"热度"的说法都是我们编的。
下面每一项都只用本次扫描真实取回的字段（`app/agent/trends.py`）：

```text
trend_score = 100 × ( recency×0.30 + spread×0.25 + engagement×0.20
                    + source_quality×0.15 + novelty×0.10 )
```

| 项 | 定义 | 为什么是它 |
|---|---|---|
| `recency` | `0.5 ** (最新一条的年龄 / 24h)` | "为什么是现在"的量化。没有发布时间就记 0，**不**假装新鲜 |
| `spread` | `min(1, 不同作者数 / 4)` | 防"一个大 V 发了一条 → 分数虚高"（工作单第十节）。空作者名不计入 |
| `engagement` | `log1p(加权互动) / log1p(500)` | 转发/引用是选择放大，比点赞重；取对数让 500 与 50000 不至于差 100 倍 |
| `novelty` | 首条出现在 72 小时窗口内的位置（0=窗口起点，1=刚刚） | 不是"这个词没见过"，是**时间位置**——窗口后段才冒出来的才是新变化 |
| `source_quality` | `可核验引用数 / 声称引用数` | 编造出处要有代价。分母是**声称数**，不是核验数 |

**单一来源上限：** 作者数 < 2 或可核验来源数 < 2 时，`trend_score` 被压到 40 以下，
且**永远不会**被判成 qualified，无论四个计数值多高。封顶前的值留在 `trend_score_raw` 里——
被压过头却不留痕迹，等于把"为什么只有 40 分"变成一个查不出来的问题。

**确定性**：`trends.py` 是纯函数库，没有 IO、没有随机、没有 `datetime.now()`
（观察时刻 `observed_at` 一律由调用方传入）。同一份输入必然得到同一个分数。
排序键 `(-trend_score, -discussion_count, title)` 末项用标题，是为了让并列不再是
"顺序未定义"——LLM 给出的簇顺序对同一批材料并不稳定。

**可解释**：每个分项都写成一句带分子分母的话（`trend_reasons`），例如
"3 位不同作者参与，spread 0.7500（满值 4 人）"、"引用 7 条来源、其中 6 条可核验"。
"可解释"不是把公式印出来，而是人能拿着它回原始材料复核。

---

## 4. Evidence 模型

证据包是 `EvidencePack`：`facts` / `consensus` / `counterpoints` / `unknowns` / `source_ids`。

追溯链是**两跳**，每一跳都有工具在核验：

```text
Angle.evidence_ids ──→ EvidenceFact.fact_id ──→ EvidenceFact.source_ids ──→ research_item.id
        （引用事实）                    （事实的出处）              （真实取回的材料）
```

- `fact_id` 由工具赋值（f1、f2…），**不采信模型给的值**。核验不上的出处会被
  **丢弃而不是保留**——见 `tools/evidence.py::_keep_traceable`。丢掉一条事实是保护下游。
- 而 `source_quality` 那条（第 3 节）对同一件事的处置**相反**：那里保留、但扣分。
  两处处置不同是有理由的：证据链上留一条引不到的出处会污染下游；
  打分链上静默丢弃一次编造，等于让编造毫无代价。

`counterpoints`（与主流说法相左）和 `unknowns`（查不到/互相矛盾）是**一等公民**：
观点型内容的张力通常来自前者，而后者正是 Planner 决定"要不要再研究一轮"的依据。
显式留白比装作知道更有价值。

---

## 5. Topic qualification

`qualify()` 返回 `(qualification, lifecycle)`，判定顺序是**从硬约束到软约束**：

```text
1. 时间     最新一条超过 72 小时（或完全没有时间戳）  → stale
2. 结构     作者数 < 2 或可核验来源数 < 2            → weak_support
3. 分数     trend_score < 55                        → low_signal
            否则                                      → qualified
```

**顺序不能反。** 先看分数的话，一条爆款帖子会把前两条一起盖过去——而那正是工作单
第十一条要避免的"看到一条热门 Tweet 就直接宣布这是今天热点"。

时间排在最前是因为它回答的是**事实**问题而不是量的问题：一条很旧但很热闹的帖子，
分数可以很高（`stale_topic` fixture 是 61.27 分），判定仍然是 stale。

`lifecycle` 是给控制台与后续阶段用的状态：`discovered` / `qualified` / `researching` /
`discarded` / `stale`。**不达标的候选仍然留在候选列表里**——Planner 要能看到全部，
只是它有分档可依。

---

## 6. Planner 如何使用发现（以及什么时候**不**用）

### 动作没有新增，`discover_topics` 是被**升级**的那个

工作单里叫它 "Trend Scout"。实现上它仍然是同一个动作名、同一个 Tool 槽位
（Decision 1：不新增 `discover_trends`），但它现在做四件事：
围窗口（72 小时，并把窗口原样传给 X 做服务端过滤）→ 让模型**分组** →
Python **打分** → 分档判定。

模型给的簇里**没有任何分数字段**（`TopicClusterDraft` 里就没有），
所以"LLM 随机给 95 分然后称 trend_score = 95"这件事在结构上做不到。

### `select_topic` **不调 LLM**

分数已经算好并排好序了，再让模型"选一次"等于把同一个判断做两遍，还给了它一次
推翻确定性排序的机会。这一条规定来自工作单第十一节。`target` 认 `topic_id` 或标题原文，
都不认就回退到**分数最高的 qualified 候选**，并把回退写进 summary。

它是一次性的：选完之后 `select_topic` 不再可行，Planner 换不了题——
换题会让已取回的材料、已形成的证据与新选题对不上，而 state 里没有任何字段能表达
"这些材料属于上一个题"。

### Goal 决定从哪里开始（工作单第八节）

`research_topic` **不再要求先有过候选选题**。目标是
"帮我研究一下 MCP 在企业 Agent 里的采用情况"时，题目已经给定了，强行先做一轮
discovery 只会多花一次 X 调用、多花一步预算，还可能把用户指定的题目换成一个"趋势更好"的。
没有候选时 research 工具用目标文本兜底。

`FALLBACK_ORDER` 里 `DISCOVER_TOPICS` 仍排在 `RESEARCH_TOPIC` 前面：开局两者都可行时，
模型选了个不可行的动作，回退应当选**制造选项**的那一个。

### Planner prompt 里新增的一条

做 `discover_topics` 时**必须**在 `arguments.query` 里给检索词。用户的
"今天帮我找一个值得讨论的 AI Agent 话题"是一句**委托**，不是搜索词——拿它去检索 X
只会按字面分词匹配，回来的材料与"AI Agent 的讨论"没有关系。这条是真机 smoke run
第一次跑时发现并补上的（当时 prompt 里只要求 `deepen_research` 给检索词，
于是 discovery 退化成"把整句目标丢给 X"）。

---

## 7. 检索词：一条链接不该杀死一次运行

真机 smoke 第二次跑时，一次运行在第四步上直接死掉了：

```text
GET /2/tweets/search/recent?query=https%3A%2F%2Ft.co%2FRMhWA6lmX9
400 {"message":"...no viable alternative at character '/' (at position 7)"}
```

链条是这样的：检索回来的材料里有一类帖子正文**就是一条链接**（分享型）→
它独自成了一个候选选题，标题就是那条链接 → 没有别的达标候选，`select_topic`
按"分数最高的"选中了它 → `research_topic` 拿标题去检索 → X 判参数非法 →
异常从工具里抛出去，Run 判失败。

三步处置，各管一段：

**这类材料撑不起一个选题**（`app/agent/trends.py`）。标题换成只陈述事实的占位
文本「（材料只有链接，没有可归纳的正文）」，discover 的 prompt 里也加了一条：
正文只有链接的材料不能单独成题。

**标题换掉，分数一点不改。** 单一来源本来就会被压到 40 分以下、判成
`weak_support`。保留分数是为了让"这批材料撑不起一个选题"在候选列表里**看得见**
——把这类材料从打分里摘掉，它就从"低分但看得见"变成"不存在"，而后者在报告上
看不出发生过什么。

**哪些字符串不能交给 X**（`app/agent/search_query.py`，新模块）。X 用自己的
查询语法解析 `query`，其中 `/`、`:`、ASCII 括号与 `|` 有特殊含义（括号是分组
运算符，`|` 是 OR，`/` 与 `:` 构不成任何合法 token），而一条 URL 带前两个。
`|` 是终验收阶段补进去的（见第 13 节 Defect 3）。这个模块只做两件纯函数
的事：判断一个字符串能不能直接用；在一串**有优先级**的候选里取第一个能用的。
顺序仍然由调用方给，它不改变任何一条既有的优先级规则——只把"不能用"的挑出去。
discover 的检索词链（决策给的词 → target → 目标）与 deepen 的候选链（决策给的词
→ 未知项 → 反方 → 原题）都过这一层。

**换掉的词要说出来。** 候选全都不能用时（例如目标本身就带 `/`），它把非法字符
换成空格：`MCP/Agent` → `MCP Agent` 是有意义的降级，而一条 URL 会变成一串碎片
——难看，但**是从原文来的**。编几个关键词会体面得多，那才是这一层绝不能做的事。
降级与替换都写进 summary：只写在日志里，界面上就只剩一个与材料对不上的题目。

**边界为什么划在这里而不是 `app/services/x_api.py`。** 与 `end_time` 那条正好相反：
`end_time` 是**传输层**的硬约束（不满足就没法发请求），所以它归客户端；
而"换成什么词才对"是**语义**决定，客户端没有这个答案，所以它归工具层。

---

## 8. Budget 如何约束

原有预算一个都没变（`config/agent.yaml`：`max_steps` 12、`max_research_calls` 4、
`max_candidates` 3、`max_revisions` 2），discovery 被**算进研究预算**：

- `DISCOVER_TOPICS` 要求 `research_count < max_research_calls`。不这么算的话，
  "发现不出东西"会变成一个可以无限重试的动作。
- 它同时是一次性的（`not filled(candidate_topics)`），发现过就不再出现在可行动作里。
- 兜底路径（模型给的簇全被核验丢掉）退化成"一条帖子一个候选"而不是报错：
  一次 discovery 失败不该让整条 Run 连一个候选都没有。退化出来的候选会被判成
  weak_support、分数压在 40 以下，**不会被误当成好选题**，而且这件事会写进
  tool summary 与日志（`logger.warning`），藏不住。

窗口的右端不是"现在"，而是"现在减 15 秒"：X 硬性要求 `end_time` 至少比请求时刻早
10 秒，否则整个请求 400。这条约束实现在 `app/services/x_api.py`（最靠近 X 的地方），
而不是让每个调用方自己记得退——真机第一次跑就是 4/5 次撞在这上面。

---

## 9. Evaluation 怎么验证

三层，各管一段，**没有任何一层依赖真机**（`docs/agent-evaluation/`）：

**单元层**（`tests/test_agent_trends.py`，23 条）：`trend_score` 是纯函数，
所以可以被单独证明——同一输入两次调用结果完全相同；改一项输入只有该变的那一项变；
结构不足时封顶（一个大 V 连发十条仍然只算一个作者）；窗口只有一个定义
（`scan_window` 的宽度恒为 72 小时，且它不取挂钟时间）。

**工具层**（`tests/test_agent_discover.py` 8 条 + `tests/test_agent_search_query.py` 20 条 +
`tests/test_agent_loop.py` 新增的 3 条 + `tests/test_agent_tools.py` 新增的 3 条）：
短编号 → 真 id 的往返、落库 id 与候选引用 id
是同一批、编造的编号照旧扣分、整组引用失效时退化成"一条帖子一个候选"而不是抛异常；
链接标题被换成占位文本而分数不变；**实际发给 X 的那个 query 能搜**，且带得动窗口
（`A | B` 这类词不会原样发出去，三条检索路径的 `start_time` / `end_time` 逐字相同）；
`finalize` 把选定的题目落库、不覆盖已有的题目。
这些测试都刻意走**完整工具调用**而不是单测辅助函数：它们要防的缺陷没有一个在辅助
函数里，全部在它们被调用的地方（第 13 节三条缺陷同理）。

**规则层**（`evals/behavior/rules.py` + 6 份新 fixture + `goals.json`）：

| 规则 | 级别 | 断言 |
|---|---|---|
| RULE-015 | gate | 单条帖子不能单独制造高可信 trend：`qualified` ⇒ 作者数/来源数达标；结构不足却分数越顶也算失败 |
| RULE-016 | gate | 交付路径真的经过证据：只看 trace，要求 `extract_evidence` 出现在首次生成之前、`criticize` 在最后一次生成之后 |
| RULE-017 | gate | trend_score 可复现：用生产的 `score_topic` 从 `trend_inputs` 重算两次，比对每一个分项与时间戳 |
| RULE-018 | gate | stale 不得被标记为 fresh：按各话题自己的观测时间判年龄 |

RULE-015 / RULE-017 是**两侧夹住同一个失败模式**的：前者管判定（结构与 qualified 是否自洽），
后者管算术（这个数字是不是由这个公式产生的）。中间那条缝——"判定对、数字也对，
但数字是用别的口径算出来的"——由 RULE-017 的重算堵住。

RULE-016 读的是 trace，不是 state：这是 state 侧的 RULE-006 看不到的那一面。
**state 齐全只说明结果对，不说明过程对**（恢复过的 checkpoint、手工导出的记录、
将来新增的代码路径都可能在 state 上看着完整）。

`evals/selftest.py` 除了要求"每条 gate 都得有一份能让它红的 fixture"，还反向要求
**每条新规则两边都能判**（`check_trend_rules_discriminate`）：多作者话题必须过、
单条爆款必须红；`fresh_topic` 与 `stale_topic` 同结构只差时间，必须一个判 fresh 一个判 stale。
没有这条，"永远失败的规则"会一直绿着。

**复现方式**（`evals/` 与 `app/` 的依赖是单向的，`evals → app`）：

```bash
python -m evals.selftest           # 12 通过 / 0 失败；14 条 gate 与 severity 一致，共 18 条规则
python -m evals.run_benchmark --dataset   # 16 通过 / 0 未通过（13 份 fixture）
pytest -q                          # 341 passed（必须串行，见 docs/setup-new-machine.md）
```

> 在**工作树**里跑 `evals` 必须用 `python -m evals.…`，不能 `python evals/run_benchmark.py`。
> 本机 conda 环境的 site-packages 里有一个 `_editable_impl_x_agent.pth` 指向 master 检出，
> 直接按路径执行脚本时 `import evals` 会解析到 **master 的** `evals` 包。这是本机的安装
> 状态导致的，不是仓库的问题；`scripts/smoke_5b1.py` 用显式 `sys.path.insert` 绕开同一件事。

---

## 10. 真实 smoke test

五次**真机只读**运行：真实 X 检索 + 真实 DeepSeek + 共享 PostgreSQL。
脚本 `scripts/smoke_5b1.py`，记录写在 `docs/phase-5b1-smoke-runs.json`。

**没有发布任何内容。** 这不是靠"记得不要发"保证的：`RealXApiClient._request` 被换成一层
守卫，每个请求先落进清单再放行，**方法不是 GET 就直接抛**。五次运行对 X 共发出
**10 次请求，全部是 `GET`，`POST` 为 0**。

### 最终验收批次（本阶段唯一有效的五连批次）

> 批次标识：`generated_at = 2026-09-30T05:27:22Z`，
> 五次运行 `run_id` 见 `docs/phase-5b1-smoke-runs.json`。
> **这是修复第 13 节三个缺陷之后重跑的整批**，不是从更早的失败批次里挑出来的。
> 更早的批次（含 smoke-4 因 `|` 触发 400 的那一批）已作废，不再作为验收依据。

| # | steps | X 检索 | 候选 | 事实 | 异议/未知 | 评审 | 改稿 | 耗时 |
|---|---|---|---|---|---|---|---|---|
| smoke-1 | 8 | 1 | 5 | 18 | 3 / 9 | 1 | 无 | 84.6s |
| smoke-2 | 13 | 2 | 6 | 9 | 4 / 7 | 3 | 2 | 153.9s |
| smoke-3 | 10 | 3 | 4 | 5 | 1 / 6 | 1 | 无 | 70.2s |
| smoke-4 | 13 | 2 | 4 | 7 | 2 / 7 | 3 | 2 | 131.5s |
| smoke-5 | 9 | 2 | 5 | 16 | 2 / 9 | 1 | 无 | 86.1s |

五次选定的题目：

| # | selected_topic |
|---|---|
| smoke-1 | Meta 被指加速进入 AI Agent 竞争，从 Llama 走向 Muse |
| smoke-2 | OpenAI 因内部安全测试未过关，暂停 GPT-6.1 Astra 发布 |
| smoke-3 | 🚀 找官方 MCP Server 不再大海捞针：开发者必备的 MCP 生态清单 |
| smoke-4 | AI 竞争重心从模型能力转向成本、速度与落地干活 |
| smoke-5 | Meta 被指加速进入 AI Agent 竞争，Muse 引发"从模型能力到智能体"的讨论 |

五次结果：**5/5 全部满足工作单第二十一节的验收条件**，判定写在同一份 JSON 的
`acceptance` 字段里（逐条判据与原始字段同源，可以自己复核）。具体地：

- 每次都停在 `waiting_for_review`，`Guardrail` 通过，**没有触发任何发布**；
- 每次 `agent_run.topic` 都已落库，且与 checkpoint 里的 `selected_topic` 逐字一致（5/5）；
- **发出的 10 个检索词全部带 72 小时窗口**，宽度 71.996 小时（右端被
  `x_api.END_TIME_MIN_LAG` 收了 15 秒），没有一次退化成无界的 recent search；
- 落到库里的 **98 条材料里，`published_at` 超出窗口的有 0 条**（同一判据在修复前
  是 5 条里 2 条超标，见第 13 节 Defect 2）；
- 发出的检索词里含 X 非法字符的 **0 个**（含 `|` 的 0 个）；
- smoke-1 与 smoke-5 是同一个目标，两次选出的题目都落在"Meta 的 Muse/智能体竞争"
  这一件事上但措辞不同——同一热点，候选有合理漂移。

---

## 11. 已知限制

**trend_score 不是"X 官方热度排名"。** 第 3 节已经说过，这里再重复一次是因为它最容易被误用：
它是项目内部的排序信号，只反映"我们这次扫描取回的数据"。X 的公开 API 不给曝光、
不给转发链、也不给窗口外的任何信息。

**中文科技圈的检索质量受限于 X 的检索能力。** `search/recent` 是关键词字面匹配 +
服务端时间过滤。用 "AI Agent 智能体 讨论" 这类词检索回来的中文内容里，相当一部分是
低互动、与话题无关的帖子（观测到 like/reply 为 0），其中还有一类正文**只有一条链接**。
系统不会把这两类材料伪装成热门——spread / engagement 会把它们打到低分，链接材料
也不会再变成"标题是一条网址"的候选（第 7 节）——但"检索回来的池子本身质量不高"是
数据源的限制，不是打分的限制。

**几乎没有一个话题能"达标"。** 五次真机运行里，唯一反复被判成 `qualified` 的话题是
同一件：72 小时内被**两位作者**同时提到的英伟达 Agent 安全平台。其余候选几乎全是
单作者单来源 → 封顶 40 分 → `weak_support`。这不是打分过严，而是这个检索池的真实
结构：中文科技内容在 X 上的"多点讨论"本来就少。**这条限制比任何一条规则都更能解释
"为什么候选看上去都差不多"**，别把它误读成发现能力不行。

**检索词由 LLM 生成，质量直接决定发现质量。** Prompt 已经明确要求给检索词而不是整句目标，
但模型给什么词仍然不可控；给得不好时候选会退化成"一条帖子一个候选"，
日志里有 warning，UI 上没有专门提示。工具侧只保证"发出去的词 X 一定能解析"，
不保证它是个好词。

**Planner 偶尔会返回非法 JSON，一次运行就此失败。** 五次真机运行里出现过一次
（`Invalid JSON: expected ',' or '}' at line 1 column 85`）。现在错误信息里会带上
模型原始输出的前 200 字，**能诊断了，但没有自动重试**——加了重试就等于替模型
把这一步重做一遍，那件事该不该做、算不算一次决策，本阶段没有结论。所以它仍然
是一次运行失败，只是下一次能看清它为什么坏。

**"模型的话没说完"原先会被报成"JSON 语法错误"。** 一次真机运行里 `extract_evidence`
报 `Invalid JSON: expected , or ] at line 1 column 5959`，读起来像是模型写坏了结构，
实际上更可能是输出撞上了服务端的 `max_tokens`——被截断的输出是一段**合法前缀**，
解析器只能看到"括号没闭合"。两者的处置完全不同（一个是调 prompt，一个是调上限），
所以现在 `app/services/llm.py` 在返回前先看 `finish_reason`，是 `length` 就直接报截断。
本阶段**没有设置 `max_tokens`**（沿用服务端默认），也没有自动重试：先把错误说对，
要不要提上限是另一件事。

**同一次失败在两处读起来不一样。** httpx 连接失败时抛的 `ConnectError("")` 的 `str()`
是空的，真正的原因（DNS / 代理 / TLS）在 `__cause__` 里。发布路径从 2026-09-23 起
就会顺着原因链往下挖，而冒烟脚本自己拼 `f"{类型}: {exc}"`，于是报告里留下
`XApiTransportError: X API request could not be sent:`——冒号后面什么都没有，
而这是这份记录唯一的线索。现在两处共用 `app/diagnostics.py::describe`。
**这不改变失败本身**：一次网络抖动仍然会让那一次运行失败（smoke 里发生过两次），
只是下一次能看清它为什么失败。

**候选与证据包不落库，只活在 LangGraph checkpoint 里。** 这是 Decision 2 的结果
（`candidate_topic` / `evidence` / `angle` / `candidate_content` 都不建表）：
它们是一次 Run 的工作状态，与 Phase 5A 的 `guardrail_*` 机制相同。checkpoint 被清理后，
控制台只剩决策轨迹，看不到"当时抽出了哪些事实、选了哪个角度"。

**`agent_run.topic` 要到 `finalize` 才写。** 终验收把题目落库收成一条接缝
（第 13 节 Defect 1），代价是**运行中的 Run 在列表里暂时看不到题目**，
一次中途失败的 Run 也不会留下题目。这是有意的取舍：换来的是"只有一处写库"，
而不是两个节点各自判断该不该写。要看运行中的题目，看 checkpoint 里的
`selected_topic`。

**本阶段没有跨 Run 的智力。** 同一个话题今天发现、明天再发现，两次互不知道；
`normalized_topic` / `topic_fingerprint` 只是设计预留，不落库、不查询、不参与打分。

**真机验证停在 `waiting_for_review`。** review → resume → publish 这条路径
由 `tests/test_agent_loop.py` 覆盖，smoke 按工作单不触发发布。

**没有验证"足够多的样本"。** 五次运行是一次性的观察记录，不是统计结论：
X 的内容每天不一样，换一天跑分数会变。它要证明的是"这条链路在真机上走得通、
每一步都留得下可核对的记录"，不是"发现质量有 X% 的提升"。

---

## 12. 明确没有做什么

工作单第二十四、三十九节的排除项，本阶段一条都没有引入：

```text
Style Distillation          高粉账号分析              Viewpoint Engine
Persona learning            Feedback loop             Fine-tuning / LoRA
X metrics ingestion         自主发布                  Multi-agent
embedding pipeline          向量数据库                Redis / Celery / Kafka
Playwright / 浏览器自动化   Google / Bing 抓取        第三方搜索 API
```

数据源一个都没有新增：**只用了现有的 X 只读检索**（工作单第六节）。
数据库只加了 **两列**（`research_item.quote_count` / `observed_at`），
没有新建任何表（工作单第三十节："不要为了一个字段就建一张表"）。
新增的生产代码只有两个模块：`app/agent/trends.py`（打分与判定，纯函数）
与 `app/agent/search_query.py`（哪些词能交给 X，纯函数）——都是**没有 IO 的纯函数
库**，因为这一阶段真正想加的能力是"判断"，而判断必须可复现。
另有 `app/diagnostics.py`，它是把 `app/services/publish.py` 里已有的异常原因链渲染
**提取**成共享模块（发布路径与冒烟脚本共用一份），没有新增任何行为。

前端只改了 5 个文件（工作单第二十九节的 3～5 个），且只做展示层：
动作中文名、研究素材的"引用"计数与三个时间（发布/抓取/入库）各显示各的。
没有 UI 大改。

终验收修复那一轮（第 13 节）额外**只**做了三件事：`UNUSABLE_CHARS` 加一个 `|`、
窗口计算抽成一个共享纯函数、题目落库收成 `finalize` 一处。没有加 retry，
没有改 `LLMService` / 两个 parser / Agent budget / `max_steps`，没有动打分公式、
权重、单来源封顶或达标阈值，没有改 schema，没有**批量回填历史 Run 的 topic**
（历史数据保持审计事实），没有碰前端。

**没有并回 master。** 分支停在 `feat/phase-5b1-content-intelligence`。

---

## 13. 终验收修掉的三个缺陷

第一次终验收的结论是"代码整体通过，但五次真机 smoke 只有 4/5"。那一次失败
不是网络问题（X 明确回了 400），追下去牵出三个**生产代码**缺陷。三个都修了，
都补了回归测试，都在修复后的真机批次上复验过。三条都保留原始记录，
不做历史回填。

### Defect 1 — 选出来的 topic 没有落库

**根因。** `select_topic` 把选定的题目写进 state 的 `selected_topic`，**不写库**；
而唯一写库的那一句在 `finalize` 里，条件写成 `if not state.get("selected_topic")`。
于是最正常的那条路径——选完题目直接出稿——条件恒为假，分支被整个跳过。

真机数据（修复前）：

```text
有 goal 的 run          113，topic 为 NULL 87
有 goal 且有 draft 的    50，topic 为 NULL 29
这 29 个逐个核对 checkpoint：state 里有 selected_topic 的 29 / 29
```

即"Agent 确实选了题目，数据库不知道"。运行记录列表里这些 Run 全是空题目。

**修复。** `app/agent/nodes/finalize.py` 成为**唯一**的落库接缝：以 `selected_topic`
为准，没有它才回退到分数最高的候选（兜底依据必须是 `trend_score`，不能编）。
`app/agent/tools/research.py` 里那份重复的 `set_run_topic` 删掉——工具层不再
各写各的（工作单第十四节要求）。`set_run_topic` 原有的 `WHERE topic IS NULL`
保留，所以已写下的题目不会被半路改名。

**取舍（明写在这里免得被当成缺陷）。** 题目现在在 `finalize` 时才出现，
运行中的 Run 在列表里暂时是空题目；`research_topic` 时代是跑到中就写了。
换来的是一条路径、一个条件，而不是两处写入各自判断。中途失败的 Run 不再
留下题目——那本来也不是它的"最终 topic"。

**回归测试**（`tests/test_agent_loop.py`）：
`test_finalize_persists_the_selected_topic`（state 有 `selected_topic` → 库里有，
且与分数最高的那个候选刻意不同，证明取的是选定的而不是重挑的）、
`test_finalize_falls_back_to_the_top_scoring_candidate`、
`test_finalize_does_not_overwrite_a_topic_that_is_already_set`。

**真机复验。** 修复后五连批次：`state.selected_topic` 与 `agent_run.topic`
逐字一致 **5/5**。历史那 29 个仍是 NULL——**没有做任何批量回填**
（工作单第二十六节），历史数据保持审计事实。

### Defect 2 — 72 小时窗口只贯穿了链路的第一步

**根因。** 窗口只在 `discover_topics` 里算并传给 X，`research_topic` 与
`deepen_research` 调 `x_service.research(query, max_results=...)` 时不带
`start_time` / `end_time`。X 于是用**自己的**默认范围（最近 7 天）过滤，
回来的材料看着正常、只是不是被问到的那一段。

真机证据（修复前 smoke-3）：5 条材料里 **2 条 `published_at` 在 72 小时窗口外**，
最早的 `2026-09-23`，比观察时刻早约 7 天。

**修复。** 窗口计算抽成一个纯函数 `app/agent/trends.py::scan_window(now)`
（`now` 由调用方给，模块本身仍然不取挂钟时间——第 3 节的纯函数约定不变），
三条检索路径共用它：`discover_topics`、`research_topic`、`deepen_research`
一律带 `start_time` / `end_time` / `observed_at`。右端那 15 秒仍由
`x_api.END_TIME_MIN_LAG` 在传输层收，不在这一层。

**回归测试**：`tests/test_agent_trends.py::test_scan_window_is_the_single_definition_of_recent`；
`tests/test_agent_search_query.py` 里
`test_research_topic_carries_the_same_window_as_discovery`、
`test_deepen_research_carries_the_same_window_as_discovery`、
`test_material_outside_the_window_cannot_enter_the_run`（假客户端按收到的窗口做
服务端过滤，和 X 一样，所以"窗口没传下去"会让它立刻失败）；
`tests/test_agent_discover.py::test_discover_declares_the_scan_window_it_used`。

**真机复验。** 五连批次里**发出的 10 个检索词全部带 72 小时窗口**
（宽度 71.996 小时）；落库的 **98 条材料里窗口外的 0 条**。
判断一律用 `published_at`，不是 `created_at` 也不是 `observed_at`。

### Defect 3 — 检索词没屏蔽 `|`

**根因。** `app/agent/search_query.py` 的 `UNUSABLE_CHARS` 是
`("/", ":", "(", ")")`，漏了 `|`。X 的查询语法里 `|` 是 OR 运算符，
单独出现就是语法错误：

```text
GET .../search/recent?query=...%20%7C%20...
400 {"message":"... no viable alternative at character '|' (at position 20)"}
```

第一次终验收的 smoke-4 就是这样整次失败的。`A | B` 是中文标题里很常见的并列
写法，`discover` 的兜底路径又直接拿帖子正文当标题，所以它出现的概率不低。

**修复。** 只加一个字符：`UNUSABLE_CHARS = ("/", ":", "(", ")", "|")`。
处置沿用既有规则——`choose()` 把非法字符换成空格并在说明里写明降级过，
**没有**新增"遇到非法字符就中止"的分支（工作单第十一节：不做复杂 sanitizer）。
一个字符废掉整次运行，比降级一个词更糟。

**回归测试**：`tests/test_agent_search_query.py` 里
`test_the_pipe_operator_is_not_usable`、`test_a_pipe_is_degraded_to_a_space_not_dropped`、
`test_a_pipe_query_never_reaches_x`、`test_a_pipe_is_stripped_even_when_nothing_else_is_usable`
——断言的是**实际发给 X 的每一个 query**，不是 state 里记的题目。

**真机复验。** 五连批次发出的 10 个检索词里含 `|` 的 **0 个**、
含任何非法字符的 **0 个**。

### 本轮明确没做的事

没有给 LLM 的非法结构化输出加自动重试（第 11 节仍按 Known limitation 记），
没有动 `trend_score` 的公式 / 权重 / 封顶 / 阈值，没有改 schema、没有新增表、
没有批量回填历史数据、没有碰前端。三个缺陷之外的代码一行未改。

---

**本阶段到此为止**（工作单第三十九节）。下一步做什么属于下一个阶段，见交接文档。
