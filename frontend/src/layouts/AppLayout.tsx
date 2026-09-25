import { NavLink, Outlet, useLocation } from 'react-router-dom'

import { useConsole } from '../hooks/useConsole'
import { EMPTY } from '../utils/format'

const NAV = [
  { to: '/', label: 'Overview', end: true, title: 'Overview' },
  { to: '/runs', label: 'Runs', end: false, title: 'Runs' },
  { to: '/review', label: 'Review', end: false, title: 'Review Workspace' },
  { to: '/published', label: 'Published', end: false, title: 'Published Posts' },
  { to: '/analytics', label: 'Analytics', end: false, title: 'System Analytics' },
]

/**
 * 控制台外壳：左侧导航 + 顶栏 + 内容区。
 *
 * 布局本身不做任何数据判断——它只读取 context 里的待办计数。所有业务判断都在
 * 各页面里，页面才是知道"这个数字意味着什么"的地方。
 */
export function AppLayout() {
  const location = useLocation()
  const { pendingReviews, pendingResolutions, ready } = useConsole()

  const current = NAV.find((entry) =>
    entry.end ? location.pathname === entry.to : location.pathname.startsWith(entry.to),
  )

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar__brand">
          <div className="sidebar__brand-name">X AI Content Agent</div>
          <div className="sidebar__brand-meta">Operations Console</div>
        </div>

        <nav className="sidebar__nav">
          {NAV.map((entry) => {
            // 每个导航项挂上它对应的待办数：Review 是待审队列，Published 是待裁决
            // 队列（publishing / publish_unknown 两类 unresolved 都在这里）。
            const count =
              entry.to === '/review'
                ? pendingReviews
                : entry.to === '/published'
                  ? pendingResolutions
                  : 0
            const alert = entry.to === '/published' && count > 0
            return (
              <NavLink
                key={entry.to}
                to={entry.to}
                end={entry.end}
                className={({ isActive }) => `nav-item${isActive ? ' nav-item--active' : ''}`}
              >
                <span>{entry.label}</span>
                {ready && count > 0 ? (
                  <span className={`nav-item__count${alert ? ' nav-item__count--alert' : ''}`}>
                    {count}
                  </span>
                ) : null}
              </NavLink>
            )
          })}
        </nav>

        <div className="sidebar__foot">
          <div>Phase 4b</div>
          <div>Read-only console · writes go through the existing API</div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <h1 className="topbar__title">{current?.title ?? EMPTY}</h1>
          <div className="topbar__actions">
            <span className="cell-muted">
              {pendingReviews} awaiting review · {pendingResolutions} to resolve
            </span>
          </div>
        </header>

        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
