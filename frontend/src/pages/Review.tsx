import { useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'

import { api } from '../api/client'
import { Pagination } from '../components/Pagination'
import { ReviewPane } from '../components/ReviewPane'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { useToast } from '../components/ui/Toast'
import { useAsync } from '../hooks/useAsync'
import { useConsole } from '../hooks/useConsole'
import { useDebouncedValue } from '../hooks/useDebouncedValue'
import { usePageClamp } from '../hooks/usePageClamp'
import { EMPTY, dateTime, relativeTime } from '../utils/format'
import { autoReviewHeadline, originLabel } from '../utils/autoReview'
import { nextSelection, parseQueueOrder, type QueueOrder } from '../utils/queue'
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
 *
 * ## 右栏由谁决定"能不能审"
 *
 * 是**这条 Run 自己的状态**（`run_status === 'waiting_for_review'`），判据长在
 * `ReviewPane` 里。这一页从前用"选中的这条是否在当前页的 items 里"来判断，于是
 * `/review?run=<id>` 一旦指向别的页（运行记录抽屉的「去审核 →」、总览页的「审核 →」
 * 都是这么进来的），右栏就退化成只读——人明明点的是"去审核"，看到的却是一份不能审的
 * 详情。`selectedInQueue` 现在只剩一个用途：左栏的高亮，外加提示一句"这条不在本页"。
 *
 * ## 「挪到下一条」的那条边界
 *
 * 队列里只有这一条时没有下一条可挪。这时**保留当前这条**而不是清空选中项
 * （规则见 `utils/queue.ts`）—— 人刚在这里做完决定，最想看的就是决定之后的样子：
 * 编辑稿存下来了没有、现在什么状态、能不能发布。清空会把这份反馈一起抹掉。
 * 面板本身会由 `ReviewPane` 在那个时刻重取一次，翻成只读详情。
 *
 * ## 队列的搜索与排序
 *
 * 队列几十条深时（真实数据：56 条待审、每页 20 → 今天刚跑的那条在第 3 页）靠翻页
 * 找一个主题是不现实的，所以有搜索框；而"刚跑完就想审这一条"在默认的先进先出里
 * 排在最后一页，所以有排序切换。**默认顺序不变**——最老优先是"扫积压"的正确默认，
 * 切换只补上"刚跑完"那条路径（后端 `order` 参数与 `parseQueueOrder` 一一对应）。
 */

const TOPIC_DEBOUNCE_MS = 400

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

/**
 * 自动审阅在这条队列条目上的痕迹。
 *
 * 排队里必须让人**一眼看出**这条不是人排进来的，而且机器已经替他说过一句话。
 * 没有这个标记，人会把每条都当全新的从零审一遍——影子模式量到的一致率就永远
 * 没人看、也永远翻不了正，这一整套功能的度量来源就断了。
 *
 * 机器"不通过"用的是与"规则检查未通过"同一个红徽标样式，但**文案不同**：
 * 两者是两套判断（前者是自动发布四项判据的合取，后者是 checkpoint 里的内容闸门），
 * 一块红一块绿是正常组合，不能看起来像同一件事说了两遍。
 */
function QueueAuto({ item }: { item: ReviewQueueItem }) {
  if (!item.auto_review) return null
  const auto = item.auto_review
  return (
    <span
      className={auto.verdict === 'approve' ? 'guard guard--pass' : 'guard guard--fail'}
      title={auto.reasons.length ? auto.reasons.join('；') : '机器没有留下理由文本'}
    >
      {autoReviewHeadline(auto)}
    </span>
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
 *
 * 走全局 Toast（右下角固定位置、统一停留时间），不再由这个页面自己长一块绿色横条。
 * 同一个"操作成功了"的反馈在审核页是一种样子、在发布页是另一种样子，是这个界面
 * 之前最明显的"拼装感"来源之一。
 */
const DECISION_MESSAGES: Record<Decided['action'], string> = {
  approve: '已批准，该内容可以进入发布流程。',
  edit: '编辑已保存，该内容可以进入发布流程。',
  reject: '已拒绝，该内容不会进入发布流程。',
}

/** 排序开关的两个选项。文案与后端 `order` 取值一一对应。 */
const ORDER_OPTIONS: readonly { value: QueueOrder; label: string; title: string }[] = [
  {
    value: 'oldest',
    label: '最老优先',
    title: '按进入队列的时间正序：先来的先审。适合清积压。',
  },
  {
    value: 'newest',
    label: '最新优先',
    title: '最近创建的排最前。适合"刚跑完，就看这一条"。',
  },
]

export function Review() {
  const [searchParams, setSearchParams] = useSearchParams()

  // 翻页位置、当前选中的 Run、以及队列的筛选条件都放在 URL 里，不放在组件 state 里。
  // 审核是长时间坐着的活：刷新一下、从 Overview 的「审核 →」深链进来、或者按一下
  // 后退，都应该回到同一条 Run 与同一份筛选结果，而不是莫名其妙跳回队列第一条。
  const rawPage = Number(searchParams.get('page') ?? '1')
  const page = Number.isFinite(rawPage) && rawPage >= 1 ? Math.floor(rawPage) : 1
  const selected = searchParams.get('run')
  const topic = searchParams.get('topic') ?? ''
  const order = parseQueueOrder(searchParams.get('order'))

  // 只有 debounce 之后的值进请求依赖（与 Runs.tsx 同一个做法、同一个常量）：
  // 后端做的是模糊匹配，每敲一个字符发一次请求既没意义，也会让"后发出的请求先
  // 返回"更容易发生。输入框本身仍然是即时响应的——被延迟的只是查询条件。
  const debouncedTopic = useDebouncedValue(topic, TOPIC_DEBOUNCE_MS)
  // 全空格的搜索词按"没搜索"处理：否则发出去的是 `topic=%20%20`，命中一堆
  // 题目里带空格的 Run，看起来像是搜索坏了。
  const effectiveTopic = debouncedTopic.trim()

  const queue = useAsync(
    () => api.listReviews({ page, topic: effectiveTopic || undefined, order }),
    [page, effectiveTopic, order],
  )
  const { refresh } = useConsole()
  const toast = useToast()

  /**
   * 写回 URL。用 `replace` 而不是 `push`：连续审十条会留下十条历史记录，
   * 那时"后退"要按十次才出得去，比不能后退更烦人。真正的深链由 URL 本身承担
   * ——它是可复制的，刷新和收藏都能回到原处。取默认值的参数一律从 URL 删掉，
   * 保持它干净（`/review` 而不是 `/review?page=1&topic=&order=oldest&run=`）。
   *
   * **基于上一次的参数算，而不是基于这次渲染时的那一份。** 这一点是必须的：
   * 筛选变换要在同一个 tick 里写两个参数（新条件 + 回第 1 页），而 `usePageClamp`
   * 在"当前页空了"时也会写 `page`。基于过期的快照拼 URL，两次写入中后落地的那个
   * 会把前一个的结果覆盖掉——表现出来就是"清了搜索却停在第 3 页"。函数式更新让
   * 每次写入都接在最新状态上，这类竞争从结构上就不存在了（react-router 6.30 支持）。
   */
  function writeParams(next: {
    page?: number
    run?: string | null
    topic?: string
    order?: QueueOrder
  }) {
    setSearchParams(
      (previous) => {
        const params = new URLSearchParams(previous)
        if (next.page !== undefined) {
          if (next.page > 1) params.set('page', String(next.page))
          else params.delete('page')
        }
        if (next.run !== undefined) {
          if (next.run) params.set('run', next.run)
          else params.delete('run')
        }
        if (next.topic !== undefined) {
          if (next.topic) params.set('topic', next.topic)
          else params.delete('topic')
        }
        if (next.order !== undefined) {
          if (next.order !== 'oldest') params.set('order', next.order)
          else params.delete('order')
        }
        return params
      },
      { replace: true },
    )
  }

  // 两个 setter 的签名保持和原来的 useState 一致，下面用它们的地方不用改。
  const setPage = (next: number) => writeParams({ page: next })
  const setSelected = (next: string | null) => writeParams({ run: next })

  // **筛选一变就回第 1 页，而且和筛选条件写在同一处。**
  // 拆成"写条件"和"用 effect 回第一页"两件事的话，`usePageClamp` 与那个 effect
  // 会在同一 tick 里各写一次 `page`；`writeParams` 用函数式更新已经能扛住这种竞争，
  // 但让页面位置的变更只有一个来源仍然更清楚。
  function setTopic(next: string) {
    writeParams({ topic: next, page: 1 })
  }

  function setOrder(next: QueueOrder) {
    writeParams({ order: next, page: 1 })
  }

  // `queue.data?.items` 而不写成 `?? []`：后者每次渲染都产生一个新数组，
  // 会让下面那个 effect 的依赖每次都变，等于每渲染必跑。
  const items = queue.data?.items
  const contentMaxLength = queue.data?.content_max_length

  // 选中的这条是否还在**当前页**的待审队列里。它只用来决定左栏的高亮，以及
  // 下面那句"这条不在本页"的说明 —— 决定右栏给不给决定按钮的是 Run 自己的状态。
  // 深链进来的那条多半不在这里（它可能在第 3 页，也可能早就决定了）。
  const selectedInQueue = items?.some((item) => item.run_id === selected) ?? false

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

  /**
   * 决定提交成功。
   *
   * `topic` 由 `ReviewPane` 从它自己那份详情里带上来，**这里不查表**：从前是从当前页的
   * `items` 里找题目，而深链进来的那条（`?run=…`）多半不在当前页——找不到就退化成
   * "—：已批准"，一条什么也没说的提示。运行详情页那一份是唯一确定有的。
   */
  function handleDecided(action: Decided['action'], decidedTopic: string | null) {
    const list = items ?? []

    // 提示带上题目：连续审十条时，"已批准"单看不知道批的是哪一条。
    toast.success(`${decidedTopic ?? EMPTY}：${DECISION_MESSAGES[action]}`)
    // 下一条：被决定的这条会从队列里消失，所以它后面的那条会顶上来，位置不变。
    // 没有下一条时**保留刚决定的这条**，不清空 —— 见文件头与 `utils/queue.ts`。
    setSelected(nextSelection(list, selected))
    queue.reload()
    // 侧栏的待办计数与这里读的是同一份数据，必须一起更新，否则导航上还挂着刚处理掉的那个数。
    refresh()
  }

  return (
    <div className="workspace">
      <div>
        <div className="card card--flush">
          <div className="card__head">
            <div>
              <h3 className="card__title">待审核</h3>
              <p className="card__hint">Agent 已停下来等人做决定</p>
            </div>
            {queue.data ? (
              // 有搜索时改口径：同一个数字，一个说的是"队列里一共有多少"，
              // 另一个说的是"你搜的这个词命中了多少"，混淆会让人以为积压清空了。
              <span className="cell-muted">
                {effectiveTopic ? `筛选结果 ${queue.data.page.total} 条` : `共 ${queue.data.page.total} 条`}
              </span>
            ) : null}
          </div>

          {/* 筛选行**不**跟着下方队列条目的零内边距走：它属于"自己带内边距"的表格，
              所以给它一层 `card__body--padded`（见 app.css 里那条规则）。 */}
          <div className="card__body card__body--padded">
            <div className="filter-bar">
              <div className="field field--grow">
                <label className="field__label" htmlFor="review-topic">
                  搜索主题
                </label>
                <input
                  id="review-topic"
                  className="input"
                  type="search"
                  placeholder="输入主题里的文字"
                  value={topic}
                  onChange={(event) => setTopic(event.target.value)}
                />
              </div>
              <div className="field">
                <span className="field__label" id="review-order-label">
                  排序
                </span>
                <div className="segmented" role="group" aria-labelledby="review-order-label">
                  {ORDER_OPTIONS.map((option) => (
                    <button
                      key={option.value}
                      type="button"
                      className={
                        option.value === order ? 'segmented__item is-active' : 'segmented__item'
                      }
                      aria-pressed={option.value === order}
                      title={option.title}
                      onClick={() => setOrder(option.value)}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </div>

          {queue.loading && !queue.data ? <LoadingState message="正在加载待审核队列…" /> : null}
          {queue.error ? (
            <div className="card__body card__body--padded">
              <ErrorState
                error={queue.error}
                what="加载待审核队列失败。"
                onRetry={queue.reload}
              />
            </div>
          ) : null}

          {queue.data && items && items.length === 0 ? (
            // 两种"空"要说两种话：队列本来就空（好事，没有积压）与搜索没命中
            // （改一下词或清掉搜索就有）。用同一句"没有待审核的内容"会把后者
            // 说成前者 —— 人会以为积压清完了。
            effectiveTopic ? (
              <EmptyState
                title="没有匹配的主题"
                // 这里**不能**报一个数字。`page.total` 此刻必然为 0（否则这条空态
                // 根本不会渲染），而它说的是"命中了几条"，写进去等于告诉人
                // "队列里一共 0 条"—— 把一次搜索没命中说成积压清空。
                hint={`待审队列里没有题目含"${effectiveTopic}"的 Run。清掉搜索可以看到队列里的其他条目。`}
              />
            ) : (
              <EmptyState
                title="没有待审核的内容"
                hint="一旦 Agent 停下来等人工决定，新的 Run 就会出现在这里。"
              />
            )
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
                  {/* Agent 还没选定主题时 topic 是 null：显示 —，不要留一片空白。 */}
                  <div className="queue-item__title">
                    {/* 只有自动排进来的那条才带标记：给每条都挂一个"人工发起"，
                        等于把一个默认值印满整个列表，真正要看的那条反而淹没了。 */}
                    {item.origin === 'scheduled' ? (
                      <span className="tag tag--auto" title="由定时自动发布建出来的 Run">
                        {originLabel(item.origin)}
                      </span>
                    ) : null}
                    {item.topic ?? <span className="cell-muted">{EMPTY}</span>}
                  </div>
                  <div className="queue-item__meta">
                    {item.draft_preview ? (
                      <div className="table__clamp">{item.draft_preview}</div>
                    ) : (
                      <div className="cell-muted">未存草稿</div>
                    )}
                    {/* 机器**不通过**的理由必须在队列里就看得到，不能只在右栏。
                        这一列的全部意义是"人扫一眼就知道机器为什么拦了这一条"；
                        要通过点选才看得到，等于逼人把每一条都点开一遍。 */}
                    {item.auto_review?.reasons.length ? (
                      <ul className="auto-reasons">
                        {item.auto_review.reasons.map((reason, index) => (
                          <li key={index}>{reason}</li>
                        ))}
                      </ul>
                    ) : null}
                    <div className="row row--tight queue-item__foot">
                      {/* 相对时间给出"等了多久"，绝对时间在 title 里。
                          队列最容易出的事就是"有一条被漏了两天"，所以等待时长
                          是这个列表里唯一必须一直显示的辅助信息。 */}
                      <span title={dateTime(item.created_at)}>
                        等待 {relativeTime(item.created_at)}
                      </span>
                      <QueueStatus item={item} />
                      <QueueAuto item={item} />
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
      </div>

      <div>
        {/* 选中项不在左栏这一页时说明一句。两种来源都会走到这里：从运行记录 / 总览页
            深链进来（那条可能在第 3 页），以及刚决定完最后一条（它已经离开队列）。
            不说的话，左栏一个高亮都没有，看起来像是选中丢了。 */}
        {selected && queue.data && !selectedInQueue ? (
          <div className="cell-muted review-offpage">
            这条 Run 不在左栏当前这一页里 —— 可能是从别处深链进来的，也可能已经决定过了。
          </div>
        ) : null}

        {selected && contentMaxLength !== undefined ? (
          // 不再二选一：`ReviewPane` 自己按这条 Run 的状态决定给决定界面还是只读详情。
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
