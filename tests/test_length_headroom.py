"""长度遵从：算给模型看的字数，与既有判据用的是同一把尺子。

背景：真机上 `persona.max_length = 140` 写出过 188 字的稿子。根因不是 prompt 里
没写上限（`candidates.txt:48` 写了，还写了"超出会被直接拒收"），而是**没有任何
一环把"你超了多少"算出来告诉模型**。中文字符数模型目测不出来；但"把 188 砍掉
48"是一个它做得到的任务。所以修法是把数字算好递过去，而不是再写一遍"不许超"。

这里盯住三件事：

1. `format_candidates` 不传 `max_length` 时输出**逐字不变**——它只有 Critic 一个
   调用方，但保持默认路径不变才不会波及盯住这段渲染的测试；
2. 传了时，超长那条被标出来，且标的是**算出来的**差值；
3. `format_length_headroom` 的算术，以及它与 `guardrail.evaluate` 的 `too_long`
   在**边界上判得一样**。两把尺子不一致的后果很具体：critic 说长度没问题、
   发布那一步却 422，改稿回路等于没有。
"""

from app.agent.nodes.guardrail import evaluate
from app.agent.prompts import format_candidates, format_length_headroom
from app.agent.schemas import Candidate

MAX = 140


def _candidate(
    candidate_id: str,
    content: str,
    *,
    label: str = "直接观点",
    revision: int = 0,
) -> Candidate:
    return Candidate(
        candidate_id=candidate_id, label=label, content=content, revision=revision
    )


# --- format_candidates：默认路径必须逐字不变 ---------------------------------


def test_format_candidates_without_max_length_is_unchanged():
    """不传 `max_length` 时，输出与加这个参数之前**逐字相同**。

    这条是回归保护：把一个"不加料"的默认路径钉死，将来谁改了表头格式，
    先在这里失败，而不是在某个远处的集成用例里。
    """
    candidates = [
        _candidate("c1", "第一句就是钩子。"),
        _candidate("c2", "第二条。", revision=1),
    ]
    assert format_candidates(candidates) == (
        "[c1]（直接观点，第 0 稿）\n第一句就是钩子。\n\n"
        "[c2]（直接观点，第 1 稿）\n第二条。"
    )


def test_format_candidates_empty_is_unchanged_either_way():
    assert format_candidates([]) == "（无候选）"
    assert format_candidates([], max_length=MAX) == "（无候选）"


# --- format_candidates：字数标注 ---------------------------------------------


def test_format_candidates_marks_over_cap_with_computed_excess():
    content = "字" * 188
    out = format_candidates([_candidate("c2", content)], max_length=MAX)
    assert out.startswith("[c2]（直接观点，第 0 稿，188 字，**超出上限 48 字**）")
    assert content in out  # 正文原样保留，一个字都不动


def test_format_candidates_reports_length_without_flag_when_within_cap():
    out = format_candidates([_candidate("c1", "字" * 120)], max_length=MAX)
    assert "，120 字）" in out
    assert "超出上限" not in out


def test_format_candidates_at_exactly_the_cap_is_not_over():
    """恰好等于上限**不算超**——与 `evaluate` 的 `length > max_length` 一致。"""
    out = format_candidates([_candidate("c1", "字" * MAX)], max_length=MAX)
    assert f"，{MAX} 字）" in out
    assert "超出上限" not in out


def test_format_candidates_annotates_every_candidate_independently():
    """三条里只有一条超长时，另外两条不该被牵连。"""
    out = format_candidates(
        [
            _candidate("c1", "字" * 100),
            _candidate("c2", "字" * 188),
            _candidate("c3", "字" * 130),
        ],
        max_length=MAX,
    )
    assert "，100 字）" in out
    assert "，188 字，**超出上限 48 字**）" in out
    assert "，130 字）" in out
    assert out.count("超出上限") == 1


# --- format_length_headroom --------------------------------------------------


def test_headroom_over_cap_states_the_exact_number_to_cut():
    out = format_length_headroom("字" * 188, MAX)
    assert "原稿 188 字" in out
    assert "超过上限 140 字 48 字" in out
    assert "砍掉至少 48 字" in out


def test_headroom_over_cap_forbids_trading_facts_for_length():
    """砍长度的指令必须同时给出**不许动什么**。

    不给的话，"把正文压到 140 字以内"最省事的解法就是删掉一条事实或软化观点，
    而那是比超长更糟的结果——超长发不出去，删事实是发出去了一条错的。
    """
    out = format_length_headroom("字" * 188, MAX)
    assert "不许删事实" in out
    assert "不许删观点" in out
    assert "钩子" in out


def test_headroom_within_cap_tells_the_model_not_to_trim():
    """未超长时也要给话——而且要给的恰恰是"别动长度"。

    不写这一句，模型会顺手把一条已经合规的稿子也压缩一遍，于是改稿这一步
    反而丢掉了内容。
    """
    out = format_length_headroom("字" * 132, MAX)
    assert "原稿 132 字" in out
    assert "已在 140 字以内" in out
    assert "不要为了长度删内容" in out
    assert "砍掉至少" not in out


def test_headroom_at_exactly_the_cap_is_treated_as_within():
    out = format_length_headroom("字" * MAX, MAX)
    assert "不要为了长度删内容" in out
    assert "砍掉至少" not in out


def test_headroom_excess_matches_the_number_in_the_candidate_header():
    """同一段正文：候选表头里的"超出 N 字"与改稿提示里的 N 必须是**同一个数**。

    这两处由两个函数分别渲染，一旦漂开，Critic 会按一个数升 P1、revise 按
    另一个数去砍，回路就对着一个错误的差值改稿。
    """
    for length in (141, 160, 188, 300):
        content = "字" * length
        header = format_candidates([_candidate("c1", content)], max_length=MAX)
        headroom = format_length_headroom(content, MAX)
        excess = length - MAX
        assert f"**超出上限 {excess} 字**" in header
        assert f"超过上限 {MAX} 字 {excess} 字" in headroom


# --- 与守卫同一把尺子 --------------------------------------------------------


def test_length_ruler_agrees_with_guardrail_at_the_boundary():
    """我们这边说"没超"的稿子，`guardrail.evaluate` 就不该报 `too_long`，反之亦然。

    这是整件事成立的前提：Critic 判长度用的是这里算的数，而真正拦下发布的是
    `evaluate`。两个判据在边界上（上限-1 / 上限 / 上限+1）只要差一位，就会出现
    "critic 认为合格 → 循环不修 → 发布 422"，也就是这次要修的那个 bug 的翻版。
    """
    for length in (MAX - 1, MAX, MAX + 1):
        content = "A" * length  # 字母：避开 _is_well_formed 与门禁，只留长度这一条
        _, reasons = evaluate(content, max_length=MAX)
        guardrail_says_over = "too_long" in reasons

        header = format_candidates([_candidate("c1", content)], max_length=MAX)
        headroom = format_length_headroom(content, MAX)

        assert guardrail_says_over == ("超出上限" in header), f"表头不一致 len={length}"
        assert guardrail_says_over == ("砍掉至少" in headroom), f"改稿提示不一致 len={length}"
