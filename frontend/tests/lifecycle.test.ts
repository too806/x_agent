import assert from 'node:assert/strict'
import { test } from 'node:test'

import { buildLifecycle } from '../src/utils/lifecycle.ts'
import type { LifecycleInput, LifecycleKey } from '../src/utils/lifecycle.ts'

/**
 * Run 生命周期时间线的行为测试。
 *
 * 这个纯函数里每一条分支都是"记录里有什么"与"能说它怎么样"之间的一个判断。
 * 判错了不会抛异常，只会安静地在界面上说一句假话——正是那种必须靠测试钉住的
 * 地方。用 Node 自带的 `node:test`（`npm run test:frontend`），不引入框架。
 *
 * 下面的用例基本都在验同一条规矩：
 *
 *   每个阶段的状态由它自己的证据决定，不由 Run 的整体状态反推。
 */

/** 一个"什么都没发生"的 Run。每个用例只覆盖它关心的那几个字段。 */
function input(overrides: Partial<LifecycleInput> = {}): LifecycleInput {
  return {
    run_status: 'running',
    created_at: '2026-09-01T00:00:00Z',
    started_at: null,
    research_count: 0,
    has_draft: false,
    draft_version: null,
    guardrail_passed: null,
    review_action: null,
    review_at: null,
    publish_status: null,
    publish_at: null,
    ...overrides,
  }
}

function step(steps: ReturnType<typeof buildLifecycle>, key: LifecycleKey) {
  const found = steps.find((item) => item.key === key)
  assert.ok(found, `生命周期里应该有 ${key} 这一段`)
  return found
}

/** 阶段顺序是固定的：界面按数组顺序从上往下画。 */
const ORDER: LifecycleKey[] = ['created', 'research', 'generate', 'guardrail', 'review', 'publish']

test('永远输出六段，且顺序固定', () => {
  const keys = buildLifecycle(input()).map((item) => item.key)
  assert.deepEqual(keys, ORDER)
})

test('每一段都有一句说明，不留空白', () => {
  // LifecycleStep.note 的约定是"永远有值"：留白在界面上看起来像渲染出错。
  const cases = [
    input(),
    input({ run_status: 'waiting_for_review' }),
    input({ run_status: 'published', publish_status: 'published' }),
    input({ run_status: 'failed' }),
    input({ run_status: 'rejected', review_action: 'reject' }),
  ]
  for (const value of cases) {
    for (const item of buildLifecycle(value)) {
      assert.equal(typeof item.note, 'string')
      assert.notEqual(item.note.trim(), '', `${value.run_status} 的 ${item.key} 没有说明`)
    }
  }
})

// ---------------------------------------------------------------------------
// 研究 / 生成：不拿整体状态反推素材与草稿
// ---------------------------------------------------------------------------

test('到了人工审核却没存素材：说"走过但没记录"，不说"没研究"也不说"研究好了"', () => {
  // 这是文件头举的那个真实例子。反推会显示"研究 ✓ 7 条"，而库里一条都没有。
  const steps = buildLifecycle(input({ run_status: 'waiting_for_review' }))
  assert.equal(step(steps, 'research').state, 'unknown')
  assert.equal(step(steps, 'generate').state, 'unknown')
})

test('存了素材就按素材说，与整体状态无关', () => {
  const steps = buildLifecycle(input({ run_status: 'running', research_count: 4 }))
  assert.equal(step(steps, 'research').state, 'done')
  assert.match(step(steps, 'research').note, /4/)
})

test('还在跑：有素材说明在写稿，没素材说明还没开始', () => {
  assert.equal(
    step(buildLifecycle(input({ run_status: 'running', research_count: 2 })), 'generate').state,
    'current',
  )
  assert.equal(
    step(buildLifecycle(input({ run_status: 'running' })), 'generate').state,
    'pending',
  )
})

test('整轮失败的 Run 不会因此被抹掉已有的证据', () => {
  // failed 在 PROGRESS 里排最靠前，正是为了让每一段退回看它自己的证据。
  const steps = buildLifecycle(
    input({
      run_status: 'failed',
      started_at: '2026-09-01T00:01:00Z',
      research_count: 3,
      has_draft: true,
      draft_version: 2,
    }),
  )
  assert.equal(step(steps, 'research').state, 'done')
  assert.equal(step(steps, 'generate').state, 'done')
  assert.match(step(steps, 'generate').note, /v2/)
})

// ---------------------------------------------------------------------------
// 规则检查：null 是"读不到"，不是"通过"
// ---------------------------------------------------------------------------

test('规则检查读不到时既不说通过也不说未通过', () => {
  const steps = buildLifecycle(input({ run_status: 'waiting_for_review', guardrail_passed: null }))
  const guardrail = step(steps, 'guardrail')
  assert.equal(guardrail.state, 'unknown')
  assert.match(guardrail.note, /不等于通过/)
})

test('还没走到那一步时规则检查是 pending，不是 unknown', () => {
  // 这两种在界面上的含义完全不同：pending 是"还没轮到"，unknown 是"轮到过但丢了"。
  const steps = buildLifecycle(input({ run_status: 'running', guardrail_passed: null }))
  assert.equal(step(steps, 'guardrail').state, 'pending')
})

test('规则检查通过 / 未通过各自成态', () => {
  assert.equal(
    step(buildLifecycle(input({ guardrail_passed: true })), 'guardrail').state,
    'ok',
  )
  assert.equal(
    step(buildLifecycle(input({ guardrail_passed: false })), 'guardrail').state,
    'failed',
  )
})

test('规则检查这一格不编造时间戳', () => {
  // checkpoint 里没有记这一步的时刻，所以两处都得是 null；用 created_at 之类
  // 凑一个近似值会让时间线看起来精确，而它并不精确。
  assert.equal(
    step(buildLifecycle(input({ guardrail_passed: true })), 'guardrail').at,
    null,
  )
})

// ---------------------------------------------------------------------------
// 审核
// ---------------------------------------------------------------------------

test('人工决定留了记录就按记录说', () => {
  const steps = buildLifecycle(
    input({
      run_status: 'approved',
      review_action: 'approve',
      review_at: '2026-09-01T02:00:00Z',
    }),
  )
  const review = step(steps, 'review')
  assert.equal(review.state, 'ok')
  assert.equal(review.at, '2026-09-01T02:00:00Z')
  assert.match(review.note, /批准/)
})

test('拒绝让审核这一段成 failed', () => {
  const steps = buildLifecycle(input({ run_status: 'rejected', review_action: 'reject' }))
  assert.equal(step(steps, 'review').state, 'failed')
})

test('正等人工决定时审核是 current', () => {
  const steps = buildLifecycle(input({ run_status: 'waiting_for_review' }))
  assert.equal(step(steps, 'review').state, 'current')
})

// ---------------------------------------------------------------------------
// 发布
// ---------------------------------------------------------------------------

test('被拒绝的内容：发布这一段是"不会发生"，而不是"还没开始"', () => {
  // 这个区别是整条时间线上最要紧的一处：skipped 告诉人"不用再等了"。
  const steps = buildLifecycle(input({ run_status: 'rejected', review_action: 'reject' }))
  const publish = step(steps, 'publish')
  assert.equal(publish.state, 'skipped')
  assert.match(publish.note, /不会发布/)
})

test('已通过审核但还没发布：发布这一段是"等着发"，不是 pending', () => {
  for (const runStatus of ['approved', 'edited']) {
    const steps = buildLifecycle(input({ run_status: runStatus }))
    assert.equal(step(steps, 'publish').state, 'current')
  }
})

test('结果未知既不显示成成功，也不显示成失败', () => {
  // 这两种误读在各方向上都是有害的：显示成功会让人以为帖子在（可能不在），
  // 显示失败会让人顺手重发（可能造出第二条）。
  const steps = buildLifecycle(
    input({ run_status: 'publish_unknown', publish_status: 'publish_unknown' }),
  )
  const publish = step(steps, 'publish')
  assert.equal(publish.state, 'unknown')
  assert.match(publish.note, /裁决/)
})

test('publishing 是"在途"，不是"在等人工"', () => {
  const steps = buildLifecycle(input({ run_status: 'publishing', publish_status: 'publishing' }))
  assert.equal(step(steps, 'publish').state, 'current')
})

test('发布失败时明确说重发是安全的', () => {
  // 与 publish_unknown 相对：这里结果确定，所以重发不会造出第二条。
  const steps = buildLifecycle(input({ run_status: 'failed', publish_status: 'failed' }))
  const publish = step(steps, 'publish')
  assert.equal(publish.state, 'failed')
  assert.match(publish.note, /重发是安全的/)
})

test('发布记录的状态优先于 Run 的状态', () => {
  // 两个字段可能合法地互相矛盾。发布状态才是权威，时间线按它画。
  const steps = buildLifecycle(
    input({ run_status: 'published', publish_status: 'failed', publish_at: '2026-09-01T03:00:00Z' }),
  )
  const publish = step(steps, 'publish')
  assert.equal(publish.state, 'failed')
  assert.equal(publish.at, '2026-09-01T03:00:00Z')
})

test('没有发布记录时不假装发布过', () => {
  const steps = buildLifecycle(input({ run_status: 'published' }))
  // 即便 Run 说自己 published，没有发布记录就只能是"还没有发布记录"这一档，
  // 绝不能落进上面那几个 assert 里的 ok。
  assert.notEqual(step(steps, 'publish').state, 'ok')
  assert.match(step(steps, 'publish').note, /还没有发布记录/)
})
