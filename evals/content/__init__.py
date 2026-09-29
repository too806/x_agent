"""第四层评估：Content Quality Evaluation。

这一层只**定义评估**，不接入生产，也不推导"AI 味"的最终真值。
它的产出是给人看的观察值，用来回答"这条稿子为什么读起来像模板写的"。

与前三层的区别：前三层判的是行为（可判定、可门禁），这一层判的是文本
（只能测量、不能判定）。所以这里没有 gate，只有指标 + 人工标注位。

本阶段**不做** Style Distillation——那是另一个问题（怎么改），
这一层只负责把"哪里不对"变成可比较的数字。见
`docs/agent-evaluation/content-quality.md`。
"""

from evals.content.ai_style import AiStyleReport, analyze_style, human_edit_distance

__all__ = ["AiStyleReport", "analyze_style", "human_edit_distance"]
