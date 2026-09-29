import type { ReactNode } from 'react'

import { ApiError } from '../api/client'
import { backendErrorLabel, httpStatusLabel } from '../utils/format'
import { IconRefresh } from './ui/icons'
import { SkeletonCards, SkeletonLines, SkeletonTable } from './ui/Skeleton'

/**
 * 加载 / 空 / 错误 三种状态的统一呈现。
 *
 * 每个数据区域都必须能表达这三种情况——不给错误态留位置的界面，出问题时只剩
 * 一片空白或一个白屏，使用者既不知道发生了什么，也不知道能做什么。
 */

/**
 * 加载态。
 *
 * 之前这里是"加载中…"四个字：内容还没到时页面塌成一行高，数据回来再撑开，
 * 切换筛选时每切一次跳一次。现在占位块撑住即将出现的内容的高度，跳就不见了。
 *
 * `message` 仍然保留，但它改成**只给屏幕阅读器**：视觉上骨架屏自己会说
 * "正在加载"，再叠一行字是多余的；而读屏用户看不到骨架屏，需要这句话，
 * 并且需要 `role="status"` 让它被播报出来。
 */
export function LoadingState({
  message = '加载中…',
  variant = 'lines',
}: {
  message?: string
  /** 占位的形状。'table' 用于列表页，'cards' 用于 KPI 行。 */
  variant?: 'lines' | 'table' | 'cards'
}) {
  return (
    <div aria-busy="true">
      <span className="sr-only" role="status">
        {message}
      </span>
      {variant === 'table' ? (
        <SkeletonTable />
      ) : variant === 'cards' ? (
        <SkeletonCards />
      ) : (
        <SkeletonLines />
      )}
    </div>
  )
}

export function EmptyState({ title, hint }: { title: string; hint?: ReactNode }) {
  return (
    <div className="state">
      <div className="state__title">{title}</div>
      {/* 空态必须说清"为什么是空的"，以及"什么情况下会有东西"。
          只说"暂无数据"等于让人自己猜是没数据还是坏了。 */}
      {hint ? <div className="state__hint">{hint}</div> : null}
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
      <div className="cell-muted mono" style={{ marginTop: 'var(--space-1)' }}>
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
    <div className="state state--error" role="alert">
      <div className="state__title">{what}</div>
      <div className="state__hint">
        <div>{httpStatusLabel(error.status)}</div>
        <ErrorDetail error={error} />
      </div>
      {onRetry ? (
        <div className="state__actions">
          <button type="button" className="btn" onClick={onRetry}>
            <IconRefresh size={14} />
            重试
          </button>
        </div>
      ) : null}
    </div>
  )
}
