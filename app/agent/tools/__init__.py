"""Agent 的工具集与「动作 → 工具」的唯一映射表。

工作单第六节列了 7 个工具。这里的表比它多几条，说明如下：

- `select_angle` 与 `select_topic` 不调 LLM：Planner 已经在决策里给出了 target，
  再让模型"选择"一次是把同一个判断做两遍。它们仍然是动作，仍然要记 trace。
  `select_topic` 更彻底一点——候选本身已经带了确定性分数，"选哪个"有算术依据，
  不该由措辞决定（见 tools/discover.py）。
- `deepen_research` 与 `research_topic` 是两个动作、两个实现，因为它们的**检索词
  来源不同**（见 tools/research.py）。合成一个工具就会丢掉"这次搜的是哪个空白"。

表放在这里而不是散在 graph 里，是因为"Planner 说 X → 执行 Y"这件事只能有一个答案。
两处各写一份的话，Planner 的 prompt 和实际执行会各自演化，最后出现
"模型选了 research_topic，实际跑的是别的东西"——而这种错误不会有任何报错。
"""

from typing import Mapping

from app.agent.budget import AgentBudget
from app.agent.schemas import AgentAction
from app.agent.tools.angles import make_angles_tool, make_select_angle_tool
from app.agent.tools.base import Tool, ToolResult
from app.agent.tools.candidates import make_candidates_tool
from app.agent.tools.critic import make_critic_tool
from app.agent.tools.discover import make_discover_tool, make_select_topic_tool
from app.agent.tools.evidence import make_evidence_tool
from app.agent.tools.research import make_deepen_tool, make_research_tool
from app.agent.tools.revise import make_revise_tool

__all__ = ["Tool", "ToolResult", "build_tools"]


def build_tools(deps, budget: AgentBudget) -> Mapping[AgentAction, Tool]:
    """把依赖绑进工具，返回 动作 → 可调用对象 的表。

    每次运行现建（而不是模块级单例）：工具持有 x_service / llm_service /
    session_factory，这些是每次运行注入的，做成全局单例就没法给测试换假实现。
    建一次的开销是几个闭包，可以忽略。
    """
    return {
        AgentAction.DISCOVER_TOPICS: make_discover_tool(
            deps.x_service, deps.llm_service, deps.session_factory
        ),
        AgentAction.SELECT_TOPIC: make_select_topic_tool(),
        AgentAction.RESEARCH_TOPIC: make_research_tool(deps.x_service, deps.session_factory),
        AgentAction.DEEPEN_RESEARCH: make_deepen_tool(deps.x_service, deps.session_factory),
        AgentAction.EXTRACT_EVIDENCE: make_evidence_tool(deps.llm_service),
        AgentAction.GENERATE_ANGLES: make_angles_tool(deps.llm_service),
        AgentAction.SELECT_ANGLE: make_select_angle_tool(),
        AgentAction.GENERATE_CANDIDATES: make_candidates_tool(
            deps.llm_service, deps.persona, budget
        ),
        AgentAction.CRITICIZE: make_critic_tool(deps.llm_service, deps.persona),
        AgentAction.REVISE: make_revise_tool(deps.llm_service, deps.persona),
    }
