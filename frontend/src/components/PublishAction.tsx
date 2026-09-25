import { useState } from 'react'

import { api, xPostUrl } from '../api/client'
import { ConfirmDialog } from './ConfirmDialog'
import { useMutation } from '../hooks/useMutation'
import { EMPTY, dateTime } from '../utils/format'
import type { PublishCall, PublishRecord } from '../types/api'

/**
 * 发布与裁决。
 *
 * 这里是整个控制台**唯一会向外发帖**的地方，所以它比别处多两层克制：
 *
 * 1. **两道确认。** 普通发布要勾选"我知道这会立刻发到线上 X 账号"；对"结果未知"
 *    的重发要求更强：先确认已亲眼在 X 上看过、帖文确实不存在，再确认理解重发可能
 *    造出重复帖。这两个勾选项与后端 `RetryPublishDecision.confirm_maybe_duplicate`
 *    的 `Literal[True]` 是同一个意思的界面侧表达。
 * 2. **不许自动重试。** 没有轮询、没有"失败后自动再发一次"、没有进入页面就尝试。
 *    `publishing` / `publish_unknown` 两种状态下这个组件根本不提供发布入口——
 *    Phase 4a 的整个设计就是为了让这两种状态只能由人裁决。
 *
 * 重发刻意**复用既有的** `POST /api/runs/{id}/publish`，不在前端另造一套重试：
 * 后端那套带条件 UPDATE 的抢占是这个系统不出重复帖的根据。
 */

type Tone = 'ok' | 'warn' | 'danger' | 'info'

interface Outcome {
  tone: Tone
  title: string
  detail: string | null
  xPostId: string | null
}

/**
 * `outcome` → 一句能直接读的结论。
 *
 * 刻意**不**按 HTTP 状态码来分支：200 与 502 都可能对应"什么都没发生"，而
 * `outcome` 是后端对**实际发生了什么**的判断。状态码只作为附加信息展示。
 */
function describeOutcome(call: PublishCall): Outcome {
  const { body } = call
  const postId = body.publish?.x_post_id ?? null
  switch (body.outcome) {
    case 'published':
      return {
        tone: 'ok',
        title: 'Published',
        detail: 'X accepted the post. The id below is the confirmation.',
        xPostId: postId,
      }
    case 'resolved':
      return {
        tone: 'ok',
        title: 'Resolution recorded',
        detail: `The publish record is now "${body.publish?.status ?? 'unknown'}". Nothing was sent to X by this action.`,
        xPostId: postId,
      }
    case 'unknown':
      return {
        tone: 'warn',
        title: 'Result unknown — a post may exist on X',
        detail:
          'The request reached X but the outcome could not be determined. Check the account on X before doing anything else. This is not a failure and it is not a success.',
        xPostId: postId,
      }
    case 'failed':
      return {
        tone: 'danger',
        title: 'Publish failed — nothing was sent to X',
        detail: 'X rejected the request, so no post was created. This can be retried.',
        xPostId: postId,
      }
    case 'conflict':
      return {
        tone: 'warn',
        title: 'Not published — the run is not in a publishable state',
        detail: 'Nothing was sent to X. The reason is below.',
        xPostId: postId,
      }
    case 'invalid':
      return {
        tone: 'danger',
        title: 'Not published — the content failed a local check',
        detail: 'Nothing was sent to X. A local precondition failed, shown below.',
        xPostId: postId,
      }
    default:
      return {
        tone: 'warn',
        title: `Outcome: ${body.outcome}`,
        detail: null,
        xPostId: postId,
      }
  }
}

/** 发布结果的呈现。成功、失败、未知三种在视觉上必须一眼可分。 */
export function PublishOutcomeNotice({ call }: { call: PublishCall }) {
  const outcome = describeOutcome(call)
  const toneClass = {
    ok: 'notice--ok',
    warn: 'notice--warn',
    danger: 'notice--danger',
    info: 'notice--info',
  }[outcome.tone]

  return (
    <div className={`notice ${toneClass}`}>
      <div className="notice__title">{outcome.title}</div>
      {outcome.detail ? <div>{outcome.detail}</div> : null}
      {call.body.error ? <div className="body-block body-block--mono">{call.body.error}</div> : null}
      <div className="cell-muted" style={{ marginTop: 6 }}>
        HTTP {call.httpStatus} · outcome <code>{call.body.outcome}</code>
        {outcome.xPostId ? (
          <>
            {' · '}
            <a href={xPostUrl(outcome.xPostId)} target="_blank" rel="noreferrer noopener">
              open on X
            </a>
          </>
        ) : null}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 发布（首次发布 / 失败后重发）
// ---------------------------------------------------------------------------

interface PublishControlProps {
  runId: string
  /** 将要发出的正文，用于确认框里让人再核对一遍。 */
  body: string | null
  /** true 表示重发一次已经失败过的尝试。 */
  isRetry?: boolean
  onSettled: () => void
}

export function PublishControl({ runId, body, isRetry = false, onSettled }: PublishControlProps) {
  const [confirming, setConfirming] = useState(false)
  const [acknowledged, setAcknowledged] = useState(false)
  const [result, setResult] = useState<PublishCall | null>(null)
  const publish = useMutation(() => api.publishRun(runId))

  async function submit() {
    const call = await publish.run()
    if (call === null) return
    setResult(call)
    setConfirming(false)
    setAcknowledged(false)
    onSettled()
  }

  return (
    <>
      <div className="row">
        <button
          type="button"
          className="btn btn--primary"
          onClick={() => {
            setAcknowledged(false)
            setResult(null)
            publish.reset()
            setConfirming(true)
          }}
          disabled={publish.busy}
        >
          {isRetry ? 'Retry publish' : 'Publish to X'}
        </button>
        {publish.busy ? <span className="cell-muted">Publishing…</span> : null}
      </div>

      {result ? (
        <div style={{ marginTop: 12 }}>
          <PublishOutcomeNotice call={result} />
        </div>
      ) : null}

      {confirming ? (
        <ConfirmDialog
          title={isRetry ? 'Retry publishing to X?' : 'Publish to X?'}
          confirmLabel={isRetry ? 'Retry publish' : 'Publish now'}
          danger
          busy={publish.busy}
          confirmDisabled={!acknowledged}
          error={publish.error}
          onCancel={() => {
            setConfirming(false)
            publish.reset()
          }}
          onConfirm={submit}
        >
          <p style={{ marginTop: 0 }}>
            This sends the text below to the <strong>live X account</strong> as a public post. It
            happens immediately and cannot be taken back from this console.
          </p>
          <div className="body-block">{body ?? EMPTY}</div>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />
            <span>I understand this will post publicly on X right now.</span>
          </label>
        </ConfirmDialog>
      ) : null}
    </>
  )
}

// ---------------------------------------------------------------------------
// 裁决：结果未知
// ---------------------------------------------------------------------------

interface ResolveControlProps {
  runId: string
  publish: PublishRecord
  onSettled: () => void
}

/**
 * 两件事，两条路径，确认强度不同：
 *
 * - **确认已发出** → 只改状态。需要人从 X 页面把 post_id 抄回来，系统不猜。
 * - **确认没发出** → 只改状态，**不发帖**。要真正重发必须再点一次发布按钮。
 *
 * 后者要求两个勾选。这不是仪式感：它是这个系统里唯一能把"可能已经存在的推文"
 * 变成"可以再发一次"的操作，勾错一个就是重复推文。
 */
export function ResolveControl({ runId, publish, onSettled }: ResolveControlProps) {
  const [mode, setMode] = useState<'mark_published' | 'retry' | null>(null)
  const [xPostId, setXPostId] = useState('')
  const [checkedOnX, setCheckedOnX] = useState(false)
  const [acceptDuplicate, setAcceptDuplicate] = useState(false)
  const [result, setResult] = useState<PublishCall | null>(null)

  const resolve = useMutation((decision: Parameters<typeof api.resolvePublish>[1]) =>
    api.resolvePublish(runId, decision),
  )

  function close() {
    setMode(null)
    setXPostId('')
    setCheckedOnX(false)
    setAcceptDuplicate(false)
    resolve.reset()
  }

  async function submit() {
    if (mode === null) return
    const call = await resolve.run(
      mode === 'mark_published'
        ? { action: 'mark_published', x_post_id: xPostId.trim() }
        : { action: 'retry', confirm_maybe_duplicate: true },
    )
    if (call === null) return
    setResult(call)
    close()
    onSettled()
  }

  return (
    <div className="stack--tight">
      <div className="notice notice--warn">
        <div className="notice__title">This publish attempt has no known outcome</div>
        The status is <code>{publish.status}</code>. Go to X and look at the account: if the post is
        there, record its id; if it is not, mark it as failed. <strong>No automatic path will call X
        again for this run.</strong>
        {publish.last_error ? (
          <div className="body-block body-block--mono">{publish.last_error}</div>
        ) : null}
        <div className="cell-muted">Attempt started {dateTime(publish.updated_at)}</div>
      </div>

      <div className="row">
        <button
          type="button"
          className="btn"
          onClick={() => {
            setResult(null)
            setMode('mark_published')
          }}
          disabled={resolve.busy}
        >
          It is on X — record the post id
        </button>
        <button
          type="button"
          className="btn btn--danger"
          onClick={() => {
            setResult(null)
            setMode('retry')
          }}
          disabled={resolve.busy}
        >
          It is not on X — allow a retry
        </button>
      </div>

      {result ? <PublishOutcomeNotice call={result} /> : null}

      {mode === 'mark_published' ? (
        <ConfirmDialog
          title="Record the post id from X"
          confirmLabel="Record as published"
          busy={resolve.busy}
          confirmDisabled={!xPostId.trim()}
          error={resolve.error}
          onCancel={close}
          onConfirm={submit}
        >
          <p style={{ marginTop: 0 }}>
            Open the post on X and copy the numeric id out of its URL — the part after{' '}
            <code>/status/</code>. This console will not guess it: an id that came from the system
            itself is exactly what &ldquo;unknown outcome&rdquo; means.
          </p>
          <div className="field">
            <label className="field__label" htmlFor="x-post-id">
              X post id (required)
            </label>
            <input
              id="x-post-id"
              className="input mono"
              value={xPostId}
              onChange={(event) => setXPostId(event.target.value)}
              placeholder="e.g. 2102664104339226711"
            />
          </div>
          <p className="cell-muted" style={{ marginBottom: 0 }}>
            This only corrects the record. Nothing is sent to X.
          </p>
        </ConfirmDialog>
      ) : null}

      {mode === 'retry' ? (
        <ConfirmDialog
          title="Allow this run to be published again"
          confirmLabel="Mark as not published"
          danger
          busy={resolve.busy}
          confirmDisabled={!(checkedOnX && acceptDuplicate)}
          error={resolve.error}
          onCancel={close}
          onConfirm={submit}
        >
          <p style={{ marginTop: 0 }}>
            This records the attempt as failed so it can be retried. It does{' '}
            <strong>not</strong> post anything by itself — you will have to press publish
            afterwards, deliberately.
          </p>
          <div className="notice notice--danger">
            If the post <strong>is</strong> actually on X, marking it here makes the system forget
            that — and the next publish creates a second post. Check the account first.
          </div>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={checkedOnX}
              onChange={(event) => setCheckedOnX(event.target.checked)}
            />
            <span>I checked the X account myself and this post is not there.</span>
          </label>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={acceptDuplicate}
              onChange={(event) => setAcceptDuplicate(event.target.checked)}
            />
            <span>I understand a later retry may create a duplicate post.</span>
          </label>
        </ConfirmDialog>
      ) : null}
    </div>
  )
}
