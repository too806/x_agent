"""Learning Readiness（5C）与 Learning Proposal（5D）——从反馈到"可以试试什么"。

这是继 Trend / Style / Viewpoint / Feedback 之后的**第五层**。它分两段，两段都
**不产出任何会进入生产的内容**：不写正文、不改 prompt、不调权重。

    5C  readiness   我过去发出去的东西、后来被观察到的结果，够不够可靠？
    5D  proposal    够可靠的话，未来**可以尝试**什么变化？（只是提出来）

## 5D 的位置：观察 / 建议 / 执行，三件事

    Observation   过去发生了什么          ——已存在（5C 的证据与报告）
    Proposal      可以尝试什么            ——本包 5D 新增，止步于此
    Recommendation 应该做什么             ——本层**没有**

5D 只做 Observation → Proposal。提案带 `status: proposed`，等人裁决；人批了之后
状态变成 `approved`，而 `approved` **不等于被应用**——本阶段不实现应用。

## 它到此为止

本包**不做**任何自动动作：不改 prompt、不改 StyleRecipe、不改 Viewpoint 规则、
不改 Trend 权重、不改 Critic threshold、不改预算、不改 Planner、不自动发布、
不做微调、不引入 embedding / 向量库 / 多智能体 / A/B 实验 / 调度器。
评价一条改动是否越界，只需要问一句：**它有没有依据反馈去改变系统的行为？**
有，就越界了。

包内**没有** `apply_learning()`，也没有任何 `apply_*` / `execute_*`——而且不该有。
5D 新增的 `generate_proposals()` 只是产出结构化建议，`validate_proposal()` 只是
判断一条建议合不合规；两者都不碰生产配置。`automatic_modification` 与
`automatic_application` 是**恒假的字段**而不是约定，这样测试能把它钉住。

## 提案能碰什么、不能碰什么

    能（v1）  StyleRecipe.hook_type / structure_type / target_length、
              Viewpoint 的 counterpoint_awareness / stance
    不能      Trend 权重、Guardrail 与安全规则、预算、发布逻辑、Critic 安全阈值、
              人工审核与 CAS

后一类不是"暂时不做"：`validate_proposal()` 见到这些目标一律判 invalid，
命中时写明它踩的是哪条生产边界。理由很直接——那些旋钮的取值关系到"发不发得出去、
发出去会不会出事"，而本层的证据来自**已经发出去**的帖子，天生看不到这些风险。

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
`proposal.py`（5D）从报告推候选建议并自查它合不合规；
`runtime.py`（唯一碰数据库/checkpoint 的模块）**只读**装配。
除 `runtime.py` 外全部是纯的：没有 IO、没有随机、没有 `datetime.now()`。

`runtime.py` 只读这件事由 `evals/learning/selftest.py` 的 AST 扫描守着，
它同时植入一次 `session.add()` 证明自己看得见写操作。
"""

__all__: list[str] = []
