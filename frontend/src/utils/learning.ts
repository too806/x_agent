/**
 * 学习准备度块的**纯函数**部分（Phase 5C）。
 *
 * 抽出来而不是写在组件里，理由同 `utils/feedback.ts`：`node --test` 只测纯函数
 * （没有 Jest / Vitest，也没有 DOM 环境）。而这一层的风险**全在措辞上**，
 * 且每一种错法都是安静的：
 *
 *   - `null`（这一组在该指标上没有读数）被渲染成 `0` —— 于是"没采到"变成
 *     一条"这一类表现更差"的假结论；
 *   - 未成立的比较被摆成两个数字并排 —— 读的人一定会自己去比，而那条比较
 *     之所以不成立，恰恰是因为不该比（样本不够 / 窗口不同 / 读数缺失）；
 *   - 观察被写成评价 —— "在当前样本中观察到更高"被读成"这样写更好"。
 *
 * 所以下面几乎全是否定式的保证：`null` 不变成 `0`、不成立的比较不给数字、
 * 不出现"好 / 差 / 优秀 / 应该"。
 */

import type {
  LearningComparisonRow,
  LearningEvidenceRow,
  LearningOverview,
} from '../types/api'

/**
 * 未知读数的显示文本。
 *
 * **必须**与 `utils/feedback.ts::NO_DATA` 逐字相同，`tests/learning.test.ts` 里
 * 有一条断言把这件事钉住。
 *
 * 为什么是两个常量而不是一个 import：`src/utils/*` 全部只做**类型**导入
 * （见 `viewpoint.ts` / `format.ts`），因为 `node --test` 解析不了无扩展名的
 * 相对导入——而这个仓库的前端测试正是靠 `node --test` 直接加载这些模块的。
 * 为了让 `medianLabel` 拿到那个字符串而破例做一次运行时导入，就等于让本文件
 * 从"可被测试直接加载"变成"只能在打包器里加载"。
 *
 * 两处常量 + 一条相等断言，比一处常量 + 一个别人不知道自己踩了的加载限制好。
 */
export const NO_DATA = '暂无数据'

/**
 * 闸门原因的**封闭表**。表外的原因原样显示英文枚举值，**不猜一个中文名**——
 * 猜出来的名字会让人以为那是一种新的结论，而它其实只是后端加了一个新分支。
 */
export const READINESS_REASON_LABELS: Record<string, string> = {
  insufficient_sample: '样本不足',
  inconsistent_window: '观察窗口不一致',
  missing_metrics: '可用读数不足',
  invalid_comparison: '没有成立的比较',
  ready: '已具备',
}

/** 分组维度的中文名。与后端 `app/learning/cohort.py::DIMENSION_LABELS` 同义。 */
export const DIMENSION_LABELS: Record<string, string> = {
  structure_type: '写作结构',
  hook_type: '开头方式',
  stance: '判断强度',
  length_bucket: '正文长度档',
  counterpoint_awareness: '是否带反方视角',
  scope_exceeds_evidence: '结论是否超出证据',
}

/** 指标的中文名。这三个**不合成**一个"互动量"——合成会把区别提前抹掉。 */
export const METRIC_LABELS: Record<string, string> = {
  attention: '点赞（注意力）',
  conversation: '对话（回复+引用）',
  amplification: '转发（扩散）',
}

/** 学习证据种类。只有一项是正向结论，其余四项都是"什么也没法说"的诚实出口。 */
export const EVIDENCE_LABELS: Record<string, string> = {
  cohort_difference: '组间观察差异',
  insufficient_sample: '样本不足',
  missing_metric: '缺读数',
  window_mismatch: '窗口不一致',
  invalid_comparison: '比较不成立',
}

export function labelOf(table: Record<string, string>, key: string | null | undefined): string {
  if (!key) return NO_DATA
  return table[key] ?? key
}

/**
 * 一个中位数（或任意计数）的显示文本。
 *
 * 这是这一层最容易被改坏的一个函数：把它写成 `value ?? 0` 不会有任何东西崩，
 * 只会让每一组"没有该指标读数"的帖子看起来像"读数是 0"，然后它在图上落在
 * 最下沿，变成一条关于表现好坏的假结论。
 */
export function medianLabel(value: number | null | undefined): string {
  if (value === null || value === undefined) return NO_DATA
  return Number.isInteger(value) ? String(value) : value.toFixed(2)
}

/**
 * 一句话说清"能不能学"，并且**永远带上 n 与窗口**。
 *
 * 三种状态必须长得不一样：没有帖子 / 还不能学 / 已经可以学。
 * 把"还不能学"显示成一句中性的话，会让它看起来像"已经看过了，没什么发现"。
 */
export function readinessHeadline(overview: LearningOverview): string {
  if (!overview.available) {
    return overview.reason || '还没有任何已发布内容可供观察。'
  }
  const where = `窗口 ${overview.observation_window}，有读数的帖子 ${overview.readiness.sample_count} 条`
  if (overview.readiness.learning_ready) {
    return `可以作为未来的学习材料（${where}）。这不等于会自动调整 Agent。`
  }
  const why = labelOf(READINESS_REASON_LABELS, overview.readiness.reason)
  return `还不能作为学习材料：${why}（${where}）。`
}

/**
 * 一条比较的标题行：维度 + 两组 + 各自的 n + 窗口 + 指标。
 *
 * **样本量必须跟着每一个结论出现**：一个没有 n 的结论无法被判断为"样本太小"。
 */
export function comparisonHeadline(row: LearningComparisonRow): string {
  const dimension = labelOf(DIMENSION_LABELS, row.dimension)
  const metric = labelOf(METRIC_LABELS, row.metric)
  return (
    `${dimension}：${row.group_a}（n=${row.sample_count_a}） vs ` +
    `${row.group_b}（n=${row.sample_count_b}） · ${metric} · ${row.observation_window}`
  )
}

/**
 * 未成立的比较那一行。
 *
 * 它**不返回任何数字**，只返回为什么不比。这不是省略——把两个中位数并排摆出来，
 * 读的人一定会自己去比，而那条比较之所以不成立，恰恰是因为不该比。
 */
export function invalidComparisonLine(row: LearningComparisonRow): string {
  const reason = row.invalid_reason || '比较不成立。'
  return `${comparisonHeadline(row)} —— 不构成可比：${reason}`
}

/**
 * 一条比较在界面上该显示的那句话。
 *
 * 成立 → 后端给的观察性陈述（措辞由后端模板保证，前端一个字节都不改写）；
 * 不成立 → 只说不成立的原因。
 */
export function comparisonStatement(row: LearningComparisonRow): string {
  return row.valid ? row.statement : invalidComparisonLine(row)
}

export function evidenceHeadline(row: LearningEvidenceRow): string {
  const kind = labelOf(EVIDENCE_LABELS, row.type)
  const group = row.comparison_group ? ` · ${row.comparison_group}` : ''
  return `${kind} · n=${row.sample_count}${group}`
}

/**
 * 这一块**没有**回答什么。后端下发的 `limitations` 原样显示，前端不筛也不改写——
 * 被筛掉的那条通常恰好是最该被读到的那条。
 */
export function limitationLines(overview: LearningOverview): string[] {
  return overview.limitations
}

/**
 * 这一层的口径说明，恒为一句。
 *
 * 它讲的是**这一层永远成立**的边界，与 `limitations`（这次数据的具体缺口）分开：
 * 混在一起，具体缺口会被当成又一句免责声明略过去。最后一句是这一阶段最要紧的：
 * 本层只观察，不修改任何东西。
 */
export const LEARNING_DISCLAIMER =
  '这些是观察到的差异，不是因果关系，也不是"以后该怎么写"的规则；' +
  '本层只观察与评估，不会自动修改 Agent 的任何设置。'

/** Run Detail 里那一行紧凑状态的文本。同样**永远带 n 与窗口**。 */
export function compactReadiness(overview: LearningOverview): string {
  if (!overview.available) {
    return overview.reason || '还没有已发布内容可供观察'
  }
  const verdict = overview.readiness.learning_ready ? '可作学习材料' : '尚不可作学习材料'
  const why = labelOf(READINESS_REASON_LABELS, overview.readiness.reason)
  return `${verdict}（${why}）· 窗口 ${overview.observation_window} · n=${overview.readiness.sample_count}`
}
