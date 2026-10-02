from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator

from app.agent.schemas import ViewpointAssessment
from app.style.schemas import AiStyleSignals


class ResearchResult(BaseModel):
    """X API 搜索结果映射后的研究条目。

    **三个时间字段的语义必须分清**（Phase 5B.1 第一节）：

    - `published_at` —— X 侧原帖的发布时间。**来源给的时间**，可能缺失
      （X 对 search/recent 通常会给 `created_at`，但这里不假设它一定有）。
    - `observed_at` —— 本次从 X API **取回**这条帖子的时刻。由 `XService` 在
      映射时统一打上，同一次 search 取回的所有条目共享同一个值（一次检索
      就是一次观察）。
    - `created_at` —— 行落库的时刻，属于数据库层（`research_item.created_at`），
      不在本模型里。

    `observed_at` 与 `created_at` 在正常路径上几乎同时，但它们是**两件事**：
    前者是"Agent 什么时候看到的这条讨论"，后者是"这行什么时候写进库"。
    Phase 5B.1 之前 second 的语义被隐含在 `created_at` 里，于是"帖子多新"
    和"这条记录多新"分不开——而 trend 的时效判断必须问的是前者。

    `likes` / `reposts` / `replies` / `quote_count` 是 **observed engagement**：
    X 在响应里给出的四个公开计数，原样记录，不做任何解释或加权。它们**不是**
    "热度排名"，也不代表 X 官方的任何排序信号。
    """

    platform_post_id: str
    author_username: str | None = None
    #: 作者显示名与自述，来自 `expansions=author_id` 的 `includes.users`。
    #: Phase 5B.2 加的，用途只有一个：回填 `tracked_account` 的元信息。
    #: 采集侧本来就要这两个字段（`user.fields` 已经要了），
    #: 在映射时丢掉它们等于让人事后凭记忆补账号名——那是编造，不是记录。
    author_name: str | None = None
    author_description: str | None = None
    content: str
    url: str | None = None
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    # X v2 `public_metrics` 里就有这一项，Phase 5B.1 之前映射时被丢掉了。
    # 丢掉会让 trend 的 engagement 口径不完整：引用是"讨论被放大"的信号，
    # 与转发（分发）和点赞（认可）不是同一件事。
    quote_count: int = 0
    published_at: datetime | None = None
    observed_at: datetime | None = None
    # Phase 5B.2：这条是不是转发/引用他人内容（X 的 `referenced_tweets` 非空）。
    # 加在这里而不是让 corpus 自己再解析一遍原始响应：来源语义的唯一落点
    # 是本模型（见模块 docstring），多一个解析点就多一处会漂的口径。
    # 默认 False 而不是 None：trend 路径拿不到这个信息时，"不是转发"与
    # "不知道是不是转发"在使用上等价（那两条路径都不据此丢材料）。
    is_repost: bool = False
    #: 这条是不是**回复**别人的帖子。
    #:
    #: 必须与 `is_repost` 分开：X 的 `referenced_tweets` 是一个**混装**列表，
    #: `type` 可以是 `retweeted` / `quoted` / `replied_to` 三种。只看"非空"，
    #: 一条回复就会被标成转发，于是 corpus 把它剔掉并写下一个**错误的理由**
    #: ——而"理由必须是最具体的那一条"正是筛选层存在的意义（工作单第十一节）。
    #:
    #: 一条回复与一条独立发言是两种不同的写作行为：回复是对着别人的半句话
    #: 接话，没有自己的开头与收尾。学"一段完整表达怎么写"时把它们混进来，
    #: 会让 Profile 的段落数、开头分布与结尾分布一起失真。
    is_reply: bool = False


class PostMetricsObservation(BaseModel):
    """**我们自己已发布帖子**在某一时刻的公开指标（Phase 5B.4）。

    与 `ResearchResult` 的关键区别，也是本模型存在的理由：

    - `ResearchResult` 描述的是**别人发的**帖子（检索回来的材料），缺失的计数
      填 0 是那一路径的既定口径。
    - 本模型描述的是**我们自己发的**帖子后来怎么样了，而这里"没拿到"和"是 0"
      必须分开：0 表示观测到"没人互动"，NULL 表示"这次没读到"。用 0 冒充未知，
      会让一条被 X 限流、字段缺失的帖子看起来像一条没人理的帖子——而这两种
      情况对反馈的含义完全相反。

    因此所有计数默认 `None`，且**永远不由客户端补默认值**。

    ## 时间

    只有 `observed_at` 这**一个**时间字段：**我们**抓取的时刻，一次抓取一个值。
    发布时刻不在这里——它已经在 `published_post.published_at` 上（来源给定），
    重复采集只会制造第二个来源。`hours_since_publish` 由调用方用两者相减得出。
    """

    platform_post_id: str
    likes: int | None = None
    replies: int | None = None
    reposts: int | None = None
    quote_count: int | None = None
    #: v1 恒为 None：需要 user-context 的 non_public_metrics，当前没有采集路径。
    #: 字段留着，是为了将来有来源时下游不必改形状。
    impressions: int | None = None
    bookmarks: int | None = None
    observed_at: datetime


class GeneratedDraft(BaseModel):
    """LLM 结构化输出，经 schema validation。"""

    content: str
    hook_type: str
    topic: str


class RunRequest(BaseModel):
    """一次运行的输入。

    `topic` 与 `goal` 二选一，恰好一个：

    - 只给 topic → Phase 1–4 的固定链路（行为与之前逐字相同）；
    - 只给 goal → Phase 5A 的 Agent 自主循环，题目由 Agent 自己发现。

    两个都给是**有歧义**的：那时谁定题目？固定在 topic 上等于悄悄忽略了目标，
    交给 Agent 又等于忽略了调用方明确给的题目。有歧义的输入拒收（422），
    比猜一个更安全——猜错的表现是"运行结果和输入对不上"，很难查。
    """

    topic: str | None = None
    goal: str | None = None

    @model_validator(mode="after")
    def _exactly_one_of_topic_goal(self) -> "RunRequest":
        if (self.topic is None) == (self.goal is None):
            raise ValueError("必须且只能提供 topic 或 goal 其中之一")
        return self


class ReviewPayload(BaseModel):
    """interrupt() 交给人工审核者的内容。

    同时是 GET/POST 响应里 review 字段的形状，字段与 human_review 节点
    构造的 payload 一一对应。
    """

    type: str = "content_review"
    run_id: str
    draft_id: str | None = None
    content: str | None = None
    guardrail_passed: bool = False
    guardrail_reasons: list[str] = []


class ReviewRecord(BaseModel):
    """review 表里已保存的审核记录（人工决定之后才存在）。"""

    review_id: str
    action: str
    original_content: str
    edited_content: str | None = None
    reject_reason: str | None = None
    reviewed_at: datetime | None = None


class RunResponse(BaseModel):
    run_id: str
    status: str
    research_results: list[ResearchResult] = []
    draft_content: str | None = None
    draft_id: str | None = None
    guardrail_passed: bool | None = None
    guardrail_reasons: list[str] = []
    human_action: str | None = None
    final_content: str | None = None
    review: ReviewPayload | None = None
    error: str | None = None


class PublishRecord(BaseModel):
    """published_post 行的对外形状。

    同时返回 body 与 draft_id：edit 路径下 body 是人工改写稿、与原 draft.content
    不同，所以这一行**不能自证"我是从哪来的"**。V1 接受这一点，把两者都摆出来。
    """

    status: str
    draft_id: str | None = None
    body: str | None = None
    x_post_id: str | None = None
    published_at: datetime | None = None
    last_error: str | None = None
    # 最近一次状态写入时刻。人工裁决"结果未知"时需要它来判断卡了多久——
    # created_at 在重试过的行上不会刷新，单独看不出来。
    updated_at: datetime | None = None


def publish_record_from_row(row) -> PublishRecord:
    """published_post 行 → 对外形状。

    单一实现：`app/main.py`（发布接口）与 `app/services/admin.py`（控制台）都用它。
    两侧各自复制一份的话，将来给 published_post 加一列必须记得改两处——漏掉的那
    一处会静默少一个字段，而且不会有任何测试报错（两边都只是"少了个键"）。
    """
    return PublishRecord(
        status=row.status,
        draft_id=str(row.draft_id),
        body=row.body,
        x_post_id=row.x_post_id,
        published_at=row.published_at,
        last_error=row.last_error,
        updated_at=row.updated_at,
    )


class PublishResponse(BaseModel):
    """发布结果。

    outcome 是给调用方做分支判断的；status 是 published_post 的落库状态。
    两者不总是一致，这是刻意的：outcome="unknown" 时 status 是 "publish_unknown"，
    表示"结果未知、需要人工裁决"。
    """

    run_id: str
    outcome: str
    publish: PublishRecord | None = None
    error: str | None = None


class RunDetailResponse(BaseModel):
    run_id: str
    topic: str
    status: str
    draft_id: str | None = None
    draft_content: str | None = None
    guardrail_passed: bool | None = None
    guardrail_reasons: list[str] = []
    human_action: str | None = None
    final_content: str | None = None
    review: ReviewRecord | None = None
    publish: PublishRecord | None = None
    # Run **真正终结**的时刻。waiting_for_review / approved / edited / publishing /
    # publish_unknown 一律为空——"流水线跑完了"不等于"这个 Run 结束了"，发布是
    # 另一条时间线，由 published_post 表达。
    finished_at: datetime | None = None
    error: str | None = None


def _strip_not_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


class ApproveDecision(BaseModel):
    action: Literal["approve"]


class EditDecision(BaseModel):
    action: Literal["edit"]
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def _normalize_content(cls, value: str) -> str:
        return _strip_not_blank(value)


class RejectDecision(BaseModel):
    action: Literal["reject"]
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def _normalize_reason(cls, value: str) -> str:
        return _strip_not_blank(value)


# 人工审核决定：以 action 为判别字段的联合类型，HTTP 层与 Graph 内都走同一套校验。
ReviewDecision = Annotated[
    ApproveDecision | EditDecision | RejectDecision, Field(discriminator="action")
]
ReviewDecisionAdapter = TypeAdapter(ReviewDecision)

REVIEW_ACTION_STATUS = {"approve": "approved", "edit": "edited", "reject": "rejected"}


class MarkPublishedDecision(BaseModel):
    """人工上 X 看过，确认**确实发出去了** → 把状态补成 published。"""

    action: Literal["mark_published"]
    # 必须由人从 X 页面抄回来。不自动推导：这个值只有亲眼看过才有意义，
    # 系统自己猜出来的 post_id 正是"结果未知"要解决的问题，不是它的答案。
    x_post_id: str = Field(min_length=1)

    @field_validator("x_post_id")
    @classmethod
    def _normalize_post_id(cls, value: str) -> str:
        return _strip_not_blank(value)


class RetryPublishDecision(BaseModel):
    """人工上 X 看过，确认**确实没有** → 改回 failed，之后可显式重发。"""

    action: Literal["retry"]
    # 强制显式确认：误触的代价虽然只是"状态改错"，但这个字段让调用方
    # 不可能"顺手"发出这个决定。Literal[True] 意味着必须传 true，不能省略。
    confirm_maybe_duplicate: Literal[True]


# 发布裁决：与 ReviewDecision 同一套判别联合模式。
PublishResolution = Annotated[
    MarkPublishedDecision | RetryPublishDecision, Field(discriminator="action")
]
PublishResolutionAdapter = TypeAdapter(PublishResolution)


# ---------------------------------------------------------------------------
# 运营控制台（Phase 4b）只读响应模型
#
# 这些模型**只描述读取**。控制台不新增任何状态、不改变任何既有语义：所有写操作
# 仍然调用 Phase 3/4a 的 review 与 publish 端点，前端不碰数据库。
#
# 字段命名遵循两条约定：
#   - run_status 一定来自 agent_run.status（运行状态镜像）
#   - publish_status 一定来自 published_post.status（发布状态权威）
# 两者**绝不合并成一个字段**：它们可能合法地互相矛盾（比如发布失败时 Run 仍是
# approved），合并显示会把这个事实藏起来。
# ---------------------------------------------------------------------------


class Page(BaseModel):
    """服务器端分页元信息。前端据此渲染翻页控件，不需要自己数。"""

    page: int
    page_size: int
    total: int
    pages: int


class DecisionTraceRecord(BaseModel):
    """Agent 决策轨迹里的一步（Phase 5A）。

    读的是 `agent_decision` 表，而不是 checkpoint 里的 `decision_trace`。两者内容
    同源（Planner 落库时写的是同一份），但表里这份是**持久的**：checkpoint 被清理
    或那次运行早已结束时，"当时为什么这么做"仍然答得出来。

    `input_summary` / `result_summary` 都是摘要而不是完整 prompt —— 记的是"当时手
    上有什么、允许做什么"和"这一步实际产出了什么"（工作单第十五节）。
    """

    step: int
    action: str
    reason: str
    target: str | None = None
    input_summary: str
    result_summary: str
    created_at: datetime


class ResearchItemRecord(BaseModel):
    """research_item 表里真实落库的一条研究材料。

    `published_at` / `observed_at` / `created_at` 三个时间在这里同时出现，
    刻意**不合并、不互相兜底**（Phase 5B.1 第一节）：

    - `published_at`：原帖在 X 上的发布时间（来源时间）
    - `observed_at`：Agent 从 X API 取回它的时间（观察时间）
    - `created_at`：这行写进本地库的时间（记录时间）

    控制台上三者都要能分别看到。把 `observed_at` 显示成 `created_at` 会让人
    以为"这条是刚发生的"，而它可能是一小时前抓取、三天前发布的。`observed_at`
    对老数据可能为 NULL —— 那一列是 Phase 5B.1 才加的，历史行没有这个信息，
    UI 必须把 None 显示成 `—` 而不是拿 `created_at` 顶上。
    """

    item_id: str
    platform_post_id: str
    author_username: str | None = None
    content: str
    source_url: str | None = None
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    quote_count: int = 0
    published_at: datetime | None = None
    observed_at: datetime | None = None
    created_at: datetime


class DraftRecord(BaseModel):
    draft_id: str
    content: str
    status: str
    version: int
    model_name: str | None = None
    prompt_version: str | None = None
    generated_at: datetime | None = None


class RunSummary(BaseModel):
    """Run 列表行。发布状态可能为 None —— 表示**没有发布记录**。

    刻意不用 None 之外的任何值去"兜底"（比如拿 run_status 顶替）：没有
    published_post 行就是没有，UI 显示 `—`。

    `topic` 同样可能为 None：Phase 5A 的 Run 是给一个**目标**、由 Agent 自己发现
    主题，所以在 Agent 选定之前它确实是空的。`goal` 是另一回事，两者不能互相顶替
    （见 `AdminRunDetail` 的说明）。
    """

    run_id: str
    topic: str | None = None
    goal: str | None = None
    run_status: str
    review_status: str | None = None
    publish_status: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class RunListPage(BaseModel):
    items: list[RunSummary]
    page: Page


class ReviewQueueItem(BaseModel):
    """待人工审核的 Run。

    guardrail_* 来自 LangGraph checkpoint（数据库里没有这一列），因此可能为
    None —— 表示"读不到"，不是"没通过"。UI 必须把 None 显示成 `—`。
    """

    run_id: str
    topic: str
    run_status: str
    created_at: datetime
    draft_id: str | None = None
    draft_preview: str | None = None
    guardrail_passed: bool | None = None
    guardrail_reasons: list[str] = []


class ResolutionQueueItem(BaseModel):
    """需要人工裁决的发布尝试（publishing / publish_unknown）。"""

    run_id: str
    topic: str | None = None
    run_status: str | None = None
    publish: PublishRecord
    detail: str | None = None


class TrendPoint(BaseModel):
    date: str  # ISO 日期（UTC 自然日）
    count: int


class OverviewKpis(BaseModel):
    total_runs: int
    waiting_for_review: int
    # 以下三项一律来自 published_post —— 发布状态的权威。
    published: int
    publish_failed: int
    publish_unknown: int
    # publishing 单列：与 publish_unknown 同为"不可自动重试"，但含义不同
    # （可能仍在飞行中）。藏掉它会让一个卡住的发布在首页完全不可见。
    publishing: int


class OverviewResponse(BaseModel):
    window_days: int
    kpis: OverviewKpis
    runs_trend: list[TrendPoint]
    published_trend: list[TrendPoint]
    review_queue: list[ReviewQueueItem]
    review_queue_total: int
    resolution_queue: list[ResolutionQueueItem]
    resolution_queue_total: int


class AgentQualityMetrics(BaseModel):
    """一次 Agent 运行的质量指标（Phase 5A.1 第五节）。

    全部从已有的 RunState 算出来，不新增数据库字段、不新增基础设施。
    非 Agent 运行（`goal` 为空）没有这些字段，所以外层的 `quality` 是可选的。

    这些数字回答的是"决策循环健不健康"，**不是**"稿子写得好不好"——后者要人读，
    前者用来发现"研究了三轮还是不写稿"这类行为异常。
    """

    total_steps: int = 0
    research_calls: int = 0
    revision_count: int = 0
    candidate_count: int = 0
    critic_accept_count: int = 0
    critic_revise_count: int = 0
    critic_reject_count: int = 0
    # 决策步数是否用满。正常收尾在远未用满时就结束了，所以 True 基本等于
    # "这一轮没能自己收敛"，值得回头看一遍轨迹。
    budget_exhausted: bool = False
    # finish 那一步的理由**原文**。刻意不解析成枚举：理由是给人读的，
    # 转成枚举恰好把"为什么"丢掉。
    finish_reason: str = ""


class RunStyleSummary(BaseModel):
    """这次运行采用的写作结构（Phase 5B.2，工作单第三十九节）。

    数据来自 checkpoint 里的 `style_recipe`。非 Agent 运行（`goal` 为空）
    没有 recipe，所以外层的 `style` 是可选的——与 `quality` 同一种门控。

    ## 为什么取值和中文标签**都**给

    只给取值（`opinion_first`），界面得自己维护一份映射表，于是同一组标签
    在 prompt 与前端各有一份，将来必然漂——而漂了不报错，只是人和模型
    理解的"目标结构"悄悄变成两个东西。只给标签，界面就失去了原始取值，
    没法做筛选、也没法在标签表更新后核对旧运行。所以两个都给，且**同源于**
    `app/style/labels.py`。

    ## 这里没有样本原文

    recipe 本身就不含任何一条真实帖子的句子（工作单第二十二节），所以这个
    块里不可能漏出语料。想加也加不进来——它没有能装正文的字段。
    """

    structure_type: str
    structure_label: str
    hook_type: str
    hook_label: str

    #: 长度有三个数，**它们是三件不同的事**（Phase 5B.2 收口）：
    #:
    #: - `observed_target_length`：语料里典型的一条多长（语料观察）
    #: - `effective_target_length`：这次实际让 Writer 写多长（执行值）
    #: - `hard_max_length`：发布路径的硬上限（平台约束）
    #:
    #: 三者都给，而不是只给 `effective`：只给执行值时，界面上的 140
    #: 看起来像一条关于语料的结论，而事实上语料说的是 180。人被这个数
    #: 误导的方向恰好是最难发现的那种——他会以为"我们学到的写法就是写短句"。
    observed_target_length: int | None = None
    effective_target_length: int | None = None
    hard_max_length: int | None = None
    density: str
    density_label: str
    stance_strength: str
    stance_label: str
    evidence_requirement: str
    evidence_label: str
    tone: str
    tone_label: str
    ending_type: str
    ending_label: str

    #: 选择依据原文（"因为证据包里有 2 条反方说法…"）。折进技术细节。
    reasons: list[str] = []
    #: 这一版 recipe 依据的 Profile，形如 `corpus@2026-09-30T11:00:00+00:00`。
    #: 没有它，一个 recipe 就是一组无法复核的参数。
    profile_ref: str = ""

    #: 交付正文的确定性 AI 味辅助信号。**按需重算、不入库**：它是正文的纯函数，
    #: 重算逐位相同，存一份只会多一个会漂移的来源。
    #: 它在界面上的措辞必须是"这些痕迹出现了几次"，**不是**"AI 概率"。
    ai_signals: AiStyleSignals | None = None


class ViewpointEvidenceItem(BaseModel):
    """一条支持（或反驳）某判断的事实，**已解析回原文**。

    `fact_id` 与 `text` 都给：id 是与 trace / 评估对账用的句柄，原文是人读的。
    只给 id 等于让人去别处查表；只给原文等于失去"这条到底引的是哪一条"的核对能力。
    """

    fact_id: str
    text: str


class RunViewpointSummary(BaseModel):
    """这次运行形成的核心判断（Phase 5B.3，工作单第三十五节）。

    数据来自 checkpoint 里的 `selected_angle`。与 `style` 同一处门控：非 Agent
    运行（`goal` 为空）没有观点，外层的 `viewpoint` 就是 None。

    ## 首屏只给五件事：判断 / 证据 / 反方 / 风险 / 置信度

    工作单第三十五节点名了这五项，并明确要求技术细节**折叠**。所以这里字段
    分两类：上面五个是给人做审核判断用的，`assessment` / `candidates` /
    scope 信号是给复核用的——界面把它们折进 `<details>`，但后端一并给全，
    因为没有这些，"置信度 0.83"就是一个无法追问的数。

    ## 为什么 `evidence` 是原文而不是 id 列表

    人工审核员要回答的是"这个帖子为什么这样说"。一串 `f1、f2` 回答不了这个
    问题，他得再去翻证据包——而那个动作在界面上并不存在。所以在后端解析回
    原文（同一个 state 的 evidence pack 里取），前端只负责显示。

    ## 为什么 stance 同时给取值和标签

    与 `RunStyleSummary` 同一条理由：同源于 `app/agent/labels.py`，
    界面不自己维护第二份映射表。

    ## 三个 stance 必须分清

    `stance` 是**判断**的强度（工作单第十节），不是 `style.stance_strength`
    （表达有多硬），也不是 Critic 的 `stance_strength`（文本有没有明确立场）。
    字段名不同、标签来源不同，界面上要显示成两句话。
    """

    claim: str
    why_it_matters: str = ""
    #: 从事实走到这条判断的那一步（工作单第六节）。空 = 没写出来。
    reasoning: str = ""

    #: 支持这个判断的事实，原文已解析回来。
    evidence: list[ViewpointEvidenceItem] = []
    #: 声明了却引不到真实事实的反方**也在里面**（`bound=False`），
    #: 因为"说过但没证据"这件事本身就该被看见。分开标注，不混进证据里。
    counterpoints: list[str] = []
    counterpoint_evidence: list[ViewpointEvidenceItem] = []

    assumptions: list[str] = []
    implications: list[str] = []
    risk: str = ""

    confidence: float
    #: 0–100，排序用。**与 confidence 是两个问题**：confidence 答"站不站得住"，
    #: 它答"值不值得拿出来说"。界面上不要把它们并排成一个数。
    viewpoint_score: float | None = None

    stance: str = ""
    stance_label: str = ""
    #: 模型**原本申报**的立场。与 `stance` 不同才值得显示（说明被证据压过）。
    stance_declared: str | None = None
    #: 证据推出的上界（工作单第九节）。它单独显示，才能看出"这条判断是模型
    #: 主动保守，还是证据只允许到这里"。
    max_stance: str = ""
    max_stance_label: str = ""

    claim_scope: str | None = None
    claim_scope_label: str = ""
    evidence_scope: str | None = None
    evidence_scope_label: str = ""
    #: 申报范围超出证据范围（或正文出现全称量词而证据是局部的）。
    #: **是参考信号，不是扣分**——界面上要说成"这条判断主张的范围比证据大"，
    #: 而不是一个红色的分数。
    scope_exceeds_evidence: bool | None = None
    #: 模型压根没申报适用范围。与"申报了一个恰好合规的范围"是两件事。
    scope_declared_missing: bool | None = None

    #: 分项与理由（Python 算的，LLM 不参与）。折进技术细节。
    assessment: ViewpointAssessment | None = None
    #: 同一批里落选的候选判断，只给判断本身与分数——审核员偶尔需要知道
    #: "Agent 还考虑过什么"，但那是次要信息，不该占首屏。
    other_candidates: list["ViewpointCandidateBrief"] = []


class ViewpointCandidateBrief(BaseModel):
    """落选的候选判断。**只给判断与它的分**，不给完整评估明细。

    理由：完整明细只对**被选中**的那一条有意义（它决定了正文怎么写）。
    把三条的明细都摊出来，首屏就变成了一个技术面板——而工作单第三十六节
    明确要求不做分析实验室界面。
    """

    claim: str
    viewpoint_score: float | None = None
    stance: str = ""
    stance_label: str = ""


class FeedbackSignalRow(BaseModel):
    """一条反馈信号的对外形状（Phase 5B.4）。

    与 `app/feedback/schema.py::FeedbackSignal` 字段一一对应，但**分开定义**：
    那个是纯核心的内部契约，这个是 API 契约。合成一个的话，改一次内部字段
    就会悄悄改掉一个已经发布的接口，而这种改动不会有任何测试报错。
    """

    kind: str
    statement: str
    metric: str | None = None
    value: float | None = None
    #: 与谁比较。v1 恒为 None——还没有基线，只描述分布。
    comparison_group: str | None = None
    sample_count: int = 0
    #: 经过校准的置信度。v1 恒为 None（编一个数字就是捏造）。
    confidence: float | None = None
    #: 依据的快照 id，非空。
    evidence: list[str] = []
    window: str | None = None


class FeedbackPostSummary(BaseModel):
    """一条已发布帖子的**最新一次观察**。

    计数全部可空，而且**没有默认值 0**：`None` 表示"这次没读到/没采到"，
    `0` 表示"读到的是零"。界面必须把两者显示成不同的样子
    （见 `frontend/src/utils/feedback.ts`）。
    """

    published_post_id: str
    x_post_id: str | None = None
    published_at: datetime | None = None
    observation_count: int = 0
    latest_observed_at: datetime | None = None
    hours_since_publish: float | None = None
    likes: int | None = None
    replies: int | None = None
    reposts: int | None = None
    quotes: int | None = None
    impressions: int | None = None
    bookmarks: int | None = None


class RunFeedbackSummary(BaseModel):
    """**这次运行读到的**反馈（Phase 5B.4，来自 checkpoint 的 `feedback_context`）。

    与 `FeedbackOverview` 是两个不同的东西，刻意分开：

    - `FeedbackOverview` 是"我们这个账号发出去的东西现在怎么样"——一个聚合视图；
    - 本模型是"**这一次运行当时看到了什么**"——一个运行期事实，事后不可重算
      （快照会继续增加，而那次运行看到的是当时的那一批）。

    所以它必须从 checkpoint 读，不能拿现在的快照现算一份最新的塞进去。
    `available=False` 表示这次运行没有读到反馈（Phase 5B.4 之前的运行，
    或者取用失败），**不是**"读到了零条"。
    """

    available: bool
    reason: str | None = None
    sample_count: int = 0
    post_count: int = 0
    snapshot_count: int = 0
    latest_observed_at: datetime | None = None
    signals: list[FeedbackSignalRow] = []
    limitations: list[str] = []
    #: 这次运行的摘要依据了哪些快照行，让 Run Detail 能回到原始观察。
    source_snapshot_ids: list[str] = []


class AdminRunDetail(BaseModel):
    """Run 的完整生命周期视图。

    guardrail_passed / guardrail_reasons / human_action / final_content 来自
    checkpoint，其余来自数据库。两处来源在这里**只读地**汇合，不写回任何一边。

    `topic` 与 `goal` 是**两个不同的东西**，绝不能互相顶替显示：goal 是人给的
    目标（"找一个值得讨论的 AI 话题"），topic 是 Agent 从中选定的结果（"某模型
    开源"）。goal 可以永远不为空，topic 在 Agent 选定之前就是空的。

    `decision_trace` 来自 `agent_decision` 表（不是 checkpoint）：即使这个 Run 的
    checkpoint 已经读不到，它每一步为什么这么做也仍然查得到。
    """

    run_id: str
    topic: str | None = None
    goal: str | None = None
    run_status: str
    review_status: str | None = None
    publish_status: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None

    guardrail_passed: bool | None = None
    guardrail_reasons: list[str] = []
    human_action: str | None = None
    final_content: str | None = None

    research_items: list[ResearchItemRecord] = []
    decision_trace: list[DecisionTraceRecord] = []
    # 只有 Agent 运行（goal 非空）才有；topic 链路的 Run 这一项是 None。
    quality: AgentQualityMetrics | None = None
    # 同上，只有 Agent 运行才有 recipe 可展示。**必须与 quality 同一处门控**，
    # 否则 V1 topic 链的 Run 会冒出一个空白的"写作结构"块。
    style: RunStyleSummary | None = None
    # 同上：只有 Agent 运行才形成观点。**必须与 quality / style 同一处门控**，
    # 否则 V1 topic 链的 Run 会冒出一个空白的"核心判断"块。两侧都要加——
    # 它是 response_model，schema 里没有的字段会被静默丢弃。
    viewpoint: RunViewpointSummary | None = None
    # Phase 5B.4：这次运行**读到**的反馈（checkpoint），与它发布出去的帖子
    # **后来被观察到的**结果（数据库快照）。两者来源不同、时间语义不同，
    # 所以是两个字段而不是一个：前者是运行期决策，后者是外部世界的回音。
    # 与 quality / style / viewpoint 同一处门控（只有 Agent 运行才有）。
    feedback: RunFeedbackSummary | None = None
    #: 这个 Run 的已发布帖子的**最新一次观察**。没有快照（还没采过、
    #: 或者这条帖子根本没有 x_post_id）时为 `None`，**不是一行 0**。
    #: 它不门控于 goal：发布链路是 V1 就有的，topic 链路的 Run 也会发帖。
    performance: FeedbackPostSummary | None = None
    draft: DraftRecord | None = None
    review: ReviewRecord | None = None
    publish: PublishRecord | None = None


class PublishedRow(BaseModel):
    """published_post 行 + 它所属 Run 的少量展示字段。

    `body` 是**实际发布出去的正文**：edit 路径下它是人工改写稿，与原
    draft.content 不同。所以这里同时给出 draft_id，让这一行能够自证来源。
    """

    publish_id: str
    run_id: str
    draft_id: str
    topic: str | None = None
    body: str
    status: str
    x_post_id: str | None = None
    published_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None
    #: 这一行**后来被观察到的**最新一次结果（Phase 5B.4）。
    #:
    #: 与 Run Detail 里的 `performance` 是同一个东西、同一个来源（快照表）。
    #: 没采过的帖子这里是 `None`，**不是**一行 0——"没采到"与"采到了零"
    #: 在界面上必须长得不一样（见 `frontend/src/utils/feedback.ts`）。
    latest_metrics: FeedbackPostSummary | None = None


class PublishedListPage(BaseModel):
    items: list[PublishedRow]
    page: Page


class ReviewQueuePage(BaseModel):
    items: list[ReviewQueueItem]
    page: Page
    # 编辑稿的字数上限，取自已加载的 persona 配置。界面用它渲染 "N / 140"。
    #
    # 必须由后端下发而不是前端写死：这个值同时是发布前的硬校验
    # （`app/services/publish.py` 里 `len(content) > persona.max_length` → invalid），
    # 界面抄一份就会在配置改动后开始说谎，而它存在的意义恰恰是别让人写出会被打回的稿子。
    #
    # 口径：这是 **Python `len()`**，字符数，不是 X 的加权字符数计算。中文一个字算 1。
    content_max_length: int


class MetricAvailability(BaseModel):
    """指标是否可计算。不可计算时必须给出原因，**不得**用 0 冒充。"""

    available: bool
    reason: str | None = None


class FeedbackOverview(BaseModel):
    """反馈的一览（`GET /api/admin/feedback`）。

    ## 没有快照时返回 `available=False` + 原因，不返回一串 0

    同 `MetricAvailability` 的口径：0 会被读成"采到了，结果是零"，
    而真实情况是"从来没采过"。这两件事在运维上的处置完全不同。

    ## 这个接口**不读 checkpoint**

    `app/services/admin.py` 的整体边界就是"聚合接口不读 checkpoint"（见该模块
    说明）。每条帖子的**归因**（当时选了什么观点、什么结构）在 Run Detail 里，
    那里本来就要读那一次运行的 checkpoint。在这里读 N 条帖子的 checkpoint，
    会把一个一览接口变成一个慢查询，而且失败模式会从"少一块"变成"整页报错"。
    """

    available: bool
    reason: str | None = None
    sample_count: int = 0
    snapshot_count: int = 0
    post_count: int = 0
    latest_observed_at: datetime | None = None
    signals: list[FeedbackSignalRow] = []
    limitations: list[str] = []
    posts: list[FeedbackPostSummary] = []


class ReviewDecisionDistribution(BaseModel):
    approve: int = 0
    edit: int = 0
    reject: int = 0
    other: int = 0


class PublishOutcomeDistribution(BaseModel):
    published: int = 0
    failed: int = 0
    publish_unknown: int = 0
    publishing: int = 0
    # 口径说明随数据一起下发：published_post 每个 Run 最多一行、失败重试复用
    # 同一行，所以这里是"每个 Run 的当前结局"，不是"尝试次数"。
    note: str


class AverageRunDuration(BaseModel):
    available: bool
    average_seconds: float | None = None
    sampled_runs: int = 0
    total_runs: int = 0
    reason: str | None = None


class TopicCount(BaseModel):
    topic: str
    count: int


class AnalyticsResponse(BaseModel):
    window_days: int
    # 窗口内的 Run 总数。日粒度序列在 daily_runs —— 两者刻意不重复下发同一份数据。
    run_volume: int
    review_decisions: ReviewDecisionDistribution
    publish_outcomes: PublishOutcomeDistribution
    daily_runs: list[TrendPoint]
    daily_published: list[TrendPoint]
    average_run_duration: AverageRunDuration
    topic_distribution: list[TopicCount]
    x_content_performance: MetricAvailability


class AdminErrorResponse(BaseModel):
    """控制台错误信息的对外形状。只带安全文案，绝不带 traceback。"""

    detail: str
