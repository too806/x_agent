"""运营控制台（Phase 4b）的只读 Admin API。

**本模块只暴露 GET，一个写端点都没有。** 这不是暂时没做，是刻意的边界：

- 控制台展示的一切状态，其迁移路径继续由 Phase 3/4a 决定——审核走
  `POST /api/runs/{id}/review`，发布走 `POST /api/runs/{id}/publish`，
  裁决未知结果走 `POST /api/runs/{id}/publish/resolve`。前端调用这些既有端点，
  而不是在这里新增一套"控制台专用的写入"。
- 两套写路径一定会漂移，而漂移的那一套多半会漏掉 Phase 4a 用条件 UPDATE 换来的
  并发与幂等保护。宁可让前端多调一个既有端点。

本模块同样**不写 SQL**：查询全部落在 `app/services/admin.py` → `app/repositories.py`。

关于 `create_admin_router(runtime_dep)` 这个工厂：`get_runtime` 定义在 `app/main.py`，
而 `main.py` 需要 import 本模块来挂载路由——直接在本模块顶层 import 会形成循环。
工厂把依赖以**函数对象**的形式注入，既绕开了循环，又保住了 FastAPI 的依赖覆盖：
`app.dependency_overrides[get_runtime]` 之所以在测试里有效，正是因为这里用的就是
同一个 `get_runtime` 对象。若改成在函数体内 `from app.main import get_runtime` 直接
调用，覆盖会静默失效，测试会把生产 runtime 当成被测对象。
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from app.schemas import (
    AdminRunDetail,
    AnalyticsResponse,
    FeedbackOverview,
    OverviewResponse,
    PublishedListPage,
    ReviewQueuePage,
    RunListPage,
)
from app.services.admin import PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX, AdminService


def create_admin_router(runtime_dep) -> APIRouter:
    router = APIRouter(prefix="/api/admin", tags=["admin"])

    def get_admin_service(runtime=Depends(runtime_dep)) -> AdminService:
        # 每次请求构造一个，无状态、开销可忽略。AdminService 只持有
        # session_factory 与 graph 两个引用，不建立任何自己的连接池或生命周期。
        return AdminService(
            session_factory=runtime.session_factory,
            graph=runtime.graph,
        )

    @router.get("/overview", response_model=OverviewResponse)
    async def get_overview(
        days: int = Query(7, ge=1, le=365, description="趋势窗口，含今天在内的自然日数"),
        service: AdminService = Depends(get_admin_service),
    ) -> OverviewResponse:
        """首页所需的全部数据：KPI、两条趋势、两个待处理队列。

        刻意做成**一个**接口：首页如果靠五个请求拼装，用户会看到五个独立加载的
        区块陆续跳出，而且任何一个失败都会让首页半空——而这三类数据本来就是同
        一个时刻的系统快照。
        """
        return await service.overview(days=days)

    @router.get("/runs", response_model=RunListPage)
    async def list_runs(
        status: str | None = Query(None, description="agent_run.status 精确匹配"),
        topic: str | None = Query(None, max_length=200, description="topic 模糊匹配"),
        date_from: datetime | None = Query(None, description="含（按 created_at）"),
        date_to: datetime | None = Query(None, description="不含（半开区间右端）"),
        page: int = Query(1, ge=1),
        page_size: int = Query(PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX),
        service: AdminService = Depends(get_admin_service),
    ) -> RunListPage:
        """分页的 Run 列表。分页在服务器端完成，不提供"取全表"的用法。

        `status` 是精确匹配：传入未知值只会得到空页，不报错。刻意不做取值白名单
        ——那需要在两处维护同一份状态枚举，漏改一处就会出现"明明有这个状态却报 422"。
        """
        return await service.list_runs(
            status=status,
            topic=topic,
            date_from=date_from,
            date_to=date_to,
            page=page,
            page_size=page_size,
        )

    @router.get("/runs/{run_id}", response_model=AdminRunDetail)
    async def get_run_detail(
        run_id: uuid.UUID,
        service: AdminService = Depends(get_admin_service),
    ) -> AdminRunDetail:
        """单个 Run 的完整生命周期：库里的全部关联记录 + checkpoint 里的审核/护栏结论。

        这是控制台里**唯一**读 checkpoint 的单条接口。聚合接口刻意不读它（见
        `app/services/admin.py` 的模块说明）。
        """
        detail = await service.run_detail(str(run_id))
        if detail is None:
            raise HTTPException(status_code=404, detail="run not found")
        return detail

    @router.get("/reviews", response_model=ReviewQueuePage)
    async def list_reviews(
        page: int = Query(1, ge=1),
        page_size: int = Query(PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX),
        service: AdminService = Depends(get_admin_service),
        runtime=Depends(runtime_dep),
    ) -> ReviewQueuePage:
        """待人工审核的队列（`waiting_for_review`），最老的排最前。

        只返回"待办"本身。审核**决定**要提交请调用既有的
        `POST /api/runs/{run_id}/review` —— 控制台不实现第二套审核入口。

        `content_max_length` 在这里补上：审核队列本身不关心它，但审核工作台要用它
        渲染字数计数，而它是 persona 配置、不属于 AdminService 的查询职责。
        """
        return await service.review_queue(
            page=page,
            page_size=page_size,
            content_max_length=runtime.persona.max_length,
        )

    @router.get("/published", response_model=PublishedListPage)
    async def list_published(
        status: str | None = Query(None, description="published_post.status 精确匹配"),
        unresolved: bool = Query(
            False,
            description="只看 publishing / publish_unknown（待人工裁决），并按紧急程度排序",
        ),
        date_from: datetime | None = Query(None, description="含（按 created_at）"),
        date_to: datetime | None = Query(None, description="不含（半开区间右端）"),
        page: int = Query(1, ge=1),
        page_size: int = Query(PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX),
        service: AdminService = Depends(get_admin_service),
    ) -> PublishedListPage:
        """发布记录列表（含 published / failed / publishing / publish_unknown 四种）。

        刻意不过滤掉未成功的行：`failed` 与 `publish_unknown` 正是运维需要盯着看的
        东西，把它们从"发布记录"列表里藏起来只会让人以为一切正常。

        本系统是**审计记录**，不是 X 当前状态的实时镜像：人工去 X 上删了推文，这里
        的行不会跟着变。所以列表不提供任何删除或"标记删除"操作。
        """
        return await service.list_published(
            status=status,
            unresolved=unresolved,
            date_from=date_from,
            date_to=date_to,
            page=page,
            page_size=page_size,
        )

    @router.get("/feedback", response_model=FeedbackOverview)
    async def get_feedback(
        service: AdminService = Depends(get_admin_service),
    ) -> FeedbackOverview:
        """我们**自己过去发布内容**被观察到的结果（Phase 5B.4）。

        只读：报告由已存的快照现算，接口不触发任何采集。要采集请手工运行
        `scripts/refresh_post_metrics.py`（本阶段刻意没有调度器）。

        一条快照都没有时返回 `available=false` + 原因，**不返回一串 0**——
        "没采过"与"采到了，结果是零"在处置上完全不同。
        """
        return await service.feedback()

    @router.get("/analytics", response_model=AnalyticsResponse)
    async def get_analytics(
        days: int = Query(7, ge=1, le=365, description="趋势窗口，含今天在内的自然日数"),
        service: AdminService = Depends(get_admin_service),
    ) -> AnalyticsResponse:
        """系统运行分析。

        统计的是**本系统自己的**运行数据。`x_content_performance` 是唯一一个
        关于外部世界的项：Phase 5B.4 起它在**已有快照**时标为可用并附口径说明
        （只有公开计数、手工采集），**没有快照**时标为不可用并给出原因——
        两种情况都不返回 0 冒充。
        """
        return await service.analytics(days=days)

    return router
