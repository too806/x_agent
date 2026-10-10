import { useEffect, useState } from 'react'

import { api } from '../api/client'
import { NewRunDialog } from '../components/NewRunDialog'
import { Pagination } from '../components/Pagination'
import { RunDetailDrawer } from '../components/RunDetailDrawer'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { StatusBadge } from '../components/StatusBadge'
import { IconAlert, IconRefresh } from '../components/ui/icons'
import { useAsync } from '../hooks/useAsync'
import { useDebouncedValue } from '../hooks/useDebouncedValue'
import { usePageClamp } from '../hooks/usePageClamp'
import { RUN_STATUSES } from '../types/api'
import { originLabel } from '../utils/autoReview'
import {
  CUSTOM_RANGE_SEED_DAYS,
  TIME_RANGES,
  daysAgoDateString,
  rangeStartDate,
} from '../utils/dateRange'
import { EMPTY, dateTime, relativeTime, runStatusLabel, runTitle, shortId } from '../utils/format'
import type { TimeRangeValue } from '../utils/dateRange'

const TOPIC_DEBOUNCE_MS = 400

/**
 * 筛选条件。
 *
 * `range` 是时间筛选的**唯一入口**，`from`/`to` 只在 `range === 'custom'` 时有意义。
 * 把它们放在同一个对象里而不是用一个 `mode` 标志位加两个游离的 state，是为了
 * "清除筛选"能一把还原——否则很容易出现"日期框清空了、下拉还停在最近 30 天"
 * 这种自相矛盾的界面。
 */
interface Filters {
  status: string
  topic: string
  range: TimeRangeValue
  from: string
  to: string
}

const NO_FILTERS: Filters = { status: '', topic: '', range: '', from: '', to: '' }

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

/**
 * 运行记录 —— "Agent 跑过哪些次"的探索器。
 *
 * 列的取舍：
 *
 * - **主题**（可截断 + 悬停看全文）下面挂一行 Run ID。中文主题长度不可控，
 *   不截断的话一行能撑到屏幕外面；而 Run ID 是排查时唯一能把界面、日志、
 *   数据库对起来的东西，藏在抽屉里意味着每查一次要多点一下。
 * - **运行状态 / 审核状态 / 发布状态**三列并排。它们是三个不同权威：
 *   Run 状态说的是"Agent 走到哪了"，审核状态说的是"人做了什么决定"，
 *   发布状态来自发布记录、说的是"X 上到底发生了什么"。合并或省掉任何一个，
 *   都会让人拿其中一个去猜另外两个。
 * - **创建时间 / 完成时间**：绝对时间在上（用来对账），相对时间在下（用来判断
 *   新鲜度）。只给相对时间的话，"3 天前"是哪天还得心算。
 * - **操作**：显式的"查看"。这一版把整行的 `role="button"` 去掉了——
 *   一个 `role="button"` 的 `<tr>` 在无障碍树里不再是表格行，读屏用户会失去
 *   整个表头对应关系；而键盘用户本来就有这个按钮可达。
 */
export function Runs() {
  const [filters, setFilters] = useState<Filters>(NO_FILTERS)
  const [page, setPage] = useState(1)
  const [openRunId, setOpenRunId] = useState<string | null>(null)
  const [compact, setCompact] = useState(false)
  const [creating, setCreating] = useState(false)

  const debouncedTopic = useDebouncedValue(filters.topic, TOPIC_DEBOUNCE_MS)

  // 真正发给后端的时间边界。预设只算左端、右端留空（"到现在"），只有自定义才读
  // 那两个日期框。`rangeStartDate` 每次渲染重新算"今天"，所以页面跨过午夜也不会
  // 停在昨天的窗口上——而它的返回值在一天之内是稳定的字符串，不会把 useAsync
  // 的依赖搅成每渲染必跑。
  const effectiveFrom = filters.range === 'custom' ? filters.from : rangeStartDate(filters.range)
  const effectiveTo = filters.range === 'custom' ? filters.to : ''

  // 筛选条件一变就回到第 1 页。留在第 7 页会导致"筛选后一片空白"——那一页在新的
  // 结果集里根本不存在。
  useEffect(() => {
    setPage(1)
  }, [filters.status, debouncedTopic, effectiveFrom, effectiveTo])

  const { data, loading, error, reload } = useAsync(
    () =>
      api.listRuns({
        status: filters.status || undefined,
        topic: debouncedTopic || undefined,
        dateFrom: localDateToIso(effectiveFrom),
        dateTo: localDateToIso(effectiveTo, 1),
        page,
      }),
    [filters.status, debouncedTopic, effectiveFrom, effectiveTo, page],
  )

  const dirty =
    filters.status !== '' ||
    filters.topic !== '' ||
    filters.range !== '' ||
    filters.from !== '' ||
    filters.to !== ''

  /**
   * 切时间范围。
   *
   * 进「自定义区间」时给一个**看得见的**起点：从刚才那个预设的左端抄过来（之前是
   * "全部时间"则回退 30 天）。不这么做的话，两个空日期框和「全部时间」的效果完全
   * 一样，人会以为自己点错了；而悄悄在背后加一个窗口更糟——界面上必须看得到。
   */
  function setRange(next: TimeRangeValue) {
    setFilters((current) => {
      if (next !== 'custom') return { ...current, range: next }
      if (current.from) return { ...current, range: next }
      const seeded = rangeStartDate(current.range) || daysAgoDateString(CUSTOM_RANGE_SEED_DAYS)
      return { ...current, range: next, from: seeded }
    })
  }

  // 抽屉里发布 / 裁决成功后，这一行可能就不再满足当前筛选（比如按 rejected 过滤，
  // 而它被改成了别的状态）。空的尾页要退回去，否则会看到一句"没有匹配的 Run"，
  // 而前面几页明明还有。
  usePageClamp(page, setPage, data !== null && data.items.length === 0, data !== null)

  return (
    <>
      <section className="section">
        <div className="section__head">
          <h2 className="section__title">运行记录</h2>
          <div className="row row--tight">
            {/* 新建入口放在标题这一行，而不是页面别处：换主题（今天 AI、明天美股）
                的动作就是"再建一条 Run"，它比这一页上任何操作都高频。
                它是主按钮，其余控件保持次要——一眼能看出哪个是"开始一件事"。 */}
            <button
              type="button"
              className="btn btn--sm btn--primary"
              onClick={() => setCreating(true)}
              title="用一个新的题目或目标开始一次运行。它不会修改任何已有的 Run。"
            >
              + 新建 Run
            </button>
            {data ? (
              <span className="cell-muted">
                {dirty ? `筛选结果 ${data.page.total} 条` : `共 ${data.page.total} 条`}
              </span>
            ) : null}
            <div className="segmented" role="group" aria-label="表格密度">
              <button
                type="button"
                className={compact ? 'segmented__item' : 'segmented__item is-active'}
                aria-pressed={!compact}
                onClick={() => setCompact(false)}
              >
                舒适
              </button>
              <button
                type="button"
                className={compact ? 'segmented__item is-active' : 'segmented__item'}
                aria-pressed={compact}
                onClick={() => setCompact(true)}
              >
                紧凑
              </button>
            </div>
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

        <div className="card">
          <div className="card__body">
            {/* 搜索框放第一个：这是实际用得最多的一个条件（"那次讲 XX 的跑了没"）。
                时间筛选从两个常驻日期框收成一个下拉，只有真的选「自定义区间」才
                展开日期——多数时候人只是想知道"最近跑过什么"。 */}
            <div className="filter-bar">
              <div className="field field--grow">
                <label className="field__label" htmlFor="run-topic">
                  搜索主题
                </label>
                <input
                  id="run-topic"
                  className="input"
                  type="search"
                  placeholder="输入主题里的文字"
                  value={filters.topic}
                  onChange={(event) =>
                    setFilters((current) => ({ ...current, topic: event.target.value }))
                  }
                />
              </div>

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

              <div className="field">
                <label className="field__label" htmlFor="run-range">
                  创建时间
                </label>
                <select
                  id="run-range"
                  className="select"
                  value={filters.range}
                  onChange={(event) => setRange(event.target.value as TimeRangeValue)}
                >
                  {TIME_RANGES.map((range) => (
                    <option key={range.value} value={range.value}>
                      {range.label}
                    </option>
                  ))}
                </select>
              </div>

              {filters.range === 'custom' ? (
                <>
                  <div className="field">
                    <label className="field__label" htmlFor="run-from">
                      从
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
                      到（含当天）
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
                </>
              ) : null}

              <div className="filter-bar__spacer" />

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
        {loading && !data ? <LoadingState message="正在加载运行记录…" variant="table" /> : null}
        {error ? <ErrorState error={error} what="加载运行记录失败。" onRetry={reload} /> : null}

        {data ? (
          <div className={loading ? 'card card--flush is-loading' : 'card card--flush'}>
            {data.items.length === 0 ? (
              <EmptyState
                title={dirty ? '没有符合筛选条件的运行记录' : '还没有任何运行记录'}
                hint={
                  dirty ? (
                    <>当前的筛选条件没有匹配到任何 Run。可以清除某个条件，或把日期范围放宽一些。</>
                  ) : (
                    <>
                      每当 Agent 开始一次新的运行，这里就会多一条记录。
                      如果预期这里应该有内容，先确认后端服务在运行。
                    </>
                  )
                }
              />
            ) : (
              <>
                <div className="table-wrap">
                  <table className={compact ? 'table table--compact' : 'table'}>
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
                        <th>
                          <span className="sr-only">操作</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.items.map((run) => (
                        <tr
                          key={run.run_id}
                          className={
                            run.run_id === openRunId
                              ? 'table__row--clickable table__row--selected'
                              : 'table__row--clickable'
                          }
                          onClick={() => setOpenRunId(run.run_id)}
                        >
                          <td className="table__topic">
                            {/* 主题最多两行，超出打点，完整值在悬停提示和抽屉里。
                                `table__clamp` 自己就是 `-webkit-box`，不需要再包一层。 */}
                            <span className="table__clamp" title={runTitle(run) ?? undefined}>
                              {runTitle(run) ?? <span className="cell-muted">{EMPTY}</span>}
                            </span>
                            {/* Run ID：等宽、只显示前 8 位，完整值在悬停和抽屉里。
                                等宽字体 + 定宽截断是刻意的——UUID 一旦完整铺开，
                                这一列会把主题和状态一起挤窄。 */}
                            <div className="table__secondary mono" title={run.run_id}>
                              {shortId(run.run_id)}
                            </div>
                            {/* 来源标记只给"自动调度"：当日额度、影子一致率的分母、
                                reaper 该收谁，三件事都靠这个字段界定范围，而它们全都
                                发生在**不在待审队列**的 Run 上（已发布的、被收走的），
                                所以列表里也得看得到。给每条都挂"人工发起"等于把一个
                                默认值印满整列，真正要找的那条反而淹没。

                                单独一行、不塞进上面那个 div：那个 div 是 `max-width:
                                14ch` 的等宽截断容器，塞进去会把 Run ID 挤到省略号。 */}
                            {run.origin === 'scheduled' ? (
                              <div className="table__secondary">
                                <span className="tag">{originLabel(run.origin)}</span>
                              </div>
                            ) : null}
                          </td>
                          <td>
                            <StatusBadge status={run.run_status} kind="run" />
                            {run.error ? (
                              <span className="cell-error" title={run.error}>
                                <IconAlert size={12} />
                                有错误
                              </span>
                            ) : null}
                          </td>
                          <td>
                            <StatusBadge status={run.review_status} kind="review" />
                          </td>
                          <td>
                            {/* 发布状态为 `—` 表示**没有发布记录**，不是"还没发"这个
                                笼统的说法，更不能用左边的 Run 状态去顶替。 */}
                            <StatusBadge status={run.publish_status} kind="publish" />
                          </td>
                          <td className="cell-nowrap">
                            <div className="table__time-abs">{dateTime(run.created_at)}</div>
                            <div className="table__time-rel">{relativeTime(run.created_at)}</div>
                          </td>
                          <td className="cell-nowrap">
                            <div className="table__time-abs">{dateTime(run.finished_at)}</div>
                            {run.finished_at ? (
                              <div className="table__time-rel">{relativeTime(run.finished_at)}</div>
                            ) : null}
                          </td>
                          <td className="table__actions">
                            <button
                              type="button"
                              className="btn btn--sm"
                              onClick={(event) => {
                                // 整行可点，但按钮不该冒泡成第二遍——点按钮只开一次。
                                event.stopPropagation()
                                setOpenRunId(run.run_id)
                              }}
                            >
                              查看
                            </button>
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

      {creating ? (
        <NewRunDialog
          onClose={() => setCreating(false)}
          // 创建成功之后：关掉弹窗、回到第 1 页重取列表、把**新**那条 Run 打开。
          //
          // 回第 1 页是因为列表按创建时间倒序，新 Run 一定在那里；不回的话，用户
          // 停在第 3 页会看不到刚才建的东西，以为没成功。
          //
          // **不动筛选条件**：清了筛选会让"我正在看什么"被悄悄换掉。极端情况下
          // （筛选条件恰好排除了新 Run）列表里看不到它，但下面打开的抽屉是按 id
          // 取的详情，照样能看——这比擅自改动用户的筛选条件好。
          //
          // 直接打开详情，而不是只发个提示：这条 Run 现在停在等待人工审核，
          // 人要看的下一件事就是它的草稿。
          onCreated={(runId) => {
            setCreating(false)
            setPage(1)
            reload()
            setOpenRunId(runId)
          }}
        />
      ) : null}
    </>
  )
}
