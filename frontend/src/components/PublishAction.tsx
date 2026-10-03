import { useState } from 'react'
import { Link } from 'react-router-dom'

import { api, xPostUrl } from '../api/client'
import { ConfirmDialog } from './ConfirmDialog'
import { StatusBadge } from './StatusBadge'
import { CopyButton } from './ui/CopyButton'
import { IconExternal } from './ui/icons'
import { useMutation } from '../hooks/useMutation'
import { EMPTY, dateTime, publishStatusLabel } from '../utils/format'
import {
  FIRST_PUBLISH_LABEL,
  REPUBLISH_LABEL,
  UNRESOLVED_PUBLISHED_PATH,
  publishActionView,
} from '../utils/publishAction'
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
        title: '已发布',
        detail: 'X 已接受这条帖子。下面的 ID 就是凭据。',
        xPostId: postId,
      }
    case 'resolved':
      return {
        tone: 'ok',
        title: '裁决已记录',
        detail: `这条发布记录现在是"${publishStatusLabel(body.publish?.status ?? null)}"。本次操作没有向 X 发送任何内容。`,
        xPostId: postId,
      }
    case 'unknown':
      return {
        tone: 'warn',
        title: '发布结果未知 —— X 上可能已经有这条帖子',
        detail:
          '请求已经到达 X，但结果无法确定。在采取任何其它操作之前，先去 X 上看一下这个账号。这既不是失败，也不是成功。',
        xPostId: postId,
      }
    case 'failed':
      return {
        tone: 'danger',
        title: '发布失败 —— 没有内容发到 X',
        detail: 'X 拒绝了这次请求，所以没有创建帖子。这一条可以重试。',
        xPostId: postId,
      }
    case 'conflict':
      return {
        tone: 'warn',
        title: '未发布 —— 这个 Run 不在可发布的状态',
        detail: '没有内容发到 X。原因见下方。',
        xPostId: postId,
      }
    case 'invalid':
      return {
        tone: 'danger',
        title: '未发布 —— 内容没有通过本地检查',
        detail: '没有内容发到 X。本地前置检查未通过，详情见下方。',
        xPostId: postId,
      }
    default:
      return {
        tone: 'warn',
        title: `未知结果：${body.outcome}`,
        detail: null,
        xPostId: postId,
      }
  }
}

/**
 * 发布结果的呈现。成功、失败、未知三种在视觉上必须一眼可分。
 *
 * "结果未知"用的是**这个界面里只在它身上出现的那套橙色**（`notice--unknown`），
 * 不是普通的警示黄。理由在 §16：那不是一次失败，而是一次"不知道"——如果它和
 * "发布失败"长得一样，人会顺手再点一次发布，而这一次真的可能发出第二条。
 */
export function PublishOutcomeNotice({ call }: { call: PublishCall }) {
  const outcome = describeOutcome(call)
  const toneClass = {
    ok: 'notice--ok',
    warn: 'notice--unknown',
    danger: 'notice--danger',
    info: 'notice--info',
  }[outcome.tone]
  const publishedAt = call.body.publish?.published_at ?? null

  return (
    <div className={`notice ${toneClass}`}>
      <div className="notice__title">{outcome.title}</div>
      {outcome.detail ? <div>{outcome.detail}</div> : null}
      {call.body.error ? <div className="body-block body-block--mono">{call.body.error}</div> : null}

      {/* 成功时把"凭据"给全：Post ID 可复制、时间可对账、链接可直达。
          这三样是"确实发出去了"唯一能被验证的证据，缺一样都只能靠信。 */}
      {outcome.tone === 'ok' && outcome.xPostId ? (
        <div className="publish-proof">
          <div className="meta">
            <div className="meta__label">X Post ID</div>
            <div className="meta__value meta__value--mono row row--tight">
              <span>{outcome.xPostId}</span>
              <CopyButton value={outcome.xPostId} label="复制 X Post ID" />
            </div>
          </div>
          <div className="meta">
            <div className="meta__label">发布时间</div>
            <div className="meta__value">{dateTime(publishedAt)}</div>
          </div>
          <a
            className="btn btn--sm"
            href={xPostUrl(outcome.xPostId)}
            target="_blank"
            rel="noreferrer noopener"
          >
            <IconExternal size={14} />
            在 X 中打开
          </a>
        </div>
      ) : null}

      {/* HTTP 状态码与 outcome 枚举值：给排查用，所以用等宽字体、降一档。
          显示结论用中文，对日志用英文，两者都要在。 */}
      <div className="notice__meta">
        HTTP {call.httpStatus} · outcome <code>{call.body.outcome}</code>
        {outcome.xPostId && outcome.tone !== 'ok' ? (
          <>
            {' · '}
            <a href={xPostUrl(outcome.xPostId)} target="_blank" rel="noreferrer noopener">
              在 X 中打开
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
          // busy 期间禁用是这里唯一防止重复发布的机制：请求已经在路上，
          // 再点一次就是第二次 POST。按钮文案同时变成"正在发布…"，
          // 让人看到"它在动"，而不是猜为什么点不动。
          disabled={publish.busy}
        >
          {/* 按钮文案来自契约（首次发布 / 重发），不在这里另起一套说法：
              "发布到 X" 和 "重新发布" 是同一个动作在两种上下文里的叫法，
              分不清哪个是第一次、哪个是重来，就等于没提示。 */}
          {publish.busy ? '正在发布…' : isRetry ? REPUBLISH_LABEL : FIRST_PUBLISH_LABEL}
        </button>
      </div>

      {result ? (
        <div className="publish-result">
          <PublishOutcomeNotice call={result} />
        </div>
      ) : null}

      {confirming ? (
        <ConfirmDialog
          title={isRetry ? '重新发布到 X？' : '首次发布到 X？'}
          confirmLabel={isRetry ? '重新发布' : '立即发布'}
          danger
          busy={publish.busy}
          confirmDisabled={!acknowledged}
          error={publish.error}
          risk={
            <>
              <p className="risk-line">
                确定要发布到真实 X 账号吗？下面的文字会以<strong>公开帖子</strong>的形式发出去。
              </p>
              <p className="risk-line risk-line--strong">
                该操作会创建一条公开帖子。
              </p>
              <p className="risk-line">
                它会立刻生效，并且<strong>无法从这个控制台撤销</strong>（要撤回只能去 X 上手动删除，
                而本系统不会跟着同步）。
              </p>
              {/* 勾选框放在按钮正上方，而不是正文中间：它是按下确认前的最后一道闸门，
                  位置和"立即发布"越近越好。 */}
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={acknowledged}
                  onChange={(event) => setAcknowledged(event.target.checked)}
                />
                <span>我明白：这一下会立刻在 X 上公开发出这条帖子。</span>
              </label>
            </>
          }
          onCancel={() => {
            setConfirming(false)
            publish.reset()
          }}
          onConfirm={submit}
        >
          <div className="field">
            <div className="field__label">将要发出的正文</div>
            <div className="body-block body-block--authoritative">{body ?? EMPTY}</div>
          </div>
        </ConfirmDialog>
      ) : null}
    </>
  )
}

// ---------------------------------------------------------------------------
// 发布入口：由契约决定"这里该放什么"
// ---------------------------------------------------------------------------

interface PublishActionSlotProps {
  runStatus: string | null | undefined
  /**
   * 当前有效的发布记录状态。三个取值含义不同，别传错：
   *   - `'failed' | 'publishing' | 'publish_unknown' | 'published'` 有记录
   *   - `null`      明确没有记录（可以首次发布）
   *   - `undefined` 读不到（会落进 HIDDEN）
   */
  publishStatus: string | null | undefined
  runId: string
  /** 将要发出的正文，只在真的渲染发布按钮时用得上。 */
  body?: string | null
  onSettled: () => void
}

/**
 * 发布入口。**所有分支判断都在 `utils/publishAction.ts` 里** —— 这里只照着那份
 * 视图模型渲染，自己一个 if 都不写（包括"要不要渲染按钮"这件事本身）。
 *
 * 这是整个控制台唯一会向外发帖的地方，所以"什么时候有按钮、按钮叫什么"必须只有
 * 一个答案。此前它散在抽屉的 `canPublish` 和发布记录页的一串三元表达式里，各自
 * 看着都合理，但没有任何东西保证它们一致 —— 而这种不一致的代价是多一条推文。
 *
 * 没有入口时**不渲染**，而不是渲染一个禁用按钮：一个灰着的"发布"按钮会被读成
 * "点一下就好了"，而这里的事实是"这个动作根本不该存在"。
 */
export function PublishActionSlot({
  runStatus,
  publishStatus,
  runId,
  body,
  onSettled,
}: PublishActionSlotProps) {
  const view = publishActionView(runStatus, publishStatus)

  if (view.showPublish) {
    // 走既有的 PublishControl —— 它那套确认框、勾选框、risk 文案一个字都没动。
    return (
      <PublishControl
        runId={runId}
        body={body ?? null}
        isRetry={view.isRetry}
        onSettled={onSettled}
      />
    )
  }

  if (!view.note && !view.showResolve) return null

  return (
    <div className="detail-action">
      {/* 色调也由契约给：橙色（`--unknown`）只属于 publish_unknown。
          `publishing` / 状态读不到 / 资格不足一律中性，绝不借用橙色。 */}
      {view.note ? (
        <div className={view.noteTone === 'unknown' ? 'notice notice--unknown' : 'notice'}>
          {view.note}
        </div>
      ) : null}

      {/* 导航，不是发布动作：它不调 X，只是把人送到能处理这条未决记录的地方。 */}
      {view.showResolve ? (
        <Link className="btn btn--primary" to={UNRESOLVED_PUBLISHED_PATH}>
          {view.resolveLabel} →
        </Link>
      ) : null}
    </div>
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
      <div className="notice notice--unknown">
        <div className="notice__title">这次发布尝试没有确定结果</div>
        <div className="row row--tight resolve-meta">
          <StatusBadge status={publish.status} kind="publish" />
          <span className="cell-muted">尝试开始于 {dateTime(publish.updated_at)}</span>
        </div>
        {/* 这两句是整段文字的结论，单独成块、加粗——它们解释了为什么这里
            没有"重试一次"这样一个按钮。 */}
        <div className="resolve-verdict">
          系统无法确认 X 是否已经创建帖子。自动重试可能导致重复发布。
        </div>
        <div>
          先到 X 上看一下这个账号：帖子在，就把它记下来；帖子不在，才可以允许重试。
          在人来裁决之前，<strong>系统不会为这个 Run 自动再调一次 X</strong>。
        </div>
        {publish.last_error ? (
          <div className="body-block body-block--mono">{publish.last_error}</div>
        ) : null}
      </div>

      {/* 两个裁决动作直接摆出来，不做成"裁决 → 再选一个"的两级菜单：
          这里每一步都要人想清楚，多一层点击只会让人机械地点下去。
          真正不可逆的那一步（下面那个重试确认框）才是需要减速的地方。 */}
      <div className="field resolve-actions">
        <div className="field__label">裁决</div>
        <div className="row">
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => {
              setResult(null)
              setMode('mark_published')
            }}
            disabled={resolve.busy}
            title="帖子已经在 X 上，把它的帖子 ID 抄回来"
          >
            标记为已发布
          </button>
          <button
            type="button"
            className="btn btn--danger"
            onClick={() => {
              setResult(null)
              setMode('retry')
            }}
            disabled={resolve.busy}
            title="帖子不在 X 上，允许这个 Run 重新发布"
          >
            确认可能重复后重试
          </button>
        </div>
      </div>

      {result ? <PublishOutcomeNotice call={result} /> : null}

      {mode === 'mark_published' ? (
        <ConfirmDialog
          title="记录 X 上的帖子 ID"
          confirmLabel="标记为已发布"
          tone="warn"
          busy={resolve.busy}
          confirmDisabled={!xPostId.trim()}
          error={resolve.error}
          onCancel={close}
          onConfirm={submit}
        >
          <p className="modal-lede">
            在 X 上打开那条帖子，把它网址里 <code>/status/</code> 后面那串数字复制过来。
            本控制台不会替你猜这个 ID：一个由系统自己产生的 ID，本身就是"结果未知"的含义。
          </p>
          <div className="field">
            <label className="field__label" htmlFor="x-post-id">
              X 帖子 ID（必填）
            </label>
            <input
              id="x-post-id"
              className="input mono"
              value={xPostId}
              onChange={(event) => setXPostId(event.target.value)}
              placeholder="例如 2102664104339226711"
            />
          </div>
          <p className="cell-muted modal-note">这一步只修正记录，不会向 X 发送任何内容。</p>
        </ConfirmDialog>
      ) : null}

      {mode === 'retry' ? (
        <ConfirmDialog
          title="允许这个 Run 重新发布"
          confirmLabel="确认可能重复后重试"
          danger
          busy={resolve.busy}
          confirmDisabled={!(checkedOnX && acceptDuplicate)}
          error={resolve.error}
          risk={
            <>
              <p className="risk-line risk-line--strong">
                如果那条帖子确实已经在 X 上，在这里标记为未发布，就等于让系统忘掉它 ——
                下一次发布会创建第二条帖子。请先去 X 上确认。
              </p>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={checkedOnX}
                  onChange={(event) => setCheckedOnX(event.target.checked)}
                />
                <span>我亲自查过 X 账号，这条帖子不在上面。</span>
              </label>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={acceptDuplicate}
                  onChange={(event) => setAcceptDuplicate(event.target.checked)}
                />
                <span>我明白：之后再重试可能创建出一条重复的帖子。</span>
              </label>
            </>
          }
          onCancel={close}
          onConfirm={submit}
        >
          <p className="modal-lede">
            这一步会把这次尝试记为失败，好让它能够重试。它<strong>本身不会</strong>发出任何帖子
            —— 之后你还需要自己再按一次发布按钮。
          </p>
        </ConfirmDialog>
      ) : null}
    </div>
  )
}
