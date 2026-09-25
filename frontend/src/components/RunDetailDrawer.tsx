import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api, xPostUrl } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { useConsole } from '../hooks/useConsole'
import { Drawer } from './Drawer'
import { PublishControl } from './PublishAction'
import { ErrorState, LoadingState } from './States'
import { StatusBadge } from './StatusBadge'
import { isUnresolved } from '../types/api'
import {
  EMPTY,
  agentActionLabel,
  dateTime,
  draftStatusLabel,
  duration,
  text,
} from '../utils/format'
import type {
  AdminRunDetail,
  DecisionTraceEntry,
  PublishRecord,
  ResearchItemRecord,
} from '../types/api'

/**
 * Run 详情抽屉。
 *
 * 这是控制台里唯一读 LangGraph checkpoint 的地方（后端 `run_detail` 同理）。
 * 界面必须让使用者随时分得清**每个字段的来源**，因为两个来源的可信度不同：
 *
 *   - 数据库（research / draft / review / published_post）是持久事实
 *   - checkpoint 里的 guardrail / human_action / final_content 是运行态快照，
 *     可能因为 checkpoint 被清理或 Run 还在跑而**整体读不到**
 *
 * 后者读不到时显示 `—` 并说明原因，绝不用数据库里的近似值去顶替。
 */

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="meta">
      <div className="meta__label">{label}</div>
      <div className="meta__value">{children}</div>
    </div>
  )
}

function Section({
  title,
  hint,
  children,
}: {
  title: string
  hint?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section className="detail-section">
      <div className="section__head">
        <h3 className="section__title">{title}</h3>
        {hint ? <span className="section__hint">{hint}</span> : null}
      </div>
      {children}
    </section>
  )
}

const RESEARCH_PREVIEW = 3

function ResearchRow({ item }: { item: ResearchItemRecord }) {
  return (
    <li className="research-item">
      <div className="row row--between">
        <strong>{item.author_username ? `@${item.author_username}` : '作者未知'}</strong>
        <span className="cell-muted">
          {item.likes} 点赞 · {item.reposts} 转发 · {item.replies} 回复
          {item.published_at ? ` · ${dateTime(item.published_at)}` : ''}
        </span>
      </div>
      <div className="body-block">{item.content}</div>
      <div className="cell-muted">
        {item.source_url ? (
          <a href={item.source_url} target="_blank" rel="noreferrer noopener">
            {item.platform_post_id}
          </a>
        ) : (
          item.platform_post_id
        )}
      </div>
    </li>
  )
}

function TraceRow({ entry }: { entry: DecisionTraceEntry }) {
  return (
    <li className="trace-item">
      <div className="row row--between">
        <strong>
          <span className="trace-item__step">第 {entry.step} 步</span>
          {agentActionLabel(entry.action)}
        </strong>
        <span className="cell-muted">{dateTime(entry.created_at)}</span>
      </div>
      {/* 为什么这么做。这是时间线上唯一一列**由 Agent 自己写**的字段，
          其余都是系统记录的客观事实。 */}
      <div className="body-block">{entry.reason}</div>
      {entry.target ? <div className="cell-muted">作用于：{entry.target}</div> : null}
      {/* 存的是"当时手上有什么"，不是完整 prompt —— 所以这两行很短，可以全列出来。 */}
      <div className="cell-muted">当时手里：{entry.input_summary}</div>
      <div className="cell-muted">
        结果：{entry.result_summary || '（还没回填）'}
      </div>
    </li>
  )
}

/** Agent 决策轨迹。刻意只做**最小版本**（工作单第十五节：后端先提供数据，
 *  控制台不因此大改）。 */
function AgentTraceSection({ detail }: { detail: AdminRunDetail }) {
  // 没有 goal 也没有轨迹的 Run（也就是 Phase 5A 之前的全部 Run）看到的东西
  // 与从前**逐像素一致**：这一段根本不出现。
  if (!detail.goal && detail.decision_trace.length === 0) return null

  return (
    <Section
      title="Agent 决策轨迹"
      hint={
        detail.decision_trace.length
          ? `${detail.decision_trace.length} 步，按步号排序`
          : undefined
      }
    >
      {detail.decision_trace.length === 0 ? (
        <div className="notice">
          这个 Run 没有 Agent 决策记录。直接给主题的 Run 本来就没有；Agent 模式的 Run
          则可能是第一步就被上游挡住，还没落下记录。
        </div>
      ) : (
        <ol className="trace-list">
          {detail.decision_trace.map((entry) => (
            <TraceRow key={entry.step} entry={entry} />
          ))}
        </ol>
      )}
    </Section>
  )
}

function PublishBlock({ publish }: { publish: PublishRecord }) {
  return (
    <div className="stack stack--tight">
      <div className="drawer__metagrid">
        <Field label="发布状态">
          <StatusBadge status={publish.status} kind="publish" />
        </Field>
        <Field label="发布时间">{dateTime(publish.published_at)}</Field>
        <Field label="记录更新时间">{dateTime(publish.updated_at)}</Field>
        <Field label="X 帖子 ID">
          {publish.x_post_id ? (
            <a
              href={xPostUrl(publish.x_post_id)}
              target="_blank"
              rel="noreferrer noopener"
              className="mono"
            >
              {publish.x_post_id}
            </a>
          ) : (
            // 没有 id 不代表失败：publish_unknown 的典型形态就是"发出去过，但拿不到 id"
            EMPTY
          )}
        </Field>
      </div>

      {publish.last_error ? (
        <div className="notice notice--danger">
          <div className="notice__title">最后错误</div>
          <div className="body-block body-block--mono">{publish.last_error}</div>
        </div>
      ) : null}

      <div>
        <div className="meta__label">实际发出的正文</div>
        <div className="body-block">{publish.body ? text(publish.body) : EMPTY}</div>
      </div>
    </div>
  )
}

export function DetailBody({ detail, onSettled }: { detail: AdminRunDetail; onSettled: () => void }) {
  const [showAllResearch, setShowAllResearch] = useState(false)
  const { draft, review, publish } = detail

  // 可发布 = 审核已通过 且 没有在途/未知的发布记录。`failed` 是允许的：
  // 明确的失败意味着"确定没发出去"，重发是安全的（后端也会把 Run 退回 approved/edited）。
  const canPublish =
    (detail.run_status === 'approved' || detail.run_status === 'edited') &&
    (publish === null || publish.status === 'failed')

  // 将要发出的正文。**必须与后端的取法一致**：approve 用原稿、其余（edit）用改写稿
  // （见 app/services/publish.py 里 `review.status == "approve" ? original_content : edited_content`）。
  // 这里只是为了让人在确认框里核对，权威始终在后端。
  const publishableBody = review
    ? review.action === 'approve'
      ? review.original_content
      : review.edited_content
    : null

  // 时长只能由 started_at / finished_at 算出。两者缺一就说明这个 Run 还没到终态，
  // 或者根本没有 started_at —— 这时显示 `—`，不用 created_at 凑一个似是而非的数。
  const seconds =
    detail.started_at && detail.finished_at
      ? (new Date(detail.finished_at).getTime() - new Date(detail.started_at).getTime()) / 1000
      : null

  const research = showAllResearch
    ? detail.research_items
    : detail.research_items.slice(0, RESEARCH_PREVIEW)

  return (
    <>
      <div className="drawer__metagrid">
        <Field label="运行状态">
          <StatusBadge status={detail.run_status} kind="run" />
        </Field>
        <Field label="审核决定">
          <StatusBadge status={detail.review_status} kind="review" />
        </Field>
        <Field label="发布状态">
          <StatusBadge status={detail.publish_status} kind="publish" />
        </Field>
        <Field label="运行时长">{duration(seconds)}</Field>
        <Field label="创建时间">{dateTime(detail.created_at)}</Field>
        <Field label="开始时间">{dateTime(detail.started_at)}</Field>
        <Field label="完成时间">{dateTime(detail.finished_at)}</Field>
        <Field label="Run ID">
          <span className="mono">{detail.run_id}</span>
        </Field>
        {/* 只有 Agent 模式的 Run 才有目标。这里**不与 topic 合并**：goal 是人给的
            输入，topic 是 Agent 从中选出的结果，合成一个字段就分不清"Agent 选了没"。 */}
        {detail.goal ? (
          <Field label="目标">
            <span title={detail.goal}>{detail.goal}</span>
          </Field>
        ) : null}
      </div>

      {detail.error ? (
        <div className="notice notice--danger" style={{ marginTop: 16 }}>
          <div className="notice__title">Run 错误</div>
          <div className="body-block body-block--mono">{detail.error}</div>
        </div>
      ) : null}

      <AgentTraceSection detail={detail} />

      <Section title="规则检查" hint="来自 LangGraph checkpoint">
        {detail.guardrail_passed === null ? (
          <div className="notice">
            规则检查结果无法从 checkpoint 读出。这<strong>不等于</strong>通过 —— checkpoint
            要么不存在，要么没有带着这个 Run 的状态。
          </div>
        ) : detail.guardrail_passed ? (
          <div className="notice notice--ok">规则检查通过。</div>
        ) : (
          <div className="notice notice--danger">
            <div className="notice__title">规则检查未通过</div>
            {detail.guardrail_reasons.length ? (
              <ul className="reason-list">
                {detail.guardrail_reasons.map((reason, index) => (
                  <li key={index}>{reason}</li>
                ))}
              </ul>
            ) : (
              <div>后端没有记录原因文本。</div>
            )}
          </div>
        )}
      </Section>

      <Section title="内容">
        <div className="stack stack--tight">
          <div>
            <div className="meta__label">
              最终内容 <span className="cell-muted">（来自 checkpoint）</span>
            </div>
            <div className="body-block">{detail.final_content ?? EMPTY}</div>
          </div>

          {review ? (
            <div>
              <div className="meta__label row">
                人工决定
                <StatusBadge status={review.action} kind="review" />
                <span className="cell-muted">{dateTime(review.reviewed_at)}</span>
              </div>
              <div className="meta__label" style={{ marginTop: 6 }}>
                原始内容
              </div>
              <div className="body-block">{review.original_content}</div>
              {review.edited_content !== null ? (
                <>
                  <div className="meta__label" style={{ marginTop: 6 }}>
                    编辑后的内容
                    {review.action === 'edit' ? '（这条才是实际发布的）' : ''}
                  </div>
                  <div className="body-block">{review.edited_content}</div>
                </>
              ) : null}
              {review.reject_reason ? (
                <>
                  <div className="meta__label" style={{ marginTop: 6 }}>
                    拒绝原因
                  </div>
                  <div className="body-block">{review.reject_reason}</div>
                </>
              ) : null}
            </div>
          ) : (
            <div className="cell-muted">这个 Run 还没有人工决定记录。</div>
          )}

          {draft ? (
            <div>
              <div className="meta__label">
                草稿 v{draft.version} · {draftStatusLabel(draft.status)}
                {draft.model_name ? ` · ${draft.model_name}` : ''}
                {draft.prompt_version ? ` · 提示词 ${draft.prompt_version}` : ''}
                {draft.generated_at ? ` · ${dateTime(draft.generated_at)}` : ''}
              </div>
              <div className="body-block">{draft.content}</div>
            </div>
          ) : (
            <div className="cell-muted">这个 Run 没有存下草稿。</div>
          )}
        </div>
      </Section>

      <Section
        title="研究内容"
        hint={detail.research_items.length ? `${detail.research_items.length} 条` : undefined}
      >
        {detail.research_items.length === 0 ? (
          <div className="cell-muted">这个 Run 没有存下研究内容。</div>
        ) : (
          <>
            <ul className="research-list">
              {research.map((item) => (
                <ResearchRow key={item.item_id} item={item} />
              ))}
            </ul>
            {detail.research_items.length > RESEARCH_PREVIEW ? (
              <button
                type="button"
                className="btn btn--sm"
                onClick={() => setShowAllResearch((value) => !value)}
              >
                {showAllResearch ? '收起' : `展开全部 ${detail.research_items.length} 条`}
              </button>
            ) : null}
          </>
        )}
      </Section>

      <Section title="发布">
        {publish ? (
          <PublishBlock publish={publish} />
        ) : (
          <div className="notice">
            这个 Run <strong>没有发布记录</strong>，没有内容发到 X。上面的运行状态<strong>不能</strong>
            当作发布结果来读。
          </div>
        )}

        {/* 发布入口只在**确实可以发布**时出现：Run 已通过审核（approved / edited），
            且没有在途或结果未知的发布记录。

            publishing / publish_unknown 一律不给按钮 —— Phase 4a 的整个设计就是让这
            两种状态只能由人在 X 上亲眼确认后裁决（去 Published 页），任何"再发一次"
            的快捷方式都会绕过那道判断，直接变成重复推文。 */}
        {canPublish ? (
          <div style={{ marginTop: 12 }}>
            <PublishControl runId={detail.run_id} body={publishableBody} onSettled={onSettled} />
          </div>
        ) : null}

        {isUnresolved(detail.publish_status) ? (
          <div className="notice notice--warn" style={{ marginTop: 12 }}>
            这个 Run 有一次<strong>结果未定的发布尝试</strong>。请先到 X 上确认账号，再到
            <Link to="/published?status=__unresolved__">发布记录</Link>
            页裁决 —— 这个抽屉不会代你发布。
          </div>
        ) : null}
      </Section>

      <div className="cell-muted" style={{ marginTop: 16 }}>
        这里存的是审计记录，不是 X 的实时镜像 —— 帖子之后在 X 上被删掉，这里也不会跟着变。
        发布状态才是权威字段，上面的运行状态只是它的一个镜像视图。
      </div>
    </>
  )
}

/**
 * Run 详情（不带抽屉外壳）。
 *
 * 审核工作台在「刚决定的这条 Run 已经离开待审队列、而且后面没有下一条」时需要它。
 * 这时右侧不能空掉 —— 人刚做的决定、存下来的编辑稿、以及发布入口都在这上面 ——
 * 也不适合弹一个盖住队列的抽屉。渲染的内容与抽屉里**完全一致**，两边共用同一个
 * `DetailBody`，所以不会出现"抽屉里看得到、工作台上看不到"的字段。
 */
export function RunDetailPanel({
  runId,
  onSettled: notifyParent,
}: {
  runId: string
  onSettled?: () => void
}) {
  const { data, loading, error, reload } = useAsync(() => api.getRunDetail(runId), [runId])
  const { refresh } = useConsole()

  function onSettled() {
    reload()
    refresh()
    notifyParent?.()
  }

  if (loading) return <LoadingState message="正在加载这个 Run…" />
  if (error) return <ErrorState error={error} what="加载该 Run 失败。" onRetry={reload} />
  if (!data) return null

  return (
    <div className="stack">
      <div className="card">
        <div className="card__body">
          <h2 className="drawer__title">{data.topic ?? 'Run 详情'}</h2>
          <div className="row" style={{ marginTop: 8 }}>
            <StatusBadge status={data.run_status} kind="run" />
            <span className="cell-muted mono" title="Run ID">
              {data.run_id}
            </span>
          </div>
          <div className="cell-muted" style={{ marginTop: 8 }}>
            这个 Run 已经决定了，不再出现在左侧待审队列里。下面是它的完整记录。
          </div>
        </div>
      </div>
      <div className="card">
        <div className="card__body">
          <DetailBody detail={data} onSettled={onSettled} />
        </div>
      </div>
    </div>
  )
}

export function RunDetailDrawer({
  runId,
  onClose,
  onSettled: notifyParent,
}: {
  runId: string
  onClose: () => void
  /**
   * 抽屉内发生写操作后额外通知持有这个抽屉的页面。
   *
   * 抽屉自己会重取详情、也会刷新侧栏计数，但它**不知道背后那张表的存在**：在发布记录
   * 页打开抽屉、在里面重发或裁决成功后，那一行背后的列表仍然是旧的。列表的 reload
   * 只有页面手里才有，所以要由页面传进来。
   */
  onSettled?: () => void
}) {
  const { data, loading, error, reload } = useAsync(() => api.getRunDetail(runId), [runId])
  const { refresh } = useConsole()

  // 发过一次之后要同时刷新：抽屉里的详情、侧栏的待裁决计数、以及页面上的列表。
  function onSettled() {
    reload()
    refresh()
    notifyParent?.()
  }

  return (
    <Drawer
      onClose={onClose}
      title={
        <>
          {/* 题目还没选出来时退回中性标题，不拿 goal 顶替（见 DetailBody 里的说明）。 */}
          <h2 className="drawer__title">{data?.topic ?? 'Run 详情'}</h2>
          <div className="cell-muted mono">{runId}</div>
        </>
      }
    >
      {loading ? <LoadingState message="正在加载 Run…" /> : null}
      {error ? <ErrorState error={error} what="加载该 Run 失败。" onRetry={reload} /> : null}
      {data && !loading ? <DetailBody detail={data} onSettled={onSettled} /> : null}
    </Drawer>
  )
}
