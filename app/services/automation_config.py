"""自动发布的配置：开关、模式、额度、判据阈值（`config/automation.yaml`）。

与 `app/agent/budget.py::load_budget` / `app/style/config.py` 同一套做法：
pydantic 模型 + 一个薄加载函数，阈值不写进代码。

**一处刻意的不同：这个配置每个 tick 都重新读。**

`load_budget` 在一次运行开始时读一次就够了——预算是那一次运行的属性。
本配置里的 `enabled` 是**刹车**，而刹车必须能在不重启进程的前提下生效。
所以自动发布的编排器在每个 tick 的第一件事就是调 `load_automation_config()`，
拿到什么就按什么办。代价是每个 tick 一次文件读，那是免费的。

配置坏了怎么办？**加载函数抛异常，由 tick 捕获并跳过本次**。让一个写坏的
YAML 把调度器整个搞崩，等于把一个"少发几条"的小事故变成"没人知道它停了"
的大事故——而无人值守的东西最怕的就是**静默地不干活**。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import settings
from app.models.auto_review import MODE_LIVE, MODE_SHADOW

DEFAULT_RECENT_WINDOW_DAYS = 14
DEFAULT_RECENT_CORPUS_LIMIT = 50

#: 合法的 mode 取值。从模型层的常量派生，而不是在这里再写一遍字面量——
#: 配置里的 mode 和 `auto_review.mode` 那一列必须是同一套取值，
#: 各写一份迟早会分叉成"配置说 live、库里存着 Live"。
ALLOWED_MODES: frozenset[str] = frozenset({MODE_SHADOW, MODE_LIVE})


class AutomationConfig(BaseModel):
    """`config/automation.yaml` 的强类型形式。

    每个字段都有默认值，所以配置文件缺项时用的是**安全的那个默认**——
    尤其 `enabled=False` 与 `mode=shadow`：一个读不完整的配置应当什么都不做，
    而不是按一份不完整的意图去发帖。
    """

    #: 总开关。false = 连 run 都不建。
    enabled: bool = False
    #: 见 `config/automation.yaml` 的说明。取值的合法集合派生自模型层常量
    #: （见 ALLOWED_MODES），由下面的校验器把关。
    mode: str = MODE_SHADOW
    #: Agent 链路的目标池，按自动 Run 的累计条数轮流取。
    goals: list[str] = Field(default_factory=list)

    interval_minutes: int = Field(default=360, ge=1)
    max_auto_runs_per_day: int = Field(default=3, ge=1)
    max_publishes_per_day: int = Field(default=1, ge=0)

    min_critic_score: float = Field(default=7.0, ge=0.0, le=10.0)

    recent_window_days: int = Field(default=DEFAULT_RECENT_WINDOW_DAYS, ge=1)
    recent_corpus_limit: int = Field(default=DEFAULT_RECENT_CORPUS_LIMIT, ge=1)

    tick_timeout_seconds: float = Field(default=900.0, gt=0)

    @field_validator("mode")
    @classmethod
    def _mode_must_be_known(cls, value: str) -> str:
        """写错的 mode 必须响亮地失败。

        静默接受未知值时，`mode: Shadow`（大小写错了）会被当成"不是 live"，
        也就是**悄悄退化成影子模式**——本来说好了要发，结果一直没发，
        而且没有任何地方说得出为什么。宁可让 tick 报错跳过。
        """
        if value not in ALLOWED_MODES:
            raise ValueError(
                f"mode must be one of {sorted(ALLOWED_MODES)}, got {value!r}"
            )
        return value

    @model_validator(mode="after")
    def _enabled_needs_a_goal(self) -> "AutomationConfig":
        """开着却没有目标，是配置错误而不是"跑一次空转"。

        Agent 链路必须有一个 goal 才知道要研究什么（`create_agent_run` 要求
        topic / goal 二选一）。放开这个组合，tick 会在建 Run 那一步抛异常，
        于是每 N 分钟失败一次——而失败信息只会说"exactly one of topic / goal"，
        看起来像代码坏了。不如在这里就拒绝加载。
        """
        if self.enabled and not [g for g in self.goals if g.strip()]:
            raise ValueError("automation is enabled but `goals` is empty")
        return self

    @property
    def is_shadow(self) -> bool:
        return self.mode == MODE_SHADOW

    def choose_goal(self, runs_so_far: int) -> str:
        """按**累计自动 Run 条数**轮流取一个目标。确定性，不用随机。

        随机能避免连续两天写同一个题，但代价是"昨天为什么写这个"再也无法从
        配置复现。轮换给出同样的多样性，而且是可复现的——重跑同一份配置、
        从同一个计数出发，一定得到同一个目标。
        """
        pool = [g for g in self.goals if g.strip()]
        if not pool:  # pragma: no cover - 由 _enabled_needs_a_goal 挡住
            raise ValueError("no usable goal in automation config")
        return pool[runs_so_far % len(pool)]


def load_automation_config(path: str | None = None) -> AutomationConfig:
    """读一次配置。**调用方每个 tick 都要重新调它**——见模块说明。

    文件缺失或 YAML / 字段非法时抛异常，由 `AutoPublishService.tick()` 捕获后
    跳过本次。刻意**不**回退到一份内置的宽松默认：那是"配置读不到就按代码里
    的意图发帖"，比停下来危险得多。
    """
    file_path = Path(path or settings.automation_config_path)
    data = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    return AutomationConfig(**data)


__all__ = ["AutomationConfig", "load_automation_config"]
