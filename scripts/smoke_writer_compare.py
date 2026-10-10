"""升级前后对比：同一个模型、同一份输入，两版 Writer 提示词各写一遍。

## 它做的是什么、不是什么

**是**：把 `git HEAD`（升级前）与工作区当前（升级后）两版
`prompts/agent/candidates.txt` + `config/persona.yaml`，配上**同一份**证据包、
同一个目标、同一个观点，各渲染一次真实的 Writer 提示词，再让**同一个真实模型**
各写三条候选。

**不是**：重放某次历史运行。两条链路用的都是生产代码里那个
`generate_candidates` 的渲染路径（`render` + `format_*` + `load_prompt`），
所以这份对比里唯一的变量就是这次升级本身。

## 为什么不用假的 LLM

假 LLM 只会复读 fixture，两个版本写出来的东西一模一样——那验的是代码路径，
不是"升级之后稿子变成什么样"。这个脚本因此**不在测试套件里**：它要真的联网、
真的花钱，只有人手动跑。（§九 的离线评估仍然不联网、不调用 LLM。）

用法：

    python scripts/smoke_writer_compare.py [--out docs/xxx.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from app.agent.persona import Persona  # noqa: E402
from app.agent.prompts import (  # noqa: E402
    format_context,
    format_evidence,
    format_feedback_context,
    format_persona,
    format_style_recipe,
    format_viewpoint,
    render,
)
from app.agent.schemas import (  # noqa: E402
    Angle,
    ClaimScope,
    EvidenceFact,
    EvidencePack,
    TopicContext,
    ViewpointStance,
)
from app.config import settings  # noqa: E402
from app.services.llm import OpenAILLMService  # noqa: E402
from app.style.runtime import resolve_recipe  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def from_head(path: str) -> str:
    """`git HEAD` 版本的某个文件内容。升级前的那一版就取自这里。"""
    return subprocess.run(
        ["git", "show", f"HEAD:{path}"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def topic_cases() -> list[tuple[str, str, EvidencePack, Angle, str]]:
    """三个选题：有数据的、没有数据的、以及一个双方都有说法的投资话题。

    刻意让其中一条**没有可核验的数字**——那正是"不许编数字"这条新规则唯一
    能被看出来的地方。
    """
    return [
        (
            "T1",
            "AI Agent 的实际价值：企业到底该不该为客服 Agent 掏钱，给一个明确判断",
            EvidencePack(
                context=TopicContext(
                    subject="企业正在试用 AI Agent 处理客服工单",
                    why_now="最近一轮产品被大量用进客服与内部流程",
                    core_tension="省下的人力与新增的维护成本谁更大，试点结果并不一致",
                ),
                source_ids=["s1", "s2"],
                facts=[
                    EvidenceFact(
                        fact_id="f1",
                        fact="一家公司把客服团队从 40 人缩到 12 人，响应时间从 4 小时压到 20 分钟。",
                        source_ids=["s1"],
                    ),
                    EvidenceFact(
                        fact_id="f2",
                        fact="同一批试点里另外三家公司的客服人力没有变化。",
                        source_ids=["s2"],
                    ),
                ],
                counterpoints=["有人认为省下的人力被新增的质检与标注岗位抵消了"],
                unknowns=["查不到这几家原来的工单量"],
            ),
            Angle(
                angle_id="a1",
                claim="现在还不是给客服 Agent 掏钱的时候，问题不在模型能力。",
                reasoning="试点结果分成两半，说明见效与否取决于流程，而不是产品",
                why_it_matters="关系到企业这一轮预算投给谁",
                evidence_ids=["f1", "f2"],
                risk="若试点的时间还太短，结论可能只是过早",
                counterpoints=["也有人认为先跑起来的团队已经拿到优势"],
                counterpoint_evidence_ids=[],
                assumptions=[],
                implications=[],
                claim_scope=ClaimScope.SEGMENT,
                stance=ViewpointStance.CAUTIOUS,
            ),
            "AI Agent 的实际价值",
        ),
        (
            "T2",
            "AI 时代普通人的赚钱机会：工具把哪一类小生意的门槛降低了，别写成鸡汤",
            EvidencePack(
                context=TopicContext(
                    subject="做本地服务的人开始把文书工作交给 AI",
                    why_now="报价、合同这类活原本要找人代写",
                    core_tension="工具替掉的是手艺，还是「必须找个专业人士」这个前提",
                ),
                source_ids=["s1", "s2"],
                facts=[
                    EvidenceFact(
                        fact_id="f1",
                        fact="一些人开始用 AI 直接出报价单和合同初稿，不再找外面的代写。",
                        source_ids=["s1"],
                    ),
                    EvidenceFact(
                        fact_id="f2",
                        fact="另一条材料提到，客户抱怨最多的还是回复太慢。",
                        source_ids=["s2"],
                    ),
                ],
                counterpoints=["有人认为这只是把代写的活换了一个收钱的人"],
                unknowns=["查不到这些人原来花多少钱请人代写"],
            ),
            Angle(
                angle_id="a1",
                claim="被降低的门槛不是写文书的手艺，是接活的资格。",
                reasoning="文书本来就要有人写，AI 只是把「要找个专业人士」这个前提去掉了",
                why_it_matters="它决定了多出来的人是去用工具还是被工具替掉",
                evidence_ids=["f1", "f2"],
                risk="若客户仍然只认有资质的服务方，这个判断就不成立",
                counterpoints=["也有人认为AI 生成的合同没人敢签"],
                counterpoint_evidence_ids=[],
                assumptions=[],
                implications=[],
                claim_scope=ClaimScope.SEGMENT,
                stance=ViewpointStance.CAUTIOUS,
            ),
            "AI 时代普通人的赚钱机会",
        ),
        (
            "T3",
            "投资话题：某类公司集体提价到底说明了什么，不要给投资建议",
            EvidencePack(
                context=TopicContext(
                    subject="这一类公司开始同步调整套餐价格",
                    why_now="它们过去两年靠低价获客",
                    core_tension="提价说明成本扛不住了，还是说明需求变强了",
                ),
                source_ids=["s1", "s2"],
                facts=[
                    EvidenceFact(
                        fact_id="f1",
                        fact="两家公司在同一周把同类套餐的价格提高了 15%。",
                        source_ids=["s1"],
                    ),
                    EvidenceFact(
                        fact_id="f2",
                        fact="同一周，其中一家的新增用户数环比下降。",
                        source_ids=["s2"],
                    ),
                ],
                counterpoints=["有人认为提价是为了给下一轮融资做数字"],
                unknowns=["查不到提价后第二周的续费率"],
            ),
            Angle(
                angle_id="a1",
                claim="这次提价更像是获客成本算不过来了，不是找到了新需求。",
                reasoning="提价与新增用户下滑同时发生，更符合「买不起流量了」这一种解释",
                why_it_matters="它决定了这类公司后面还有没有降价空间",
                evidence_ids=["f1", "f2"],
                risk="若续费率同时上升，就说明是需求变强而不是成本推动",
                counterpoints=["也有人认为这是行业进入成熟期的正常动作"],
                counterpoint_evidence_ids=[],
                assumptions=[],
                implications=[],
                claim_scope=ClaimScope.SEGMENT,
                stance=ViewpointStance.CAUTIOUS,
            ),
            "有数据和不同观点的投资话题",
        ),
    ]


async def render_and_generate(
    llm: OpenAILLMService,
    template: str,
    persona: Persona,
    goal: str,
    pack: EvidencePack,
    angle: Angle,
) -> str:
    recipe = await resolve_recipe(None, pack, existing=None, hard_max_length=persona.max_length)
    prompt = render(
        template,
        GOAL=goal,
        TOPIC="",
        CONTEXT=format_context(getattr(pack, "context", None)),
        EVIDENCE=format_evidence(pack),
        VIEWPOINT=format_viewpoint(angle),
        PERSONA=format_persona(persona),
        STYLE=format_style_recipe(recipe),
        FEEDBACK=format_feedback_context(None),
        MAX_LENGTH=persona.max_length,
        COUNT=3,
    )
    return await llm.complete(prompt, json_mode=True)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/writer-upgrade-compare.json")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    model = args.model or settings.llm_model
    llm = OpenAILLMService(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=model,
        timeout=120,
    )

    old_template = from_head("prompts/agent/candidates.txt")
    old_persona = Persona(**yaml.safe_load(from_head("config/persona.yaml")))
    new_template = (REPO / "prompts/agent/candidates.txt").read_text(encoding="utf-8")
    new_persona = Persona(
        **yaml.safe_load((REPO / "config/persona.yaml").read_text(encoding="utf-8"))
    )

    records = []
    for case_id, goal, pack, angle, label in topic_cases():
        entry = {"case_id": case_id, "label": label, "goal": goal, "runs": {}}
        for version, template, persona in (
            ("before", old_template, old_persona),
            ("after", new_template, new_persona),
        ):
            raw = await render_and_generate(llm, template, persona, goal, pack, angle)
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"_raw": raw}
            entry["runs"][version] = parsed
            print(f"[{case_id}/{version}]")
            for cand in parsed.get("candidates", []):
                print(f"  <{cand.get('label')}> {cand.get('content')}")
            print()
        records.append(entry)

    out = {
        "note": (
            "同一个模型、同一份输入，两版 Writer 提示词（git HEAD vs 工作区当前）"
            "各写一遍。不是重放历史运行。"
        ),
        "model": model,
        "cases": records,
    }
    path = REPO / args.out
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
