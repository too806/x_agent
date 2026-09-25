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
  published_at: string | null
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
  topic: string
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

export interface AdminRunDetail {
  run_id: string
  topic: string
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
}

export interface PublishedListPage {
  items: PublishedRow[]
  page: Page
}

export interface ReviewQueuePage {
  items: ReviewQueueItem[]
  page: Page
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
