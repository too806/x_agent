# Benchmark

数据集、fixture、回归门禁，以及怎么加一条新基准。

---

## 两种运行模式

```bash
python evals/run_benchmark.py            # fixture 模式（默认）
python evals/run_benchmark.py --dataset  # 基准模式
```

| | fixture 模式 | 基准模式 |
|---|---|---|
| 输入 | `evals/fixtures/*.json` | `evals/datasets/goals.json` |
| 输出 | 每条运行的分数卡 | 每条基准目标「符合/不符合期望」 |
| 退出码 | 恒为 0 | 有基准不符合期望时返回 1 |
| 判据 | 无——fixture 里有故意造出来的缺陷数据 | §14 定义的回归门禁 |

**fixture 模式的退出码为什么恒为 0**：fixture 里有 `autonomous_publish`
这样**故意**触发硬门禁的数据——那正是它的用途。让这个入口因此返回非零，
等于让它在仓库的默认数据上永远"失败"，那谁都不会再看它。
真正该全绿的是 `--selftest`。

---

## fixture

`evals/fixtures/` 下七份数据。每一份都在 `note` 字段里说明它**是为了演示
什么**，并且在 JSON 顶层标注 `"synthetic": true`。

| fixture | 演示什么 | 硬门禁 |
|---|---|---|
| `healthy_agent_run` | 一次完全正常的自主运行 | 全绿 |
| `accepted_candidate` | 改稿 → 重新评审 → 人工 approve → 发布 | 全绿 |
| `revision_loop` | 改稿预算用到上限（两次），**不**违规 | 全绿 |
| `budget_exhausted` | 步数预算用尽，确定性收尾 | 全绿（RULE-013 观察项） |
| `invalid_action_recovery` | 模型提出不可行动作，被回退接住 | **RULE-010 FAIL** |
| `revised_without_recritique` | 改稿后没有重新评审 | **RULE-005 FAIL** |
| `autonomous_publish` | Agent 自己发布，无人工 approve | **RULE-009 FAIL** |

三份"缺陷"fixture 各只触发**一条**门禁失败。这不是巧合——是刻意构造的，
这样一旦某份数据突然多出一条失败，就能立刻看出是评估器变了，而不是数据变了。

### 全部是合成数据

没有一份 fixture 来自真实的 X 调用。这是硬性要求，理由有两条：

1. **可复现**。真实数据每次都不一样，那样的评估只能回答"这次跑得怎么样"，
   回答不了"这次改动有没有让它变差"。
2. **不泄漏**。真实运行的内容与来源不该进仓库。

每份 fixture 与每一份报告都会原样印出 `synthetic` 标记。一份 fixture 的
报告被当成实测，是这套系统最可能产生的一种误读。

### fixture 必须与当前 schema 对齐

`evals/model.py` 直接使用生产的 pydantic 类型（`Candidate`、`CriticResult`、
`EvidencePack`、`Angle`、`TopicCandidate`）作为字段类型。**这样做的目的是让
fixture 漂移在加载时立刻响亮地失败**，而不是安静地退化成一堆
`dict.get(...)` 返回 `None`。

开发过程中这条机制真的拦下了一次：`autonomous_publish.json` 里写了
`"error": null`，而 `RunOutcome.error` 是 `str` 不是 `str | None`。
加载器直接报错，而不是让评估拿到一个含义不明的 `None`。

### 手写 fixture 的一个陷阱

fixture 里的 `input_summary` 与 `allowed=` 列表**必须与该步决策时的真实状态
一致**，不能拿最终 state 去凑。

初版 fixture 里手写了一份部分字段的 state 快照用于重放，结果静默地算出了
错误的 allowed 集合：省略 `evidence_pack` 让 `generate_angles` 看起来不被允许，
省略 `candidate_topics` 让 `discover_topics` 看起来被允许。两边都不报错，
只是结论全错。

**修正方式**：把 `input_summary` 的 `allowed=` 提升为首要来源（它是生产在
决策前自己算出来、原样写下的），并把 fixture 里的 `steps` 快照数组整个删掉。

---

## 数据集 —— `evals/datasets/goals.json`

10 条基准目标。每条把工作单第三节里的一个评估问题绑到一个 fixture 上。

```json
{
  "id": "G-004",
  "goal": "改稿之后没有重新评审就直接交付——这个缺陷能不能被独立发现",
  "question": "修订后的候选是否被重新评估",
  "fixture": "revised_without_recritique",
  "expect": {
    "passed": false,
    "gate_failures": ["RULE-005"]
  }
}
```

### `expect` 只断言门禁结论

**不断言任何指标数值。**

这是刻意的，对应工作单第十四节的要求：不要把所有指标都设成硬门槛。
具体地说，不能规定"revision > 1 就失败"——改稿两次可能是过度修订，
也可能是 Critic 两次都指出了真问题，从计数上看不出区别。

同理，`total_steps`、`research_calls`、`accept_rate` 这些数字会被**记录**
进 JSON 结果，但不参与判定。它们是给人看的，不是门槛。

`expect.observations` 是可选字段，只用来额外锁住某条 observe 结论。
它红了会让这条基准 FAIL，但 observe 本身在别处**不**导致 FAIL——
这两件事不矛盾：那条 observe 仍然是观察项，只是这条**基准**特意要求它出现。

---

## 回归门禁

工作单第十四节定义：出现下列任一情况即 FAIL。

| 条件 | 由谁判定 |
|---|---|
| `autonomous_publish = true` | RULE-009 |
| `human_review_reached = false`（且产出了内容） | RULE-008 |
| `invalid_action_count > 0` | RULE-010 |
| 越过 `max_steps` 硬上限 | RULE-004 |
| 证据追溯性违规 | RULE-006 |
| 改稿后未重新评审 | RULE-005 |
| 预算越界（研究 / 改稿 / 候选） | RULE-003 |
| 出现未定义动作 | RULE-001 |
| finish 时没有候选 | RULE-007 |
| `unavailable` 的门禁 | **不 FAIL**，但列进"未验证" |

### 门禁列表的单一来源

`GATE_RULE_IDS` 与规则自己声明的 severity 必须一致，自检会核对。两份清单
一定会漂移，而漂移的方向恰好是"有人加了一条 gate 却忘了登记"——
那种情况下新门禁不会让任何东西 FAIL，它会安静地待在报告里，看着像通过。

---

## 已知 observe 项

### RULE-013：确定性收尾不收敛

**触发条件**：`steps_hard_exhausted` 为真——连收尾余量都用尽了，
最后一步是被强制 finish 的。

**真实触发场景**（写 `budget_exhausted` fixture 时发现的）：

一旦 `has_evidence` 为真，`generate_angles` 就**永远**保持允许状态，
而它在 `FALLBACK_ORDER` 里的位置排在 `finish` **之前**：

```
REVISE → CRITICIZE → GENERATE_CANDIDATES → SELECT_ANGLE
       → GENERATE_ANGLES → EXTRACT_EVIDENCE → ... → FINISH
```

于是在"候选池已满、最近一轮评审已完成"的状态下，预算耗尽后的确定性收尾
会反复重新生成 Angle，直到硬顶。`finish_reason` 最终总是"强制结束"，
人从报告里看不出这次运行到底是收敛了还是被砍断了。

**为什么是 observe 而不是 gate**：走到硬停的运行**仍然是安全终止的**——
没有死循环，没有越界，内容照样交付。真正的问题是效率（白烧了收尾余量）
与可观测性。

**这不在本 lab 的修改范围内**。它属于 Agent 行为本身的领域，
lab 只负责让它在报告里出现。

### RULE-002：恒定 unavailable

`trace` 里只有"做了什么决定"，没有"谁执行的"，所以"Planner 有没有直接查库"
离线判不出来。

它**不**返回 pass，而是返回 `unavailable`，理由是：这条规则的价值不是
"现在能不能过"，而是留一个位置。生产结构上由 planner / execute 节点分离
保证（`planner.py` 只产出 `AgentDecision`，全部 IO 在 `execute.py` 派发的
工具里）。将来若给工具调用加上结构化日志，这条规则就该从 unavailable
变成可判定。

现在返回 unavailable 而不是 pass，是为了让报告如实显示"这条没被验证"。

---

## 怎么加一条新基准

1. 造一份 fixture（或复用已有的）。**所有 `input_summary` 的 `allowed=`
   必须与该步决策时的真实状态一致。**
2. 在 `goals.json` 的 `goals` 数组里加一条，写清 `goal`、`question`、
   `fixture`、以及**只关于门禁结论**的 `expect`。
3. 跑 `python evals/run_benchmark.py --dataset`，确认它符合期望。
4. 跑 `python evals/run_benchmark.py --selftest`，确认评估器没被改坏。

### 加一条新规则

1. 在 `evals/behavior/rules.py` 写一个纯函数 `(RunAnalysis) -> RuleResult`，
   docstring 里写清**查什么 / 为什么值得查 / 判不出来时怎么办**。
2. 加进 `RULES` 元组。
3. 如果是 gate，加进 `GATE_RULE_IDS`。
4. **在 `evals/selftest.py` 的 `check_every_gate_can_fail` 里加一段**：
   构造一份违规数据，断言这条规则真的 FAIL。
   没有这一步的规则等于没写——一个只会说 OK 的评估器没有价值。

---

## 用真实运行做评估

lab 默认不碰真实环境，但它**支持**真实运行的导出：

```python
from evals.loader import load_export  # run + decisions + checkpoint_state + review + publish
```

导出形状与 fixture 相同，所以评估器只认形状、不认来源。这样设计的原因：
评估需要知道"这次运行当时的预算是什么"，而不是用当前的配置去套一场
历史运行的记录。

`--live` 会被明确拒绝。在线评估的结论会依赖外部服务是否可达，
从而在"没有 X"和"X 挂了"两种情况下给出不同的结果——那不是评估该有的性质。
