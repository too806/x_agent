"""Learning Readiness（Phase 5C）——判断 Feedback 是否已经足够可靠，可以进入未来学习。

这是继 Trend / Style / Viewpoint / Feedback 之后的**第五层**，也是唯一一层
**不产出任何内容**的层：它不写正文、不改 prompt、不调权重，只回答一句话：

    我过去发出去的东西、后来被观察到的结果，够不够可靠，能拿去学习？

## 它到此为止

本包**不做**任何自动动作：不改 prompt、不改 StyleRecipe、不改 Viewpoint 规则、
不改 Trend 权重、不改 Critic threshold、不改预算、不改 Planner、不自动发布、
不做微调、不引入 embedding / 向量库 / 多智能体 / A/B 实验 / 调度器。
评价一条改动是否越界，只需要问一句：**它有没有依据反馈去改变系统的行为？**
有，就越界了。

包内**没有** `apply_learning()`，而且不该有：本阶段只存在
`evaluate_learning_readiness()`——它产出一个确定性的准入开关，
`learning_ready=True` 只表示"可以作为未来学习材料"，不表示"立即改变 Agent"。

## 五条互相独立的层

    Trend             = 当前外部世界在发生什么
    Style             = 外部语料是怎么写的
    Viewpoint         = 在现有证据下一个判断站不站得住
    Feedback          = 我们自己过去的行为，实际带来了什么可观察的结果
    Learning Readiness = Feedback 是否已经足够可靠，可以进入未来学习

做成独立包（而不是塞进 `app/feedback/`）是为了让"它是独立的一层"在目录结构上
可见——放在 `app/feedback/` 里，迟早会有人想"顺手让反馈去改点东西"。

## 本包最容易被写坏的一条：分析单位

**分析单位是帖子，不是快照。** 同一条帖子的 1h / 6h / 24h 三条快照是同一条帖子的
重复观察。把它们当成三个样本，是小样本上造出"显著差异"最快的一条路。所以每个帖子
在一个观察窗口里只贡献一个值，`sample_count` 一律指帖子数。真实数据 1 post /
2 snapshots 因此是 n=1，正确地够不上比较下限。

## 模块分工

`schema.py` 结构化契约；`performance.py` 窗口取值与分布；`cohort.py` 分组与比较；
`evidence.py` 证据与准入开关（learning gate）；`report.py` 组装与出口校验；
`runtime.py`（唯一碰数据库/checkpoint 的模块）只读装配。
除 `runtime.py` 外全部是纯的：没有 IO、没有随机、没有 `datetime.now()`。
"""

__all__: list[str] = []
