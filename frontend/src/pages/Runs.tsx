import { useEffect, useState } from 'react'

import { api } from '../api/client'
import { Pagination } from '../components/Pagination'
import { RunDetailDrawer } from '../components/RunDetailDrawer'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { useAsync } from '../hooks/useAsync'
import { useDebouncedValue } from '../hooks/useDebouncedValue'
import { usePageClamp } from '../hooks/usePageClamp'
import { RUN_STATUSES } from '../types/api'
import { EMPTY, dateTime, runStatusLabel } from '../utils/format'

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

  // 抽屉里发布 / 裁决成功后，这一行可能就不再满足当前筛选（比如按 rejected 过滤，
  // 而它被改成了别的状态）。空的尾页要退回去，否则会看到一句"没有匹配的 Run"，
  // 而前面几页明明还有。
  usePageClamp(page, setPage, data !== null && data.items.length === 0, data !== null)

  return (
    <>
      <section className="section">
        <div className="section__head">
          <h2 className="section__title">运行记录</h2>
          <button type="button" className="btn btn--sm" onClick={reload} disabled={loading}>
            刷新
          </button>
        </div>

        <div className="card">
          <div className="card__body">
            <div className="filter-bar">
              <div className="field">
                <label className="field__label" htmlFor="run-status">
                  运行状态
                </label>
                <select
                  id="run-status"
                  className="select"
                  value={filters.status}
                  onChange={(event) =>
                    setFilters((current) => ({ ...current, status: event.target.value }))
                  }
                >
                  <option value="">全部</option>
                  {/* value 仍然是后端枚举，只有显示文案走中文映射——
                      筛选发出去的是 status=running，不是 status=运行中。 */}
                  {RUN_STATUSES.map((status) => (
                    <option key={status} value={status}>
                      {runStatusLabel(status)}
                    </option>
                  ))}
                </select>
              </div>

              <div className="field field--grow">
                <label className="field__label" htmlFor="run-topic">
                  主题包含
                </label>
                <input
                  id="run-topic"
                  className="input"
                  type="search"
                  placeholder="搜索主题文字"
                  value={filters.topic}
                  onChange={(event) =>
                    setFilters((current) => ({ ...current, topic: event.target.value }))
                  }
                />
              </div>

              <div className="field">
                <label className="field__label" htmlFor="run-from">
                  创建时间从
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
                  创建时间到（含当天）
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
                清除筛选
              </button>
            </div>
          </div>
        </div>
      </section>

      <section className="section">
        {loading && !data ? <LoadingState message="正在加载运行记录…" /> : null}
        {error ? (
          <ErrorState error={error} what="加载运行记录失败。" onRetry={reload} />
        ) : null}

        {data ? (
          <div className="card card--flush" style={{ opacity: loading ? 0.6 : 1 }}>
            {data.items.length === 0 ? (
              <EmptyState
                title="没有符合筛选条件的运行记录"
                hint={
                  dirty
                    ? '可以清除某个筛选条件，或把日期范围放宽一些。'
                    : '目前还没有任何运行记录。'
                }
              />
            ) : (
              <>
                <div className="table-wrap">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>主题</th>
                        <th>运行状态</th>
                        <th>审核状态</th>
                        <th>发布状态</th>
                        <th>创建时间</th>
                        <th title="该 Run 已到达终态的时间。未结束的 Run 没有这个值。">
                          完成时间
                        </th>
                        <th title="该 Run 存有错误信息">错误</th>
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
                <Pagination page={data.page} onChange={setPage} />
              </>
            )}
          </div>
        ) : null}
      </section>

      {openRunId ? (
        // 抽屉里发布成功后，这一行显示的状态就过期了，要跟着重取当前页。
        <RunDetailDrawer
          runId={openRunId}
          onClose={() => setOpenRunId(null)}
          onSettled={reload}
        />
      ) : null}
    </>
  )
}
