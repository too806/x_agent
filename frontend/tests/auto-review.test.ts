import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  AUTO_PUBLISH_GATE_DISCLAIMER,
  agreementHeadline,
  autoReviewHeadline,
  autoReviewHint,
  criticScoreLabel,
  falseApproveLine,
  falseRejectLine,
  gateSummary,
  modeLabel,
  originLabel,
  rateLabel,
  switchSentence,
  verdictLabel,
} from '../src/utils/autoReview.ts'
import type {
  AutoPublishAgreement,
  AutoPublishGate,
  AutoReviewSummary,
} from '../src/types/api.ts'

/**
 * 自动发布那一层的措辞（Phase auto-publish）。
 *
 * 这里的风险全在**一个数被读成另一个意思**上，而错了不会崩、只会让人按错按钮：
 *
 *   - `null` 显示成 `0%`：把"还没有数据"说成"机器完全不可信"；
 *   - `null` 分数显示成 `0.0`：把"没评价过"说成"评过了，很差"；
 *   - `auto_approvals = 0` 时的"零误放行"：分母是 0，这个 0 什么也没证明；
 *   - 机器的"不自动放行"写成"拒绝"：人会以为这条已经结束，不再去看它。
 *
 * 所以断言以**否定式**为主：不含 `%`、不含 `0`、不含"已拒绝"。
 *
 *   npm run test:frontend
 */

function agreement(overrides: Partial<AutoPublishAgreement> = {}): AutoPublishAgreement {
  return {
    decided_pairs: 0,
    agreed: 0,
    false_approve: 0,
    soft_mismatch: 0,
    false_reject: 0,
    auto_approvals: 0,
    agreement_rate: null,
    soft_mismatch_rate: null,
    ...overrides,
  }
}

function gate(overrides: Partial<AutoPublishGate> = {}): AutoPublishGate {
  return { ready: false, criteria: [], manual_steps: [], ...overrides }
}

function auto(overrides: Partial<AutoReviewSummary> = {}): AutoReviewSummary {
  return {
    mode: 'shadow',
    verdict: 'reject',
    reasons: ['重复了 14 天内的一条选题。'],
    critic_score: 8.4,
    acted: false,
    ...overrides,
  }
}

// ---------------------------------------------------------------- null ≠ 0

test('一致率没有数据时是破折号，绝不是一个百分数', () => {
  const label = rateLabel(null)
  assert.equal(label, '—')
  assert.ok(!label.includes('%'), 'null 不得渲染成任何百分比')
  assert.ok(!/\d/.test(label), 'null 不得渲染出任何数字')
})

test('一致率真的是 0 时才显示 0.0% —— 两个零必须分得开', () => {
  assert.equal(rateLabel(0), '0.0%')
  assert.notEqual(rateLabel(0), rateLabel(null))
})

test('一致率保留一位小数，不四舍五入成整数', () => {
  assert.equal(rateLabel(0.9234), '92.3%')
})

test('没有已决配对时说"还没有数据"，而不是"一致率为 0"', () => {
  const headline = agreementHeadline(agreement())
  assert.match(headline, /还没有已决配对/)
  assert.ok(!headline.includes('%'), '没有配对时不该出现任何百分比')
  assert.ok(!headline.includes('0.0'), '没有配对时不该出现 0.0')
})

test('有配对时把分子分母一起说出来', () => {
  const headline = agreementHeadline(
    agreement({ decided_pairs: 20, agreed: 19, agreement_rate: 0.95 }),
  )
  assert.match(headline, /95\.0%/)
  assert.match(headline, /19\/20/)
})

test('Critic 分数为 null 是"未评价"，不是 0.0 分', () => {
  assert.equal(criticScoreLabel(null), null)
  assert.equal(criticScoreLabel(undefined), null)
  assert.equal(criticScoreLabel(0), '0.0', '真的是 0 分要显示成 0.0 —— 两者不可混')
  assert.equal(criticScoreLabel(8.44), '8.4')
})

// ------------------------------------------------- 零误放行不能当成绩

test('自动侧一次都没放行过时，零误放行必须说明它什么也没证明', () => {
  const line = falseApproveLine(agreement({ auto_approvals: 0, false_approve: 0 }))
  assert.match(line, /一次都没有放行过/)
  // 分母是"已决配对里机器判通过的次数"，不是"机器一共放行了多少次"。
  // 不限定口径，这句话在正式模式下就是假的：它可能放过好几条、只是没人审。
  assert.match(line, /已决配对/, '0 必须限定在"已决配对"这个口径里说')
  assert.ok(!/机器一共|总共放行/.test(line), '不得说成"机器一共放行了 0 次"')
  // 关键：不能出现"没有误放行"这种像成绩的结论
  assert.ok(!/没有一次被人拒绝/.test(line), '分母为 0 时不得说出"零误放行"')
})

test('真的放行过且没被拒，才是好消息', () => {
  const line = falseApproveLine(agreement({ auto_approvals: 12, false_approve: 0 }))
  assert.match(line, /放行过 12 次/)
  assert.match(line, /没有一次被人拒绝/)
})

test('出现过误放行时是硬阻断，且指向"查判据"而不是"继续攒数据"', () => {
  const line = falseApproveLine(agreement({ auto_approvals: 5, false_approve: 2 }))
  assert.match(line, /最危险的错/)
  assert.match(line, /哪条判据漏了/)
})

test('误拦被说成"不是错误"，因为代价只是一条队列记录', () => {
  const line = falseRejectLine(agreement({ false_reject: 3 }))
  assert.match(line, /不是错误/)
  assert.ok(!line.includes('最危险'), '误拦不得与误放行共用同一等级的措辞')
})

test('没有误拦时如实说没有，不要借机夸奖', () => {
  assert.equal(falseRejectLine(agreement()), '没有出现"机器拦下、人本来会发"的情况。')
})

// ---------------------------------------------------- 机器的不通过不是拒绝

test('影子模式下无论判什么都强调"一条都不会发"', () => {
  const approved = autoReviewHint(auto({ mode: 'shadow', verdict: 'approve' }))
  const rejected = autoReviewHint(auto({ mode: 'shadow', verdict: 'reject' }))
  for (const hint of [approved, rejected]) {
    assert.match(hint, /影子模式/)
    assert.match(hint, /一条都不会发/)
  }
})

test('正式模式下被判不通过要说明人仍然可以批准', () => {
  const hint = autoReviewHint(auto({ mode: 'live', verdict: 'reject' }))
  assert.match(hint, /机器不通过不等于拒绝/)
  assert.match(hint, /仍然可以批准/)
  assert.ok(!hint.includes('已被拒绝'), '不得把机器的结论说成人的拒绝')
})

test('正式模式下被判通过却还在队列里，必须提示下一步没走完', () => {
  const hint = autoReviewHint(auto({ mode: 'live', verdict: 'approve' }))
  assert.match(hint, /下一步没有走完/)
})

test('标题把模式一并说出，因为同一个结论在两种模式下含义不同', () => {
  assert.equal(autoReviewHeadline(auto()), '自动审阅：不通过（影子模式）')
  assert.equal(
    autoReviewHeadline(auto({ mode: 'live', verdict: 'approve' })),
    '自动审阅：通过（正式模式）',
  )
})

// ------------------------------------------------------------ 门槛不是放行

test('门槛达标只覆盖机器能算的那几项，免责声明必须跟着结论一起给', () => {
  assert.match(AUTO_PUBLISH_GATE_DISCLAIMER, /机器算不出来/)
  assert.ok(!/可以(转正式|发布)/.test(AUTO_PUBLISH_GATE_DISCLAIMER))
})

test('门槛摘要给的是分子分母，不是一句"达标"', () => {
  const summary = gateSummary(
    gate({
      criteria: [
        { key: 'a', label: '样本量', met: true, detail: '20/20' },
        { key: 'b', label: '一致率', met: true, detail: '95%' },
        { key: 'c', label: '误放行', met: false, detail: '2' },
      ],
    }),
  )
  assert.match(summary, /3 项判据里达标 2 项/)
})

test('零条判据时不夸口', () => {
  assert.match(gateSummary(gate()), /达标 0 项/)
})

// ------------------------------------------------------------------ 开关

test('总开关关着时明说"连 Run 都不建"', () => {
  const line = switchSentence({ enabled: false, mode: 'shadow', config_error: null })
  assert.match(line, /连 Run 都不建/)
  assert.match(line, /config\/automation\.yaml/, '要告诉人去哪里改')
})

test('影子模式下开着也强调"一条都不会发"', () => {
  const line = switchSentence({ enabled: true, mode: 'shadow', config_error: null })
  assert.match(line, /一条都不会发/)
})

test('正式模式要说清它会真的发出去', () => {
  const line = switchSentence({ enabled: true, mode: 'live', config_error: null })
  assert.match(line, /会被自动发布/)
})

test('配置读不了是安全的，说明成"跳过本轮"而不是报错', () => {
  const line = switchSentence({
    enabled: true,
    mode: 'shadow',
    config_error: 'YAML 解析失败',
  })
  assert.match(line, /跳过本轮/)
  assert.match(line, /一条都不会发/)
})

// -------------------------------------------------------------- 未知取值

test('未知的模式 / 结论原样显示，不兜底成看起来很正常的那个', () => {
  // 后端加了新模式而前端没跟上时，显示 `live` 会让人以为"它会自己发"。
  assert.equal(modeLabel('canary'), 'canary')
  assert.equal(verdictLabel('defer'), 'defer')
  // null 模式 = 配置文件读不了，显示破折号而不是某个模式名。
  assert.equal(modeLabel(null), '—')
})

test('来源标记只区分两种，未知取值原样显示', () => {
  assert.equal(originLabel('scheduled'), '自动调度')
  assert.equal(originLabel('manual'), '人工发起')
  assert.equal(originLabel('cron'), 'cron')
})
