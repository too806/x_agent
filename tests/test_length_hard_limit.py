"""正文的**交付硬上限**：一个尺子，三处判界，一处都不许超长出门。

## 这一轮要修的问题

长度以前只在提示词里"建议"。`guardrail` 会算出一个 `too_long`，但它是个
**提示性**原因码——不拦人。于是"最终送审/发布的正文到底会不会超过 140 字"
这件事，取决于模型有没有听话，而模型是会不听话的。

修法的形状刻意做成三层，各管一段，且**读同一个尺子**
（`app/agent/post_structure.over_hard_limit`，`len()` 口径）：

1. **Critic**（`_apply_length_ceiling`）：模型判了 accept 但正文超长时，
   **程序**把它确定性地改判成 `revise`，并给出一条具体的改稿指令。这一步让
   超长的候选在正常流程里**有得救**——它会被打回改稿，而不是直接判死。
2. **finalize**（`pick_final_candidate`）：选稿时先按"超没超长"分档，宁可
   交付分数低一点但合规的那条；**全池都超长**时抛 `AgentOverlongError`，
   让这次 Run 以安全、可解释的状态收场。
3. **发布预检**（`PublishService._preflight`）：最后一道，超长一律
   `OUTCOME_INVALID`。前三层任何一层漏了，这一层兜住。

三处都不是"把正文截断"：截断会造成句子残缺、观点失真、事实关系破坏，
那是把不合格伪装成合格的假象。**没有任何一处调用方在改正文**。

## 这里钉的是什么

- 判界的边界（`>` 而不是 `>=`，空值当 0）；
- Critic 改判只动 `decision / flags / revision_targets`，**分数与正文一字不动**；
- finalize 的档位优先级、`max_length=None` 的旧行为、全超长时的异常；
- 提示词里那些**必须留下**的措辞（它们是"偏好 vs 硬约束"这件事在提示词层的
  落点，删掉就等于又回到"只是建议"）。

发布预检那一条在 `tests/test_publish_service.py`，自动审阅的长度判据在
`tests/test_auto_review.py`——各自贴在它们该待的地方，不在这里重复。
"""

import pytest

from app.agent.nodes.finalize import (
    AgentIncompleteError,
    AgentOverlongError,
    pick_final_candidate,
)
from app.agent.post_structure import length_detail, over_hard_limit
from app.agent.schemas import Candidate, CriticResult
from app.agent.tools.critic import _apply_length_ceiling

LIMIT = 140


def candidate(cid: str, content: str, label: str = "直接观点") -> Candidate:
    return Candidate(candidate_id=cid, label=label, content=content)


def verdict(cid: str, decision: str = "accept", score: float = 8.0) -> CriticResult:
    return CriticResult(candidate_id=cid, decision=decision, score=score)


def post_state(*, candidates, critic_results=()) -> dict:
    return {"candidates": list(candidates), "critic_results": list(critic_results)}


# ---------------------------------------------------------------------------
# 1. 尺子本身：边界、空值、口径
# ---------------------------------------------------------------------------


def test_just_at_the_limit_is_not_over():
    """`>` 而不是 `>=`：正好 140 字是**合规**的。

    差一个字符的边界判错，会让一条完全合格的稿子被反复打回改稿，
    而每轮改稿都在减少信息量——最后改出来的是比 140 更短、也更空洞的东西。
    """
    assert over_hard_limit("字" * LIMIT, LIMIT) == 0
    assert over_hard_limit("字" * (LIMIT + 1), LIMIT) == 1
    assert over_hard_limit("字" * (LIMIT - 1), LIMIT) == 0


def test_the_ruler_counts_characters_not_bytes():
    """口径是 `len()`（Python 字符数），与 `guardrail` 和前端显示的是同一件事。

    用 `len(content.encode())` 会把一个汉字算成 3，"超过 140 字"会变成
    "超过 46 字"——同一个数在两处显示不同，排查时会先怀疑数据、再怀疑代码，
    而真正的问题是两个口径。
    """
    content = "汉" * 30
    assert over_hard_limit(content, LIMIT) == 0
    assert over_hard_limit(content, 29) == 1


def test_empty_content_is_zero_over():
    """空值不炸：`None` 与 `""` 都当 0 字。

    判界最后一道在发布预检上，那里 `content` 可能还没解析出来（走的是别的
    岔路），一个 `TypeError` 会把"该被拦下"变成"服务 500"。
    """
    assert over_hard_limit("", LIMIT) == 0
    assert over_hard_limit(None, LIMIT) == 0


def test_length_detail_says_how_much_is_left():
    """说明文本要同时给"现在多少字"和"还要砍多少字"——改稿只读这一句。"""
    under = length_detail("字" * 100, LIMIT)
    assert "100" in under and "以内" in under

    over = length_detail("字" * 146, LIMIT)
    assert f"超过硬上限 {LIMIT} 字 6 字" in over


# ---------------------------------------------------------------------------
# 2. Critic 的确定性拦截：accept → revise
# ---------------------------------------------------------------------------


def test_an_overlong_accept_is_reviewed_into_revise():
    """模型说 accept，但正文超长 → 程序改判 `revise`。这是这一层的全部意义。"""
    long_content = "字" * (LIMIT + 20)
    results = [verdict("c1", "accept", 9.0)]

    rewritten, blocked = _apply_length_ceiling(results, [candidate("c1", long_content)], LIMIT, "run-1")

    assert blocked == ["c1"]
    assert rewritten[0].decision == "revise"
    assert "too_long" in rewritten[0].flags


def test_the_revision_target_says_how_many_characters_to_cut():
    """改稿指令必须是**可执行**的：说清楚超了多少字、以及压回的办法。

    只说"太长了"的指令，模型下一轮大概率还是写这么长——它不知道差多少。
    """
    results = [verdict("c1", "accept")]
    rewritten, _ = _apply_length_ceiling(
        results, [candidate("c1", "字" * (LIMIT + 12))], LIMIT, "run-1"
    )

    targets = " ".join(rewritten[0].revision_targets)
    assert "12 字" in targets
    assert f"{LIMIT} 字以内" in targets


def test_the_ceiling_never_touches_the_content_or_the_score():
    """**只动判定，不动稿子，也不动分。**

    - 稿子：改稿是 revise 那条路的事，Critic 在这里动手就是"自己写正文"，
      而且一旦动手就会顺手截断——那正是被禁止的做法；
    - 分：分数说的是"这条稿好不好"，超长说的是"这条稿现在能不能交付"。
      一条写得很好但超长的稿子仍然该是高分，否则改短之后它也回不来了
      （Planner 会拿这个分去比）。
    """
    long_content = "字" * (LIMIT + 30)
    results = [verdict("c1", "accept", 9.4)]

    rewritten, _ = _apply_length_ceiling(results, [candidate("c1", long_content)], LIMIT, "run-1")

    assert rewritten[0].score == 9.4
    assert rewritten[0].candidate_id == "c1"
    # 正文压根不在 CriticResult 上——这条断言钉住"它不该出现在这里"。
    assert not hasattr(rewritten[0], "content")


def test_an_in_limit_accept_is_left_alone():
    """合规的 accept 一个字都不许动——否则拦截会变成"什么都拦"。"""
    results = [verdict("c1", "accept", 8.5)]

    rewritten, blocked = _apply_length_ceiling(
        results, [candidate("c1", "字" * LIMIT)], LIMIT, "run-1"
    )

    assert blocked == []
    assert rewritten[0] is results[0]


def test_non_accept_verdicts_are_left_alone():
    """已经是 revise / reject 的不动：它们本来就不会被交付，
    再叠一条长度指令只会让 LLM 混淆"这次改稿真正该做什么"。"""
    results = [verdict("c1", "revise"), verdict("c2", "reject")]

    rewritten, blocked = _apply_length_ceiling(
        results,
        [candidate("c1", "字" * (LIMIT + 50)), candidate("c2", "字" * (LIMIT + 50))],
        LIMIT,
        "run-1",
    )

    assert blocked == []
    assert [r.decision for r in rewritten] == ["revise", "reject"]


def test_the_ceiling_is_a_no_op_on_an_empty_batch():
    assert _apply_length_ceiling([], [], LIMIT, "run-1") == ([], [])


def test_a_missing_candidate_is_treated_as_empty_content():
    """评价里有 candidate_id 却找不到对应候选（数据不全）→ 当 0 字处理，不拦。

    这里**故意**不失败：数据不全时拦下的是"程序自身的 bug"，而把它伪装成
    "这条稿子超长"会让改稿指令去砍一条根本不存在的东西。宁可漏拦这一层，
    后面 finalize 与发布预检还有两道。
    """
    results = [verdict("c-missing", "accept")]

    rewritten, blocked = _apply_length_ceiling(results, [candidate("c1", "正文")], LIMIT, "run-1")

    assert blocked == []
    assert rewritten[0].decision == "accept"


# ---------------------------------------------------------------------------
# 3. finalize 的选稿：合规优先，全超长就安全失败
# ---------------------------------------------------------------------------


def test_a_short_lower_scored_candidate_wins_over_an_overlong_higher_one():
    """这是这一层存在的理由：一条 9.0 分但超长的稿子，**发不出去**。

    交付一条能用的 6.0，比交付一条纸面好看的 9.0 诚实——前者能发，
    后者只会让人工审核接手删字（那不是审核，是替 Agent 写稿）。
    """
    st = post_state(
        candidates=[candidate("long", "字" * (LIMIT + 60)), candidate("short", "短正文。")],
        critic_results=[verdict("long", "accept", 9.0), verdict("short", "accept", 6.0)],
    )

    assert pick_final_candidate(st, max_length=LIMIT).candidate_id == "short"


def test_without_a_limit_the_old_score_order_is_kept_verbatim():
    """`max_length=None`（手写 state 的单测、旧调用方）→ 与加规则前逐字相同。

    这条是给旧数据与旧测试留的"没传参数就没有新行为"的保证。
    """
    st = post_state(
        candidates=[candidate("long", "字" * (LIMIT + 60)), candidate("short", "短正文。")],
        critic_results=[verdict("long", "accept", 9.0), verdict("short", "accept", 6.0)],
    )

    assert pick_final_candidate(st).candidate_id == "long"


def test_when_every_candidate_is_overlong_the_run_fails_safely():
    """全池超长 → `AgentOverlongError`，不是"挑条最接近的送审"。

    硬上限是交付条件而不是打分项：送一条 200 字的稿子给人工审核，审核者要么
    替 Agent 删字，要么没看见就发出去。**没有合格正文**是这件事的真实状态，
    把它说出来比用一条不合格的稿子盖过去诚实。
    """
    st = post_state(
        candidates=[candidate("c1", "字" * 200, "A"), candidate("c2", "字" * 160, "B")],
        critic_results=[verdict("c1", "accept", 9.0), verdict("c2", "accept", 7.0)],
    )

    with pytest.raises(AgentOverlongError) as excinfo:
        pick_final_candidate(st, max_length=LIMIT)

    message = str(excinfo.value)
    # 逐个候选写清字数：这条错误唯一的下游是运维/开发者，它要回答"离合格差多少"。
    assert "c1" in message and "c2" in message
    assert f"超过硬上限 {LIMIT} 字 60 字" in message
    assert f"超过硬上限 {LIMIT} 字 20 字" in message


def test_the_overlong_error_is_an_incomplete_error():
    """继承 `AgentIncompleteError` 是刻意的：对下游来说这是**同一件事**
    ——"这次 Run 没有可交付的正文"。

    所有已经写好的"没有正文"恢复路径（例如 `auto_review.resolve_delivered_verdict`
    返回 `(None, None)` 而不是放行）因此继续生效，不必再加一条分支。
    """
    assert issubclass(AgentOverlongError, AgentIncompleteError)

    st = post_state(candidates=[candidate("c1", "字" * 200)], critic_results=[verdict("c1", "accept")])
    with pytest.raises(AgentIncompleteError):
        pick_final_candidate(st, max_length=LIMIT)


def test_an_in_limit_candidate_is_never_rejected_by_the_limit():
    """合规的候选按原优先级选，长度这一档不许改变分数的作用。"""
    st = post_state(
        candidates=[candidate("low", "短"), candidate("high", "也短")],
        critic_results=[verdict("low", "accept", 6.0), verdict("high", "accept", 9.0)],
    )

    assert pick_final_candidate(st, max_length=LIMIT).candidate_id == "high"


def test_the_limit_never_overrides_the_reject_ranking():
    """长度档在**最前面**，所以一条合规但被 reject 的稿子仍然垫底——
    除非其余每一条都超长（那时它是唯一能交付的）。

    这是刻意的次序：先问"能不能交付"，再问"好不好"。
    """
    st = post_state(
        candidates=[
            candidate("rejected_but_short", "短正文。"),
            candidate("accepted_but_long", "字" * (LIMIT + 10)),
        ],
        critic_results=[
            verdict("rejected_but_short", "reject", 3.0),
            verdict("accepted_but_long", "accept", 9.5),
        ],
    )

    assert pick_final_candidate(st, max_length=LIMIT).candidate_id == "rejected_but_short"


# ---------------------------------------------------------------------------
# 4. 提示词层的落点：偏好与硬约束必须被分开说
# ---------------------------------------------------------------------------


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


@pytest.mark.parametrize("path", ["prompts/agent/candidates.txt", "prompts/agent/critic.txt", "prompts/agent/revise.txt"])
def test_writer_and_critic_prompts_call_the_limit_a_delivery_hard_limit(path):
    """三份提示词都要把上限说成**硬上限**，而不是"写作目标"。

    提示词降低的是**发生概率**，判界由代码负责（上面三层）。但提示词若把
    上限说成"建议"，模型就会合理地超一点，而超一点会被程序打回——改稿预算
    被用在一个本可以避免的来回上。
    """
    text = _read(path)
    assert "硬上限" in text


def test_the_writer_prompt_keeps_the_pinned_wording():
    """候选提示词里那句被钉住的措辞必须还在（测试直接断言它）。

    它说的是"这是我们自己的目标，不是平台拦我们"——这句话是**事实**
    （这个账号是 Premium），删掉会让人以为改短是为了绕开平台限制。
    """
    text = _read("prompts/agent/candidates.txt")
    assert "**写作目标**，不是平台" in text


def test_the_persona_keeps_the_two_lengths_apart():
    """`persona.yaml` 里那两个数必须被说成**两类东西**：
    上限是交付条件（会拦人），下沿只是写作偏好（不拦人）。
    """
    text = _read("config/persona.yaml")
    assert "交付硬上限" in text
    # 下沿那一段（markdown 里被加粗隔开，所以按片段断）
    assert "短于此不构成任何拒绝理由" in text
    assert "两类东西" in text
