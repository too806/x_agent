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
