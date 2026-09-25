import { useState } from 'react'
import type { ReactNode } from 'react'

import { api } from '../api/client'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { TrendChart } from '../components/TrendChart'
import { useAsync } from '../hooks/useAsync'
import { duration } from '../utils/format'
import type {
  AverageRunDuration,
  PublishOutcomeDistribution,
  ReviewDecisionDistribution,
  TopicCount,
} from '../types/api'

/**
 * 系统运行分析。
 *
 * 这个页面**不含任何 X 的内容表现指标**（impressions / likes / engagement）。
 * 系统没有回读 X 的机制，那类数字在库里根本不存在，所以这里既不显示它们，
 * 也不显示 0——一张"阅读量为 0"的图会被读成"这篇没人看"，而事实是"我们不知道"。
 * 相关的数据缺口在页面末尾单独列成一栏，把"缺什么、为什么缺"说清楚。
 *
 * 第二个贯穿全页的口径问题：**窗口内 vs 全量**。后端只有 run_volume 与两张日
 * 趋势图带日期过滤，review / publish / duration / topic 四块都是全库统计
 * （见 `app/services/admin.py` 的 `analytics()`，那几个查询根本没传 date_from）。
 * 与其在界面上把它们笼统地放进"最近 N 天"，不如每块自带范围标注。混在一起报，
 * 使用者会拿一个含全部历史的分布去解释一个只覆盖 7 天的趋势。
 */

const WINDOWS = [7, 14, 30, 90]

/** 条形色。用色名而不是语义名：这里只管画一道横条，语义由调用方的映射决定。 */
type Tone = 'green' | 'red' | 'orange' | 'violet' | 'grey'

interface Row {
  key: string
  /** 徽标里的展示文案。 */
  label: string
  count: number
  tone: Tone
  /** 有状态语义的行给它一个徽标，纯文本行（话题）不给。 */
  kind?: 'run' | 'review' | 'publish'
}

function BarRow({
  label,
  count,
  max,
  tone,
  kind,
}: {
  label: string
  count: number
  max: number
  tone: Tone
  kind?: 'run' | 'review' | 'publish'
}) {
  // 非零值至少给 2% 宽度：一条 0.5% 的条在屏幕上等于不存在，会让人以为计数是 0。
  const pct = max > 0 ? Math.max((count / max) * 100, count > 0 ? 2 : 0) : 0
  return (
    <div className="bar-row">
      <div className="bar-row__label" title={kind ? undefined : label}>
        {kind ? <StatusBadge status={label} kind={kind} /> : label}
      </div>
      <div className="bar">
        <div className={`bar__fill bar__fill--${tone}`} style={{ width: `${pct}%` }} />
      </div>
      <div className="bar-row__value num">{count}</div>
    </div>
  )
}

/**
 * 一组计数。全为 0 时显示空态而不是四条零长度的横条——后者看起来像"有四个分类，
 * 都很低"，而不是"什么记录都还没有"。
 */
function Bars({ rows, empty }: { rows: Row[]; empty: { title: string; hint: string } }) {
  const total = rows.reduce((sum, row) => sum + row.count, 0)
  if (total === 0) {
    return <EmptyState title={empty.title} hint={empty.hint} />
  }
  const max = Math.max(...rows.map((row) => row.count))
  return (
    <div className="bars">
      {rows.map((row) => (
        <BarRow
          key={row.key}
          label={row.label}
          count={row.count}
          max={max}
          tone={row.tone}
          kind={row.kind}
        />
      ))}
    </div>
  )
}

/** 明确"算不出来"。**不给 0，也不给一个空占位符**——两者都会被当成一个数值读。 */
function NotAvailable({ what, reason }: { what: ReactNode; reason: string | null }) {
  return (
    <div className="na">
      <div className="na__title">Not available</div>
      <div>{what}</div>
      {reason ? <div className="cell-muted">{reason}</div> : null}
    </div>
  )
}

function reviewRows(counts: ReviewDecisionDistribution): Row[] {
  return [
    { key: 'approve', label: 'approve', kind: 'review', count: counts.approve, tone: 'green' },
    { key: 'edit', label: 'edit', kind: 'review', count: counts.edit, tone: 'violet' },
    { key: 'reject', label: 'reject', kind: 'review', count: counts.reject, tone: 'grey' },
    { key: 'other', label: 'other', kind: 'review', count: counts.other, tone: 'grey' },
  ]
}

function publishRows(counts: PublishOutcomeDistribution): Row[] {
  return [
    {
      key: 'published',
      label: 'published',
      kind: 'publish',
      count: counts.published,
      tone: 'green',
    },
    { key: 'failed', label: 'failed', kind: 'publish', count: counts.failed, tone: 'red' },
    {
      key: 'publish_unknown',
      label: 'publish_unknown',
      kind: 'publish',
      count: counts.publish_unknown,
      tone: 'orange',
    },
    {
      key: 'publishing',
      label: 'publishing',
      kind: 'publish',
      count: counts.publishing,
      tone: 'violet',
    },
  ]
}

function DurationBlock({ metric }: { metric: AverageRunDuration }) {
  if (!metric.available || metric.average_seconds === null) {
    return <NotAvailable what="Average Run duration" reason={metric.reason} />
  }
  const partial = metric.sampled_runs < metric.total_runs
  return (
    <div className="stack stack--tight">
      <div className="kpi__value">{duration(metric.average_seconds)}</div>
      <div className="kpi__sub">
        averaged over <strong>{metric.sampled_runs}</strong> of {metric.total_runs} runs
      </div>
      {partial ? (
        <div className="notice notice--warn">
          Only Runs that reached a terminal state have both <code>started_at</code> and{' '}
          <code>finished_at</code>, and only those can be measured. Runs still waiting for review,
          approved or edited but not yet published, and Runs stuck in an unresolved publish are{' '}
          <strong>not</strong> in this average — it is not the average over all{' '}
          {metric.total_runs} Runs.
        </div>
      ) : null}
    </div>
  )
}

function ScopeNote() {
  return (
    <div className="notice notice--info" style={{ marginBottom: 20 }}>
      <div className="notice__title">What the window applies to</div>
      Only the two charts and &ldquo;Runs in window&rdquo; are filtered by the selected window. The
      review decisions, publish outcomes, average duration and topic ranking are computed over{' '}
      <strong>every record in the database</strong> — the backend has no date filter for them. Each
      block is labelled with its own scope so a 7-day chart is never read together with an all-time
      distribution.
    </div>
  )
}

export function Analytics() {
  const [days, setDays] = useState(7)
  const { data, loading, error, reload } = useAsync(() => api.getAnalytics(days), [days])

  const decisions = data ? reviewRows(data.review_decisions) : []
  const outcomes = data ? publishRows(data.publish_outcomes) : []
  const decisionTotal = decisions.reduce((sum, row) => sum + row.count, 0)
  const outcomeTotal = outcomes.reduce((sum, row) => sum + row.count, 0)
  const unresolved =
    data ? data.publish_outcomes.publish_unknown + data.publish_outcomes.publishing : 0

  return (
    <>
      <section className="section">
        <div className="section__head">
          <h2 className="section__title">System activity</h2>
          <div className="row">
            <label className="cell-muted" htmlFor="analytics-window">
              Window
            </label>
            <select
              id="analytics-window"
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

        {loading && !data ? <LoadingState message="Loading analytics…" /> : null}
        {error ? (
          <ErrorState error={error} what="Failed to load analytics." onRetry={reload} />
        ) : null}
      </section>

      {data ? (
        <>
          <ScopeNote />

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">Totals</h2>
            </div>
            <div className="kpi-grid">
              <div className="kpi">
                <div className="kpi__label">Runs in window</div>
                <div className="kpi__value">{data.run_volume}</div>
                <div className="kpi__sub">created in the last {data.window_days} days</div>
              </div>
              <div className="kpi">
                <div className="kpi__label">Review decisions</div>
                <div className="kpi__value">{decisionTotal}</div>
                <div className="kpi__sub">all time · one row per human decision</div>
              </div>
              <div className="kpi">
                <div className="kpi__label">Publish records</div>
                <div className="kpi__value">{outcomeTotal}</div>
                <div className="kpi__sub">all time · at most one row per run</div>
              </div>
            </div>
          </section>

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">Daily activity</h2>
              <span className="section__hint">
                last {data.window_days} days · UTC calendar days
              </span>
            </div>
            <div className="chart-grid">
              <div className="card">
                <div className="card__body">
                  <div className="section__title">Runs created</div>
                  <TrendChart
                    points={data.daily_runs}
                    emptyHint="No Runs were created in this window. Widen the window to look further back."
                  />
                </div>
              </div>
              <div className="card">
                <div className="card__body">
                  <div className="section__title">Publish attempts</div>
                  <TrendChart
                    points={data.daily_published}
                    emptyHint="No publish attempt was recorded in this window. This counts attempts, not successes — a failed or unresolved attempt still appears here."
                  />
                </div>
              </div>
            </div>
            <p className="cell-muted">
              Attempts are bucketed by <code>published_post.created_at</code>, the moment the attempt
              started — not by <code>published_at</code>, which only the successful rows have. Using
              the latter would make the chart look more optimistic than reality.
            </p>
          </section>

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">Review decisions</h2>
              <span className="section__hint">all time</span>
            </div>
            <div className="card">
              <div className="card__body">
                <Bars
                  rows={decisions}
                  empty={{
                    title: 'No review decision has been recorded',
                    hint: 'A row appears here once a human approves, edits or rejects a Run.',
                  }}
                />
                {decisionTotal > 0 ? (
                  <p className="cell-muted" style={{ marginBottom: 0 }}>
                    Decide once per Run. <strong>other</strong> collects anything outside the three
                    known decisions — in practice a review row that never progressed past{' '}
                    <code>pending</code>. It is shown rather than dropped so the four numbers always
                    add up to the total.
                  </p>
                ) : null}
              </div>
            </div>
          </section>

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">Publish outcomes</h2>
              <span className="section__hint">all time</span>
            </div>
            <div className="card">
              <div className="card__body">
                <Bars
                  rows={outcomes}
                  empty={{
                    title: 'No publish attempt has been recorded',
                    hint: 'Nothing has been sent to X yet, or every attempt predates this console.',
                  }}
                />
                {/* 口径说明逐字来自后端：它讲的是这份数据本身怎么算出来的，
                    不是前端能替它概括的东西。 */}
                <p className="cell-muted" style={{ marginBottom: 0 }}>
                  {data.publish_outcomes.note}
                </p>
              </div>
            </div>
            {unresolved > 0 ? (
              <div className="notice notice--warn" style={{ marginTop: 12 }}>
                <div className="notice__title">
                  {unresolved} publish attempt{unresolved === 1 ? '' : 's'} with no known outcome
                </div>
                These are <code>publish_unknown</code> or <code>publishing</code>. A post may or may
                not exist on X, so they are not counted as failures and not counted as successes.
                They need a human decision on the Published page.
              </div>
            ) : null}
          </section>

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">Average Run duration</h2>
              <span className="section__hint">all time</span>
            </div>
            <div className="card">
              <div className="card__body">
                <DurationBlock metric={data.average_run_duration} />
              </div>
            </div>
          </section>

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">Most used topics</h2>
              <span className="section__hint">all time · top 10 by Run count</span>
            </div>
            <div className="card">
              <div className="card__body">
                <Bars
                  rows={data.topic_distribution.map((item: TopicCount): Row => ({
                    key: item.topic,
                    label: item.topic,
                    count: item.count,
                    tone: 'grey',
                  }))}
                  empty={{
                    title: 'No Runs recorded',
                    hint: 'There is nothing to rank until at least one Run exists.',
                  }}
                />
                {data.topic_distribution.length >= 10 ? (
                  <p className="cell-muted" style={{ marginBottom: 0 }}>
                    Capped at the ten most used topics, so these counts do not add up to the total
                    number of Runs.
                  </p>
                ) : null}
              </div>
            </div>
          </section>

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">X content performance</h2>
            </div>
            <div className="card">
              <div className="card__body">
                <NotAvailable
                  what={
                    <>
                      Impressions, likes, replies, reposts, bookmarks and engagement rate are{' '}
                      <strong>not shown anywhere in this console</strong>. They are not unavailable
                      because a query failed — the data was never collected.
                    </>
                  }
                  reason={data.x_content_performance.reason}
                />
              </div>
            </div>
          </section>
        </>
      ) : null}
    </>
  )
}
