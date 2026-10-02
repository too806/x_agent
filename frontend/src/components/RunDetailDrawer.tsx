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
import {
  FEEDBACK_DISCLAIMER,
  feedbackHeadline,
  hoursSinceLabel,
  metricLabel,
  observationLine,
  signalHeadline,
} from '../utils/feedback'
import { buildLifecycle } from '../utils/lifecycle'
import { LEARNING_DISCLAIMER, compactReadiness } from '../utils/learning'
import { proposalsHeadline } from '../utils/proposals'
import { traceSummary } from '../utils/trace'
import {
  capNote,
  confidenceBreakdown,
  confidenceLabel,
  scopeSentence,
  scoreLabel,
  stanceSentence,
} from '../utils/viewpoint'
import { isUnresolved } from '../types/api'
import {
  EMPTY,
  criticOutcomeSummary,
  dateTime,
  draftStatusLabel,
  duration,
  text,
} from '../utils/format'
import type {
  AdminRunDetail,
  AiStyleSignals,
  PublishRecord,
  RunStyleSummary,
  RunViewpointSummary,
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
 *
 * 阅读顺序按"从结论到材料"排：
 *
 *   1. 状态条     —— 运行 / 审核 / 发布三个权威各自的现状
 *   2. 生命周期   —— 六段走到哪了，每段留下了什么
 *   3. Agent 决策轨迹 —— 这个 Agent 为什么这么走（§11，整个控制台的核心）
 *   4. 运行质量   —— 这一轮决策循环健不健康（只有 Agent 模式的 Run 有）
 *   5. 核心判断   —— 这一轮主张什么、凭什么、反方在哪（Phase 5B.3，同上）
 *   6. 写作结构   —— 这一轮打算怎么表达（Phase 5B.2，同上）
 *   7. 研究素材 / 内容 / 规则检查 / 发布 —— 具体材料
 *   8. 运行信息   —— 精确时间戳，对账用
 *
 * 5 在 6 前面：**判断决定说什么，写法只决定怎么说**。反过来摆，读者会
 * 把观点读成措辞的产物。
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
 * 这次运行采用的写作结构（Phase 5B.2）。
 *
 * 与 `AgentQualitySection` 同一个门槛：不是 Agent 的 Run（后端 `style` 为
 * null）这一段不出现，页面与从前逐像素一致。后端的 null 有两种来源——
 * 这个 Run 是 V1 topic 链路，或者它跑在 Phase 5B.2 之前——**两种情况界面
 * 都不该猜**：显示一个"结构：不指定"的块，会让人以为 Agent 主动选了中性
 * 结构，而它那会儿根本没有这个能力。
 *
 * 放在"运行质量"之后、材料之前：它和运行质量一样，回答的是"这一轮 Agent
 * 做了什么样的决定"，而不是"它拿到了什么材料"。
 *
 * **这里永远不会出现样本原文。** recipe 是生成约束，本身不含任何一条真实
 * 帖子的句子；这不是靠界面克制，是数据里就没有这个字段。
 */
/**
 * 目标长度：**语料观察**与**平台执行值**分开显示。
 *
 * 被平台上限压过时必须把两句都写出来。只显示 `effective_target_length`
 * 的话，界面上的 140 看起来像一条关于语料的结论（"这种写法就是写短句"），
 * 而语料说的是 180——人拿这个数去判断"我们学到的东西对不对"，就会往
 * 一个语料从未支持的方向下结论。没被压过时不显示那半句：恒显示会让
 * 一次没有冲突的运行也背上一句解释。
 */
function LengthLine({ style }: { style: RunStyleSummary }) {
  if (style.effective_target_length === null) return <>{EMPTY}</>
  const capped =
    style.observed_target_length !== null &&
    style.hard_max_length !== null &&
    style.hard_max_length < style.observed_target_length
  if (!capped) return <>约 {style.effective_target_length} 字</>
  return (
    <>
      不超过 {style.effective_target_length} 字
      <div className="meta">
        语料里典型长度是 {style.observed_target_length} 字，受发布硬上限{' '}
        {style.hard_max_length} 字约束，本次执行值取前者与后者中较小的一个。
        <strong>这不是语料的结论。</strong>
      </div>
    </>
  )
}

function StyleSection({ detail }: { detail: AdminRunDetail }) {
  const style = detail.style
  if (!style) return null

  return (
    <DetailSection title="本次运行采用的写作结构">
      <FieldGrid>
        <Field label="结构">{style.structure_label}</Field>
        <Field label="开头">{style.hook_label}</Field>
        {/* 目标长度可能没定（recipe 允许 null），给 — 而不是 0：
            0 字是一个可执行的目标，与"没定"是完全不同的两件事。 */}
        <Field label="目标长度">
          <LengthLine style={style} />
        </Field>
        <Field label="结尾">{style.ending_label}</Field>
      </FieldGrid>

      <details className="disclosure">
        <summary>技术细节（其余约束、选择依据与正文痕迹）</summary>
        <div className="disclosure__body stack stack--tight">
          <FieldGrid>
            {/* 取值与标签并排给出：标签回答"这是什么意思"，取值让人能拿它
                去核对 prompt 与日志里出现的字符串。 */}
            <Field label="结构（取值）">
              <span className="mono">{style.structure_type}</span>
            </Field>
            <Field label="开头（取值）">
              <span className="mono">{style.hook_type}</span>
            </Field>
            <Field label="信息密度">{style.density_label}</Field>
            <Field label="立场强度">{style.stance_label}</Field>
            <Field label="证据要求">{style.evidence_label}</Field>
            <Field label="语气">{style.tone_label}</Field>
          </FieldGrid>

          {/* 三个长度并排给出，让人一眼看出哪个是语料说的、哪个是执行值。
              只留执行值的话，那个数会被当成语料的结论。 */}
          <FieldGrid>
            <Field label="语料目标长度">
              {style.observed_target_length === null
                ? EMPTY
                : `${style.observed_target_length} 字`}
            </Field>
            <Field label="本次执行目标长度">
              {style.effective_target_length === null
                ? EMPTY
                : `${style.effective_target_length} 字`}
            </Field>
            <Field label="发布硬上限">
              {style.hard_max_length === null ? EMPTY : `${style.hard_max_length} 字`}
            </Field>
          </FieldGrid>

          <div className="meta">
            <div className="meta__label">选择依据</div>
            {style.reasons.length === 0 ? (
              // 空 reasons 是"没留下依据"，不是"依据为空"。直说。
              <div>这次没有留下选择依据。</div>
            ) : (
              <ul className="stack stack--tight">
                {style.reasons.map((reason, index) => (
                  <li key={index}>{reason}</li>
                ))}
              </ul>
            )}
          </div>

          <div className="meta">
            <div className="meta__label">依据的语料统计</div>
            <div className="mono">{style.profile_ref || '（没有记录来源）'}</div>
          </div>

          <div className="meta">
            <div className="meta__label">正文的套话痕迹（确定性计数）</div>
            <AiSignalsLine signals={style.ai_signals} />
          </div>
        </div>
      </details>
    </DetailSection>
  )
}

/**
 * 这些反馈够不够可靠，可以作为未来学习材料（Phase 5C）。
 *
 * **这一块不属于这个 Run。** 它回答的是整个账号的问题——"我们过去的反馈够不够格"
 * ——与这条 Run 是否成功、写了什么无关。放在这里是因为 Run Detail 是人唯一会逐条
 * 细看内容的地方：看完一条帖子的表现，紧接着最自然的疑问就是"那这些数据算数吗"。
 *
 * 因此它**单独取** `/api/admin/learning`，不并进 Run Detail 的响应。理由有两条：
 *
 *   1. 并进去会让每一次打开抽屉都先装配完所有已发布帖子的 checkpoint 才能显示，
 *      而这一块通常只是顺带看一眼——那就是拿最重的读去挡最常走的路；
 *   2. 它是**旁注**，不是详情的一部分。它读不到时详情照样完整，所以这里不摆
 *      `ErrorState`（一个侧栏请求失败配一整块错误提示，与它的分量不相称），
 *      但也不装作一切正常——如实说它暂时读不到。
 *
 * 调用方按"有没有真的发出去"门控（与 `PerformanceSection` 同一条件），
 * 所以这个组件只在需要时才挂载，请求也只在需要时才发。
 */
function LearningReadinessSection() {
  const { data, loading, error } = useAsync(() => api.getLearning(), [])

  return (
    <DetailSection
      title="学习准备度"
      hint={data ? '全账号 · 不属于这个 Run · 只读评估' : undefined}
    >
      {error ? (
        <div className="meta">
          学习准备度暂时读不到（这是一次单独的只读请求，不影响上面的详情）。
        </div>
      ) : !data ? (
        <div className="meta">{loading ? '正在评估…' : '暂时读不到学习准备度。'}</div>
      ) : (
        <>
          {/* 一句话，永远带 n 与窗口：一个没有样本量的结论没法被判断为"样本太小"。 */}
          <div>{compactReadiness(data)}</div>
          {/* 提案条数（Phase 5D）**来自同一个响应**（`proposal_count`），所以这里
              不另发一次请求：这个抽屉每打开一次都要拉一次准备度，再加一次同样重的
              比较读取，就是拿最重的读去挡最常走的路。标题走**同一个**函数，
              免得两处各写一句、迟早互相矛盾。

              只报条数，不在这里裁决——裁决要看清证据、样本与边界，那是 Analytics
              那一块才能给的全部上下文。 */}
          <div>
            {proposalsHeadline({
              available: data.available,
              reason: data.reason,
              observation_window: data.observation_window,
              learning_ready: data.readiness.learning_ready,
              proposal_count: data.proposal_count,
            })}
          </div>
          <div className="meta">{LEARNING_DISCLAIMER}</div>
        </>
      )}
    </DetailSection>
  )
}

/**
 * 这条帖子**后来被观察到的**结果（Phase 5B.4）。
 *
 * 与 `FeedbackSection` 是两件事，并且**必须分成两段**：
 *
 *   - 这一段来自快照表，回答"这条帖子现在怎么样"——它还会继续变；
 *   - 那一段来自 checkpoint，回答"这次运行当时看到了什么"——它永远不变。
 *
 * 合成一段的话，人就会拿今天的点赞数去解释当时 Agent 为什么那么写。
 *
 * 门槛与其它段不同：**不是**看 `detail.performance` 是否为空，而是看这个 Run
 * 有没有一条真正带着 `x_post_id` 的发布记录。没发出去的 Run 不出现这一段
 * ——一个"发都没发出去"的 Run 下面挂一句"还没有观察记录"，会被读成"发了但没人看"。
 */
function PerformanceSection({ detail }: { detail: AdminRunDetail }) {
  const publish = detail.publish
  if (!publish || !publish.x_post_id) return null
  const observed = detail.performance

  return (
    <DetailSection
      title="这条帖子的后续观察"
      hint={observed ? observationLine(observed.observation_count, observed.latest_observed_at) : undefined}
    >
      {observed === null ? (
        <div className="notice">
          <div className="notice__title">还没有观察记录</div>
          这条帖子确认发出去了（x_post_id 为 <span className="mono">{publish.x_post_id}</span>），
          但从没跑过采集，所以本系统对它的表现<strong>一无所知</strong>。
          这不是"没人互动"——是没看过。采集由
          <span className="mono">scripts/refresh_post_metrics.py</span> 手工触发。
        </div>
      ) : (
        <>
          <FieldGrid>
            {/* 每一个计数都走 metricLabel：未知显示「暂无数据」，**不是** 0。
                这两者在界面上长得不一样，是这一整块存在的意义。 */}
            <Field label="点赞">{metricLabel(observed.likes)}</Field>
            <Field label="回复">{metricLabel(observed.replies)}</Field>
            <Field label="转发">{metricLabel(observed.reposts)}</Field>
            <Field label="引用">{metricLabel(observed.quotes)}</Field>
          </FieldGrid>

          <FieldGrid>
            <Field label="发布时刻">{dateTime(observed.published_at)}</Field>
            <Field label="最近观察">{dateTime(observed.latest_observed_at)}</Field>
            <Field label="发布后时长">{hoursSinceLabel(observed.hours_since_publish)}</Field>
            <Field label="观察次数">{observationLine(observed.observation_count, null)}</Field>
          </FieldGrid>

          <div className="meta">
            {/* 取的是**最新**一条观察，不是各次之和。求和会造出一个任何一个
                时刻都不曾成立的数——计数是单调累计的，相加等于把同一个人
                数了一遍又一遍。 */}
            上面的计数是<strong>最新一次观察</strong>读到的公开数字，
            不是各次观察之和。曝光与书签不在采集范围内（v1 只读公开计数），
            因此它们在这里不显示，也不显示 0。
          </div>
          <div className="meta">{FEEDBACK_DISCLAIMER}</div>
        </>
      )}
    </DetailSection>
  )
}

/**
 * 这次运行**读到的**反馈摘要（Phase 5B.4）。
 *
 * 门槛与 `AgentQualitySection` 相同（非 Agent 运行的 `feedback` 为 null，
 * 这一段不出现）。
 *
 * 最要紧的一件事是**把三种"没有"分开**，否则读的人会把它们混成一种：
 *
 *   1. `available=false` —— 这次运行压根没读到反馈（5B.4 之前的运行，
 *      或者取用失败）。后端给的原因原样显示，前端不翻译、不改写。
 *   2. `available=true` 但一条信号都没有 —— 读到了，但当时还没有可观察的东西。
 *   3. 有信号 —— 正常。
 *
 * `limitations` 一条都不筛：被筛掉的那条通常恰好是最该被读到的那条。
 */
function FeedbackSection({ detail }: { detail: AdminRunDetail }) {
  const feedback = detail.feedback
  if (!feedback) return null

  return (
    <DetailSection
      title="这次运行读到的反馈"
      hint={feedback.available ? undefined : '没有读到'}
    >
      <div>
        <div className="meta__label">当时对过去发布结果的了解</div>
        <div>{feedbackHeadline(feedback)}</div>
        {!feedback.available && feedback.reason ? (
          // 原因必须印出来：两种"没有"（键不在 vs 键在但为空）在后端就是
          // 两条不同的常量，界面把它们合并成一句就等于抹掉了这个区别。
          <div className="meta">{feedback.reason}</div>
        ) : null}
      </div>

      {feedback.available ? (
        <FieldGrid>
          <Field label="观察条数">{feedback.sample_count}</Field>
          <Field label="涉及帖子">{feedback.post_count}</Field>
          <Field label="依据快照">{feedback.snapshot_count}</Field>
          <Field label="最近观察">{dateTime(feedback.latest_observed_at)}</Field>
        </FieldGrid>
      ) : null}

      {feedback.available && feedback.signals.length > 0 ? (
        <div>
          <div className="meta__label">观察到的信号（{feedback.signals.length} 条）</div>
          <ul className="stack stack--tight">
            {feedback.signals.map((signal, index) => (
              <li key={index}>
                {/* 样本量印在每一条结论上：没有 n 的结论无法被判断为
                    "样本太小"，而样本太小恰恰是这批数据最常见的问题。 */}
                <div className="meta__label">{signalHeadline(signal)}</div>
                <div>{signal.statement}</div>
                {signal.evidence.length > 0 ? (
                  // 证据 id 是可核对的把手：一个不能回到原始观察的结论，
                  // 和一个不能核对的分数一样没用。
                  <div className="meta">
                    <span className="mono cell-muted">{signal.evidence.join('、')}</span>
                  </div>
                ) : null}
                {/* comparison_group 与 confidence 恒为 null（v1 没有基线，
                    也没有校准过的置信度）。这里**不显示**这两项：给一个
                    恒为空的字段配一句"暂无"，只会让人以为它本来该有值。 */}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {feedback.limitations.length > 0 ? (
        <div className="meta">
          <div className="meta__label">这份摘要没有回答什么</div>
          <ul className="stack stack--tight">
            {feedback.limitations.map((note, index) => (
              <li key={index}>{note}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="meta">{FEEDBACK_DISCLAIMER}</div>

      {feedback.source_snapshot_ids.length > 0 ? (
        <details className="disclosure">
          <summary>技术细节（依据的原始观察）</summary>
          <div className="disclosure__body">
            <div className="mono">{feedback.source_snapshot_ids.join('\n')}</div>
          </div>
        </details>
      ) : null}
    </DetailSection>
  )
}

/**
 * 交付正文的确定性 AI 味计数。
 *
 * **这是计数，不是判定。** 措辞上必须一直说"出现了几次"，不能说成评分或
 * 概率：这几个词表判不了"这是 AI 写的"，把它渲染成一个分数就等于宣称它能。
 * 全零时说"未发现"，与"没测"（signals 为 null）分开说。
 */
function AiSignalsLine({ signals }: { signals: AiStyleSignals | null }) {
  if (!signals) {
    return <div>没有交付正文，因此没有可计算的痕迹。</div>
  }

  const counts: [string, number][] = [
    ['模板化开头', signals.template_opener_count],
    ['空洞连接词', signals.empty_connective_count],
    ['虚假权威措辞', signals.false_authority_count],
    ['套话', signals.generic_phrase_count],
    ['模糊限定词', signals.hedge_count],
    ['重复开头', signals.repeated_opener_count],
  ]
  const hits = counts.filter(([, count]) => count > 0)

  if (hits.length === 0) {
    return <div>{signals.char_count} 字，未发现以上痕迹。</div>
  }

  return (
    <div>
      {signals.char_count} 字：{hits.map(([name, count]) => `${name} ×${count}`).join('、')}
      {signals.generic_phrases.length > 0 ? (
        // 命中的套话原文印出来，人才能反驳"这个也算套话？"——
        // 一个不能核对的信号，和一个不能核对的分数一样没用。
        <span className="cell-muted">（套话：{signals.generic_phrases.join('、')}）</span>
      ) : null}
    </div>
  )
}

/**
 * 这次运行形成的核心判断（Phase 5B.3，工作单第三十五节）。
 *
 * 与 `AgentQualitySection` / `StyleSection` 同一个门槛：不是 Agent 的 Run
 * （后端 `viewpoint` 为 null）这一段不出现，页面与从前逐像素一致。
 *
 * **首屏只有五件事**：核心判断 / 证据 / 反方 / 风险 / 置信度。分数、推理层、
 * 评估明细、其它候选、范围信号全部折进 `<details>`——工作单第三十六节明确
 * 要求不做分析实验室界面，而这个块的用途是**让人做审核判断**，不是让人读一份
 * 评估报告。但折叠区必须给全：没有它们，"置信度 0.83"就是一个无法追问的数。
 *
 * 排在最前面，在写作结构之前：**判断决定说什么，写作结构只决定怎么说**
 * （工作单第二十五节）。反过来摆，读者会以为立场是写法派生的。
 */
function ViewpointSection({ detail }: { detail: AdminRunDetail }) {
  const viewpoint: RunViewpointSummary | null = detail.viewpoint
  if (!viewpoint) return null

  const assessment = viewpoint.assessment
  const score = scoreLabel(viewpoint.viewpoint_score)

  return (
    <DetailSection title="核心判断" hint={viewpoint.stance_label || undefined}>
      <div>
        <div className="meta__label">这条稿子主张的判断</div>
        {/* 与"最终正文"同样的权威样式：这是这一轮真正被交付出去的那个判断，
            不是候选、不是草稿。 */}
        <div className="body-block body-block--authoritative">{viewpoint.claim}</div>
        {viewpoint.why_it_matters ? (
          <div className="meta">{viewpoint.why_it_matters}</div>
        ) : null}
      </div>

      <FieldGrid>
        <Field label="置信度">
          {/* 措辞纪律：这是 0–1 的加权结果，**不是**"有 83% 的概率是对的"。
              故意不写成百分比，也故意不写成"评分"。 */}
          <span className="mono">{confidenceLabel(viewpoint.confidence)}</span>
          <div className="meta">
            由证据支撑度 / 来源多样性 / 反方意识 / 未知压力四项加权算出，
            <strong>不含新颖性</strong>。
          </div>
        </Field>
        <Field label="立场">
          {stanceSentence(viewpoint)}
          {!viewpoint.assessment ? (
            // 5B.3 之前的运行：这个数是模型自己给的，与上面那句推导**不是一回事**。
            <div className="meta">
              这个 Run 跑在 Phase 5B.3 之前，当时的置信度由模型直接给出，
              没有可复算的评估明细。
            </div>
          ) : null}
        </Field>
        <Field label="适用范围">{scopeSentence(viewpoint)}</Field>
      </FieldGrid>

      <div>
        <div className="meta__label">
          支持这个判断的事实（{viewpoint.evidence.length} 条）
        </div>
        {viewpoint.evidence.length === 0 ? (
          <div className="notice notice--danger">
            这条判断一条证据都没引上。没有依据的判断不是"差一点的判断"，
            它是一个会被漂亮话讲出来的断言。
          </div>
        ) : (
          <ul className="stack stack--tight">
            {viewpoint.evidence.map((item) => (
              <li key={item.fact_id}>
                <span className="mono cell-muted">{item.fact_id}</span> {item.text}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div>
        <div className="meta__label">反方（{viewpoint.counterpoints.length} 条）</div>
        {viewpoint.counterpoints.length === 0 ? (
          // "没写反方"有两种：证据包里本来就没有，或者有却当它不存在。
          // 界面上不猜是哪一种——那由后端规则判定（RULE-022）。这里只说没有。
          <div className="cell-muted">这条判断没有写下任何反方意见。</div>
        ) : (
          <ul className="stack stack--tight">
            {viewpoint.counterpoints.map((point, index) => (
              <li key={index}>{point}</li>
            ))}
          </ul>
        )}
        {viewpoint.counterpoint_evidence.length > 0 ? (
          <div className="meta">
            其中引到事实的：
            {viewpoint.counterpoint_evidence.map((item) => (
              <div key={item.fact_id}>
                <span className="mono cell-muted">{item.fact_id}</span> {item.text}
              </div>
            ))}
          </div>
        ) : null}
      </div>

      <Field label="风险">
        {viewpoint.risk || '这条判断没有说明自己可能错在哪里。'}
      </Field>

      <details className="disclosure">
        <summary>技术细节（推理层、范围信号与评估依据）</summary>
        <div className="disclosure__body stack stack--tight">
          <Field label="推理层">
            {/* 这一层是工作单第六节的"事实 → 推理 → 判断"三段里的中间一段。
                它空着时要说清楚：没有它，"判断"与"事实"之间就是一个
                无法检查的跳跃。 */}
            {viewpoint.reasoning || (
              <span className="cell-muted">
                没有写出从事实走到这条判断的那一步。
              </span>
            )}
          </Field>

          <FieldGrid>
            <Field label="观点分">
              {score ? <span className="mono">{score}</span> : EMPTY}
              <div className="meta">
                排序用（0–100），回答"值不值得拿出来说"，
                <strong>与置信度不是同一个问题</strong>。
              </div>
            </Field>
            <Field label="立场（取值）">
              <span className="mono">{viewpoint.stance || EMPTY}</span>
            </Field>
            <Field label="模型申报的立场">
              <span className="mono">{viewpoint.stance_declared || EMPTY}</span>
            </Field>
            <Field label="证据允许的上界">
              <span className="mono">{viewpoint.max_stance || EMPTY}</span>
            </Field>
            <Field label="申报范围（取值）">
              <span className="mono">{viewpoint.claim_scope || EMPTY}</span>
            </Field>
            <Field label="证据范围（取值）">
              <span className="mono">{viewpoint.evidence_scope || EMPTY}</span>
            </Field>
          </FieldGrid>

          {viewpoint.assumptions.length > 0 ? (
            <div className="meta">
              <div className="meta__label">这条判断依赖的假设</div>
              <ul className="stack stack--tight">
                {viewpoint.assumptions.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {viewpoint.implications.length > 0 ? (
            <div className="meta">
              <div className="meta__label">
                影响（以下是对将来的推断，不是已发生的事实）
              </div>
              <ul className="stack stack--tight">
                {viewpoint.implications.map((item, index) => (
                  <li key={index}>{item}</li>
                ))}
              </ul>
            </div>
          ) : null}

          {assessment ? (
            <>
              <div className="meta">
                <div className="meta__label">置信度是怎么算出来的</div>
                <div className="mono">{confidenceBreakdown(assessment)}</div>
                {/* 封顶只在真的咬住时说明——恒显示会让人不再读它。 */}
                {capNote(
                  '证据支撑度',
                  assessment.evidence_support_raw,
                  assessment.evidence_support,
                ) ? (
                  <div>
                    {capNote(
                      '证据支撑度',
                      assessment.evidence_support_raw,
                      assessment.evidence_support,
                    )}
                  </div>
                ) : null}
                {capNote(
                  '置信度',
                  assessment.confidence_raw,
                  viewpoint.confidence,
                ) ? (
                  <div>
                    {capNote('置信度', assessment.confidence_raw, viewpoint.confidence)}
                  </div>
                ) : null}
              </div>

              <FieldGrid>
                <Field label="绑定事实">
                  {assessment.bound_facts} / {assessment.total_facts}
                </Field>
                <Field label="绑定来源 / 作者">
                  {assessment.bound_sources} / {assessment.bound_authors}
                </Field>
                <Field label="反方（绑定 / 声明）">
                  {assessment.bound_counterpoints} / {assessment.declared_counterpoints}
                </Field>
                <Field label="未被事实引用的材料">
                  {assessment.unreferenced_materials} / {assessment.total_materials}
                </Field>
              </FieldGrid>

              <div className="meta">
                <div className="meta__label">评估依据</div>
                {assessment.reasons.length === 0 ? (
                  <div>这次没有留下评估依据。</div>
                ) : (
                  <ul className="stack stack--tight">
                    {assessment.reasons.map((reason, index) => (
                      <li key={index}>{reason}</li>
                    ))}
                  </ul>
                )}
              </div>

              {assessment.breadth_markers.length > 0 ? (
                <div className="meta">
                  <div className="meta__label">判断里出现的全称量词</div>
                  <div className="mono">{assessment.breadth_markers.join('、')}</div>
                </div>
              ) : null}
            </>
          ) : null}

          {viewpoint.other_candidates.length > 0 ? (
            <div className="meta">
              <div className="meta__label">同一轮里落选的判断</div>
              <ul className="stack stack--tight">
                {viewpoint.other_candidates.map((other) => (
                  <li key={other.claim}>
                    <span className="mono cell-muted">
                      {scoreLabel(other.viewpoint_score) ?? EMPTY}
                    </span>{' '}
                    {other.claim}
                    <span className="cell-muted">
                      {other.stance_label ? `（${other.stance_label}）` : ''}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      </details>
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

      {/* 判断在写作结构之前：这一轮**说什么**决定它怎么说，不是反过来。
          与 `quality` 同一门控（非 Agent 运行返回 null，整段不出现）。 */}
      <ViewpointSection detail={detail} />

      <StyleSection detail={detail} />

      {/* 反馈两块排在一起，且**按时间顺序**排：
          先"这次运行当时读到了什么"（它是这次决策的输入），
          再"这条帖子后来怎么样"（它是这次决策的结果）。
          反过来摆，读的人会以为 Agent 当时就知道后面的数。 */}
      <FeedbackSection detail={detail} />

      <PerformanceSection detail={detail} />

      {/* 学习准备度紧跟在"这条帖子后来怎么样"之后：那是**一条帖子**的结果，
          这一块是**全账号**的可靠性判断。门控与 `PerformanceSection` 相同
          （没真的发出去的 Run 不谈"这些数据够不够格"——它连数据都还没有）。 */}
      {detail.publish?.x_post_id ? <LearningReadinessSection /> : null}

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
