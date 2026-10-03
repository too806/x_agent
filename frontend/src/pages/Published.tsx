import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { api, xPostUrl } from '../api/client'
import { Pagination } from '../components/Pagination'
import { PublishControl, ResolveControl } from '../components/PublishAction'
import { RunDetailDrawer } from '../components/RunDetailDrawer'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { CopyButton } from '../components/ui/CopyButton'
import { IconExternal, IconRefresh } from '../components/ui/icons'
import { useAsync } from '../hooks/useAsync'
import { useConsole } from '../hooks/useConsole'
import { usePageClamp } from '../hooks/usePageClamp'
import { PUBLISH_STATUSES, isUnresolved } from '../types/api'
import { NO_DATA, hoursSinceLabel, metricLabel } from '../utils/feedback'
import { EMPTY, codePointLength, dateTime, publishStatusLabel } from '../utils/format'
import { UNRESOLVED_FILTER } from '../utils/publishAction'
import type { FeedbackPostSummary, PublishedRow } from '../types/api'

/**
 * 发布记录列表 + 裁决台。
 *
 * 两条**不能混淆**的读法，界面上分开呈现：
 *
 *   - `published_post.status` 是发布状态的唯一权威
 *   - `agent_run.status` 只是它的镜像，两者可能合法地互相矛盾
 *
 * 这个列表刻意**不过滤掉** failed / publish_unknown：它们是运维最需要盯着的东西，
 * 从"发布记录"里藏起来只会让人以为一切正常。
 *
 * 本系统是审计记录，不是 X 当前状态的实时镜像——所以这里**没有删除按钮，也没有
 * "标记删除"**。人工去 X 上删了推文，这一行不会跟着变，也不该变。
 *
 * Phase 5B.4 加的「最新观察」列是这张表里**唯一**一个例外，所以它单独标注时间
 * （"发布后 N 小时"）和来源（手工采集）：上面几列是发布那一刻的审计事实，
 * 那一列是后补的观察。两者不加区分地并排，人会拿今天的数字解释当时的行为。
 *
 * ---------------------------------------------------------------------------
 * 为什么这一页**不**调用 `utils/publishAction.ts` 里的 `getPublishAction`
 * ---------------------------------------------------------------------------
 *
 * 那套契约要两个输入：Run 状态与发布记录状态。`PublishedRow` **只有后者** ——
 * 发布列表接口不返回 `run_status`（要它就得改后端 schema，超出这个契约的范围）。
 * 拿 `null` 顶替 Run 状态会让契约把这一页的每一行都判成 HIDDEN，那是功能倒退：
 * 明确的 `failed` 就没有重发入口了。
 *
 * 更要紧的是 `publishing` 这一行**必须留着裁决入口**。契约对 `publishing` 返回
 * HIDDEN 是对的 —— 它管的是"要不要放一个会调 X 的按钮"，而 `publishing` 时第一个
 * 请求可能还在飞，绝不能给发布按钮。但裁决入口是另一回事：后端
 * `UNRESOLVED_STATUSES` 明确接受 `publishing`（见 `app/repositories.py` 里
 * `confirm_published_manually` 的注释——"这两者都需要人去看，也都需要同一个出口"），
 * 而且进程崩在中间时 `publishing` 会**永久卡住**，这是它唯一的出口。
 * 两个入口一起收掉，那条记录在界面上就再也没救了。
 *
 * 所以分工是：抽屉（Run 审计视图）严格照契约走；这一页是**唯一的裁决台**，
 * 继续按 `isUnresolved` 给未决行开裁决入口。两条规则管的是两个不同的问题。
 */

/** 短于这个长度的正文直接整段显示，不给"展开全文"。 */
const INLINE_BODY_LIMIT = 60

/**
 * 这条帖子**后来被观察到的**最新一次结果（Phase 5B.4）。
 *
 * 这是整张表里唯一一个**会随时间变化**的格：上面几列都是发布那一刻的审计事实，
 * 这一列是后采的。所以它必须自带时间（"发布后 N 小时"），否则人会拿今天的数
 * 去对昨天那一行做结论。
 *
 * 没采过时显示「暂无数据」——**不是 `0`**。整张表里一个"赞 0 · 回 0 · 转 0"
 * 会被直接读成"这条没人看"，而事实是这个帖子从没被采集过。
 */
function MetricsCell({ metrics }: { metrics: FeedbackPostSummary | null }) {
  if (metrics === null) {
    // 采集是手工触发的（scripts/refresh_post_metrics.py），没跑过就一个快照都没有。
    return <span className="cell-muted">{NO_DATA}</span>
  }
  return (
    <div className="stack stack--tight">
      <span className="cell-nowrap">
        赞 {metricLabel(metrics.likes)} · 回 {metricLabel(metrics.replies)} · 转{' '}
        {metricLabel(metrics.reposts)} · 引 {metricLabel(metrics.quotes)}
      </span>
      <span className="cell-muted">
        {hoursSinceLabel(metrics.hours_since_publish)} · 观察 {metrics.observation_count} 次
      </span>
    </div>
  )
}

function PublishRow({
  row,
  onSettled,
  onOpenRun,
}: {
  row: PublishedRow
  onSettled: () => void
  onOpenRun: (runId: string) => void
}) {
  const [expanded, setExpanded] = useState(false)

  const length = codePointLength(row.body)
  // 正文短的时候（一条推文本来就可能是几十个字）折起来没有意义：
  // 一个只能展开出一行的"展开全文"是纯噪声。
  const collapsible = length > INLINE_BODY_LIMIT
  const clamped = collapsible && !expanded
  const unresolved = isUnresolved(row.status)

  return (
    <>
      {/* 结果未定的行整行加一道左色条。它不是"另一条记录"，它是**需要人介入**的
          记录——在长列表里必须能一眼扫出来，而不是靠逐行读状态徽标。 */}
      <tr className={unresolved ? 'table__row--unknown' : undefined}>
        <td className="table__topic">
          <span className="table__clamp" title={row.topic ?? undefined}>
            {row.topic ?? <span className="cell-muted">{EMPTY}</span>}
          </span>
        </td>
        <td className="publish-body">
          <div className={clamped ? 'body-preview table__clamp' : 'body-preview'}>
            {row.body}
          </div>
          {collapsible ? (
            <button
              type="button"
              className="btn btn--link publish-body__toggle"
              onClick={() => setExpanded((value) => !value)}
            >
              {expanded ? '收起全文' : `展开全文（${length} 字）`}
            </button>
          ) : null}
        </td>
        <td>
          <StatusBadge status={row.status} kind="publish" />
          {/* 最后错误挂在状态下面，而不是单独占一列：它只在 failed / 未定时有内容，
              单开一列会让绝大多数行多出一个空列。 */}
          {row.last_error ? (
            <div className="publish-error" title={row.last_error}>
              {row.last_error}
            </div>
          ) : null}
        </td>
        <td className="cell-muted cell-nowrap">
          {row.x_post_id ? (
            <span className="row row--tight">
              <span className="mono" title={row.x_post_id}>
                {row.x_post_id}
              </span>
              <CopyButton value={row.x_post_id} label="复制 X Post ID" />
            </span>
          ) : (
            // 没有 ID 不代表失败：publish_unknown 的典型形态就是"发出去过，但拿不到 ID"。
            EMPTY
          )}
        </td>
        <td className="cell-nowrap">
          <div className="table__time-abs">{dateTime(row.published_at)}</div>
        </td>
        <td>
          <MetricsCell metrics={row.latest_metrics} />
        </td>
        <td className="cell-nowrap">
          <div className="table__time-abs">{dateTime(row.updated_at)}</div>
        </td>
        <td className="table__actions">
          {/* 三种可操作状态，三种不同的操作：
                failed      → 重发（明确的失败，重发是安全的）
                unresolved  → 裁决（不知道发没发出去，只能人去 X 上看）
                其他        → 没有操作。published 是终态，没有"撤销"。 */}
          <button type="button" className="btn btn--sm" onClick={() => onOpenRun(row.run_id)}>
            查看运行
          </button>
          {row.x_post_id ? (
            <a
              className="btn btn--sm"
              href={xPostUrl(row.x_post_id)}
              target="_blank"
              rel="noreferrer noopener"
            >
              <IconExternal size={14} />
              在 X 中打开
            </a>
          ) : null}
          {row.status === 'failed' ? (
            <PublishControl runId={row.run_id} body={row.body} isRetry onSettled={onSettled} />
          ) : unresolved ? (
            <span className="cell-muted publish-action-hint">需要在下方裁决</span>
          ) : (
            <span className="cell-muted publish-action-hint">终态</span>
          )}
        </td>
      </tr>
      {unresolved ? (
        <tr className="table__row--unknown">
          <td colSpan={8} className="resolve-cell">
            <ResolveControl
              runId={row.run_id}
              publish={{
                status: row.status,
                draft_id: row.draft_id,
                body: row.body,
                x_post_id: row.x_post_id,
                published_at: row.published_at,
                last_error: row.last_error,
                updated_at: row.updated_at,
              }}
              onSettled={onSettled}
            />
          </td>
        </tr>
      ) : null}
    </>
  )
}

export function Published() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [page, setPage] = useState(1)
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [openRunId, setOpenRunId] = useState<string | null>(null)
  const { refresh } = useConsole()

  // 筛选状态放在 URL 里：Overview 上"查看全部 N 条 →"能直接落到待裁决视图，
  // 刷新页面也不会把筛选条件丢掉。
  const statusFilter = searchParams.get('status') ?? ''
  const unresolvedOnly = statusFilter === UNRESOLVED_FILTER
  const filtered = statusFilter !== '' || from !== '' || to !== ''

  const { data, loading, error, reload } = useAsync(
    () =>
      api.listPublished({
        status: unresolvedOnly || statusFilter === '' ? undefined : statusFilter,
        unresolved: unresolvedOnly,
        dateFrom: localDateToIso(from),
        dateTo: localDateToIso(to, 1),
        page,
      }),
    [statusFilter, from, to, page],
  )

  useEffect(() => {
    setPage(1)
  }, [statusFilter, from, to])

  // 重发 / 裁决成功后这一行会改变状态，可能就不再满足当前的筛选（按 failed 过滤时
  // 重发成功、按待裁决过滤时裁决完成）。空的尾页要退回去：留在那里会显示一句
  // "没有发布记录"，读起来像"一条都没有"，而实际上前面几页还有。
  usePageClamp(page, setPage, data !== null && data.items.length === 0, data !== null)

  function setStatus(value: string) {
    const next = new URLSearchParams(searchParams)
    if (value) next.set('status', value)
    else next.delete('status')
    setSearchParams(next, { replace: true })
  }

  function afterSettle() {
    reload()
    refresh() // 侧栏的待裁决计数要跟着变
  }

  return (
    <>
      <section className="section">
        <div className="section__head">
          <h2 className="section__title">发布记录</h2>
          <div className="row row--tight">
            {data ? <span className="cell-muted">共 {data.page.total} 条</span> : null}
            <button
              type="button"
              className="btn btn--sm"
              onClick={reload}
              disabled={loading}
              title="刷新列表（保留当前筛选条件）"
            >
              <IconRefresh size={14} />
              刷新列表
            </button>
          </div>
        </div>

        {/* 口径说明从页面最顶上挪到标题下面：它是"怎么看这个列表"，不是"要做的第一件事"。
            放在最上面会把筛选栏推下去一屏。 */}
        <p className="cell-muted scope-note">
          这里是<strong>审计记录</strong>，不是 X 的实时镜像。在 X 上删掉一条帖子，这里不会有任何变化
          —— 这是刻意的。发布状态才是权威字段，Run 自己的状态只是它的镜像视图。
          因此本页<strong>没有删除功能</strong>，也没有"标记已删除"。
        </p>

        <div className="card">
          <div className="card__body">
            <div className="filter-bar">
              <div className="field">
                <label className="field__label" htmlFor="publish-status">
                  发布状态
                </label>
                <select
                  id="publish-status"
                  className="select"
                  value={statusFilter}
                  onChange={(event) => setStatus(event.target.value)}
                >
                  <option value="">全部</option>
                  <option value={UNRESOLVED_FILTER}>待裁决（发布结果未定）</option>
                  {/* value 仍然是后端枚举，只有显示文案走中文映射——
                      筛选发出去的是 status=publish_unknown，不是 status=发布结果未知。 */}
                  {PUBLISH_STATUSES.map((value) => (
                    <option key={value} value={value}>
                      {publishStatusLabel(value)}
                    </option>
                  ))}
                </select>
              </div>

              <div className="field">
                <label className="field__label" htmlFor="publish-from">
                  发布时间从
                </label>
                <input
                  id="publish-from"
                  className="input"
                  type="date"
                  value={from}
                  onChange={(event) => setFrom(event.target.value)}
                />
              </div>

              <div className="field">
                <label className="field__label" htmlFor="publish-to">
                  发布时间到（含当天）
                </label>
                <input
                  id="publish-to"
                  className="input"
                  type="date"
                  value={to}
                  onChange={(event) => setTo(event.target.value)}
                />
              </div>

              <button
                type="button"
                className="btn"
                disabled={!statusFilter && !from && !to}
                onClick={() => {
                  setFrom('')
                  setTo('')
                  setStatus('')
                }}
              >
                清除筛选
              </button>
            </div>
          </div>
        </div>
      </section>

      <section className="section">
        {loading && !data ? <LoadingState message="正在加载发布记录…" variant="table" /> : null}
        {error ? (
          <ErrorState error={error} what="加载发布记录失败。" onRetry={reload} />
        ) : null}

        {data ? (
          <div className={loading ? 'card card--flush is-loading' : 'card card--flush'}>
            {data.items.length === 0 ? (
              // 三种"空"是不同的事，不能共用一句话：没有任何发布记录、待裁决队列
              // 是空的（这是好状态）、以及筛选条件没匹配上（要动的是筛选，不是别人）。
              <EmptyState
                title={
                  unresolvedOnly
                    ? '没有需要裁决的发布'
                    : filtered
                      ? '没有符合筛选条件的发布记录'
                      : '还没有发布记录'
                }
                hint={
                  unresolvedOnly
                    ? '每一次发布尝试都有确定结果。这正是你想要的状态。'
                    : filtered
                      ? '可以清除某个筛选条件，或把日期范围放宽一些。'
                      : 'Run 尝试发布之后，记录会出现在这里。'
                }
              />
            ) : (
              <>
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>主题</th>
                        <th>发布内容</th>
                        <th>发布状态</th>
                        <th>X 帖子 ID</th>
                        <th title="X 接受这条帖子的时刻。结果未定的行这里是 —。">发布时间</th>
                        <th title="手工采集到的最新一次公开计数（点赞 / 回复 / 转发 / 引用），以及距发布多久。没有采集记录时显示「暂无数据」——那不是 0。">
                          最新观察
                        </th>
                        <th title="这条发布记录最后一次被改动的时刻。结果未定的行靠它判断这次尝试是什么时候发起的。">
                          记录更新时间
                        </th>
                        <th>
                          <span className="sr-only">操作</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.items.map((row) => (
                        <PublishRow
                          key={row.publish_id}
                          row={row}
                          onSettled={afterSettle}
                          onOpenRun={setOpenRunId}
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
                <Pagination page={data.page} onChange={setPage} />
              </>
            )}
          </div>
        ) : null}
      </section>

      {openRunId ? (
        // 抽屉里重发 / 裁决成功后，这一行的状态就过期了，列表要跟着重取。
        <RunDetailDrawer
          runId={openRunId}
          onClose={() => setOpenRunId(null)}
          onSettled={afterSettle}
        />
      ) : null}
    </>
  )
}

/**
 * `<input type="date">` 的本地日历日 → 带时区的 ISO 时刻。
 * 与 Runs 页同一套规则：本地零点起算，右端 +1 天（后端 date_to 是半开区间）。
 */
function localDateToIso(value: string, addDays = 0): string | undefined {
  if (!value) return undefined
  const parts = value.split('-')
  if (parts.length !== 3) return undefined
  const year = Number(parts[0])
  const month = Number(parts[1])
  const day = Number(parts[2])
  if (!Number.isFinite(year) || !Number.isFinite(month) || !Number.isFinite(day)) return undefined
  return new Date(year, month - 1, day + addDays).toISOString()
}
