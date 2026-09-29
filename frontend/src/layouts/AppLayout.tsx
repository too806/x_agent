import { NavLink, Outlet, useLocation } from 'react-router-dom'

import { IconAnalytics, IconOverview, IconPublished, IconRefresh, IconReview, IconRuns } from '../components/ui/icons'
import { useConsole } from '../hooks/useConsole'

const NAV = [
  {
    to: '/',
    label: '系统总览',
    end: true,
    title: '系统总览',
    blurb: '系统健康、核心指标与当前待办',
    Icon: IconOverview,
  },
  {
    to: '/runs',
    label: '运行记录',
    end: false,
    title: '运行记录',
    blurb: 'Agent 的每一次运行：研究、草稿、决策轨迹',
    Icon: IconRuns,
  },
  {
    to: '/review',
    label: '内容审核',
    end: false,
    title: '审核工作台',
    blurb: '逐条决定批准、编辑或拒绝',
    Icon: IconReview,
  },
  {
    to: '/published',
    label: '发布记录',
    end: false,
    title: '发布记录',
    blurb: '发布到 X 的结果，以及需要人工裁决的未知状态',
    Icon: IconPublished,
  },
  {
    to: '/analytics',
    label: '数据分析',
    end: false,
    title: '数据分析',
    blurb: '窗口内的运行与发布分布',
    Icon: IconAnalytics,
  },
]

/**
 * 控制台外壳：左侧导航 + 顶栏 + 内容区。
 *
 * 布局本身不做任何业务判断——它只读取 context 里的待办计数。所有业务判断都在
 * 各页面里，页面才是知道"这个数字意味着什么"的地方。
 *
 * 顶栏右侧那组数字和侧栏上的同一组数字来自**同一个 context**，所以它们不可能
 * 互相矛盾。顶栏这份不是重复：侧栏在窄屏（≤1024）会收成图标条，数字被压成一个
 * 小点，而"还有多少条待裁决"是这一屏最重要的信息，不能因为窗口变窄就消失。
 */
export function AppLayout() {
  const location = useLocation()
  const { pendingReviews, pendingResolutions, ready, refresh, loading, days } = useConsole()

  const current = NAV.find((entry) =>
    entry.end ? location.pathname === entry.to : location.pathname.startsWith(entry.to),
  )

  return (
    <div className="app-shell">
      {/* 键盘用户第一个 Tab 就能跳过整条导航。侧栏有 5 个链接，没有它就得按 6 次。 */}
      <a className="skip-link" href="#main">
        跳到主要内容
      </a>

      <aside className="sidebar">
        <div className="sidebar__brand">
          <div className="sidebar__brand-name">X AI Content Agent</div>
          <div className="sidebar__brand-meta">运营控制台</div>
        </div>

        <nav className="sidebar__nav" aria-label="主导航">
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
                // 窄屏（≤1024）标签被 CSS 隐藏。`display: none` 会把文字从无障碍树里
                // 一起拿掉，链接就只剩一个 aria-hidden 的图标——等于没有名字。
                // aria-label 让它在任何屏宽下都有名字；title 顺带给鼠标用户一个提示。
                aria-label={entry.label}
                title={entry.label}
              >
                <entry.Icon className="nav-item__icon" size={16} />
                <span className="nav-item__label">{entry.label}</span>
                {ready && count > 0 ? (
                  <span className={`nav-item__count${alert ? ' nav-item__count--alert' : ''}`}>
                    {count}
                    {/* 窄屏下这个数字被 CSS 压成一个点（font-size:0 + 透明）。数字本身
                        还在无障碍树里，但"3"是什么的 3 得说出来。 */}
                    <span className="sr-only">
                      条{entry.to === '/review' ? '待审核' : '待裁决'}
                    </span>
                  </span>
                ) : null}
              </NavLink>
            )
          })}
        </nav>

        <div className="sidebar__foot">
          {/* 窗口是真实读到的值，不是写死的文案——它和首页的下拉框是同一份状态。 */}
          <div>指标窗口 · 最近 {days} 天</div>
          <div>Phase 4b.2 · 写操作走既有后端 API</div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="topbar__head">
            <h1 className="topbar__title">{current?.title ?? 'X AI Content Agent'}</h1>
            {current?.blurb ? <span className="topbar__blurb">{current.blurb}</span> : null}
          </div>
          <div className="topbar__context">
            <span className="topbar__stat">
              待审核
              <span className="topbar__stat-value">{ready ? pendingReviews : '—'}</span>
            </span>
            <span className={pendingResolutions > 0 ? 'topbar__stat topbar__stat--alert' : 'topbar__stat'}>
              待裁决
              <span className="topbar__stat-value">{ready ? pendingResolutions : '—'}</span>
            </span>
            <button
              type="button"
              className="icon-btn"
              onClick={refresh}
              disabled={loading}
              aria-label="刷新数据"
              title="刷新数据"
            >
              <IconRefresh size={15} />
            </button>
          </div>
        </header>

        <main className="content" id="main" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  )
}
