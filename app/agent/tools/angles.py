"""generate_angles / select_angle：形成并选定观点。

工作单第十节第一版的目标很克制，就一句话：
**"Agent 会不会自己形成一个明确、可证据支持的观点"**。

所以这里做了两条约束：

1. 每个 Angle 的 evidence_ids 必须落在证据包真实的 fact_id 集合里，
   引用不到的论点**直接丢弃**——与 evidence 工具的处置一致。
   没有证据支撑的论点不是"差一点的论点"，它是一个会被模型用漂亮话讲出来的断言。
2. Angle 只能引 fact_id，不能引原始材料（见 app/agent/schemas.py 的说明）。
   两级引用让"这个观点凭什么成立"永远能在两步之内查到出处。
"""

import logging

from app.agent.prompts import format_evidence, load_prompt, render
from app.agent.schemas import Angle, AngleList
from app.agent.tools.base import ToolResult, complete_json
from app.services.llm import LLMService

logger = logging.getLogger(__name__)

MAX_ANGLES = 5
MIN_ANGLES = 3


def make_angles_tool(llm_service: LLMService):
    async def generate_angles(state, decision) -> ToolResult:
        run_id = state["run_id"]
        pack = state.get("evidence_pack")
        fact_ids = {fact.fact_id for fact in getattr(pack, "facts", [])}
        logger.info("agent angles started (run_id=%s, facts=%d)", run_id, len(fact_ids))

        prompt = render(
            load_prompt("angles"),
            GOAL=state.get("goal") or state.get("topic") or "",
            TOPIC=state.get("selected_topic") or "",
            MIN_ANGLES=MIN_ANGLES,
            MAX_ANGLES=MAX_ANGLES,
            EVIDENCE=format_evidence(pack),
        )
        parsed: AngleList = await complete_json(llm_service, prompt, AngleList)

        angles: list[Angle] = []
        dropped = 0
        for angle in parsed.angles[:MAX_ANGLES]:
            valid = [eid for eid in angle.evidence_ids if eid in fact_ids]
            if not valid:
                dropped += 1
                continue
            angles.append(
                angle.model_copy(
                    update={"angle_id": f"a{len(angles) + 1}", "evidence_ids": valid}
                )
            )

        logger.info("agent angles completed (run_id=%s, angles=%d)", run_id, len(angles))
        summary = f"生成 {len(angles)} 个可证据支撑的观点：" + "；".join(a.claim for a in angles)
        if dropped:
            summary += f"（丢弃 {dropped} 个引不到证据的）"
        return ToolResult(delta={"angles": angles}, summary=summary)

    return generate_angles


def make_select_angle_tool():
    """选定观点。

    这一步**不调用 LLM**，它只是把 Planner 的选择落到 state 上。Planner 已经在
    决策里给出了 target，再让模型"选择"一次等于把同一个判断做两遍——
    多一次调用、多一处可能不一致的地方。

    target 无效时回退到置信度最高的一个，并在 trace 里写明发生了回退。
    """

    async def select_angle(state, decision) -> ToolResult:
        angles = state.get("angles") or []
        if not angles:  # pragma: no cover - 前置条件已挡住，防御性分支
            return ToolResult(delta={}, summary="没有可选的观点")

        chosen = next((a for a in angles if a.angle_id == decision.target), None)
        if chosen is None:
            chosen = max(angles, key=lambda a: a.confidence)
            how = f"决策给的「{decision.target or '（空）'}」不是有效观点，回退到置信度最高的"
        else:
            how = "按决策选定"

        logger.info("agent angle selected (run_id=%s, angle=%s)", state["run_id"], chosen.angle_id)
        return ToolResult(
            delta={"selected_angle": chosen},
            summary=f"{how} {chosen.angle_id}：{chosen.claim}",
        )

    return select_angle
