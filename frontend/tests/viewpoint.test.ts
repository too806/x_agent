import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  capNote,
  confidenceBreakdown,
  confidenceLabel,
  scopeSentence,
  scoreLabel,
  stanceSentence,
} from '../src/utils/viewpoint.ts'
import type { ViewpointAssessment } from '../src/types/api.ts'

/**
 * 核心判断块的措辞（Phase 5B.3）。
 *
 * 这些函数一个字符的渲染都不做，全是纯函数——正因为这样才值得测：它们承担的
 * 全部风险都在**措辞**上，而措辞错的方式是安静的。把 `confidence` 写成百分比、
 * 把"模型没申报立场"与"证据只允许到这里"混成一句话、把 `scope_exceeds_evidence`
 * 渲染成一条扣分警告——这三件事都不会让任何东西崩，只会让人读错。
 *
 * 所以下面的断言几乎都是**否定式**的：不含 `%`、不出现"扣分"以外的歧义、
 * `null` 不得变成 `0.00`。
 *
 *   npm run test:frontend
 */

/** 只填这条测试关心的键，其余走默认值——字段一多起来，逐字构造会淹没差异。 */
function assessment(overrides: Partial<ViewpointAssessment> = {}): ViewpointAssessment {
  return {
    evidence_support: 0.8,
    evidence_support_raw: 0.8,
    source_diversity: 0.7,
    counterpoint_awareness: 0.5,
    unknown_pressure: 0.2,
    specificity: 0.6,
    novelty: 0.3,
    newness_contribution: 0.0,
    confidence_raw: 0.74,
    viewpoint_score_raw: 79.7,
    bound_facts: 4,
    total_facts: 5,
    bound_sources: 5,
    bound_authors: 5,
    missing_author_sources: 0,
    declared_counterpoints: 1,
    bound_counterpoints: 1,
    total_materials: 7,
    unreferenced_materials: 2,
    breadth_markers: [],
    originality_claim: false,
    restates_fact: false,
    duplicate_group: '',
    reasons: [],
    ...overrides,
  }
}

// ---------------------------------------------------------------------------
// 两个数：置信度 / 观点分
// ---------------------------------------------------------------------------

test('置信度是小数，不是百分比', () => {
  assert.equal(confidenceLabel(0.8345), '0.83')
  // 项目里的每个 0–1 分项都走这一步。一旦有人把它改成 `${(x*100).toFixed(0)}%`，
  // "0.83" 会被读成"83% 的概率是对的"——那是一个系统从未计算过的量。
  assert.ok(!confidenceLabel(0.8345).includes('%'))
})

test('置信度缺失时给破折号，不给 0', () => {
  // `0.00` 是一个可断言的说法（"完全站不住"），与"这个 Run 没留下这个数"是
  // 两件事。5B.3 之前的 Run 走的正是这一支。
  assert.equal(confidenceLabel(null), '—')
  assert.equal(confidenceLabel(undefined), '—')
  assert.notEqual(confidenceLabel(null), '0.00')
})

test('观点分缺失时返回 null，与实际算出的 0 分分开', () => {
  assert.equal(scoreLabel(79.73), '79.7')
  assert.equal(scoreLabel(0), '0.0')
  // 关键区别：0 分是"评估过，不值一提"，null 是"这次没有评估"。
  // 渲染层靠这个 null 决定要不要显示这一格。
  assert.equal(scoreLabel(null), null)
  assert.equal(scoreLabel(undefined), null)
})

// ---------------------------------------------------------------------------
// 立场：三种情形必须长得不一样
// ---------------------------------------------------------------------------

const STANCE_BASE = {
  stance: 'strong',
  stance_label: '明确判断',
  max_stance_label: '明确判断',
}

test('模型没申报立场时说明取的是证据上界', () => {
  const sentence = stanceSentence({ ...STANCE_BASE, stance_declared: null })
  assert.ok(sentence.includes('没有单独申报'), sentence)
  assert.ok(sentence.includes('明确判断'), sentence)
})

test('申报值与生效值一致时说"一致"', () => {
  const sentence = stanceSentence({ ...STANCE_BASE, stance_declared: 'strong' })
  assert.ok(sentence.includes('一致'), sentence)
  assert.ok(!sentence.includes('压下来'), sentence)
})

test('申报得比证据允许的更强时，两个值都要出现并指出是证据压下来的', () => {
  const sentence = stanceSentence({
    stance: 'cautious',
    stance_label: '谨慎',
    stance_declared: 'strong',
    max_stance_label: '谨慎',
  })
  assert.ok(sentence.includes('谨慎') && sentence.includes('申报得更强'), sentence)
  // 只说生效值会让人以为模型一开始就这么谨慎——那是把"被规则压过"读成
  // "模型的判断力"。
  assert.ok(sentence.includes('证据上界') || sentence.includes('证据'), sentence)
})

test('完全没有立场时直说，不编一个默认值', () => {
  // 5B.3 之前的 Angle 落在这里（`stance` 是空串）。这时说"一致"是错的——
  // 没有立场，谈不上一致。
  const sentence = stanceSentence({
    stance: '',
    stance_label: '',
    stance_declared: null,
    max_stance_label: '',
  })
  assert.ok(sentence.includes('没有留下'), sentence)
})

// ---------------------------------------------------------------------------
// 适用范围
// ---------------------------------------------------------------------------

const SCOPE_BASE = {
  claim_scope: 'industry',
  claim_scope_label: '整个行业',
  evidence_scope_label: '某个细分',
  scope_exceeds_evidence: false,
  scope_declared_missing: false,
}

test('未申报范围与"申报了一个恰好合规的范围"措辞不同', () => {
  const missing = scopeSentence({ ...SCOPE_BASE, claim_scope: null, claim_scope_label: '', scope_declared_missing: true })
  const declared = scopeSentence(SCOPE_BASE)
  assert.notEqual(missing, declared)
  assert.ok(missing.includes('没有申报'), missing)
})

test('超出证据范围时，明说这是参考信号而不是扣分', () => {
  const sentence = scopeSentence({ ...SCOPE_BASE, scope_exceeds_evidence: true })
  assert.ok(sentence.includes('参考信号'), sentence)
  // 这一条是防"运维去修一个本来正确的分数"：如果界面读起来像扣分，
  // 就会有人去调整那个并没有被这个信号影响过的 viewpoint_score。
  assert.ok(sentence.includes('不改变'), sentence)
  assert.ok(!sentence.includes('扣了'), sentence)
})

test('范围没超出时也要把那句话说完', () => {
  const sentence = scopeSentence(SCOPE_BASE)
  assert.ok(sentence.includes('整个行业') && sentence.includes('某个细分'), sentence)
  assert.ok(sentence.includes('没有超出'), sentence)
})

// ---------------------------------------------------------------------------
// 置信度的推导过程
// ---------------------------------------------------------------------------

test('没有评估明细时不给推导，只返回 null', () => {
  // 5B.3 之前的运行，置信度是模型直接给的。硬套现在的四项公式会**伪造**
  // 一段并不存在的推导过程——比不显示更糟。
  assert.equal(confidenceBreakdown(null), null)
})

test('推导把四个分项按权重全部列出', () => {
  const text = confidenceBreakdown(assessment())
  assert.ok(text, '有 assessment 时必须返回文本')
  for (const name of ['证据支撑度', '来源多样性', '反方意识', '未知的反面']) {
    assert.ok(text!.includes(name), `缺少分项：${name}`)
  }
  // 权重之和为 1，所以这四个乘加是完整的，不是摘录。
  assert.ok(text!.includes('0.4') && text!.includes('0.25'), text)
  assert.ok(text!.includes('0.2') && text!.includes('0.15'), text)
})

test('未知的反面用 1 − 未知压力，且未知压力缺失时不假装算过', () => {
  const text = confidenceBreakdown(assessment({ unknown_pressure: 0.2 }))
  assert.ok(text!.includes('0.8000'), text)

  const unknown = confidenceBreakdown(assessment({ unknown_pressure: null }))
  // 缺输入时给破折号：填 0 会让"没算出来"读成"压力为零，非常确定"。
  assert.ok(unknown!.includes('—'), unknown)
})

test('封顶前与封顶后相同就不说明', () => {
  // 恒显示会让一次没被压过的运行也背上一句"被压过"的解释，久了就没人读了。
  assert.equal(capNote('证据支撑度', 0.8, 0.8), null)
  assert.equal(capNote('证据支撑度', null, 0.8), null)
  assert.equal(capNote('证据支撑度', 0.8, null), null)
})

test('真的被封顶时把两个值都写出来', () => {
  const note = capNote('证据支撑度', 0.95, 0.8)
  assert.ok(note && note.includes('0.9500') && note.includes('0.8000'), String(note))
})
