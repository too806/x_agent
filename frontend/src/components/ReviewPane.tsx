import { useState } from 'react'

import { api } from '../api/client'
import { ConfirmDialog } from './ConfirmDialog'
import { ErrorState, LoadingState } from './States'
import { StatusBadge } from './StatusBadge'
import { useAsync } from '../hooks/useAsync'
import { useMutation } from '../hooks/useMutation'
import { EMPTY, codePointLength, dateTime, text } from '../utils/format'
import type { AdminRunDetail, ReviewDecision } from '../types/api'

/**
 * 审核工作台的右半边：一个 Run 的完整材料 + 三个决定。
 *
 * 材料按人做决定实际需要的顺序排列：**题目 → 研究材料 → 草稿 → 护栏结论 → 决定**。
 * 护栏排在草稿之后是有意的——它是"这篇稿子能不能发"的机器意见，人应该先自己读完
 * 再看到它，否则很容易变成对着结论点头。
 *
 * 写操作只走既有的 `POST /api/runs/{id}/review`。控制台不实现第二套审核入口：那
 * 一套一定会漏掉 Phase 4a 用条件 UPDATE 换来的并发保护（同一个 Run 被 resume 两次
 * 会重复抓取、重复生成）。
 */

type Mode = 'approve' | 'edit' | 'reject'

interface Props {
  runId: string
  /** 后端下发的编辑字数上限。**不是**前端常量，见 types/api.ts 的说明。 */
  contentMaxLength: number
  /**
   * 决定提交成功后回调。**队列不归这个组件管**：它只知道当前这一个 Run，
   * "接下来该选哪条"由持有队列的父组件决定。
   */
  onDecided: () => void
}

function GuardrailBlock({ detail }: { detail: AdminRunDetail }) {
  if (detail.guardrail_passed === null) {
    return (
      <div className="notice">
        Guardrail result could not be read from the checkpoint. This is <strong>not</strong> a pass.
      </div>
    )
  }
  if (detail.guardrail_passed) {
    return <div className="notice notice--ok">Guardrail passed.</div>
  }
  return (
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
  )
}

export function ReviewPane({ runId, contentMaxLength, onDecided }: Props) {
  const { data, loading, error, reload } = useAsync(() => api.getRunDetail(runId), [runId])
  const submit = useMutation((decision: ReviewDecision) => api.submitReview(runId, decision))

  const [mode, setMode] = useState<Mode | null>(null)
  const [editText, setEditText] = useState('')
  const [rejectReason, setRejectReason] = useState('')

  if (loading) return <LoadingState message="Loading this run…" />
  if (error) return <ErrorState error={error} what="Failed to load this run." onRetry={reload} />
  if (!data) return null

  const draftContent = data.draft?.content ?? ''
  const editedLength = codePointLength(editText)
  const overLimit = editedLength > contentMaxLength

  function startEdit() {
    setMode('edit')
    setEditText(draftContent)
  }

  async function decide(decision: ReviewDecision) {
    const result = await submit.run(decision)
    // 失败时停在原地：错误信息就在本组件里显示着（含 HTTP 状态与后端文案），
    // 跳到下一个 Run 会把这条错误一起带走，人只会看到"点了没反应"。
    if (result === null) return
    onDecided()
  }

  return (
    <div className="stack">
      <div className="card">
        <div className="card__body">
          <h2 className="drawer__title">{data.topic}</h2>
          <div className="row" style={{ marginTop: 8 }}>
            <StatusBadge status={data.run_status} kind="run" />
            <span className="cell-muted">created {dateTime(data.created_at)}</span>
            <span className="cell-muted mono">{data.run_id}</span>
          </div>
          {data.error ? (
            <div className="notice notice--danger" style={{ marginTop: 12 }}>
              <div className="notice__title">Run error</div>
              <div className="body-block body-block--mono">{data.error}</div>
            </div>
          ) : null}
        </div>
      </div>

      <section className="section" style={{ marginBottom: 0 }}>
        <div className="section__head">
          <h3 className="section__title">Research material</h3>
          <span className="section__hint">
            {data.research_items.length} item{data.research_items.length === 1 ? '' : 's'} · what the
            draft was written from
          </span>
        </div>
        <div className="card">
          <div className="card__body">
            {data.research_items.length === 0 ? (
              <div className="cell-muted">No research items were stored for this Run.</div>
            ) : (
              <ul className="research-list">
                {data.research_items.map((item) => (
                  <li key={item.item_id} className="research-item">
                    <div className="row row--between">
                      <strong>
                        {item.author_username ? `@${item.author_username}` : 'unknown author'}
                      </strong>
                      <span className="cell-muted">
                        {item.likes} likes · {item.reposts} reposts · {item.replies} replies
                      </span>
                    </div>
                    <div className="body-block">{item.content}</div>
                    {item.source_url ? (
                      <a href={item.source_url} target="_blank" rel="noreferrer noopener">
                        {item.platform_post_id}
                      </a>
                    ) : (
                      <span className="cell-muted mono">{item.platform_post_id}</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </section>

      <section className="section" style={{ marginBottom: 0 }}>
        <div className="section__head">
          <h3 className="section__title">Draft</h3>
          <span className="section__hint">
            {data.draft
              ? `v${data.draft.version} · ${data.draft.model_name ?? 'unknown model'} · ${text(data.draft.prompt_version)}`
              : undefined}
          </span>
        </div>
        <div className="card">
          <div className="card__body">
            {data.draft ? (
              <div className="body-block">{data.draft.content}</div>
            ) : (
              <div className="cell-muted">No draft is stored for this Run.</div>
            )}
          </div>
        </div>
      </section>

      <section className="section" style={{ marginBottom: 0 }}>
        <div className="section__head">
          <h3 className="section__title">Guardrail</h3>
          <span className="section__hint">from the LangGraph checkpoint</span>
        </div>
        <GuardrailBlock detail={data} />
      </section>

      <section className="section" style={{ marginBottom: 0 }}>
        <div className="section__head">
          <h3 className="section__title">Decision</h3>
          <span className="section__hint">
            goes through POST /api/runs/{'{id}'}/review — the same endpoint the API uses
          </span>
        </div>

        {submit.error ? (
          <div className="notice notice--danger" style={{ marginBottom: 12 }}>
            <div className="notice__title">
              {submit.error.status === 0 ? 'Network error' : `HTTP ${submit.error.status}`}
            </div>
            <div>{submit.error.message}</div>
            {submit.error.status === 409 ? (
              // 409 是"这个 Run 已经不在等人工了"——最常见的原因就是别处已经决定了。
              // 这时最有用的一句话是"刷新看看现在的状态"，而不是让人反复重试。
              <div style={{ marginTop: 6 }}>
                The run is no longer waiting for review. Reload it to see its current state.
              </div>
            ) : null}
          </div>
        ) : null}

        {mode === null ? (
          <div className="row">
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => setMode('approve')}
              disabled={submit.busy}
            >
              Approve
            </button>
            <button
              type="button"
              className="btn"
              onClick={startEdit}
              disabled={submit.busy || !data.draft}
              title={data.draft ? undefined : 'This run has no draft to edit'}
            >
              Edit
            </button>
            <button
              type="button"
              className="btn btn--danger"
              onClick={() => setMode('reject')}
              disabled={submit.busy}
            >
              Reject
            </button>
          </div>
        ) : null}

        {mode === 'edit' ? (
          <div className="card">
            <div className="card__body">
              <div className="field">
                <label className="field__label" htmlFor="edit-content">
                  Edited content
                </label>
                <textarea
                  id="edit-content"
                  className="textarea"
                  value={editText}
                  onChange={(event) => setEditText(event.target.value)}
                />
              </div>
              <div className="row row--between" style={{ marginTop: 8 }}>
                <span className={overLimit ? 'counter counter--over' : 'counter'}>
                  {editedLength} / {contentMaxLength}
                </span>
                <span className="cell-muted" style={{ maxWidth: 420 }}>
                  Character count as the system measures it (<code>len()</code>), not X&apos;s
                  weighted count. A draft over this limit is rejected at publish time, so the
                  submit button stops here.
                </span>
              </div>
              <div className="row" style={{ marginTop: 12 }}>
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={submit.busy || overLimit || !editText.trim()}
                  onClick={() => decide({ action: 'edit', content: editText })}
                >
                  {submit.busy ? 'Submitting…' : 'Submit edited draft'}
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => setMode(null)}
                  disabled={submit.busy}
                >
                  Cancel
                </button>
              </div>
            </div>
          </div>
        ) : null}

        {mode === 'reject' ? (
          <div className="card">
            <div className="card__body">
              <div className="field">
                <label className="field__label" htmlFor="reject-reason">
                  Why is this draft rejected?
                </label>
                <textarea
                  id="reject-reason"
                  className="textarea"
                  style={{ minHeight: 100 }}
                  placeholder="Required. The reason is stored with the run."
                  value={rejectReason}
                  onChange={(event) => setRejectReason(event.target.value)}
                />
              </div>
              <div className="row" style={{ marginTop: 12 }}>
                <button
                  type="button"
                  className="btn btn--danger"
                  disabled={submit.busy || !rejectReason.trim()}
                  onClick={() => decide({ action: 'reject', reason: rejectReason })}
                >
                  {submit.busy ? 'Submitting…' : 'Reject this draft'}
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => setMode(null)}
                  disabled={submit.busy}
                >
                  Cancel
                </button>
              </div>
              <p className="cell-muted" style={{ marginBottom: 0 }}>
                Rejecting ends this run. Nothing is sent to X.
              </p>
            </div>
          </div>
        ) : null}

        {submit.busy && mode === 'approve' ? <LoadingState message="Submitting…" /> : null}
      </section>

      {mode === 'approve' ? (
        <ConfirmDialog
          title="Approve this draft?"
          confirmLabel="Approve"
          busy={submit.busy}
          error={submit.error}
          onCancel={() => {
            setMode(null)
            submit.reset()
          }}
          onConfirm={() => decide({ action: 'approve' })}
        >
          <p style={{ marginTop: 0 }}>
            This accepts the draft as written and makes the run publishable. <strong>Nothing is
            sent to X yet</strong> — publishing is a separate, explicitly confirmed step.
          </p>
          <div className="body-block">{draftContent || EMPTY}</div>
        </ConfirmDialog>
      ) : null}
    </div>
  )
}
