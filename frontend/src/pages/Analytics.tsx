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
 *
 * 口径说明（`publish_outcomes.note` / `*_reason`）后端下发的是**英文常量**，而且被
 * `tests/test_admin_api.py` 断言着，所以后端一个字节都没动。界面上的中文说明是这里
 * 按同一份语义写的，不是翻译腔的逐字转写。改后端那几条常量之前，先看那三条断言。
 */

const WINDOWS = [7, 14, 30, 90]

/** 条形色。用色名而不是语义名：这里只管画一道横条，语义由调用方的映射决定。 */
type Tone = 'green' | 'red' | 'orange' | 'violet' | 'grey'

interface Row {
  key: string
  /**
   * **徽标里的原始枚举值**，不是展示文案。
   *
   * 它被当作 `StatusBadge` 的 `status` 传下去，由 `utils/format.ts` 的三个映射表
   * 翻成中文（approve → 批准、publish_unknown → 发布结果未知）。这里**不能**改成
   * 中文：改了就查不到映射，徽标会显示成兜底的大写原文。
   */
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
function NotAvailable({ what, reason }: { what: ReactNode; reason?: ReactNode }) {
  return (
    <div className="na">
      <div className="na__title">暂不可用</div>
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

/**
 * 平均运行时长。
 *
 * `metric.reason` 后端下发的是英文常量（被测试断言着），所以这里不直接渲染它——
 * 不可用的原因只有一种，就是把后端那句话按同样的语义写成中文。少渲染一句话，
 * 换掉的是页面上唯一一段英文。
 */
function DurationBlock({ metric }: { metric: AverageRunDuration }) {
  if (!metric.available || metric.average_seconds === null) {
    return (
      <NotAvailable
        what="平均运行时长"
        reason="还没有任何一个终态 Run 同时具备 started_at 和 finished_at。只有终态（已发布 / 已拒绝 / 失败 / 已完成）才会写 finished_at；已批准、已编辑、待审核以及结果未定的发布都刻意留空。"
      />
    )
  }
  const partial = metric.sampled_runs < metric.total_runs
  return (
    <div className="stack stack--tight">
      <div className="kpi__value">{duration(metric.average_seconds)}</div>
      <div className="kpi__sub">
        取自 <strong>{metric.sampled_runs}</strong> 个 Run，共 {metric.total_runs} 个
      </div>
      {partial ? (
        <div className="notice notice--warn">
          只有到达终态的 Run 才同时有 <code>started_at</code> 和 <code>finished_at</code>
          ，也只有这些能被测量。仍在等待审核的、已批准或已编辑但还没发布的、以及卡在结果未定
          的发布里的 Run，<strong>都不在</strong>这个平均值里 —— 它不是全部{' '}
          {metric.total_runs} 个 Run 的平均。
        </div>
      ) : null}
    </div>
  )
}

function ScopeNote() {
  return (
    <div className="notice notice--info" style={{ marginBottom: 20 }}>
      <div className="notice__title">窗口对哪些区块生效</div>
      只有两张图和「窗口内 Run 数」会被上方选定的时间窗口过滤。审核决定分布、发布结果分布、
      平均运行时长和主题排行都是<strong>全库统计</strong> —— 后端对这几项没有日期过滤参数。
      每个区块都标了自己的范围，免得用一张 7 天的图去解释一个含全部历史的分布。
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
          <h2 className="section__title">系统活动</h2>
          <div className="row">
            <label className="cell-muted" htmlFor="analytics-window">
              时间窗口
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
                  最近 {value} 天
                </option>
              ))}
            </select>
            <button type="button" className="btn btn--sm" onClick={reload} disabled={loading}>
              刷新
            </button>
          </div>
        </div>

        {loading && !data ? <LoadingState message="正在加载数据分析…" /> : null}
        {error ? (
          <ErrorState error={error} what="加载数据分析失败。" onRetry={reload} />
        ) : null}
      </section>

      {data ? (
        <>
          <ScopeNote />

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">总量</h2>
            </div>
            <div className="kpi-grid">
              <div className="kpi">
                <div className="kpi__label">窗口内 Run 数</div>
                <div className="kpi__value">{data.run_volume}</div>
                <div className="kpi__sub">最近 {data.window_days} 天内创建的</div>
              </div>
              <div className="kpi">
                <div className="kpi__label">审核决定数</div>
                <div className="kpi__value">{decisionTotal}</div>
                <div className="kpi__sub">全库 · 一次人工决定一行</div>
              </div>
              <div className="kpi">
                <div className="kpi__label">发布记录数</div>
                <div className="kpi__value">{outcomeTotal}</div>
                <div className="kpi__sub">全库 · 每个 Run 最多一行</div>
              </div>
            </div>
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
                    points={data.daily_runs}
                    emptyHint="该窗口内没有创建过 Run。可以把窗口放宽，看得更远一些。"
                  />
                </div>
              </div>
              <div className="card">
                <div className="card__body">
                  <div className="section__title">每日发布量</div>
                  <TrendChart
                    points={data.daily_published}
                    emptyHint="该窗口内没有发布尝试记录。这里统计的是尝试次数，不是成功次数 —— 失败或结果未定的尝试同样会出现在图上。"
                  />
                </div>
              </div>
            </div>
            <p className="cell-muted">
              尝试按 <code>published_post.created_at</code> 归入某一天，也就是尝试开始的时刻 ——
              不是按 <code>published_at</code>，那个只有成功的行才有。用后者会让这张图比现实好看。
            </p>
          </section>

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">审核决策分布</h2>
              <span className="section__hint">全库</span>
            </div>
            <div className="card">
              <div className="card__body">
                <Bars
                  rows={decisions}
                  empty={{
                    title: '还没有任何审核决定',
                    hint: '人工批准、编辑或拒绝一个 Run 之后，这里会出现一行记录。',
                  }}
                />
                {decisionTotal > 0 ? (
                  <p className="cell-muted" style={{ marginBottom: 0 }}>
                    每个 Run 只决定一次。<strong>其他</strong>收的是三种已知决定之外的记录 ——
                    实际上就是那些一直停在 <code>pending</code> 的审核行。把它显示出来而不是丢掉，
                    是为了四个数字加起来始终等于总数。
                  </p>
                ) : null}
              </div>
            </div>
          </section>

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">发布结果分布</h2>
              <span className="section__hint">全库</span>
            </div>
            <div className="card">
              <div className="card__body">
                <Bars
                  rows={outcomes}
                  empty={{
                    title: '还没有任何发布尝试',
                    hint: '要么还没有内容发到 X，要么所有尝试都发生在这个控制台上线之前。',
                  }}
                />
                {/* 口径说明对应后端下发的 `publish_outcomes.note`（英文常量，被
                    tests/test_admin_api.py 断言着）。这里按同一份语义用中文重写，
                    不是逐字翻译：它讲的是这份数据本身怎么算出来的。 */}
                <p className="cell-muted" style={{ marginBottom: 0 }}>
                  计数按 <strong>Run</strong> 计（每个 Run 记它的当前结局），不是按尝试次数：
                  <code>published_post</code> 每个 Run 最多一行、重试会复用同一行，所以被覆盖掉的
                  那次失败不会出现在任何计数里。
                </p>
              </div>
            </div>
            {unresolved > 0 ? (
              <div className="notice notice--warn" style={{ marginTop: 12 }}>
                <div className="notice__title">
                  {unresolved} 次发布尝试的结果未定
                </div>
                它们的发布状态是 <code>publish_unknown</code> 或 <code>publishing</code>。X 上可能
                有这条帖子，也可能没有，所以它们既不算失败也不算成功。需要人到发布记录页裁决。
              </div>
            ) : null}
          </section>

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">平均运行时长</h2>
              <span className="section__hint">全库</span>
            </div>
            <div className="card">
              <div className="card__body">
                <DurationBlock metric={data.average_run_duration} />
              </div>
            </div>
          </section>

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">主题分布</h2>
              <span className="section__hint">全库 · 按 Run 数取前 10</span>
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
                    title: '没有可排行的 Run',
                    hint: '至少要有一个 Run，才有东西可以排。',
                  }}
                />
                {data.topic_distribution.length >= 10 ? (
                  <p className="cell-muted" style={{ marginBottom: 0 }}>
                    只取使用最多的十个主题，所以这些计数加起来不等于 Run 总数。
                  </p>
                ) : null}
              </div>
            </div>
          </section>

          <section className="section">
            <div className="section__head">
              <h2 className="section__title">X 内容指标</h2>
            </div>
            <div className="card">
              <div className="card__body">
                <NotAvailable
                  what={
                    <>
                      <strong>暂未接入 X 内容指标同步</strong>
                      ：曝光、点赞、回复、转发、收藏和互动率在本控制台里
                      <strong>一律不显示</strong>。它们不是查询失败才拿不到 —— 是这些数据从来没有被采集过。
                    </>
                  }
                />
              </div>
            </div>
          </section>
        </>
      ) : null}
    </>
  )
}
