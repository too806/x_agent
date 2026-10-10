/**
 * API 客户端。
 *
 * 只用原生 `fetch`——这个项目不需要 axios 的那一层。
 *
 * 两条刻意的设计：
 *
 * 1. **请求路径全部是同源相对路径**（`/api/...`）。开发时由 Vite 转发到 8000，
 *    生产时反代接管。前端因此完全不知道后端地址，也不存在把内网地址硬编码进
 *    构建产物的可能。
 * 2. **不把后端的原始错误体直接抛给界面**。`ApiError.message` 是经过挑选的、
 *    可直接展示给使用者的一句话；原始响应体（可能很长、可能带内部路径）只保留
 *    在 `ApiError.raw` 里供排查。
 */

import type {
  AdminRunDetail,
  AnalyticsResponse,
  AutoPublishStatus,
  CreateRunPayload,
  CreatedRunResponse,
  FeedbackOverview,
  LearningOverview,
  LearningProposalRow,
  OverviewResponse,
  ProposalReviewDecision,
  ProposalsOverview,
  PublishCall,
  PublishResolution,
  PublishResponse,
  PublishedListPage,
  ReviewCallResult,
  ReviewDecision,
  ReviewQueuePage,
  RunListPage,
} from '../types/api'
import type { QueueOrder } from '../utils/queue'

/** 网络层或 HTTP 层失败。`status === 0` 表示请求根本没发出去（服务没起 / 断网）。 */
export class ApiError extends Error {
  readonly status: number
  readonly raw: unknown

  constructor(status: number, message: string, raw: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.raw = raw
  }
}

/**
 * 把后端的错误响应压成一句能直接显示的话。
 *
 * FastAPI 的错误体有两种形状：业务错误是 `{"detail": "..."}`，而请求校验失败是
 * `{"detail": [{...}, ...]}`（一个字段错误数组）。后者如果直接丢进界面会变成
 * 一串 `[object Object]`，所以这里把它拼成 `loc: msg` 的形式。
 *
 * 拿不到可用信息时退化成状态码文案——**绝不回显整个响应体**：那既可能很长，
 * 也可能把后端的内部结构暴露给使用者。
 */
function describeError(status: number, payload: unknown): string {
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = (payload as { detail: unknown }).detail
    if (typeof detail === 'string' && detail.trim()) {
      return detail
    }
    if (Array.isArray(detail)) {
      const parts = detail
        .map((entry) => {
          if (!entry || typeof entry !== 'object') return null
          const item = entry as { loc?: unknown[]; msg?: unknown }
          const where = Array.isArray(item.loc) ? item.loc.filter((p) => p !== 'body').join('.') : ''
          const msg = typeof item.msg === 'string' ? item.msg : ''
          return [where, msg].filter(Boolean).join(': ')
        })
        .filter((part): part is string => Boolean(part))
      // 前缀是中文，拼进来的 `msg` 是 FastAPI / Pydantic 自己写的英文文案（比如
      // "Field required"），改不动也不该改——那是后端的诊断信息，逐字保留才能和
      // 服务端日志对上。这类错误正常不会出现在控制台上：请求体由前端自己构造。
      if (parts.length) return `请求不合法 —— ${parts.join('; ')}`
    }
  }
  return `请求失败：HTTP ${status}。`
}

async function readBody(response: Response): Promise<unknown> {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST'
  body?: unknown
  /**
   * 这些状态码不算"失败"，照样把响应体交给调用方。
   *
   * 发布接口需要它：409（已发过 / 结果未知）和 502（发布失败）都是**业务上正常的
   * 结果**，它们的响应体带着 `outcome`，正是界面要展示的东西。把它们当异常抛出
   * 会逼调用方去解析异常对象。
   */
  acceptStatuses?: readonly number[]
}

/**
 * 带状态码的请求。
 *
 * 发布类接口需要它：**HTTP 状态码本身是结果的一部分**（200 成功 / 409 冲突 /
 * 422 内容不合法 / 502 上游失败），只看响应体会丢掉一半信息。
 *
 * 4b-1 里这里曾经只返回响应体，而 `publishRun` 声明自己返回 `{httpStatus, body}`，
 * 于是 `httpStatus` 在运行时是 `undefined` —— 类型检查发现不了，只有真点到发布
 * 按钮才会暴露。所以这个函数是**唯一**的请求实现，`request` 只是它的薄包装。
 */
async function rawRequest<T>(
  path: string,
  options: RequestOptions = {},
): Promise<{ status: number; body: T }> {
  const { method = 'GET', body, acceptStatuses = [] } = options

  let response: Response
  try {
    response = await fetch(path, {
      method,
      headers: body === undefined ? { Accept: 'application/json' } : {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
  } catch {
    // fetch 只在网络层失败时抛异常。这里**不**把原始异常透出去：它的文案对使用者
    // 没有意义，而"服务没起来"才是真正要传达的信息。
    throw new ApiError(0, '连不上 API 服务，请确认后端是否在运行。')
  }

  const payload = await readBody(response)

  if (!response.ok && !acceptStatuses.includes(response.status)) {
    throw new ApiError(response.status, describeError(response.status, payload), payload)
  }
  return { status: response.status, body: payload as T }
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return (await rawRequest<T>(path, options)).body
}

function query(params: Record<string, string | number | undefined | null>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    // 空值一律不发：不然 `status=` 会被后端当成"按空字符串过滤"，语义完全不同。
    if (value === undefined || value === null || value === '') continue
    search.set(key, String(value))
  }
  const serialized = search.toString()
  return serialized ? `?${serialized}` : ''
}

export interface RunListParams {
  status?: string
  topic?: string
  dateFrom?: string
  dateTo?: string
  page?: number
  pageSize?: number
}

export interface ReviewQueueParams {
  page?: number
  pageSize?: number
  /** topic 模糊匹配（服务端）。空串由 `query()` 丢掉，不会变成"按空串过滤"。 */
  topic?: string
  /** 排序方向。**默认 `'oldest'`**，与后端默认值一致；`'newest'` 只服务于"刚跑完就想审这一条"。 */
  order?: QueueOrder
}

export interface PublishedListParams {
  status?: string
  /** 只看 publishing / publish_unknown（待人工裁决），按紧急程度排序。 */
  unresolved?: boolean
  dateFrom?: string
  dateTo?: string
  page?: number
  pageSize?: number
}

export const api = {
  // ---------- 只读 Admin API ----------

  /**
   * 定时无人值守自动发布的开关、额度、一致率与转正式门槛。
   *
   * **只读，而且控制台刻意不提供"暂停"按钮**：刹车是 `config/automation.yaml`
   * 里的 `enabled: false`，每个 tick 重读一次，改完立即生效。它比一个 HTTP 端点
   * 更可靠——后端没起来、页面打不开时它照样是刹车。所以这里读到的开关状态要
   * **显眼地**显示出来并说明去哪里改，而不是递一个会写文件的按钮：写那份配置要
   * 保住它整整一篇注释，那件事不该长在一个网页请求里。
   *
   * 后端另有 `POST /api/admin/auto-publish/tick`（供计划任务触发），需要单独的
   * token，**不经过这个客户端**。
   */
  getAutoPublishStatus(): Promise<AutoPublishStatus> {
    return request('/api/admin/auto-publish/status')
  },


  getOverview(days = 7): Promise<OverviewResponse> {
    return request(`/api/admin/overview${query({ days })}`)
  },

  listRuns(params: RunListParams = {}): Promise<RunListPage> {
    return request(
      `/api/admin/runs${query({
        status: params.status,
        topic: params.topic,
        date_from: params.dateFrom,
        date_to: params.dateTo,
        page: params.page,
        page_size: params.pageSize,
      })}`,
    )
  },

  getRunDetail(runId: string): Promise<AdminRunDetail> {
    return request(`/api/admin/runs/${encodeURIComponent(runId)}`)
  },

  listReviews(params: ReviewQueueParams = {}): Promise<ReviewQueuePage> {
    return request(
      `/api/admin/reviews${query({
        page: params.page,
        page_size: params.pageSize,
        topic: params.topic,
        order: params.order,
      })}`,
    )
  },

  listPublished(params: PublishedListParams = {}): Promise<PublishedListPage> {
    return request(
      `/api/admin/published${query({
        status: params.status,
        unresolved: params.unresolved ? 'true' : undefined,
        date_from: params.dateFrom,
        date_to: params.dateTo,
        page: params.page,
        page_size: params.pageSize,
      })}`,
    )
  },

  getAnalytics(days = 7): Promise<AnalyticsResponse> {
    return request(`/api/admin/analytics${query({ days })}`)
  },

  /**
   * 自己过去发布内容的观察一览（Phase 5B.4）。
   *
   * **只读**：这个接口在后端只有 GET（`tests/test_admin_feedback_api.py` 断言
   * POST 返回 405）。采集由 `scripts/refresh_post_metrics.py` 手工触发——
   * 控制台不提供"马上抓一次"，那会把一个只读控制台变成一个能对外发起请求的东西。
   */
  getFeedback(): Promise<FeedbackOverview> {
    return request('/api/admin/feedback')
  },

  /**
   * 过去反馈够不够可靠，可以作为未来学习材料（Phase 5C）。
   *
   * **只读，而且不改任何东西**：这个接口在后端只有 GET
   * （`tests/test_learning_api.py` 断言四个写方法都返回 405），它也不触发任何
   * 采集。它返回的仍然是观察——每个分组带着样本量、窗口与选取规则，
   * 每条比较带着它是怎么成立的（或为什么不成立）。
   *
   * `window` 不传时由后端取读数最多的那一档。真实数据只有 `0-1h` 的观察，
   * 所以默认就是它；把它写死在前端会让"数据变了、窗口没跟着变"变成一个
   * 只在特定数据下才显形的 bug。
   *
   * 这是控制台里**最重**的一次读（它按条读 checkpoint），所以它只在这一个
   * 页面与 Run Detail 的那一次单独请求里发生，绝不进别的聚合。
   */
  getLearning(window?: string): Promise<LearningOverview> {
    return request(`/api/admin/learning${query({ window })}`)
  },

  /**
   * 可以尝试什么（Phase 5D）。**只读**。
   *
   * 与 `getLearning` 的关系：那个接口回答"够不够可靠"，这个回答"可靠的话可以试
   * 什么方向"。提案是**现算**的，不是从库里读的；库里只存人对提案的裁决，
   * 所以没人裁决过的提案不会出现在数据库里，但一定出现在这里。
   *
   * `window` 必须与 `getLearning` 用同一个：窗口不同，算出来的提案就不是同一批，
   * 而裁决记录是按提案的确定性 id 存的，错配会让裁决看起来"过期了"。
   */
  getLearningProposals(window?: string): Promise<ProposalsOverview> {
    return request(`/api/admin/learning/proposals${query({ window })}`)
  },

  /**
   * 裁决一条提案：批准或否决。**控制台里唯一的写端点。**
   *
   * 按下之后**什么都不会发生**：不改 Prompt、不改 StyleRecipe、不改权重 / 阈值 /
   * 预算、不发布。它写下的只有一条裁决（谁在什么时候对哪条建议做了什么决定），
   * 供下一阶段参考。"批准"不等于"已应用"。
   *
   * 两种情况是业务上的正常结果，但**这里都当异常抛出**，因为界面需要区分它们
   * 并给出不同的提示：
   *
   * - `404`：这条提案在当前观察下已经不存在了（数据变了）。不要重试，
   *   应当提示"重新加载列表"；
   * - `409`：已经被裁决过了。裁决是不可改写的审计记录，不是可以覆盖的开关。
   */
  async reviewLearningProposal(
    proposalId: string,
    decision: ProposalReviewDecision,
    window?: string,
  ): Promise<LearningProposalRow> {
    return request(
      `/api/admin/learning/proposals/${encodeURIComponent(proposalId)}/review${query({ window })}`,
      { method: 'POST', body: decision },
    )
  },

  // ---------- 写操作：一律复用 Phase 3/4a 的既有端点 ----------

  /**
   * 新建一次运行。**只走既有的 `POST /api/runs`**，控制台不另造创建入口。
   *
   * 这是本客户端里**唯一会同步跑完整个 Agent**的请求：它返回时运行已经推进到
   * 人工审核（或跑完），所以耗时可观。也正因为如此，创建之后就**没有第二步**了
   * ——不要在它后面再去调一次"触发执行"，那会把同一条 Run 跑第二遍。
   *
   * 失败有两种形状，都由 `ApiError` 带出来：
   *   - 请求体不合法 → 422 `{"detail": [...]}`（本控制台不会构造出来，输入在
   *     前端就拦掉了）；
   *   - 运行跑失败 → 5xx，**`RunResponse` 形状**（`{run_id, status:"failed",
   *     error}`）。后者的 `error` 才是原因，见 `utils/newRun.ts`。
   */
  createRun(payload: CreateRunPayload): Promise<CreatedRunResponse> {
    return request('/api/runs', { method: 'POST', body: payload })
  },

  /**
   * 提交人工审核决定。**只走既有的 `/api/runs/{id}/review`**。
   *
   * 控制台不实现第二套审核入口：那套一定会漏掉 Phase 4a 用 CAS 换来的并发保护
   * （同一个 Run 被 resume 两次会重复抓取、重复生成）。
   */
  submitReview(runId: string, decision: ReviewDecision): Promise<ReviewCallResult> {
    return request(`/api/runs/${encodeURIComponent(runId)}/review`, {
      method: 'POST',
      body: decision,
    })
  },

  /**
   * 发布。**只走既有的 `/api/runs/{id}/publish`**。
   *
   * 失败与冲突都以响应体形式返回（见 `acceptStatuses`），因为 `outcome` 才是界面
   * 要展示的东西：`published` / `unknown` / `conflict` / `invalid` / `failed`。
   */
  async publishRun(runId: string): Promise<PublishCall> {
    const { status, body } = await rawRequest<PublishResponse>(
      `/api/runs/${encodeURIComponent(runId)}/publish`,
      { method: 'POST', acceptStatuses: [409, 422, 502] },
    )
    return { httpStatus: status, body }
  },

  /**
   * 裁决"结果未知"的发布。**只纠正状态，不发帖**——真正的重发必须另外再调一次
   * `publishRun`。这是 Phase 4a 刻意留的两道确认。
   */
  async resolvePublish(runId: string, decision: PublishResolution): Promise<PublishCall> {
    const { status, body } = await rawRequest<PublishResponse>(
      `/api/runs/${encodeURIComponent(runId)}/publish/resolve`,
      { method: 'POST', body: decision, acceptStatuses: [409, 422, 502] },
    )
    return { httpStatus: status, body }
  },
}

/** 用 x_post_id 拼出 X 上的链接。**刻意不把 URL 存进数据库**——它是纯派生值。 */
export function xPostUrl(xPostId: string): string {
  return `https://x.com/i/web/status/${encodeURIComponent(xPostId)}`
}
