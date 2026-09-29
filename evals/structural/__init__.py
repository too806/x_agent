"""第一层评估：Structural Evaluation。

只回答"这次运行长什么样"——步数、各类动作的次数、预算是否用尽、结束时说了什么。
**不做判断**（那是 behavior 层的事），也**不看内容**（那是 content 层的事）。

这一层之所以独立，是因为它是后两层的分母：行为规则的"非法动作率"要用这里的
总步数，内容指标要挂到这里的候选数上。"跑了几步"只有一个答案，就该只有一个来源。
"""

from evals.structural.metrics import STRUCTURAL_FIELDS, StructuralMetrics, compute_structural

__all__ = ["STRUCTURAL_FIELDS", "StructuralMetrics", "compute_structural"]
