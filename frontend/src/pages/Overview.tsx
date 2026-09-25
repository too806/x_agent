import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { RunDetailDrawer } from '../components/RunDetailDrawer'
import { ErrorState, EmptyState, LoadingState } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { TrendChart } from '../components/TrendChart'
import { useAsync } from '../hooks/useAsync'
import { EMPTY, relativeTime } from '../utils/format'
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
      <span className="guard guard--unknown" title="Not readable from the checkpoint">
        {EMPTY}
      </span>
    )
  }
  if (item.guardrail_passed) {
    return <span className="guard guard--pass">PASS</span>
  }
  return (
    <span className="guard guard--fail" title={item.guardrail_reasons.join('\n')}>
      FAIL{item.guardrail_reasons.length ? ` (${item.guardrail_reasons.length})` : ''}
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
          <h2 className="section__title">System snapshot</h2>
          <div className="row">
            <label className="cell-muted" htmlFor="overview-window">
              Window
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
                  Last {value} days
                </option>
              ))}
            </select>
            <button type="button" className="btn btn--sm" onClick={reload} disabled={loading}>
              Refresh
            </button>
          </div>
        </div>

        {loading && !data ? <LoadingState message="Loading overview…" /> : null}
        {error ? <ErrorState error={error} what="Failed to load the overview." onRetry={reload} /> : null}
      </section>

      {data ? (
        <>
          {/* 待裁决队列排在最前：它的存在意味着一条推文可能已经发出去了却没人知道，
              这是整个控制台里唯一"不管就会更糟"的状态。 */}
          {data.resolution_queue_total > 0 ? (
            <div className="notice notice--warn" style={{ marginBottom: 20 }}>
              <div className="notice__title">
                {data.resolution_queue_total} publish attempt
                {data.resolution_queue_total === 1 ? '' : 's'} need a human decision
              </div>
              These are in <code>publishing</code> or <code>publish_unknown</code>. No automatic
              path will call X again for them — resolving one is a manual call, and it is not
              reversible.
            </div>
          ) : null}

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">Key figures</h2>
              <span className="section__hint">all-time counts, not limited to the window</span>
            </div>

            <div className="kpi-grid">
              <div className="kpi">
                <div className="kpi__label">Total runs</div>
                <div className="kpi__value">{data.kpis.total_runs}</div>
                <div className="kpi__sub">every run ever recorded</div>
              </div>
              <div className="kpi kpi--attention">
                <div className="kpi__label">Awaiting review</div>
                <div className="kpi__value">{data.kpis.waiting_for_review}</div>
                <div className="kpi__sub">sitting in the queue right now</div>
              </div>
              <div className="kpi">
                <div className="kpi__label">Published</div>
                <div className="kpi__value">{data.kpis.published}</div>
                <div className="kpi__sub">confirmed on X</div>
              </div>
              <div className="kpi">
                <div className="kpi__label">Publish failed</div>
                <div className="kpi__value">{data.kpis.publish_failed}</div>
                <div className="kpi__sub">known failure, safe to retry</div>
              </div>
              <div className="kpi kpi--critical">
                <div className="kpi__label">Publish unknown</div>
                <div className="kpi__value">{data.kpis.publish_unknown}</div>
                <div className="kpi__sub">may or may not be live</div>
              </div>
              <div className="kpi kpi--attention">
                <div className="kpi__label">Publishing</div>
                <div className="kpi__value">{data.kpis.publishing}</div>
                <div className="kpi__sub">attempt started, outcome not recorded</div>
              </div>
            </div>

            <p className="cell-muted" style={{ marginBottom: 0 }}>
              The last four come from publish records, not from run status. A run can sit at{' '}
              <code>approved</code> while its publish record is <code>failed</code> — the publish
              record is the one telling the truth.
            </p>
          </section>

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">Daily activity</h2>
              <span className="section__hint">last {data.window_days} days · UTC days</span>
            </div>
            <div className="chart-grid">
              <div className="card">
                <div className="card__body">
                  <div className="section__title">Runs created</div>
                  <TrendChart
                    points={data.runs_trend}
                    emptyHint="No runs were created in this window. Widen the window to look further back."
                  />
                </div>
              </div>
              <div className="card">
                <div className="card__body">
                  <div className="section__title">Publish records created</div>
                  <TrendChart
                    points={data.published_trend}
                    emptyHint="No publish attempts were recorded in this window. This counts attempts, not successes — a failed attempt still appears here."
                  />
                </div>
              </div>
            </div>
          </section>

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">Publish resolution queue</h2>
              {/* 直接落到"待裁决"筛选视图，而不是让人到了 Published 再自己挑一遍状态。 */}
              <Link to="/published?status=__unresolved__">
                {data.resolution_queue_total > data.resolution_queue.length
                  ? `View all ${data.resolution_queue_total} →`
                  : 'Open Published →'}
              </Link>
            </div>
            <div className="card card--flush">
              {data.resolution_queue.length === 0 ? (
                <EmptyState
                  title="Nothing to resolve"
                  hint="Every publish attempt has a definite outcome. This is the state you want."
                />
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Topic</th>
                        <th>Publish</th>
                        <th>Run status</th>
                        <th>Last error</th>
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
                              Open
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
              <h2 className="section__title">Review queue</h2>
              <Link to="/review">
                {data.review_queue_total > data.review_queue.length
                  ? `View all ${data.review_queue_total} →`
                  : 'Open Review →'}
              </Link>
            </div>
            <div className="card card--flush">
              {data.review_queue.length === 0 ? (
                <EmptyState
                  title="No runs waiting for review"
                  hint="New runs will appear here as soon as the agent pauses for a human decision."
                />
              ) : (
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Topic</th>
                        <th>Draft preview</th>
                        <th>Guardrail</th>
                        <th>Waiting</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {data.review_queue.map((item) => (
                        <tr key={item.run_id}>
                          <td className="table__topic">{item.topic}</td>
                          <td className="cell-muted clamp-2">
                            {item.draft_preview ?? 'no draft stored'}
                          </td>
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
                              Open
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
              Guardrail shows {EMPTY} when the result cannot be read from the checkpoint. That means
              unknown, not passed.
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
