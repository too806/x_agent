import assert from 'node:assert/strict'
import { test } from 'node:test'

import { NO_DATA as LEARNING_NO_DATA, medianLabel } from '../src/utils/learning.ts'
import {
  NO_DATA,
  PROPOSALS_DISCLAIMER,
  PROPOSAL_STATUS_LABELS,
  REVIEW_ACTION_LABELS,
  TARGET_LABELS,
  baselineIsKnown,
  changeLine,
  evidenceLine,
  isReviewable,
  isStale,
  proposalHeadline,
  proposalLimitations,
  proposalStatusLabel,
  proposalsHeadline,
  reviewOutcomeLine,
  targetLabel,
  validationLine,
  versionLine,
} from '../src/utils/proposals.ts'
import type { LearningProposalRow, ProposalsOverview } from '../src/types/api.ts'

/**
 * 学习提案块的措辞（Phase 5D）。
 *
 * 这一块比 5C 那块多出三种**安静的**错法，每一种都在这里被钉住：
 *
 *   1. **建议被读成动作** —— "可以尝试把开头方式取 question"被读成"系统要改了"。
 *   2. **批准被读成已应用** —— `approved` 是"人做过的决定"，不是"配置已经变了"。
 *      整个阶段最容易说出口的一句谎就是在这里。
 *   3. **当前取值被当成事实** —— 没有基准配方时，`current_value` 只是"这次比较里
 *      较低的那一侧"。后端同时把两个版本号设为 `null` 就是为了让界面能分辨，
 *      所以 `changeLine` 在那种情况下必须**不显示**当前取值。
 *
 * 与 `learning.test.ts` 同一套纪律：纯函数、无 DOM、`node --test` 直接跑。
 *
 *   npm run test:frontend
 */

const FULL_VERSIONS = {
  current_recipe_version: 'sr1-aaaaaaaaaaaa',
  proposed_recipe_version: 'sr1-bbbbbbbbbbbb',
} as const

function proposal(overrides: Partial<LearningProposalRow> = {}): LearningProposalRow {
  return {
    id: 'proposal:hook_type@24-72h',
    target: 'style_recipe.hook_type',
    target_label: '开头方式',
    current_value: 'statement',
    proposed_value: 'question',
    rationale: '在本样本中观察到「question」一侧更高。',
    evidence_ids: ['a', 'b', 'c'],
    sample_count: 10,
    observation_window: '24-72h',
    comparison_group: 'hook_type: question vs statement @ 24-72h · attention',
    confidence: null,
    limitations: ['这是一次观察性比较，没有对照组。'],
    status: 'proposed',
    ...FULL_VERSIONS,
    outlier_count: 0,
    automatic_application: false,
    validation: { valid: true, reasons: [], passed_checks: ['has_evidence'] },
    reviewable: true,
    reviewed_at: null,
    review_note: null,
    stale_reason: null,
    ...overrides,
  }
}

function overview(overrides: Partial<ProposalsOverview> = {}): ProposalsOverview {
  return {
    available: true,
    reason: '',
    observation_window: '24-72h',
    learning_ready: true,
    proposal_count: 1,
    reviewable_count: 1,
    decided_count: 0,
    proposals: [proposal()],
    limitations: [],
    ...overrides,
  }
}

// ---------------------------------------------------------------------------
// 当前取值是不是事实
// ---------------------------------------------------------------------------

test('没有基准配方时不显示当前取值——那只是一个观察', () => {
  const row = proposal({
    current_recipe_version: null,
    proposed_recipe_version: null,
  })
  assert.equal(baselineIsKnown(row), false)
  assert.equal(changeLine(row), 'question（当前取值未知）')
  // 关键的否定：不能把 current_value 照出来充数。
  assert.ok(!changeLine(row).includes('statement'), changeLine(row))
  assert.equal(versionLine(row), NO_DATA)
})

test('有基准配方时才显示成对的两个版本号', () => {
  const row = proposal()
  assert.equal(baselineIsKnown(row), true)
  assert.equal(changeLine(row), 'statement → question')
  assert.equal(versionLine(row), 'sr1-aaaaaaaaaaaa → sr1-bbbbbbbbbbbb')
})

test('版本号只有一半时不算已知', () => {
  // 只有"当前版本"没有"建议版本"，读的人会以为建议版本恰好等于当前版本。
  const row = proposal({ proposed_recipe_version: null })
  assert.equal(baselineIsKnown(row), false)
  assert.equal(versionLine(row), NO_DATA)
  assert.ok(!changeLine(row).includes('statement'), changeLine(row))
})

test('未知读数仍然走同一个 NO_DATA，不写成 0', () => {
  assert.equal(NO_DATA, LEARNING_NO_DATA)
  assert.equal(medianLabel(null), NO_DATA)
})

// ---------------------------------------------------------------------------
// 建议 ≠ 动作，批准 ≠ 已应用
// ---------------------------------------------------------------------------

/**
 * 去掉**否定式**的措辞，只留下断言部分。
 *
 * 为什么需要它：口径说明里有一句 `不是"应该做什么"的结论`——这句**否定**正是
 * 要的措辞，而"应该"两个字确实在里面。直接 `includes('应该')` 会把它判成违规，
 * 于是唯一能让这条断言变绿的办法是把那句免责声明删掉，方向完全反了。
 *
 * 子串匹配分不清一句断言和它的否定。这里把已知的否定形式剥掉，剩下的才当作
 * 断言来检查——**只剥这一句**，不去猜别的否定句式（猜错了会静默放过真的违规）。
 */
function assertedPart(line: string): string {
  return line.replace(/不是["“”]?应该做什么["“”]?的/g, '')
}

test('措辞里不出现"应该 / 推荐 / 已生效 / 已应用"', () => {
  const lines = [
    PROPOSALS_DISCLAIMER,
    proposalsHeadline(overview()),
    proposalHeadline(proposal()),
    reviewOutcomeLine(proposal({ status: 'approved' })),
    reviewOutcomeLine(proposal({ status: 'rejected' })),
    ...Object.values(REVIEW_ACTION_LABELS),
    ...Object.values(PROPOSAL_STATUS_LABELS),
  ]
  for (const line of lines) {
    for (const forbidden of ['推荐', '已生效', '已应用', '一定要']) {
      assert.ok(!line.includes(forbidden), `${forbidden} 出现在：${line}`)
    }
    assert.ok(
      !assertedPart(line).includes('应该'),
      `「应该」出现在断言里（不只是被否定）：${line}`,
    )
  }
})

test('口径说明必须把"应该做什么"明确否定掉', () => {
  // 上一条只保证"没有断言该怎么做"。这一条保证反过来的那半也在：
  // 那句否定必须真的写出来，否则"关于该怎么做什么都不说"也会让上一条变绿。
  const asserted = assertedPart(PROPOSALS_DISCLAIMER)
  assert.ok(PROPOSALS_DISCLAIMER.includes('不是"应该做什么"'), PROPOSALS_DISCLAIMER)
  assert.ok(!asserted.includes('应该做什么'), asserted)
  assert.ok(!asserted.includes('应该'), asserted)
})

test('被批准的提案状态里写明"未应用"', () => {
  assert.equal(proposalStatusLabel('approved'), '已批准（未应用）')
  const line = reviewOutcomeLine(proposal({ status: 'approved' }))
  assert.ok(line.includes('没有'), line)
  assert.ok(line.includes('改变任何设置'), line)
})

test('批准的动作文案本身就说明了"只记录，不应用"', () => {
  assert.ok(REVIEW_ACTION_LABELS.approve.includes('不应用'))
})

test('口径说明同时否定因果与自动应用', () => {
  assert.ok(PROPOSALS_DISCLAIMER.includes('不构成因果'))
  assert.ok(PROPOSALS_DISCLAIMER.includes('不会改变 Agent 的任何设置'))
  assert.ok(PROPOSALS_DISCLAIMER.includes('不执行建议'))
})

test('待裁决的提案不宣称任何后果', () => {
  const line = reviewOutcomeLine(proposal({ status: 'proposed' }))
  assert.ok(!line.includes('批准'), line)
})

// ---------------------------------------------------------------------------
// 零条建议 ≠ 没问题
// ---------------------------------------------------------------------------

test('闸门关着时零条建议是一个结论，不是"没问题"', () => {
  const headline = proposalsHeadline(
    overview({ learning_ready: false, proposal_count: 0, reviewable_count: 0, proposals: [] }),
  )
  assert.ok(headline.includes('不足以提出建议'), headline)
  assert.ok(headline.includes('不是一次失败'), headline)
  // 最危险的那句话：把"数据不够"说成"看过了，没问题"。
  for (const wrong of ['没有问题', '没有发现可以改进', '一切正常']) {
    assert.ok(!headline.includes(wrong), headline)
  }
})

test('闸门开着但没有方向时说的是"没有值得尝试的方向"', () => {
  const headline = proposalsHeadline(overview({ proposal_count: 0, reviewable_count: 0, proposals: [] }))
  assert.ok(headline.includes('没有值得尝试的方向'), headline)
  assert.ok(headline.includes('24-72h'), '没有窗口的结论无法被判断为样本太小')
})

test('没有数据时用后端给的原因，不自己编一句', () => {
  const headline = proposalsHeadline(
    overview({ available: false, reason: '还没有任何已发布内容可供观察。' }),
  )
  assert.equal(headline, '还没有任何已发布内容可供观察。')
})

test('标题永远带 n 与窗口', () => {
  const headline = proposalsHeadline(overview())
  assert.ok(headline.includes('24-72h'), headline)
  assert.ok(headline.includes('1'), headline)
})

test('没有裁决明细时不说"待裁决 0 条"', () => {
  // Run Detail 那一行来自 `GET /api/admin/learning`，它只有条数、没有裁决明细。
  // 拿 0 顶上会说出"待裁决 0 条"——而真实情况是"不知道有几条"，两件事。
  const headline = proposalsHeadline({
    available: true,
    reason: '',
    observation_window: '24-72h',
    learning_ready: true,
    proposal_count: 3,
  })
  assert.ok(headline.includes('3 条'), headline)
  assert.ok(!headline.includes('待裁决'), headline)
  assert.ok(!headline.includes('0'), headline)
  // 有明细时照常给出，这条分支没有被上面那条改掉。
  assert.ok(proposalsHeadline(overview()).includes('待裁决 1 条'))
})

// ---------------------------------------------------------------------------
// 依据与边界
// ---------------------------------------------------------------------------

test('依据那一行同时给出证据条数、帖子数与窗口', () => {
  const line = evidenceLine(proposal())
  assert.ok(line.includes('3 条证据'), line)
  assert.ok(line.includes('n=10'), line)
  assert.ok(line.includes('24-72h'), line)
})

test('n 是帖子并集，不在这里再乘一遍', () => {
  // 三条证据、n=10 —— 写 n=30 就是把同一句话数了三遍。
  const line = evidenceLine(proposal())
  assert.ok(!line.includes('n=30'), line)
})

test('边界原样显示，不被筛掉', () => {
  const row = proposal({
    limitations: ['当前取值未知，它不代表生产配置的取值。', '只有一条指标支持这个方向。'],
  })
  assert.deepEqual(proposalLimitations(row), row.limitations)
})

test('判据不通过的提案会显式说出来', () => {
  const row = proposal({
    validation: { valid: false, reasons: ['理由里出现因果措辞'], passed_checks: [] },
  })
  const line = validationLine(row)
  assert.ok(line && line.includes('因果'), line)
  assert.ok(line.includes('不应被采纳'), line)

  assert.equal(validationLine(proposal()), null, '合法的提案不该多一行噪声')
})

// ---------------------------------------------------------------------------
// 状态、过期与可裁决
// ---------------------------------------------------------------------------

test('过期只信后端下发的 stale_reason，不在前端自己比版本号', () => {
  assert.equal(isStale(proposal()), false)
  assert.equal(isStale(proposal({ stale_reason: '配方版本已更新' })), true)
  // 版本号相同也可能过期（比如配方算不出来），前端比不出来——所以只信后端。
  assert.equal(isStale(proposal({ stale_reason: '已经算不出来这条提案' })), true)
})

test('裁决过的不再可裁决，但过期的已批准记录仍可读', () => {
  assert.equal(isReviewable(proposal()), true)
  assert.equal(isReviewable(proposal({ status: 'approved', reviewable: false })), false)
  assert.equal(isReviewable(proposal({ status: 'rejected', reviewable: false })), false)
  // 一条只是后端把 reviewable 关掉的提案，前端也不放行。
  assert.equal(isReviewable(proposal({ reviewable: false })), false)
})

test('状态表是封闭的，表外的状态原样显示而不是猜一个中文名', () => {
  assert.equal(proposalStatusLabel('proposed'), '待裁决')
  assert.equal(proposalStatusLabel('weird_new_state'), 'weird_new_state')
})

test('目标名优先用后端下发的 target_label', () => {
  assert.equal(targetLabel(proposal()), '开头方式')
  // 后端没给时退回本地表，再退回原始 key —— 不显示空白。
  assert.equal(targetLabel(proposal({ target_label: '' })), '开头方式')
  assert.equal(targetLabel(proposal({ target_label: '', target: 'unknown.target' })), 'unknown.target')
  assert.equal(TARGET_LABELS['viewpoint.stance'], '判断强度')
})

test('紧凑标题带目标、改成什么与状态', () => {
  const line = proposalHeadline(proposal())
  assert.ok(line.includes('开头方式'), line)
  assert.ok(line.includes('statement → question'), line)
  assert.ok(line.includes('待裁决'), line)
})
