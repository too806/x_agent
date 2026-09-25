import type { ReactNode } from 'react'

import { ApiError } from '../api/client'
import { backendErrorLabel, httpStatusLabel } from '../utils/format'

/**
 * 加载 / 空 / 错误 三种状态的统一呈现。
 *
 * 每个数据区域都必须能表达这三种情况——不给错误态留位置的界面，出问题时只剩
 * 一片空白或一个白屏，使用者既不知道发生了什么，也不知道能做什么。
 */

export function LoadingState({ message = '加载中…' }: { message?: string }) {
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
  /** 出错的是什么，用在文案里（"加载运行记录失败。"）。 */
  what: string
  onRetry?: () => void
}

/**
 * 错误正文。
 *
 * 后端 `detail` 是**英文**（`app/main.py` 里那 5 条），认得出的先给中文，再把后端
 * 原文用小字跟在后面；认不出的原样显示。
 *
 * 原文**不删**，理由和 `ErrorState` 里那条一样：它是接口返回的诊断文本，删掉就只剩
 * 一句界面上独有的话，排查时对不上服务端日志。这里做的是"先说人话，原文留着"，
 * 不是"把后端的话替换掉"。
 */
export function ErrorDetail({ error }: { error: ApiError }) {
  const label = backendErrorLabel(error.message)
  if (label === null) return <div>{error.message}</div>
  return (
    <>
      <div>{label}</div>
      <div className="cell-muted mono" style={{ marginTop: 4 }}>
        {error.message}
      </div>
    </>
  )
}

/**
 * 错误态。
 *
 * 只展示 **HTTP 状态码 + 后端给的安全文案**。刻意不显示 stack / 原始响应体 /
 * 异常对象——那些既不帮助使用者做决定，又可能把后端内部结构暴露出来。
 * 需要深挖时去看服务端日志。
 */
export function ErrorState({ error, what, onRetry }: ErrorStateProps) {
  return (
    <div className="state state--error">
      <div className="state__title">{what}</div>
      <div>{httpStatusLabel(error.status)}</div>
      <ErrorDetail error={error} />
      {onRetry ? (
        <div className="state__actions">
          <button type="button" className="btn btn--sm" onClick={onRetry}>
            重试
          </button>
        </div>
      ) : null}
    </div>
  )
}
