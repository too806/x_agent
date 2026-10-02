# Phase 5B.3 — Viewpoint Intelligence

**范围**：让 Agent 形成**明确、有边界、可追溯的判断**——可以形成判断，但不能把判断
伪装成事实；可以表达立场，但必须知道自己的证据边界。

**基线**：`master`，HEAD `d91f517`（Phase 5B.2 COMPLETE：backend 482 通过 / frontend 46 通过）。

**结论**：完成。链路上缺的那一环补上了——

```
找不到话题 ↓ 研究 ↓ Evidence ↓ Counterpoints ↓ 3 个候选观点 ↓ 确定性评估 ↓ 选一个
          ↓ StyleRecipe ↓ Writer ↓ Critic ↓ Revision ↓ Human Review
```

**本阶段没有发布任何内容**：三次真机运行共 9 个 X 请求，全部是
`GET /2/tweets/search/recent`；`POST /2/tweets = 0`、`POST /2/media = 0`。

---

## A. 交付概览

**新增**

```
app/agent/viewpoint.py                    确定性评估核心（纯函数）
app/agent/labels.py                       新枚举 → 中文（prompt 与 Run Detail 共用）
app/style/spans.py                        n-gram 共享片段检测器（从 evals/style/spans.py 上移）
evals/viewpoint/{__init__,cases,scope,selftest}.py
tests/test_agent_viewpoint.py
tests/test_viewpoint_writer_integration.py
frontend/src/utils/viewpoint.ts
frontend/tests/viewpoint.test.ts
scripts/smoke_5b3.py
scripts/make_viewpoint_fixtures.py
docs/phase-5b3-smoke-runs.json
evals/fixtures/viewpoint_{supported,overclaim,counterpoint_erased,no_reasoning,score_drifted}.json
```

**改动**：`app/agent/{schemas,prompts,search_query}.py`、
`app/agent/tools/{angles,candidates,critic,revise}.py`、`app/db/checkpointer.py`、
`app/schemas.py`、`app/services/admin.py`、
`evals/{behavior/rules,model,selftest}.py`、`evals/style/{selftest,spans}.py`、
`evals/datasets/goals.json`、`prompts/agent/{angles,candidates,critic,revise}.txt`、
`tests/{fakes,test_agent_loop,test_agent_tools,test_agent_search_query}.py`、
`frontend/src/{components/RunDetailDrawer.tsx,types/api.ts}`、`scripts/smoke_5b2.py`。

（`app/agent/state.py` **不在**这个清单里，这是有意的：`RunState` 一个键都没增——
观点与评估明细全部装在既有的 `angles` / `selected_angle` 里。）

**没有新增表、没有迁移、没有新增 AgentAction。**

---

## B. 只读审计结论（工作单第四节）

写代码之前先指认问题。结论：**工作单里假设要新建的那个 `Viewpoint` 模型，系统里
已经有了——它就是 `Angle`，只是缺一半。**

| 问题 | 结论 |
|---|---|
| EvidencePack 能表达什么 | `facts` 两跳可追溯；`consensus`/`counterpoints`/`unknowns` 是**裸字符串、无句柄**；没有范围概念 |
| Angle 与 Viewpoint 的边界 | **已经塌缩成一层**——`Angle.claim` 今天就是判断（`angles.txt` 明写"它是一个判断，不是事实复述"）。真正的边界在 (a) `angles`（候选）vs `selected_angle`（已提交）；(b) 一个观点**内部**的 fact / inference / thesis 三层 |
| SelectedAngle 是否已承担观点职能 | **是**，但很薄：Writer/Critic 只拿到 `claim` + `why_it_matters` |
| Writer 如何使用 Angle | `{{CLAIM}}` + `{{WHY_IT_MATTERS}}`，并且拿到**整包** EVIDENCE 而非该观点的依据子集；`Candidate` 不回指 angle |
| Critic 能判断什么 | 能判编造/立场/角度对不对得上；**判不了** overclaim、counterpoint_awareness、viewpoint_consistency |
| 哪些只是 checkpoint | 全部：`angles` / `selected_angle` / 评估明细（沿用 5B.1 Decision 2、5B.2 §C） |

**审计额外发现**：重生成 `angles` 不清 `selected_angle`（`tools/angles.py`）——当时不可达，
但 §22 的研究回路会把它变成可达的静默不一致。已修（见 R 节 #1）。

于是最终决定是**就地升级 `Angle`，保留类名与动作名**：不新增 `Viewpoint`/`ViewpointAgent`、
不新增动作（§3/§24）。

---

## C. 模型：`AngleDraft` / `Angle` 的分离

照 5B.1 `TopicClusterDraft` 的先例：**LLM 输出契约与存储模型分开**。

```
AngleDraft  ← LLM 输出，没有任何分数字段
    claim            thesis（判断本身）
    reasoning        ← 新增：把事实推到 thesis 的那一步
    why_it_matters
    evidence_ids     支持这个判断的 fact_id
    counterpoints[]  ← 新增：{point, evidence_ids → fact_id}
    assumptions[]    ← 新增
    implications[]   ← 新增
    unknowns[]       ← 新增
    risk             可能错在哪里
    claim_scope      ← 新增：模型声明的适用范围
    claim_type       ← 新增：causal / predictive / evaluative / observational
    novelty_note     ← 新增：新在哪（语义自述，不是分数）

Angle  ← Draft + Python 赋的 id + Python 算出的评估
    angle_id / rank_key
    confidence       确定性计算（§11）
    stance_declared  模型申报的强度；stance 是被证据上界钳过的**生效值**
    viewpoint_score  排序用
    assessment: ViewpointAssessment | None
```

**所有新增字段都有默认值**：`evals/model.py::AgentRunState.angles` 复用生产模型，
任何必填字段都会让**全部现有 fixture 加载失败**。

**`confidence` 保留原字段名**：新角度由 Python 算；`assessment is None` 标记"5B.3 之前的
运行，这个数是模型给的"，Run Detail 如实说明。不新增第二个置信度字段。

**三个 stance 必须分清**（写进了 docstring 与 prompt）：
`CriticResult.stance_strength`（**文本**有没有明确立场）≠ `StyleRecipe.stance_strength`
（**表达强度**目标）≠ `Angle.stance`（**判断**的强度）。

---

## D. 确定性核心：`app/agent/viewpoint.py`（工作单第十一、十二、四十节）

形状照 `app/agent/trends.py`：无 IO、无随机、不取挂钟时间、阈值全在模块级常量，
每条子分数带一句**能在输入里核对**的说明。

| 数 | 由什么算 | 答什么问题 |
|---|---|---|
| `evidence_support` | 绑定到真实 `fact_id` 的支持事实数（分子/分母都写进 reasons） | 撑得够不够 |
| `source_diversity` | 支持事实覆盖的**不同来源**与**不同作者**数 | 是不是一个声音 |
| `counterpoint_awareness` | 已绑定的反方数；**声明了但引不到 fact 的反方不计分** | 有没有找过反方 |
| `unknown_pressure` | **不可被 LLM 操纵**：未被任何事实引用的材料占比 | 还有多少没看懂的 |
| `confidence` | 以上四项加权，**不含 novelty** | 站不站得住 |
| `specificity` | 复用 `app/style/features.py` 的实体/数字密度 − 套话/hedge | 具体还是空话 |
| `novelty` | 确定性部分 + LLM 语义自述，**受 `evidence_support` 门控** | 值不值得说 |
| `viewpoint_score` | 上述加权 | 值不值得拿出来说 |

**`confidence` 与 `viewpoint_score` 是两个数**：前者答"站不站得住"，后者答"值不值得说"。
两者都与 `trend_score` 无关（§13）。

### D1. `confidence` 不由 LLM 决定（§11/§12）

四个输入项全部来自可复算的事实（绑定数 / 来源数 / 作者数 / 未被引用的材料数），
四项权重写死在模块里。LLM 只能提供解释文本，进不了这个数。

**单来源封顶**：`evidence_support` 与 `confidence` 各有一条上限，触发时**保留封顶前的
原值**（`evidence_support_raw` / `confidence_raw`）——"被压住了"与"本来就低"必须分得开，
否则一个被封顶压到 0.45 的观点和一个真的只有 0.45 的观点在分数面前一模一样。

### D2. Stance 上界（§9/§10）

Python 从评估结果推出 `max_stance`（`strong / moderate / cautious / uncertain`）。
LLM 可以申报任何**不超过**上界的 stance；超了由 Python 降级并写明理由，
申报值仍保留在 `stance_declared` 里。于是"允许 cautious / no_strong_viewpoint"
是一条**策略**，不取决于模型当天的心情；而模型主动选得更保守时，那个选择被尊重
（`evals/viewpoint` 的 `conflicting_evidence` 第 0 条钉的就是这件事）。

### D3. Novelty 与 evidence 分离（§14/§15/§31）

`novelty` 的确定性部分只判两件事：**是不是只是某条事实的复述**、**是否与另一个候选
近似重复**。判不出来时返回 `None`，不填 0。它还**受 `evidence_support` 门控**——
一条没有证据的大胆判断不能因为"新"而排到前面。

`reasons` 里**绝不**出现"全网原创"这类措辞：系统没有全网语义检索能力，
声称原创就是在声称一个做不到的事（§31）。

### D4. Claim scope 与 overclaim（§28/§29）

- LLM 申报 `claim_scope`（`case / product / segment / industry / society`）；
- Python 从绑定事实算出 `evidence_scope`（不同来源数、不同作者数），照
  `EVIDENCE_SCOPE_LADDER` 比对后置 `scope_exceeds_evidence` 并写理由；
- **另外**扫描 claim 原文里的全称量词（整个/所有/全部/必然/一定/时代/行业…），
  所以"声明了窄 scope、正文却写成整个行业"照样会被标出来。

**防操纵**：这个标志是给 Critic 与 eval 规则用的**参考信号**，不是悄悄扣分的暗分。
申报值、证据覆盖面、命中的量词三者都写进 reasons，人能复核。于是"一律申报最窄
scope"既拿不到好处，也盖不住正文。

---

## E. 流程接入（不新增动作）

### E1. 生成 `tools/angles.py::generate_angles`

- 解析目标改为 `AngleDraft`；逐条跑 `viewpoint.assess(...)`；
- 引不到 `fact_id` 的支持证据照旧丢弃，整条没有支持证据的照旧丢弃（§7）；
- **`angle_id` 在确定性排序之后再赋**（a1 = 最高分）。理由照 `tools/discover.py` 已写下的
  那条：`format_state` 里 id 与分数会同时出现，一个名次对不上分数的编号比没有编号更误导；
- 重生成时同时 `delta={"selected_angle": None}`。

### E2. 选择 `tools/angles.py::select_angle`（§20/§21）

与 `select_topic` 同构——**分数算好了就不再让模型选一遍**：

```
确定性排序 rank_key → 分差 > TIE_MARGIN：直接取最高分
                    → 分差 ≤ TIE_MARGIN：ambiguous，Planner 的 target 只在这个并列区间内被接受
```

被推翻/无效的 target 与 ambiguous 判定都写进 tool summary 与 trace。
**不把 `select_angle` 从 `allowed_actions` 里移除**（ambiguous 时移除会让研究预算
用尽的运行无路可走）；改为在 `format_state` 里显式标出 ambiguous，由 prompt 引导
Planner 在还有检索预算时优先补反方证据。

### E3. 预算：一处都没动

不新增动作 ⇒ `AgentAction` / `ACTION_HINTS` / `FALLBACK_ORDER` / `allowed_actions` /
`graph.TOOL_ACTIONS` / `tools.build_tools` **六处全不动**；`config/agent.yaml` 的
`max_steps` / `max_research_calls` 也不动。研究回路（§22）已经由这两个预算约束住，
本阶段只让缺口**可见**。

### E4. 状态渲染 `prompts.py::format_state`

观点那一行从"已生成 N 个"升级为逐个列出：编号、`viewpoint_score`、支持事实数、
已绑定反方数、来源数、`stance`、ambiguous 标记。Planner 的 `reason` 才能是
**缺口驱动**的（"当前最高分观点只有 0 条反方证据"），而不是"继续研究"。

---

## F. Writer 与 Critic

### F1. 单一渲染函数 `format_viewpoint(angle) -> str`

照 `format_style_recipe` 的形状：**永远返回文本**，漏传时字面量 `{{VIEWPOINT}}`
原样发给模型（不静默变成空串）。

`candidates.txt` / `revise.txt` / `critic.txt` 三个模板的 `{{CLAIM}}` + `{{WHY_IT_MATTERS}}`
合并替换为 `{{VIEWPOINT}}`——**Writer 与 Critic 必须看到同一份渲染**（5B.2 的教训：
两边看到的目标不一致时，Critic 会拿另一个目标去判）。

内容：判断本身 / 推理层 / 支持事实（带 `fact_id`）/ 反方 / 适用范围 / 已知假设与未知 /
风险 / stance。**不含任何分数**——分数不该影响"怎么写"（§25/§26）。

### F2. Writer 不得改变判断（§26）

- `Candidate` 新增 `viewpoint_ref`，由工具赋为 `selected_angle.angle_id`，**不采信模型**；
- `revise` 原地替换时保留 `viewpoint_ref`；
- `candidates.txt` 明写：可以表达、压缩、调整结构，**不得替换 thesis**。

### F3. Critic 新增维度（§27）

`CriticResult` 新增嵌套 `viewpoint_fit: ViewpointFit | None`，照 `StyleFit` 的先例：
全部 `float | None`（"没评" ≠ "评了 0"），任何分项缺失不得让整份响应校验失败。

```
viewpoint_clarity        正文有没有把那个判断说清楚
viewpoint_support        判断是否落在证据范围内
viewpoint_consistency    正文是否忠实于它 claims 的那个观点（靠 viewpoint_ref）
counterpoint_awareness   有没有处理反方，还是只挑支持自己的说
overclaim_risk           **越高越差**：claim 的范围超出证据的范围
```

**不给总分**（§27 明禁 `viewpoint_score = 9.2`）。确定性 scope 参考信号另走一条路
传给 Critic（照 `AI_SIGNALS` 的做法：只作参考事实，**绝不混进 LLM 自己的分数**、
**不做门禁**）。

---

## G. 持久化与序列化（§37/§38）

- **不新增表、不写迁移**。`tests/support.py::_ALL_TABLES` 不动。
- `app/db/checkpointer.py` SERDE：登记新增的**嵌套枚举**（枚举即使嵌套也必须登记）
  与 `AngleDraft`。嵌套**模型**（`ViewpointAssessment` / `Counterpoint` / `ViewpointFit`）
  **不登记**——会被 `model_dump()` 内联，登记了就是死条目。
- `RunState` 不需要新增键。
- Run Detail 的 viewpoint 块是 **checkpoint-only**，与 `guardrail_*` / `human_action` 一致。

**不建 `viewpoint_memory`、不做 embedding、不做 vector DB**（§38）：
"Agent 记住自己以前的观点"不是本阶段的能力。

---

## H. Evaluation

### H1. 离线基准 `evals/viewpoint/`（工作单第三十二节）

四文件布局照 `evals/style/`。`cases.py` 是**手写**正反例，不碰真实 X、不碰语料、不碰 LLM——
CI 里跑不需要网络，也不会有"今天红了明天绿了"的抖动。

七类，**每类都成对**：只看正例的基准分不清"检测器在判"与"检测器对什么都判"。

| 类别 | 数据 |
|---|---|
| `strong_evidence` | 四条事实/四条来源/四位作者；作为 `overclaim` 的对照 |
| `weak_evidence` | 一条事实、一处未知四起 ⇒ `uncertain`，不强行生成强观点 |
| `conflicting_evidence` | 两条草稿：回应了反方的 vs 装作没争议的 |
| `single_source` | 三条事实全部同一来源同一作者 ⇒ 支撑度与置信度都被压住 |
| `counterpoint_present` | 两条草稿：绑到 fact 的反方 vs 只写一句空话、引 `f99` |
| `overclaim` | 同一批证据，一条申报 `society`、一条申报 `segment` |
| `prediction` | 同一批证据，一条写成"必然"、一条写成"或许" |

预期写成**数据**而不是注释：`(草稿下标, 字段, 关系, 期望值)`。关系是显式的
（`<= 0.40` 而不是"被压住了"），所以常数被改动时失败信息直接指向
"0.45 <= 0.40 不成立"，而不是一句"不符合预期"。

**每条检查都带反证**（照 `check_the_shared_span_detector_actually_fires` 的先例）——
没有反证，"永远返回空集的检测器"会让其余检查全绿。17 项里有 8 项的名字本身
就带「（反证）」，报告里逐条印出来：

```
单一来源上限确实咬住（反证）              立场上界由证据推出，模型只能更保守（反证）
申报范围超标会压低上界（成对反证）        overclaim 检测器确实会响（反证）
反方检测器读的是证据包（反证）            单一来源检测器确实会响（反证）
无证据与低支撑是两个码（反证）            干净观点零问题（反证）
```

还有一条 `每个问题码都可达`——反证的是"某条分支从来不会触发"这件事本身。

### H2. Run 级规则（4 条新 gate）

| 规则 | 断言 |
|---|---|
| RULE-020 | **重要 thesis 必须有证据**：选定的观点 `evidence_ids` 为空或全部解析不到 ⇒ fail；且必须有推理层与自我反驳 |
| RULE-021 | **overclaim**：claim 声明的范围超出绑定证据的范围（或正文出现全称量词而证据是局部）⇒ fail |
| RULE-022 | **反方不得被抹掉**：证据包里已有反方，观点却既无绑定反方、正文也声称无争议 ⇒ fail |
| RULE-023 | **`viewpoint_score` 可复算**：用生产的 `viewpoint.assess` 从 state 里的原始输入重算两次，逐字段相同且与存下来的值一致 |

RULE-023 直接照抄 RULE-017 的两条已确立纪律：

1. **缺原始输入返回 `unavailable`，不返回 pass**——原注释写得很清楚："缺输入时返回 pass
   会让这条规则在大多数数据上变成装饰品"。所以 `evals/model.py::AgentRunState` 声明了
   原始输入字段，新 fixture 必须带上，否则 RULE-023 会在整批数据上静默失效。
2. **口径变了 fixture 就该红**，由人重新生成并看见 diff。这是特性不是缺陷。

`check_every_gate_can_fail` 强制每条都能在构造数据上变红——`evals/datasets/goals.json`
新增 G-017…G-021 五条，其中四条是**成对的反例**（每条只改一个变量）：

- **G-017** 证据充分的正常路径：`passed: true`（正例，证明这四条不会误伤干净数据）
- **G-018** 同一判断、同一证据，只把推理层与反方删掉 ⇒ `RULE-020` 红
- **G-019** 同一证据、同一反方，只把 `claim_scope` 从 `industry` 抬到 `society` ⇒ `RULE-021` 红
- **G-020** 证据包里写着反方，观点既不提也不绑，正文还加一句"其实并没有争议" ⇒ `RULE-022` 红
- **G-021** 候选观点的 `viewpoint_score` 抬高 7 分，其余字段全部与复算值一致 ⇒ `RULE-023` 红

### H3. 单测 `tests/`

- `test_agent_viewpoint.py`：同一输入两次结果逐字段相同、改一项输入只有该变的那一项变、
  novelty 与 evidence 分离、stance 上界钳制、scope 比对、并列的确定性；
- `test_agent_tools.py`：`AngleDraft` → `Angle` 往返、引不到 fact 的反方不计分、
  重生成 angles 清 `selected_angle`、`select_angle` 的并列与推翻路径；
- `test_agent_loop.py`：**checkpoint 往返断言 `isinstance` 而不是值**（5B.2 的教训：
  未登记的嵌套枚举会被拦成字符串再被 pydantic 悄悄校验回去，测试全绿而日志在响）；
- `test_viewpoint_writer_integration.py`：`{{VIEWPOINT}}` 确实被替换（不存在未替换的
  占位符）、Writer/Critic 拿到同一份渲染、`viewpoint_ref` 贯通 generate → revise → critic。

---

## I. 真实 Smoke（工作单第三十三、三十四、四十二节）

`scripts/smoke_5b3.py`：**三次真实运行**（X + DeepSeek），**五个观测面**。
Smoke 1/4/5 共用同一次运行（每条记录带 `source_run`）——观测面是读数的角度，
不是必须各跑一次的仪式。

只读守卫照 `smoke_5b1.py` 的先例：`RealXApiClient._request` 被包一层，
每个请求先留痕、**非 GET 在发出去之前抛**。记录写 `docs/phase-5b3-smoke-runs.json`。

**每个观测面记两样东西**，这是本阶段 smoke 设计上唯一的实质决定：

- **观察**：这一轮真机上实际发生了什么（可能**缺席**——今天没有争议就是没有）；
- **确定性对照**：拿这一轮的**真实证据索引**喂给生产的打分器，证明**策略**成立。

理由：真机数据不由我们决定。某天证据里恰好没有反方时，如果只报一个布尔值，
"反方处理已验证"就会在没有任何反方被处理的情况下变绿。所以每个面把
`property_exercised`（这条性质今天有没有被真的踩到）与它要支持的结论分开记。

| # | 目标 | 验证 | 结果 |
|---|---|---|---|
| 1 | 热点 | Trend → Evidence → 3+ 候选观点 → 确定性评估 → 选一个 | ✅ 25 条事实 / 22 来源；5 个候选；a1 选中；`id_order_matches_score_order: true`；评分复算 5×32 字段 0 处不符 |
| 2 | 争议 | 确实出现 `counterpoints` 与 `cautious` stance | ✅ 证据包 2 条反方，`property_exercised: true`；5 个观点的 stance 全是 `cautious`；对照：申报 `strong` 的草稿被钳到 `uncertain` |
| 3 | 冷门 | 证据不足时**不**强行生成强观点 | ✅ 6 条事实 / 2 条来源；4 个观点全 `cautious`，`stance_exceeds_ceiling: []`；对照：无证据 → `evidence_support 0.0`、`max_stance uncertain`、总分 24.12 < 上限 50；单来源 → 封顶生效（0.6625 → 0.45） |
| 4 | 新判断空间 | `novelty` / `specificity` 有区分度 | ✅ `specificity` 0.6167 有区分度；`novelty` **恒为 1.0**（见下） |
| 5 | 全链路 | Viewpoint → StyleRecipe → Writer → Critic → waiting_for_review | ✅ 11 步，`reached_human_review: true`；3 个候选全部回指 `a1`；Critic `viewpoint_fit` 三个候选都在；`viewpoint_render_has_placeholder: false` |

**Smoke 4 的诚实结论**：这一轮的 5 个候选观点 `novelty` 全是 1.0，也就是**这个维度
今天没有产生任何区分度**。原因是 `novelty` 的确定性部分只判"是不是复述某条事实 /
是否与另一候选近似重复"，这 5 条都没踩到；语义部分则由 LLM 自述。

这里**没有**结论说"novelty 和 evidence_support 排出来的顺序一样"——
smoke 记录的是 `novelty_and_evidence_order_differ: null` **加一句说明**，
而不是 `false`。区分度为零时，"两者排序相同"与"两者排序不同"都是没有分辨力的读数，
报成布尔值就是拿一个无法证伪的比较去支持一个结论。这与 RULE-023 的
`unavailable ≠ pass` 是同一条纪律。

**Smoke 5 的 DB 状态**：`db_status: "running"`，`reached_human_review: true`。
两者不矛盾，且记录里写明了为什么：`agent_run.status = "waiting_for_review"` 是
`app/main.py:213-216` 在看到 `__interrupt__` 之后写的，而 smoke 直接调 `graph.ainvoke`，
绕过了 API 那一层。判据用 `"__interrupt__" in state`，同时把 DB 里的原值一并记下来——
把"API 层没参与"这件事写进记录，比让一个 `false` 看起来像"没跑到人工审核"要好。

### X 请求账（工作单第三十四节）

```
请求总数     9
按方法       GET: 9
按端点       GET /2/tweets/search/recent: 9

POST /2/tweets = 0（必须为 0）
POST /2/media  = 0（必须为 0）
```

**为测试而发布一次都没有做。**

**这份记录不需要重跑。** `docs/phase-5b3-smoke-runs.json` 的末次写入时间是本阶段
**最新**的一个改动——比它晚的源码改动一个都没有。也就是说记录里这一轮就是对着
**当前代码**跑的，再跑一次不会得到新信息，只会占掉真实配额。
这一点是核对过文件时间的，不是默认。

（阶段性对账：本阶段最后一次改源码是 `app/agent/search_query.py` 与
`tests/test_agent_search_query.py`，改的是 `*` 那条降级规则——**它正是被前一轮
真机 smoke 的失败逼出来的**，改完之后才跑了末次 smoke。）

---

## J. 前端（工作单第三十五、三十六节）

不重做 Dashboard、不新增页面、不做 viewpoint 图表。只在 Run Detail 增加一节。

- 后端：`app/schemas.py::RunViewpointSummary` + `app/services/admin.py::viewpoint_summary(state)`
  （照 `style_summary` 的形状，**同一处按 `state.get("goal")` 门控**）+
  `AdminRunDetail.viewpoint`。两侧都加了——它是 `response_model`，schema 里没有的字段
  会被静默丢弃。
- 摘要把 `evidence_ids` **解析回事实原文**（从同一个 state 的 evidence pack 里取），
  人工审核员能直接看到"这条帖子为什么这样说"。
- 前端：`types/api.ts` 加类型、`RunDetailDrawer.tsx` 加 `ViewpointSection`。与
  `StyleSection` 完全平行：非 Agent Run 返回 `null`，页面与从前一致。
- 首屏只给：**核心判断 / 证据 / 反方 / 风险 / 置信度**；分数、全部候选观点、
  评估依据、scope 信号折进已有的 `<details className="disclosure">`。
- 新枚举的中文标签由后端随取值一并给出（同源于 `app/agent/labels.py`）。
- `frontend/tests/viewpoint.test.ts`（`node --test`，纯函数级，15 项）。

**阅读顺序上的一个决定**：`ViewpointSection` 摆在 `StyleSection` **前面**。
判断决定**说什么**，写法只决定**怎么说**；反过来摆，读者会把观点读成措辞的产物。
这一条写进了组件的 docstring。

**置信度那一格**：显示的不是百分比，并且旁边明写"由证据支撑度 / 来源多样性 /
反方意识 / 未知压力四项加权算出，**不含新颖性**"。这是把 §11/§15 的纪律带到界面上——
一个没有来源的数字看起来像客观真值，写清它是怎么来的才不至于。

---

## K. 已知限制

1. **`novelty` 在真机上没有区分度**（见 I 节 Smoke 4）。这是一个**观察**，不是一个已修
   的缺陷：系统的确定性部分只判复述与重复，这两件事在一轮正常运行里本来就不常发生。
   要让 novelty 真正有分辨力，需要的是**跨运行**的语料视野（"这个判断最近被人说过吗"），
   而那正是本阶段明确不做的东西（§38：不要 memory、不要 embedding）。
2. **`similarity` 那份 n-gram 实现现在是单一来源**（`app/style/spans.py`），
   但 `_duplicate_group` 的近似阈值是 `app/agent/viewpoint.py` 里另设的常数。
   两处的**切片口径**同源，**判重阈值**不同源且目前没有被测试钉住。
3. **反方的"是反方"由 LLM 决定**。系统能验证的是"你引的这个 `fact_id` 真的存在"，
   验证不了"它真的是这个判断的反方"。引一条无关事实当反方，`counterpoint_awareness`
   照样给分——`evals/viewpoint` 的 `counterpoint_present` 第 0 条就是这个形状
   （引 `f1` 而 `f1` 本身是支持性事实）。这条限制无法用确定性代码消除。
4. **`evidence_scope` 只有来源数与作者数两个输入**。一个"10 个来源但全都转发同一条
   新闻"的证据集会算出很高的 scope，而它的实际覆盖面很窄。
5. **Run Detail 的 viewpoint 块只活在 checkpoint 里**。checkpoint 被清理或 thread 丢失时
   这一块就没了；与 `guardrail_passed` / `human_action` 现在行为一致。
6. **`assessment is None` 的老数据没有迁移**。5B.3 之前的 Run 打开后仍显示
   "这个数是模型给的"，这是有意的——补算一个当时没有算过的分数，比留空更糟。
7. **`max_stance` 的上界表是四条拍出来的常数**（`EVIDENCE_SCOPE_LADDER` 与
   各封顶值），没有真实语料统计支撑。它们是本阶段的**假设**，改动会让
   fixture 变红——这是特性。
8. **`claim_scope` 的申报仍是 LLM 的自由选择**。系统只在"申报得比证据宽"时报警，
   申报得比证据**窄**不报警（那通常是保守，不是错误）——但这也意味着一个观点可以
   永远申报最窄的 scope 来规避 RULE-021。缓解手段是正文量词扫描（§29：
   "模型声明了窄 scope、正文却写成整个行业"照样被标），不是彻底解决。

---

## L. 明确没有做什么（工作单第三、三十八、四十五节）

```text
Feedback Loop        X engagement ingestion   Memory / viewpoint_memory
Style evolution      Fine-tuning / LoRA       Vector DB / embedding / pgvector
Multi-agent          Autonomous Publish       新数据库表 / 迁移
ViewpointAgent       新 AgentAction           新 Dashboard 页面 / 图表
LLM 决定 confidence  LLM 决定排序             "全网原创" 之类的能力声称
```

- **没有新增任何 AgentAction**；`max_steps` / `max_research_calls` 一个都没动。
- **没有新增 ViewpointAgent**——仍然是 **One Agent + Tools**。
- **没有建 `viewpoint_memory`、没有 embedding、没有向量库**（§38）。
- **没有让 Agent 自行改变用户立场**：观点只来自 Evidence + Reasoning（§39）。
- **没有真的发布过任何内容**（§34）。
- **没有声称"这个观点是全网原创"**——系统没有全网语义检索能力（§31）。
- **没有把 `trend_score` 和 `viewpoint_score` 混起来**（§13）。
- **没有让 LLM 决定最终 confidence、排序或资格**（§11/§12/§40）。
- **Phase 5B.3 到此为止**（§45）。

---

## M. 本阶段修掉的真实缺陷

全部**在修之前是静默的**——没有一条是靠读代码看出来的。

| # | 缺陷 | 后果 | 发现方式 |
|---|---|---|---|
| 1 | 重生成 `angles` 不清 `selected_angle` | `selected_angle` 仍指向旧编号的对象，Writer 会拿一个不属于当前候选集的观点写稿**且不报错** | 实现 §22 研究回路时发现它从"不可达"变成"可达" |
| 2 | `score_viewpoints` 把 `Angle.stance` 当成模型申报的 stance 读 | 打分**不可复算**——因为 `select_angle` 会把上一次算出来的 `Angle` 再喂进去，第二次读到的"申报值"其实是第一次的**生效值**。这是 RULE-023 想拦的那一类错误，只是它长在规则自己脚下 | 写 RULE-023 的复算比对时，第二次结果与第一次不同 |
| 3 | `declared_stance_of` 按真值判断字段有没有申报 | 申报 `None` 与"没申报"被混为一谈，"模型主动选得更保守"这件事丢失，等于 §10 的一半失效 | 同上，修完 #2 之后暴露出来 |
| 4 | `_differs` 把 `[]` 与 `()` 判成不同 | 存储层的 pydantic `list` 与计算层的 `tuple` 永远"不相等"，RULE-023 **对任何数据都红** | 新 fixture 全红 |
| 5 | RULE-021 在干净 fixture 上不可达 | `evidence_scope` 算到 `SOCIETY` 时把申报路径整条关掉，"范围超出证据"永远触发不了 | `check_every_gate_can_fail` |
| 6 | `evals/viewpoint` 用 `Angle` 造"模型草稿" | 造出来的是一份自相矛盾的记录（存储模型却只带申报），`declared_stance_of` 读成"模型没申报"；`conflicting_evidence` 与 `single_source` 两条静默失效 | 自检从 17/17 掉到 15/17 |
| 7 | `search_query` 不认 `*` | 目标里的 Markdown 强调（`**双方吵起来**`）让候选选题被判定不可检索，检索词兜底到目标原文 → X 回 `400 Wildcard '*' character cannot appear in a term` → **整次运行到此为止** | 真机 Smoke 2 整轮失败 |
| 8 | `format_state` 的观点行只报"已生成 N 个" | Planner 的 `reason` 只能是"继续研究"这类无信息的话 | 真机 trace 里连续三次同样的 reason |
| 9 | smoke 用 `agent_run.status` 判有没有到人工审核 | 直接调图时 DB 状态停在 `running`，`reached_waiting_for_review` 恒为 `false`——一个正在正常工作的运行被判成没跑到 | 第一次真机 smoke |
| 10 | smoke 从 `state["finish_reason"]` 取值 | 它**不是** state 的键（由 `metrics.finish_reason(state)` 从 `decision_trace` 末条 `finish` 推出来），于是永远记录成 `None` | 三次运行的收尾理由全是 `null` |
| 11 | smoke 在 novelty 区分度为零时报 `novelty_and_evidence_order_differ: false` | 一个没有分辨力的比较被报成一个结论，读起来像"两者排序相同" | 逐字段核对读数时发现 novelty 恒为 1.0 |
| 12 | smoke 的 `links` 里一个值为 `false` 的条目读起来像"这个链接缺失" | `guardrail: false` 实际意思是"护栏跑了但没通过"，与"没有护栏"完全是两件事 | 与 `candidate_viewpoint_refs` 对账时 |
| 13 | `app/` 不能 import `evals/`，而 novelty 需要 n-gram 检测器 | 直接抄一份会在某天漂成两套口径，而**两套长得一样是最难发现的错误** | 实现 novelty 时 |

第 2、3、4 条值得单独说：它们全部长在**本阶段新写的那条规则自己的脚下**。
RULE-023 的设计意图是"分数量必须能从原始输入复算"，而它的第一次运行就发现了
打分函数本身不幂等、以及比较函数会把等价的两种序列判成不同。如果当时只是
"把规则调绿"，这条规则就会变成 H2 里警告过的装饰品。

第 13 条的处置**改掉了计划里的决定**：计划写的是"n-gram 原语放 `app/agent/viewpoint.py`，
`evals/style/spans.py` 原样不动，用一条测试钉住两边同口径"。实现时选择了更干净的一条——
照 `app/style/features.py` 已经在文档里写下的同一处置**把检测器整体上移到
`app/style/spans.py`**，`evals/style/spans.py` 变成转发层。
这样**口径只有一份**（不是两份靠测试同步），代价是动了 5B.2 已验收的文件——
5B.2 的 22 项自检全部保持绿色，`scripts/smoke_5b2.py` 的调用点只改了 import 来源。

### 真机那几轮又逼出两条

| # | 缺陷 | 后果 | 发现方式 |
|---|---|---|---|
| 14 | `pack_of` 不把 JSON dict 还原成 `EvidencePack` 模型 | `getattr(dict, "facts")` → `None`，**每个分数都算成 0**，复算报告于是报出一份"不可复现"的假案 | 评分复算在真实 state 上先报全红，但分数看着正常 |
| 15 | `smoke_5_chain` 用属性访问读候选观点 | 候选回来是 dict 时抛 `AttributeError: 'dict' object has no attribute 'candidate_id'`——**观测代码自己把运行判失败** | 第一次跑 Smoke 5 |

第 14 条的形状值得记下来：`state` 里的 `evidence_pack` 在 checkpoint 往返之后
可能是一个 dict。`getattr` 对 dict 永远成功、永远返回默认值，所以它不是抛异常，
而是**安静地把 25 条事实读成 0 条**。这与 5B.2 修掉的 `published_at` 是同一类：
一个只在数据里缺、不在代码里错的路径。

---

## N. 边界与持久性

- **`evals → app` 是仓库里唯一的允许方向**，本阶段没有反向。
  `app/style/spans.py` 的上移与 `app/style/features.py` 同源，正是为了守住它。
- **观点候选、选中的观点、评估明细全部只进 checkpoint**，不进数据库（§37）。
- **没有新增表、没有写迁移**，`tests/support.py::_ALL_TABLES` 不动（§38）。
- Run Detail 的 viewpoint 块是 checkpoint-only，与 `guardrail_*` / `human_action` 一致。
- **不新增动作 ⇒ 六处同步约束全不动**（§24）：`AgentAction` / `ACTION_HINTS` /
  `FALLBACK_ORDER` / `allowed_actions` / `graph.TOOL_ACTIONS` / `tools.build_tools`。

---

## O. 逐条回答工作单第四十四节的 21 个问题

1. **Angle 与 Viewpoint 最终如何区分。**
   不区分成两个模型——**就地升级 `Angle`**。真正的边界有两层：
   横向是 `angles`（候选集）vs `selected_angle`（已提交的那一个）；
   纵向是一个观点内部的 `evidence_ids`（事实）/ `reasoning`（推理）/
   `claim`（判断）。类名与动作名全部保留。

2. **Viewpoint schema。**
   `AngleDraft`（LLM 输出，无分数字段）→ `Angle`（存储模型）。
   见 C 节的字段表。新增字段全部带默认值，否则现有 fixture 全部加载失败。

3. **Fact / Inference / Viewpoint 如何分层。**
   `evidence_ids` → `EvidenceFact`（Fact）；`reasoning` 是"把事实推到判断"的那一步
   （Inference），RULE-020 强制它非空；`claim` 是判断本身（Viewpoint）。
   三者在同一个模型里，由不同的字段承载，而不是靠约定。

4. **Evidence binding。**
   两跳可追溯：`Angle.evidence_ids` → `EvidenceFact.fact_id` →
   `EvidenceFact.source_ids` → `research_item.id`。引不到的 id 在生成时丢弃；
   反方同理（`counterpoint_evidence_ids` 引不到 fact 就不计分）。

5. **Counterpoint。**
   双层：**声明的**（`counterpoints[]`）与**绑定的**（`counterpoint_evidence_ids`）。
   `counterpoint_awareness` 只给绑定计满分，声明不绑只给 0.35——"说了"与"引到事实"分开。

6. **Confidence。**
   由 `evidence_support` / `source_diversity` / `counterpoint_awareness` /
   `unknown_pressure` 四项加权，**不含 novelty**。全部输入可复算，LLM 只提供解释。
   两条封顶各保留 `*_raw`。"推导不出"返回 `None`，不返回 0。

7. **Viewpoint scoring。**
   `viewpoint_score`（0–100）与 `confidence` 是两个数，也与 `trend_score` 无关。
   排序、并列判定、资格全部由 Python 算，LLM 不参与。

8. **Novelty。**
   确定性部分（是否复述某条事实、是否与其他候选近似重复）+ LLM 语义自述，
   **受 `evidence_support` 门控**，与 `evidence_support` 是两个独立字段。
   判不出来返回 `None`。真机上这一维在三次运行里没有产生区分度（见 K 节 #1）。

9. **Overclaim detection。**
   `claim_scope`（申报）× `evidence_scope`（从绑定事实算）× 正文全称量词扫描。
   三者都写进 reasons，作为**参考信号**给 Critic 与 RULE-021，不是暗分。

10. **Viewpoint 与 Writer 的接口。**
    `format_viewpoint(angle)` 单一渲染函数，Writer 与 Critic 看**同一份**。
    内容含判断/推理/支持事实（带 id）/反方/适用范围/假设与未知/风险/stance，
    **不含任何分数**。`Candidate.viewpoint_ref` 由工具赋值，不采信模型。

11. **Critic 新增维度。**
    `viewpoint_fit`：`viewpoint_clarity` / `viewpoint_support` / `viewpoint_consistency` /
    `counterpoint_awareness` / `overclaim_risk`（越高越差）。五个 `float | None`，
    **不给总分**。

12. **是否新增 AgentAction。**
    **没有。** `AgentAction` / `ACTION_HINTS` / `FALLBACK_ORDER` / `allowed_actions` /
    `graph.TOOL_ACTIONS` / `tools.build_tools` 六处全不动。

13. **是否新增数据库。**
    **没有。** 不新增表、不写迁移。全部只进 checkpoint。

14. **Evaluation。**
    离线基准 `evals/viewpoint/`（七类成对正反例，17 项检查，每项带反证）
    + 4 条新 gate 规则 RULE-020…023（`check_every_gate_can_fail` 强制每条可变红）
    + `tests/` 的新单测与 checkpoint 往返断言。

15. **Offline benchmark。**
    `evals/datasets/goals.json` 新增 G-017…G-021：一条正例 + 四条**每条只改一个变量**
    的反例，各自钉住一条新规则。`run_benchmark --dataset goals.json` 全绿。

16. **5 次真实 Smoke。**
    三次真实运行、五个观测面，见 I 节。Smoke 1/4/5 共用同一次运行。
    每个面各记"观察"与"确定性对照"两样东西。

17. **X GET。**
    9 个请求，全部 `GET /2/tweets/search/recent`。见 I 节。

18. **X POST。**
    `POST /2/tweets = 0`、`POST /2/media = 0`。为测试而发布**一次都没有做**。

19. **完整测试。**
    见 P 节。

20. **已知限制。**
    见 K 节，8 条。

21. **没有做什么。**
    见 L 节。

---

## P. 验证命令与结果（全部串行）

```bash
PY=/c/Users/Too_Far/miniconda3/envs/2026-agent/python.exe

$PY -m pytest -q
$PY -m evals.selftest
$PY -m evals.run_benchmark --dataset goals.json
$PY -m evals.style.selftest
$PY -m evals.viewpoint.selftest
$PY scripts/smoke_5b3.py

cd frontend && node --test "tests/**/*.test.ts"
cd frontend && npx tsc --noEmit
cd frontend && npm run build
```

**只增不减**（5B.2 文档记的基线 → 本阶段；本阶段没有删除或放松任何一条既有测试）：

| 项 | 5B.2 基线 | 5B.3 | 结果 |
|---|---|---|---|
| `pytest -q` | 482 | 555 | ✅ 全通过 |
| `evals.selftest` | 12 | 14 | ✅ 全通过 |
| `evals.style.selftest` | 22 | 22 | ✅ 无回归 |
| `run_benchmark` | 16 | 21 | ✅ 全通过 |
| `evals.viewpoint.selftest` | — | 17 | ✅ 新增 |
| 前端 `node --test` | 46 | 61 | ✅ 全通过 |
| `tsc --noEmit` | clean | clean | ✅ |
| `npm run build` | ✅ | ✅ | ✅ |

**注意**：在仓库根跑 `evals` 必须用 `python -m evals.…`（本机 editable 安装的 `.pth`
指向 master 检出，按路径直跑会 import 到 master 的 `evals` 包，见
`docs/phase-5b1-content-intelligence.md:333-336`）；`run_benchmark` 的 `--dataset`
取的是**数据集名字**（`goals.json`），给路径会被 `DATASET_DIR` 二次拼接后报
`FileNotFoundError`。

---

## Q. 结论

Viewpoint Intelligence 决定的是**"说什么判断"**，而不是"怎么表达"（5B.2）或
"说哪个话题"（5B.1）。三者的分界在代码里是硬的：

- **判断来自证据**：`evidence_ids` 两跳可追溯；没有绑定事实的观点在生成时就被丢掉；
- **强度由证据定上界**：`max_stance` 是 Python 推出来的策略，LLM 只能更保守，不能更笃定；
- **置信度不由 LLM 报数**：四个可复算的输入项，两条封顶各留原值；
- **值不值得说与站不站得住是两个数**：`viewpoint_score` ≠ `confidence`，两者都 ≠ `trend_score`；
- **反方是能力不是装饰**：声明了引不到事实的反方只得 0.35，且 RULE-022 会把
  "证据里有反方、观点装作没有"判失败；
- **范围必须自证**：申报的范围超出证据绑定的范围就会被标出来，正文里的全称量词
  单独扫一遍。

链路的终点因此从"一篇措辞不错的稿子"变成
**基于当前证据形成的一个明确、有边界、可追溯的判断**——它知道自己凭什么成立、
管到哪一步为止、可能错在哪里。**这是本阶段的成功判据（§43）。**

**Phase 5B.3 — Viewpoint Intelligence 完成，立即停止（§45）。**
