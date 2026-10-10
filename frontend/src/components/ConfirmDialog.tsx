import { useEffect, useId, useRef, type ReactNode, type RefObject } from 'react'

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
  /**
   * busy 时仍允许关闭。**默认 false**，因为默认情形是"不可撤销、必须亲眼看到结果"
   * 的操作（发布到 X、裁决未知结果）：请求已经发出去了，关掉对话框不会取消它，
   * 只会让人以为没发。
   *
   * 但也有反例：新建 Run 的请求是"在服务端把一件事跑起来"，可能要几分钟，而关掉
   * 窗口**不会**取消它。把用户锁在一个要等几分钟的对话框里，比让他走开更糟。这类
   * 调用方显式传 true。
   */
  dismissibleWhileBusy?: boolean
  /**
   * 取消按钮的文案，默认「取消」。`dismissibleWhileBusy` 的调用方通常要换成
   * 「关闭」——「取消」会被读成"取消这次运行"，而这个按钮只关窗口，不取消任何东西。
   */
  cancelLabel?: string
  /** 额外的前置条件未满足时禁用确认（例如"必须勾选已确认"）。 */
  confirmDisabled?: boolean
  /**
   * 打开时焦点落在哪。**默认是取消键**（见上面的键盘行为说明）。
   *
   * 这条默认对"不可撤销"的对话框是对的，但对**要人先打字**的对话框是错的：
   * 焦点在一个按钮上，敲什么都不会出现在输入框里，而用户的第一反应正是直接打字。
   * 那类调用方（新建 Run）把自己的输入框 ref 传进来。
   */
  initialFocusRef?: RefObject<HTMLElement | null>
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
 *     例外是要人**先打字**的对话框（新建 Run）：那里传 `initialFocusRef`，
 *     否则人打开窗口直接打字，字全打在取消键上。
 *   - Tab 在对话框内循环；Esc 关闭，但 busy 时不关（请求已经发出去了，
 *     关掉对话框不会取消它，只会让用户以为没发）。`dismissibleWhileBusy`
 *     的调用方不受这条限制——那类请求在关掉之后照样跑完，结果另行通知。
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
  dismissibleWhileBusy = false,
  cancelLabel = '取消',
  confirmDisabled = false,
  initialFocusRef,
  error = null,
  onConfirm,
  onCancel,
}: Props) {
  const titleId = useId()
  const bodyId = useId()
  const panelRef = useRef<HTMLDivElement | null>(null)
  const cancelRef = useRef<HTMLButtonElement | null>(null)

  // 键盘行为要用的三个值里，`onCancel` 是每次渲染都会换新身份的函数（调用方几乎
  // 都传内联箭头函数）。**不能把它们写进下面那个 effect 的 deps**：那会让
  // "挂载时聚焦 + 装监听 + 锁滚动" 这段逻辑每渲染一次就重跑一次，而它的清理函数
  // 会把焦点还给打开它的元素、函数体又立刻把焦点抓回取消键。
  //
  // 这个 bug 在只有按钮的对话框上看不出来（焦点本来就该在按钮上），但新建 Run 的
  // 输入框里**一个字都留不住**：敲一下，setState 触发重渲染，焦点刚写进输入框就被
  // 夺回取消键，第二下就打在按钮上了。所以这里用 ref 把"最新值"和"只装一次"分开。
  const latest = useRef({ onCancel, busy, dismissibleWhileBusy })
  useEffect(() => {
    latest.current = { onCancel, busy, dismissibleWhileBusy }
  })

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    // 默认落在取消键上；要人先打字的对话框自带 initialFocusRef。
    ;(initialFocusRef?.current ?? cancelRef.current)?.focus()

    function onKey(event: KeyboardEvent) {
      const { onCancel, busy, dismissibleWhileBusy } = latest.current
      if (event.key === 'Escape') {
        if (!busy || dismissibleWhileBusy) onCancel()
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
      // 焦点已经掉到面板外（例如输入框在请求在途时被 disable，浏览器把焦点退回
      // `<body>`）时，别按"循环"处理——那两条分支都要求焦点**正好**在第一/最后一个
      // 元素上，焦点在 `<body>` 时一个都不成立，Tab 会直接穿到模态框后面的页面上去。
      // 这种情况下把它拽回面板里，而不是放它出去。
      const active = document.activeElement
      if (!(active instanceof HTMLElement) || !panel.contains(active)) {
        event.preventDefault()
        ;(event.shiftKey ? last : first).focus()
        return
      }
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
    // 故意只跑一次（挂载 / 卸载）：需要变化的值走上面的 `latest`。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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
          <button
            type="button"
            className="btn"
            onClick={onCancel}
            disabled={busy && !dismissibleWhileBusy}
            ref={cancelRef}
          >
            {cancelLabel}
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
