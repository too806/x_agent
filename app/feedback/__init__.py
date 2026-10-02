"""Feedback Loop Foundation（Phase 5B.4）。

这一层的职责只有一个：**第一次让 Agent 看到自己过去发出去的东西后来怎么样了**
——真实发布结果 → 采集 → 归因 → 可解释信号 → Agent 可读。

它到此为止。本包**不做**任何自动动作：不改 Prompt、不改 StyleRecipe、
不改预算、不改策略、不自动发布、不做微调、不做多智能体、不做向量检索、
不做 A/B 实验、不引入调度器。评价一条改动是否越界，只需要问一句：
**它有没有依据反馈去改变系统的行为？** 有，就越界了。

## 四条互相独立的层

    Trend     = 当前外部世界在发生什么
    Style     = 外部语料是怎么写的
    Viewpoint = 在现有证据下一个判断站不站得住
    Feedback  = 我们自己过去的行为，实际带来了什么可观察的结果

Feedback 是第四个。把它做成独立包（而不是塞进 `app/agent/`）就是为了让
"它是独立的一层"这件事在目录结构上可见——放在 `viewpoint.py` 旁边，
迟早会有人想"顺手把反馈并进观点分"。

## 三条不写在别处的纪律

1. **未知是 `None`，永远不是 0**（`metrics.py` / `schema.py`）。
2. **只描述观察，绝不声称因果**（`signals.py::assert_no_causal_claim`）。
3. **低于样本下限就什么都不比**，而且不把下限伪装成显著性
   （`signals.py::MIN_SAMPLES_FOR_COMPARISON`）。

`profile.py`（归因）与 `runtime.py`（取用入口）是仅有的两个会碰到
数据库/checkpoint 的模块；其余三个（`schema` / `metrics` / `signals` / `report`）
是纯的：没有 IO、没有随机、没有 `datetime.now()`。
"""
