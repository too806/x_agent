"""LangGraph State。

一份 State 同时服务两套拓扑，用 `goal` 是否为 None 区分（**不是**用一个单独的
graph_kind 字段：两个字段表达同一件事就会互相矛盾，而判别式只需要一个真相来源）：

- `goal is None`：Phase 1–4 的 V1 链路（Topic → Research → Generate → Guardrail →
  Human Review），字段与语义完全不变；
- `goal is not None`：Phase 5A 的 Agentic 循环（Planner 自主决定每一步）。

两套拓扑共用的是**下游那一半**：`draft_content` / `draft_id` / `guardrail_*` /
`human_action` / `final_content`。Agent 的最终候选由 finalize 节点翻译成
`draft_content`，于是 Guardrail、Human Review、Review API、Publish 状态机、
控制台一行都不用改。这是整个 Phase 5A 唯一的兼容接缝，故意只留这一个。
"""

from typing import Annotated, TypedDict

from app.agent.schemas import (
    AgentDecision,
    Angle,
    Candidate,
    CriticResult,
    DecisionTraceEntry,
    EvidencePack,
    TopicCandidate,
)
from app.agent.schemas import merge_trace
from app.schemas import ResearchResult


class RunState(TypedDict, total=False):
    """字段说明（V1 部分见 app/agent/graph.py 的既有说明，此处只列新增）。

    V1：
    - run_id: agent_run 的主键（str），同时用作 LangGraph thread_id。
    - topic: 本次运行的选题（V1 必填；Agent 运行时由 finalize 之后的镜像写入，
      运行开始时为 None）。
    - research_results / research_item_ids: 研究材料与它们的库内 id。
    - draft_content / draft_id: 正文与 draft 表 id。
    - guardrail_passed / guardrail_reasons: 护栏结果。
    - human_action / final_content: 人工审核结论。

    Phase 5A（`goal` 非空时使用）：
    - goal: 用户给 Agent 的目标。**它也是拓扑判别式**。
    - candidate_topics: discover 发现的候选选题。
    - selected_topic: 已选定的题目（由 research_topic 落定）。
    - evidence_pack: 可追溯的证据包。
    - angles / selected_angle: 候选观点与选定观点。
    - candidates: 候选正文池（revise 原地替换其中一条）。
    - critic_result: 最近一次评价里"该关注的那条"；critic_results 是整批。
    - revision_count / research_count / step_count: 三个预算计数。
    - writer_epoch / critic_epoch: "候选池改过几次"与"评价过几轮"，用来判断
      某批候选是否已经被评价过（见 app/agent/budget.py）。
    - decision / last_result: 本步的决策与执行结果，供路由与节点间传递。
    - decision_trace: 决策轨迹。用 merge_trace 做 reducer —— 同一步先由 Planner
      写入意图、再由 Tool 节点补上结果，必须**覆盖**而不是追加。
    """

    run_id: str
    topic: str
    research_results: list[ResearchResult]
    research_item_ids: list[str]
    draft_content: str
    draft_id: str
    guardrail_passed: bool
    guardrail_reasons: list[str]
    human_action: str
    final_content: str

    # --- Phase 5A ---
    goal: str
    candidate_topics: list[TopicCandidate]
    selected_topic: str
    evidence_pack: EvidencePack
    angles: list[Angle]
    selected_angle: Angle
    candidates: list[Candidate]
    critic_result: CriticResult
    critic_results: list[CriticResult]
    revision_count: int
    research_count: int
    step_count: int
    writer_epoch: int
    critic_epoch: int
    decision: AgentDecision
    last_result: str
    decision_trace: Annotated[list[DecisionTraceEntry], merge_trace]
