from typing import TYPE_CHECKING

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.style_sample import StyleSample


class TrackedAccount(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """一个**由人指定**的样本来源账号（Phase 5B.2）。

    这张表存在的唯一理由是：Style Corpus 的取样范围必须是人明确划出来的，
    不是系统自己找出来的。工作单第七、八、三十节把这条写成硬约束——
    "高粉 ≠ 高质量"、"高互动 ≠ 适合学习的写法"，所以本表：

    - **没有**任何自动发现/自动纳排的机制。代码只负责采集、统计、提炼。
    - **没有**排序字段，也不参与任何排行榜。
    - 不进 Agent 运行期。它是 offline 的采集输入，不是运行时依赖。

    为什么不复用已有的 `account` 表：那张表在本仓库里**从未被任何代码引用过**
    （除模型定义、`__init__` 导出与初次迁移），其字段形状与语义指向的很可能
    是"我们自己发布用的账号"。把它改造成"用来学写法的样本账号"，
    等于让两个含义不同的东西共用一个表名，将来一定有人按错的那半理解。

    与 `StyleSample` 是 `1:N`：一个账号采到多条样本。删除账号时样本
    `CASCADE` 一起走——样本脱离来源账号就没有意义。
    """

    __tablename__ = "tracked_account"

    username: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: 账号自述。采集时从 X 的 user.fields=description 回填，人工可覆盖。
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 分类，取值见 `app/style/taxonomy.py::ACCOUNT_CATEGORIES`。
    #: 存字符串而不是数据库枚举：分类会随认知调整，改一个 Python 常量
    #: 比改一次数据库类型容易，而这里没有需要数据库强约束的不变量。
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 来源类型，例如 manual / import。保留是为了将来能区分"人手动加的"
    #: 与"从别处导进来的"，不是自动发现的钩子。
    source_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 关掉一个账号后，采集脚本跳过它，但历史样本与 Profile **不动**——
    #: 停采不等于否认已经采到的数据。
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    samples: Mapped[list["StyleSample"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<TrackedAccount @{self.username} enabled={self.enabled}>"


__all__ = ["TrackedAccount"]
