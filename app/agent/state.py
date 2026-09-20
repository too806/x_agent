from typing import TypedDict

from app.schemas import ResearchResult


class RunState(TypedDict, total=False):
    """V1 LangGraph State（Topic → Research → Generate → Guardrail → Human Review）。

    字段说明：
    - run_id: agent_run 的主键（str），同时用作 LangGraph thread_id。
    - topic: 本次运行的选题，是 Research 与 Generate 的共同输入。
    - research_results: Research 节点产出的结构化研究条目（list[ResearchResult]）。
    - research_item_ids: 已持久化到 research_item 表的 id 列表，供后续审计/关联。
    - draft_content: Generate 节点产出的正文（str）。
    - draft_id: 已持久化到 draft 表的 id（str）。
    - guardrail_passed: Guardrail 是否全部规则通过（bool）。
    - guardrail_reasons: 未通过的原因码列表（list[str]），通过时为空列表。
    - human_action: 人工审核决定（"approve" / "edit" / "reject"）。
    - final_content: 审核后的最终正文；reject 时不设置。
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
