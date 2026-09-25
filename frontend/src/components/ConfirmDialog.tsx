import { useEffect, type ReactNode } from 'react'

import { ApiError } from '../api/client'
import { ErrorDetail } from './States'
import { httpStatusLabel } from '../utils/format'

interface Props {
  title: string
  children: ReactNode
  confirmLabel: string
  /** 破坏性操作（发布到 X、裁决未知结果）用红色主按钮。 */
  danger?: boolean
  busy?: boolean
  /** 额外的前置条件未满足时禁用确认（例如"必须勾选已确认"）。 */
  confirmDisabled?: boolean
  error?: ApiError | null
  onConfirm: () => void
  onCancel: () => void
}

/**
 * 二次确认对话框。
 *
 * 存在的理由是**防止误触**：控制台上有两类操作是不可撤销的——向真实 X 账号发帖，
 * 以及在"结果未知"时重新发帖。它们不能靠一个按钮点下去就发生。
 *
 * Esc 关闭、锁定滚动，都是为了让这个确认框像一道真正的闸门，而不是一个可以
 * 顺手点过去的提示。
 */
export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  danger = false,
  busy = false,
  confirmDisabled = false,
  error = null,
  onConfirm,
  onCancel,
}: Props) {
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape' && !busy) onCancel()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [busy, onCancel])

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label={title}>
      <div className="modal">
        <div className="modal__head">{title}</div>
        <div className="modal__body">
          {children}
          {error ? (
            <div className="notice notice--danger" style={{ marginTop: 12 }}>
              <div className="notice__title">{httpStatusLabel(error.status)}</div>
              <ErrorDetail error={error} />
            </div>
          ) : null}
        </div>
        <div className="modal__foot">
          <button type="button" className="btn" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button
            type="button"
            className={danger ? 'btn btn--danger' : 'btn btn--primary'}
            onClick={onConfirm}
            disabled={busy || confirmDisabled}
          >
            {busy ? '处理中…' : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
