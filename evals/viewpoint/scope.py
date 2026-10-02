"""观点评估的**确定性检测器**：离线基准与真机 smoke 共用同一份。

## 为什么要有这个文件

`cases.py` 的 `expect` 断言的是"算出来的数是多少"，而真机 smoke 要回答的是
一个不同的问题："这次真跑出来的观点，有没有毛病？"如果那两件事各写一份实现，
就会出现最糟的那种局面——离线全绿、真机上没人看得懂哪里不对，而且两份判据
会慢慢漂开，漂的过程不报错。

所以判据只写一份：`problems()`。离线基准拿它证明"这类数据必须报警、那条对照
数据必须不报警"，真机 smoke 拿它把每次运行的结论逐条打出来。两边跑的是同一个
函数、同一组阈值。

`observe()` 只做一件事：把 `(证据包, 材料, 草稿)` 解成 `app/agent/viewpoint.py`
要的索引与分数。**打分本身一行都不在这里**——那是生产代码，这里不许有第二份。

## 与生产的边界

这个模块**不重新实现任何阈值**，全部 `from app.agent.viewpoint import`。
新写一个数就会多一个漂移点，而且漂的那一天不会有任何测试变红。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.agent.schemas import ViewpointStance
from app.agent.viewpoint import (
    MIN_EVIDENCE_SUPPORT_FOR_FULL_SCORE,
    EvidenceIndex,
    ViewpointScore,
    evidence_index,
    score_viewpoints,
)

# ---------------------------------------------------------------------------
# 问题码
# ---------------------------------------------------------------------------

#: 判断没有绑定任何真实事实。工作单第七节：核心论点需要至少一条证据。
NO_EVIDENCE = "no_evidence"
#: 支撑度低于"低支撑"门槛——证据包里有一堆事实，这条判断只绑了很少几条。
UNSUPPORTED = "unsupported"
#: 申报范围（或正文里的全称量词）超出证据能支撑的范围。工作单第二十八、二十九节。
OVERCLAIM = "overclaim"
#: 证据包里已经写明有反方，这条判断一条都没回应。工作单第八节。
COUNTERPOINT_IGNORED = "counterpoint_ignored"
#: 写了反方，但一条都引不到真实事实——是姿态，不是论证。
COUNTERPOINT_UNBOUND = "counterpoint_declared_unbound"
#: 多条事实只覆盖一条来源。量的多少不能替质的不足说话。
SINGLE_SOURCE = "single_source"
#: 用了"全网/首创"这类本系统无法核验的表述。工作单第三十一节。
UNVERIFIABLE_ORIGINALITY = "unverifiable_originality"
#: 置信度只支持到 uncertain。**不是错误**——工作单第九节要求允许这样；
#: 报出来是为了让"这次没有强观点"在报告里是一句有依据的话，而不是沉默。
LOW_CONFIDENCE = "low_confidence"

PROBLEM_CODES: tuple[str, ...] = (
    NO_EVIDENCE,
    UNSUPPORTED,
    OVERCLAIM,
    COUNTERPOINT_IGNORED,
    COUNTERPOINT_UNBOUND,
    SINGLE_SOURCE,
    UNVERIFIABLE_ORIGINALITY,
    LOW_CONFIDENCE,
)

#: 少于这个条数就不谈"单一来源"——一条事实一条来源是**正常**的最小证据，
#: 对它报单一来源只会训练读者忽略这个码。
MIN_FACTS_FOR_SINGLE_SOURCE_NOTE = 2


@dataclass(frozen=True)
class Problem:
    """一条判出来的问题。`code` 是机器读的，`detail` 是人读的。"""

    code: str
    position: int
    claim: str
    detail: str

    def render(self) -> str:
        return f"[{self.code}] 第 {self.position + 1} 条「{self.claim}」：{self.detail}"


@dataclass(frozen=True)
class Observation:
    """一次评估：解好的索引 + 逐条草稿的确定性分数。"""

    index: EvidenceIndex
    scores: tuple[ViewpointScore, ...]

    def score(self, position: int = 0) -> ViewpointScore:
        return self.scores[position]

    def codes(self, position: int | None = None) -> tuple[str, ...]:
        """问题码。给了 `position` 就只看那一条草稿。"""
        found = problems(self)
        if position is None:
            return tuple(p.code for p in found)
        return tuple(p.code for p in found if p.position == position)


def observe(
    pack: Any,
    materials: Mapping[str, Any] | Sequence[str],
    drafts: Sequence[Any],
    *,
    total_facts: int | None = None,
) -> Observation:
    """把 `(证据包, 材料, 草稿)` 解成一份 `Observation`。

    `materials` 是 `{来源 id: 材料}`。用映射而不是两个平铺的序列，是因为
    `author_index` 靠 `zip(item_ids, results)` 对齐——两个序列分开传，
    总有一次会被传成反的，而反了之后的表现是"作者数算错"，不报错。
    这里由本函数同时产出两个序列，那个对齐关系就没有交出去的机会。
    """
    if isinstance(materials, Mapping):
        item_ids = list(materials)
        results = [materials[item_id] for item_id in item_ids]
    else:  # pragma: no cover - 兼容只关心 id 的调用方
        item_ids = list(materials)
        results = []

    index = evidence_index(pack, item_ids, results)
    scores = score_viewpoints(list(drafts), index, total_facts=total_facts)
    return Observation(index=index, scores=tuple(scores))


def observe_case(case: Any) -> Observation:
    """从 `cases.Case`（或任何有同名属性的对象）跑一次。

    不 import `cases`：`scope` 保持对"数据长什么样"零依赖，而 smoke 脚本
    手里的对象也不一定是 `Case`。
    """
    return observe(case.pack, case.materials, case.drafts)


def problems(observation: Observation) -> tuple[Problem, ...]:
    """逐条列出这份评估里的问题。**只判，不改分**。

    与 `viewpoint.py` 的分工：那边算出数并写下理由，这边读那些数、用同一组
    生产阈值判"这算不算有问题"。所以这里的每一条都能在 `reasons` 里找到
    对应的量，人能复核，而不是一个不可解释的红叉。
    """
    found: list[Problem] = []
    for position, score in enumerate(observation.scores):
        found.extend(_problems_of(score, observation.index, position))
    return tuple(found)


def _problems_of(score: ViewpointScore, index: EvidenceIndex, position: int) -> list[Problem]:
    out: list[Problem] = []

    if score.bound_facts == 0:
        out.append(
            Problem(
                NO_EVIDENCE,
                position,
                score.claim,
                "这条判断没有绑定任何真实事实（工作单第七节：核心论点需要至少一条证据）",
            )
        )
    elif score.evidence_support < MIN_EVIDENCE_SUPPORT_FOR_FULL_SCORE:
        out.append(
            Problem(
                UNSUPPORTED,
                position,
                score.claim,
                f"支撑度 {score.evidence_support:.4f} 低于 "
                f"{MIN_EVIDENCE_SUPPORT_FOR_FULL_SCORE}：证据包里有 {score.total_facts} "
                f"条事实，这条判断只绑了 {score.bound_facts} 条",
            )
        )

    if score.scope_exceeds_evidence:
        if score.claim_scope is not None and score.evidence_scope is not None:
            gap = (
                f"申报范围 {score.claim_scope.value} 超过证据能支撑的 "
                f"{score.evidence_scope.value}"
            )
        else:
            gap = "模型没有申报范围，而正文出现全称量词"
        markers = "、".join(score.breadth_markers)
        out.append(
            Problem(
                OVERCLAIM,
                position,
                score.claim,
                f"{gap}"
                + (f"；正文全称量词：{markers}" if markers else ""),
            )
        )

    if index.counterpoints and score.counterpoint_awareness <= 0:
        out.append(
            Problem(
                COUNTERPOINT_IGNORED,
                position,
                score.claim,
                f"证据包里写明有 {len(index.counterpoints)} 条反方说法，"
                "这条判断一条都没有回应",
            )
        )
    elif score.declared_counterpoints > 0 and score.bound_counterpoints == 0:
        out.append(
            Problem(
                COUNTERPOINT_UNBOUND,
                position,
                score.claim,
                f"写了 {score.declared_counterpoints} 条反方，但一条都引不到真实事实"
                "——是姿态，不是论证",
            )
        )

    if (
        score.bound_facts >= MIN_FACTS_FOR_SINGLE_SOURCE_NOTE
        and score.bound_sources <= 1
    ):
        out.append(
            Problem(
                SINGLE_SOURCE,
                position,
                score.claim,
                f"{score.bound_facts} 条事实只覆盖 {score.bound_sources} 条来源 / "
                f"{score.bound_authors} 位作者",
            )
        )

    if score.originality_claim:
        out.append(
            Problem(
                UNVERIFIABLE_ORIGINALITY,
                position,
                score.claim,
                "正文用了「全网/首创」类表述，而本系统没有全网语义检索能力",
            )
        )

    if score.stance is ViewpointStance.UNCERTAIN:
        out.append(
            Problem(
                LOW_CONFIDENCE,
                position,
                score.claim,
                f"置信度 {score.confidence:.4f} 只支持 uncertain；"
                "这不是错误，是这次证据确实撑不起更硬的判断",
            )
        )

    return out


def report(observation: Observation) -> list[str]:
    """给人看的逐条结论。真机 smoke 把它写进 JSON。"""
    found = problems(observation)
    if not found:
        return ["未发现问题"]
    return [problem.render() for problem in found]
