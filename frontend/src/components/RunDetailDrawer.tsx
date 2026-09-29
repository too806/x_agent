import { Link } from 'react-router-dom'

import { api, xPostUrl } from '../api/client'
import { useAsync } from '../hooks/useAsync'
import { useConsole } from '../hooks/useConsole'
import { Drawer } from './Drawer'
import { PublishControl } from './PublishAction'
import { ErrorState, LoadingState } from './States'
import { StatusBadge } from './StatusBadge'
import { CopyButton } from './ui/CopyButton'
import { LifecycleTimeline } from './ui/LifecycleTimeline'
import { ResearchList } from './ui/ResearchList'
import { DetailSection, Field, FieldGrid } from './ui/Section'
import { TraceTimeline } from './ui/TraceTimeline'
import { IconExternal } from './ui/icons'
import { buildLifecycle } from '../utils/lifecycle'
import { traceSummary } from '../utils/trace'
import { isUnresolved } from '../types/api'
import {
  EMPTY,
  criticOutcomeSummary,
  dateTime,
  draftStatusLabel,
  duration,
  text,
} from '../utils/format'
import type { AdminRunDetail, PublishRecord } from '../types/api'

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
 *
 * 阅读顺序按"从结论到材料"排：
 *
 *   1. 状态条     —— 运行 / 审核 / 发布三个权威各自的现状
 *   2. 生命周期   —— 六段走到哪了，每段留下了什么
 *   3. Agent 决策轨迹 —— 这个 Agent 为什么这么走（§11，整个控制台的核心）
 *   4. 运行质量   —— 这一轮决策循环健不健康（只有 Agent 模式的 Run 有）
 *   5. 研究素材 / 内容 / 规则检查 / 发布 —— 具体材料
 *   6. 运行信息   —— 精确时间戳，对账用
 *
 * 反过来的顺序（先铺材料、结论沉在最下面）正是"渲染 API 的页面"和"给人用的
 * 控制台"之间的差别：人来这里通常是为了回答一个具体问题，而不是为了通读一遍。
 */

function PublishBlock({ publish }: { publish: PublishRecord }) {
  return (
    <div className="stack stack--tight">
      <FieldGrid>
        <Field label="发布状态">
          <StatusBadge status={publish.status} kind="publish" />
        </Field>
        <Field label="发布时间">{dateTime(publish.published_at)}</Field>
        <Field label="记录更新时间">{dateTime(publish.updated_at)}</Field>
        <Field label="X 帖子 ID">
          {publish.x_post_id ? (
            <span className="row row--tight">
              <a
                href={xPostUrl(publish.x_post_id)}
                target="_blank"
                rel="noreferrer noopener"
                className="mono"
              >
                {publish.x_post_id}
              </a>
              <CopyButton value={publish.x_post_id} label="复制 X Post ID" />
            </span>
          ) : (
            // 没有 id 不代表失败：publish_unknown 的典型形态就是"发出去过，但拿不到 id"
            EMPTY
          )}
        </Field>
      </FieldGrid>

      {publish.last_error ? (
        <div className="notice notice--danger">
          <div className="notice__title">最后错误</div>
          <div className="body-block body-block--mono">{publish.last_error}</div>
        </div>
      ) : null}

      <div>
        <div className="meta__label">实际发出的正文</div>
        <div className="body-block body-block--authoritative">
          {publish.body ? text(publish.body) : EMPTY}
        </div>
      </div>
    </div>
  )
}

/** Agent 运行的质量指标。与轨迹同一个门槛：非 Agent 的 Run（quality 为 null）
 *  这一整段不出现，页面与从前逐像素一致。 */
function AgentQualitySection({ detail }: { detail: AdminRunDetail }) {
  const quality = detail.quality
  if (!quality) return null

  return (
    <DetailSection title="运行质量" hint={criticOutcomeSummary(quality)}>
      <FieldGrid>
        <Field label="总步数">
          {quality.total_steps}
          {quality.budget_exhausted ? (
            <span className="cell-muted">（已用满预算，是被步数上限停下的）</span>
          ) : null}
        </Field>
        <Field label="检索次数">{quality.research_calls}</Field>
        <Field label="候选数">{quality.candidate_count}</Field>
        <Field label="改稿次数">{quality.revision_count}</Field>
      </FieldGrid>
      <Field label="结束原因">
        {/* 空串是"没留下理由"，不是"理由为空"——所以显示成一句话而不是空白。 */}
        {quality.finish_reason || '这一步没有留下理由'}
      </Field>
    </DetailSection>
  )
}

/**
 * Agent 决策轨迹。
 *
 * 没有 goal 也没有轨迹的 Run（Phase 5A 之前的全部 Run）看到的东西与从前完全一致：
 * 这一段根本不出现。
 *
 * 这是整个控制台里最能说明"这是 Agent"的地方，所以给它最好的位置——紧跟在
 * 生命周期之后，在所有**材料**之前。把它排在研究素材后面，等于把"这个 Agent
 * 做了什么决定"降级成"一篇稿子的附录"。
 */
function AgentTraceSection({ detail }: { detail: AdminRunDetail }) {
  if (!detail.goal && detail.decision_trace.length === 0) return null

  return (
    <DetailSection title="Agent 决策轨迹" hint={traceSummary(detail.decision_trace) ?? undefined}>
      {detail.decision_trace.length === 0 ? (
        <div className="notice">
          这个 Run 没有 Agent 决策记录。直接给主题的 Run 本来就没有；Agent 模式的 Run
          则可能是第一步就被上游挡住，还没落下记录。
        </div>
      ) : (
        <TraceTimeline entries={detail.decision_trace} />
      )}
    </DetailSection>
  )
}

export function DetailBody({ detail, onSettled }: { detail: AdminRunDetail; onSettled: () => void }) {
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

  // 生命周期由纯函数算（`utils/lifecycle.ts`），组件只负责贴类名。
  // 每段的状态来自它自己的证据，不从 Run 的整体状态反推。
  const lifecycle = buildLifecycle({
    run_status: detail.run_status,
    created_at: detail.created_at,
    started_at: detail.started_at,
    research_count: detail.research_items.length,
    has_draft: draft !== null,
    draft_version: draft?.version ?? null,
    guardrail_passed: detail.guardrail_passed,
    review_action: review?.action ?? null,
    review_at: review?.reviewed_at ?? null,
    publish_status: publish?.status ?? null,
    publish_at: publish?.published_at ?? publish?.updated_at ?? null,
  })

  return (
    <>
      <div className="status-strip">
        <span className="status-strip__item">
          <span className="status-strip__label">运行状态</span>
          <StatusBadge status={detail.run_status} kind="run" />
        </span>
        <span className="status-strip__item">
          <span className="status-strip__label">审核决定</span>
          <StatusBadge status={detail.review_status} kind="review" />
        </span>
        <span className="status-strip__item">
          <span className="status-strip__label">发布状态</span>
          <StatusBadge status={detail.publish_status} kind="publish" />
        </span>
        <span className="status-strip__item">
          <span className="status-strip__label">运行时长</span>
          <span className="status-strip__value">{duration(seconds)}</span>
        </span>
      </div>

      {detail.error ? (
        <div className="notice notice--danger detail-notice">
          <div className="notice__title">Run 错误</div>
          <div className="body-block body-block--mono">{detail.error}</div>
        </div>
      ) : null}

      <DetailSection title="生命周期" hint="每一步都按它自己留下的记录判断">
        <LifecycleTimeline steps={lifecycle} />
      </DetailSection>

      <AgentTraceSection detail={detail} />

      <AgentQualitySection detail={detail} />

      <DetailSection
        title="研究素材"
        hint={detail.research_items.length ? `${detail.research_items.length} 条` : undefined}
      >
        <ResearchList items={detail.research_items} initialVisible={3} />
      </DetailSection>

      <DetailSection title="内容">
        <div className="stack stack--tight">
          <div>
            <div className="meta__label">
              最终内容 <span className="cell-muted">（来自 checkpoint）</span>
            </div>
            <div className="body-block">{detail.final_content ?? EMPTY}</div>
          </div>

          {review ? (
            <div>
              <div className="meta__label row row--tight">
                人工决定
                <StatusBadge status={review.action} kind="review" />
                <span className="cell-muted">{dateTime(review.reviewed_at)}</span>
              </div>
              <div className="meta__label body-label">原始内容（Agent 写的，未改动）</div>
              {/* 两块正文必须长得不一样：上面这块是存档，下面那块才是会发出去的。
                  同一种样式会让人分不清自己正在读哪一份。 */}
              <div className="body-block body-block--readonly">{review.original_content}</div>
              {review.edited_content !== null ? (
                <>
                  <div className="meta__label body-label">
                    编辑后的内容
                    {review.action === 'edit' ? '（这条才是实际发布的）' : ''}
                  </div>
                  <div className="body-block">{review.edited_content}</div>
                </>
              ) : null}
              {review.reject_reason ? (
                <>
                  <div className="meta__label body-label">拒绝原因</div>
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
      </DetailSection>

      <DetailSection title="规则检查" hint="来自 LangGraph checkpoint">
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
      </DetailSection>

      <DetailSection title="发布">
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
          <div className="detail-action">
            <PublishControl runId={detail.run_id} body={publishableBody} onSettled={onSettled} />
          </div>
        ) : null}

        {isUnresolved(detail.publish_status) ? (
          <div className="notice notice--unknown detail-action">
            这个 Run 有一次<strong>结果未定的发布尝试</strong>。请先到 X 上确认账号，再到
            <Link to="/published?status=__unresolved__">发布记录</Link>
            页裁决 —— 这个抽屉不会代你发布。
          </div>
        ) : null}
      </DetailSection>

      <DetailSection title="运行信息">
        <FieldGrid>
          <Field label="创建时间">{dateTime(detail.created_at)}</Field>
          <Field label="开始时间">{dateTime(detail.started_at)}</Field>
          <Field label="完成时间">{dateTime(detail.finished_at)}</Field>
          {/* 只有 Agent 模式的 Run 才有目标。这里**不与 topic 合并**：goal 是人给的
              输入，topic 是 Agent 从中选出的结果，合成一个字段就分不清"Agent 选了没"。 */}
          {detail.goal ? (
            <Field label="目标">
              <span title={detail.goal}>{detail.goal}</span>
            </Field>
          ) : null}
        </FieldGrid>
        <p className="cell-muted scope-note">
          这里存的是审计记录，不是 X 的实时镜像 —— 帖子之后在 X 上被删掉，这里也不会跟着变。
          发布状态才是权威字段，上面的运行状态只是它的一个镜像视图。
        </p>
      </DetailSection>
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
          <div className="row row--tight review-head">
            <StatusBadge status={data.run_status} kind="run" />
            <span className="row row--tight review-head__id">
              <span className="mono" title={data.run_id}>
                {data.run_id}
              </span>
              <CopyButton value={data.run_id} label="复制 Run ID" />
            </span>
          </div>
          <div className="cell-muted review-head__note">
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

  const xPostId = data?.publish?.x_post_id ?? null

  return (
    <Drawer
      onClose={onClose}
      title={
        // 题目还没选出来时退回中性标题，不拿 goal 顶替（见 DetailBody 里的说明）。
        <h2 className="drawer__title">{data?.topic ?? 'Run 详情'}</h2>
      }
      subtitle={
        // 状态徽标放在标题下面而不是标题右边：题目可以很长，挤在一行会把徽标顶出去。
        <div className="row row--tight">
          <StatusBadge status={data?.run_status} kind="run" />
          <span className="row row--tight drawer__id">
            <span className="mono" title={runId}>
              {runId}
            </span>
            <CopyButton value={runId} label="复制 Run ID" />
          </span>
        </div>
      }
      actions={
        xPostId ? (
          <a
            className="btn btn--sm"
            href={xPostUrl(xPostId)}
            target="_blank"
            rel="noreferrer noopener"
          >
            <IconExternal size={14} />
            在 X 中打开
          </a>
        ) : null
      }
    >
      {loading ? <LoadingState message="正在加载 Run…" variant="lines" /> : null}
      {error ? <ErrorState error={error} what="加载该 Run 失败。" onRetry={reload} /> : null}
      {data && !loading ? <DetailBody detail={data} onSettled={onSettled} /> : null}
    </Drawer>
  )
}
