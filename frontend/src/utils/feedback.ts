/**
 * 反馈块的**纯函数**部分（Phase 5B.4）。
 *
 * 抽出来而不是写在组件里，是为了能被 `node --test` 直接测：这个仓库的前端测试
 * 只测纯函数（没有 Jest / Vitest，也没有 DOM 环境）。
 *
 * 这一层承担的**唯一**风险是措辞，而措辞错的方式全是安静的：
 *
 *   - `null`（这次没读到）被渲染成 `0`——"没采到"就变成了一条"零互动"的结论；
 *   - 不同时刻的观察被加起来——造出一个任何时刻都不曾成立的数；
 *   - 观察被写成评价——"点赞最多"被读成"最好"。
 *
 * 所以下面几乎全是**否定式**的保证：`null` 不变成 `0`、不做加法、
 * 不出现"好/差/优秀"。
 */

import type { FeedbackSignalRow, RunFeedbackSummary } from '../types/api'

/** 未知计数的显示文本。**必须**是它，不能是 `0` 或 `—` 之外的任何东西。
 *
 * 用 `暂无数据` 而不是 `0`：界面上一旦出现 `0`，读的人就会得出"这条帖子
 * 没人互动"——而那与我们**没采到**是完全相反的两件事。用 `—` 也不够，
 * 那个符号在这个界面里已经表示"这个字段不适用"了。 */
export const NO_DATA = '暂无数据'

/**
 * 一个计数的显示文本。`null` / `undefined` → `暂无数据`。
 *
 * 这是整个反馈层最容易被改坏的一个函数，也是唯一一个**必须**有测试钉住的：
 * 把它改成 `value ?? 0` 不会有任何东西崩，只会让每一张没采过的帖子看起来
 * 像零互动。
 */
export function metricLabel(value: number | null | undefined): string {
  if (value === null || value === undefined) return NO_DATA
  return String(value)
}

/** 发布后经过了多久：`3.5` → `发布后 3.5 小时`。未知 → `暂无数据`。 */
export function hoursSinceLabel(hours: number | null | undefined): string {
  if (hours === null || hours === undefined) return NO_DATA
  return `发布后 ${hours} 小时`
}

/**
 * 这条帖子的观察摘要：`5 次观察，最近一次在 2026-09-28 03:00`。
 *
 * **不带任何计数**：这一行回答的是"我们看过几次"，不是"表现如何"。
 * 把数字塞进来会让人以为它是结论，而它只是一个采样次数。
 */
export function observationLine(
  observationCount: number,
  latestObservedAt: string | null | undefined,
): string {
  if (!observationCount) return '还没有观察记录'
  const when = latestObservedAt ? `，最近一次在 ${latestObservedAt}` : ''
  return `${observationCount} 次观察${when}`
}

/**
 * 这次运行**读到的**反馈那一块的一句话。
 *
 * 三种情形要长得不一样，否则读的人分不清"没读过""读过了但没数据""读到了"：
 *
 * 1. `available === false` → 直接给后端下发的原因（两种"没有"由后端区分：
 *    键不在 = 5B.4 之前的运行；键在但空 = 当时还没有采集过）；
 * 2. 读到了但一条信号都没有 → 说清"读到了 N 条观察但没有产出结论"；
 * 3. 正常 → 给样本量。
 */
export function feedbackHeadline(feedback: RunFeedbackSummary): string {
  if (!feedback.available) {
    return feedback.reason || '这次运行没有读到反馈。'
  }
  if (!feedback.signals.length) {
    return `这次运行读到 ${feedback.sample_count} 条观察，但没有产出可解释的信号。`
  }
  const posts = feedback.post_count ? `、${feedback.post_count} 条帖子` : ''
  return `这次运行读到 ${feedback.sample_count} 条观察${posts}。`
}

/**
 * 一条信号的标题行：种类的中文名 + 样本量。
 *
 * **样本量必须跟着每一个结论出现**：一个没有 n 的结论无法被判断为"样本太小"，
 * 而"样本太小"恰恰是这批数据最常见的问题。
 */
export function signalHeadline(signal: FeedbackSignalRow): string {
  const name = SIGNAL_LABELS[signal.kind] ?? signal.kind
  const window = signal.window ? ` · ${signal.window}` : ''
  return `${name} · n=${signal.sample_count}${window}`
}

/** 信号种类的中文名。**封闭表**：出现表外的种类直接显示原始值，
 * 而不是猜一个中文名——猜出来的名字会让人以为那是一种新结论。 */
export const SIGNAL_LABELS: Record<string, string> = {
  attention: '注意力（点赞）',
  conversation: '对话（回复 + 引用）',
  amplification: '扩散（转发）',
  share: '互动构成',
  insufficient_sample: '样本不足',
  missing_metric: '缺读数',
}

/**
 * 这块摘要**没有**回答什么。
 *
 * 不是装饰文案：工作单要求"数据不足就不许编"，那么"哪里不足"就必须摆出来。
 * 后端下发的 `limitations` 原样显示，前端不筛也不改写——被筛掉的那条
 * 通常恰好是最该被读到的那条。
 */
export function limitationLines(feedback: RunFeedbackSummary): string[] {
  return feedback.limitations
}

/**
 * 这一块的口径说明，恒为一句。
 *
 * 与 `limitations` 分开：`limitations` 是**这次数据的具体缺口**，
 * 这句是**这一层永远成立的口径**（点赞不等于质量）。两者混在一起，
 * 具体缺口就会被读者当成又一句免责声明略过去。
 */
export const FEEDBACK_DISCLAIMER =
  '这些是观察到的公开计数，不是质量评分；同时出现不等于谁导致了谁。'
