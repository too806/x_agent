import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { RunDetailDrawer } from '../components/RunDetailDrawer'
import { ErrorState, EmptyState, LoadingState } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { TrendChart } from '../components/TrendChart'
import { useAsync } from '../hooks/useAsync'
import { EMPTY, publishStatusLabel, relativeTime, runStatusLabel } from '../utils/format'
import type { ReviewQueueItem, ResolutionQueueItem } from '../types/api'

const WINDOWS = [7, 14, 30, 90]

/**
 * 首页。
 *
 * 全部数据来自**一次** `GET /api/admin/overview` 调用（后端刻意做成单接口，
 * 见 `app/api/admin.py`）。前端不在这里循环调用其它接口拼装仪表盘——那会让首页
 * 变成五个独立加载、独立失败的区块。
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
  const [days, setDays] = useState(7)
  const [openRunId, setOpenRunId] = useState<string | null>(null)

  const { data, loading, error, reload } = useAsync(() => api.getOverview(days), [days])

  return (
    <>
      <section className="section">
        <div className="section__head">
          <h2 className="section__title">系统快照</h2>
          <div className="row">
            <label className="cell-muted" htmlFor="overview-window">
              时间窗口
            </label>
            <select
              id="overview-window"
              className="select"
              style={{ width: 130 }}
              value={days}
              onChange={(event) => setDays(Number(event.target.value))}
            >
              {WINDOWS.map((value) => (
                <option key={value} value={value}>
                  最近 {value} 天
                </option>
              ))}
            </select>
            <button type="button" className="btn btn--sm" onClick={reload} disabled={loading}>
              刷新
            </button>
          </div>
        </div>

        {loading && !data ? <LoadingState message="正在加载系统总览…" /> : null}
        {error ? <ErrorState error={error} what="加载系统总览失败。" onRetry={reload} /> : null}
      </section>

      {data ? (
        <>
          {/* 待裁决队列排在最前：它的存在意味着一条推文可能已经发出去了却没人知道，
              这是整个控制台里唯一"不管就会更糟"的状态。 */}
          {data.resolution_queue_total > 0 ? (
            <div className="notice notice--warn" style={{ marginBottom: 20 }}>
              <div className="notice__title">
                {data.resolution_queue_total} 次发布尝试需要人工裁决
              </div>
              它们的发布状态是 <strong>{publishStatusLabel('publishing')}</strong> 或{' '}
              <strong>{publishStatusLabel('publish_unknown')}</strong>
              。系统不会为它们自动再调一次 X —— 裁决必须由人执行，而且不可撤销。
            </div>
          ) : null}

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">关键指标</h2>
              <span className="section__hint">全量计数，不受上方窗口限制</span>
            </div>

            <div className="kpi-grid">
              <div className="kpi">
                <div className="kpi__label">总运行数</div>
                <div className="kpi__value">{data.kpis.total_runs}</div>
                <div className="kpi__sub">历史累计，全部 Run</div>
              </div>
              <div className="kpi kpi--attention">
                <div className="kpi__label">待审核</div>
                <div className="kpi__value">{data.kpis.waiting_for_review}</div>
                <div className="kpi__sub">当前正排在队列里</div>
              </div>
              <div className="kpi">
                <div className="kpi__label">已发布</div>
                <div className="kpi__value">{data.kpis.published}</div>
                <div className="kpi__sub">已在 X 上确认</div>
              </div>
              <div className="kpi">
                <div className="kpi__label">发布失败</div>
                <div className="kpi__value">{data.kpis.publish_failed}</div>
                <div className="kpi__sub">确定失败，重发是安全的</div>
              </div>
              <div className="kpi kpi--critical">
                <div className="kpi__label">发布结果未知</div>
                <div className="kpi__value">{data.kpis.publish_unknown}</div>
                <div className="kpi__sub">可能已发出，也可能没有</div>
              </div>
              <div className="kpi kpi--attention">
                <div className="kpi__label">发布中</div>
                <div className="kpi__value">{data.kpis.publishing}</div>
                <div className="kpi__sub">已发起，结果尚未记录</div>
              </div>
            </div>

            <p className="cell-muted" style={{ marginBottom: 0 }}>
              后四项取自<strong>发布记录</strong>，不是 Run 状态。一个 Run 可以停在
              <strong>{runStatusLabel('approved')}</strong>，而它的发布记录是
              <strong>{publishStatusLabel('failed')}</strong> —— 说真话的是发布记录。
            </p>
          </section>

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">每日活动</h2>
              <span className="section__hint">最近 {data.window_days} 天 · 按 UTC 自然日</span>
            </div>
            <div className="chart-grid">
              <div className="card">
                <div className="card__body">
                  <div className="section__title">每日运行量</div>
                  <TrendChart
                    points={data.runs_trend}
                    emptyHint="该窗口内没有创建过 Run。可以把窗口放宽，看得更远一些。"
                  />
                </div>
              </div>
              <div className="card">
                <div className="card__body">
                  <div className="section__title">每日发布量</div>
                  <TrendChart
                    points={data.published_trend}
                    emptyHint="该窗口内没有发布尝试记录。这里统计的是尝试次数，不是成功次数——失败的尝试同样会出现在图上。"
                  />
                </div>
              </div>
            </div>
          </section>

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">发布裁决队列</h2>
              {/* 直接落到"待裁决"筛选视图，而不是让人到了 Published 再自己挑一遍状态。 */}
              <Link to="/published?status=__unresolved__">
                {data.resolution_queue_total > data.resolution_queue.length
                  ? `查看全部 ${data.resolution_queue_total} 条 →`
                  : '打开发布记录 →'}
              </Link>
            </div>
            <div className="card card--flush">
              {data.resolution_queue.length === 0 ? (
                <EmptyState
                  title="没有需要裁决的发布"
                  hint="每一次发布尝试都有确定结果。这是你想要的状态。"
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
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {data.resolution_queue.map((item: ResolutionQueueItem) => (
                        <tr key={item.run_id}>
                          <td className="table__topic">
                            {item.topic ?? <span className="cell-muted">{EMPTY}</span>}
                          </td>
                          <td>
                            <StatusBadge status={item.publish.status} kind="publish" />
                          </td>
                          <td>
                            <StatusBadge status={item.run_status} kind="run" />
                          </td>
                          <td className="cell-muted clamp-2">{item.detail ?? EMPTY}</td>
                          <td>
                            <button
                              type="button"
                              className="btn btn--sm"
                              onClick={() => setOpenRunId(item.run_id)}
                            >
                              查看
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </section>

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">待审核队列</h2>
              <Link to="/review">
                {data.review_queue_total > data.review_queue.length
                  ? `查看全部 ${data.review_queue_total} 条 →`
                  : '打开审核工作台 →'}
              </Link>
            </div>
            <div className="card card--flush">
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
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {data.review_queue.map((item) => (
                        <tr key={item.run_id}>
                          <td className="table__topic">{item.topic}</td>
                          <td className="cell-muted clamp-2">{item.draft_preview ?? '未存草稿'}</td>
                          <td>
                            <GuardrailCell item={item} />
                          </td>
                          <td className="cell-muted cell-nowrap">
                            <span title={item.created_at}>{relativeTime(item.created_at)}</span>
                          </td>
                          <td>
                            <button
                              type="button"
                              className="btn btn--sm"
                              onClick={() => setOpenRunId(item.run_id)}
                            >
                              查看
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
            <p className="cell-muted">
              当结果无法从 checkpoint 读出时，规则检查一列显示 {EMPTY}。那表示<strong>未知</strong>，
              不等于通过。
            </p>
          </section>
        </>
      ) : null}

      {openRunId ? (
        <RunDetailDrawer runId={openRunId} onClose={() => setOpenRunId(null)} />
      ) : null}
    </>
  )
}
