import { useState } from 'react'
import type { ReactNode } from 'react'

import { api, ApiError } from '../api/client'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { TrendChart } from '../components/TrendChart'
import { IconRefresh } from '../components/ui/icons'
import { useAsync } from '../hooks/useAsync'
import { FEEDBACK_DISCLAIMER, signalHeadline } from '../utils/feedback'
import { EMPTY, dateTime, duration } from '../utils/format'
import {
  LEARNING_DISCLAIMER,
  comparisonHeadline,
  comparisonStatement,
  evidenceHeadline,
  invalidComparisonLine,
  limitationLines,
  readinessHeadline,
} from '../utils/learning'
import {
  PROPOSALS_DISCLAIMER,
  REVIEW_ACTION_LABELS,
  changeLine,
  evidenceLine,
  isReviewable,
  isStale,
  proposalHeadline,
  proposalLimitations,
  proposalsHeadline,
  reviewOutcomeLine,
  validationLine,
  versionLine,
} from '../utils/proposals'
import type {
  AverageRunDuration,
  FeedbackOverview,
  LearningOverview,
  LearningProposalRow,
  ProposalsOverview,
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
 * - **上面四层不含任何 X 的内容表现指标**（impressions / likes / engagement）。
 *   Phase 5B.4 起系统确实有了回读机制，但那是**每条帖子**的公开计数、由脚本手工采集，
 *   与这一页"系统自己跑了多少"不是同一种东西，所以单独成节（「发布内容的反馈」）
 *   而不是并进这些按 Run 计数的图里。曝光与书签仍然采不到——v1 只读公开计数，
 *   它们一律不显示，也不显示 0。
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

/**
 * 自己过去发布内容的反馈（Phase 5B.4）。
 *
 * 这是这一页上**唯一**一块"关于我们自己发出去的东西"的数据，所以它与上面所有
 * 块都不同：上面回答"系统跑了多少"，这一块回答"发出去的东西后来怎么样了"。
 *
 * 三种状态必须长得不一样，否则读的人会把它们混成一种：
 *
 *   1. **请求失败** —— 这是"这一块没拿到"，与下面两种都不同。绝不能显示成
 *      `available=false` 的那句"还没有采集过"：那会把一次加载失败说成一个
 *      关于数据的结论。（`useAsync` 的错误与 `data` 是分开的两个值。）
 *   2. `available=false` —— 确实一条快照都没有，原因由后端下发。
 *   3. 有数据 —— 样本量、信号、缺口。
 */
function FeedbackBlock({
  data,
  loading,
  error,
  onRetry,
}: {
  data: FeedbackOverview | null
  loading: boolean
  error: ApiError | null
  onRetry: () => void
}) {
  if (error && !data) {
    return (
      <ErrorState error={error} what="加载反馈数据失败。" onRetry={onRetry} />
    )
  }
  if (!data) {
    return loading ? <LoadingState message="正在加载反馈…" /> : null
  }

  if (!data.available) {
    return (
      <NotAvailable
        what={
          <>
            <strong>还没有采集过任何发布结果</strong>
            ：本系统对自己发出去的帖子表现如何<strong>一无所知</strong>，所以这里不显示
            任何计数——不是显示 0。0 会被读成"发了，但没人互动"。
          </>
        }
        reason={data.reason}
      />
    )
  }

  return (
    <div className="stack">
      <div className="kpi-grid">
        <CountKpi label="观察条数" value={data.sample_count} sub="一条快照 = 一次观察，不是一条帖子" />
        <CountKpi label="涉及帖子" value={data.post_count} sub="有观察记录的已发布帖子" />
        <CountKpi label="依据快照" value={data.snapshot_count} sub="采到并落库的原始读数" />
        <div className="kpi kpi--quiet">
          <div className="kpi__label">最近一次采集</div>
          <div className="kpi__value kpi__value--none">{dateTime(data.latest_observed_at)}</div>
          {/* 采集是**手工**触发的，所以"这份数据有多新"是这一块最要紧的元信息之一：
              一个三天前的数字与一个三分钟前的数字，读法完全不同。 */}
          <div className="kpi__sub">采集由脚本手工触发，没有定时任务</div>
        </div>
      </div>

      {data.signals.length > 0 ? (
        <Block title="观察到的信号" hint={`${data.signals.length} 条 · v1 只描述分布，不做比较`}>
          <ul className="stack stack--tight">
            {data.signals.map((signal, index) => (
              <li key={index}>
                {/* 样本量跟每一条结论一起出现：没有 n 的结论没法被判断为"样本太小"。 */}
                <div className="meta__label">{signalHeadline(signal)}</div>
                <div>{signal.statement}</div>
              </li>
            ))}
          </ul>
        </Block>
      ) : null}

      {data.limitations.length > 0 ? (
        <Block title="这份反馈没有回答什么">
          <ul className="stack stack--tight">
            {data.limitations.map((note, index) => (
              <li key={index} className="cell-muted">
                {note}
              </li>
            ))}
          </ul>
        </Block>
      ) : null}

      <p className="cell-muted scope-note">{FEEDBACK_DISCLAIMER}</p>
    </div>
  )
}

/**
 * 反馈够不够可靠，可以作为未来学习材料（Phase 5C）。
 *
 * 这是这一页上**唯一**一块不是"观察了什么"、而是"这些观察够不够格"的内容，
 * 所以它与 `FeedbackBlock` 并排但分开显示：上面那块答"看到了什么"，
 * 这一块答"看到的这些，敢不敢拿去做未来的判断"。
 *
 * 刻意极简：状态 / 样本量 / 窗口 / 原因 / 成立的比较 / 证据。**没有图表**——
 * 一个折线图会让 n=1 的数据看起来像一条趋势线，而这一层存在的意义恰恰是
 * 在数据不够时明确地说"不够"。
 *
 * 三个状态必须长得不一样（同 `FeedbackBlock`）：请求失败 / 没有已发布内容 /
 * 有数据。把"加载失败"显示成"还没有内容"会把一次故障说成一个关于数据的结论。
 */
function LearningBlock({
  data,
  loading,
  error,
  onRetry,
}: {
  data: LearningOverview | null
  loading: boolean
  error: ApiError | null
  onRetry: () => void
}) {
  if (error && !data) {
    return <ErrorState error={error} what="加载学习准备度失败。" onRetry={onRetry} />
  }
  if (!data) {
    return loading ? <LoadingState message="正在加载学习准备度…" /> : null
  }

  if (!data.available) {
    return (
      <NotAvailable
        what={
          <>
            <strong>还没有任何已发布内容可供观察</strong>
            ：学习准备度回答的是"过去的反馈够不够可靠"，一条帖子都没有时这个问题
            无从谈起。这里<strong>不会</strong>用 0 或"尚未评估"之类的字样顶上——
            那看起来像评估过了、只是没结果。
          </>
        }
        reason={data.reason}
      />
    )
  }

  const ready = data.readiness.learning_ready
  const validComparisons = data.comparisons.filter((item) => item.valid)

  return (
    <div className="stack">
      <div className={ready ? 'notice' : 'notice notice--warn'}>
        <div className="notice__title">
          {ready ? '这些反馈已可作为学习材料' : '这些反馈还不足以作为学习材料'}
        </div>
        {readinessHeadline(data)}
        {/* 这一行是这一阶段的核心承诺，所以它单独显示，而不是藏在说明里。 */}
        <div className="cell-muted">
          本层<strong>不会</strong>自动修改 Prompt、写作结构、观点规则、阈值或预算：
          {data.readiness.automatic_modification ? '（后端报告了 automatic_modification=true，这是异常）' : '自动修改恒为关闭。'}
        </div>
      </div>

      <div className="kpi-grid">
        <CountKpi
          label="有读数的帖子"
          value={data.readiness.sample_count}
          sub={`已发布帖子共 ${data.post_count} 条 · 分析单位是帖子，不是快照`}
        />
        <CountKpi
          label="比较组"
          value={data.cohort_count}
          sub="同维度、同取值、同观察窗口"
        />
        <CountKpi
          label="成立的比较"
          value={data.valid_comparison_count}
          sub={`共尝试 ${data.comparisons.length} 组 · 不成立的组不给结论`}
        />
        <div className="kpi kpi--quiet">
          <div className="kpi__label">观察窗口</div>
          <div className="kpi__value kpi__value--none">{data.observation_window}</div>
          <div className="kpi__sub">取自读数最多的那一档；不同窗口不放在一起比</div>
        </div>
      </div>

      {data.readiness.checks.length > 0 ? (
        <Block title="闸门的四条判据" hint="由代码判定，不经模型">
          <ul className="stack stack--tight">
            {data.readiness.checks.map((check) => (
              <li key={check.name}>
                <div className="meta__label">
                  {check.name} · {check.passed ? '通过' : '未通过'}
                </div>
                <div className="cell-muted">{check.detail}</div>
              </li>
            ))}
          </ul>
        </Block>
      ) : null}

      {validComparisons.length > 0 ? (
        <Block
          title="观察到的差异"
          hint={`${validComparisons.length} 组 · 只是观察，不是规则`}
        >
          <ul className="stack stack--tight">
            {validComparisons.map((row, index) => (
              <li key={index}>
                <div className="meta__label">{comparisonHeadline(row)}</div>
                <div>{comparisonStatement(row)}</div>
              </li>
            ))}
          </ul>
        </Block>
      ) : (
        <Block title="观察到的差异" hint="没有成立的比较">
          {/* 不成立的比较也要显示原因，但要显示得**不像一个结论**：
              不给中位数、不给方向，只说为什么没法比。 */}
          <ul className="stack stack--tight">
            {data.comparisons.slice(0, 6).map((row, index) => (
              <li key={index} className="cell-muted">
                {invalidComparisonLine(row)}
              </li>
            ))}
            {data.comparisons.length > 6 ? (
              <li className="cell-muted">另有 {data.comparisons.length - 6} 组未列出。</li>
            ) : null}
          </ul>
        </Block>
      )}

      {data.evidence.length > 0 ? (
        <Block title="依据" hint={`${data.evidence.length} 条 · 每条都能回到原始观察`}>
          <ul className="stack stack--tight">
            {data.evidence.map((item) => (
              <li key={item.id}>
                <div className="meta__label">{evidenceHeadline(item)}</div>
                <div>{item.statement}</div>
                {/* 可追溯性：一条没有出处的证据在这层里不该存在。 */}
                <div className="cell-muted mono">
                  {item.source_x_post_ids.length
                    ? `x_post: ${item.source_x_post_ids.join(', ')}`
                    : '没有可引用的 x_post_id（帖子尚未发布或未取到 id）'}
                </div>
              </li>
            ))}
          </ul>
        </Block>
      ) : null}

      {limitationLines(data).length > 0 ? (
        <Block title="这份评估没有回答什么">
          <ul className="stack stack--tight">
            {limitationLines(data).map((note, index) => (
              <li key={index} className="cell-muted">
                {note}
              </li>
            ))}
          </ul>
        </Block>
      ) : null}

      <p className="cell-muted scope-note">{LEARNING_DISCLAIMER}</p>
    </div>
  )
}

/**
 * 一条提案的行内视图。
 *
 * 工作单第三十一节点名的七样东西一样不少：目标 / 当前值 / 建议值 / 依据 / 样本 /
 * 限制 / 状态。少任何一样，这条建议就会开始像一道命令——没有样本量的建议无法被
 * 判断为"样本太小"，没有限制的建议读起来像一条规则。
 *
 * 两个按钮是**记录一个决定**，不是"执行"。批准之后界面上必须说清楚什么都没发生
 * （`reviewOutcomeLine`），否则"批准"这个词本身就会让人以为系统动了。
 */
function ProposalRow({
  row,
  busy,
  error,
  onReview,
}: {
  row: LearningProposalRow
  busy: boolean
  error: string | null
  onReview: (decision: 'approve' | 'reject') => void
}) {
  const [rejecting, setRejecting] = useState(false)
  const [note, setNote] = useState('')
  const invalid = validationLine(row)

  return (
    <li>
      <div className="meta__label">{proposalHeadline(row)}</div>
      <div>
        <strong>{changeLine(row)}</strong>
      </div>
      <div className="cell-muted">{row.rationale}</div>
      <div className="cell-muted">{evidenceLine(row)}</div>
      <div className="cell-muted mono">配方版本：{versionLine(row)}</div>
      {isStale(row) ? (
        <div className="cell-muted">这条裁决已经过期：{row.stale_reason}</div>
      ) : null}
      {invalid ? <div className="notice notice--warn">{invalid}</div> : null}

      {proposalLimitations(row).length > 0 ? (
        <ul className="stack stack--tight">
          {proposalLimitations(row).map((item, index) => (
            <li key={index} className="cell-muted">
              {item}
            </li>
          ))}
        </ul>
      ) : null}

      {row.review_note ? (
        <div className="cell-muted">裁决备注：{row.review_note}</div>
      ) : null}

      {isReviewable(row) ? (
        <div className="row-actions">
          {rejecting ? (
            <>
              <input
                type="text"
                value={note}
                onChange={(event) => setNote(event.target.value)}
                placeholder="否决理由（必填）"
                disabled={busy}
              />
              <button
                type="button"
                className="btn btn--danger"
                disabled={busy || !note.trim()}
                onClick={() => onReview('reject')}
              >
                确认否决
              </button>
              <button type="button" className="btn" disabled={busy} onClick={() => setRejecting(false)}>
                取消
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                className="btn btn--primary"
                disabled={busy}
                onClick={() => onReview('approve')}
              >
                {REVIEW_ACTION_LABELS.approve}
              </button>
              <button
                type="button"
                className="btn btn--danger"
                disabled={busy}
                onClick={() => setRejecting(true)}
              >
                {REVIEW_ACTION_LABELS.reject}
              </button>
            </>
          )}
        </div>
      ) : (
        <div className="cell-muted">{reviewOutcomeLine(row)}</div>
      )}

      {error ? <div className="notice notice--warn">{error}</div> : null}
    </li>
  )
}

/**
 * 提案块（Phase 5D）。
 *
 * 放在 `LearningBlock` **下面**而不是并进去：它回答的是另一个问题。
 * 上面那块说"够不够可靠"，这块说"可靠的话可以试什么"。合成一块以后，
 * "数据不够所以零条建议"会看起来像"这一层坏了"。
 *
 * 唯一的写操作就在这里。它按下的后果只有一条记录——本组件**不**触发任何
 * 重新计算之外的动作，也没有第二个接口可调。
 */
function ProposalBlock({
  data,
  learning,
  loading,
  error,
  onRetry,
  onReviewed,
}: {
  data: ProposalsOverview | null
  learning: LearningOverview | null
  loading: boolean
  error: ApiError | null
  onRetry: () => void
  onReviewed: () => void
}) {
  const [busyId, setBusyId] = useState<string | null>(null)
  const [rowError, setRowError] = useState<{ id: string; message: string } | null>(null)

  async function review(row: LearningProposalRow, decision: 'approve' | 'reject', note?: string) {
    setBusyId(row.id)
    setRowError(null)
    try {
      await api.reviewLearningProposal(
        row.id,
        decision === 'approve' ? { action: 'approve' } : { action: 'reject', note: note ?? '' },
        data?.observation_window || undefined,
      )
      onReviewed()
    } catch (caught) {
      // 404 与 409 都不是"重试一下就好"：一个说明这条提案已经不存在了，
      // 一个说明它已经被裁决过。两种都要让使用者去重新加载，而不是反复点。
      const message =
        caught instanceof ApiError ? describeReviewFailure(caught) : String(caught)
      setRowError({ id: row.id, message })
    } finally {
      setBusyId(null)
    }
  }

  if (error && !data) {
    return <ErrorState error={error} what="加载学习提案失败。" onRetry={onRetry} />
  }
  if (!data) {
    return loading ? <LoadingState message="正在加载学习提案…" /> : null
  }

  if (!data.available) {
    return (
      <NotAvailable
        what={
          <>
            <strong>还没有任何已发布内容可供观察</strong>
            ：提案回答的是"基于过去的反馈可以尝试什么"，一条帖子都没有时这个问题
            无从谈起。这里<strong>不会</strong>用"暂无建议"顶上——那读起来像"看过了，
            没有问题"。
          </>
        }
        reason={data.reason}
      />
    )
  }

  return (
    <div className="stack">
      {/* 三种"零条"必须长得不一样：没有数据（上面已经返回了）/ 闸门关着 /
          闸门开着但没有方向。把后两种渲染成"没有发现可以改进的地方"，就是把一个
          "数据不够"的结论说成一个"看过了，没问题"的结论。 */}
      <div className={data.learning_ready ? 'notice' : 'notice notice--warn'}>
        <div className="notice__title">{proposalsHeadline(data)}</div>
        {!data.learning_ready && learning ? (
          <div className="cell-muted">
            学习准备度给出的原因是：{learning.readiness.reason}。
          </div>
        ) : null}
        {!data.learning_ready ? (
          <div className="cell-muted">
            所以这里没有提案——是因为<strong>数据还不够</strong>，不是因为"看过了，
            没有问题"。这也是为什么下面没有可裁决的东西：闸门关着时，生成环节根本
            不会产出建议，而不是产出了再被界面藏起来。
          </div>
        ) : null}
      </div>

      {data.proposals.length > 0 ? (
        <ul className="stack stack--tight">
          {data.proposals.map((row) => (
            <ProposalRow
              key={row.id}
              row={row}
              busy={busyId === row.id}
              error={rowError?.id === row.id ? rowError.message : null}
              onReview={(decision) => void review(row, decision, undefined)}
            />
          ))}
        </ul>
      ) : null}

      {data.limitations.length > 0 ? (
        <div>
          <div className="meta__label">这份提案列表没有回答什么</div>
          <ul className="stack stack--tight">
            {data.limitations.map((note, index) => (
              <li key={index} className="cell-muted">
                {note}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <p className="cell-muted scope-note">{PROPOSALS_DISCLAIMER}</p>
    </div>
  )
}

/**
 * 裁决失败时说人话。
 *
 * 刻意**不**把后端原文透出去（`ApiError.message` 是英文的服务层文案）：
 * 使用者需要知道的是"接下来该做什么"，而不是哪个约束叫什么名字。
 */
function describeReviewFailure(error: ApiError): string {
  if (error.status === 404) {
    return '这条提案在当前观察下已经不存在了（数据可能变了）。请重新加载列表。'
  }
  if (error.status === 409) {
    return '这条提案已经被裁决过。裁决是不可改写的记录，不能覆盖。'
  }
  if (error.status === 422) {
    return '这次裁决没有被接受：否决需要写明理由。'
  }
  return error.message
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
  // 反馈是**另一个接口**（`GET /api/admin/feedback`），单独取、单独失败。
  // 合成一个请求的话，反馈拿不到会让整页变成错误页——而这一页上其它每一块
  // 都还能用。日期窗口对它也无效：反馈是全库的，没有 window 参数。
  const feedback = useAsync(() => api.getFeedback(), [])
  // 学习准备度是**第三个**接口，同样单独取、单独失败。它是控制台里最重的一次
  // 读（按条读 checkpoint），所以更不能并进反馈那一块：反馈拿得到、准备度读不动
  // 时，上面那些观察仍然应当显示出来。
  const learning = useAsync(() => api.getLearning(), [])
  // 提案是**第四个**接口（Phase 5D），同样单独取、单独失败。它比准备度更重一点
  // ——准备度只算"够不够格"，提案还要真的比一遍。两个都不传 window：后端按读数
  // 最多的那一档取，同一个决定，所以两边不会落到不同的窗口上。真要让它们错开，
  // 裁决记录就会因为提案 id 变了而看起来集体"过期"。
  const proposals = useAsync(() => api.getLearningProposals(), [])

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
          {/* 反馈单独成节，不并进上面任何一块：它的**对象**不一样。
              上面全部在数"系统自己跑了多少"，这一节说的是"发出去的东西
              后来怎么样了"——那是外部世界，而且全部是观察，不是成绩。 */}
          <section className="section">
            <div className="section__head">
              <h2 className="section__title">发布内容的反馈</h2>
              <span className="section__hint">公开计数 · 手工采集 · 不是质量评分</span>
            </div>
            <FeedbackBlock
              data={feedback.data}
              loading={feedback.loading}
              error={feedback.error}
              onRetry={feedback.reload}
            />

            {/* 学习准备度紧跟在反馈后面、但**单独成块**：上面答"看到了什么"，
                这一块答"看到的这些够不够格拿去做未来的判断"。它是只读评估，
                不触发任何调整，也不推进任何学习流程。 */}
            <div className="block-gap">
              <Block
                title="学习准备度"
                hint="只读评估 · 不修改任何设置 · 不是质量评分"
              >
                <LearningBlock
                  data={learning.data}
                  loading={learning.loading}
                  error={learning.error}
                  onRetry={learning.reload}
                />
              </Block>
            </div>

            {/* 提案紧跟着准备度，但**单独成块**：上面那块答"够不够可靠"，这一块答
                "可靠的话可以尝试什么"。合成一块以后，"数据不够所以零条建议"会看
                起来像这一层坏了。这一块也是控制台里**唯一**能按下写操作的地方，
                而那个写操作只记下一个决定——不改任何设置。 */}
            <div className="block-gap">
              <Block
                title="可以尝试什么"
                hint="只是建议，不是结论 · 批准不等于应用 · 本层不执行建议"
              >
                <ProposalBlock
                  data={proposals.data}
                  learning={learning.data}
                  loading={proposals.loading}
                  error={proposals.error}
                  onRetry={proposals.reload}
                  // 裁决之后只重新拉提案：裁决本身会改变列表的 reviewable 状态。
                  // 不重拉准备度——它一个字段都不会因裁决而变，重拉只是再读一遍
                  // checkpoint，而那是这一页最贵的一次读。
                  onReviewed={proposals.reload}
                />
              </Block>
            </div>
          </section>

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
                      metric="发布内容的反馈"
                      scope={data.x_content_performance.available ? '全库 · 最新一次观察' : EMPTY}
                      source="post_metrics_snapshot"
                      note={
                        <>
                          只有<strong>公开计数</strong>（点赞 / 回复 / 转发 / 引用），由脚本
                          <strong>手工采集</strong>；没有曝光与书签，也没有定时任务。
                          这一块的数值见上面的「发布内容的反馈」。
                        </>
                      }
                    />
                  </tbody>
                </table>
              </div>
            </div>

            <div className="block-gap">
              {/* 按 `available` 分支，而不是把结论写死在文案里：采集脚本落地之后
                  "从来没有被采集过"这句只在真的还没跑过时成立。 */}
              {data.x_content_performance.available ? (
                <div className="notice">
                  <div className="notice__title">反馈数据已就绪，但不在这一页的图表里</div>
                  本页的图都按 Run 计数（"系统跑了多少"），而反馈是**每条帖子**的公开计数，
                  两者不是同一种东西，所以没有并进上面的分布图。逐条的数值在
                  上面的「发布内容的反馈」、发布记录页的「最新观察」列，以及
                  Run 详情里的「这条帖子的后续观察」。
                </div>
              ) : (
                <NotAvailable
                  what={
                    <>
                      <strong>还没有采集过任何发布结果</strong>
                      ：曝光、点赞、回复、转发、收藏和互动率目前
                      <strong>一律不显示</strong>。它们不是查询失败才拿不到 —— 是采集脚本
                      （<code>scripts/refresh_post_metrics.py</code>）还没有跑过。
                      本页也<strong>不会</strong>用 0 或任何近似值填充。
                    </>
                  }
                  reason={data.x_content_performance.reason}
                />
              )}
            </div>
          </section>
        </div>
      ) : null}
    </>
  )
}
