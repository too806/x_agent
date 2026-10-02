# Phase 5D — Learning Proposal Foundation

**范围**：在 Learning Readiness 之后新增**第六层**，回答且只回答一句：

```text
反馈已经够可靠了的话，未来可以尝试什么变化？
```

**基线**：开工时 `master` HEAD `540c793`（Phase 5C COMPLETE，backend 694 通过 /
frontend 93 通过）。

**封版 HEAD**：**本文件所在的 `master` 提交**（`git log --oneline -1` 即得）。
此处刻意不写字面哈希——文档每改一次哈希就变，写死必然过期。

**状态**：**Complete**。五条 smoke 全部跑通（`docs/phase-5d-smoke-runs.json`），
真机数据给出 `learning_ready=false` / `proposal_count=0`，离线充分数据给出可裁决提案。

**结论**：完成，而且**只是"提出建议"**。这一层不写正文、不改 prompt、不动 style /
viewpoint / trend 的任何参数、不动预算与阈值、不自动发布。它唯一的功能是把
"要不要换个写法"从一个印象变成一份**带证据、带样本量、带边界、等人裁决**的书面建议：

```text
Trend              = 当前外部世界在发生什么
Style              = 外部语料是怎么写的
Viewpoint          = 在现有证据下一个判断站不站得住
Feedback           = 我们自己过去的行为，实际带来了什么可观察的结果
Learning Readiness = Feedback 是否已经足够可靠，可以进入未来学习
Learning Proposal  = 基于可靠反馈，未来可以尝试什么        ← 本阶段新增，且只到这里
```

流水线是四段，本阶段实现前三段，**第四段只留出入口**：

```text
Learning Evidence → Learning Proposal → Human Review → Approved / Rejected
      ↑ 5C 已有            ↑ 本阶段          ↑ 本阶段        ↑ 本阶段只记录，不执行
```

---

## A. 交付概览

**新增**

```text
app/learning/proposal.py                        提案生成与判据（纯函数，零 IO）
app/models/learning_proposal.py                 learning_proposal 表（只存裁决快照）
alembic/versions/f3b8c1a95d27_phase_5d_learning_proposal.py
                                                手写迁移（一张新表，不动任何已有表）
evals/learning/proposals.py                     §26 的八个 case + 七项自检
scripts/smoke_5d.py                             §36 的五条 smoke（GET-only）
tests/test_learning_proposal.py                 纯层单测（49 条）
tests/test_learning_proposal_api.py             写端点 / 表结构集成测试（16 条）
frontend/src/utils/proposals.ts                 措辞纯函数
frontend/tests/proposals.test.ts                前端措辞测试（24 条）
docs/phase-5d-smoke-runs.json                   smoke 记录
docs/phase-5d-learning-proposals.md             本文件
```

**改动（全部是追加式，不改既有语义）**

```text
app/learning/schema.py       追加 ProposalStatus / ProposalTarget / ProposalValidation /
                             LearningProposal；LearningReport 追加 proposals 字段
app/learning/__init__.py     包说明扩到 5D：Observation/Proposal/Recommendation 三分、
                             允许与禁止的目标、以及"包内没有 apply_learning()"
app/style/recipe.py          追加 recipe_version()（blake2b 指纹，最小版本标识）
app/models/__init__.py       导出 LearningProposalDecision
app/repositories.py          四个裁决读写函数（insert / get / list / count）
app/schemas.py               LearningProposalRow / ProposalsOverview / ProposalReviewRequest /
                             ProposalValidationRow …（对外模型）
app/services/admin.py        AdminService.learning() 加 proposal_count；
                             新增 learning_proposals() 与 review_learning_proposal()
app/api/admin.py             GET /api/admin/learning/proposals（只读）
                             POST /api/admin/learning/proposals/{id}/review（唯一写端点）
app/agent/prompts.py         format_proposal_preview()（纯函数；**不接入 Graph**）
evals/learning/selftest.py   追加 4 项检查（写扫描 / 可追溯 / 阈值复用 / 无应用路径）
tests/support.py             测试夹具补 learning_proposal 建表
tests/test_admin_api.py      断言 overview 里 proposal_count 的取值与语义
frontend/src/types/api.ts    提案层类型
frontend/src/api/client.ts   getLearningProposals() / reviewLearningProposal()
frontend/src/pages/Analytics.tsx             ProposalBlock（最小展示 + 裁决）
frontend/src/components/RunDetailDrawer.tsx  一行提案条数（来自同一个响应）
```

**没有的**（这是本阶段最重要的一张清单）：没有 `apply_learning()`、没有自动修改
Prompt / StyleRecipe / Trend 权重 / Viewpoint 权重 / Critic 阈值 / 预算的代码路径、
没有调度器、没有 A/B 框架、没有微调、没有向量库、没有多智能体、没有自动发布。
**也没有第二张表、没有给任何已有表加列。**

---

## B. 只读审计结论（写代码之前先指认问题，§二）

工单要求在写代码之前先回答六个问题。以下是当时的结论，后来逐条被实现或证伪：

**1. LearningGate 的输入是什么？**
四个确定性条件，全部来自 `LearningReport`，没有一个来自模型：
`sample_count >= MIN_SAMPLES_FOR_COMPARISON`、`window_consistent`（同一窗口）、
`metrics_present`（相关指标有读数）、`comparison_valid`（至少一条比较成立）。
结论：**闸门的输入已经足够，不需要新增任何"提案专用"的可靠性判据**。

**2. 什么算 LearningEvidence？**
五种封闭类型：`cohort_difference` / `insufficient_sample` / `missing_metric` /
`window_mismatch` / `invalid_comparison`。其中**只有第一种能支撑提案**——后四种
说的是"什么也没说"，让它们生成提案等于把"数据不够"包装成一条建议。

**3. 理论上可被调整的生产规则有哪些？**
StyleRecipe 的九个决策字段（结构 / 开头 / 长度 / 密度 / 判断强度 / 证据要求 /
语气 / 结尾 / 长度上限）+ Viewpoint 的 stance 与 counterpoint 强调。工单 §6 圈定的
五个允许目标正是这个集合里**有观测维度支撑**的那一部分，见 E 节。

**4. 哪些数据不足以支撑调整？**
真实库当前 1 条已发布帖子 / 2 次快照，全部落在 `0-1h`。六个 cohort 全部
`sample_count < 5`，零条有效比较。**任何目标都不足以支撑调整**——这正是本阶段
第一条正确的结论，不是失败（见 M 节）。

**5. 为什么 `format_learning_context()` 没有调用点？**
因为 5C 刻意不接入 Graph（§25）。当时的问题是"接口该不该先建"，结论是建、但只用于
preview / 人工审核 / 离线评测。**本阶段维持这个结论**：新加的
`format_proposal_preview()` 同样**没有任何调用点**（`grep` 全仓只有定义），
`{{LEARNING}}` / `{{PROPOSAL}}` 占位符都不存在。

**6. checkpoint 与持久化的边界在哪？**
checkpoint 绑定一次 Run；提案的裁决看的是**一批**已发布帖子在某个窗口下的表现，
跨许多次 Run，而且是一条比任何一次运行都活得久的**审计记录**。结论：裁决**必须**
进数据库；而提案本身**不进**——它随时可以从证据重算（见 I 节）。

---

## C. 闸门是硬闸门（§七）

```python
if not report.readiness.learning_ready:
    return ()          # 一个提案都不生成，不是"生成了再过滤"
```

三条刻意的性质：

1. **闸门在生成之前**，不在生成之后。`generate_proposals()` 的第一行就是它，
   因此不存在"过滤前有 N 条"这个中间状态——没有一个地方能拿到闸门关着时的提案。
2. **不是排序，不是降权**。理由写在服务层的文案里：
   > *The readiness gate is a hard requirement rather than a ranking: insufficient
   > data yields no suggestion at all instead of a few plausible-looking ones.*
3. **LLM 绕不过去**。整个 `app/learning/proposal.py` 没有一次模型调用，
   `learning_ready` / `sample_count` / 比较有效性全部由 Python 判定（§二十一）。
   LLM 在本层唯一能碰的是**措辞**（`_rationale` 的模板），而它连模板都不是运行时生成的。

自检里有一条专门证明闸门**是承重的**而不是碰巧相关：`check_the_gate_is_load_bearing`
拿一份**本来能产出提案**的报告，只把 `learning_ready` 强改为 `False`，断言提案归零。
如果闸门只是"数据不够时恰好也没提案"，这条会失败。

---

## D. 从证据到提案（§八 / §九）

一个目标（不是一条证据）对应一条提案。理由是同一维度的多条证据讲的是**同一件事**：
点赞、对话、转发三个指标上"question 更高"是同一句观察被读了三次，不是三个独立选项。

**必须一致才提案（两条真实契约）**

```python
directions = {_direction(c) for _, c in usable}
if None in directions or len(directions) != 1:
    return None          # 指标之间不一致 → 不提案
```

- 任何一个指标**读不出方向** → 不提案；
- 指标之间**方向互相矛盾**（点赞说 A 高、转发说 B 高）→ 不提案。

这是一条**保守**的选择，代价是召回率：三条指标里两条同意也不算数。理由是这一层
产出的东西会被人当成"可以试试"，而"两条说 A、一条说 B"恰恰是最容易被读成
"A 更好"的形状。

**已经生效就不说话**

```python
if has_baseline and baseline == observed_higher:
    return None          # 配方已经在观察更高的一侧了
```

"建议把开头方式改成 question"，而配方当前取的就是 question，不是一条建议。

**证据链完整，且不能自说自话（§八）**

- `evidence_ids` 逐条列出**本次比较实际用到的**证据 id，不是"这一维度上所有证据"；
- `sample_count` 是这些证据涉及帖子的**并集**（`set.update`，不是相加）——
  三条指标看的是同一批帖子，相加就是把同一句话数了三遍；
- 没有 `evidence_ids` 的提案在判据里直接出局（`has_evidence`）；
- 生成后用 `validate_proposal()` **再核验一遍**，任何一项不通过就返回 `None`。
  所以"invalid ⇒ 没有提案"（§二十六）是**结构上**成立的，不是靠人在出口处过滤。

**不读原始正文（§九）**
`generate_proposals()` 的入参只有 `LearningReport` 与一个 `StyleRecipe`。它看不到
帖子正文，拿不到 `FeedbackReport` 的原始文本，也**没有** `session` ——
`tests/test_learning_proposal.py` 直接扫模块源码断言里面不出现 `session` /
`insert` / `commit` / `sqlalchemy`。

---

## E. 允许的目标 vs 禁止的目标（§六）

**允许（v1，五个，封闭枚举）**

```text
style_recipe.hook_type                开头方式
style_recipe.structure_type           写作结构
style_recipe.target_length            正文字数档
viewpoint.stance                      判断强度
viewpoint.counterpoint_awareness      是否带反方视角
```

**禁止（安全或生产边界，一律拒绝）**

拒绝方式有**两道**，任一道命中就出局：

1. `target_allowed`：目标必须在上面的封闭枚举里。表外的目标连同"允许的目标只有
   这五个"一起写进 `reasons`——不给"再试一次"的余地；
2. `not_security_related`：目标字符串里出现下面任一标记即拒绝：

```python
FORBIDDEN_TARGET_MARKERS = ('trend', 'guardrail', 'publish', 'budget', 'critic',
                            'security', 'safety', 'review', 'cas', 'threshold',
                            'persona', 'prompt')
```

第二道是**冗余**的：枚举已经挡得住。留着它是因为它挡的是另一种错误——将来有人
往枚举里加了一个名字里带 `prompt` 的目标，第一道会放行，第二道不会。
`evals/learning/proposals.py` 里 `security_target` 这个 case 断言的是
**五条禁止目标各出两条原因**（`security_flagged_count == 5`），而不是"原因里出现了
某个词"——后者会被一句无心的文案改动弄成永真。

**发布 / Guardrail / 预算 / CAS / 人工审核完全独立（§十六）**
提案层不 import 它们中的任何一个，`validate_proposal` 也读不到它们。
最直接的证据是 `check_there_is_no_apply_path`：AST 扫描 `app/learning/*.py`，
断言没有任何名为 `apply_*` / `execute_*` / `activate_*` / `deploy_*` / `enforce_*`
的顶层函数。

---

## F. 判据：11 项全过才算一条提案（§二十六）

```python
VALIDATION_CHECKS = ('has_evidence', 'evidence_is_a_comparison', 'sample_sufficient',
                     'window_consistent', 'target_allowed', 'not_security_related',
                     'not_causal', 'not_absolute', 'not_a_recommendation',
                     'confidence_absent', 'automatic_application_false')
```

后三项值得单独说：

- **`confidence_absent`**：`confidence` 必须恒为 `None`（§十七）。这里**没有**
  `proposal_score`、没有优先级、没有排名，也不会有——一个未校准的概率是这一层
  最容易被误读的数字；
- **`automatic_application_false`**：`automatic_application` 是提案上的一个
  **字段**，恒 `False`，而且是判据的一部分。写成约定的话，某个分支忘了设就会静默
  变成 `True`；写成字段，`True` 的提案**根本生成不出来**；
- 三项措辞检查见下一节。

**样本量沿用同一个常量（§十八）**

```python
def generate_proposals(report, *, min_samples: int = MIN_SAMPLES_FOR_COMPARISON, ...)
```

`MIN_SAMPLES_FOR_COMPARISON == 5` 是 5C 的常量，本阶段**import 复用**，不重定义、
不下调。自检 `check_the_proposal_layer_reuses_the_same_threshold` 断言的是
**对象同一性**（`proposal_module.MIN_SAMPLES_FOR_COMPARISON is MIN_SAMPLES_FOR_COMPARISON`），
不是相等——两个各自写着 `5` 的常量迟早会分叉。

**离群值不删除（§二十）**
`outlier_count` 累加两个组各自的离群标记数并原样上报，**不排除**任何一条读数。
5C 已经定下 IQR 栅栏只标记不删除，这里维持：把一个极值踢掉，等于让建议看起来
比数据支持的更稳。

---

## G. 因果措辞守卫（§二十四）

提案的 `rationale` 会被三组标记扫一遍，命中任意一个即判据不通过：

```text
因果    因为 / 导致 / 证明 / 说明是 …        （find_instruction_language 之前）
绝对    一定 / 必然 / 必定 / 保证 / 确保 / 肯定 / guarantee(d) / definitely / certainly
指令    应该 …                              （find_instruction_language）
```

允许的措辞是"在 24-72h 观察窗口下、在开头方式分组中观察到 question 一侧更高"、
"值得尝试"；禁止的是"证明 / 导致 / 一定 / 保证"。**"更好"这个词也不出现**——
比较结论只能说"更高"，不能替读者把它翻译成"更好"。

`check_the_valid_template_is_actually_valid` 是这组检查的反证：那三个负面 case
（因果 / 绝对 / 指令）复用的是同一个模板，而这个模板自身必须**通过全部 11 项**。
否则"负面 case 被拒"可能只是因为模板本来就烂。

---

## H. 版本标识：`recipe_version()`（§十三）

工单要求记录 `current_recipe_version` / `proposed_recipe_version`，但**不允许**切换，
也不允许引入复杂配置中心。做法是一个**指纹**：

```python
RECIPE_VERSION_PREFIX = "sr1"        # → sr1-e1b5fb3d2060
```

- 定义只有一句：**同一组决策字段 → 同一个版本号；任何一个字段变了 → 另一个版本号**；
- `blake2b(digest_size=6)`，与库、机器、运行次数无关，因此可以进数据库、进提案、进报告；
- 参与指纹的九个字段顺序**写死**（顺序变了指纹就变，所以它是一份契约）；
- 刻意**不含** `reasons` 与 `profile_ref`：那两样是**依据**，不是决策。同一组参数
  配不同的解释说明，仍然是同一版配方；
- **没有版本表、没有版本中心、没有回滚机制**。版本号是算出来的，不是存出来的。

`None` 与"有一版中性配方"是两件事：拿不到基准配方时两个版本号**同时为 `None`**，
界面因此能分辨"当前取值是事实"和"当前取值只是这次比较里较低的那一侧"（见 K 节）。

`target_length` 是唯一一个**不给版本号**的目标：长度档（short/medium/long）与
StyleRecipe 的整数长度上限之间没有可公认的确定换算，因此那条提案不带版本号，
并把这个缺口写进 `limitations`。

---

## I. 数据库：只存裁决，不存提案（§三十三 / §三十四）

**表** `learning_proposal`（迁移 `f3b8c1a95d27`，**手写**）：

```text
id                            uuid 主键（沿用仓库的 UUIDPrimaryKeyMixin）
proposal_key                  确定性 key，唯一：proposal:<dimension>@<window>
                              （如 proposal:hook_type@24-72h）
target / current_value / proposed_value / rationale / limitations
evidence_ids / sample_count / observation_window
status / reviewed_at / note
current_recipe_version / proposed_recipe_version
created_at                    （沿用仓库的 CreatedAtMixin）
```

工单第三十三节点名的五样——`proposal` / `evidence_ids` / `status` / `created_at` /
`reviewed_at`——都在。

**与最初的接口草案有一处刻意的差异**：草案写的是"`id` 就是确定性 key"，实现拆成了
`id`（uuid 主键，与仓库其余表一致）+ `proposal_key`（确定性 key，带**具名**唯一约束
`uq_learning_proposal_proposal_key`）。原因是 `ON CONFLICT DO NOTHING` 必须按名字指定
冲突目标，而把 uuid 主键拿去当确定性 key 会让"这条提案的身份"与"这一行的身份"
混成一件事——前者应当在重新计算后可复现，后者不该。

`created_at` 与 `reviewed_at` 是**两个不同的事实**，所以两个都留着：前者是"这一行
什么时候建的"，后者是"这个决定是什么时候做的"。通常只差几毫秒，但把它们合成一个
字段之后，"补录一条三天前的裁决"就再也说不清楚了。

**这张表存的不是提案，是人对提案的裁决。** 这是本阶段最容易搞反的一点：

```text
GET  /api/admin/learning/proposals       现算提案（每次都是新的），叠加库里的裁决
POST .../{id}/review                     写一行裁决。这是控制台里**唯一**的写操作
```

推论有三条，每条都被测试钉住：

1. **没人裁决过的提案不在库里**，但一定在 `GET` 的结果里。库里查不到一条提案，
   不等于系统没想过它；
2. **`status` 只有 `approved` / `rejected` 会落库**。`proposed` 是"还没人做过决定"，
   把"没有决定"写成一行记录，会让"库里有多少条"变成一个没有意义的数；
3. `expired` 是**读的时候**算的：裁决当时批准过，但配方版本已经往前走过了。
   它不落库——过期是一个随时间变化的判断，存下来必然要有人去更新它。

**只允许追加，不允许改写**：同一个 `id` 第二次裁决返回 `409`（唯一约束 + `ON CONFLICT
DO NOTHING`，约束名写死）。审计记录可以被覆盖的话，它就不是审计记录。

**不存什么（§三十四）**：没有 full prompt、没有 LLM secret、没有 raw completion、
没有模型名、没有 `proposal_score`。表里也没有外键——裁决不是任何一个 Run / 帖子 /
提案的子对象。`tests/test_learning_proposal_api.py` 用一份**封闭的列清单**
断言这张表**没有** `applied_at` / `applied_by` / `prompt` / `completion` / `score`。

---

## J. 人工裁决（§十一 / §三十二）

```text
proposed   还没人决定
approved   人批准了 —— **不等于已应用**
rejected   人否决了
expired    曾经批准，但配方已经往前走过了
```

- 批准与否决都是**记录一个决定**。后端没有任何代码路径读取 `approved` 去做别的事；
  服务层的方法名是 `review_learning_proposal`，做的事只有一件：写一行；
- **否决必须写明理由**（`note` 非空），空白或纯空格返回 `422`。批准可以不带理由；
- 裁决后返回**最新的一行**，不是回显请求；
- 前端按钮文案本身就写着"批准（只记录，不应用）"，批准后的回执必须明说
  "本系统现在没有因此改变任何设置"（`reviewOutcomeLine`），并有前端测试断言这一点。

**5D 只预留入口，不实现应用（§三十五）**：`approved` 只是下一阶段的输入数据，
本阶段没有任何代码消费它。

---

## K. 只读面（§三十一）

**后端**

```text
GET  /api/admin/learning                        已有，追加 proposal_count（现算）
GET  /api/admin/learning/proposals              新增，只读，现算 + 叠加裁决
POST /api/admin/learning/proposals/{id}/review  新增，唯一写端点
```

`GET /api/admin/learning` 如今也带 `proposal_count`，与 proposals 接口**同源同窗**：
两处各算一遍迟早会给出两个数。

**前端（最小）**

- `Analytics.tsx` 新增 `ProposalBlock`，紧跟在"学习准备度"之后、**单独成块**：
  上面那块答"够不够可靠"，这块答"可靠的话可以试什么"。合成一块会让"数据不够所以
  零条建议"读起来像这一层坏了；
- 每条提案按 §31 给全七样：**目标 / 当前值 / 建议值 / 依据 / 样本 / 限制 / 状态**。
  少任何一样都会让建议开始像命令——没有样本量就无法判断"样本太小"，没有限制就读起来像规则；
- `RunDetailDrawer` 加一行提案条数。它**不另发请求**：`proposal_count` 就在
  `GET /api/admin/learning` 的响应里，而抽屉每打开一次都要拉一次准备度，
  再加一次同样重的比较读取就是拿最重的读去挡最常走的路；
- **没有 Learning Dashboard**（§三十一）。没有独立页面、没有图表、没有趋势线。

三种"零条"在界面上长得**不一样**，这是前端措辞测试的主要对象：

```text
没有数据        → 后端给的原因（"还没有任何已发布内容可供观察"）
闸门关着零条    → "数据还不足以提出建议（窗口 X）。零条建议是一个结论，不是一次失败。"
闸门开着零条    → "在当前样本中没有值得尝试的方向（窗口 X）。"
```

把后两种渲染成"没有发现可以改进的地方"，就是把"数据不够"说成"看过了，没问题"。

---

## L. 离线评测（§二十六）

`evals/learning/proposals.py` —— 八个 case（固定顺序）+ 七项自检。
负向 case **复用同一个正向模板**，模板本身必须通过全部 11 项判据：

```text
valid_learning_evidence   正向，必须产出提案
insufficient_sample       n < 5        → 零条
missing_metric            指标缺失     → 零条
mixed_window              窗口混用     → 零条
causal_evidence           因果措辞     → 零条
invalid_target            表外目标     → 零条
security_target           安全边界目标 → 零条（五条各出两条原因）
proposal_without_evidence 无证据       → 零条
```

七项自检（`python -m evals.learning.proposals`）：

```text
check_the_eight_categories_are_all_present      八个 case 一个不少，顺序固定
check_every_case_matches_its_expectations       逐 field 断言（不是"看起来对"）
check_the_valid_template_is_actually_valid      正向模板必须真的过 11 项
check_invalid_never_comes_out_of_generation     invalid ⇒ 提案数为 0
check_the_gate_is_load_bearing                  强关闸门 ⇒ 提案归零（证明闸门承重）
check_already_in_effect_produces_nothing        配方已在更高侧 ⇒ 沉默
check_there_is_no_apply_path                    AST 扫 app/learning/*.py，无 apply_* 等
```

设计上的一致纪律：断言指向**违规的具体数据必须失败**，每个检查配一个反证；
字段与关系用**封闭表**列出来，这样拼错字段名会响亮地失败而不是静默通过。

`evals/learning/selftest.py` 另加 4 项（共 20 项），其中
`check_the_learning_package_never_writes` 会**自己种一个 `session.add()` + `commit()`
进去**，断言扫描器确实报出 2 处违规，再对 `x.update([1])`（一个集合操作）断言报 0 处
——证明这个扫描器不是永真也不是过敏。

---

## M. 真实数据结果（§二十八）

真机库当前状态（smoke 前后一致，本阶段没有写入任何一行）：

```text
已发布帖子           1 条
观察快照             2 次（发布后 0.0019h / 0.0288h，都落在 0-1h）
窗口                 0-1h
learning_ready       false
reason               insufficient_sample
sample_count         1
cohort_count         6      （六个分组都 < 5）
valid_comparison_count 0
proposal_count       0
```

**这是预期结果，不是失败。** n=1 时任何分组都不成立，因此没有任何比较结论，
因此没有提案。要注意的是"零条"在这里是**闸门关着**给出的结论，
和"看过了、没问题"不是一回事。

另外验证了：闸门关着时 `POST .../{id}/review` 对任何 id 都返回 `404`
（`proposal 'proposal:hook_type@24-72h' is not produced by the current observation
and cannot be reviewed`）——**写路径本身也受闸门约束**，不是只有读路径。

---

## N. 五条 smoke（§三十六）

`python scripts/smoke_5d.py`，全部通过：

```text
1 真实数据闸门关闭   1 帖 / 2 快照 → learning_ready=false, proposal_count=0（两条路径一致）
                     且 review 一个不存在的提案 id → 404
2 离线足够数据闸门开 10 帖（5+5）→ learning_ready=true, 3 条有效比较
3 合法证据产出提案   → 1 条，全部判据通过，automatic_application=false
4 因果措辞被拒       → 因果 / 绝对 / 指令三组标记各自命中并给出原因
5 生产目标被拒       → guardrail / publish / budget 等目标各出两条原因
```

`smoke_1_real_data_gate_closed` 同时走 `generate_proposals()` 与
`AdminService.learning()/learning_proposals()` 两条路径，断言两边给出**同一个**
`proposal_count`。

---

## O. X GET / X POST（§二十九）

```text
本次运行 X 请求总数   1
按方法                GET: 1, POST: 0
POST /2/tweets        0
```

脚本沿用 `smoke_5c.py` 的守卫：包装 `RealXApiClient._request`，在**发出之前**就
拒绝任何非 GET，并记录每一次请求。唯一那次 GET 是 `GET /2/tweets?ids=<x_post_id>`
（HTTP 200，返回 1 条），用于证明只读链路仍通。**没有发布任何测试帖**，
也没有为了让提案出现而伪造数据（§二十八 / §三十）。

---

## P. 逐条回答工作单第三十八节的 18 个问题

**1. Schema**
`app/learning/schema.py` 追加 `ProposalStatus`（4 值）、`ProposalTarget`（5 值）、
`ProposalValidation`、`LearningProposal`；`LearningReport` 追加 `proposals`。
对外的 `app/schemas.py` 追加 `LearningProposalRow` / `ProposalValidationRow` /
`ProposalsOverview` / `ProposalReviewRequest`。见 I 节。

**2. Evidence → Proposal**
一个**目标**一条提案（不是一条证据一条）；只接受 `cohort_difference` 类证据；
证据方向必须**跨指标一致**；`sample_count` 是帖子**并集**；生成后再过一遍判据。
见 D 节。

**3. Gate**
硬闸门，在生成之前，`learning_ready=False ⇒ 提案数为 0`，LLM 无法绕过。
自检里有"闸门承重"的反证。见 C 节。

**4. 允许的目标**
`style_recipe.hook_type` / `structure_type` / `target_length`、`viewpoint.stance` /
`counterpoint_awareness`，五个封闭枚举。见 E 节。

**5. 禁止的目标**
Trend 权重 / Guardrail / Publish / Budget / Critic 阈值 / 安全 / 人工审核 / CAS / Persona /
Prompt —— 两道拒绝（枚举 + 标记），且提案层不 import 它们中的任何一个。见 E 节。

**6. 人工审核**
`POST /api/admin/learning/proposals/{id}/review`，批准 / 否决；否决必须给理由；
不可改写（409）。见 J 节。

**7. Validation**
11 项 `VALIDATION_CHECKS` 全过才是一条提案；任何一项不过，生成阶段就返回 `None`。
见 F 节。

**8. 因果守卫**
三组标记（因果 / 绝对 / 指令）扫描 `rationale`；不出现"更好"；`confidence` 恒 `None`。
见 G 节。

**9. Versioning**
`recipe_version()` = 九个决策字段的 blake2b 指纹 → `sr1-<12 hex>`；只记录、不切换；
没有版本表、没有配置中心。见 H 节。

**10. 数据库**
一张新表 `learning_proposal`，**只存裁决快照**（提案本身不落库）；手写迁移；
不带外键。见 I 节。

**11. Evaluation**
`evals/learning/proposals.py` 七项自检全通过；`evals/learning/selftest.py` 从 16 项
扩到 20 项全通过。见 L 节。

**12. Offline benchmark**
§26 的八个 case 一个不少且顺序固定；负向 case 复用同一个必须自己合法的模板；
断言指向具体字段而非"看起来对"。见 L 节。

**13. 真实数据结果**
1 帖 / 2 快照 → `learning_ready=false`、`insufficient_sample`、`proposal_count=0`、
写路径 404。见 M 节。

**14. 五条 smoke**
全通过，见 N 节与 `docs/phase-5d-smoke-runs.json`。

**15. X GET / POST**
GET 1 次（HTTP 200），**POST /2/tweets = 0**。见 O 节。

**16. 测试**
backend `759 passed`（694 基线 + 49 + 16）；frontend `117 passed` + `tsc --noEmit`
退出码 0。见 Q 节。

**17. 已知限制**
见 Q 节末尾与每条提案自带的 `limitations`（包括"只做了观察性比较，没有对照组"、
"当前取值未知时它不代表生产配置"、"长度档没有可换算的配方字段"等）。

**18. 明确声明没有自动应用**
**本阶段没有自动应用任何提案。** 没有任何代码路径消费 `approved`；
`automatic_application` 与 `automatic_modification` 在报告、提案、前端三层都是恒 `False`
的**字段**，并被测试钉住；`app/learning/` 内不存在任何 `apply_*` / `execute_*` 函数
（AST 扫描断言）。见 R 节。

---

## Q. 验证命令与结果（全部串行）

```text
python -m pytest -q                       759 passed
python -m evals.learning.selftest         20/20
python -m evals.learning.proposals        7/7
python scripts/smoke_5d.py                5/5（X：GET 1 / POST 0；库：0 行写入）
python scripts/smoke_5c.py                5/5（未回归）
npm run test:frontend                     117 passed
npx tsc --noEmit                          退出码 0
```

**已知限制（§三十八第 17 项）**

1. **真实数据不足以产生任何提案**：1 帖 / 2 快照，所有分组 < 5。本阶段在真机上
   只能验证"正确地什么都不提议"这一半；"产出提案"那一半只能在离线合成数据上验证；
2. **方向必须跨指标一致**是保守的，代价是召回率。三条指标里两条同意、一条反向时
   不提案，即使那两条在直觉上"更有分量"；
3. **`target_length` 不产生版本号**：长度档与 StyleRecipe 的整数长度上限之间没有
   可公认的换算，那里是刻意的缺口；
4. **没有基准配方时 `current_value` 只能取"观察中较低的一侧"**，它是一个观察不是
   一个事实。后端用"两个版本号同时为 `None`"把这一点暴露给界面，`changeLine()`
   在那种情况下**不显示**当前取值；
5. **指标选择固定为 attention / conversation / amplification 三个**，没有为不同
   目标挑选不同指标，也没有指标加权；
6. 提案是**观察性**的：没有对照组、没有随机化、没有统计显著性检验。
   `MIN_SAMPLES_FOR_COMPARISON = 5` 是**采集量门槛，不是显著性**，三处文案都写明；
7. `format_learning_context()` 与 `format_proposal_preview()` **都没有调用点**，
   Agent 不消费本层的任何东西。

---

## R. 明确没有做什么（§一 / §三十五 / §三十九）

**Automatic Learning / Automatic Prompt Optimization / Automatic Style Update /
Automatic Trend Weight Update / Automatic Viewpoint Weight Update / Automatic Critic
Threshold Change / Automatic Budget Change / A/B Testing / Fine-tuning / LoRA /
Embedding / Vector DB / Multi-agent / Autonomous Publish —— 全部未做。**

具体到代码：

- `app/agent/prompts.py` 里只有**定义**，没有调用点；`{{LEARNING}}` / `{{PROPOSAL}}`
  占位符不存在；Graph 的节点一个都没有改；
- 没有改 Writer / Critic / PublishService / Guardrail 的行为；
- 没有改 Trend / Style / Viewpoint / Feedback / LearningReadiness 的任何既有语义
  （§二十七），本阶段的改动全部是追加式；
- 没有给任何已有表加列；唯一的新表只存人的裁决；
- 控制台里唯一新增的写操作是"记下一个人对一条建议的决定"，它不改任何设置。

**严格停在这里（§三十九）。**
