import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  FEEDBACK_DISCLAIMER,
  NO_DATA,
  feedbackHeadline,
  hoursSinceLabel,
  limitationLines,
  metricLabel,
  observationLine,
  signalHeadline,
} from '../src/utils/feedback.ts'
import type { FeedbackSignalRow, RunFeedbackSummary } from '../src/types/api.ts'

/**
 * 反馈块的措辞（Phase 5B.4）。
 *
 * 这个文件里几乎所有断言都是**否定式**的，因为这一层的错误全部是安静的：
 *
 *   - `metricLabel(null)` 返回 `'0'` —— 界面上一张从没采过的帖子看起来像零互动。
 *     这一行是整个 Phase 5B.4 前端最容易被"顺手优化"掉的一行
 *     （`value ?? 0` 更短、更顺手、不会让任何测试库报错），所以它必须被钉住。
 *   - 不同时刻的观察被加起来 —— 造出一个不曾存在的数。
 *   - 观察被写成评价 —— "点赞最多"被读成"最好"。
 *
 * 与 `viewpoint.test.ts` 同一套纪律：纯函数、无 DOM、`node --test` 直接跑。
 *
 *   npm run test:frontend
 */

/** 一条信号，只填这条测试关心的键。 */
function signal(overrides: Partial<FeedbackSignalRow> = {}): FeedbackSignalRow {
  return {
    kind: 'attention',
    statement: '最近 5 次观察里，点赞数的中位数是 12。',
    metric: 'likes',
    value: 12,
    comparison_group: null,
    sample_count: 5,
    confidence: null,
    evidence: ['snap-1', 'snap-2'],
    window: '6-24h',
    ...overrides,
  }
}

/** 一份"读到了"的反馈上下文。 */
function feedback(overrides: Partial<RunFeedbackSummary> = {}): RunFeedbackSummary {
  return {
    available: true,
    reason: null,
    sample_count: 12,
    post_count: 2,
    snapshot_count: 12,
    latest_observed_at: '2026-09-28T03:00:00+00:00',
    signals: [signal()],
    limitations: ['没有曝光数据，算不出互动率。'],
    source_snapshot_ids: ['snap-1', 'snap-2'],
    ...overrides,
  }
}

// ---------------------------------------------------------------------------
// 未知计数：不得变成 0
// ---------------------------------------------------------------------------

test('未知计数显示成「暂无数据」，绝不显示 0', () => {
  assert.equal(metricLabel(null), NO_DATA)
  assert.equal(metricLabel(undefined), NO_DATA)
  // 这一条是**反例**：它必须与上面两句不同，否则整件事就没有意义。
  assert.equal(metricLabel(0), '0')
  assert.notEqual(NO_DATA, '0')
})

test('已有计数原样显示，不千分位、不加单位', () => {
  // 千分位（`1,234`）会让一个数字看起来像一段文案里的数字；
  // 加单位（`12 赞`）会把计数与结论混在一格。这里只给数字本身。
  assert.equal(metricLabel(1234), '1234')
  assert.equal(metricLabel(0), '0')
})

test('发布后时长未知时也不给 0', () => {
  assert.equal(hoursSinceLabel(null), NO_DATA)
  assert.equal(hoursSinceLabel(undefined), NO_DATA)
  assert.equal(hoursSinceLabel(3.5), '发布后 3.5 小时')
  // 0 小时是一个合法读数（刚发出去就采到了），不能与"不知道"混同。
  assert.equal(hoursSinceLabel(0), '发布后 0 小时')
})

// ---------------------------------------------------------------------------
// 观察次数：说"看过几次"，不说"表现如何"
// ---------------------------------------------------------------------------

test('观察次数那一行不含任何计数', () => {
  const line = observationLine(5, '2026-09-28T03:00:00+00:00')
  assert.match(line, /5 次观察/)
  assert.match(line, /2026-09-28/)
  // 如果哪天有人把点赞数塞进这一行，读的人就会以为它是结论。
  assert.doesNotMatch(line, /赞|like|互动/i)
})

test('一次都没采过时说「还没有观察记录」，不写成「0 次观察」', () => {
  assert.equal(observationLine(0, null), '还没有观察记录')
  assert.doesNotMatch(observationLine(0, null), /0 次/)
})

// ---------------------------------------------------------------------------
// 这次运行读到的反馈：三种"没有"必须长得不一样
// ---------------------------------------------------------------------------

test('没读到反馈时原样显示后端给的原因', () => {
  const absent = feedback({
    available: false,
    reason: 'This Run did not read any feedback context.',
    sample_count: 0,
    post_count: 0,
    snapshot_count: 0,
    latest_observed_at: null,
    signals: [],
  })
  assert.match(feedbackHeadline(absent), /did not read/)
})

test('读到了但一条信号都没有时，仍然说清读到了几条观察', () => {
  const empty = feedback({ signals: [], sample_count: 3, post_count: 1 })
  const line = feedbackHeadline(empty)
  assert.match(line, /3 条观察/)
  assert.match(line, /没有产出/)
  // 不能因为没信号就退化成与"没读过"同一句话
  assert.doesNotMatch(line, /没有读到/)
})

test('正常情形报样本量与帖子数', () => {
  assert.equal(feedbackHeadline(feedback()), '这次运行读到 12 条观察、2 条帖子。')
})

test('样本量为 0 时不编出一个帖子数', () => {
  const line = feedbackHeadline(feedback({ sample_count: 0, post_count: 0 }))
  assert.match(line, /0 条观察/)
  assert.doesNotMatch(line, /条帖子/)
})

// ---------------------------------------------------------------------------
// 信号：样本量必须跟着结论出现
// ---------------------------------------------------------------------------

test('每条信号的标题都带样本量', () => {
  assert.match(signalHeadline(signal()), /n=5/)
  assert.match(signalHeadline(signal()), /注意力/)
  assert.match(signalHeadline(signal()), /6-24h/)
})

test('未知的信号种类显示原始值，不猜一个中文名', () => {
  // 猜出来的名字会让人以为那是一种已经被理解的新结论。
  const line = signalHeadline(signal({ kind: 'brand_new_kind' }))
  assert.match(line, /brand_new_kind/)
})

test('没有窗口时不硬塞一个空的窗口段', () => {
  // `·` 是种类与 n 之间的分隔符，恒在；没有窗口时不该多出**第二个**，
  // 更不该留下一个 `窗口：` 后面空着。
  const line = signalHeadline(signal({ window: null }))
  assert.equal(line, '注意力（点赞） · n=5')
  assert.equal(line.split('·').length, 2)
})

// ---------------------------------------------------------------------------
// 局限说明：一条都不许筛
// ---------------------------------------------------------------------------

test('局限说明原样透传，不筛不改写', () => {
  const notes = ['没有曝光数据，算不出互动率。', '样本不足 5 条。']
  assert.deepEqual(limitationLines(feedback({ limitations: notes })), notes)
  assert.deepEqual(limitationLines(feedback({ limitations: [] })), [])
})

test('口径说明是恒定的那一句，且不含评价词', () => {
  assert.match(FEEDBACK_DISCLAIMER, /不是质量评分/)
  assert.match(FEEDBACK_DISCLAIMER, /不等于谁导致了谁/)
  // 一旦出现"表现好/差"这类词，这一层就变回了一个评分器。
  assert.doesNotMatch(FEEDBACK_DISCLAIMER, /表现好|表现差|优秀|出色/)
})
