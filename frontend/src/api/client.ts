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
  OverviewResponse,
  PublishCall,
  PublishResolution,
  PublishResponse,
  PublishedListPage,
  ReviewCallResult,
  ReviewDecision,
  ReviewQueuePage,
  RunListPage,
} from '../types/api'

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
      if (parts.length) return `Invalid request — ${parts.join('; ')}`
    }
  }
  return `Request failed with HTTP ${status}.`
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
    throw new ApiError(0, 'Cannot reach the API server. Is the backend running?')
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

  listReviews(page = 1, pageSize = 20): Promise<ReviewQueuePage> {
    return request(`/api/admin/reviews${query({ page, page_size: pageSize })}`)
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

  // ---------- 写操作：一律复用 Phase 3/4a 的既有端点 ----------

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
