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
import { EMPTY, dateTime, duration, text } from '../utils/format'
import type { AdminRunDetail, PublishRecord, ResearchItemRecord } from '../types/api'

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
        <strong>{item.author_username ? `@${item.author_username}` : 'unknown author'}</strong>
        <span className="cell-muted">
          {item.likes} likes · {item.reposts} reposts · {item.replies} replies
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

function PublishBlock({ publish }: { publish: PublishRecord }) {
  return (
    <div className="stack stack--tight">
      <div className="drawer__metagrid">
        <Field label="Publish status">
          <StatusBadge status={publish.status} kind="publish" />
        </Field>
        <Field label="Published at">{dateTime(publish.published_at)}</Field>
        <Field label="Record updated">{dateTime(publish.updated_at)}</Field>
        <Field label="X post">
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
          <div className="notice__title">Last error</div>
          <div className="body-block body-block--mono">{publish.last_error}</div>
        </div>
      ) : null}

      <div>
        <div className="meta__label">Body actually sent</div>
        <div className="body-block">{publish.body ? text(publish.body) : EMPTY}</div>
      </div>
    </div>
  )
}

function DetailBody({ detail, onSettled }: { detail: AdminRunDetail; onSettled: () => void }) {
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
        <Field label="Run status">
          <StatusBadge status={detail.run_status} kind="run" />
        </Field>
        <Field label="Review decision">
          <StatusBadge status={detail.review_status} kind="review" />
        </Field>
        <Field label="Publish status">
          <StatusBadge status={detail.publish_status} kind="publish" />
        </Field>
        <Field label="Duration">{duration(seconds)}</Field>
        <Field label="Created">{dateTime(detail.created_at)}</Field>
        <Field label="Started">{dateTime(detail.started_at)}</Field>
        <Field label="Finished">{dateTime(detail.finished_at)}</Field>
        <Field label="Run ID">
          <span className="mono">{detail.run_id}</span>
        </Field>
      </div>

      {detail.error ? (
        <div className="notice notice--danger" style={{ marginTop: 16 }}>
          <div className="notice__title">Run error</div>
          <div className="body-block body-block--mono">{detail.error}</div>
        </div>
      ) : null}

      <Section title="Guardrail" hint="from the LangGraph checkpoint">
        {detail.guardrail_passed === null ? (
          <div className="notice">
            Guardrail result could not be read from the checkpoint. This is <strong>not</strong> a
            pass — the checkpoint is either missing or does not carry this Run&apos;s state.
          </div>
        ) : detail.guardrail_passed ? (
          <div className="notice notice--ok">Guardrail passed.</div>
        ) : (
          <div className="notice notice--danger">
            <div className="notice__title">Guardrail rejected this draft</div>
            {detail.guardrail_reasons.length ? (
              <ul className="reason-list">
                {detail.guardrail_reasons.map((reason, index) => (
                  <li key={index}>{reason}</li>
                ))}
              </ul>
            ) : (
              <div>No reason text was recorded.</div>
            )}
          </div>
        )}
      </Section>

      <Section title="Content">
        <div className="stack stack--tight">
          <div>
            <div className="meta__label">
              Final content <span className="cell-muted">(checkpoint)</span>
            </div>
            <div className="body-block">{detail.final_content ?? EMPTY}</div>
          </div>

          {review ? (
            <div>
              <div className="meta__label row">
                Human decision
                <StatusBadge status={review.action} kind="review" />
                <span className="cell-muted">{dateTime(review.reviewed_at)}</span>
              </div>
              <div className="meta__label" style={{ marginTop: 6 }}>
                Original content
              </div>
              <div className="body-block">{review.original_content}</div>
              {review.edited_content !== null ? (
                <>
                  <div className="meta__label" style={{ marginTop: 6 }}>
                    Edited content
                    {review.action === 'edit' ? ' (this is what was published)' : ''}
                  </div>
                  <div className="body-block">{review.edited_content}</div>
                </>
              ) : null}
              {review.reject_reason ? (
                <>
                  <div className="meta__label" style={{ marginTop: 6 }}>
                    Reject reason
                  </div>
                  <div className="body-block">{review.reject_reason}</div>
                </>
              ) : null}
            </div>
          ) : (
            <div className="cell-muted">No review decision has been recorded for this Run.</div>
          )}

          {draft ? (
            <div>
              <div className="meta__label">
                Draft v{draft.version} · {draft.status}
                {draft.model_name ? ` · ${draft.model_name}` : ''}
                {draft.prompt_version ? ` · prompt ${draft.prompt_version}` : ''}
                {draft.generated_at ? ` · ${dateTime(draft.generated_at)}` : ''}
              </div>
              <div className="body-block">{draft.content}</div>
            </div>
          ) : (
            <div className="cell-muted">No draft is stored for this Run.</div>
          )}
        </div>
      </Section>

      <Section
        title="Research"
        hint={
          detail.research_items.length
            ? `${detail.research_items.length} item${detail.research_items.length === 1 ? '' : 's'}`
            : undefined
        }
      >
        {detail.research_items.length === 0 ? (
          <div className="cell-muted">No research items were stored for this Run.</div>
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
                {showAllResearch
                  ? 'Show fewer'
                  : `Show all ${detail.research_items.length}`}
              </button>
            ) : null}
          </>
        )}
      </Section>

      <Section title="Publish">
        {publish ? (
          <PublishBlock publish={publish} />
        ) : (
          <div className="notice">
            This Run has <strong>no publish record</strong>. Nothing has been sent to X for it.
            The run status above must not be read as a publish result.
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
            This run has an <strong>unresolved publish attempt</strong>. Decide it on the{' '}
            <Link to="/published?status=__unresolved__">Published page</Link> after checking the
            account on X — the console will not publish from here.
          </div>
        ) : null}
      </Section>

      <div className="cell-muted" style={{ marginTop: 16 }}>
        This is an audit record, not a live mirror of X — nothing here changes if a post is later
        deleted on X. Publish status is the authoritative field; the run status above is only a
        mirrored view of it.
      </div>
    </>
  )
}

export function RunDetailDrawer({
  runId,
  onClose,
}: {
  runId: string
  onClose: () => void
}) {
  const { data, loading, error, reload } = useAsync(() => api.getRunDetail(runId), [runId])
  const { refresh } = useConsole()

  // 发过一次之后要同时刷新：抽屉里的详情、以及侧栏的待裁决计数。
  function onSettled() {
    reload()
    refresh()
  }

  return (
    <Drawer
      onClose={onClose}
      title={
        <>
          <h2 className="drawer__title">{data ? data.topic : 'Run detail'}</h2>
          <div className="cell-muted mono">{runId}</div>
        </>
      }
    >
      {loading ? <LoadingState message="Loading run…" /> : null}
      {error ? <ErrorState error={error} what="Failed to load this run." onRetry={reload} /> : null}
      {data && !loading ? <DetailBody detail={data} onSettled={onSettled} /> : null}
    </Drawer>
  )
}
