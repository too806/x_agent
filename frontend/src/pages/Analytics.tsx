import { useState } from 'react'
import type { ReactNode } from 'react'

import { api } from '../api/client'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { TrendChart } from '../components/TrendChart'
import { IconRefresh } from '../components/ui/icons'
import { useAsync } from '../hooks/useAsync'
import { EMPTY, duration } from '../utils/format'
import type {
  AverageRunDuration,
  PublishOutcomeDistribution,
  ReviewDecisionDistribution,
  TopicCount,
} from '../types/api'

/**
 * 系统运行分析。
 *
 * 页面按**四层**组织，而不是把每个后端字段各开一节：
 *
 *   1. 核心数字 —— 四个能直接回答"最近怎么样"的数
 *   2. 趋势     —— 两张日粒度折线
 *   3. 分布     —— 三组构成（审核决定 / 发布结果 / 主题）
 *   4. 明细     —— 每一块的口径与缺口，一张表说清
 *
 * 之前是七个平级 section，其中四个各自只装一张卡。这种"一字段一节"的排法在视觉上
 * 把"窗口内 Run 数"和"平均运行时长"摆成同等重要的两件事，读者没有层级可循——正
 * 是"12 个半成品"的那种观感。现在只有四层，层内是同类的块。
 *
 * 三件贯穿全页的诚实性约束：
 *
 * - **不含任何 X 的内容表现指标**（impressions / likes / engagement）。系统没有回读
 *   X 的机制，那类数字在库里根本不存在，所以这里既不显示它们，也不显示 0——一张
 *   "阅读量为 0"的图会被读成"这篇没人看"，而事实是"我们不知道"。
 * - **算不出来的不给 0，也不给占位符。** 平均运行时长在样本不足时显示"暂无足够数据"
 *   并说明缺的是哪一类 Run。计数类的 0 是**真实测量值**（窗口内确实一条都没有），
 *   照常显示，但用弱化样式，免得一个 0 看起来像个成绩。
 * - **窗口内 vs 全库必须分开标注。** 后端只有 run_volume 与两张日趋势图带日期过滤，
 *   review / publish / duration / topic 四块都是全库统计（见 `app/services/admin.py`
 *   的 `analytics()`，那几个查询根本没传 date_from）。第 4 层的表逐行列了范围。
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

/**
 * 层内的一块。标题、hint、正文三件套统一，所以四层里的所有卡片长得一样。
 *
 * 不抽成全局 UI 组件：它的全部内容就是三个 class 名，别处也不长这个形状。
 */
function Block({ title, hint, children }: { title: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="card">
      <div className="card__body">
        <div className="block__head">
          <h3 className="card__title">{title}</h3>
          {hint ? <span className="card__hint">{hint}</span> : null}
        </div>
        {children}
      </div>
    </div>
  )
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
 * 一组计数。全为 0 时显示空态而不是几条零长度的横条——后者看起来像"有四个分类，
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
 * 一个计数型 KPI。
 *
 * 0 是**真实测量值**（这个窗口内确实一条都没有），不是缺失值，所以照常显示数字——
 * 但加 `kpi--quiet` 把它压暗：一个孤零零的 0 摆在最显眼的位置会看起来像个成绩或
 * 像个故障提示，而它两样都不是。
 */
function CountKpi({ label, value, sub }: { label: string; value: number; sub: string }) {
  return (
    <div className={value === 0 ? 'kpi kpi--quiet' : 'kpi'}>
      <div className="kpi__label">{label}</div>
      <div className="kpi__value">{value}</div>
      <div className="kpi__sub">{sub}</div>
    </div>
  )
}

/**
 * 平均运行时长。
 *
 * 它和上面三个计数不是同一类东西：分母不是"全部 Run"，而是"同时具备 started_at 与
 * finished_at 的 Run"。样本为空时**显示"暂无足够数据"而不是 0 秒** —— 0 秒会被读成
 * "跑得飞快"。样本不全时把缺口写在下面，因为拿它当全部 Run 的平均值会得出错误结论。
 */
function DurationKpi({ metric }: { metric: AverageRunDuration }) {
  const value = metric.available ? metric.average_seconds : null

  if (value === null) {
    return (
      <div className="kpi kpi--quiet">
        <div className="kpi__label">平均运行时长</div>
        <div className="kpi__value kpi__value--none">暂无足够数据</div>
        <div className="kpi__sub">还没有任何一个 Run 同时具备开始与完成时间</div>
      </div>
    )
  }

  const partial = metric.sampled_runs < metric.total_runs
  return (
    <div className={partial ? 'kpi kpi--attention' : 'kpi'}>
      <div className="kpi__label">平均运行时长</div>
      <div className="kpi__value">{duration(value)}</div>
      <div className="kpi__sub">
        取自 <strong>{metric.sampled_runs}</strong> 个 Run，共 {metric.total_runs} 个
      </div>
    </div>
  )
}

/** 明细表的一行。范围那一列是全页最要紧的信息，所以它单独成列而不是塞进说明里。 */
function ScopeRow({
  metric,
  scope,
  source,
  note,
}: {
  metric: string
  scope: ReactNode
  source: string
  note: ReactNode
}) {
  return (
    <tr>
      <td>{metric}</td>
      <td className="cell-nowrap">{scope}</td>
      <td className="cell-nowrap mono">{source}</td>
      <td className="cell-muted">{note}</td>
    </tr>
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

  const topicRows: Row[] =
    data?.topic_distribution.map((item: TopicCount) => ({
      key: item.topic,
      label: item.topic,
      count: item.count,
      tone: 'grey',
    })) ?? []

  return (
    <>
      <section className="section">
        <div className="section__head">
          <h2 className="section__title">系统活动</h2>
          <div className="row row--tight">
            {/* 窗口是这一页唯一的输入，放在页头而不是每块里各来一个：
                两个窗口控件迟早会不一致，而这张页面上所有数字都靠它限定范围。 */}
            <label className="cell-muted" htmlFor="analytics-window">
              时间窗口
            </label>
            <select
              id="analytics-window"
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
            <button type="button" className="btn btn--sm" onClick={reload} disabled={loading}>
              <IconRefresh size={14} />
              刷新
            </button>
          </div>
        </div>

        <p className="cell-muted scope-note">
          时间窗口<strong>只对第 1 层里的「窗口内 Run 数」和第 2 层的两张图生效</strong>。
          审核决定分布、发布结果分布、平均运行时长和主题分布都是全库统计 —— 后端对这几项
          没有日期过滤参数。每块的范围在第 4 层的表里逐行列着，别拿一张 7 天的图去解释一个
          含全部历史的分布。
        </p>

        {loading && !data ? <LoadingState message="正在加载数据分析…" variant="lines" /> : null}
        {error ? (
          <ErrorState error={error} what="加载数据分析失败。" onRetry={reload} />
        ) : null}
      </section>

      {data ? (
        <div className={loading ? 'is-loading' : undefined}>
          {/* ── 1. 核心数字 ─────────────────────────────────────────────── */}
          <section className="section">
            <div className="section__head">
              <h2 className="section__title">核心数字</h2>
            </div>
            <div className="kpi-grid">
              <CountKpi
                label="窗口内 Run 数"
                value={data.run_volume}
                sub={`最近 ${data.window_days} 天内创建的`}
              />
              <CountKpi
                label="审核决定数"
                value={decisionTotal}
                sub="全库 · 一次人工决定一行"
              />
              <CountKpi
                label="发布记录数"
                value={outcomeTotal}
                sub="全库 · 每个 Run 最多一行"
              />
              <DurationKpi metric={data.average_run_duration} />
            </div>

            {/* 平均时长的缺口单独说清楚：它是这一行里唯一一个分母不是"全部 Run"的数。 */}
            {data.average_run_duration.available &&
            data.average_run_duration.sampled_runs < data.average_run_duration.total_runs ? (
              <div className="notice notice--warn block-gap">
                <div className="notice__title">平均运行时长不是全部 Run 的平均</div>
                只有到终态的 Run 才同时有 <code>started_at</code> 和 <code>finished_at</code>
                ，也只有这些能被测量。仍在等待审核的、已批准或已编辑但还没发布的、以及卡在
                结果未定的发布里的 Run，<strong>都不在</strong>这个平均值里 —— 而它们通常正是
                耗时最长的那一批。
              </div>
            ) : null}
          </section>

          {/* ── 2. 趋势 ─────────────────────────────────────────────────── */}
          <section className="section">
            <div className="section__head">
              <h2 className="section__title">趋势</h2>
              <span className="section__hint">最近 {data.window_days} 天 · 按 UTC 自然日</span>
            </div>
            <div className="chart-grid">
              <Block title="每日运行量" hint="Run 被创建的时刻">
                <TrendChart
                  points={data.daily_runs}
                  emptyHint="该窗口内没有创建过 Run。可以把窗口放宽，看得更远一些。"
                />
              </Block>
              <Block title="每日发布量" hint="发布尝试开始的时刻">
                <TrendChart
                  points={data.daily_published}
                  emptyHint="该窗口内没有发布尝试记录。这里统计的是尝试次数，不是成功次数 —— 失败或结果未定的尝试同样会出现在图上。"
                />
              </Block>
            </div>
            <p className="cell-muted scope-note">
              发布尝试按 <code>published_post.created_at</code> 归入某一天，也就是尝试开始的
              时刻 —— 不是按 <code>published_at</code>，那个只有成功的行才有。用后者会让这张图
              比现实好看。
            </p>
          </section>

          {/* ── 3. 分布 ─────────────────────────────────────────────────── */}
          <section className="section">
            <div className="section__head">
              <h2 className="section__title">分布</h2>
              <span className="section__hint">全库 · 不受上面那个窗口影响</span>
            </div>
            <div className="chart-grid">
              <Block title="审核决策分布" hint={`${decisionTotal} 次决定`}>
                <Bars
                  rows={decisions}
                  empty={{
                    title: '还没有任何审核决定',
                    hint: '人工批准、编辑或拒绝一个 Run 之后，这里会出现一行记录。',
                  }}
                />
                {decisionTotal > 0 ? (
                  <p className="cell-muted scope-note">
                    每个 Run 只决定一次。<strong>其他</strong>收的是三种已知决定之外的记录 ——
                    实际上就是那些一直停在 <code>pending</code> 的审核行。把它显示出来而不是
                    丢掉，是为了四个数字加起来始终等于总数。
                  </p>
                ) : null}
              </Block>

              <Block title="发布结果分布" hint={`${outcomeTotal} 条发布记录`}>
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
                {outcomeTotal > 0 ? (
                  <p className="cell-muted scope-note">
                    计数按 <strong>Run</strong> 计（每个 Run 记它的当前结局），不是按尝试次数：
                    <code>published_post</code> 每个 Run 最多一行、重试会复用同一行，所以被
                    覆盖掉的那次失败不会出现在任何计数里。
                  </p>
                ) : null}
              </Block>
            </div>

            {/* 结果未定是这一页上最需要人动一下的数字。用橙色那一套（只在"不知道"
                这个语义上出现的颜色），并且把去处直接写出来。 */}
            {unresolved > 0 ? (
              <div className="notice notice--unknown block-gap">
                <div className="notice__title">
                  {unresolved} 次发布尝试的结果未定，需要人去裁决
                </div>
                它们的发布状态是 <code>publish_unknown</code> 或 <code>publishing</code>。X 上
                可能有这条帖子，也可能没有，所以它们<strong>既不算失败也不算成功</strong>，
                在下面两张图里也不会体现成成功。请到发布记录页查看并在 X 上确认。
              </div>
            ) : null}

            <div className="block-gap">
              <Block title="主题分布" hint="全库 · 按 Run 数取前 10">
                <Bars
                  rows={topicRows}
                  empty={{
                    title: '没有可排行的 Run',
                    hint: '至少要有一个 Run，才有东西可以排。',
                  }}
                />
                {data.topic_distribution.length >= 10 ? (
                  <p className="cell-muted scope-note">
                    只取使用最多的十个主题，所以这些计数加起来不等于 Run 总数。
                  </p>
                ) : null}
              </Block>
            </div>
          </section>

          {/* ── 4. 明细 ─────────────────────────────────────────────────── */}
          <section className="section">
            <div className="section__head">
              <h2 className="section__title">口径与数据缺口</h2>
              <span className="section__hint">上面每一个数字的来源与范围</span>
            </div>
            <div className="card card--flush">
              <div className="table-wrap">
                <table className="table table--compact">
                  <thead>
                    <tr>
                      <th>指标</th>
                      <th>时间范围</th>
                      <th>来源</th>
                      <th>口径说明</th>
                    </tr>
                  </thead>
                  <tbody>
                    <ScopeRow
                      metric="窗口内 Run 数"
                      scope={`最近 ${data.window_days} 天`}
                      source="agent_run.created_at"
                      note="按创建时刻归入窗口，不是按完成时刻。"
                    />
                    <ScopeRow
                      metric="每日运行量"
                      scope={`最近 ${data.window_days} 天`}
                      source="agent_run.created_at"
                      note="后端补齐成连续日期序列，没有活动的日子是 0，不是缺失。"
                    />
                    <ScopeRow
                      metric="每日发布量"
                      scope={`最近 ${data.window_days} 天`}
                      source="published_post.created_at"
                      note="尝试次数，含失败与结果未定的尝试。"
                    />
                    <ScopeRow
                      metric="审核决策分布"
                      scope="全库"
                      source="review"
                      note="后端对这张表没有日期过滤参数。每个 Run 只决定一次。"
                    />
                    <ScopeRow
                      metric="发布结果分布"
                      scope="全库"
                      source="published_post"
                      note="后端没有日期过滤参数。按 Run 记当前结局，不是按尝试次数。"
                    />
                    <ScopeRow
                      metric="平均运行时长"
                      scope={data.average_run_duration.available ? '全库' : EMPTY}
                      source="agent_run.started_at / finished_at"
                      note="只有两者俱全的终态 Run 参与，不是全部 Run 的平均。"
                    />
                    <ScopeRow
                      metric="主题分布"
                      scope="全库 · 前 10"
                      source="agent_run.topic"
                      note="后端没有日期过滤参数。计数之和 ≠ Run 总数。"
                    />
                    <ScopeRow
                      metric="X 内容指标"
                      scope={EMPTY}
                      source="未采集"
                      note={<strong>暂未接入 X 内容指标同步</strong>}
                    />
                  </tbody>
                </table>
              </div>
            </div>

            <div className="block-gap">
              {/* 后端若某天开始下发这些字段，这里不能继续硬说"从来没采集过"——
                  所以按 `available` 分支，而不是把结论写死在文案里。 */}
              {data.x_content_performance.available ? (
                <div className="notice">
                  <div className="notice__title">X 内容指标：接口报告可用，但本页仍不显示</div>
                  后端的可用性标记是 true，但当前的分析响应里没有逐条帖子的指标字段，所以
                  这个控制台拿不到曝光 / 点赞 / 转发 / 收藏 / 互动率的数值。要显示它们，需要
                  后端先下发这些字段 —— 在拿到之前这里<strong>不会</strong>用任何近似值填充。
                </div>
              ) : (
                <NotAvailable
                  what={
                    <>
                      <strong>暂未接入 X 内容指标同步</strong>
                      ：曝光、点赞、回复、转发、收藏和互动率在本控制台里
                      <strong>一律不显示</strong>。它们不是查询失败才拿不到 —— 是这些数据从来
                      没有被采集过。
                    </>
                  }
                />
              )}
            </div>
          </section>
        </div>
      ) : null}
    </>
  )
}
