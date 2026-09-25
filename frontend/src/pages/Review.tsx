import { useEffect, useState } from 'react'

import { api } from '../api/client'
import { Pagination } from '../components/Pagination'
import { ReviewPane } from '../components/ReviewPane'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { useAsync } from '../hooks/useAsync'
import { useConsole } from '../hooks/useConsole'
import { EMPTY, relativeTime } from '../utils/format'
import type { ReviewQueueItem } from '../types/api'

/**
 * 审核工作台：左侧待审队列，右侧当前 Run 的全部材料。
 *
 * 做成一屏两栏而不是"列表 → 详情页"，是因为审核是**连续决策**：看完一条立刻处理
 * 下一条。每处理一条就跳一次页面会把队列位置、翻页状态全丢掉。
 *
 * 队列分页在服务器端。决定提交后这里只做两件事：重新取当前页、把选中项挪到下一条。
 * 刻意**不**在本地把已决定的条目从数组里删掉就完事——那样翻页总数会与实际不符，
 * 而且别处（另一个控制台 / API）的决定就不会反映出来。
 */

function QueueStatus({ item }: { item: ReviewQueueItem }) {
  if (item.guardrail_passed === null) {
    return (
      <span className="guard guard--unknown" title="Not readable from the checkpoint">
        guardrail {EMPTY}
      </span>
    )
  }
  return item.guardrail_passed ? (
    <span className="guard guard--pass">guardrail pass</span>
  ) : (
    <span className="guard guard--fail">guardrail fail</span>
  )
}

export function Review() {
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<string | null>(null)
  const [justDecided, setJustDecided] = useState<string | null>(null)

  const queue = useAsync(() => api.listReviews(page), [page])
  const { refresh } = useConsole()

  // `queue.data?.items` 而不写成 `?? []`：后者每次渲染都产生一个新数组，
  // 会让下面那个 effect 的依赖每次都变，等于每渲染必跑。
  const items = queue.data?.items
  const contentMaxLength = queue.data?.content_max_length

  // 自动选中第一条：待审队列里通常只有个位数条目，进来就让人多点一次没有意义。
  // 只在"还没有选中项"时生效，所以人手动选过的不会被覆盖。
  useEffect(() => {
    if (selected === null && items && items.length > 0) {
      setSelected(items[0]?.run_id ?? null)
    }
  }, [selected, items])

  // 处理完当前页最后一条后，这一页会变成空的。留在空页上会让人以为队列已清空，
  // 而后面的页里还有待审的 Run。
  useEffect(() => {
    if (queue.data && queue.data.items.length === 0 && page > 1) {
      setPage(page - 1)
    }
  }, [queue.data, page])

  function handleDecided() {
    const list = items ?? []
    const index = list.findIndex((item) => item.run_id === selected)
    const topic = list[index]?.topic ?? null
    // 下一条：被决定的这条会从队列里消失，所以它后面的那条会顶上来，位置不变。
    const next = index >= 0 ? (list[index + 1]?.run_id ?? null) : null

    setJustDecided(topic)
    setSelected(next)
    queue.reload()
    // 侧栏的待办计数与这里读的是同一份数据，必须一起更新，否则导航上还挂着刚处理掉的那个数。
    refresh()
  }

  return (
    <div className="workspace">
      <div>
        <div className="card card--flush">
          <div className="card__body" style={{ paddingBottom: 8 }}>
            <div className="row row--between">
              <strong>Waiting for review</strong>
              <span className="cell-muted num">{queue.data?.page.total ?? EMPTY}</span>
            </div>
          </div>

          {queue.loading && !queue.data ? <LoadingState message="Loading queue…" /> : null}
          {queue.error ? (
            <div className="card__body">
              <ErrorState
                error={queue.error}
                what="Failed to load the queue."
                onRetry={queue.reload}
              />
            </div>
          ) : null}

          {queue.data && items && items.length === 0 ? (
            <EmptyState
              title="Nothing to review"
              hint="New runs show up here as soon as the agent pauses for a human decision."
            />
          ) : null}

          {items && items.length > 0 ? (
            <div className="queue-list">
              {items.map((item) => (
                <button
                  key={item.run_id}
                  type="button"
                  className={`queue-item${item.run_id === selected ? ' queue-item--active' : ''}`}
                  onClick={() => setSelected(item.run_id)}
                >
                  <div className="queue-item__title">{item.topic}</div>
                  <div className="queue-item__meta">
                    {item.draft_preview ? (
                      <div className="clamp-2">{item.draft_preview}</div>
                    ) : (
                      <div>no draft stored</div>
                    )}
                    <div className="row" style={{ gap: 8, marginTop: 4 }}>
                      <span title={item.created_at}>{relativeTime(item.created_at)}</span>
                      <QueueStatus item={item} />
                    </div>
                  </div>
                </button>
              ))}
            </div>
          ) : null}

          {queue.data && queue.data.page.pages > 1 ? (
            <Pagination page={queue.data.page} onChange={setPage} itemLabel="runs" />
          ) : null}
        </div>

        {justDecided ? (
          <div className="notice notice--ok" style={{ marginTop: 12 }}>
            <div className="row row--between">
              <span>
                Decision recorded for <strong>{justDecided}</strong>.
              </span>
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => setJustDecided(null)}
              >
                Dismiss
              </button>
            </div>
          </div>
        ) : null}
      </div>

      <div>
        {selected && contentMaxLength !== undefined ? (
          <ReviewPane
            // key 让切换 Run 时组件彻底重建：上一条的编辑框、半填的理由、
            // 上一次的错误都会随之清空，不依赖 effect 里的手工清理。
            key={selected}
            runId={selected}
            contentMaxLength={contentMaxLength}
            onDecided={handleDecided}
          />
        ) : queue.loading ? (
          <LoadingState message="Loading…" />
        ) : (
          <EmptyState
            title="No run selected"
            hint="Pick a run from the queue on the left to see its research, draft and guardrail result."
          />
        )}
      </div>
    </div>
  )
}
