/**
 * 后端 API 的响应类型。
 *
 * 这些类型**逐字对应** `app/schemas.py` 里的 pydantic 模型，不是重新设计的。
 * 刻意不用代码生成：模型数量有限，而人工对照能让人在字段变化时被迫回来看一眼
 * 后端的注释——那些注释记录了每个字段为什么长这样（比如为什么 publish_status
 * 会是 null、为什么发布状态不能从 Run 状态反推）。
 */

// ---------------------------------------------------------------------------
// 状态
// ---------------------------------------------------------------------------

/**
 * agent_run.status —— **运行状态**，给运维查列表用的镜像。
 *
 * 注意它包含了 `publishing` / `publish_unknown` / `published` 三个与发布同名的值：
 * Run 在发布期间会被推进到这些状态。但它**不是**发布的权威，权威在
 * `published_post.status`。UI 上两者永远分列显示。
 */
export type RunStatus =
  | 'running'
  | 'waiting_for_review'
  | 'approved'
  | 'edited'
  | 'rejected'
  | 'failed'
  | 'completed'
  | 'publishing'
  | 'publish_unknown'
  | 'published'

/** 按 Run 生命周期顺序排列，供筛选下拉使用。 */
export const RUN_STATUSES: readonly RunStatus[] = [
  'running',
  'waiting_for_review',
  'approved',
  'edited',
  'publishing',
  'publish_unknown',
  'published',
  'rejected',
  'failed',
  'completed',
]

/** review.status —— 人工审核决定。 */
export type ReviewStatus = 'approve' | 'edit' | 'reject' | 'pending'

/**
 * published_post.status —— **发布状态的唯一权威**。
 *
 * - `publishing`      尝试已提交，本地对结果一无所知（可能还在飞，也可能进程崩了）
 * - `publish_unknown` 尝试已结束，拿到了信息却判不出结果（5xx / 超时 / 2xx 无 id）
 * - `published`       确认成功，x_post_id 必非空
 * - `failed`          确定没发出去，可以重试
 *
 * 前两者合称 unresolved：**任何自动路径都不得再调 X**，只能人工裁决。
 */
export type PublishStatus = 'publishing' | 'publish_unknown' | 'published' | 'failed'

export const PUBLISH_STATUSES: readonly PublishStatus[] = [
  'published',
  'failed',
  'publish_unknown',
  'publishing',
]

/** 需要人工裁决的发布状态。前端用它来决定"显示 Retry 还是显示 Resolve"。 */
export const UNRESOLVED_PUBLISH_STATUSES: readonly PublishStatus[] = [
  'publishing',
  'publish_unknown',
]

export function isUnresolved(status: string | null | undefined): boolean {
  return status === 'publishing' || status === 'publish_unknown'
}

// ---------------------------------------------------------------------------
// 通用
// ---------------------------------------------------------------------------

export interface Page {
  page: number
  page_size: number
  total: number
  pages: number
}

export interface MetricAvailability {
  available: boolean
  reason: string | null
}

/** 发布记录。四个状态都可能出现；`body` 是**实际发出去的正文**。 */
export interface PublishRecord {
  status: string
  draft_id: string | null
  body: string | null
  x_post_id: string | null
  published_at: string | null
  last_error: string | null
  updated_at: string | null
}

/** 已保存的审核记录。**只有人工决定之后才存在**。 */
export interface ReviewRecord {
  review_id: string
  action: string
  original_content: string
  edited_content: string | null
  reject_reason: string | null
  reviewed_at: string | null
}

export interface ResearchItemRecord {
  item_id: string
  platform_post_id: string
  author_username: string | null
  content: string
  source_url: string | null
  likes: number
  reposts: number
  replies: number
  /**
   * 引用数（Phase 5B.1 新增）。与另外三项一样是**观测到的**互动量，
   * 不是 X 的官方热度值——trend_score 由后端实算，这一列只用于展示。
   */
  quote_count: number
  /** 原帖在 X 上的发布时间。不是被我们抓回来的时间。 */
  published_at: string | null
  /**
   * Agent 从 X API 取回它的时间（Phase 5B.1 新增）。
   *
   * **对 Phase 5B.1 之前的行是 null** —— 那一列是后加的，历史行没有这个信息。
   * 界面上必须把 null 显示成 `—`，不能拿 `created_at` 顶上：两者在正常路径上
   * 几乎同时，但它们是两件事（见 app/schemas.py 的说明）。
   */
  observed_at: string | null
  /** 这行写进本地库的时间。与上面两个都不同。 */
  created_at: string
}

export interface DraftRecord {
  draft_id: string
  content: string
  status: string
  version: number
  model_name: string | null
  prompt_version: string | null
  generated_at: string | null
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------

export interface RunSummary {
  run_id: string
  /** null = Agent 模式（Phase 5A）的 Run 还没选出主题。不要拿 goal 顶替它。 */
  topic: string | null
  /** 人给 Agent 的目标。null = 这条 Run 走的是既有 topic 链路。 */
  goal: string | null
  run_status: RunStatus
  /** null = 还没有人工决定。 */
  review_status: ReviewStatus | null
  /** null = **没有发布记录**。不要用 run_status 去顶替它。 */
  publish_status: PublishStatus | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  error: string | null
}

export interface RunListPage {
  items: RunSummary[]
  page: Page
}

export interface ReviewQueueItem {
  run_id: string
  topic: string
  run_status: RunStatus
  created_at: string
  draft_id: string | null
  draft_preview: string | null
  /** 来自 LangGraph checkpoint，数据库里没有这一列。null = **读不到**，不是"没通过"。 */
  guardrail_passed: boolean | null
  guardrail_reasons: string[]
}

export interface ResolutionQueueItem {
  run_id: string
  topic: string | null
  run_status: string | null
  publish: PublishRecord
  detail: string | null
}

export interface TrendPoint {
  date: string
  count: number
}

export interface OverviewKpis {
  total_runs: number
  waiting_for_review: number
  published: number
  publish_failed: number
  publish_unknown: number
  publishing: number
}

export interface OverviewResponse {
  window_days: number
  kpis: OverviewKpis
  runs_trend: TrendPoint[]
  published_trend: TrendPoint[]
  review_queue: ReviewQueueItem[]
  review_queue_total: number
  resolution_queue: ResolutionQueueItem[]
  resolution_queue_total: number
}

// ---------------------------------------------------------------------------
// Run 详情
// ---------------------------------------------------------------------------

/** Agent 决策轨迹里的一步（`agent_decision` 表的一行）。字段全是摘要，不是完整 prompt。 */
export interface DecisionTraceEntry {
  step: number
  action: string
  reason: string
  target: string | null
  input_summary: string
  result_summary: string
  created_at: string
}

/**
 * 一次 Agent 运行的质量指标。
 *
 * 回答的是"决策循环本身健不健康"，不是"稿子写得好不好"——后者要人读。
 * 只对 Agent 模式的 Run 存在：直接给主题的 Run 没有 planner 循环，
 * 那几个计数恒为 0，报出来只会让人以为"这一轮什么都没做"。
 */
export interface AgentQualityMetrics {
  total_steps: number
  research_calls: number
  revision_count: number
  candidate_count: number
  critic_accept_count: number
  critic_revise_count: number
  critic_reject_count: number
  /** 步数是否撞到了上限。注意：这是拿**当前配置**的上限比的。 */
  budget_exhausted: boolean
  /** finish 那一步的理由原文；没有则空串。 */
  finish_reason: string
}

/**
 * 交付正文的确定性 AI 味信号（Phase 5B.2）。
 *
 * 它是**几个能被复算的计数**，不是"AI 概率"。界面上的措辞必须是
 * "这类痕迹出现了几次"，绝不能渲染成分数或判定。
 */
export interface AiStyleSignals {
  char_count: number
  template_opener_count: number
  empty_connective_count: number
  false_authority_count: number
  generic_phrase_count: number
  hedge_count: number
  repeated_opener_count: number
  /** 命中的套话原文，供人核对"这个也算套话？" */
  generic_phrases: string[]
}

/**
 * 这次运行采用的写作结构（Phase 5B.2）。
 *
 * **取值和中文标签都带着**：只给标签，界面没法核对原始取值；只给取值，
 * 前端得自己维护一份标签表，与后端 prompt 用的那份迟早会漂。
 * 标签来自后端 `app/style/labels.py`，与 Writer / Critic 看到的是同一份。
 *
 * 这里**没有样本原文**——recipe 是生成约束，从来没有正文。
 */
export interface RunStyleSummary {
  structure_type: string
  structure_label: string
  hook_type: string
  hook_label: string
  /**
   * 长度是三个数，对应三件不同的事，**不要把它们混起来读**：
   *
   * - `observed_target_length` —— 语料里典型的一条多长（语料观察）
   * - `effective_target_length` —— 这次实际让 Writer 写多长（执行值）
   * - `hard_max_length` —— 发布路径的硬上限（平台约束）
   *
   * 三者都为 null 表示这一版 recipe 没定长度。只显示执行值时，界面上的
   * 一个 140 会被读成"学到的写法就是写短句"，而语料说的其实是 180。
   */
  observed_target_length: number | null
  effective_target_length: number | null
  hard_max_length: number | null
  density: string
  density_label: string
  stance_strength: string
  stance_label: string
  evidence_requirement: string
  evidence_label: string
  tone: string
  tone_label: string
  ending_type: string
  ending_label: string
  /** 每个选择的可核对依据。 */
  reasons: string[]
  /** 这一版 recipe 依据的 Profile，形如 `corpus@2026-09-30T11:00:00+00:00`。 */
  profile_ref: string
  /** 交付正文的信号。没有交付正文时为 null（不是"全是 0"）。 */
  ai_signals: AiStyleSignals | null
}

/** 一条支持（或反驳）某判断的事实，**已经解析回原文**。
 *
 * 后端一并给 `fact_id` 与原文，而不是只给 id 列表：审核员要回答的是
 * "这个帖子为什么这样说"，而一串 `f1、f2` 回答不了，他得再去翻证据包——
 * 那个动作在界面上并不存在。
 */
export interface ViewpointEvidenceItem {
  fact_id: string
  text: string
}

/** 落选的候选判断。**只给判断与它的分**，不给完整评估明细。 */
export interface ViewpointCandidateBrief {
  claim: string
  viewpoint_score: number | null
  stance: string
  stance_label: string
}

/** 这次运行形成的核心判断（Phase 5B.3，工作单第三十五节）。
 *
 * 首屏只显示五件事：**核心判断 / 证据 / 反方 / 风险 / 置信度**。
 * `assessment` / `other_candidates` / scope 那几项折进技术细节——它们是给人
 * 复核用的，没有它们"置信度 0.83"就是一个无法追问的数。
 *
 * 三个 stance 不要混：`stance` 是**判断**有多笃定（这里），
 * `style.stance_strength` 是**表达**有多硬，Critic 的那个是**文本**里有没有
 * 明确立场。字段名不同、标签来源不同，界面上是两句话。
 */
export interface RunViewpointSummary {
  claim: string
  why_it_matters: string
  /** 从事实走到判断的那一步。空 = 没写出来（会被 RULE-020 拦下）。 */
  reasoning: string

  evidence: ViewpointEvidenceItem[]
  /** 声明了却引不到真实事实的反方**也在里面**——"说过但没证据"本身要被看见。 */
  counterpoints: string[]
  counterpoint_evidence: ViewpointEvidenceItem[]

  assumptions: string[]
  implications: string[]
  risk: string

  confidence: number
  /** 0–100，排序用。**与 confidence 回答的不是同一个问题**：
   * confidence 答"站不站得住"，它答"值不值得拿出来说"。不要并排成一个数。 */
  viewpoint_score: number | null

  stance: string
  stance_label: string
  /** 模型原本申报的立场。与 stance 不同才说明被证据压过。 */
  stance_declared: string | null
  /** 证据推出的上界。它单独显示，才能看出"主动保守"还是"证据只允许到这里"。 */
  max_stance: string
  max_stance_label: string

  claim_scope: string | null
  claim_scope_label: string
  evidence_scope: string | null
  evidence_scope_label: string
  /** 申报范围超出证据范围。**是参考信号，不是扣分**——要写成一句话。 */
  scope_exceeds_evidence: boolean | null
  /** 模型压根没申报适用范围（与"申报了一个恰好合规的范围"是两件事）。 */
  scope_declared_missing: boolean | null

  assessment: ViewpointAssessment | null
  other_candidates: ViewpointCandidateBrief[]
}

/** 确定性评估明细。Phase 5B.3 之前的运行是 null（那时的置信度是模型给的）。 */
export interface ViewpointAssessment {
  evidence_support: number | null
  evidence_support_raw: number | null
  source_diversity: number | null
  counterpoint_awareness: number | null
  unknown_pressure: number | null
  specificity: number | null
  novelty: number | null
  newness_contribution: number | null
  confidence_raw: number | null
  viewpoint_score_raw: number | null
  bound_facts: number
  total_facts: number
  bound_sources: number
  bound_authors: number
  missing_author_sources: number
  declared_counterpoints: number
  bound_counterpoints: number
  total_materials: number
  unreferenced_materials: number
  breadth_markers: string[]
  originality_claim: boolean | null
  restates_fact: boolean | null
  duplicate_group: string
  reasons: string[]
}

// ---------------------------------------------------------------------------
// 反馈（Phase 5B.4）
//
// 三个类型，回答三个**不同**的问题，不要合并成一个：
//
//   RunFeedbackSummary  —— 这次运行**当时读到了什么**（checkpoint，事后不可重算）
//   FeedbackPostSummary —— 一条帖子**后来被观察到的**最新结果（快照表）
//   FeedbackOverview    —— 我们这个账号发出去的东西**现在**怎么样（聚合）
//
// 前两个在 Run Detail 里并排出现，且**必须并排出现**：快照会继续增加，
// 而那次运行看到的是当时那一批。合成一个字段，界面就再也分不出
// "它当时知道什么"与"现在是什么"。
// ---------------------------------------------------------------------------

/** 一条反馈信号。
 *
 * `comparison_group` 与 `confidence` 在 v1 **恒为 null**——还没有基线，
 * 也没有经过校准的置信度。界面上不要给它们找占位文案：一个编出来的
 * "置信度 80%" 与一个真的算出来的，读的人是分不出来的。
 */
export interface FeedbackSignalRow {
  kind: string
  statement: string
  metric: string | null
  value: number | null
  comparison_group: string | null
  sample_count: number
  confidence: number | null
  /** 依据的快照 id，非空。没有证据就没有信号。 */
  evidence: string[]
  window: string | null
}

/** 一条已发布帖子的**最新一次观察**。
 *
 * 计数全部可空。`null` = 没读到/没采到，`0` = 读到的是零。渲染必须走
 * `utils/feedback.ts::metricLabel`，**不要**写 `value ?? 0`。
 */
export interface FeedbackPostSummary {
  published_post_id: string
  x_post_id: string | null
  published_at: string | null
  observation_count: number
  latest_observed_at: string | null
  hours_since_publish: number | null
  likes: number | null
  replies: number | null
  reposts: number | null
  quotes: number | null
  /** v1 从不写入，恒为 null。 */
  impressions: number | null
  /** 同上。 */
  bookmarks: number | null
}

/** **这次运行读到的**反馈（来自 checkpoint 的 `feedback_context`）。
 *
 * `available === false` 表示这次运行没有读到反馈（Phase 5B.4 之前的运行，
 * 或者取用失败），**不是**"读到了零条"。两种情况的后端原因不同，原样显示。
 */
export interface RunFeedbackSummary {
  available: boolean
  reason: string | null
  sample_count: number
  post_count: number
  snapshot_count: number
  latest_observed_at: string | null
  signals: FeedbackSignalRow[]
  limitations: string[]
  /** 这次运行的摘要依据了哪些快照行，让 Run Detail 能回到原始观察。 */
  source_snapshot_ids: string[]
}

/** 反馈一览（`GET /api/admin/feedback`）。一条快照都没有时返回
 * `available=false` + 原因，**不返回一串 0**。 */
export interface FeedbackOverview {
  available: boolean
  reason: string | null
  sample_count: number
  snapshot_count: number
  post_count: number
  latest_observed_at: string | null
  signals: FeedbackSignalRow[]
  limitations: string[]
  posts: FeedbackPostSummary[]
}

export interface AdminRunDetail {
  run_id: string
  /** Agent 选定的主题。null = 还没选出来（不是"没有"这个字段的意思）。 */
  topic: string | null
  /** 人给 Agent 的目标。null = 不是 Agent 模式的 Run。两者不可互相顶替。 */
  goal: string | null
  run_status: RunStatus
  review_status: ReviewStatus | null
  publish_status: PublishStatus | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  error: string | null

  /** 以下四项来自 checkpoint，可能整体为 null（表示读不到）。 */
  guardrail_passed: boolean | null
  guardrail_reasons: string[]
  human_action: string | null
  final_content: string | null

  research_items: ResearchItemRecord[]
  /** 来自 agent_decision 表（不是 checkpoint），按 step 升序。空数组 = 没有决策记录。 */
  decision_trace: DecisionTraceEntry[]
  /** 只有 Agent 模式的 Run 才有；其余为 null。 */
  quality: AgentQualityMetrics | null
  /** 同上，只有 Agent 模式的 Run 才有 recipe 可展示；其余为 null。 */
  style: RunStyleSummary | null
  /** 同上，只有 Agent 模式的 Run 才会形成判断；其余为 null。
   * 三处与 `quality` 同一门控——非 Agent 运行不该冒出空白的"核心判断"块。 */
  viewpoint: RunViewpointSummary | null
  /** 这次运行**读到的**反馈摘要（Phase 5B.4，来自 checkpoint）。
   * 与 `quality` 同一门控：非 Agent 运行没有它。`available=false` 时
   * 里面说的是"这次运行没读到反馈"，不是"读到了零条"。 */
  feedback: RunFeedbackSummary | null
  /** 这条帖子**后来被观察到的**最新一次结果（快照表）。
   * 发过帖但一次都没采过时是 null——不是一行 0。与 `feedback` 是两件事。 */
  performance: FeedbackPostSummary | null
  draft: DraftRecord | null
  review: ReviewRecord | null
  publish: PublishRecord | null
}

// ---------------------------------------------------------------------------
// Published
// ---------------------------------------------------------------------------

export interface PublishedRow {
  publish_id: string
  run_id: string
  draft_id: string
  topic: string | null
  body: string
  status: PublishStatus
  x_post_id: string | null
  published_at: string | null
  created_at: string
  updated_at: string
  last_error: string | null
  /** 这一行**后来被观察到的**最新一次结果（Phase 5B.4）。
   * 没采过的帖子为 null，渲染成"暂无数据"，不是 0。 */
  latest_metrics: FeedbackPostSummary | null
}

export interface PublishedListPage {
  items: PublishedRow[]
  page: Page
}

export interface ReviewQueuePage {
  items: ReviewQueueItem[]
  page: Page
  /**
   * 编辑稿字数上限，取自后端已加载的 persona 配置。
   *
   * **不要在前端写死 140。** 这个数字同时是发布前的硬校验
   * （`len(content) > persona.max_length` → invalid），抄一份到界面就会在配置改动后
   * 开始骗人，而它存在的意义恰恰是别让人写出会被打回的稿子。
   *
   * 口径是 Python 的 `len()`（码点数），不是 X 的加权字符数。
   */
  content_max_length: number
}

// ---------------------------------------------------------------------------
// Analytics
// ---------------------------------------------------------------------------

export interface ReviewDecisionDistribution {
  approve: number
  edit: number
  reject: number
  other: number
}

export interface PublishOutcomeDistribution {
  published: number
  failed: number
  publish_unknown: number
  publishing: number
  /** 口径说明由后端下发：这里是"每个 Run 的当前结局"，不是"尝试次数"。 */
  note: string
}

export interface AverageRunDuration {
  available: boolean
  average_seconds: number | null
  /** 参与计算的 Run 数（只有终态 Run 才有 finished_at）。 */
  sampled_runs: number
  total_runs: number
  reason: string | null
}

export interface TopicCount {
  topic: string
  count: number
}

export interface AnalyticsResponse {
  window_days: number
  run_volume: number
  review_decisions: ReviewDecisionDistribution
  publish_outcomes: PublishOutcomeDistribution
  daily_runs: TrendPoint[]
  daily_published: TrendPoint[]
  average_run_duration: AverageRunDuration
  topic_distribution: TopicCount[]
  x_content_performance: MetricAvailability
}

// ---------------------------------------------------------------------------
// 写操作（一律复用既有端点，控制台不新增写接口）
// ---------------------------------------------------------------------------

export type ReviewDecision =
  | { action: 'approve' }
  | { action: 'edit'; content: string }
  | { action: 'reject'; reason: string }

export type PublishResolution =
  | { action: 'mark_published'; x_post_id: string }
  | { action: 'retry'; confirm_maybe_duplicate: true }

export interface PublishResponse {
  run_id: string
  outcome: string
  publish: PublishRecord | null
  error: string | null
}

/** 发布类接口的返回：HTTP 状态码本身是结果的一部分，不能只看 body。 */
export interface PublishCall {
  httpStatus: number
  body: PublishResponse
}

/** 审核接口返回的 Run 摘要（对应后端的 RunResponse）。 */
export interface ReviewCallResult {
  run_id: string
  status: string
  human_action: string | null
  final_content: string | null
  draft_content: string | null
  guardrail_passed: boolean | null
  guardrail_reasons: string[]
  error: string | null
}

// ---------------------------------------------------------------------------
// 学习准备度（Phase 5C）
//
// 这一层回答的**只有一句**："我们过去的反馈，够不够可靠，可以作为未来的
// 学习材料？" 它不回答"以后该怎么写"，所以这里没有任何建议、分数或排名字段——
// 而且不该有。界面上因此也不给这些数字配解释性文案（"说明这么写更有效"之类）：
// 一句不存在的数据配上解释，比没有这一块更糟。
//
// 三个类型对应三个不同的对象，不要合并：
//
//   LearningCohortRow     —— 一个比较组（同维度、同取值、同窗口的一批帖子）
//   LearningComparisonRow —— 两个组在**一个指标**上的观察性比较
//   LearningOverview      —— 上面两者的容器 + 确定性闸门的结果
// ---------------------------------------------------------------------------

/** 一个比较组。`selection_rule` 必须一起显示：没有它，`n=5` 无法被追问。 */
export interface LearningCohortRow {
  dimension: string
  value: string
  observation_window: string
  selection_rule: string
  sample_count: number
  topics: string[]
  limitations: string[]
}

/** 一条观察性比较。
 *
 * `valid === false` 时**没有任何结论**，只有 `invalid_reason`——界面必须照这个
 * 分支显示，不能把两个中位数并排摆出来让人自己比：那正是这一层要防的事。
 * `median_a` / `median_b` 可用 `null` 表示"这一组在该指标上没有读数"，
 * 渲染走 `utils/learning.ts::medianLabel`，**不要**写 `?? 0`。
 */
export interface LearningComparisonRow {
  dimension: string
  metric: string
  observation_window: string
  group_a: string
  group_b: string
  sample_count_a: number
  sample_count_b: number
  median_a: number | null
  median_b: number | null
  observed_difference: number | null
  valid: boolean
  invalid_reason: string | null
  statement: string
  limitations: string[]
}

/** 一条学习证据。`confidence` 恒为 `null`——没有校准过的概率模型。 */
export interface LearningEvidenceRow {
  id: string
  type: string
  statement: string
  sample_count: number
  comparison_group: string | null
  observation_window: string | null
  metric: string | null
  dimension: string | null
  source_snapshot_ids: string[]
  source_post_ids: string[]
  source_x_post_ids: string[]
  confidence: number | null
  limitations: string[]
}

/** 确定性闸门的结果。
 *
 * `automatic_modification` **恒为 false**，而且它是后端下发的一个字段而不是
 * 前端的约定：约定不会让测试红，字段会。界面照实显示它。
 */
export interface LearningReadinessRow {
  learning_ready: boolean
  reason: string
  observation_window: string
  sample_count: number
  post_count: number
  checks: Array<{ name: string; passed: boolean; detail: string }>
  automatic_modification: boolean
  limitations: string[]
}

/** 学习准备度一览（`GET /api/admin/learning`）。 */
export interface LearningOverview {
  available: boolean
  reason: string
  observation_window: string
  readiness: LearningReadinessRow
  sample_count: number
  post_count: number
  cohort_count: number
  valid_comparison_count: number
  /**
   * 提案条数（Phase 5D）。**恒为现算**，不是从库里查出来的计数。
   *
   * 闸门关着时它**必然是 0**——`learning_ready=false` ⇒ 零条提案。
   * 不要把它渲染成"发现了 0 个问题"：它说的是"还不足以提出建议"。
   */
  proposal_count: number
  cohorts: LearningCohortRow[]
  comparisons: LearningComparisonRow[]
  evidence: LearningEvidenceRow[]
  limitations: string[]
}

// ---------------------------------------------------------------------------
// Phase 5D：学习提案
// ---------------------------------------------------------------------------
//
// 三个类型对应三个不同的对象，与前一组一样**不要合并**：
//
//   LearningProposalRow        —— 一条"可以尝试什么"的建议（不是"应该做什么"）
//   LearningProposalValidation —— 这条建议自己过不过判据（后端算的，不是前端猜的）
//   ProposalsOverview          —— 上面两者的容器 + 闸门与裁决计数
//
// 这一组有几个字段是**刻意缺席**的，补上任何一个都是在加一个本层没有的东西：
// 没有 `score`、没有 `priority`、没有 `rank`、没有 `applied_at`。工作单第十七节
// 明确不要 proposal_score；第三十五节明确本阶段不实现应用。
// ---------------------------------------------------------------------------

/** 一条提案的判据结果。`passed_checks` / `reasons` 都由后端判定，前端只显示。 */
export interface LearningProposalValidationRow {
  valid: boolean
  reasons: string[]
  passed_checks: string[]
}

/**
 * 一条学习提案。`status` 的四个取值含义不同，不要合并显示：
 *
 * - `proposed` —— 还没人裁决过（**不在库里**，每次现算出来的）；
 * - `approved` / `rejected` —— 人做过的决定（库里存着，不可改写）；
 * - `expired` —— 曾经批准过，但配方版本已经往前走过了，这条建议不再对应当前配置。
 *
 * `automatic_application` **恒为 false**，且是后端下发的字段而不是前端的约定。
 * `confidence` 恒为 `null`——没有校准过的概率模型。渲染走
 * `utils/proposals.ts`，**不要**写 `?? 0`。
 */
export interface LearningProposalRow {
  id: string
  target: string
  target_label: string
  current_value: string
  proposed_value: string
  rationale: string
  evidence_ids: string[]
  sample_count: number
  observation_window: string
  comparison_group: string
  confidence: number | null
  limitations: string[]
  status: string
  current_recipe_version: string | null
  proposed_recipe_version: string | null
  outlier_count: number
  automatic_application: boolean
  validation: LearningProposalValidationRow
  reviewable: boolean
  reviewed_at: string | null
  review_note: string | null
  /** 只在"裁决过、但这次算不出来"时非空。见 `utils/proposals.ts::isStale`。 */
  stale_reason: string | null
}

/** 提案一览（`GET /api/admin/learning/proposals`）。 */
export interface ProposalsOverview {
  available: boolean
  reason: string
  observation_window: string
  learning_ready: boolean
  /** 现算出来的提案条数。闸门关着时恒为 0。 */
  proposal_count: number
  /** 其中还没被裁决过的。 */
  reviewable_count: number
  /** 库里已存的裁决条数。可能大于 `proposal_count`（含已过期的历史裁决）。 */
  decided_count: number
  proposals: LearningProposalRow[]
  limitations: string[]
}

/**
 * 裁决一条提案的请求体。
 *
 * 写成**判别联合**而不是 `{ action: 'approve' | 'reject'; note?: string }`：
 * 后端对两者要求不同（批准可以不写理由，否决必须写），而"否决没写理由"是
 * 一个后端会 422 拦下的错误。用可选字段描述它，等于把这个错误从编译期
 * 推到运行期——界面上就是一次"点了按钮才报错"。
 */
export type ProposalReviewDecision =
  | { action: 'approve'; note?: string }
  | { action: 'reject'; note: string }
