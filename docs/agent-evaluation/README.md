# Agent Evaluation Lab

一套**离线、可复现**的评估系统，用来回答一个问题：

> 这次改动让 Agent 更自主、更稳定、更高效了吗？内容质量真的提升了吗？

它不开发 Agent 能力，也不参与生产流程。它只做一件事：把一次 Run 的行为
拆开、量出来、和上一次比。

---

## 快速开始

```bash
# 1. 跑全部 fixture，看每条运行的分数卡
python -m evals.run_benchmark

# 2. 按基线期望逐条判定（16 条基准目标）
python -m evals.run_benchmark --dataset

# 3. 自检：确认评估器本身还在工作
python -m evals.selftest

# 4. 存一份结果，改完代码后再跑一次，比较差异
python -m evals.run_benchmark --dataset --out before.json
#   ... 改代码 ...
python -m evals.run_benchmark --dataset --out after.json
python -m evals.compare before.json after.json
```

> 用 `-m` 而不是 `python evals/run_benchmark.py`。本机 conda 环境的
> site-packages 里有一个指向 **master 检出**的 `.pth`，按路径执行脚本时
> `import evals` 会解析到那一份——于是你跑的是另一个分支的评估器，
> 结果看起来完全正常。`-m` 让解释器从当前工作目录解析包，堵掉这条歧义。

不需要 PostgreSQL，不需要 X，不需要模型 API。整个 lab 是纯函数加上一组
JSON 文件，在任何机器上跑出来的结果逐位相同。

---

## 四条设计约束

这四条不是风格偏好，是这个系统能不能被信任的前提。

### 1. 只读生产代码，生产代码不读它

`evals/**` 单向依赖 `app/**`（复用生产的 `allowed_actions`、`steps_exhausted`、
`finish_reason`、以及全部 pydantic schema），反方向没有任何依赖。
生产代码里不存在 `import evals`。

这样做的直接后果是：**评估不会因为自己的 bug 让生产出问题**，而评估用到的
判定逻辑（什么叫"预算耗尽"、什么叫"可以收尾"）与生产**是同一份实现**，
不会因为两边各写一遍而分歧。

### 2. 判不出来就说 `unavailable`，不说 0

这是整个系统最重要的一条纪律。

`0` 是一个**测量结果**，"算不出来"不是。把一个算不出来的指标渲染成 0，
会让"不知道"在下一次比较中变成"变差了"，或者更糟——变成"改善了"。

所以：

- `rate(0, 0)` 返回 `"unavailable"`，不是 `0.0`；
- 求均值时 `unavailable` 被**跳过**，并单独报出样本数 `n`；
- 报告里每一项不可用的指标都带一句**为什么不可用**；
- 一条判不出来的**门禁**单独列在"未验证"里，它不导致 FAIL，
  但也**不**等于通过。

一句话：`unavailable` 和 `0` 在报告里必须长得完全不一样。

### 3. 不给总分

没有 `AgentScore = 87`。

理由不是"总分不好"，而是这五个维度**量纲不同**：安全是布尔的（发布出去了
就是发出去了），自主性是比例的，研究效率是"事实数 / 调用数"。把它们加权
求和，等于宣布"少发一次内容可以抵消多两次无意义循环"——这个等式不成立。

分数卡按维度分开列，每个数都能被单独解释、单独回溯。要判断"整体变好还是
变差"，人来看差异（`compare.py` 的输出），而不是让一个数替你决定。

### 4. 不让 LLM 给内容质量打真值

内容质量的**自动指标**是确定性的字符串与统计量（泛化短语表、句长方差、
具体实体计数、n-gram 重复、编辑距离），它们负责**筛**；
**人工标注**（`human_ai_style_rating` 等八个维度）是**真值**。

两者并置，不互相替代。让模型给自己的输出打质量分，得到的是一个既不可复现
也不权威的数字——而且它会系统性地偏高。

---

## 目录结构

```
evals/
  run_benchmark.py     入口：跑 fixture / 跑数据集 / 自检
  compare.py           比较两份结果
  selftest.py          评估器自己的测试
  analysis.py          trace → 逐步可判定视图（所有下游指标的公共地基）
  trace_marks.py       生产那几句固定措辞，以及它们的解析
  model.py             评估的输入模型（用生产 pydantic 类型，schema 漂移会响亮失败）
  loader.py            读 fixture / 读真实导出
  scorecard.py         AgentRunEvaluation：五个维度 + 门禁结论
  report.py            终端渲染
  values.py            MetricValue / UNAVAILABLE / rate / mean
  structural/          第一层：结构指标
  behavior/            第二层：行为规则 RULE-001..018
  content/             第四层：内容质量（确定性指标）
  fixtures/            离线基准数据（全部标注为 synthetic）
  datasets/goals.json  16 条基准目标
```

`docs/agent-evaluation/` 下有四份文档：

| 文件 | 内容 |
|---|---|
| [`metrics.md`](metrics.md) | 四个层次分别量什么，每个指标怎么算、来源是什么 |
| [`benchmark.md`](benchmark.md) | 数据集、fixture、回归门禁、怎么加一条新基准 |
| [`content-quality.md`](content-quality.md) | 内容质量的八个维度：定义、怎么量、不能怎么量 |

---

## 四个层次

| 层 | 位置 | 回答 |
|---|---|---|
| 一、结构指标 | `evals/structural/` | 走了几步、研究几次、改稿几次、预算有没有耗尽 |
| 二、行为规则 | `evals/behavior/` | 有没有提出不可行动作、改稿后有没有重评、有没有自动发布 |
| 三、分数卡 | `evals/scorecard.py` | 五个维度**分开**看：自主性、研究、写作、评审、安全 |
| 四、内容质量 | `evals/content/` | 具体性、立场、证据绑定、套话密度、模板相似度… |

上三层是**行为**评估（Agent 做得对不对），第四层是**产出**评估
（写出来的东西好不好）。两者不能互相替代：一次行为完全合规的运行，
照样可以产出一篇套模板的空话。

---

## 这份 lab 抓到的第一件事

写 `budget_exhausted` 这份 fixture 时发现的一个真实行为：

一旦 `has_evidence` 为真，`generate_angles` 就**永远**保持允许状态，而它在
`FALLBACK_ORDER` 里排在 `finish` 之前。于是在"候选池已满、最近一轮评审已完成"
的状态下，预算耗尽的运行会反复重新生成 Angle，直到硬顶才被强制 finish，
**永远不会靠自己走到出口**。

这不会报错，也不会死循环（硬顶是有效的），所以它此前是不可见的。现在它由
**RULE-013**（观察项，不 FAIL）标记出来。

这属于 Agent 行为本身的领域，不在本 lab 的修改范围内——lab 只负责让它在
报告里出现。见 [`benchmark.md`](benchmark.md#已知-oobserve-项)。

---

## 边界

- **不做在线评估**。`--live` 会被明确拒绝（见 `run_benchmark.py` 的说明）。
  在线评估的结论会依赖外部服务是否可达，那不是评估该有的性质。
  要评估真实运行，把它**导出**成 `evals/loader.py` 认识的形状再喂进来。
- **不改生产代码**。生产侧的缺陷由 lab 发现并报告，修复是另一个会话的事。
  评估不应该依赖被评估者的修复来发现问题——否则那个修复没做对时也没人知道。
- **fixture 全部是合成数据**。每份 fixture 与每一份报告都会原样印出
  `synthetic` 标记。一份 fixture 的报告被当成实测，是这套系统最可能
  产生的一种误读。
