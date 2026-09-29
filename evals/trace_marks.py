"""决策轨迹里那几个**由生产代码写下的固定措辞**，以及它们的识别方式。

## 为什么需要这个模块，而不是直接读 state

判断"Planner 提了一个不可行的动作"最可靠的办法是**重放**：拿着每一步的 state
调 `allowed_actions()`，看当时的动作在不在里面。这需要每一步的 state 快照——
fixture 有（我们可以构造），但从数据库/checkpoint 导出的真实 trace **没有**：
`agent_decision` 表存的是 `input_summary`（"当时手上有什么"的一行摘要），
不是结构化的 state。

于是真实 trace 上只剩一个可观测信号：Planner 在换掉动作时**一定会留下原因**。

    app/agent/nodes/planner.py:123-129
    AgentDecision(
        action=fallback,
        reason=f"（原决策 {decision.action.value} 在当前状态下不可行，"
               f"按回退顺序改为 {fallback.value}）{decision.reason}",
    )

    app/agent/nodes/planner.py:80-84（步数预算用尽）
    reason=f"决策步数预算已用尽（{budget.max_steps} 步），按回退顺序收尾：..."

    app/agent/nodes/planner.py:72-75（连收尾余量也用尽）
    reason=f"步数与收尾余量均已用尽（{budget.max_steps}+{WRAPUP_ALLOWANCE}），强制结束"

## 另一条路：`input_summary` 里的 `allowed=` —— 但它回答的是**另一个问题**

**每一步的 `input_summary` 里都写着当时的 allowed 集合**：

    app/agent/nodes/planner.py:46-64  summarize_input()
    "allowed=discover_topics,research_topic; step=1/12; research=1/4; ..."

那份摘要是 Planner **在决策之前**用生产的 `allowed_actions()` 算出来并原样写下的。

但注意它回答的是"**最终执行的那个动作**合不合法"，因为 Planner 写摘要时
就已经把动作换好了。回退之后的动作当然在 allowed 里——所以拿它去判
"模型原本想干什么"永远得到"合法"。**这条来源不能用来抓非法动作。**

它的正确用途有两处：

1. 检查**最后执行的动作**是否落在当时的允许集合内（数据自洽）；
2. 与回退标记**交叉验证**：标记说"原决策 X 不可行"，那就该有 X ∉ allowed。
   两者矛盾说明 trace 被改过，或者评估用错了当时的预算。

见 `evals/analysis.py` 里 `invalid` 与 `executed_legal` 两个字段的分工说明。

判定来源与它们各自回答的问题：

| 来源 | 回答 | 用在哪 |
|---|---|---|
| 回退标记 | 模型原本提议的动作被否决了吗 | `invalid` / RULE-010 |
| `input_summary` 的 `allowed=` | 最终执行的动作当时合法吗 | `executed_legal` |
| state 快照重放 | 同上，fixture 才有的增强数据 | `executed_legal` 兜底 |

## 这个做法的风险，以及为什么仍然可接受

从自然语言里解析状态是脆的：改一次措辞，指标会**静默变成 0**——这正是
`app/agent/state.py` 里 critic_tally 拒绝从 `result_summary` 反推的理由。
所以这里做了三件事，而不是假装看不见：

1. 所有格式标记集中在这一个模块，正则**只有一份**，要改只改一处；
2. `evals/selftest.py` 用**与生产逐字相同**的字符串去跑这些正则，措辞一变
   自检就红，而不是指标悄悄归零；
3. 报告里每个由标记推出的指标都带 `method` 字段，写明它用的是哪条路，
   人看结论时知道该给多少信任。

第 3 条尤其重要：`input_summary` 解析失败（例如旧版本没写这个字段）时会**降级**
到下一条来源，而不是当成"没有非法动作"。
"""

import re

# --- 生产措辞（逐字摘自 app/agent/nodes/planner.py）---

#: 原决策不可行，被确定性回退替换。
FALLBACK_TEMPLATE = "（原决策 {action} 在当前状态下不可行，按回退顺序改为 {fallback}）"

#: 决策步数预算用尽，之后不再调用 LLM，按 FALLBACK_ORDER 收尾。
BUDGET_EXHAUSTED_MARK = "决策步数预算已用尽"

#: 连 WRAPUP_ALLOWANCE 也用尽，强制 finish。
HARD_EXHAUSTED_MARK = "步数与收尾余量均已用尽"

_FALLBACK_RE = re.compile(
    r"原决策\s*(?P<action>[a-z_]+)\s*在当前状态下不可行，按回退顺序改为\s*(?P<fallback>[a-z_]+)"
)


def parse_fallback(reason: str) -> tuple[str, str] | None:
    """从一条 reason 里读出 (模型原本想做的动作, 实际执行的动作)。

    不是回退步就返回 None——**不是**返回 ("", "")。调用方要靠 None 区分
    "这一步不是回退"和"这一步是回退但没解析出来"，后者是要报警的坏数据。
    """
    match = _FALLBACK_RE.search(reason or "")
    if match is None:
        return None
    return match.group("action"), match.group("fallback")


def is_budget_exhausted_reason(reason: str) -> bool:
    """这一步是不是"步数预算用尽后按回退顺序收尾"的确定性步骤。"""
    return BUDGET_EXHAUSTED_MARK in (reason or "")


def is_hard_exhausted_reason(reason: str) -> bool:
    """这一步是不是"连收尾余量都用尽"的强制结束。"""
    return HARD_EXHAUSTED_MARK in (reason or "")


_ALLOWED_RE = re.compile(r"(?:^|;\s*)allowed=([^;]*)")


def parse_allowed(input_summary: str) -> frozenset[str] | None:
    """从 `input_summary` 里读出当时允许的动作集合。

    返回 `None` 的两种情况**必须区分对待**，这是本函数唯一容易做错的地方：

    - 压根没有 `allowed=` 字段（旧版本记录、或不是 planner 写的摘要）；
    - 有 `allowed=` 但值是空的。

    两者都返回 `None`，但理由不同：前者是"这份数据里没有答案"，
    后者是"生产当时一个动作都不允许"——那正是 `pick_fallback` 兜底返回
    `finish` 的退化路径（`app/agent/budget.py` 的说明）。此时记录下来的
    `finish` **不在**任何集合里，若把空集合当成"合法的空集"，
    会把这一步误判成非法动作。所以空集合一律当作"判不了"，交给下一条来源。
    """
    match = _ALLOWED_RE.search(input_summary or "")
    if match is None:
        return None
    names = frozenset(part.strip() for part in match.group(1).split(",") if part.strip())
    return names or None


def is_deterministic_reason(reason: str) -> bool:
    """这一步的决策**没有经过 LLM**。

    只有这两种：预算用尽后的收尾、收尾余量用尽后的强停。两者都由
    `pick_fallback` 产生（纯函数），模型没有被问过。
    """
    return is_budget_exhausted_reason(reason) or is_hard_exhausted_reason(reason)


def fallback_reason(action: str, fallback: str, original: str = "") -> str:
    """按生产模板拼一条回退 reason。

    **只给 fixture 与自检用**。放在这里而不是散在 fixture 生成代码里，
    是为了让"生产怎么写"和"我们怎么造"共用同一个模板：模板改了两边一起改，
    自检立刻能发现解析器失配。
    """
    return (
        FALLBACK_TEMPLATE.format(action=action, fallback=fallback) + original
    )
