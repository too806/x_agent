import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { RunDetailDrawer } from '../components/RunDetailDrawer'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { TrendChart } from '../components/TrendChart'
import { IconAlert, IconRefresh } from '../components/ui/icons'
import { useAsync } from '../hooks/useAsync'
import { useConsole } from '../hooks/useConsole'
import {
  EMPTY,
  dateTime,
  publishStatusLabel,
  relativeTime,
  runStatusLabel,
  runTitle,
} from '../utils/format'
import { UNRESOLVED_PUBLISHED_PATH } from '../utils/publishAction'
import type { ReviewQueueItem, ResolutionQueueItem, RunSummary } from '../types/api'

const WINDOWS = [7, 14, 30, 90]

/**
 * 首页。
 *
 * 数据来自 **context 里那一份** `GET /api/admin/overview`——不是本页自己发起的
 * 第二个请求。侧栏、顶栏、这里读的是同一个对象，所以它们不可能互相矛盾；
 * 在这个页面上把窗口从 7 天改成 30 天，侧栏和顶栏跟着一起变，因为它们说的
 * 本来就是同一件事。
 *
 * 页面按五层组织，从上到下回答五个不同的问题：
 *
 *   1. 系统状态   —— 现在有没有事？哪件事不管会更糟？
 *   2. 核心指标   —— 累积下来是什么规模？
 *   3. 待处理工作 —— 具体是哪几条在等我？
 *   4. 趋势分析   —— 最近在变多还是变少？
 *   5. 最近活动   —— 刚刚发生了什么？
 *
 * 层与层之间是"先看结论、再看细节"的关系，所以第 1 层用最大的字号和最少的
 * 数字，第 5 层只给五行。反过来（一屏 12 个等大的卡片）等于把"哪个更重要"
 * 这个判断推给用户，而首页存在的意义正是替他把这个判断做掉。
 *
 * 口径上有一处必须说清楚：**KPI 是全量快照，趋势图是窗口内的**。后端就是这么
 * 返回的（KPI 无日期过滤，趋势图按窗口补齐），界面上照实标注，不把两者混成
 * "最近 N 天"的一句笼统标题。
 */

function GuardrailCell({ item }: { item: ReviewQueueItem }) {
  if (item.guardrail_passed === null) {
    return (
      <span className="guard guard--unknown" title="无法从 checkpoint 读出结果">
        {EMPTY}
      </span>
    )
  }
  if (item.guardrail_passed) {
    return <span className="guard guard--pass">通过</span>
  }
  return (
    <span className="guard guard--fail" title={item.guardrail_reasons.join('\n')}>
      未通过{item.guardrail_reasons.length ? ` (${item.guardrail_reasons.length})` : ''}
    </span>
  )
}

export function Overview() {
  const { overview: data, loading, error, days, setDays, refresh, pendingReviews, pendingResolutions } =
    useConsole()
  const [openRunId, setOpenRunId] = useState<string | null>(null)

  // 最近活动是**另一个接口**（`GET /api/admin/runs`），因为总览接口不返回
  // "最近创建了哪些 Run"。这正是它单独成层的原因：前四层是快照，这一层是流水。
  //
  // 它刻意不依赖总览数据：那会变成"总览回来之后才去请求"的串行等待。两个请求
  // 同时发出，各渲染各的，谁先回来谁先显示。
  const recent = useAsync(() => api.listRuns({ page: 1, pageSize: 5 }), [])

  // 页面上的"刷新"要刷新整页，包括这一层。顶栏那个刷新按钮只重取共享的总览，
  // 因为它对"最近活动"这一层一无所知——这层的刷新入口就在这里。
  function refreshAll() {
    refresh()
    recent.reload()
  }

  const attention = (data?.resolution_queue_total ?? 0) > 0
  const handled = pendingReviews + pendingResolutions

  return (
    <>
      {/* ---------------------------------------------------- 1. 系统状态 */}
      <section className="section section--lead">
        <div className="section__head">
          <h2 className="section__title">系统状态</h2>
          <div className="row">
            <label className="cell-muted" htmlFor="overview-window">
              时间窗口
            </label>
            <select
              id="overview-window"
              className="select select--sm"
              value={days}
              onChange={(event) => setDays(Number(event.target.value))}
            >
              {WINDOWS.map((value) => (
                <option key={value} value={value}>
                  最近 {value} 天
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn btn--sm"
              onClick={refreshAll}
              disabled={loading}
              title="刷新本页全部数据（共享总览 + 最近活动）"
            >
              <IconRefresh size={14} />
              刷新列表
            </button>
          </div>
        </div>

        {loading && !data ? (
          <LoadingState message="正在加载系统总览…" variant="cards" />
        ) : null}
        {error ? <ErrorState error={error} what="加载系统总览失败。" onRetry={refreshAll} /> : null}

        {data ? (
          <>
            {/* 待裁决队列排在最前，而且是整页唯一用实心强调色的地方：它的存在
                意味着一条推文可能已经发出去了却没人知道，这是整个控制台里唯一
                "不管就会更糟"的状态。它刻意**不长得像错误**——它不是失败，
                失败至少是确定的；它是未知，两者的处置方式完全不同。 */}
            {attention ? (
              <div className="action-banner action-banner--unknown">
                <span className="action-banner__icon" aria-hidden="true">
                  <IconAlert size={16} />
                </span>
                <div className="action-banner__body">
                  <div className="action-banner__title">
                    有 {data.resolution_queue_total} 次发布结果未确定
                  </div>
                  <p className="action-banner__text">
                    系统无法确认 X 是否已经创建帖子。为避免重复发布，请人工确认后再继续。
                  </p>
                  <p className="action-banner__meta">
                    它们的发布状态是 <strong>{publishStatusLabel('publishing')}</strong> 或{' '}
                    <strong>{publishStatusLabel('publish_unknown')}</strong>
                    ，系统不会自动重试——自动重试可能发出第二条。裁决必须由人来做，而且不可撤销。
                  </p>
                </div>
                <Link
                  className="btn btn--sm btn--primary action-banner__cta"
                  to={UNRESOLVED_PUBLISHED_PATH}
                >
                  立即处理 →
                </Link>
              </div>
            ) : (
              <div className="notice notice--ok">
                <div className="notice__title">没有结果未知的发布</div>
                <p>每一次发布尝试都有确定结果。这是你想要的状态。</p>
              </div>
            )}

            <div className="status-strip">
              <span className="status-strip__item">
                <span className="status-strip__label">管理接口</span>
                {/* 能读到这个值本身就是"通"的证据：这一屏的数据就是它返回的。
                    所以这里只说"正常"，不写"本次读取成功"那种绕圈子的说法。 */}
                <span className="status-strip__value">正常</span>
              </span>
              <span className="status-strip__item">
                <span className="status-strip__label">待人工处理</span>
                <span className="status-strip__value">{handled}</span>
                <span className="status-strip__hint">
                  （审核 {pendingReviews} · 裁决 {pendingResolutions}）
                </span>
              </span>
              <span className="status-strip__item">
                <span className="status-strip__label">发布进行中</span>
                <span className="status-strip__value">{data.kpis.publishing}</span>
              </span>
              <span className="status-strip__item">
                <span className="status-strip__label">指标窗口</span>
                <span className="status-strip__value">最近 {data.window_days} 天</span>
              </span>
            </div>
          </>
        ) : null}
      </section>

      {data ? (
        <>
          {/* ------------------------------------------------ 2. 核心指标 */}
          <section className="section">
            <div className="section__head">
              <h2 className="section__title">核心指标</h2>
              <span className="section__hint">全量计数，不受上方窗口限制</span>
            </div>

            {/* 两组数字，重要程度不同，所以不并成一行六张等大的卡片。
                上面一组是"累计做了多少"（背景），下面一组是"现在有没有事"
                （每天真正要看的），后者只在有值时才有重量。 */}
            <div className="kpi-group">
              <div className="kpi-group__title">主要指标</div>
              <div className="kpi-grid kpi-grid--primary">
                <div className="kpi">
                  <div className="kpi__label">总运行数</div>
                  <div className="kpi__value">{data.kpis.total_runs}</div>
                  <div className="kpi__sub">历史累计，全部 Run</div>
                </div>
                <div
                  className={
                    data.kpis.waiting_for_review > 0
                      ? 'kpi kpi--attention'
                      : 'kpi kpi--attention kpi--quiet'
                  }
                >
                  <div className="kpi__label">待审核</div>
                  <div className="kpi__value">{data.kpis.waiting_for_review}</div>
                  <div className="kpi__sub">
                    {data.kpis.waiting_for_review > 0 ? '正排在队列里等人决定' : '队列是空的'}
                  </div>
                </div>
                <div className="kpi kpi--ok">
                  <div className="kpi__label">已发布</div>
                  <div className="kpi__value">{data.kpis.published}</div>
                  <div className="kpi__sub">已在 X 上确认</div>
                </div>
              </div>
            </div>

            <div className="kpi-group">
              <div className="kpi-group__title">发布状态</div>
              <div className="kpi-grid kpi-grid--secondary">
                <div className="kpi">
                  <div className="kpi__label">发布中</div>
                  <div className="kpi__value">{data.kpis.publishing}</div>
                  <div className="kpi__sub">已发起，结果尚未记录</div>
                </div>
                {/* 发布失败与待审核同色（黄）：两者都是"需要处理、但确定"，
                    处置方式明确。橙色那一条留给"结果未知"——它不是同一类问题，
                    也不该长得像。 */}
                <div
                  className={
                    data.kpis.publish_failed > 0
                      ? 'kpi kpi--attention'
                      : 'kpi kpi--attention kpi--quiet'
                  }
                >
                  <div className="kpi__label">发布失败</div>
                  <div className="kpi__value">{data.kpis.publish_failed}</div>
                  <div className="kpi__sub">
                    {data.kpis.publish_failed > 0 ? '确定失败，重发是安全的' : '没有确定失败的发布'}
                  </div>
                </div>
                <div
                  className={
                    data.kpis.publish_unknown > 0
                      ? 'kpi kpi--critical'
                      : 'kpi kpi--critical kpi--quiet'
                  }
                >
                  <div className="kpi__label">发布结果未知</div>
                  <div className="kpi__value">{data.kpis.publish_unknown}</div>
                  <div className="kpi__sub">可能已发出，也可能没有</div>
                </div>
              </div>
            </div>

            {/* 数字本身不会解释自己的口径。这四行是整页最容易被误读的地方，
                所以宁可写出来也不靠读者猜。 */}
            <p className="cell-muted scope-note">
              后四项取自<strong>发布记录</strong>，不是 Run 状态。一个 Run 可以停在
              <strong>{runStatusLabel('approved')}</strong>，而它的发布记录是
              <strong>{publishStatusLabel('failed')}</strong> —— 说真话的是发布记录。
            </p>
            <p className="cell-muted scope-note">
              这里没有环比、增长率或趋势箭头：后端不返回历史同期的对比值，
              算出来的任何"涨了 12%"都只能是编的。
            </p>
          </section>

          {/* -------------------------------------------- 3. 待处理工作 */}
          <section className="section">
            <div className="section__head">
              <h2 className="section__title">待处理工作</h2>
              <span className="section__hint">需要人来决定的 {handled} 条</span>
            </div>

            <div className="work-grid">
              <div className="card card--flush card--alert">
                <div className="card__head">
                  <div>
                    <h3 className="card__title">发布裁决队列</h3>
                    <p className="card__hint">
                      发布结果未知或仍在进行，系统不会自行处置
                    </p>
                  </div>
                  <Link to={UNRESOLVED_PUBLISHED_PATH}>
                    {data.resolution_queue_total > data.resolution_queue.length
                      ? `查看全部 ${data.resolution_queue_total} 条 →`
                      : '打开发布记录 →'}
                  </Link>
                </div>
                {data.resolution_queue.length === 0 ? (
                  <EmptyState
                    title="没有需要裁决的发布"
                    hint="每一次发布尝试都有确定结果。这里会在出现结果未知的发布时亮起来。"
                  />
                ) : (
                  <div className="table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>主题</th>
                          <th>发布状态</th>
                          <th>运行状态</th>
                          <th>最后错误</th>
                          <th>
                            <span className="sr-only">操作</span>
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.resolution_queue.map((item: ResolutionQueueItem) => (
                          <tr
                            key={item.run_id}
                            className="table__row--clickable"
                            onClick={() => setOpenRunId(item.run_id)}
                          >
                            <td className="table__topic">
                              {/* 这一列没有 goal 可退：后端给裁决队列的下发里只有
                                  topic。它为空时如实显示 —，不猜。 */}
                              <span className="table__clamp" title={item.topic ?? undefined}>
                                {item.topic ?? <span className="cell-muted">{EMPTY}</span>}
                              </span>
                            </td>
                            <td>
                              <StatusBadge status={item.publish.status} kind="publish" />
                            </td>
                            <td>
                              <StatusBadge status={item.run_status} kind="run" />
                            </td>
                            <td className="cell-muted table__clamp" title={item.detail ?? undefined}>
                              {item.detail ?? EMPTY}
                            </td>
                            <td className="table__actions">
                              {/* 主操作是"去干活"，不是"再读一遍"：点进去才是裁决发生的地方。
                                  整行仍然可点（打开抽屉快速看一眼），所以这里要 stopPropagation，
                                  否则一次点击会同时跳页和开抽屉。 */}
                              <Link
                                className="btn btn--sm btn--ghost"
                                to={UNRESOLVED_PUBLISHED_PATH}
                                onClick={(event) => event.stopPropagation()}
                              >
                                裁决 →
                              </Link>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              <div className="card card--flush card--attention">
                <div className="card__head">
                  <div>
                    <h3 className="card__title">待审核队列</h3>
                    <p className="card__hint">Agent 已停下来等人做决定</p>
                  </div>
                  <Link to="/review">
                    {data.review_queue_total > data.review_queue.length
                      ? `查看全部 ${data.review_queue_total} 条 →`
                      : '打开审核工作台 →'}
                  </Link>
                </div>
                {data.review_queue.length === 0 ? (
                  <EmptyState
                    title="没有待审核的内容"
                    hint="一旦 Agent 停下来等人工决定，新的 Run 就会出现在这里。"
                  />
                ) : (
                  <div className="table-wrap">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>主题</th>
                          <th>草稿预览</th>
                          <th>规则检查</th>
                          <th>已等待</th>
                          <th>
                            <span className="sr-only">操作</span>
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.review_queue.map((item) => (
                          <tr
                            key={item.run_id}
                            className="table__row--clickable"
                            onClick={() => setOpenRunId(item.run_id)}
                          >
                            <td className="table__topic">
                              {/* Agent 还没选定主题时 topic 就是 null。如实显示 —，不猜。 */}
                              <span className="table__clamp" title={item.topic ?? undefined}>
                                {item.topic ?? <span className="cell-muted">{EMPTY}</span>}
                              </span>
                            </td>
                            <td className="cell-muted table__clamp" title={item.draft_preview ?? undefined}>
                              {item.draft_preview ?? '未存草稿'}
                            </td>
                            <td>
                              <GuardrailCell item={item} />
                            </td>
                            <td className="cell-muted cell-nowrap">
                              <span title={dateTime(item.created_at)}>
                                {relativeTime(item.created_at)}
                              </span>
                            </td>
                            <td className="table__actions">
                              {/* `?run=` 让工作台直接落在那一条上，不用再去左栏找一遍。 */}
                              <Link
                                className="btn btn--sm btn--ghost"
                                to={`/review?run=${encodeURIComponent(item.run_id)}`}
                                onClick={(event) => event.stopPropagation()}
                              >
                                审核 →
                              </Link>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            </div>

            <p className="cell-muted scope-note">
              当结果无法从 checkpoint 读出时，规则检查一列显示 {EMPTY}。那表示<strong>未知</strong>，
              不等于通过。
            </p>
          </section>

          {/* ------------------------------------------------ 4. 趋势分析 */}
          <section className="section">
            <div className="section__head">
              <h2 className="section__title">趋势分析</h2>
              <span className="section__hint">最近 {data.window_days} 天 · 按 UTC 自然日</span>
            </div>
            <div className="chart-grid">
              <div className="card">
                <div className="card__body">
                  <h3 className="card__title">每日运行量</h3>
                  <TrendChart
                    points={data.runs_trend}
                    emptyHint="该窗口内没有创建过 Run。可以把窗口放宽，看得更远一些。"
                  />
                </div>
              </div>
              <div className="card">
                <div className="card__body">
                  <h3 className="card__title">每日发布量</h3>
                  <TrendChart
                    points={data.published_trend}
                    emptyHint="该窗口内没有发布尝试记录。这里统计的是尝试次数，不是成功次数——失败的尝试同样会出现在图上。"
                  />
                </div>
              </div>
            </div>
          </section>
        </>
      ) : null}

      {/* ------------------------------------------------ 5. 最近活动 */}
      <section className="section">
        <div className="section__head">
          <h2 className="section__title">最近活动</h2>
          <Link to="/runs">打开运行记录 →</Link>
        </div>
        <div className="card card--flush">
          {recent.loading && !recent.data ? (
            <LoadingState message="正在加载最近活动…" variant="table" />
          ) : null}
          {recent.error ? (
            <ErrorState
              error={recent.error}
              what="加载最近活动失败。"
              onRetry={recent.reload}
            />
          ) : null}
          {recent.data && recent.data.items.length === 0 ? (
            <EmptyState
              title="还没有任何运行"
              hint="Agent 每跑一次都会在这里留下一条记录。"
            />
          ) : null}
          {recent.data && recent.data.items.length > 0 ? (
            /* 流水用 feed 而不是表格：这一层的读法是"自上而下按时间扫一遍"，
               不是"逐列比较"。表格会让人横向比较五个字段，而这里没有任何
               两个 Run 是拿来互相比较的。时间是右对齐的锚点，状态用徽标承载。 */
            <ol className="activity-feed">
              {recent.data.items.map((row: RunSummary) => (
                <li key={row.run_id}>
                  <button
                    type="button"
                    className="activity-feed__row"
                    onClick={() => setOpenRunId(row.run_id)}
                  >
                    <span className="activity-feed__main">
                      <span className="activity-feed__title" title={runTitle(row) ?? undefined}>
                        {runTitle(row) ?? <span className="cell-muted">{EMPTY}</span>}
                      </span>
                      <span className="activity-feed__status">
                        <StatusBadge status={row.run_status} kind="run" />
                        <StatusBadge status={row.publish_status} kind="publish" />
                      </span>
                    </span>
                    <time
                      className="activity-feed__time"
                      dateTime={row.created_at}
                      title={dateTime(row.created_at)}
                    >
                      {relativeTime(row.created_at)}
                    </time>
                  </button>
                </li>
              ))}
            </ol>
          ) : null}
        </div>
      </section>

      {openRunId ? (
        <RunDetailDrawer
          runId={openRunId}
          onClose={() => setOpenRunId(null)}
          onSettled={refreshAll}
        />
      ) : null}
    </>
  )
}
