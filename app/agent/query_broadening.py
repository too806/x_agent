"""连续空结果时把检索词**加宽**，而不是换一组同样窄的词。

## 它要修的是什么

真机 run `f2c82f39`：目标是"来一个脱离世俗的主题，言语简单些"，Planner 连续
四次给出 3–4 个中文词的检索词，四次全部取回 0 条材料，于是整个 research 预算
（`max_research_calls: 4`）被耗光，Run 以"Agent 未产出任何候选正文"收场。

    脱离世俗 出世 精神生活 讨论 → 0
    隐居 山居 极简生活 慢下来   → 0
    独居 慢生活 逃离内卷        → 0
    寺庙 禅修 年轻人            → 0

每次的 reason 都写着"换个更口语的词再试",看上去在改进——但**结构没变**：
始终是 3–4 个词的空格分隔 AND。X 的 recent search 把空格当 AND，词越多召回越窄，
所以"换一组词"是伪降级：换的是词，不是约束数。

## 实测（同一 token、同一条 API、同一个 72 小时窗口）

    4 词 AND：脱离世俗 出世 精神生活 讨论              → 0
    3 词 AND：寺庙 禅修 年轻人                        → 0
    2 词 AND：同一批候选词里取 8 组，3 组仍然是 0；
              命中的 5 组也只回来 1–2 条
    1 个词：池子里 16 个词**全部**命中，每个 25 条
            （连目标整句"来一个脱离世俗的主题"都有 25 条）

两条结论决定了这里的做法：

1. **要降的是词数**，不是换词。而"1 个词必然命中"是实测的硬结论。
2. **降到 2 个词不够**。命中时也只回来 1–2 条，而 `trends` 判定 qualified 要求
   ≥2 位作者、≥2 条来源——1–2 条材料连一个达标候选都撑不起来，等于还是白搜。
   所以加宽**直接落到 1 个词**，不设"先试 2 个"的中间档：那一档只会多花掉
   一次预算，而预算一共只有 4 次。

## 为什么用这些代理指标，而不是直接测

运行期**没法先测召回再决定搜什么**——测一次就是一次 X 调用，而研究预算只有 4 次，
不能拿它去探路。所以只能用事前的代理：

- **词数**：实测的硬约束（见上）。加宽的第一件事就是把词数降到 1。
- **"目标里出现过"**：候选词全部来自 Planner 自己给出的检索词（prompt 要求它
  给"几个词，不是整句话"，所以它们本来就是关键词）。目标文本是**用户自己说的
  话**，一个同时出现在两者里的词，是这轮运行里最接近"用户想看什么"的现成信息。
  目标只当**信号**，不当候选词的来源——它是一句委托（"来一个脱离世俗的主题"），
  整句拆出来是短语而不是关键词。

本模块是一个纯函数库：没有 IO、没有随机、不读时钟。同一份输入必然得到同一个
结果，所以"为什么换宽了"可以在测试里逐条断言，而不是只能从日志里猜。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from app.agent import search_query

#: 末尾连续几次空结果之后开始加宽。
#:
#: 不是 1：第一次空结果更可能是"这组词碰巧不命中"，而**换一组等复杂度的词**
#: 仍然是一次合理的尝试（工作单里 Planner 的自主性正是要保留这个）。
#: 到第二次还空，就说明问题不在词选得准不准，而在约束太多。
BROADEN_AFTER_EMPTY = 2

#: 切词用的分隔符：空白、中文标点、以及句子里的 ASCII 标点。
#:
#: 刻意**不用分词器**：中文没有空格，而引一个分词依赖不值得（本轮不加 dependency）。
#: 切分只做保守的事——`逃离内卷` 保持完整，`MCP/Agent` 会变成两个词。
#: `*` `|` 也在表内：它们是 X 查询语法里的运算符（会让整次请求 400），
#: 在这里被切开比留给 `search_query.usable()` 整条丢掉更有用。
_SEPARATORS = re.compile(r"[\s，。、；：！？（）【】《》「」『』“”‘’·,.;:!?()\[\]{}<>\"'/\\|*]+")


def split_terms(text: str) -> list[str]:
    """把一句检索词/一段文本切成候选词。空文本给空列表。"""
    return [part for part in _SEPARATORS.split(text or "") if part]


def _field(attempt: Any, name: str) -> Any:
    """读一条 `SearchAttempt` 的属性，对 dict 形态也能读。

    checkpoint 漏登记类型时顶层模型会退化成 dict（见 `app/db/checkpointer.py`
    的说明）。那种情况下这里**不该崩**——加宽失效是可以接受的，运行崩掉不是。
    """
    if isinstance(attempt, dict):
        return attempt.get(name)
    return getattr(attempt, name, None)


def _query(attempt: Any) -> str:
    value = _field(attempt, "query")
    return str(value) if value else ""


def _result_count(attempt: Any) -> int:
    value = _field(attempt, "result_count")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def consecutive_empty(attempts: Sequence[Any] | None) -> int:
    """末尾连续几次检索一条材料都没取回（中间取回过就重新计数）。"""
    count = 0
    for attempt in reversed(list(attempts or [])):
        if _result_count(attempt) > 0:
            break
        count += 1
    return count


def _normalize(text: str) -> str:
    """检索词的比较形态：压掉重复空白。

    与 `search_query.choose()` 输出时用的归一化**同一套**，否则"这条检索词
    是不是已经搜过"会因为空格差异而判错。
    """
    return " ".join((text or "").split())


def _ordered_unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def broadened_query(
    attempts: Sequence[Any] | None, *, current: str, goal: str
) -> str | None:
    """这一轮该换成什么检索词；**不需要加宽时返回 None**。

    `attempts` 是此前每一次检索的记录（`state["search_attempts"]`），
    `current` 是本来打算用的检索词，`goal` 是用户给的目标（可以是空串）。

    返回 None 表示"照原样搜"——包括三种情况：还没到连续两次空结果、
    上一次的检索词切不出词、或者候选词全都在此之前搜过了。
    **宁可返回 None 也不返回一个注定重复的检索词**：那只是再花一次预算
    去换回同一批帖子。
    """
    empties = consecutive_empty(attempts)
    if empties < BROADEN_AFTER_EMPTY:
        return None

    history = [_query(attempt) for attempt in (attempts or [])]
    # 上一次的检索词切不出词，就没有任何"哪几个词太多"的判断依据。
    if not split_terms(history[-1] if history else ""):
        return None

    # 已经搜过的**整条检索词**不再重复。注意比较的是整条，不是单个词：
    # `脱离世俗` 作为 4 词 AND 的一半没命中，不代表它单独搜也空——
    # 实测它单独搜回来 25 条。逐词排除会把最好的候选白白丢掉。
    tried = {_normalize(text) for text in history}
    tried.add(_normalize(current))

    pool = _ordered_unique(
        term
        for text in [*history, current]
        for term in split_terms(text)
        if search_query.usable(term)
    )
    # 目标里出现过的词优先，其余按它第一次出现在检索历史里的位置
    # （越早出现的越是 Planner 一开始就认为核心的那个）。
    ranked = sorted(
        enumerate(pool),
        key=lambda pair: (0 if goal and pair[1] in goal else 1, pair[0]),
    )
    for _, term in ranked:
        if _normalize(term) not in tried:
            return term
    return None
