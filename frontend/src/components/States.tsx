import type { ReactNode } from 'react'

import { ApiError } from '../api/client'

/**
 * 加载 / 空 / 错误 三种状态的统一呈现。
 *
 * 每个数据区域都必须能表达这三种情况——不给错误态留位置的界面，出问题时只剩
 * 一片空白或一个白屏，使用者既不知道发生了什么，也不知道能做什么。
 */

export function LoadingState({ message = 'Loading…' }: { message?: string }) {
  return <div className="skeleton">{message}</div>
}

export function EmptyState({ title, hint }: { title: string; hint?: ReactNode }) {
  return (
    <div className="state">
      <div className="state__title">{title}</div>
      {hint ? <div>{hint}</div> : null}
    </div>
  )
}

interface ErrorStateProps {
  error: ApiError
  /** 出错的是什么，用在文案里（"Failed to load runs."）。 */
  what: string
  onRetry?: () => void
}

/**
 * 错误态。
 *
 * 只展示 **HTTP 状态码 + 后端给的安全文案**。刻意不显示 stack / 原始响应体 /
 * 异常对象——那些既不帮助使用者做决定，又可能把后端内部结构暴露出来。
 * 需要深挖时去看服务端日志。
 */
export function ErrorState({ error, what, onRetry }: ErrorStateProps) {
  const status = error.status === 0 ? 'network error' : `HTTP ${error.status}`
  return (
    <div className="state state--error">
      <div className="state__title">{what}</div>
      <div>
        {status} — {error.message}
      </div>
      {onRetry ? (
        <div className="state__actions">
          <button type="button" className="btn btn--sm" onClick={onRetry}>
            Retry
          </button>
        </div>
      ) : null}
    </div>
  )
}
