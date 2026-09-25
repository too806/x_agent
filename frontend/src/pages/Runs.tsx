import { useEffect, useState } from 'react'

import { api } from '../api/client'
import { Pagination } from '../components/Pagination'
import { RunDetailDrawer } from '../components/RunDetailDrawer'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { useAsync } from '../hooks/useAsync'
import { useDebouncedValue } from '../hooks/useDebouncedValue'
import { RUN_STATUSES } from '../types/api'
import { EMPTY, dateTime } from '../utils/format'

const TOPIC_DEBOUNCE_MS = 400

interface Filters {
  status: string
  topic: string
  from: string
  to: string
}

const NO_FILTERS: Filters = { status: '', topic: '', from: '', to: '' }

/**
 * `<input type="date">` 给的是本地日历日（`2026-09-25`）。后端要的是时刻。
 *
 * 转换放在本地时区完成，所以"从 9/25 起"指的是**使用者所在时区的** 9/25 零点，
 * 而不是 UTC 的——否则 UTC+8 的用户会发现每天的前 8 小时被算到前一天。
 *
 * `addDays` 只用于右端：后端的 `date_to` 是**半开区间**（不含），而使用者填
 * "到 9/25"说的显然是"含 9/25 这一天"，所以发出去时加一天。
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

export function Runs() {
  const [filters, setFilters] = useState<Filters>(NO_FILTERS)
  const [page, setPage] = useState(1)
  const [openRunId, setOpenRunId] = useState<string | null>(null)

  const debouncedTopic = useDebouncedValue(filters.topic, TOPIC_DEBOUNCE_MS)

  // 筛选条件一变就回到第 1 页。留在第 7 页会导致"筛选后一片空白"——那一页在新的
  // 结果集里根本不存在。
  useEffect(() => {
    setPage(1)
  }, [filters.status, debouncedTopic, filters.from, filters.to])

  const { data, loading, error, reload } = useAsync(
    () =>
      api.listRuns({
        status: filters.status || undefined,
        topic: debouncedTopic || undefined,
        dateFrom: localDateToIso(filters.from),
        dateTo: localDateToIso(filters.to, 1),
        page,
      }),
    [filters.status, debouncedTopic, filters.from, filters.to, page],
  )

  const dirty =
    filters.status !== '' || filters.topic !== '' || filters.from !== '' || filters.to !== ''

  return (
    <>
      <section className="section">
        <div className="section__head">
          <h2 className="section__title">Runs</h2>
          <button type="button" className="btn btn--sm" onClick={reload} disabled={loading}>
            Refresh
          </button>
        </div>

        <div className="card">
          <div className="card__body">
            <div className="filter-bar">
              <div className="field">
                <label className="field__label" htmlFor="run-status">
                  Run status
                </label>
                <select
                  id="run-status"
                  className="select"
                  value={filters.status}
                  onChange={(event) =>
                    setFilters((current) => ({ ...current, status: event.target.value }))
                  }
                >
                  <option value="">Any status</option>
                  {RUN_STATUSES.map((status) => (
                    <option key={status} value={status}>
                      {status}
                    </option>
                  ))}
                </select>
              </div>

              <div className="field field--grow">
                <label className="field__label" htmlFor="run-topic">
                  Topic contains
                </label>
                <input
                  id="run-topic"
                  className="input"
                  type="search"
                  placeholder="search the topic text"
                  value={filters.topic}
                  onChange={(event) =>
                    setFilters((current) => ({ ...current, topic: event.target.value }))
                  }
                />
              </div>

              <div className="field">
                <label className="field__label" htmlFor="run-from">
                  Created from
                </label>
                <input
                  id="run-from"
                  className="input"
                  type="date"
                  value={filters.from}
                  onChange={(event) =>
                    setFilters((current) => ({ ...current, from: event.target.value }))
                  }
                />
              </div>

              <div className="field">
                <label className="field__label" htmlFor="run-to">
                  Created to (inclusive)
                </label>
                <input
                  id="run-to"
                  className="input"
                  type="date"
                  value={filters.to}
                  onChange={(event) =>
                    setFilters((current) => ({ ...current, to: event.target.value }))
                  }
                />
              </div>

              <button
                type="button"
                className="btn"
                disabled={!dirty}
                onClick={() => setFilters(NO_FILTERS)}
              >
                Clear
              </button>
            </div>
          </div>
        </div>
      </section>

      <section className="section">
        {loading && !data ? <LoadingState message="Loading runs…" /> : null}
        {error ? (
          <ErrorState error={error} what="Failed to load runs." onRetry={reload} />
        ) : null}

        {data ? (
          <div className="card card--flush" style={{ opacity: loading ? 0.6 : 1 }}>
            {data.items.length === 0 ? (
              <EmptyState
                title="No runs match these filters"
                hint={
                  dirty
                    ? 'Try clearing a filter or widening the date range.'
                    : 'No runs have been recorded yet.'
                }
              />
            ) : (
              <>
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Topic</th>
                        <th>Run</th>
                        <th>Review</th>
                        <th>Publish</th>
                        <th>Created</th>
                        <th>Finished</th>
                        <th title="A stored error message exists for this run">Error</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.items.map((run) => (
                        <tr
                          key={run.run_id}
                          className="table__row--clickable"
                          role="button"
                          tabIndex={0}
                          onClick={() => setOpenRunId(run.run_id)}
                          onKeyDown={(event) => {
                            if (event.key === 'Enter' || event.key === ' ') {
                              event.preventDefault()
                              setOpenRunId(run.run_id)
                            }
                          }}
                        >
                          <td className="table__topic">{run.topic}</td>
                          <td>
                            <StatusBadge status={run.run_status} kind="run" />
                          </td>
                          <td>
                            <StatusBadge status={run.review_status} kind="review" />
                          </td>
                          <td>
                            {/* 发布状态为 `—` 表示**没有发布记录**，不是"还没发"这个
                                笼统的说法，更不能用左边的 Run 状态去顶替。 */}
                            <StatusBadge status={run.publish_status} kind="publish" />
                          </td>
                          <td className="cell-muted cell-nowrap">{dateTime(run.created_at)}</td>
                          <td className="cell-muted cell-nowrap">{dateTime(run.finished_at)}</td>
                          <td className="cell-nowrap">
                            {run.error ? (
                              <span className="err-dot" title={run.error}>
                                ●
                              </span>
                            ) : (
                              <span className="cell-muted">{EMPTY}</span>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <Pagination page={data.page} onChange={setPage} itemLabel="runs" />
              </>
            )}
          </div>
        ) : null}
      </section>

      {openRunId ? (
        <RunDetailDrawer runId={openRunId} onClose={() => setOpenRunId(null)} />
      ) : null}
    </>
  )
}
