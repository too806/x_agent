import assert from 'node:assert/strict'
import { test } from 'node:test'

import { EMPTY, researchItemTimes } from '../src/utils/format.ts'

/**
 * 研究素材的三个时间：发布 / 抓取 / 入库。
 *
 * 这条约定没法靠"看一眼界面"守住，因为它坏掉的时候界面**看起来完全正常**：
 * 三个时间被合并成一个，或者缺失的那个被另一个顶上，屏幕上仍然是一行合理的时间
 * 字符串。它只在一种情况下暴露——一条三天前发布、一小时前才抓回来的帖子，
 * 被人当成"刚刚发生的事"。
 *
 * 所以这里逐个字段地锁：三个值都在时必须三个都在，缺一个时那一个必须是 `—`，
 * **不能是另外两个的值**。
 *
 *   npm run test:frontend
 */

/** 三个时间刻意取成互不相同、且一眼可辨的日期。 */
const PUBLISHED = '2026-09-26T08:00:00+00:00'
const OBSERVED = '2026-09-29T11:40:00+00:00'
const CREATED = '2026-09-29T11:40:05+00:00'

/** 把 "发布 X · 抓取 Y · 入库 Z" 拆成 `{ 发布: X, 抓取: Y, 入库: Z }`。 */
function parse(line: string): Record<string, string> {
  const parts: Record<string, string> = {}
  for (const piece of line.split(' · ')) {
    const space = piece.indexOf(' ')
    parts[piece.slice(0, space)] = piece.slice(space + 1)
  }
  return parts
}

test('三个时间各显示各的', () => {
  const parts = parse(
    researchItemTimes({ published_at: PUBLISHED, observed_at: OBSERVED, created_at: CREATED }),
  )
  assert.deepEqual(Object.keys(parts).sort(), ['入库', '发布', '抓取'])
  assert.notEqual(parts['发布'], parts['抓取'])
  assert.notEqual(parts['抓取'], parts['入库'])
})

test('缺 observed_at 时显示 —，不拿 created_at 顶上', () => {
  // Phase 5B.1 之前的行就是这样：那一列当时还不存在。
  const parts = parse(
    researchItemTimes({ published_at: PUBLISHED, observed_at: null, created_at: CREATED }),
  )
  assert.equal(parts['抓取'], EMPTY)
  // 关键的一条：入库时间**只出现在它自己那一格**。
  assert.notEqual(parts['抓取'], parts['入库'])
  assert.ok(!parts['抓取'].includes('11:40:05'))
})

test('缺 published_at 时同样显示 —，且不影响另外两个', () => {
  const parts = parse(
    researchItemTimes({ published_at: null, observed_at: OBSERVED, created_at: CREATED }),
  )
  assert.equal(parts['发布'], EMPTY)
  assert.notEqual(parts['抓取'], EMPTY)
  assert.notEqual(parts['入库'], EMPTY)
})

test('三个都缺时不报错，整行都是 —', () => {
  // 不抛异常：一条什么都不全的记录仍然要能渲染出来。
  const parts = parse(researchItemTimes({ published_at: null, observed_at: null, created_at: null }))
  assert.deepEqual(Object.values(parts), [EMPTY, EMPTY, EMPTY])
})
