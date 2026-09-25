import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { api, xPostUrl } from '../api/client'
import { Pagination } from '../components/Pagination'
import { PublishControl, ResolveControl } from '../components/PublishAction'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { useAsync } from '../hooks/useAsync'
import { useConsole } from '../hooks/useConsole'
import { PUBLISH_STATUSES, isUnresolved } from '../types/api'
import { EMPTY, dateTime } from '../utils/format'
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
}: {
  row: PublishedRow
  onSettled: () => void
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
            {expanded ? 'Show less' : 'Show more'}
          </button>
        </td>
        <td>
          <StatusBadge status={row.status} kind="publish" />
        </td>
        <td className="cell-muted cell-nowrap">
          {row.x_post_id ? (
            <a
              href={xPostUrl(row.x_post_id)}
              target="_blank"
              rel="noreferrer noopener"
              className="mono"
            >
              {row.x_post_id}
            </a>
          ) : (
            EMPTY
          )}
        </td>
        <td className="cell-muted cell-nowrap">{dateTime(row.published_at)}</td>
        <td className="cell-muted cell-nowrap">{dateTime(row.updated_at)}</td>
        <td className="cell-muted clamp-2">{row.last_error ?? EMPTY}</td>
        <td className="cell-nowrap">
          {/* 三种可操作状态，三种不同的操作：
                failed      → 重发（明确的失败，重发是安全的）
                unresolved  → 裁决（不知道发没发出去，只能人去 X 上看）
                其他        → 没有操作。published 是终态，没有"撤销"。 */}
          {row.status === 'failed' ? (
            <PublishControl runId={row.run_id} body={row.body} isRetry onSettled={onSettled} />
          ) : isUnresolved(row.status) ? (
            <span className="cell-muted">needs a decision below</span>
          ) : (
            <span className="cell-muted">final</span>
          )}
        </td>
      </tr>
      {isUnresolved(row.status) ? (
        <tr>
          <td colSpan={8} style={{ background: 'var(--surface-sunken)' }}>
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
  const { refresh } = useConsole()

  // 筛选状态放在 URL 里：Overview 上"View all N →"能直接落到待裁决视图，
  // 刷新页面也不会把筛选条件丢掉。
  const statusFilter = searchParams.get('status') ?? ''
  const unresolvedOnly = statusFilter === UNRESOLVED_FILTER

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
        This is an <strong>audit record</strong>, not a live mirror of X. Deleting a post on X will
        not change anything here — on purpose. Publish status is the authoritative field; a run&apos;s
        own status is only a mirrored view of it.
      </div>

      <section className="section">
        <div className="section__head">
          <h2 className="section__title">Publish records</h2>
          <button type="button" className="btn btn--sm" onClick={reload} disabled={loading}>
            Refresh
          </button>
        </div>

        <div className="card">
          <div className="card__body">
            <div className="filter-bar">
              <div className="field">
                <label className="field__label" htmlFor="publish-status">
                  Status
                </label>
                <select
                  id="publish-status"
                  className="select"
                  value={statusFilter}
                  onChange={(event) => setStatus(event.target.value)}
                >
                  <option value="">Any status</option>
                  <option value={UNRESOLVED_FILTER}>Needs a decision (unresolved)</option>
                  {PUBLISH_STATUSES.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </div>

              <div className="field">
                <label className="field__label" htmlFor="publish-from">
                  From
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
                  To (inclusive)
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
                Clear
              </button>
            </div>
          </div>
        </div>
      </section>

      <section className="section">
        {loading && !data ? <LoadingState message="Loading publish records…" /> : null}
        {error ? (
          <ErrorState error={error} what="Failed to load publish records." onRetry={reload} />
        ) : null}

        {data ? (
          <div className="card card--flush" style={{ opacity: loading ? 0.6 : 1 }}>
            {data.items.length === 0 ? (
              <EmptyState
                title={unresolvedOnly ? 'Nothing needs a decision' : 'No publish records'}
                hint={
                  unresolvedOnly
                    ? 'Every publish attempt has a definite outcome. This is the state you want.'
                    : 'Runs appear here once a publish has been attempted.'
                }
              />
            ) : (
              <>
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Topic</th>
                        <th>Body actually sent</th>
                        <th>Publish</th>
                        <th>X post</th>
                        <th>Published at</th>
                        <th>Updated</th>
                        <th>Last error</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {data.items.map((row) => (
                        <PublishRow key={row.publish_id} row={row} onSettled={afterSettle} />
                      ))}
                    </tbody>
                  </table>
                </div>
                <Pagination page={data.page} onChange={setPage} itemLabel="records" />
              </>
            )}
          </div>
        ) : null}
      </section>
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
