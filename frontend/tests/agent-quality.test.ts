import assert from 'node:assert/strict'
import { test } from 'node:test'

import { criticOutcomeSummary } from '../src/utils/format.ts'
import type { AgentQualityMetrics } from '../src/types/api.ts'

/**
 * Agent 运行质量那一行的文案。
 *
 * 锁一条约定：**三种结论永远都显示，包括 0**。"这次一条都没否决"和"这次压根
 * 没评过"是两件完全不同的事，把计数为 0 的那项省掉就分不出来了。
 *
 *   npm run test:frontend
 */

function quality(overrides: Partial<AgentQualityMetrics> = {}): AgentQualityMetrics {
  return {
    total_steps: 8,
    research_calls: 2,
    revision_count: 1,
    candidate_count: 3,
    critic_accept_count: 0,
    critic_revise_count: 0,
    critic_reject_count: 0,
    budget_exhausted: false,
    finish_reason: '评审通过，交给人工',
    ...overrides,
  }
}

test('三种结论都显示，计数为 0 的也不省略', () => {
  const summary = criticOutcomeSummary(
    quality({ critic_accept_count: 3, critic_revise_count: 3, critic_reject_count: 1 }),
  )
  assert.match(summary, /评了 7 条/)
  assert.match(summary, /通过 3/)
  assert.match(summary, /要求改稿 3/)
  assert.match(summary, /否决 1/)
})

test('全是 0 的某一项仍然出现在文案里', () => {
  const summary = criticOutcomeSummary(
    quality({ critic_accept_count: 3, critic_revise_count: 0, critic_reject_count: 0 }),
  )
  assert.match(summary, /否决 0/)
  assert.match(summary, /要求改稿 0/)
})

test('一次都没评过时说的是"没产生结论"，不是"评了 0 条"', () => {
  const summary = criticOutcomeSummary(quality())
  assert.equal(summary, '这次运行没有产生评审结论')
  assert.doesNotMatch(summary, /评了 0 条/)
})
