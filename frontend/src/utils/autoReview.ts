/**
 * 自动发布在控制台上的措辞（Phase auto-publish）。
 *
 * 抽成纯函数而不是写在组件里，理由与 `viewpoint.ts` / `learning.ts` 一样：能被
 * `node --test` 直接跑，而这个仓库的前端测试只测纯函数。这里更值得抽——它承担的
 * 全部风险都在**措辞**上，而措辞错的方式是安静的：
 *
 *   - `null` 显示成 `0`：把"还没有数据"说成"数据说不行"；
 *   - 把机器的"不自动放行"说成"拒绝"：人会以为这条已经结束了，不再去看；
 *   - 把 `gate.ready` 读成"可以转正式了"：那几项机器算得出来的判据全过，**不等于**
 *     门槛已满足（还有人工复核与金丝雀观察）。
 *
 * 三件事都不会让任何东西崩，只会让人读错，然后按错的结论去按按钮。所以下面几乎
 * 每条规则都有一个**否定式**断言钉着。
 *
 * ## 与「规则检查」的分工
 *
 * 这一层说的是**自动发布这套功能自己的判断**：内容闸门 + 交付候选的 Critic 裁决
 * + 重复检测 + 当日额度，四项全过才叫通过。它和 checkpoint 里那个 `guardrail_passed`
 * 是两套东西，措辞上必须分得开——一块说"通过"、另一块说"不通过"是完全可能的组合，
 * 而那正是人最需要看见的组合。
 */

import type {
  AutoPublishAgreement,
  AutoPublishGate,
  AutoPublishStatus,
  AutoReviewSummary,
  RunOrigin,
} from '../types/api'

/** `agent_run.origin` → 界面文案。 */
export const ORIGIN_LABELS: Record<RunOrigin, string> = {
  manual: '人工发起',
  scheduled: '自动调度',
}

/** 未知取值（后端加了新模式而前端没跟上）就原样显示，**不**兜底成"影子模式"。 */
export function originLabel(origin: string): string {
  return ORIGIN_LABELS[origin as RunOrigin] ?? origin
}

const MODE_LABELS: Record<string, string> = {
  shadow: '影子模式',
  live: '正式模式',
}

const VERDICT_LABELS: Record<string, string> = {
  approve: '通过',
  reject: '不通过',
}

export function modeLabel(mode: string | null | undefined): string {
  if (!mode) return '—'
  return MODE_LABELS[mode] ?? mode
}

export function verdictLabel(verdict: string): string {
  return VERDICT_LABELS[verdict] ?? verdict
}

/**
 * 交付候选的 Critic 分数。
 *
 * **`null` 显示成「未评价」，不是 `0.0`。** 交付候选可能压根没被 Critic 看过，
 * 而判据在那种情况下是**拒绝**；显示 0 会把它说成"评过了，很差"——两件事后续
 * 的处置不一样（前者要去看为什么没评价，后者要看内容）。
 */
export function criticScoreLabel(score: number | null | undefined): string | null {
  if (score === null || score === undefined) return null
  return score.toFixed(1)
}

/** 一句话标题：`自动审阅：不通过（影子模式）`。 */
export function autoReviewHeadline(auto: AutoReviewSummary): string {
  return `自动审阅：${verdictLabel(auto.verdict)}（${modeLabel(auto.mode)}）`
}

/**
 * 这条自动结论**对人意味着什么**。
 *
 * 这里最要紧的一件事：机器说"不通过"**不是拒绝**。它只是没走快车道，Run 仍然停在
 * `waiting_for_review`，人照样可以批准。写成"已被拒绝"会让人跳过这条——而它明明
 * 还在队列里等人。
 *
 * 第二种要紧：影子模式下判**通过**也什么都不发。差别只在正式模式。
 */
export function autoReviewHint(auto: AutoReviewSummary): string {
  if (auto.mode === 'shadow') {
    return '影子模式：机器只记录判断，一条都不会发。这条内容是否发布仍然完全由你决定。'
  }
  if (auto.verdict === 'reject') {
    return '正式模式下机器没有放行这条，所以它留在了队列里等你。机器不通过不等于拒绝——你仍然可以批准。'
  }
  return '正式模式下机器判了通过。正常情况它会自己走发布、不会出现在这里；它还在这里，说明下一步没有走完，请手动确认。'
}

/**
 * 一致率。
 *
 * **`null` 必须显示成 `—` 并说明原因**，绝不显示 `0.0%`：这个仓库里
 * `agreement_rate` 是 `float | None`，`null` 的含义是"还没有已决配对"。用一个
 * 0% 去表达它，界面就会在一个什么都没发生的系统上显示一个吓人的数字。
 */
export function rateLabel(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return '—'
  return `${(rate * 100).toFixed(1)}%`
}

export function agreementHeadline(agreement: AutoPublishAgreement): string {
  if (agreement.agreement_rate === null) {
    return agreement.decided_pairs === 0
      ? '一致率 —：还没有已决配对'
      : '一致率 —：配对数不足以算出比率'
  }
  return `一致率 ${rateLabel(agreement.agreement_rate)}（${agreement.agreed}/${agreement.decided_pairs} 个已决配对）`
}

/**
 * `false_approve` 那一句。它是**最危险的那个错**，所以三种情形要给三种不同的话，
 * 而不是统一说一句"误放行 N 次"：
 *
 * - 有一次就是硬阻断：必须是感叹句，且要指出下一步是查判据，不是继续攒数据；
 * - 一次都没放行过（`auto_approvals === 0`）：**不能**说成"零误放行"。分母是 0，
 *   这个 0 什么也没证明——用 `0/0` 冒充"表现良好"是这一整页最容易犯的错。
 *   注意分母是"**已决配对里**机器判通过的次数"，所以措辞必须限定在这个口径里，
 *   不能写成"机器一共放行了 0 次"（正式模式下它可能放过、只是没人审）；
 * - 放行过且没被拒：这才是真的好消息。
 */
export function falseApproveLine(agreement: AutoPublishAgreement): string {
  if (agreement.false_approve > 0) {
    return `出现过 ${agreement.false_approve} 次"自动放行、人却拒绝"——这是最危险的错，转正式前必须先查清是哪条判据漏了。`
  }
  if (agreement.auto_approvals === 0) {
    return '这些已决配对里，自动侧一次都没有放行过，所以现在谈不上误放行——这个 0 只说明还没有数据。'
  }
  return `自动侧放行过 ${agreement.auto_approvals} 次，没有一次被人拒绝。`
}

/** 误拦那一句。**它不是错**：代价只是一条队列记录，不是发出去的烂帖。 */
export function falseRejectLine(agreement: AutoPublishAgreement): string {
  if (agreement.false_reject === 0) return '没有出现"机器拦下、人本来会发"的情况。'
  return `有 ${agreement.false_reject} 次"机器拦下、人本来会发"。这不是错误——代价只是多了一条队列记录——但如果都集中在同一条判据上，转正式前要考虑调松它。`
}

/** `5/7 项达标`。**不含"可以转正式了"这个意思**，见 `AUTO_PUBLISH_GATE_DISCLAIMER`。 */
export function gateSummary(gate: AutoPublishGate): string {
  const met = gate.criteria.filter((criterion) => criterion.met).length
  return `机器能算的 ${gate.criteria.length} 项判据里达标 ${met} 项`
}

export const AUTO_PUBLISH_GATE_DISCLAIMER =
  '这几项全过只说明"数据上够格了"。转正式还要人另外走完下面那几步——那几步机器算不出来。'

/**
 * 总开关那一句。
 *
 * 配置读不了是**安全**的（tick 会跳过本轮、什么都不发），所以这里也不写成
 * 一句报错：写成"读不了配置，所以一切照旧没有动"，比一个红色块更准确。
 */
export function switchSentence(
  status: Pick<AutoPublishStatus, 'enabled' | 'mode' | 'config_error'>,
): string {
  if (status.config_error) {
    return `配置文件读不了（${status.config_error}）。定时脚本会跳过本轮，一条都不会发。`
  }
  if (!status.enabled) {
    return '总开关关着：定时脚本连 Run 都不建。这是唯一的刹车，改 config/automation.yaml 后立即生效。'
  }
  if (status.mode === 'live') {
    return '总开关开着，正式模式：判据统计通过的内容会被自动发布。'
  }
  return '总开关开着，影子模式：判断照常记录、进队列，但一条都不会发。'
}
