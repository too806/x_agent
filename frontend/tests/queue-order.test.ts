import assert from 'node:assert/strict'
import { test } from 'node:test'

import { parseQueueOrder } from '../src/utils/queue.ts'

/**
 * 待审队列排序开关的解析测试。
 *
 * 与 `queue-selection.test.ts` 同一套路：`node:test` 直跑 `.ts`，不引入测试框架
 * （见那个文件头）。被测函数必须是纯函数、无依赖——这正是 `parseQueueOrder` 被
 * 从 `Review.tsx` 里抽出来的原因，否则这条规则就只能靠手点界面来验证。
 *
 * 这里守的核心是一句话：**URL 里的坏值降级，不报错**。`order` 是能被收藏、被手改、
 * 被旧版本前端写进历史记录的东西，解析它不能有失败态。
 *
 *   npm run test:frontend
 */

test('两个合法值原样返回', () => {
  assert.equal(parseQueueOrder('oldest'), 'oldest')
  assert.equal(parseQueueOrder('newest'), 'newest')
})

test('缺失（null / undefined）落回默认的 oldest', () => {
  // 没带 order 的 URL 是最常见的情况，必须等于后端默认值，否则第一次加载和
  // 点一次排序开关之后的队列顺序会对不上。
  assert.equal(parseQueueOrder(null), 'oldest')
  assert.equal(parseQueueOrder(undefined), 'oldest')
})

test('空串与纯空白落回 oldest', () => {
  // `?order=` 是手改 URL 最容易留下的形状。
  assert.equal(parseQueueOrder(''), 'oldest')
  assert.equal(parseQueueOrder('   '), 'oldest')
})

test('大小写不匹配落回 oldest（不做大小写折叠）', () => {
  // 刻意不做 `.toLowerCase()`：URL 是我们自己写的，只有精确的 'newest' 才算数。
  // 折叠大小写会凭空多出一种"等效写法"，而它并不存在于任何一处代码里。
  assert.equal(parseQueueOrder('NEWEST'), 'oldest')
  assert.equal(parseQueueOrder('Newest'), 'oldest')
})

test('别的排序说法落回 oldest，不抛错', () => {
  // 'asc' / 'desc' 是另一个维度的说法（这里没有用它），'created_at' 是后端字段名。
  // 它们都不该让审核页打不开。
  assert.equal(parseQueueOrder('asc'), 'oldest')
  assert.equal(parseQueueOrder('desc'), 'oldest')
  assert.equal(parseQueueOrder('created_at'), 'oldest')
})

test('拼错一个字符落回 oldest', () => {
  // 真实场景：手打 URL、旧书签、或者未来某次改名之后留下的旧链接。
  assert.equal(parseQueueOrder('newestt'), 'oldest')
  assert.equal(parseQueueOrder('newes'), 'oldest')
})

test('返回的永远是 QueueOrder 的两个取值之一', () => {
  // 这条锁的是函数的**值域**：调用方拿到的东西可以直接进请求参数与 URL。
  const inputs = ['oldest', 'newest', '', null, undefined, 'x', 'NEWEST', '2']
  for (const raw of inputs) {
    assert.ok(parseQueueOrder(raw) === 'oldest' || parseQueueOrder(raw) === 'newest')
  }
})
