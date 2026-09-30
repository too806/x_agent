# Phase 5B.2 — Style & Structure Intelligence

**范围**：让 Agent 知道"中文科技内容该怎么表达"——用真实语料统计出的**结构约束**，
而不是模仿某个具体的人。

**基线**：`master`，HEAD `82dfb37`（Phase 5B.1 COMPLETE）。

**结论**：完成。Style Recipe 从语料统计推导 → 进 Writer prompt → Critic 按它判
`style_fit` / `structure_fit` → Run Detail 能看见这次采用了什么结构。**Writer 从不出
样本原文**这条由机器守着（`evals/style/selftest.py` 的 n-gram 比对）。
**本阶段没有发布任何内容**：全部 X 请求为 GET（Smoke 报告里 POST 计数 = 0）。

---

## A. 交付概览

**新增**

```
app/style/features.py     确定性文本原语（从 evals/content/ai_style.py 上移）
app/style/taxonomy.py     结构/钩子/内容类型分类法 + 账号分类
app/style/schemas.py      StyleProfileFeatures / StyleRecipe / StyleFit / AiStyleSignals
app/style/profile.py      样本 → StyleProfile（纯函数）
app/style/recipe.py       Profile → StyleRecipe（确定性，不调 LLM）
app/style/runtime.py      运行期取 recipe 的唯一入口
app/style/annotate.py     LLM 语义标注（结构化 JSON，offline）
app/style/corpus.py       采集/筛选（组合条件，阈值全在配置）
app/style/signals.py      确定性 AI 味辅助信号
app/style/labels.py       枚举 → 中文（prompt 与 Run Detail 共用）
app/models/{tracked_account,style_sample,style_profile}.py
alembic/versions/c3d8a45e7f21_phase_5b2_style_intelligence.py
config/style.yaml
evals/style/{__init__,corpus,spans,selftest}.py
scripts/collect_style_corpus.py
scripts/smoke_5b2.py
tests/test_style_{features,profile,recipe,corpus,writer_integration}.py
```

**改动**：`app/agent/{prompts,schemas,state}.py`、`app/agent/tools/{candidates,critic,revise,__init__}.py`、
`app/db/checkpointer.py`、`app/services/{admin,x_api,x_service,llm}.py`、`app/schemas.py`、
`app/config.py`、`app/models/__init__.py`、`app/repositories.py`、`tests/support.py`、
`tests/test_agent_loop.py`、`evals/{content/ai_style,behavior/rules,model,selftest}.py`、
`prompts/agent/{candidates,critic,revise}.txt`、`frontend/src/{components/RunDetailDrawer.tsx,types/api.ts}`。

27 个文件改动（+852 / −180），31 个新增。

---

## B. 只读审计结论（工作单第四节）

写代码之前先指认问题。六条原因，每条都能在代码里指到具体行：

1. **Writer 的输入里没有结构这一层。** `prompts/agent/candidates.txt` 的全部输入是
   PERSONA / GOAL / TOPIC / EVIDENCE / CLAIM / WHY_IT_MATTERS / MAX_LENGTH / COUNT。
2. **persona 的正向约束只有两句**（`config/persona.yaml`：「开门见山」「短句为主」），
   其余 6 条全是禁止项。**禁止项能排除坏写法，不能生成好写法**——没有正向结构目标时，
   模型退回默认语域。
3. **唯一的"结构"概念是硬编码在模板里的三条 label**（直接观点/信息密集/反共识），
   写在 prompt 模板里、不可配置——正是工作单第十六节点名要避免的固定模板。
4. **`max_length: 140`** 要求"有事实、有数字、有判断"的中文正文，模型只能靠抽象概括压缩。
5. **"AI 味"在生产链路里只有一次 LLM 打分**（`CriticResult.generic_ai_style`），不可复现；
   而 `evals/content/ai_style.py` 里已有一整套确定性指标，**生产代码一行都没用**。
6. **Critic 手上没有可比对的目标结构**，所以 `style_fit` / `structure_fit` 当时无从判起——
   不是 Critic 不行，是没有被评判的目标。

**现有数据能支持什么**：`research_item` 有正文、互动、两套时间，足以描述"一条帖子"；
`agent_run` / `draft` / `review` 足以回溯"一次运行"。

**还缺什么**：没有任何地方能表达"这是经过筛选、进入 Style Corpus 的样本"。
`research_item` 三个理由都挡着（见 C 节）。

**哪些能力留在 checkpoint**：`StyleRecipe`（这次运行选了什么结构）、`CriticResult.style_fit`
——都是**单次运行的工作状态**，与 `guardrail_*` / `final_content` 同性质。
**哪些属于长期样本数据**：账号集合、样本本身、Profile——它们的生命周期长于任何一次 Run，
且要在运行之间复用。

---

## C. 三张新表（工作单第三十三节的三问）

对每一张，三个问题都有答案。

### 1. 为什么 checkpoint 装不下

checkpoint 绑定一次 Run，它的生命周期是"这次运行"，不是"这个语料"。Style Corpus 要在
**许多次运行之间**复用；放进 checkpoint 等于每次运行重算，而重算就得在请求路径上拿到
全部样本原文——正是工作单禁止的"大段原文进入运行期"。

### 2. 为什么不能复用 `research_item`

- `research_item.agent_run_id` 是 `NOT NULL` + `ON DELETE CASCADE`：删一次 Run 会连带
  删掉它采到的全部样本，而 corpus 必须比任何一次 Run 活得久。
- `research_item` 刻意**没有** `UNIQUE(platform_post_id)`（多轮检索取回同一条帖子，在趋势
  打分里就该算多条材料）。corpus 恰好相反：需要去重键，否则一条爆款被数很多遍，把 Profile
  拽向它。
- 语义不同。同一张帖子可以是"这次运行取回的材料"却不是"被接纳进语料的样本"（互动不够、
  是转发、正文不完整）。

### 3. 为什么属于长期语料

样本是外部来源的事实记录；账号集合是人划定的研究范围；Profile 是可复用的统计结论。
三者都不随某次 Run 结束而失效。

### 表结构要点

| 表 | 关键列 | 要点 |
|---|---|---|
| `tracked_account` | `username`(unique) / `category` / `enabled` | **没有**任何可排序的字段：想排也排不了 |
| `style_sample` | `platform_post_id`(unique) / `is_repost` / `is_link_only` / `included` / `exclusion_reason` / 10 个标注列 + 留痕三件套 | **不存**任何确定性特征 |
| `style_profile` | `scope` / `tracked_account_id` / `sample_count` / `sample_window_*` / `feature_version` / `taxonomy_version` / 9 个标量列 / 5 个 JSONB 分布列 | Account 与 Corpus **分开存** |

**刻意不存的**：`is_repost` / `is_link_only` 必须存——它们来自 X 的 `referenced_tweets` 与
正文形态，事后从 `content` 单看是判不出来的。其余（长度、句长、emoji 率、英文比例…）
全部能重算且逐位相同，存一份只会多一个将来会漂移的第二来源。

**刻意没建表**：`candidate` / `evidence` / `angle` / `style_recipe`。它们是单次 Run 的工作
状态，继续只活在 checkpoint 里。特别是 **StyleRecipe 不落库**——它是运行期决策，不是语料资产。

---

## D. 迁移与测试库同步

- **手写 Alembic，没有用 `--autogenerate`**：LangGraph 的 4 张 checkpoint 表不在
  `Base.metadata` 里，autogenerate 会把它们判成"已删除"并生成 `drop_table`。
- `down_revision = 'b7c2e94f1a03'`（迁移前的单 head），新 head `c3d8a45e7f21`，线性单 head。
- **只有新增表，没有给已有表加列**，所以 `tests/conftest.py` 那段手工 `ALTER TABLE ... ADD COLUMN`
  不需要动——`create_all` 会照常建出三张新表。
- `tests/support.py::_ALL_TABLES` 已加上三张表。**这是最容易被漏且完全不报错的一处**：
  新表与 `agent_run` 没有外键，`TRUNCATE agent_run CASCADE` 够不到它们，行会跨测试存活，
  而上一个测试残留的 profile 会变成下一个测试的隐藏输入——顺序相关的偶发失败。
- 已在开发库执行 `alembic upgrade head`，三张表确认存在。
  （`alembic heads` = `c3d8a45e7f21`，单 head。）

---

## E. Style Profile 与 Structure Profile（工作单第五、十七、十八节）

`StyleProfileFeatures` 全部来自**真实样本统计**，不是固定模板：

| 类别 | 字段 |
|---|---|
| 长度 | `median_length` / `p25_length` / `p75_length` |
| 段落与句子 | `median_paragraphs` / `mean_sentence_count` / `sentence_length_distribution{p25,p50,p75}` |
| 表层比率（每 100 字） | `emoji_density` / `question_density` / `number_density` / `english_ratio` |
| 分布（各自和为 1） | `structure_distribution` / `hook_distribution` / `opening_distribution` / `closing_distribution` |

**每个可能算不出来的数都是 `float | None`**：分母为 0 或语料为空时是 `None`，不是 `0.0`。
0 会被读成"测出来是零"，两者在聚合时差别是全部。

**Account Profile 与 Corpus Profile 分开**（`scope` 列）。不合成"超级人格"：合起来会让
"这个特征是谁的"失去答案，而学写法的前提是知道在学谁。运行期 v1 **只用 Corpus Profile**，
不自动挑某一个账号（工作单第十八、三十二节）。

`compute_profile` 是纯函数：无 IO、无随机、不取挂钟时间，不依赖集合遍历顺序。
**同一份 corpus 必然得到同一个 Profile，逐字段相同**——这正是 Smoke 5 与
`evals/style/selftest.py` 直接测的东西。

---

## F. Structure Taxonomy（工作单第十五、十六节）

`TAXONOMY_VERSION = "1.0"`。

- **`StructureType`：10 类 + `unclassified`**（工作单要求 8–12 类）——
  `opinion_first` / `fact_first` / `question_answer` / `contrast` / `how_to` /
  `case_review` / `counterintuitive` / `observation` / `data_reading` / `prediction`。
- **`HookType`：7 类 + `unclassified`**——`question` / `statistic` / `statement` /
  `list` / `story` / `contrast` / `quote`。
- **`ContentType`：6 类 + `unclassified`**——`opinion` / `analysis` / `news` /
  `tutorial` / `experience` / `announcement`。

**一个结构是类别，不是写作模板。** 每个成员描述的是"一篇文章怎么组织"，不是"第一段写什么、
第二段写什么"。后者会把风格蒸馏退化成填空题，而那正是这一阶段要摆脱的东西。

**它是待验证的假设，不是完成品。** 成员之间应当互斥——若实践中大量样本同时符合两类，
说明分类法切错了维度，那是要修分类法的信号，不是允许一个样本挂两个标签的信号。
改分类法必须进 `TAXONOMY_VERSION`，否则两个版本的分布会被当成可直接比较的。

`coerce_label`：模型给的近义写法收不下就落 `unclassified`，**不抛异常、不做模糊匹配**。
为一条样本的标签写法让整批采集失败，代价远大于少归一类；硬把"看起来像"的标签映射过去，
等于让分类法的收敛被一个字符串距离算法悄悄决定。

---

## G. 确定性 vs LLM 语义（工作单第十三、十四节）

分界是硬的：

| 由 **Python** 算（可复现、可回归） | 由 **LLM** 判（不可复现，必须留痕） |
|---|---|
| 长度、段落、句长分位、emoji/问号/数字密度、英文比例、开头/结尾表层形态、具体实体、套话命中 | `structure_type` / `hook_type` / `content_type` / `has_claim`·`has_evidence`·`has_counterpoint`·`has_question`·`has_list`·`has_number`·`has_conclusion` |

LLM 标注**只输出结构化 JSON**：`StyleAnnotation` 是 `extra="forbid"` 的 pydantic 模型，
模型返回多余字段直接失败。**宁可失败，不要一段散文**——"这条帖子写得很自然，有节奏感"
不可比较、不可聚合、不可回归，而且它会诱使下游把它当成真值。

标注留痕三件套：`annotation_model` / `annotation_version` / `annotated_at`。
标注只跑在采集脚本里，**不在 Agent run 里调用**（工作单第三十六节）。

### 上移共用原语（已确认决策 C）

`evals/content/ai_style.py` 里那句 `from evals.values import ...` 若一起搬进 `app/`，
就造出 `app → evals`，把仓库里唯一的单向依赖反过来。所以**拆开**：
原语上移到 `app/style/features.py`（只依赖 stdlib/re，返回朴素的 `float | None`），
`MetricValue`/`UNAVAILABLE` 适配留在 `evals/`。`AiStyleReport` 的字段与语义**逐字不变**。

上移时同步了：`evals/content/ai_style.py`、`evals/content/__init__.py`、
`evals/content/quality.py`，以及文档里写死路径的两处。

### 确定性 AI 味辅助信号（工作单第二十五节）

`app/style/signals.py` 从候选正文算出：模板化开头 / 空洞连接词 / 虚假权威措辞 / 套话 /
模糊限定词 / 重复开头。**按需重算、不入库、不进 state**——它是 `content` 的纯函数，
重算逐位相同，存一份只会多一个会漂移的来源。

它**只作为辅助信号**出现在 Critic prompt 与 Run Detail 里，**不做门禁、不混进 LLM 自己的
分数**。把它做成门禁就等于宣称这几个词表能判定"这是 AI 写的"，而它不能。

---

## H. 采集与筛选（工作单第九～十二节）

- **查询**：`GET /2/tweets/search/recent` + `from:<username>`。**只有 GET**。
- **窗口**：`[now-6d, now]`。取 6 而不是 7 天：X 的 `end_time` 有最小滞后钳制，
  从 7 天整往回数会丢掉窗口最老的那几秒帖子。**刻意不复用 trend 的 72 小时窗口**——
  72 小时回答"为什么这条讨论是现在值得写的"，这个回答"这些人平时怎么写"。
- **`max_results` = 100**（X 要求 [10, 100]，越界直接被拒），**窗口切成 6 段分别问**
  （`slices_per_window: 6`）。这两项必须一起调：一次请求最多回 100 条**且这个客户端
  不翻页**，只问一次的话"窗口 6 天"实际只覆盖窗口最新的那一页。报告里每行写着
  `slice_fetches` 与 `truncated_slices`——**哪一段被截断了，报告里看得见**，
  而不是靠"取回 100 条"去猜它到底是账号的真实产出还是我们的读取边界。
- **筛选是组合条件**，短路在第一处不满足的规则，理由总是**最具体**的那一条：
  空正文 → 纯链接 → 转发/引用 → 回复 → 太短/太长 → 广告抽奖 → 点赞门槛 → 互动折算门槛。
- **回复与转发分开判。** X 的 `referenced_tweets` 是一个混装列表
  （`retweeted` / `quoted` / `replied_to`）。只看"非空"会让一条**回复**带着 `repost`
  这个理由被剔掉——理由错了，"这批语料为什么这么少"的答案也跟着错。两个开关
  （`exclude_reposts` / `exclude_replies`）互相独立。
- **阈值全部在 `config/style.yaml`**，由 `app/style/config.py` 校验（`min_length >= max_length`
  这类越界在加载时就抛，而不是表现为"一条样本都没采到"）。
- **互动量不是质量真值**：它只把零互动灌水挡在外面，在 Profile 里不参与任何质量打分，
  只作为样本属性保留。
- **被排除的样本仍然留行**并写下理由——否则"为什么这批样本这么少"永远无法回答。
- **已存在的 `platform_post_id` 一律跳过，不覆盖**：重跑采集不该悄悄改掉已有样本的互动数
  与 `observed_at`。
- **账号的 `display_name` / `description` 从 X 自己返回的 `includes.users[]` 回填**，
  不凭记忆手打。回填用 `UPDATE ... coalesce(...)` 语句**只填空缺**——先读后写会在
  采集脚本反复跑时把上一次的值当成"用户写的"从而永远不更新。

**这一层的价值全在"静默"上**：一个拼错的分类、一个记错的窗口，都不会让任何东西报错。

### 真实采集结果（2026-09-30）

9 个**人工核对过**的账号（每个都用 `from:<username>` 只读检索确认窗口内确实在发中文科技内容，
不是从任何榜单上摘的），6 天窗口：

| | 数 |
|---|---|
| 对 X 发出的请求 | **54 次，全部为 GET** |
| 取回帖子 | 617 |
| 纳入语料 | **70** |
| 截断的分段 | **0**（79 个分段无一顶到 100 条上限） |
| 重复帖子 | 0 |
| `published_at` / `observed_at` 缺失 | 0 / 0 |

排除理由分布：`repost` 262、`reply` 233、`link_only` 30、`low_likes` 9、
`low_engagement` 8、`too_short` 5。**前两项占了 495/547 = 90%**。

**这就是"为什么只有 70 条"的答案，而且是可核对的答案**：这批账号在 6 天里发的东西
九成是对别人的回复和转发——回复没有自己的开头与收尾，转发不是他写的话，两类都不该进语料。
**不是阈值太严**：分组取回数（`slice_fetches`）显示窗口已经读完，把门槛放松只会让
"这条够不够格"这个判断本身变得不可信。

工作单第二节的"每账号 20～50 条、总计 100～500 条"**没有达到**（70 < 100），
实际每账号 0～18 条。**如实记录，没有凑数**：不放松阈值、不把回复和转发收进来、
不因为要凑数就多编几个账号。要往上走只有两条诚实的路——换一批**发原创短评更多**的账号，
或者把窗口拉长（而 `/2/tweets/search/recent` 上限就是 7 天，这条路基本到头了）。

---

## I. Style Recipe（工作单第十九～二十二节）

`StyleRecipe` 是一组**生成约束**：`structure_type` / `hook_type` /
`observed_target_length` / `hard_max_length` / `density` / `stance_strength` /
`evidence_requirement` / `tone` / `ending_type`
＋ `reasons: list[str]` ＋ `profile_ref`。

**它是约束，不是正文。** Writer 收到这个对象，**永远收不到样本原文**（工作单第二十二节）。

**为什么是档位而不是连续参数**：连续值（"目标长度 187.3 字"）既不可执行，又暗示了一种
并不存在的精度。所以长度给一个整数目标，密度/立场/语气/结尾给有限枚举；连续统计量留在
Profile 里供人核对。

### 长度的三段语义（工作单第六节，本阶段的最终产品决定）

长度**不能是一个数**。它同时被三件不同的事决定，而其中两件的来源完全不同：

| 名字 | 是什么 | 从哪来 | 本阶段的值 |
|---|---|---|---|
| `observed_target_length` | **语料**说"典型的一条多长" | Profile 的 `median_length` 收敛到配置区间 | **155** |
| `hard_max_length` | **平台**说"我们最多能发多长" | `persona.max_length`，由调用方递进来 | **140** |
| `effective_target_length` | 这次**实际**按哪个数写 | `min(前两者)`，一个**属性**，不是一个字段 | **140** |

三条纪律：

1. **`hard_max_length` 绝不写进 `observed_target_length`。** 语料从没说过该写 140 字。
   把两者压成一个字段，Run Detail 上就只剩一个 140，任何人都会把它读成
   "我们学到的写法就是写 140 字"——而真相是语料观察到 155 字，被平台容量削掉了 15 字。
2. **`effective_target_length` 是算出来的，不是存下来的。** 存一份就有第二个会漂移的
   来源；算出来则不可能与那两个数不一致。
3. **发生压缩时必须在 `reasons` 里说出来。** `build_recipe` 会追加一句
   "当前发布长度硬上限是 140 字，小于语料目标 155 字；本次实际执行目标取 140 字。
   **这是平台约束的结果，不是语料的结论**——语料观察到的是 155 字。"
   这句话在 Run Detail 与 Writer 的 prompt 里**都会出现**。

**这一层刻意不知道 `persona.max_length`。** `_choose_target_length` 只读 Profile 与配置区间，
上限是**从外面递进来**的一个事实。一个知道平台上限的长度推导，迟早会把上限写进
"语料说了什么"。

> **注意工作单举的例子是 180 → 140，而真实语料给出的是 155 → 140。**
> 关系（语料目标 > 平台上限）成立，但数字不同——180 是上一轮 `smoke@no-corpus` 那份
> **手工构造**的 recipe 里的值，不是语料统计的结果。这里按真实语料如实记 155。

**每个参数都要有依据**，写在 `reasons` 里，人能回原始统计核对，例如
"本次参考的是 42 条真实样本统计出的 Profile（中位长度 210 字）"。

**`profile_ref`**（形如 `corpus@2026-09-30T11:00:00+00:00`）让 Run Detail 事后能回到那一版
Profile。没有它，一个 recipe 就是一组无法复核的参数。

**为什么不让 Planner 选风格**（工作单第三十五节）：recipe 的每个参数都有确定性依据，
让模型"再选一次"等于把同一个判断做两遍，还给风格参数一个无法复核的来源。

**为什么不新增 AgentAction**（工作单第三十七节）：recipe 是 **Writer input preparation**——
在 `generate_candidates` 之前由确定性步骤备好。**没有新增任何动作，`max_steps` /
`max_research_calls` 一个都没动。**

---

## J. Writer 接入（工作单第二十一、二十二节）

- `app/agent/prompts.py::format_style_recipe(recipe | None)`：**永远返回一段文本**。
  `render()` 只替换传进去的键，**不会因为缺键报错**——调用方若因为"这次没有 recipe"
  就跳过这个参数，字面量 `{{STYLE}}` 会原样进入发给模型的 prompt。
- `prompts/agent/candidates.txt` 与 `revise.txt` 新增 `{{STYLE}}` 占位符。
  `revise` 也拿 recipe：改稿要收敛到同一个结构，否则改完就漂走了。
- `app/agent/state.py::RunState` 新增 `style_recipe`。`RunState` 是 `TypedDict, total=False`，
  LangGraph 会拒绝未声明键的 delta——**忘了声明这一步是唯一一处响亮失败**。
- 候选工具有**多条提前返回**（容量满/无新候选/模型没产出）走 `delta={}`，
  每一处都显式写 `style_recipe`，否则 Critic 那一轮可能没有 recipe 可判，而降级是静默的。
- `resolve_recipe` 支持传入 `existing`：一次运行里可能多次调用候选工具，
  **整个运行的结构必须一致**，否则两批候选的骨架不同，Critic 也就没有统一目标可比对。

### SERDE 登记（实测澄清，与初稿的说明不同）

`app/db/checkpointer.py` 的登记规则：

- **作为 state 顶层值**出现的类型要登记（`StyleRecipe`）。但**未登记的失败模式不是"少个字段"**：
  `_check_allowed` 返回 False 后返回的是**原始 payload**——顶层模型退化成 `dict`，
  **只在日志里留一条 warning，行为静默降级**。
- 嵌在已登记模型**类型化字段**里的**模型**会被 `model_dump()` 内联，不需要登记
  （`StyleFit` 因此不登记，登记了就是死条目）。
- **枚举不适用上一条**：`model_dump()` 不会把它们拍成字符串，它们照样以 EXT 形式出现。
  所以嵌套的枚举**要登记**（这也是 5B.1 登记 `TopicLifecycle` 的原因）。
  **不登记也能跑对**——被拦下的字符串会被 pydantic 在类型化字段上**再校验回枚举**，
  于是结果是对的、旧断言全绿、只有日志在响。这份"正确"完全寄生于那一步再校验：
  字段哪天改成 `Any`，同一段代码就会静默拿到字符串，而
  `recipe.structure_type is StructureType.CONTRAST` 这类比较会**恒为 False**。

所以本阶段做了三件事：登记 7 个枚举、改正注释里与实测不符的说法、
**补上断言类型而不是值的往返测试**（`test_the_recipe_read_back_from_the_checkpoint_is_still_an_object`）。
登记后 `Blocked deserialization` 警告从每次读 checkpoint 7 条降为 0 条。

---

## K. Critic 升级（工作单第二十三～二十五节）

`CriticResult` 新增 `structure_fit` 与嵌套的 `StyleFit`（`structure_match` / `hook_match` /
`density_match` / `length_match` / `tone_match`）。

**判的是"是否符合目标 StyleRecipe"，不是"像不像某个账号"**（工作单第二十三、三十二节）。
没有"这个人会不会这么写"这类判断。

**每个字段都 optional ＋ 有默认值**，嵌套的也一样：模型返回一个残缺的 `style_fit` 时，
任何必填字段都会让**整份 Critic 响应** validation 失败——一个分项缺失不该升级成整轮评审作废。

`style_fit` 是**结构化的五项**，不是一句 `style_score = 8.7`。这也正是当初它无从判起的原因：
Critic 手上没有可比对的目标，不是它不行。

---

## L. Evaluation（工作单第二十六、二十七节）

**离线 Style Benchmark `evals/style/`**（不碰真实 X、不碰 LLM、不需要数据库，
在 CI 里没有"今天红了明天绿了"的抖动）：

- 固定/合成的语料（8 条正文覆盖 7 种结构、7 种钩子）与 6 条应被排除的样本（含每条的**预期理由**）；
- 同一 corpus → Profile 逐字段相同；
- 同一 recipe → Writer prompt 片段完全相同；
- `{{STYLE}}` 确实被替换（不存在未替换的占位符）；
- 分类法稳定、recipe 是结构化的。

**`python -m evals.style.selftest` → 22 通过 / 0 失败。** 其中一条是**反证**：
`check_the_shared_span_detector_actually_fires` 先证明"共享片段检测器确实会报警"。
没有这条，检测器坏成永远返回空的时候，其余 21 条照样全绿——
而它守的正是工作单第四十节"大段原文不得进入 Writer prompt"这条绝对禁止。
检测器本身在 `evals/style/spans.py`，**真机 smoke 用的是同一份实现**
（那里换成 70 条真实样本，并且也带一条反证）。

另两条守的是长度的三段语义：`check_hard_max_is_separate_from_the_corpus_target`
（被压过时执行值等于上限、语料目标不被改写、且必须有解释）与
`check_the_recipe_never_exceeds_the_hard_max`（硬上限 40～1000 字八档下执行值都不越界，
语料目标始终不变）。

**Run 级规则**（`evals/behavior/rules.py`）：`RULE-019`「recipe 是约束，不是语料」——
state 里不得出现样本正文、`reasons` 不得为空、`profile_ref` 必须存在、
`observed_target_length` / `hard_max_length` 必须自洽、长度被平台压过时**必须在
`reasons` 里解释**、recipe 的任何字符串字段不得超过 300 字符。
它是 gate，已登记进 `GATE_RULE_IDS`，并在 `evals/selftest.py::check_every_gate_can_fail`
里补了能让它红的用例（**没有这一条，一条 gate 会被当成没写**）。

`evals/model.py::AgentRunState` 加了 `style_recipe`（字段名与 `RunState` 逐一对齐）。
**`python -m evals.selftest` → 12 通过 / 0 失败**（含新注册的检查）。

**人工内容质量评分一律不伪造**：`ContentQualityReport` 的八个维度保持 `unavailable`。

---

## M. 前端（工作单第三十九节）

Run Detail 新增一节**「本次运行采用的写作结构」**：结构 / 开头 / 目标长度 / 结尾。
技术细节折在已有的 `<details className="disclosure">` 里：其余约束、选择依据、
依据的语料统计（`profile_ref`）、正文的套话痕迹计数。

- `StyleSection` 与 `AgentQualitySection` **完全平行**：非 Agent 的 Run 返回 `null`，
  页面与从前逐像素一致。
- 后端按 `state.get("goal")` 门控（与 `quality` 同一处）；`AdminRunDetail` 加了 `style` 字段
  ——**两侧都要加**，它是 `response_model`，schema 里没有的字段会被**静默丢弃**。
- **取值和中文标签都给**：只给标签，界面没法核对原始取值；只给取值，前端得自己维护一份
  标签表。两者同源于 `app/style/labels.py`——这也是本阶段把标签表从 `prompts.py` 提出来的
  原因：两份标签表会漂，而漂的方向恰好是最难发现的那种（模型按"先给判断"写，
  界面显示"对比 A 与 B"，人拿着一个不存在的目标去评判这条稿子，**且不报错**）。
- 正文痕迹是**重算**出来的计数，措辞一直是"这类痕迹出现了几次"，
  **不是**评分、不是概率（工作单第四十节）。
- **不做** Style Analytics Dashboard，不做账号排名。

`cd frontend && npx tsc --noEmit` → 干净通过。

---

## N. Smoke 记录（工作单第三十八节）

`scripts/smoke_5b2.py`（真机 X 只读 + 真实 LLM + 共享 PostgreSQL）。
报告：`docs/phase-5b2-smoke-runs.json`。

**只读守卫**：`RealXApiClient._request` 被替换，非 GET **在发出去之前**抛。
**本次运行 3 次请求全部为 GET，POST = 0。**

### 为什么 Smoke 1–3 不跑整条 Graph

工作单要求"**同一** Evidence + Angle"。整条 Graph 每次都会重新检索、重新抽证据、重新选观点
——**三个变量一起动**，跑出来的差异归因不到 recipe 上。所以：先真跑一次完整 Agent，
把这一轮的 EvidencePack 与 SelectedAngle **冻住**，再用**同一个** freeze 出的输入
只换 `{{STYLE}}` 这一块。

### 结果（确定性指标，全部可复算）

这一轮跑在 **70 条真实样本**统计出的 Profile 上，**不是**任何手工构造的替代品。

| | Case A 不接 recipe | Case B 接真实 recipe | 换一个 recipe |
|---|---|---|---|
| recipe | （无结构指定） | contrast / statistic / **155→140** / medium / conclusion | contrast / statistic / 90 / high / **question** |
| 字数中位 | **70** | **129** | 118 |
| 句数中位 | 3 | 4 | 4 |
| 每百字数字 | **0.0** | **1.695** | 2.727 |
| 具体实体中位 | **2** | **8** | 8 |
| 问句总数 | 0 | 0 | **3** |
| 开头形态 | statement | **number**, statement | number, statement |
| 结尾形态 | statement | statement | **question** |

**读出来的结论**（§七 的 Case A/B 对照）：

- **长度**：70 → 129。没有 recipe 时 Writer 写 70 字就收工，远低于 140 的硬上限——
  也就是说**限制长度的不是平台，是没有结构约束**。接上 recipe 后落在 129，贴着 140 的上限。
- **信息密度**：每百字数字 0.0 → 1.695，具体实体 2 → 8。这是本阶段最想要的那条曲线：
  语料里每百字 1.67 个数字，接上 recipe 后写出来的正是 1.695。
- **结构 / 开头**：开头形态从清一色 `statement` 变成出现了 `number`（recipe 要求
  `statistic` 开头）。
- **换 recipe**：问句 0 → 3、结尾 statement → `question`、每百字数字 1.695 → 2.727
  ——alt recipe 的每一档都**真的落到了稿子上**，不是只改了 prompt。

> 顺带验证了本阶段修掉的一个真实缺陷：`opening_pattern` / `closing_pattern` 原先用
> `split_sentences`（它吞掉句末标点）再判 `endswith("?")`，于是 `question` 开头/结尾
> **永远不可能被识别**，`EndingType.QUESTION` 是一条死分支。修好后"换 recipe"这一支
> 立刻拿到了 `closings: ["question"]`。

### Smoke 3：同一 corpus → 同一 Profile

`identical: true`，**在 70 条真实样本上**，逐字段比对。这一条现在有了真机证据：
上一轮它是同义反复（语料为空，`sample_count = 0`），本轮不是。

### Smoke 5：recipe 不存在时整条链路仍跑完

**这一轮把 Profile 真的藏起来了**——临时把 `repositories.get_latest_corpus_profile`
换成"查不到"，让整条生产路径（候选工具 → `resolve_recipe` → 中性 recipe → Writer prompt）
走一次真实的无 Profile 分支。不这么做的话，库里既然有语料，跑出来的那一轮拿到的
是一份**真实** recipe，于是"没有 recipe 时也能跑完"这句话根本没被检验到——
而它看起来和检过了完全一样。

结果：`completed: true`，10 步跑完，`guardrail_passed: true`，
`recipe_was_neutral: true`，渲染出的 prompt 里**没有未替换的 `{{`**。
结构约束是增强，不是前置条件——语料没采够时 Agent 不会罢工。

### §九：交付候选与真实语料的 n-gram 重叠

9 条交付候选 × 70 条真实样本，12 字 n-gram 比对：**命中 0 条**。
同一段检测器（`evals/style/spans.py`，与离线基准**共用同一份实现**）再跑一次
**反证**——输入掺进一条真实样本原文——**会报警**（29 个共享片段）。
没有这条反证，"0 条"可能只是检测器永远返回空集。

报告里只记"哪一条候选撞上了哪个 `post_id`、撞了几个片段"，**绝不记片段本身**：
把命中的字写进报告，就等于把别人账号的原文抄进了仓库，而这条检查存在的全部理由
就是防止这件事。

### 历史：`smoke@no-corpus` 那一轮（2026-09-30 早些时候，**保留**）

**这一轮确实发生过，记录不删。** 当时 `config/style.yaml` 的 `accounts` 还是空的
（采样范围必须由人指定，而那之前没有拿到人工核对的账号清单），所以：

- 一条语料都没采，`sample_count = 0`；
- Smoke 2 用的是一份**显式构造**的 recipe，`profile_ref: "smoke@no-corpus"`，
  `profile_recipe_available: false`，报告里原样记着那句说明。它证明了"接 recipe 与
  不接 recipe 的稿子不一样"，**没有**证明"从真实语料推出来的 recipe 有用"；
- 那一轮的指标（字数中位 102 → 132、每百字数字 1.235 → 5.785）与本轮不可直接比较：
  证据包与观点是**另一次**真实运行的产物。

**它现在不能再作为验收证据**——工作单第五节明确排除了它。本轮的 Case A/B 用的是
真实语料推出来的 recipe。

---

## O. 验证命令与结果

```bash
PY=/c/Users/Too_Far/miniconda3/envs/2026-agent/python.exe

$PY -m pytest -q                 # 482 passed（必须串行，共享测试库）
$PY -m evals.selftest            # 12 通过 / 0 失败
$PY -m evals.style.selftest      # 22 通过 / 0 失败
$PY -m evals.run_benchmark --dataset   # 13 fixtures / 16 条基准目标，全过
$PY scripts/collect_style_corpus.py    # 真实采集：54 次 GET / 617 条取回 / 70 条纳入
$PY scripts/smoke_5b2.py         # 5 次 smoke；3 次 GET；POST = 0
cd frontend && node --test "tests/**/*.test.ts"   # 46 passed
cd frontend && npx tsc --noEmit  # 干净通过
cd frontend && npm run build     # 构建通过
```

新增单测 5 个文件，全部落在 `tests/`（不含 `evals/style/` 的 22 条检查）。

---

## P. 已知限制

1. **语料规模低于目标**（见 H 节）。70 条，工作单第二节的目标是 100～500。
   原因可核对（90% 的帖子是回复与转发，且窗口已经读完、0 个分段被截断），
   **没有靠放松阈值去凑数**。
2. **长度那个不一致已经**从"两个没被调和的约束"变成了**三段显式语义**（见 I 节）。
   原来长度目标与 `max_length` 同时进 prompt，模型自己挑；
   现在 recipe 分开记 `observed_target_length`（语料，155）/ `hard_max_length`
   （平台，140）/ `effective_target_length`（执行，140），**并在 prompt 与界面上
   明说压缩是平台造成的**。剩下这个仍**留给人的决定**：要不要放宽
   `config/persona.yaml` 的 `max_length` 去贴近语料观察到的 155——本阶段不做，
   因为那是产品决定，不是本阶段能替人拍的。
3. **分类法是待验证的假设**（`TAXONOMY_VERSION = 1.0`）。70 条真实样本里有
   **5 条（7.1%）落进 `unclassified`**——这个比例本身是分类法需要收敛的证据，
   而它被如实保留着，没有为了好看去改标注。
4. **`RunState` 里的 recipe 只活在 checkpoint 里**。checkpoint 被清理或 thread 丢失时，
   Run Detail 的这一块就没了。这与 `guardrail_passed` / `human_action` 现在的行为一致；
   若将来要 durable 的 style 评分，唯一的状态→DB 写入口是 `app/agent/nodes/finalize.py`。
5. **`abstract_noun_density` 仍是 `_approx`**：中文抽象名词没有可靠的确定性做法，
   保持标记而不是假装它精确。
6. **语料是累积的，但没有版本化的"冻结"机制**。`style_profile` 每次采集后重算并追加新行，
   运行期取 `computed_at` 最新的那一行。所以**两次运行之间语料可能已经变了**，
   只有 `profile_ref` 能事后指回当时用的是哪一版。要真正冻结，得引入显式的
   语料版本——本阶段没做。

---

## Q. 明确没有做什么（工作单第三、三十一、四十三节）

没有 fine-tuning / LoRA / embedding / 向量库 / pgvector / Redis / Kafka / Celery /
scheduler / multi-agent；没有 `StyleAgent` / `WriterAgent` / `CriticAgent`——
**仍然是 One Agent + Tools**；没有自动发布；没有 X 指标回流；没有账号排行榜；
没有账号自动发现；没有"高粉优先"；没有生成"像某某账号"的人设；
没有把样本原文塞进 Writer prompt；没有伪造人工内容质量评分；
没有假装 Style Score 是客观真值；没有 Style Analytics Dashboard。

**没有新增任何 AgentAction**，`max_steps` / `max_research_calls` 一个都没动。

---

## R. 本阶段修掉的真实缺陷

写代码过程中被测试与 smoke 逼出来的，全部**在修之前是静默的**：

| # | 缺陷 | 后果 | 发现方式 |
|---|---|---|---|
| 1 | `evidence_signals` 的数字事实探测对所有事实都返回 True | 证据要求档位恒为最高档，recipe 的分档失去意义 | 写单测时逐字段核对 |
| 2 | `opening_pattern` / `closing_pattern` 用 `split_sentences` 再判 `endswith("?")` | `question` 开头/结尾永远不可能被识别，`EndingType.QUESTION` 是死分支 | 单测覆盖五种开头/三种结尾 |
| 3 | `closing_pattern` 用 `endswith(marker)` 判号召性短语 | "评论区"这类短语几乎永不命中 | 同上 |
| 4 | `is_link_only` 把任何短帖都判成纯链接 | 短路理由变成一句误导人的话 | 5 条筛选单测同时红 |
| 5 | `hook_distribution` 用了 `StructureType.UNCLASSIFIED` | 跨枚举默认值，分布里出现错误类别 | 逐字段核对 |
| 6 | Run Detail 的正文痕迹只认 `final_content` | `final_content` 只在人工审核**之后**才有，于是**每一个待审 Run** 都会显示"没有交付正文"——而待审的 Run 恰恰最需要看这一块 | 新写的 Run Detail 断言 |
| 7 | 未登记的嵌套枚举被拦成字符串、再由 pydantic 校验回去 | 每次读 checkpoint 刷 7 条 warning；正确性寄生于再校验 | smoke 输出 |
| 8 | `--rebuild-profile` 被账号清单挡住 | "我只想重算一下 Profile"变成一句"accounts 是空的" | 手工跑该命令 |

**真实语料那几轮又逼出四条，同样是"修之前不报错"：**

| # | 缺陷 | 后果 | 发现方式 |
|---|---|---|---|
| 9 | X 的 `referenced_tweets` 混装 `retweeted`/`quoted`/`replied_to`，代码只看"非空" | 一条**回复**带着 `repost` 这个理由被剔掉——理由错了，"为什么语料这么少"的答案也就跟着错。147 条被误标 | 第一轮采集的理由分布 |
| 10 | `user.fields` 要了 `description`/`name`，但映射层把它们丢了 | 9 个账号的 `display_name`/`description` 全空，而 X 其实已经返回了 | 第一轮采集后查库 |
| 11 | `store_samples` 从不写 `published_at`，`CandidateSample` 也没有这个字段 | 45/45 条样本的 `published_at` 为空——**原帖什么时候发的**这个事实事后补不回来 | 第一轮采集后查库 |
| 12 | `backfill_account` 改的是**游离（detached）对象**的属性再 `commit()` | 新会话对那个对象一无所知，`commit()` 什么也不写，而函数返回 `True`。表现与 #10 相同，但修了 #10 也不会好 | 修完 #10 后重跑，仍然全空 |
| 13 | 一次请求最多 100 条**且不翻页**，只问一次 | 报告上"窗口 6 天 / 取回 100 条"实际只覆盖窗口最新的那一页，读起来像"这个账号 6 天只发了 100 条" | 真实采集的分组取回数 |

第 11 条尤其值得单独说：`app/models/style_sample.py` 的注释早就写着
"`published_at` / `observed_at` 两套时间语义，**不发明第三套**"，
但**没有任何东西会因为你没填它而报错**——列是可空的，插入照常成功。
一个只在数据里缺、不在代码里错的字段，是这类采集链路最典型的漏法。
现在 `store_samples` 写它，`CandidateSample` 带着它一路到落库那一步，
并且采集后的统计会把 `published_at 缺失数` 直接印出来。

---

## S. 边界与持久性

- **`evals → app` 是仓库里唯一的允许方向**，本阶段没有反向。`evals/model.py` 与
  `evals/content/ai_style.py` 已有先例，`app/style/features.py` 的上移正是为了守住它。
- **StyleRecipe 进 checkpoint，不进数据库**。它是运行期决策。
- **样本、账号、Profile 进数据库，不进 checkpoint**。它们是长期语料资产。
- **采集与 Analyze 是 offline 的**：不在 Agent 运行期调用，不占预算，
  `max_steps` / `max_research_calls` 不变。
- Run Detail 的 style 块是 checkpoint-only，与 `guardrail_*` / `human_action` 一致。

---

## T. 结论

Style Intelligence 决定的是**"怎么表达"**，不是**"说谁的话"**：

- Writer 收到的是**约束**（该写成什么样），不是范本（照着抄谁的句子）；
- Critic 判的是**是否符合目标 recipe**，不是"像不像某个账号"；
- 界面上写的是**这次采用了什么结构**，不是"我们在模仿谁"；
- 语料里存的是**统计与结构标签**，从不进入 prompt 的正文。

"大段原文不得进入 Writer prompt"这条绝对禁止，由 `evals/style/spans.py` 的 n-gram
比对**机器守着**——离线基准（8 条合成样本）与真机 smoke（70 条真实样本）**共用同一份
检测器实现**，两边都带一条反证确认检测器本身没坏。

**这一轮把 Profile 从"合成样本上的假设"变成了"真实语料上的统计"**：
70 条真实中文科技帖 → `median_length` 155 / 3 段 / 0 emoji / 每百字 1.67 个数字，
→ recipe（`fact_first` 31% 是最常见结构，`statement` 开头 67%）
→ Writer 的长度从 70 字变成 129 字、每百字数字从 0.0 变成 1.695。
**这条链路第一次从头到尾跑在真东西上。**

**下一步取决于人的决定**：

1. 要不要让 corpus 变大——换一批**发原创短评更多**的账号，或者接受 70 条这个规模
   （H 节说明了它为什么只有 70 条）；
2. 要不要放宽 `config/persona.yaml` 的 `max_length: 140` 去贴近语料观察到的 155
   （P 节第 2 条）——这是产品决定，本阶段只把它**说清楚**，没有替人拍。

**本阶段到此为止。** 不进入 Phase 5B.3、Feedback Loop、X engagement ingestion、
Fine-tuning、Vector DB、Multi-agent 或 Autonomous Publish（工作单第四十三节）。
