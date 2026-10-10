"""Agent Evaluation Lab（Phase 5A.1 之外的独立评估层）。

这个包**只读**生产代码，生产代码不 import 它。它回答的问题与
`app/agent/metrics.py` 不同：

- `app/agent/metrics.py` 是**运行时的自述**：一次 Run 结束时，它自己的数字是多少。
  它跑在服务进程里，只读当前 checkpoint，给控制台用。
- 本包是**离线评估**：给定一次 Run 的 trace（来自 fixture 或导出的真实数据），
  判断这次运行**是否违反了行为契约**，并给出可比较的维度指标。

两者的数字会重叠（步数、研究次数），但**不能合并**：把它并进生产 schema 会让
"评估结论"变成"业务状态"，而评估标准是会随认知变化的东西——它不该进数据库。

三条硬约束（本包全部代码遵守）：

1. **不调用 LLM**。所有指标都是确定性的纯计算。任何"让模型给个分"的做法都会
   引入不可复现的评分，而评估体系的第一要求是可复现。
2. **不调用 X**。fixture 就是 fixture，不联网。
3. **不改生产代码**。本包只 import，不 patch。
"""

__all__ = ["__version__"]

# 评估模型的版本。指标口径一旦变化就要进位，否则两份 evaluation JSON 会因为
# 口径不同而被 compare.py 当成"Agent 变了"——那是这类工具最典型的假阳性。
#
# 1.1.0：`app/style/features.py` 新增 `CLICKBAIT_PHRASES` 与 `find_internal_refs`。
# 1.2.0：`EMPTY_CONNECTIVES` 补进一批"总结腔"连接词（这意味着 / 本质上 /
#   换句话说 …）。同一段正文的 `empty_connective_count` 会因此变高，
#   两份 JSON 不进位就会被误读成"Agent 的 AI 味变重了"。
# 该文件明写"改词表要同时进位这里"，两者必须是同一次进位。
__version__ = "1.2.0"
