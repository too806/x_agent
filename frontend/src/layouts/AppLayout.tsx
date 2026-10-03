import { NavLink, Outlet, useLocation } from 'react-router-dom'

import { IconAnalytics, IconOverview, IconPublished, IconRefresh, IconReview, IconRuns } from '../components/ui/icons'
import { useConsole } from '../hooks/useConsole'

/**
 * 导航项。
 *
 * `label` 是侧栏上的**短标签**（总览 / 运行 / 审核 / 发布 / 分析），
 * `name` 是完整名称，用于顶栏标题、`title` 与 `aria-label`。
 *
 * 两者分开是有意的：侧栏是每天扫过几十次的东西，五个短词比五个四字词快得多；
 * 但"审核"这两个字在无障碍树里、在鼠标悬停时、在顶栏上仍然要是完整的
 * "内容审核"——短标签只应该省掉眼睛重复读的力气，不该省掉理解所需的上下文。
 */
const NAV = [
  {
    to: '/',
    label: '总览',
    name: '系统总览',
    end: true,
    title: '系统总览',
    blurb: '系统健康、核心指标与当前待办',
    Icon: IconOverview,
  },
  {
    to: '/runs',
    label: '运行',
    name: '运行记录',
    end: false,
    title: '运行记录',
    blurb: 'Agent 的每一次运行：研究、草稿、决策轨迹',
    Icon: IconRuns,
  },
  {
    to: '/review',
    label: '审核',
    name: '内容审核',
    end: false,
    title: '审核工作台',
    blurb: '逐条决定批准、编辑或拒绝',
    Icon: IconReview,
  },
  {
    to: '/published',
    label: '发布',
    name: '发布记录',
    end: false,
    title: '发布记录',
    blurb: '发布到 X 的结果，以及需要人工裁决的未知状态',
    Icon: IconPublished,
  },
  {
    to: '/analytics',
    label: '分析',
    name: '数据分析',
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
          <div className="sidebar__brand-name">X_Agent</div>
          <div className="sidebar__brand-meta">AI Content Operations</div>
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
            // 待裁决比待审核更急：前者可能是"一条帖子已经发出去了却没人知道"，
            // 后者只是"等一个人做决定"。所以它用实心橙，是全站唯一的橙色。
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
                // 两者都用**完整名称**：短标签只省眼睛，不省上下文。
                aria-label={entry.name}
                title={entry.name}
              >
                <entry.Icon className="nav-item__icon" size={16} />
                <span className="nav-item__label">{entry.label}</span>
                {/* 为 0 时不显示：一排永远存在的 "0" 只会让真正非零的那个更难被发现。 */}
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

        {/* 「系统」区：不是导航，是"这个控制台当前的设定"。用一条线和大标题分开，
            免得被误当成第六个页面。 */}
        <div className="sidebar__system">
          <div className="sidebar__system-title">系统</div>
          {/* 窗口是真实读到的值，不是写死的文案——它和首页的下拉框是同一份状态。 */}
          <div className="sidebar__system-row">
            <span>指标窗口</span>
            <span className="sidebar__system-value">最近 {days} 天</span>
          </div>
          <div className="sidebar__system-row">
            <span>写操作</span>
            <span className="sidebar__system-value">走既有后端 API</span>
          </div>
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
            {/* 这一颗只刷**全局状态**：总览数字与待办计数。页面自己的列表由页面上的
                「刷新列表」负责。两个按钮文案不同是刻意的——用户不应该需要猜
                "这个刷新到底刷了什么"。 */}
            <button
              type="button"
              className="btn btn--sm btn--ghost topbar__refresh"
              onClick={refresh}
              disabled={loading}
              aria-label="刷新状态：重新读取总览数据与待办计数"
              title="刷新状态（总览数据与待办计数）"
            >
              <IconRefresh size={14} />
              刷新状态
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
