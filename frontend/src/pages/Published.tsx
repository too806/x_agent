import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { api, xPostUrl } from '../api/client'
import { Pagination } from '../components/Pagination'
import { PublishControl, ResolveControl } from '../components/PublishAction'
import { RunDetailDrawer } from '../components/RunDetailDrawer'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { useAsync } from '../hooks/useAsync'
import { useConsole } from '../hooks/useConsole'
import { usePageClamp } from '../hooks/usePageClamp'
import { PUBLISH_STATUSES, isUnresolved } from '../types/api'
import { EMPTY, dateTime, publishStatusLabel } from '../utils/format'
import type { PublishedRow } from '../types/api'

const UNRESOLVED_FILTER = '__unresolved__'

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
 */

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

  return (
    <>
      <tr>
        <td className="table__topic">{row.topic ?? <span className="cell-muted">{EMPTY}</span>}</td>
        <td>
          <div className={expanded ? undefined : 'clamp-2'}>{row.body}</div>
          <button
            type="button"
            className="btn btn--link"
            style={{ marginTop: 4 }}
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? '收起' : '展开全文'}
          </button>
        </td>
        <td>
          <StatusBadge status={row.status} kind="publish" />
        </td>
        <td className="cell-muted cell-nowrap">
          <span className="mono">{row.x_post_id ?? EMPTY}</span>
        </td>
        <td className="cell-muted cell-nowrap">{dateTime(row.published_at)}</td>
        <td className="cell-muted cell-nowrap">{dateTime(row.created_at)}</td>
        <td className="cell-muted cell-nowrap">{dateTime(row.updated_at)}</td>
        <td className="cell-muted clamp-2">{row.last_error ?? EMPTY}</td>
        <td className="cell-nowrap">
          {/* 三种可操作状态，三种不同的操作：
                failed      → 重发（明确的失败，重发是安全的）
                unresolved  → 裁决（不知道发没发出去，只能人去 X 上看）
                其他        → 没有操作。published 是终态，没有"撤销"。 */}
          <div className="row" style={{ gap: 6 }}>
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
                在 X 中打开
              </a>
            ) : null}
          </div>
          <div style={{ marginTop: 6 }}>
            {row.status === 'failed' ? (
              <PublishControl runId={row.run_id} body={row.body} isRetry onSettled={onSettled} />
            ) : isUnresolved(row.status) ? (
              <span className="cell-muted">需要在下方裁决</span>
            ) : (
              <span className="cell-muted">终态</span>
            )}
          </div>
        </td>
      </tr>
      {isUnresolved(row.status) ? (
        <tr>
          <td colSpan={9} style={{ background: 'var(--surface-sunken)' }}>
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
      <div className="notice notice--info" style={{ marginBottom: 20 }}>
        这里是<strong>审计记录</strong>，不是 X 的实时镜像。在 X 上删掉一条帖子，这里不会有任何变化
        —— 这是刻意的。发布状态才是权威字段，Run 自己的状态只是它的镜像视图。
        因此本页<strong>没有删除功能</strong>。
      </div>

      <section className="section">
        <div className="section__head">
          <h2 className="section__title">发布记录</h2>
          <button type="button" className="btn btn--sm" onClick={reload} disabled={loading}>
            刷新
          </button>
        </div>

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
        {loading && !data ? <LoadingState message="正在加载发布记录…" /> : null}
        {error ? (
          <ErrorState error={error} what="加载发布记录失败。" onRetry={reload} />
        ) : null}

        {data ? (
          <div className="card card--flush" style={{ opacity: loading ? 0.6 : 1 }}>
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
                        <th>发布时间</th>
                        <th>创建时间</th>
                        <th>更新时间</th>
                        <th>最后错误</th>
                        <th />
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
