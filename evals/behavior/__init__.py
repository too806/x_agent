"""第二层评估：Agent Behavior Evaluation。

把"Agent 应该怎样做"写成**可判定的规则**，然后逐条检查一条 trace。

## 这不是生产 Guardrail

生产 Guardrail（`app/agent/nodes/guardrail.py`）拦的是**内容**，跑在线上的图里，
结论会阻断发布。这里拦的是**行为**，跑在离线的一条已完成的 trace 上，
结论只影响评估报告。

两者容易混，所以说明白区别在哪：

| | 生产 Guardrail | 本层 Evaluation Rule |
|---|---|---|
| 跑在哪 | Graph 节点内，每次运行 | 离线，对已有 trace |
| 拦什么 | 内容（长度、重复、违禁词） | 行为（越界动作、缺证据、跳过评审） |
| 拦错了会怎样 | 一条内容被误判 | 一份报告标红 |
| 谁改它 | 产品/合规 | 评估体系自己 |

正因为"拦错了只是标红"，这一层可以比生产**严格**。典型例子是 RULE-005：
生产 `finalize.pick_final_candidate` 明确容忍"改稿后没重评"（它的 docstring
把这条列为已知取舍），而评估把它算**硬失败**——因为那是一个会静默降低质量的
行为，容忍它是权宜，不是标准。

## 两条严重度

- `gate`：失败即整次评估 FAIL。只有"这个行为发生就说明流程坏了"的规则才是 gate。
- `observe`：只记录，不影响通过与否。

刻意**不**给所有规则都设硬门槛。真实 Agent 可能合理地多做一次修订，
把"修订次数 > 1 就失败"这种阈值写死，等于用评估体系否定掉一个正确的决策。
"""

from evals.behavior.rules import (
    GATE_RULE_IDS,
    RULES,
    BehaviorReport,
    RuleResult,
    evaluate_rules,
    gate_rules,
)

__all__ = [
    "GATE_RULE_IDS",
    "RULES",
    "BehaviorReport",
    "RuleResult",
    "evaluate_rules",
    "gate_rules",
]
