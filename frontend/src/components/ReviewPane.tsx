import { useState } from 'react'

import { api } from '../api/client'
import { ConfirmDialog } from './ConfirmDialog'
import { ErrorDetail, ErrorState, LoadingState } from './States'
import { StatusBadge } from './StatusBadge'
import { CopyButton } from './ui/CopyButton'
import { ResearchList } from './ui/ResearchList'
import { Section } from './ui/Section'
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
 *
 * 这一版最大的结构改动是**动作条吸底**。三个决定原先排在整块材料的最后：研究素材
 * 十几条、草稿几百字，批准 / 编辑 / 拒绝被推到两三屏之外。审核是连续动作，每一条都要
 * 滚到底再滚回来，效率损失是实打实的。现在它们固定在视口底部，滚动时一直在。
 * 编辑框和拒绝理由仍然长在正文里（那是"填内容"的地方），但提交 / 取消跟着动作条走，
 * 和按钮永远在同一屏——这也正是字数计数器该待的位置：它和"提交"按钮是一对。
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
    submit.reset()
  }

  function startReject() {
    setMode('reject')
    setRejectReasonMissing(false)
    submit.reset()
  }

  function cancelMode() {
    setMode(null)
    submit.reset()
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
          <div className="row row--tight review-head">
            <StatusBadge status={data.run_status} kind="run" />
            <span className="cell-muted">创建于 {dateTime(data.created_at)}</span>
            <span className="row row--tight review-head__id">
              <span className="mono" title={data.run_id}>
                {data.run_id}
              </span>
              <CopyButton value={data.run_id} label="复制 Run ID" />
            </span>
          </div>
          {data.error ? (
            <div className="notice notice--danger review-head__error">
              <div className="notice__title">Run 错误</div>
              <div className="body-block body-block--mono">{data.error}</div>
            </div>
          ) : null}
        </div>
      </div>

      <Section title="研究内容" hint={`${data.research_items.length} 条 · 草稿就是据此写出来的`}>
        <div className="card">
          <div className="card__body">
            <ResearchList items={data.research_items} />
          </div>
        </div>
      </Section>

      <Section
        title="AI 生成草稿"
        hint={
          data.draft
            ? `v${data.draft.version} · ${data.draft.model_name ?? '模型未知'} · ${text(data.draft.prompt_version)}`
            : undefined
        }
      >
        <div className="card">
          <div className="card__body">
            {data.draft ? (
              <div className="body-block">{data.draft.content}</div>
            ) : (
              <div className="cell-muted">
                这个 Run 没有存下草稿。没有草稿就不能批准或编辑，只能拒绝。
              </div>
            )}
          </div>
        </div>
      </Section>

      <Section title="规则检查" hint="来自 LangGraph checkpoint">
        <GuardrailBlock detail={data} />
      </Section>

      <Section
        title="人工决定"
        hint={`走 POST /api/runs/${'{id}'}/review —— 与 API 是同一个入口`}
      >
        {submit.error ? (
          <div className="notice notice--danger">
            <div className="notice__title">{httpStatusLabel(submit.error.status)}</div>
            <ErrorDetail error={submit.error} />
            {submit.error.status === 409 ? (
              // 409 是"这个 Run 已经不在等人工了"——最常见的原因就是别处已经决定了。
              // 这时最有用的一句话是"刷新看看现在的状态"，而不是让人反复重试。
              // 这句话由 `backendErrorLabel` 给出具体状态，这里只补"接下来做什么"。
              <div className="notice__follow">
                重新加载可以看到它当前的状态，不需要反复重试。
              </div>
            ) : null}
          </div>
        ) : null}

        {mode === 'edit' ? (
          <div className="card">
            <div className="card__body">
              <div className="field">
                <div className="field__label">原始内容（只读）</div>
                {/* 虚线边框 + 降色：这块改不了。它和下面那个可编辑框必须一眼分得开，
                    否则人会在只读块上敲字然后疑惑为什么没反应。 */}
                <div className="body-block body-block--readonly">{draftContent || EMPTY}</div>
              </div>
              <div className="field edit-field">
                <label className="field__label" htmlFor="edit-content">
                  编辑后的内容（这一份会被保存并用于发布）
                </label>
                <textarea
                  id="edit-content"
                  className="textarea textarea--editor"
                  value={editText}
                  onChange={(event) => setEditText(event.target.value)}
                />
                <div className="row row--between counter-row">
                  <span className={overLimit ? 'counter counter--over' : 'counter'}>
                    {editedLength} / {contentMaxLength} 字
                  </span>
                  <span className="cell-muted counter-row__note">
                    系统限制：{contentMaxLength} 字（按当前本地计数口径，按 Unicode 码点计，
                    含 emoji 各算 1 个）。这<strong>不是</strong> X 的加权字数口径——后端按同一口径
                    校验，超限会在提交时被拒绝，所以到上限就不再允许提交。
                  </span>
                </div>
              </div>
            </div>
          </div>
        ) : null}

        {mode === 'reject' ? (
          <div className="card">
            <div className="card__body">
              <div className="field">
                <label className="field__label" htmlFor="reject-reason">
                  拒绝原因（必填）
                </label>
                <textarea
                  id="reject-reason"
                  className="textarea textarea--reason"
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
              <div className="notice notice--info reject-notice">
                <div className="notice__title">拒绝之后会发生什么</div>
                <div>
                  这个 Run 到此结束，状态会变成已拒绝。<strong>不会有任何内容发到 X</strong>
                  —— 拒绝不会触发发布，也不会有待裁决的发布记录留下。这个决定不可撤销。
                </div>
              </div>
            </div>
          </div>
        ) : null}

        {/* 动作条：吸在视口底部，滚动材料时始终可见。
            三种模式共用同一条，按钮换内容——位置不变，人就不用重新找。 */}
        <div className="action-bar">
          {mode === null ? (
            <>
              <button
                type="button"
                className="btn btn--primary"
                onClick={() => setMode('approve')}
                disabled={submit.busy || !data.draft}
                title={data.draft ? undefined : '这个 Run 没有草稿可批准'}
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
              <span className="action-bar__hint">
                {data.draft ? '批准或编辑后，这条内容仍需要单独确认才会发到 X。' : '没有草稿，只能拒绝。'}
              </span>
            </>
          ) : null}

          {mode === 'edit' ? (
            <>
              <button
                type="button"
                className="btn btn--primary"
                disabled={submit.busy || overLimit || !editText.trim()}
                onClick={() => decide({ action: 'edit', content: editText })}
              >
                {submit.busy ? '正在提交…' : '保存'}
              </button>
              <button type="button" className="btn" onClick={cancelMode} disabled={submit.busy}>
                取消
              </button>
              {/* 计数器和"保存"按钮在一起：超限时按钮是灰的，原因必须在同一屏看得到。 */}
              <span className={overLimit ? 'action-bar__hint counter--over' : 'action-bar__hint'}>
                {overLimit
                  ? `超出 ${editedLength - contentMaxLength} 字，无法保存`
                  : `已用 ${editedLength} / ${contentMaxLength} 字`}
              </span>
            </>
          ) : null}

          {mode === 'reject' ? (
            <>
              <button
                type="button"
                className="btn btn--danger"
                disabled={submit.busy}
                onClick={submitReject}
              >
                {submit.busy ? '正在提交…' : '确认拒绝'}
              </button>
              <button type="button" className="btn" onClick={cancelMode} disabled={submit.busy}>
                取消
              </button>
              <span className="action-bar__hint">拒绝后这个 Run 就结束了，不会发布任何内容。</span>
            </>
          ) : null}
        </div>
      </Section>

      {mode === 'approve' ? (
        <ConfirmDialog
          title="批准这份草稿？"
          confirmLabel="批准"
          busy={submit.busy}
          error={submit.error}
          risk={
            <>
              批准会接受当前写好的草稿，并让它进入发布流程。
              <strong>此时还没有任何内容发到 X</strong> —— 发布是另一步，需要单独确认。
            </>
          }
          onCancel={cancelMode}
          onConfirm={() => decide({ action: 'approve' })}
        >
          <div className="body-block body-block--authoritative">{draftContent || EMPTY}</div>
        </ConfirmDialog>
      ) : null}
    </div>
  )
}
