import { useEffect, useId, useRef, type ReactNode } from 'react'

import { IconClose } from './ui/icons'

interface Props {
  title: ReactNode
  /** 标题下面的第二行：Run ID、创建时间这类"用来对账"的信息。 */
  subtitle?: ReactNode
  /** 标题右侧的操作区（复制、跳到 X 等）。 */
  actions?: ReactNode
  /** 固定在底部的操作区。有它时正文自己滚动，操作不会被长正文挤到屏幕外面。 */
  footer?: ReactNode
  onClose: () => void
  children: ReactNode
}

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'

/**
 * 右侧详情抽屉。
 *
 * Run 详情刻意做成抽屉而不是独立路由：在列表里对照多条 Run 时，每看一条就跳走
 * 一次会把筛选条件和浏览位置全部丢掉。
 *
 * Esc 关闭 + 背景锁定滚动。点了遮罩才关——不设自动关闭计时，因为这里展示的是
 * 需要人读的审计信息，不该在读到一半时自己消失。
 *
 * 键盘处理是这一版补上的。抽屉遮住整页时，如果焦点还留在底下的列表上，
 * Tab 会跑到**看不见的**按钮上去——用户按 Tab 却不知道焦点在哪，更糟的是可能
 * 按到隐藏元素触发操作。这里做三件事：
 *
 *   1. 打开时把焦点移进抽屉（`tabIndex={-1}` 的容器，读屏会念出标题）；
 *   2. Tab / Shift+Tab 在抽屉内部循环，出不去；
 *   3. 关闭时把焦点还给打开它的那个元素，用户回到列表里原来的位置继续看下一条。
 *
 * 没有用 `<dialog>`：它的原生模态行为和现有的遮罩层、动效、以及"点遮罩关闭"
 * 这套交互对不上，改造成本比这里手写二十行更高。
 */
export function Drawer({ title, subtitle, actions, footer, onClose, children }: Props) {
  const titleId = useId()
  const panelRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    // 记住是谁打开的，关闭时还回去。
    const opener = document.activeElement as HTMLElement | null
    panelRef.current?.focus()

    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        onClose()
        return
      }
      if (event.key !== 'Tab') return

      const panel = panelRef.current
      if (!panel) return
      const nodes = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        // 折起来的 `<details>` 里的东西不参与 Tab，否则焦点会跳进一个看不见的地方。
        (node) => node.offsetParent !== null || node === document.activeElement,
      )
      if (nodes.length === 0) {
        event.preventDefault()
        return
      }
      const first = nodes[0]!
      const last = nodes[nodes.length - 1]!
      const active = document.activeElement
      if (event.shiftKey && (active === first || active === panel)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && active === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKey)

    // 抽屉打开期间禁止背景滚动，否则滚轮会穿透到底下的列表
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
      // 还焦点时要确认那个元素还在——列表可能已经翻页把它卸载了。
      if (opener && document.contains(opener)) opener.focus()
    }
  }, [onClose])

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside
        className="drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        ref={panelRef}
      >
        <header className="drawer__head">
          <div className="drawer__head-main" id={titleId}>
            {title}
            {subtitle ? <div className="drawer__subtitle">{subtitle}</div> : null}
          </div>
          {actions ? <div className="drawer__head-actions">{actions}</div> : null}
          <button type="button" className="icon-btn" onClick={onClose} aria-label="关闭详情">
            <IconClose size={16} />
          </button>
        </header>
        <div className="drawer__body">{children}</div>
        {footer ? <div className="drawer__foot">{footer}</div> : null}
      </aside>
    </>
  )
}
