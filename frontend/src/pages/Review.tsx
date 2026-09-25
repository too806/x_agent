import { useEffect, useState } from 'react'

import { api } from '../api/client'
import { Pagination } from '../components/Pagination'
import { ReviewPane } from '../components/ReviewPane'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { useAsync } from '../hooks/useAsync'
import { useConsole } from '../hooks/useConsole'
import { usePageClamp } from '../hooks/usePageClamp'
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
      <span className="guard guard--unknown" title="无法从 checkpoint 读出结果">
        规则检查 {EMPTY}
      </span>
    )
  }
  return item.guardrail_passed ? (
    <span className="guard guard--pass">规则检查通过</span>
  ) : (
    <span className="guard guard--fail">规则检查未通过</span>
  )
}

/** 一次决定。`action` 决定提交成功后提示哪一句话（见 `DECISION_MESSAGES`）。 */
interface Decided {
  topic: string
  action: 'approve' | 'edit' | 'reject'
}

/**
 * 决定提交成功后的提示。
 *
 * 三条路径给三句不同的话，而不是一句通用的"决定已记录"：Edit 的人需要知道**改稿
 * 被存下来了**，Reject 的人需要知道**这条不会再发出去**——这两件事是操作者按下
 * 按钮后唯一想确认的结果，用一句通用文案把它们的差别抹掉，等于没提示。
 */
const DECISION_MESSAGES: Record<Decided['action'], string> = {
  approve: '已批准，该内容可以进入发布流程。',
  edit: '编辑已保存，该内容可以进入发布流程。',
  reject: '已拒绝，该内容不会进入发布流程。',
}

export function Review() {
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<string | null>(null)
  const [justDecided, setJustDecided] = useState<Decided | null>(null)

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
  usePageClamp(page, setPage, items !== undefined && items.length === 0, queue.data !== null)

  function handleDecided(action: Decided['action']) {
    const list = items ?? []
    const index = list.findIndex((item) => item.run_id === selected)
    const topic = list[index]?.topic ?? null
    // 下一条：被决定的这条会从队列里消失，所以它后面的那条会顶上来，位置不变。
    const next = index >= 0 ? (list[index + 1]?.run_id ?? null) : null

    setJustDecided({ topic: topic ?? EMPTY, action })
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
              <strong>待审核</strong>
              <span className="cell-muted num">{queue.data?.page.total ?? EMPTY}</span>
            </div>
          </div>

          {queue.loading && !queue.data ? <LoadingState message="正在加载待审核队列…" /> : null}
          {queue.error ? (
            <div className="card__body">
              <ErrorState
                error={queue.error}
                what="加载待审核队列失败。"
                onRetry={queue.reload}
              />
            </div>
          ) : null}

          {queue.data && items && items.length === 0 ? (
            <EmptyState
              title="没有待审核的内容"
              hint="一旦 Agent 停下来等人工决定，新的 Run 就会出现在这里。"
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
                      <div>未存草稿</div>
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
            <Pagination page={queue.data.page} onChange={setPage} />
          ) : null}
        </div>

        {justDecided ? (
          <div className="notice notice--ok" style={{ marginTop: 12 }}>
            <div className="row row--between">
              <span>
                <strong>{justDecided.topic}</strong>：{DECISION_MESSAGES[justDecided.action]}
              </span>
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => setJustDecided(null)}
              >
                知道了
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
          <LoadingState message="加载中…" />
        ) : (
          <EmptyState
            title="尚未选择 Run"
            hint="从左侧队列里点选一条 Run，这里会显示它的研究内容、草稿与规则检查结果。"
          />
        )}
      </div>
    </div>
  )
}
