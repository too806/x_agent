/**
 * 展示层的格式化。
 *
 * 一个贯穿全文件的约定：**没有值时显示 `—`，不显示 `null` / `undefined` / `NaN`**。
 * 控制台里"没有这个值"本身就是信息（比如"这个 Run 没有发布记录"），它不该长得
 * 像渲染出错。所有 `—` 都只是空值的表示，绝不代表某个猜测出来的默认值。
 */

import type { PublishStatus, ReviewStatus, RunStatus } from '../types/api'

export const EMPTY = '—'

export function text(value: string | null | undefined): string {
  if (value === null || value === undefined) return EMPTY
  const trimmed = value.trim()
  return trimmed ? trimmed : EMPTY
}

/** 后端返回的时间是 ISO 字符串（带时区）。按浏览器本地时区展示，秒级精度。 */
export function dateTime(value: string | null | undefined): string {
  if (!value) return EMPTY
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return EMPTY
  return parsed.toLocaleString(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function dateOnly(value: string | null | undefined): string {
  if (!value) return EMPTY
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return EMPTY
  return parsed.toLocaleDateString()
}

/** 趋势图横轴的短日期：`09-25`。整年显示会把刻度挤没。 */
export function shortDate(value: string): string {
  const parsed = new Date(`${value}T00:00:00Z`)
  if (Number.isNaN(parsed.getTime())) return value
  return `${String(parsed.getUTCMonth() + 1).padStart(2, '0')}-${String(parsed.getUTCDate()).padStart(2, '0')}`
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return EMPTY
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  if (minutes < 60) return `${minutes}m ${rest}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

const RUN_STATUS_LABELS: Record<RunStatus, string> = {
  running: 'RUNNING',
  waiting_for_review: 'WAITING REVIEW',
  approved: 'APPROVED',
  edited: 'EDITED',
  rejected: 'REJECTED',
  failed: 'FAILED',
  completed: 'COMPLETED',
  publishing: 'PUBLISHING',
  publish_unknown: 'PUBLISH UNKNOWN',
  published: 'PUBLISHED',
}

const REVIEW_STATUS_LABELS: Record<ReviewStatus, string> = {
  approve: 'APPROVE',
  edit: 'EDIT',
  reject: 'REJECT',
  pending: 'PENDING',
}

const PUBLISH_STATUS_LABELS: Record<PublishStatus, string> = {
  published: 'PUBLISHED',
  failed: 'FAILED',
  publishing: 'PUBLISHING',
  publish_unknown: 'PUBLISH UNKNOWN',
}

/**
 * 状态 → 展示文案。查不到就原样返回原值。
 *
 * 保留原值而不是显示"未知"：后端加了新状态而前端还没跟上时，使用者至少能看到
 * 真实的状态字符串，而不是一个把信息抹掉的占位符。
 */
export function runStatusLabel(status: string | null | undefined): string {
  if (!status) return EMPTY
  return RUN_STATUS_LABELS[status as RunStatus] ?? status.toUpperCase()
}

export function reviewStatusLabel(status: string | null | undefined): string {
  if (!status) return EMPTY
  return REVIEW_STATUS_LABELS[status as ReviewStatus] ?? status.toUpperCase()
}

export function publishStatusLabel(status: string | null | undefined): string {
  if (!status) return EMPTY
  return PUBLISH_STATUS_LABELS[status as PublishStatus] ?? status.toUpperCase()
}

/** 把可能的 null 压成空串，供搜索框等受控输入使用。 */
export function orEmpty(value: string | null | undefined): string {
  return value ?? ''
}
