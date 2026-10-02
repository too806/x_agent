import assert from 'node:assert/strict'
import { test } from 'node:test'

import { NO_DATA as FEEDBACK_NO_DATA } from '../src/utils/feedback.ts'
import {
  DIMENSION_LABELS,
  EVIDENCE_LABELS,
  LEARNING_DISCLAIMER,
  METRIC_LABELS,
  NO_DATA as LEARNING_NO_DATA,
  READINESS_REASON_LABELS,
  compactReadiness,
  comparisonHeadline,
  comparisonStatement,
  evidenceHeadline,
  invalidComparisonLine,
  limitationLines,
  medianLabel,
  readinessHeadline,
} from '../src/utils/learning.ts'
import type {
  LearningComparisonRow,
  LearningEvidenceRow,
  LearningOverview,
} from '../src/types/api.ts'

/**
 * 学习准备度块的措辞（Phase 5C）。
 *
 * 这个文件里几乎所有断言都是**否定式**的，因为这一层的错误全部是安静的：
 *
 *   - `medianLabel(null)` 返回 `'0'` —— 一个"该指标没读到"的组看起来像读数是 0，
 *     于是它落在分布下沿，变成一条关于表现好坏的假结论。这一行是本层前端最容易被
 *     "顺手优化"掉的一行（`value ?? 0` 更短、不会让任何东西报错），必须钉住。
 *   - 未成立的比较被摆成两个数字并排 —— 读的人一定会自己去比，而那条比较之所以
 *     不成立，恰恰是因为不该比（样本不够 / 窗口不同 / 读数缺失）。
 *   - 观察被写成评价 —— "在当前样本中观察到更高"被读成"这样写更好"。
 *
 * 与 `feedback.test.ts` 同一套纪律：纯函数、无 DOM、`node --test` 直接跑。
 *
 *   npm run test:frontend
 */

function comparison(overrides: Partial<LearningComparisonRow> = {}): LearningComparisonRow {
  return {
    dimension: 'hook_type',
    metric: 'attention',
    observation_window: '24-72h',
    group_a: 'question',
    group_b: 'statement',
    sample_count_a: 5,
    sample_count_b: 5,
    median_a: 20,
    median_b: 8,
    observed_difference: -12,
    valid: true,
    invalid_reason: null,
    statement: '在 24-72h 观察窗口下……在当前样本中观察到「question」更高。',
    limitations: [],
    ...overrides,
  }
}

function evidence(overrides: Partial<LearningEvidenceRow> = {}): LearningEvidenceRow {
  return {
    id: 'learning:hook-type:question:statement:24-72h:attention',
    type: 'cohort_difference',
    statement: '在 24-72h 观察窗口下……',
    sample_count: 10,
    comparison_group: 'hook_type: question vs statement @ 24-72h · attention',
    observation_window: '24-72h',
    metric: 'attention',
    dimension: 'hook_type',
    source_snapshot_ids: ['snap-1', 'snap-2'],
    source_post_ids: ['post-1', 'post-2'],
    source_x_post_ids: ['2105998097302028593'],
    confidence: null,
    limitations: [],
    ...overrides,
  }
}

function overview(overrides: Partial<LearningOverview> = {}): LearningOverview {
  const base: LearningOverview = {
    available: true,
    reason: '当前数据来自已落库的观察快照。',
    observation_window: '24-72h',
    readiness: {
      learning_ready: true,
      reason: 'ready',
      observation_window: '24-72h',
      sample_count: 10,
      post_count: 10,
      checks: [
        { name: 'sample_count', passed: true, detail: '窗口内有可用读数的帖子数 10，比较下限 5。' },
      ],
      automatic_modification: false,
      limitations: [],
    },
    sample_count: 10,
    post_count: 10,
    cohort_count: 2,
    valid_comparison_count: 1,
    cohorts: [],
    comparisons: [comparison()],
    evidence: [evidence()],
    limitations: ['未纳入 `topic class` 维度。'],
  }
  return { ...base, ...overrides }
}

// ---------------------------------------------------------------------------
// 未知读数：不得变成 0
// ---------------------------------------------------------------------------

test('未知的中位数显示成「暂无数据」，绝不显示 0', () => {
  assert.equal(medianLabel(null), LEARNING_NO_DATA)
  assert.equal(medianLabel(undefined), LEARNING_NO_DATA)
  // **反例**：必须与上面两句不同，否则整件事没有意义。
  assert.equal(medianLabel(0), '0')
  assert.notEqual(LEARNING_NO_DATA, '0')
})

test('这里与反馈层用的是同一个占位符', () => {
  // 两个模块各留一份常量（理由见 `utils/learning.ts` 里的注释：`src/utils/*`
  // 只能做类型导入，否则 `node --test` 加载不了）。这条断言就是那份代价的护栏——
  // 一边改了、另一边没改，界面会说两种话。
  assert.equal(LEARNING_NO_DATA, FEEDBACK_NO_DATA)
})

test('已有的中位数原样显示，不千分位、不加单位', () => {
  assert.equal(medianLabel(20), '20')
  assert.equal(medianLabel(0), '0')
  assert.equal(medianLabel(2.5), '2.50')
})

// ---------------------------------------------------------------------------
// 能不能学：三种状态必须长得不一样
// ---------------------------------------------------------------------------

test('还不能学时给出原因、样本量与窗口', () => {
  const line = readinessHeadline(
    overview({
      readiness: {
        ...overview().readiness,
        learning_ready: false,
        reason: 'insufficient_sample',
        sample_count: 1,
        post_count: 1,
      },
    }),
  )
  assert.match(line, /还不能作为学习材料/)
  assert.match(line, /样本不足/)
  assert.match(line, /n=1|1 条/)
  assert.match(line, /24-72h/)
  // 不能把"还不能学"写成一句中性的话——那读起来像"看过了，没什么发现"。
  assert.doesNotMatch(line, /没有发现|暂无结论/)
})

test('可以学时明确说清它不等于自动调整 Agent', () => {
  const line = readinessHeadline(overview())
  assert.match(line, /可以作为未来的学习材料/)
  assert.match(line, /不等于会自动调整/)
})

test('没有任何帖子时用后端给的原因，不编一句', () => {
  const line = readinessHeadline(
    overview({ available: false, reason: '还没有任何已发布内容可供观察。' }),
  )
  assert.equal(line, '还没有任何已发布内容可供观察。')
})

test('未知的闸门原因显示原始枚举值，不猜一个中文名', () => {
  const line = readinessHeadline(
    overview({
      readiness: { ...overview().readiness, learning_ready: false, reason: 'brand_new_reason' },
    }),
  )
  assert.match(line, /brand_new_reason/)
})

test('紧凑状态同样带 n 与窗口', () => {
  const line = compactReadiness(overview())
  assert.match(line, /可作学习材料/)
  assert.match(line, /窗口 24-72h/)
  assert.match(line, /n=10/)
  const short = compactReadiness(
    overview({
      readiness: { ...overview().readiness, learning_ready: false, reason: 'invalid_comparison' },
    }),
  )
  assert.match(short, /尚不可作学习材料/)
  assert.match(short, /没有成立的比较/)
})

// ---------------------------------------------------------------------------
// 比较：不成立的不给数字
// ---------------------------------------------------------------------------

test('成立的比较原样透传后端的观察性陈述', () => {
  const row = comparison()
  assert.equal(comparisonStatement(row), row.statement)
})

test('不成立的比较只说不成立的原因，一个数字都不给', () => {
  const row = comparison({
    valid: false,
    invalid_reason: '其中一组的帖子数低于比较下限 5（question: n=3，statement: n=5）。',
    observed_difference: null,
  })
  const line = comparisonStatement(row)
  assert.match(line, /不构成可比/)
  assert.match(line, /低于比较下限/)
  // 中位数就算被后端下发也不许出现在界面上：并排摆出来读的人一定会自己去比。
  assert.doesNotMatch(line, /20/)
  assert.doesNotMatch(line, /8/)
  assert.equal(invalidComparisonLine(row), line)
})

test('比较的标题行带两组的 n、指标与窗口', () => {
  const line = comparisonHeadline(comparison())
  assert.match(line, /开头方式/)
  assert.match(line, /question（n=5）/)
  assert.match(line, /statement（n=5）/)
  assert.match(line, /点赞（注意力）/)
  assert.match(line, /24-72h/)
})

test('未知的维度与指标显示原始值，不猜一个中文名', () => {
  const line = comparisonHeadline(
    comparison({ dimension: 'brand_new_dim', metric: 'brand_new_metric' }),
  )
  assert.match(line, /brand_new_dim/)
  assert.match(line, /brand_new_metric/)
})

// ---------------------------------------------------------------------------
// 证据：样本量与出处必须跟着出现
// ---------------------------------------------------------------------------

test('每条证据的标题都带样本量，成立的比较带上比较组', () => {
  const line = evidenceHeadline(evidence())
  assert.match(line, /组间观察差异/)
  assert.match(line, /n=10/)
  assert.match(line, /question vs statement/)
})

test('"什么也没法说"的证据没有比较组时不硬塞一个', () => {
  const line = evidenceHeadline(
    evidence({
      type: 'insufficient_sample',
      comparison_group: null,
      sample_count: 1,
    }),
  )
  assert.match(line, /样本不足/)
  assert.equal(line.split('·').length, 2)
})

// ---------------------------------------------------------------------------
// 局限与口径：一条都不许筛
// ---------------------------------------------------------------------------

test('局限说明原样透传，不筛不改写', () => {
  const notes = ['有 3 条记录的观察窗口不是 24-72h。', '样本量不足任何组间比较都不成立。']
  assert.deepEqual(limitationLines(overview({ limitations: notes })), notes)
  assert.deepEqual(limitationLines(overview({ limitations: [] })), [])
})

test('口径说明是恒定的那一句：观察、非因果、不改 Agent', () => {
  assert.match(LEARNING_DISCLAIMER, /不是因果关系/)
  assert.match(LEARNING_DISCLAIMER, /不会自动修改 Agent/)
  // 一旦出现"表现好/差"这类词，这一层就变回了一个评分器。
  assert.doesNotMatch(LEARNING_DISCLAIMER, /表现好|表现差|优秀|出色|应该/)
})

// ---------------------------------------------------------------------------
// 封闭表：不许出现评价词，也不许悄悄少一项
// ---------------------------------------------------------------------------

test('所有映射表都不含评价性词汇', () => {
  // 用"更好/更差/最好/最差"而不是裸的 `好|差`：`组间观察差异` 里的"差"是
  // "difference" 的成分，不是"差劲"——裸字匹配会把一个正确的标签判成错的。
  const tables = [READINESS_REASON_LABELS, DIMENSION_LABELS, METRIC_LABELS, EVIDENCE_LABELS]
  for (const table of tables) {
    for (const value of Object.values(table)) {
      assert.doesNotMatch(value, /更好|更差|最好|最差|优秀|出色|最佳|建议|应该|表现好|表现差/, value)
    }
  }
})

test('三张表各自覆盖后端当前会下发的全部取值', () => {
  assert.deepEqual(Object.keys(READINESS_REASON_LABELS).sort(), [
    'inconsistent_window',
    'insufficient_sample',
    'invalid_comparison',
    'missing_metrics',
    'ready',
  ])
  assert.deepEqual(Object.keys(METRIC_LABELS).sort(), [
    'amplification',
    'attention',
    'conversation',
  ])
  // 维度表与指标表**不合成**：没有 `topic_class`（后端刻意不做），
  // 也没有任何"互动总量"这一类需要先合成才能算的东西。
  assert.deepEqual(Object.keys(DIMENSION_LABELS).sort(), [
    'counterpoint_awareness',
    'hook_type',
    'length_bucket',
    'scope_exceeds_evidence',
    'stance',
    'structure_type',
  ])
})
