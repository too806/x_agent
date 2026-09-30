"""运行期取用 Style Recipe 的唯一入口（Phase 5B.2）。

## 它为什么是一个独立模块

候选工具要做的不只是"读一行 Profile"：它要处理"还没有 Profile"、
"数据库暂时不可用"、"这次运行已经选过 recipe"三种情况，并且**每一条路径
都要拿到一个 recipe**（哪怕是中性的）。把这些判断散在工具里，会让
`delta` 的某条提前返回路径漏掉 `style_recipe`，于是 Critic 那一轮
没有 recipe 可判——而降级是静默的，测试也看不出来。

所以收成一个函数，工具只负责调用它，然后无条件把结果写进 delta。

## profile_ref

recipe 里带上 `corpus@<computed_at>`，Run Detail 事后能据此回到那一版 Profile。
没有一个可回指的引用，recipe 就是一组无法复核的参数。
"""

from __future__ import annotations

import logging
from typing import Any

from app.style.config import load_style_config
from app.style.profile import features_from_row
from app.style.recipe import CORPUS_PROFILE_REF_PREFIX, build_recipe, evidence_signals
from app.style.schemas import StyleRecipe

logger = logging.getLogger(__name__)


async def resolve_recipe(
    session_factory: Any | None,
    evidence_pack: Any | None = None,
    *,
    existing: StyleRecipe | None = None,
    hard_max_length: int | None = None,
) -> StyleRecipe:
    """产出本次运行采用的 StyleRecipe。

    `existing` 非空时**直接复用**：一次运行里可能多次调用候选工具
    （第一轮写三条、改稿后再补），而"结构"应当在整个运行里保持一致——
    每轮重算会让同一个 Run 的两批候选遵循不同骨架，Critic 也就没有
    统一的目标可比对。

    `hard_max_length` 由**调用方**传入（`persona.max_length`）。这一层
    刻意不去读 persona：`app/style/` 是离线语料层，让它 import
    `app/agent/` 会把这个仓库里唯一的方向（运行期依赖风格层，而不是
    反过来）掉个头。于是"平台上限"始终是**从外面递进来的一个事实**，
    而不是风格层自己去猜的东西。

    `session_factory` 为 `None`（老测试直接构造工具）或数据库不可用时，
    退化成一个中性 recipe。**这不会让运行失败**：结构约束是增强，不是
    前置条件，而 `{{STYLE}}` 永远会拿到一段文本（见
    `app/agent/prompts.py::format_style_recipe`），不会留下未替换的占位符。
    """
    if existing is not None:
        return existing

    if session_factory is None:
        return build_recipe(None, hard_max_length=hard_max_length)

    try:
        from app import repositories

        async with session_factory() as session:
            row = await repositories.get_latest_corpus_profile(session)
    except Exception:  # noqa: BLE001 - 见 docstring：结构约束不该让运行失败
        logger.warning("style profile lookup failed; falling back to neutral recipe", exc_info=True)
        return build_recipe(None, hard_max_length=hard_max_length)

    if row is None:
        return build_recipe(None, hard_max_length=hard_max_length)

    profile = features_from_row(row)
    computed_at = getattr(row, "computed_at", None)
    profile_ref = (
        f"{CORPUS_PROFILE_REF_PREFIX}@{computed_at.isoformat()}" if computed_at else ""
    )
    return build_recipe(
        profile,
        evidence_signals(evidence_pack),
        config=load_style_config(),
        profile_ref=profile_ref,
        hard_max_length=hard_max_length,
    )


__all__ = ["resolve_recipe"]
