import { useState } from 'react'

import { api } from '../api/client'
import { ConfirmDialog } from './ConfirmDialog'
import { ErrorDetail, ErrorState, LoadingState } from './States'
import { StatusBadge } from './StatusBadge'
import { useAsync } from '../hooks/useAsync'
import { useMutation } from '../hooks/useMutation'
import { EMPTY, codePointLength, dateTime, httpStatusLabel, text } from '../utils/format'
import type { AdminRunDetail, ReviewDecision } from '../types/api'

/**
 * 审核工作台的右半边：一个 Run 的完整材料 + 三个决定。
 *
 * 材料按人做决定实际需要的顺序排列：**题目 → 研究内容 → 草稿 → 规则检查 → 决定**。
 * 规则检查排在草稿之后是有意的——它是"这篇稿子能不能发"的机器意见，人应该先自己读完
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
   * 决定提交成功后回调，带上**提交的是哪个决定**。
   *
   * **队列不归这个组件管**：它只知道当前这一个 Run，"接下来该选哪条"由持有队列的
   * 父组件决定。至于为什么要带上 action：三条路径成功后要说的话不一样（批准 /
   * 改稿已存 / 不会再发出去），父组件没法从别处推断出人刚按的是哪个按钮。
   */
  onDecided: (action: Mode) => void
}

function GuardrailBlock({ detail }: { detail: AdminRunDetail }) {
  if (detail.guardrail_passed === null) {
    return (
      <div className="notice">
        规则检查结果无法从 checkpoint 读出。这<strong>不等于</strong>通过。
      </div>
    )
  }
  if (detail.guardrail_passed) {
    return <div className="notice notice--ok">规则检查通过。</div>
  }
  return (
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
  )
}

export function ReviewPane({ runId, contentMaxLength, onDecided }: Props) {
  const { data, loading, error, reload } = useAsync(() => api.getRunDetail(runId), [runId])
  const submit = useMutation((decision: ReviewDecision) => api.submitReview(runId, decision))

  const [mode, setMode] = useState<Mode | null>(null)
  const [editText, setEditText] = useState('')
  const [rejectReason, setRejectReason] = useState('')
  // 拒绝理由为空时按钮**不禁用**，而是让人按下去看到"请输入拒绝原因"。禁用的按钮
  // 只会让人盯着它猜为什么点不动，说不清楚缺的是什么。
  const [rejectReasonMissing, setRejectReasonMissing] = useState(false)

  if (loading) return <LoadingState message="正在加载这个 Run…" />
  if (error) return <ErrorState error={error} what="加载该 Run 失败。" onRetry={reload} />
  if (!data) return null

  const draftContent = data.draft?.content ?? ''
  const editedLength = codePointLength(editText)
  const overLimit = editedLength > contentMaxLength

  function startEdit() {
    setMode('edit')
    setEditText(draftContent)
  }

  function startReject() {
    setMode('reject')
    setRejectReasonMissing(false)
  }

  function submitReject() {
    if (!rejectReason.trim()) {
      setRejectReasonMissing(true)
      return
    }
    setRejectReasonMissing(false)
    void decide({ action: 'reject', reason: rejectReason })
  }

  async function decide(decision: ReviewDecision) {
    const result = await submit.run(decision)
    // 失败时停在原地：错误信息就在本组件里显示着（含 HTTP 状态与后端文案），
    // 跳到下一个 Run 会把这条错误一起带走，人只会看到"点了没反应"。
    if (result === null) return
    onDecided(decision.action)
  }

  return (
    <div className="stack">
      <div className="card">
        <div className="card__body">
          <h2 className="drawer__title">{data.topic}</h2>
          <div className="row" style={{ marginTop: 8 }}>
            <StatusBadge status={data.run_status} kind="run" />
            <span className="cell-muted">创建于 {dateTime(data.created_at)}</span>
            <span className="cell-muted mono" title="Run ID">
              {data.run_id}
            </span>
          </div>
          {data.error ? (
            <div className="notice notice--danger" style={{ marginTop: 12 }}>
              <div className="notice__title">Run 错误</div>
              <div className="body-block body-block--mono">{data.error}</div>
            </div>
          ) : null}
        </div>
      </div>

      <section className="section" style={{ marginBottom: 0 }}>
        <div className="section__head">
          <h3 className="section__title">研究内容</h3>
          <span className="section__hint">{data.research_items.length} 条 · 草稿就是据此写出来的</span>
        </div>
        <div className="card">
          <div className="card__body">
            {data.research_items.length === 0 ? (
              <div className="cell-muted">这个 Run 没有存下研究内容。</div>
            ) : (
              <ul className="research-list">
                {data.research_items.map((item) => (
                  <li key={item.item_id} className="research-item">
                    <div className="row row--between">
                      <strong>
                        {item.author_username ? `@${item.author_username}` : '作者未知'}
                      </strong>
                      <span className="cell-muted">
                        {item.likes} 点赞 · {item.reposts} 转发 · {item.replies} 回复
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
          <h3 className="section__title">AI 生成草稿</h3>
          <span className="section__hint">
            {data.draft
              ? `v${data.draft.version} · ${data.draft.model_name ?? '模型未知'} · ${text(data.draft.prompt_version)}`
              : undefined}
          </span>
        </div>
        <div className="card">
          <div className="card__body">
            {data.draft ? (
              <div className="body-block">{data.draft.content}</div>
            ) : (
              <div className="cell-muted">这个 Run 没有存下草稿。</div>
            )}
          </div>
        </div>
      </section>

      <section className="section" style={{ marginBottom: 0 }}>
        <div className="section__head">
          <h3 className="section__title">规则检查</h3>
          <span className="section__hint">来自 LangGraph checkpoint</span>
        </div>
        <GuardrailBlock detail={data} />
      </section>

      <section className="section" style={{ marginBottom: 0 }}>
        <div className="section__head">
          <h3 className="section__title">人工决定</h3>
          <span className="section__hint">
            走 POST /api/runs/{'{id}'}/review —— 与 API 是同一个入口
          </span>
        </div>

        {submit.error ? (
          <div className="notice notice--danger" style={{ marginBottom: 12 }}>
            <div className="notice__title">{httpStatusLabel(submit.error.status)}</div>
            <ErrorDetail error={submit.error} />
            {submit.error.status === 409 ? (
              // 409 是"这个 Run 已经不在等人工了"——最常见的原因就是别处已经决定了。
              // 这时最有用的一句话是"刷新看看现在的状态"，而不是让人反复重试。
              // 这句话由 `backendErrorLabel` 给出具体状态，这里只补"接下来做什么"。
              <div style={{ marginTop: 6 }}>
                重新加载可以看到它当前的状态，不需要反复重试。
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
              批准
            </button>
            <button
              type="button"
              className="btn"
              onClick={startEdit}
              disabled={submit.busy || !data.draft}
              title={data.draft ? undefined : '这个 Run 没有草稿可编辑'}
            >
              编辑
            </button>
            <button
              type="button"
              className="btn btn--danger"
              onClick={startReject}
              disabled={submit.busy}
            >
              拒绝
            </button>
          </div>
        ) : null}

        {mode === 'edit' ? (
          <div className="card">
            <div className="card__body">
              <div className="field">
                <div className="field__label">原始内容</div>
                <div className="body-block body-block--mono">{draftContent || EMPTY}</div>
              </div>
              <div className="field" style={{ marginTop: 12 }}>
                <label className="field__label" htmlFor="edit-content">
                  编辑后的内容
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
                  {editedLength} / {contentMaxLength} 字
                </span>
                <span className="cell-muted" style={{ maxWidth: 420 }}>
                  字数按本系统的口径统计（后端 <code>len()</code>），<strong>不是</strong> X
                  的加权字数。超过这个上限，发布时会被后端拒绝，所以到上限就不再允许提交。
                </span>
              </div>
              <div className="row" style={{ marginTop: 12 }}>
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={submit.busy || overLimit || !editText.trim()}
                  onClick={() => decide({ action: 'edit', content: editText })}
                >
                  {submit.busy ? '正在提交…' : '提交编辑后的草稿'}
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => setMode(null)}
                  disabled={submit.busy}
                >
                  取消
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
                  拒绝原因
                </label>
                <textarea
                  id="reject-reason"
                  className="textarea"
                  style={{ minHeight: 100 }}
                  placeholder="必填。原因会随这个 Run 一起存下来。"
                  value={rejectReason}
                  onChange={(event) => {
                    setRejectReason(event.target.value)
                    if (event.target.value.trim()) setRejectReasonMissing(false)
                  }}
                />
              </div>
              {rejectReasonMissing ? (
                <div className="field__error" role="alert">
                  请输入拒绝原因
                </div>
              ) : null}
              <div className="row" style={{ marginTop: 12 }}>
                <button
                  type="button"
                  className="btn btn--danger"
                  disabled={submit.busy}
                  onClick={submitReject}
                >
                  {submit.busy ? '正在提交…' : '拒绝这份草稿'}
                </button>
                <button
                  type="button"
                  className="btn"
                  onClick={() => setMode(null)}
                  disabled={submit.busy}
                >
                  取消
                </button>
              </div>
              <p className="cell-muted" style={{ marginBottom: 0 }}>
                拒绝会结束这个 Run。不会有任何内容发到 X。
              </p>
            </div>
          </div>
        ) : null}

        {submit.busy && mode === 'approve' ? <LoadingState message="正在提交…" /> : null}
      </section>

      {mode === 'approve' ? (
        <ConfirmDialog
          title="批准这份草稿？"
          confirmLabel="批准"
          busy={submit.busy}
          error={submit.error}
          onCancel={() => {
            setMode(null)
            submit.reset()
          }}
          onConfirm={() => decide({ action: 'approve' })}
        >
          <p style={{ marginTop: 0 }}>
            这会接受当前写好的草稿，并让这个 Run 可以进入发布流程。<strong>此时还没有任何内容发到 X</strong>
            —— 发布是另一步，需要单独确认。
          </p>
          <div className="body-block">{draftContent || EMPTY}</div>
        </ConfirmDialog>
      ) : null}
    </div>
  )
}
