"""Style & Structure Intelligence（Phase 5B.2）。

这一层的职责是把"中文科技内容该怎么写"从**真实样本**里统计出来，变成一组
**可解释、可复用、可调整的生成约束**，而不是一段模板或某个人设的模仿。

三个刻意的边界：

- **确定性留在 Python，语义才交给 LLM。** 长度、段落、标点、emoji、数字、
  英文比例这些一律由 `features.py` 算；LLM 只负责归类与标注（`annotate.py`），
  且只准输出结构化 JSON。
- **Writer 永远不读样本原文。** 它收到的是 `StyleRecipe`（见 `recipe.py`），
  是一组约束，不是任何一条真实帖子的正文。这条由评估规则机器守着。
- **样本原文不出现在 Agent 运行期的 state 里。** 采集/统计/提炼是 offline 的
  （`corpus.py` + `profile.py`），运行期只消费算好的 Profile。
"""
