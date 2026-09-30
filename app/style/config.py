"""`config/style.yaml` 的加载与校验。

与 `app/agent/budget.py::load_budget` 同一套写法：pydantic 模型做形状与边界，
`yaml.safe_load` 读文件，缺项用默认值。放配置而不是代码常量，是因为这些数会
随着对样本的认知而调——工作单第十一节明确要求阈值可配，不要硬编码。

**每加载一次就校验一次。** 一个越界的阈值（比如 `min_length > max_length`）
在采集脚本里表现为"一条样本都没采到"，而那个现象离原因很远。在这里挡住，
错误信息才指得准。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import settings
from app.style.taxonomy import ACCOUNT_CATEGORIES


class AccountConfig(BaseModel):
    """一个**由人指定**的采样账号（工作单第七、八、三十节）。

    刻意只有这几个字段：没有粉丝数、没有评分、没有优先级。工作单把
    "高粉 ≠ 高质量"写成硬要求，所以这里**没有任何可以拿来排序的字段**——
    想排也排不了。

    `category` 只在写库时存成字符串，但**在这里就校验**：一个拼错的分类
    （`"大模型 "`、`"LLM"`）不会让任何东西报错，它只会让"按分类统计"
    这件事悄悄失去意义，而语料已经采完了。
    """

    username: str
    display_name: str | None = None
    category: str | None = None
    source_type: str = "manual"
    enabled: bool = True

    @field_validator("username")
    @classmethod
    def _clean_username(cls, value: str) -> str:
        # 允许写成 `@name`：那是最自然的写法，而"因为它前面多了个 @ 就静默
        # 采不到任何东西"是一个只会在采集脚本里表现为"0 条样本"的错误。
        cleaned = value.strip().lstrip("@")
        if not cleaned:
            raise ValueError("accounts[].username 不能为空")
        return cleaned

    @field_validator("category")
    @classmethod
    def _check_category(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if cleaned not in ACCOUNT_CATEGORIES:
            raise ValueError(
                f"未知的分类 {value!r}；可选：{'、'.join(ACCOUNT_CATEGORIES)}"
            )
        return cleaned


class CorpusConfig(BaseModel):
    """一条帖子够不够格进 Style Corpus。阈值全部来自配置。"""

    window_days: int = Field(default=6, ge=1, le=7)
    #: X 的 /2/tweets/search/recent 要求 [10, 100]，越界会被直接拒绝。
    max_results_per_request: int = Field(default=50, ge=10, le=100)
    #: 把采集窗口切成几段来问。一次请求最多回 100 条**且不翻页**，所以
    #: `1` 意味着只读到窗口最新的一页——报告上写着"窗口 6 天"，实际看到的
    #: 只是窗口顶端。默认 `1` 是因为它是"逐位等价于切片之前"的那个值，
    #: 单次请求也最省额度；要真正读完窗口就在配置里显式调大。
    #: 上限 24：再细的切分只是把同一个窗口问很多遍，不会多出材料。
    slices_per_window: int = Field(default=1, ge=1, le=24)

    min_likes: int = Field(default=5, ge=0)
    engagement_weights: dict[str, int] = Field(
        default_factory=lambda: {"likes": 1, "reposts": 2, "replies": 2, "quotes": 3}
    )
    min_engagement_score: int = Field(default=20, ge=0)

    min_length: int = Field(default=40, ge=1)
    max_length: int = Field(default=1200, ge=1)

    exclude_reposts: bool = True
    exclude_link_only: bool = True
    #: 剔除"回复别人"的帖子。默认剔除：回复没有自己的开头与收尾，
    #: 与一条独立发言是两种不同的写作行为。与 `exclude_reposts` 分开，
    #: 是因为它们会被 X 塞进同一个 `referenced_tweets` 列表里。
    exclude_replies: bool = True

    @model_validator(mode="after")
    def _check_ranges(self) -> "CorpusConfig":
        if self.min_length >= self.max_length:
            raise ValueError(
                f"corpus.min_length ({self.min_length}) 必须小于 "
                f"corpus.max_length ({self.max_length})，否则没有任何样本能通过"
            )
        return self


class RecipeConfig(BaseModel):
    """从 Profile 推导 StyleRecipe 时的分档切点。"""

    min_target_length: int = Field(default=80, ge=1)
    max_target_length: int = Field(default=600, ge=1)

    density_low_below: float = Field(default=1.0, ge=0.0)
    density_high_above: float = Field(default=4.0, ge=0.0)

    stance_strong_at: int = Field(default=2, ge=1)
    stance_moderate_at: int = Field(default=1, ge=0)

    evidence_strict_at: int = Field(default=4, ge=1)
    evidence_required_at: int = Field(default=1, ge=0)

    conversational_emoji_above: float = Field(default=0.5, ge=0.0)

    @model_validator(mode="after")
    def _check_order(self) -> "RecipeConfig":
        if self.min_target_length >= self.max_target_length:
            raise ValueError("recipe.min_target_length 必须小于 max_target_length")
        if self.density_low_below >= self.density_high_above:
            raise ValueError("recipe.density_low_below 必须小于 density_high_above")
        if self.stance_moderate_at > self.stance_strong_at:
            raise ValueError("recipe.stance_moderate_at 不能大于 stance_strong_at")
        if self.evidence_required_at > self.evidence_strict_at:
            raise ValueError("recipe.evidence_required_at 不能大于 evidence_strict_at")
        return self


class StyleConfig(BaseModel):
    accounts: list[AccountConfig] = Field(default_factory=list)
    corpus: CorpusConfig = Field(default_factory=CorpusConfig)
    recipe: RecipeConfig = Field(default_factory=RecipeConfig)

    @model_validator(mode="after")
    def _check_unique_usernames(self) -> "StyleConfig":
        """同一个账号写两遍要报错，不能静默去重。

        两行同名而 `enabled` 相反时，"这个账号到底采不采"取决于哪个先被处理，
        而这是个无解的问题。在配置层拒绝，错误信息才能指到行。
        """
        seen: dict[str, int] = {}
        for index, account in enumerate(self.accounts):
            if account.username in seen:
                raise ValueError(
                    f"accounts 里 {account.username!r} 出现了两次"
                    f"（第 {seen[account.username] + 1} 项与第 {index + 1} 项）"
                )
            seen[account.username] = index
        return self


def load_style_config(path: str | None = None) -> StyleConfig:
    """读 `config/style.yaml`。文件不存在时用全默认值。

    文件缺失回退到默认值而不是报错：阈值有合理的默认，而"配置文件没放好"
    不该让整条 Agent 链路起不来。真的越界（比如 min > max）仍然会抛——
    那是配置写错了，不是配置没写。
    """
    file_path = Path(path or settings.style_config_path)
    if not file_path.exists():
        return StyleConfig()
    data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    return StyleConfig(**data)


__all__ = [
    "AccountConfig",
    "CorpusConfig",
    "RecipeConfig",
    "StyleConfig",
    "load_style_config",
]
