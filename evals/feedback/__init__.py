"""Feedback Loop Foundation 的离线基准（Phase 5B.4）。

跑法：

    python -m evals.feedback.selftest

这一套量的是 `app/feedback/` 那四个纯模块：观察 → 信号 → 报告 → 进 checkpoint
的上下文。它与 `evals/behavior` 的分工是**尺子与刻度**的关系：
`RULE-024` 只回答"这次运行读到的反馈是不是只描述观察"，而"一个未知的计数
有没有被当成 0""低于下限时到底产出了什么"这些刻度问题在这里逐条量。

全程离线：不碰 X、不碰 LLM、不读数据库、不取挂钟时间。观察是构造出来的，
所以任何一条失败都能在一秒内复现。
"""
