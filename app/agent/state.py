from typing import TypedDict

from app.schemas import ResearchResult


class RunState(TypedDict, total=False):
    """V1 LangGraph State（Topic → Research → Generate）。

    字段说明：
    - run_id: agent_run 的主键（str），Graph 用它与已创建的 agent_run 关联。
    - topic: 本次运行的选题，是 Research 与 Generate 的共同输入。
    - research_results: Research 节点产出的结构化研究条目（list[ResearchResult]）。
    - research_item_ids: 已持久化到 research_item 表的 id 列表，供后续审计/关联。
    - draft_content: Generate 节点产出的正文（str）。
    - draft_id: 已持久化到 draft 表的 id（str）。
    """

    run_id: str
    topic: str
    research_results: list[ResearchResult]
    research_item_ids: list[str]
    draft_content: str
    draft_id: str
