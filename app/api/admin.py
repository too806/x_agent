"""运营控制台（Phase 4b）的 Admin API。

**本模块只有两个写端点，而且都不改运行行为。**

- 控制台展示的一切**运行状态**，其迁移路径继续由 Phase 3/4a 决定——审核走
  `POST /api/runs/{id}/review`，发布走 `POST /api/runs/{id}/publish`，
  裁决未知结果走 `POST /api/runs/{id}/publish/resolve`。前端调用这些既有端点，
  而不是在这里新增一套"控制台专用的写入"。
- 两套写路径一定会漂移，而漂移的那一套多半会漏掉 Phase 4a 用条件 UPDATE 换来的
  并发与幂等保护。宁可让前端多调一个既有端点。

唯一的例外是 Phase 5D 的 `POST /api/admin/learning/proposals/{id}/review`：
它记的是**人对一条学习提案的裁决**，而这件事在 Phase 3/4a 的写路径里根本没有
对应物——它不是"改变某次运行"，是"记下某次决定"。它写下的那一刻什么都不发生：
不改 Prompt、不改 StyleRecipe、不改权重 / 阈值 / 预算、不发布。所以它不违反上面
那条边界，反而是那条边界要求把它单独放出来：
`app/services/admin.py` 到 `app/repositories.py` 的箭头只通向数据库。

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
    LearningOverview,
    LearningProposalRow,
    OverviewResponse,
    ProposalReviewDecision,
    ProposalsOverview,
    PublishedListPage,
    ReviewQueuePage,
    RunListPage,
)
from app.services.admin import (
    PAGE_SIZE_DEFAULT,
    PAGE_SIZE_MAX,
    PROPOSAL_REVIEW_NOT_FOUND,
    AdminService,
    LearningProposalReviewError,
)


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

    @router.get("/learning", response_model=LearningOverview)
    async def get_learning(
        window: str | None = Query(
            None,
            description="观察窗口（0-1h / 1-6h / 6-24h / 24-72h / >72h）；不传则取读数最多的一档",
        ),
        service: AdminService = Depends(get_admin_service),
        runtime=Depends(runtime_dep),
    ) -> LearningOverview:
        """反馈够不够可靠，可以作为未来学习材料（Phase 5C）。

        只读，而且**不改任何东西**：这个接口回答的是一句话，不触发任何调整。
        它返回的仍然是观察——每个分组都带着样本量、观察窗口与选取规则，
        每条比较都带着它是怎么成立的（或为什么不成立）。

        当前真实数据（1 条帖 / 2 条观察）下正确的输出是
        `readiness.learning_ready=false` + `reason="insufficient_sample"`，
        不是一个看起来中性的空报告。

        这是控制台里唯一一个**按条读 checkpoint** 的接口（分组维度只存在于
        checkpoint），所以它有读取上界并把到达上界这件事写进 `limitations`。

        Phase 5D 起多带一个 `proposal_count`（提案条数）。它是**现算**的，
        所以这个接口的成本并没有因为多这一个数字而变成两次报告——完整提案在
        下面那个端点里。
        """
        return await service.learning(
            window=window, hard_max_length=runtime.persona.max_length
        )

    @router.get("/learning/proposals", response_model=ProposalsOverview)
    async def list_learning_proposals(
        window: str | None = Query(
            None,
            description="观察窗口（0-1h / 1-6h / 6-24h / 24-72h / >72h）；不传则取读数最多的一档",
        ),
        service: AdminService = Depends(get_admin_service),
        runtime=Depends(runtime_dep),
    ) -> ProposalsOverview:
        """提案一览：**可以尝试什么**（Phase 5D）。

        只读。它回答的是"如果将来要试，可以试哪几个方向"，而**不**执行其中
        任何一个——本工程里没有 `apply`，没有自动改 Prompt / StyleRecipe /
        权重 / 阈值 / 预算的路径，也没有自动发布。

        三件事随每条提案一起下发，缺一条这条提案就不该被读：依据了哪几条
        证据（`evidence_ids`）、依据了多少条**帖子**（`sample_count`）、
        边界在哪（`limitations`）。`confidence` 恒为 `null`——本仓库没有
        校准过的概率模型。

        闸门关着时（`learning_ready=false`）返回**零条提案**加一句原因
        （工作单第七节）。真实数据现在是 1 条帖 / 2 条观察，所以这就是当前的
        正确输出，而不是一次失败。
        """
        return await service.learning_proposals(
            window=window, hard_max_length=runtime.persona.max_length
        )

    @router.post("/learning/proposals/{proposal_id}/review", response_model=LearningProposalRow)
    async def review_learning_proposal(
        proposal_id: str,
        decision: ProposalReviewDecision,
        window: str | None = Query(
            None, description="判定这条提案时使用的观察窗口，需与列表页一致"
        ),
        service: AdminService = Depends(get_admin_service),
        runtime=Depends(runtime_dep),
    ) -> LearningProposalRow:
        """人工批准 / 否决一条提案（Phase 5D）。

        **这是整个控制台里唯一的写端点。** 它写下的只有一条裁决（谁在什么时候
        对哪条建议做了什么决定），而按下之后**什么都不会发生**：不会改 Prompt、
        不会改 StyleRecipe、不会改任何权重或阈值、不会发布。批准的意义是
        "记下来，作为下一阶段执行的数据"（工作单第十一、三十五节）。

        两种拒绝：

        - `404`：这条提案**在当前观察下不存在**。可能 id 写错了，也可能是它
          依据的比较已经不成立。刻意不回退到库里存过的内容——那会批准一条
          已经不成立的建议。
        - `409`：这条提案已经被裁决过。裁决是不可改写的审计记录。

        `proposal_id` 用 `str` 而不是 `uuid.UUID`：它是确定性的语义 key
        （形如 `proposal:hook_type@24-72h`），不是数据库主键，写成 UUID 会在
        路径校验阶段就被拒掉，而且拒得让人看不懂。
        """
        try:
            return await service.review_learning_proposal(
                proposal_id,
                action=decision.action,
                note=decision.note,
                window=window,
                hard_max_length=runtime.persona.max_length,
            )
        except LearningProposalReviewError as exc:
            status_code = (
                404 if exc.reason == PROPOSAL_REVIEW_NOT_FOUND else 409
            )
            raise HTTPException(status_code=status_code, detail=exc.detail) from exc

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
