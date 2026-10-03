"""发布前的确定性内容门禁。

规则**只实现一次**：`evaluate_content_gate()` 被两处调用——

1. `app/agent/nodes/guardrail.py::evaluate`：**信息性**。命中的原因码被写进
   `guardrail_reasons` 交给人工审核者，但**不阻断 Graph**——这是 Phase 3 定下的
   契约，本模块不改它。
2. `app/services/publish.py::_preflight`：**阻断**。命中即 `OUTCOME_INVALID`，
   接口返回 422，且不向 X 发出任何请求。

两处调同一个函数，是因为"审核时看到的理由"和"发布时拦下的理由"必须是同一张清单。
各写一份、各自演化，最后会变成"审核说没问题、发布说不行"——而那种不一致，人只会
在读到一个 422 的时候才发现。

## 只判能算准的

门禁只拦两件事：正文里泄漏了内部事实编号，以及无歧义的低质点击诱饵词。

段落数、首句形态、句子长度**不**在这里判，虽然它们也都能算：

- `split_paragraphs` 只按空行切，推文式草稿几乎总是单段，"≥2 段"会拦掉接近全部
  的稿子；
- `opening_pattern` 只判表层形态（问句 / 数字 / 引号），判不了"抓不抓人"，而新的
  阅读契约本来就允许陈述式钩子。

"抓不抓人"是语义判断，归 critic 与人工，不归一条正则可以裁决的东西——把它写成
门禁，只会把一批合格的稿子拦在门外，而把不合格的放进去。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.style.features import find_clickbait, find_internal_refs

#: 原因码。与 guardrail 既有的 `too_long` / `duplicate_content` 同一层。
#: **只在违规时出现**：guardrail 的测试断言的是一个精确列表，干净内容必须一个
#: 码都不产出。
REASON_INTERNAL_REF = "internal_ref_leak"
REASON_CLICKBAIT = "clickbait_phrase"


@dataclass(frozen=True)
class GateViolation:
    """一条命中的门禁规则：原因码 + 给人看的说明。

    说明里带上**命中的原文**（编号本身、诱饵词本身）而不是只说"有问题"：
    人被 422 拦下时要能一眼看到是哪个词、哪个编号触发的，否则只能去猜。
    """

    code: str
    detail: str


def evaluate_content_gate(
    content: str | None, *, fact_ids: Iterable[str] | None = None
) -> list[GateViolation]:
    """对正文执行发布门禁。干净内容返回空列表。

    `fact_ids` 是本轮证据包里的事实编号（`evidence_index(pack).fact_ids`），
    只用于判断**裸形**编号（`f18`）算不算泄漏——见 `find_internal_refs`。
    拿不到证据时传 `None`，此时只启用零假阳性的括号形。
    """
    text = content or ""
    violations: list[GateViolation] = []

    refs = find_internal_refs(text, fact_ids=fact_ids)
    if refs:
        violations.append(
            GateViolation(
                code=REASON_INTERNAL_REF,
                detail=(
                    "正文里出现了内部事实编号（" + "、".join(refs) + "）。"
                    "编号是内部核对证据用的，读者看不懂——去掉编号，直接说那件事本身。"
                ),
            )
        )

    hits = find_clickbait(text)
    if hits:
        violations.append(
            GateViolation(
                code=REASON_CLICKBAIT,
                detail="正文里出现了低质点击诱饵词（" + "、".join(hits) + "）。",
            )
        )

    return violations
