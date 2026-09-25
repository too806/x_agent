import assert from 'node:assert/strict'
import { test } from 'node:test'

import { nextSelection } from '../src/utils/queue.ts'

/**
 * 审核工作台「决定之后选哪一条」的行为测试。
 *
 * 用 Node 自带的 `node:test` 跑，**没有引入任何测试框架**（这个前端没有测试体系，
 * 为了一个边界情况装一整套 runner 不值得）。Node 24 能直接剥离类型跑 `.ts`，
 * 所以 `node --test` 就够了，不需要编译步骤。
 *
 * 被测的 `src/utils/queue.ts` 因此刻意写成无依赖的纯函数。
 *
 *   npm run test:frontend
 */

const A = { run_id: 'run-a', topic: 'A' }
const B = { run_id: 'run-b', topic: 'B' }

test('队列只有一条时，决定完保留这一条，不清空选中项', () => {
  // 这就是那个 P1：单条队列决定了唯一一条，没有"下一条"。
  // 清空选中项会让右侧详情消失，人看不到自己刚保存的编辑稿。
  assert.equal(nextSelection([A], A.run_id), A.run_id)
})

test('队列刷新成空之后，仍然保留刚决定的那一条', () => {
  // 真实时序：提交成功后页面先按旧队列算下一条，随后 reload 才把这条移出队列。
  // 两次计算都必须保留它，否则详情会在 reload 回来的那一刻被清掉。
  assert.equal(nextSelection([A], A.run_id), A.run_id)
  assert.equal(nextSelection([], A.run_id), A.run_id)
})

test('还有下一条时，跳到下一条（连续决策不受影响）', () => {
  assert.equal(nextSelection([A, B], A.run_id), B.run_id)
})

test('选中的是最后一条且没有下一条时，保留它', () => {
  assert.equal(nextSelection([A, B], B.run_id), B.run_id)
})

test('选中项不在当前页时不动它', () => {
  // 翻页之后选中项不在这一页里：既没有"下一条"，也不该换成别的 Run。
  assert.equal(nextSelection([A, B], 'run-elsewhere'), 'run-elsewhere')
})

test('没有选中项时保持 null', () => {
  assert.equal(nextSelection([A, B], null), null)
  assert.equal(nextSelection([], null), null)
})
