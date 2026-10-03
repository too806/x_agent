import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  CUSTOM_RANGE_SEED_DAYS,
  TIME_RANGES,
  daysAgoDateString,
  localDateString,
  rangeStartDate,
} from '../src/utils/dateRange.ts'

/**
 * 运行记录页时间筛选的行为测试。
 *
 * 这里守的是两件容易悄悄错掉的事：
 *
 * 1. **日历日的进位**。「最近 90 天」跨过月末、年末时必须由 Date 构造器自己算，
 *    手写减法的实现会在 3 月 1 日给出 12 月 1 日这种结果。
 * 2. **本地日而不是 UTC 日**。用 `toISOString().slice(0, 10)` 的实现在 UTC+8 的
 *    凌晨会整整错一天——而这一天之差在审计界面上就是"少一条记录"。
 *
 *   npm run test:frontend
 */

test('本地日历日按本地字段拼，不经过 UTC', () => {
  // 本地时间 2026-01-01 07:00。UTC 下这还是 2025-12-31，用 toISOString 就会错一天。
  assert.equal(localDateString(new Date(2026, 0, 1, 7, 0, 0)), '2026-01-01')
  // 本地时间 2026-01-01 23:30 → UTC 已经是 1 月 1 日 15:30，但本地仍是 1 月 1 日。
  assert.equal(localDateString(new Date(2026, 0, 1, 23, 30, 0)), '2026-01-01')
})

test('月和日补零到两位', () => {
  assert.equal(localDateString(new Date(2026, 2, 5)), '2026-03-05')
})

test('N 天前跨月由 Date 构造器进位', () => {
  // 3 月 1 日往前 30 天 = 1 月 30 日（2 月只有 28 天）。
  assert.equal(daysAgoDateString(30, new Date(2026, 2, 1)), '2026-01-30')
})

test('N 天前跨年进位', () => {
  assert.equal(daysAgoDateString(7, new Date(2026, 0, 3)), '2025-12-27')
})

test('N 天前跨闰日', () => {
  // 2028 是闰年，2 月有 29 天。3 月 1 日往前 2 天 = 2 月 28 日。
  assert.equal(daysAgoDateString(2, new Date(2028, 2, 1)), '2028-02-28')
})

test('零点偏移不会把结果挪到前一天', () => {
  // 凌晨 00:05 是本地日容易被 UTC 实现吞掉的时间点。
  assert.equal(daysAgoDateString(1, new Date(2026, 8, 25, 0, 5)), '2026-09-24')
})

test('「最近 N 天」含今天，所以起点是 N-1 天前', () => {
  const now = new Date(2026, 8, 25) // 2026-09-25
  // 最近 7 天 = 9/19..9/25 共 7 个自然日。
  assert.equal(rangeStartDate('7', now), '2026-09-19')
  assert.equal(rangeStartDate('30', now), '2026-08-27')
  assert.equal(rangeStartDate('90', now), '2026-06-28')
})

test('「全部时间」与「自定义区间」不产出左端', () => {
  const now = new Date(2026, 8, 25)
  assert.equal(rangeStartDate('', now), '')
  assert.equal(rangeStartDate('custom', now), '')
})

test('不认识的取值当作不设左端，而不是抛错', () => {
  // 下拉的取值将来会变（比如加一个「本季度」），旧的 URL 或旧的书签不能把页面打挂。
  assert.equal(rangeStartDate('quarter' as never, new Date(2026, 8, 25)), '')
})

test('每个预设都有一句人能读的标签，且默认项是「全部时间」', () => {
  assert.equal(TIME_RANGES[0]?.value, '')
  assert.equal(TIME_RANGES[0]?.label, '全部时间')
  for (const range of TIME_RANGES) {
    assert.ok(range.label.length > 0, `预设 ${range.value} 缺少标签`)
  }
  // 「自定义区间」必须还在，否则用户就失去了精确卡区间的能力。
  assert.ok(TIME_RANGES.some((range) => range.value === 'custom'))
})

test('进入自定义区间的兜底起点是 30 天前', () => {
  // 常量本身也是行为的一部分：它决定用户点开「自定义区间」第一眼看到什么。
  assert.equal(CUSTOM_RANGE_SEED_DAYS, 30)
  assert.equal(
    daysAgoDateString(CUSTOM_RANGE_SEED_DAYS, new Date(2026, 8, 25)),
    '2026-08-26',
  )
})
