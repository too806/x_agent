import { useEffect, type ReactNode } from 'react'

interface Props {
  title: ReactNode
  onClose: () => void
  children: ReactNode
}

/**
 * 右侧详情抽屉。
 *
 * Run 详情刻意做成抽屉而不是独立路由：在列表里对照多条 Run 时，每看一条就跳走
 * 一次会把筛选条件和浏览位置全部丢掉。
 *
 * Esc 关闭 + 背景锁定滚动。点了遮罩才关——不设自动关闭计时，因为这里展示的是
 * 需要人读的审计信息，不该在读到一半时自己消失。
 */
export function Drawer({ title, onClose, children }: Props) {
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)

    // 抽屉打开期间禁止背景滚动，否则滚轮会穿透到底下的列表
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = previous
    }
  }, [onClose])

  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-modal="true">
        <header className="drawer__head">
          <button
            type="button"
            className="btn btn--sm drawer__close"
            onClick={onClose}
            aria-label="关闭"
          >
            关闭
          </button>
          {title}
        </header>
        <div className="drawer__body">{children}</div>
      </aside>
    </>
  )
}
