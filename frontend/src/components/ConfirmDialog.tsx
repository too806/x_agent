import { useEffect, useId, useRef, type ReactNode } from 'react'

import { ApiError } from '../api/client'
import { ErrorDetail } from './States'
import { httpStatusLabel } from '../utils/format'

interface Props {
  title: string
  children: ReactNode
  confirmLabel: string
  /** 破坏性操作（发布到 X、裁决未知结果）用红色主按钮。 */
  danger?: boolean
  /** 比 danger 轻一档：操作有风险但不是"不可撤销"。优先用 danger 表达不可撤销。 */
  tone?: 'default' | 'warn'
  /** 风险提示。单独一块、放在按钮上方，和正文分开——它是要在按下确认前最后一秒读到的东西。 */
  risk?: ReactNode
  busy?: boolean
  /** 额外的前置条件未满足时禁用确认（例如"必须勾选已确认"）。 */
  confirmDisabled?: boolean
  error?: ApiError | null
  onConfirm: () => void
  onCancel: () => void
}

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'

/**
 * 二次确认对话框。
 *
 * 存在的理由是**防止误触**：控制台上有两类操作是不可撤销的——向真实 X 账号发帖，
 * 以及在"结果未知"时重新发帖。它们不能靠一个按钮点下去就发生。
 *
 * 结构固定为 标题 / 说明 / 风险 / 取消 · 确认，三个调用方共用同一套骨架。
 * 以前"风险"是由各调用方自己在正文里塞一段红字或一个勾选框，位置和写法都不一样，
 * 于是同一个"这一步会发生什么"的问题，在三个对话框里有三种读法。现在它是一块
 * 固定位置的内容（按钮正上方），扫一眼就知道该看哪里。
 *
 * 键盘行为：
 *   - 打开时焦点落在**取消**上，不是确认上。这几个对话框的确认键会真的发帖，
 *     默认聚焦它等于给"回车"准备了一个危险的含义；回车取消是安全的。
 *   - Tab 在对话框内循环；Esc 关闭，但 busy 时不关（请求已经发出去了，
 *     关掉对话框不会取消它，只会让用户以为没发）。
 *   - 关闭时焦点回到打开它的按钮。
 */
export function ConfirmDialog({
  title,
  children,
  confirmLabel,
  danger = false,
  tone = 'default',
  risk,
  busy = false,
  confirmDisabled = false,
  error = null,
  onConfirm,
  onCancel,
}: Props) {
  const titleId = useId()
  const bodyId = useId()
  const panelRef = useRef<HTMLDivElement | null>(null)
  const cancelRef = useRef<HTMLButtonElement | null>(null)

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    cancelRef.current?.focus()

    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        if (!busy) onCancel()
        return
      }
      if (event.key !== 'Tab') return
      const panel = panelRef.current
      if (!panel) return
      const nodes = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (node) => node.offsetParent !== null || node === document.activeElement,
      )
      if (nodes.length === 0) return
      const first = nodes[0]!
      const last = nodes[nodes.length - 1]!
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKey)
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
      if (opener && document.contains(opener)) opener.focus()
    }
  }, [busy, onCancel])

  const toneClass = danger ? ' modal--danger' : tone === 'warn' ? ' modal--warn' : ''

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={bodyId}
    >
      <div className={`modal${toneClass}`} ref={panelRef}>
        <div className="modal__head" id={titleId}>
          {title}
        </div>
        <div className="modal__body" id={bodyId}>
          {children}
          {error ? (
            <div className="notice notice--danger modal__error">
              <div className="notice__title">{httpStatusLabel(error.status)}</div>
              <ErrorDetail error={error} />
            </div>
          ) : null}
        </div>
        {risk ? <div className="modal__risk">{risk}</div> : null}
        <div className="modal__foot">
          <button type="button" className="btn" onClick={onCancel} disabled={busy} ref={cancelRef}>
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
