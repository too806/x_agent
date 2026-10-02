/**
 * 学习提案块的**纯函数**部分（Phase 5D）。
 *
 * 抽出来的理由同 `utils/learning.ts`：`node --test` 只测纯函数（没有 DOM 环境），
 * 而这一块的风险**全在措辞上**。5C 那块的风险是"把没读数渲染成 0"，这一块多出
 * 三种新的错法，而且每一种都是安静的：
 *
 *   - **建议被读成动作**："可以尝试把开头方式取 question"被读成"系统要改成
 *     question 了"。本层按下按钮之后什么都不发生，界面必须说出这一点；
 *   - **批准被读成已应用**：`approved` 是"人做过的决定"，不是"生产配置已经变了"。
 *     两者在界面上长得一样，是这个阶段最容易出的谎；
 *   - **当前取值被当成事实**：没有基准配方时 `current_value` 只是"这次比较里
 *     较低的那一侧"，不是生产在用的值。渲染成"当前是 statement"就是拿一个观察
 *     冒充一个事实（后端为此同时把两个版本号设为 `null`）。
 *
 * 所以下面同样几乎全是否定式的保证：不出现"应该 / 建议采用 / 已生效 / 已应用"，
 * `null` 不变成 `0`，没有版本号时**不显示当前取值**。
 */

import type { LearningProposalRow, ProposalsOverview } from '../types/api'

/**
 * `proposalsHeadline` 需要的最小输入。
 *
 * 从 `ProposalsOverview` 里 `Pick` 出来，而不是另写一份字段清单：另写一份的话，
 * 后端改了字段名，这里会安静地继续编译，直到界面上少显示半句话。
 *
 * 唯独裁决计数是**可选**的：Run Detail 那一行来自 `GET /api/admin/learning`，
 * 那个响应里就有 `proposal_count`，但它没有裁决明细，也不该为了一句标题再发一次
 * 重读。让两个页面共用**同一个**标题函数，是为了避免"准备度说数据不够、提案说
 * 没问题"这种两处各写一句才会出现的矛盾。
 */
export type ProposalsHeadlineInput = Pick<
  ProposalsOverview,
  'available' | 'reason' | 'observation_window' | 'learning_ready' | 'proposal_count'
> & {
  reviewable_count?: number
  decided_count?: number
}

export const NO_DATA = '暂无数据'

/**
 * 提案状态的**封闭表**。表外的状态原样显示英文枚举值，**不猜一个中文名**——
 * 猜出来的名字会让人以为那是一种新的结论，而它其实只是后端加了一个分支。
 *
 * 四个取值分两组，差别是"谁做的决定"：
 *   proposed          还没人决定
 *   approved/rejected 人做过的决定（库里存着）
 *   expired           曾经批准，但配方已经往前走过了
 */
export const PROPOSAL_STATUS_LABELS: Record<string, string> = {
  proposed: '待裁决',
  approved: '已批准（未应用）',
  rejected: '已否决',
  expired: '已过期',
}

/** 允许的提案目标。与后端 `ProposalTarget` 同义，用于给不给不出中文名兜底。 */
export const TARGET_LABELS: Record<string, string> = {
  'style_recipe.structure_type': '写作结构',
  'style_recipe.hook_type': '开头方式',
  'style_recipe.target_length': '正文字数档',
  'viewpoint.stance': '判断强度',
  'viewpoint.counterpoint_awareness': '是否带反方视角',
}

export function proposalStatusLabel(status: string): string {
  return PROPOSAL_STATUS_LABELS[status] ?? status
}

export function targetLabel(row: LearningProposalRow): string {
  return row.target_label || TARGET_LABELS[row.target] || row.target
}

/**
 * 这条裁决是不是"过期"了：当时批准过，但配方版本已经往前走。
 *
 * **只信后端下发的 `stale_reason`**，不在前端自己比对版本号。前端的判据会漏掉
 * "配方算不出来"这一类（那时版本号是 `null`，比对不出任何东西），而后端的判据
 * 恰好覆盖了它——两边各算一次，迟早会给出两个答案。
 */
export function isStale(row: LearningProposalRow): boolean {
  return Boolean(row.stale_reason)
}

/**
 * **当前取值是不是一个可信的事实**。
 *
 * 判据是版本号，不是 `current_value` 自己：后端只在"基准配方在该目标上有取值"
 * 时才给出两个版本号，取不到基准时两者都是 `null`。那时 `current_value` 只是
 * 这次比较里较低的那一侧。
 *
 * 用 `!= null` 而不是真值判断：版本号理论上可能是空串（虽然不会），而把它当成
 * "已知"总比把 `"sr1-xxx"` 当成"未知"安全——后者会让界面上一条完全正常的提案
 * 显示成"取值未知"。
 */
export function baselineIsKnown(row: LearningProposalRow): boolean {
  return row.current_recipe_version != null && row.proposed_recipe_version != null
}

/**
 * "改成什么"的那一行。
 *
 * 有可信基准 → `statement → question`；
 * 没有可信基准 → **不显示当前值**，只说建议取什么，并明说当前取值未知。
 * 把 `current_value` 照出来会让人以为那就是生产在用的值。
 */
export function changeLine(row: LearningProposalRow): string {
  if (baselineIsKnown(row)) {
    return `${row.current_value} → ${row.proposed_value}`
  }
  return `${row.proposed_value}（当前取值未知）`
}

/**
 * 配方版本那一行。
 *
 * 版本号缺失时**不显示半对**：只有"当前版本"没有"建议版本"等于说了一半，
 * 读的人会以为建议版本恰好等于当前版本。
 */
export function versionLine(row: LearningProposalRow): string {
  if (!baselineIsKnown(row)) {
    return NO_DATA
  }
  return `${row.current_recipe_version} → ${row.proposed_recipe_version}`
}

/**
 * 一条提案的依据行。**三段缺一不可**：依据了几条证据、看了几条**帖子**、
 * 在哪个窗口。
 *
 * `sample_count` 是帖子数（并集），不是三条指标相加——不要在这里再乘一遍。
 * 没有 n 的建议无法被判断为"样本太小"。
 */
export function evidenceLine(row: LearningProposalRow): string {
  const evidence = row.evidence_ids.length
  return `依据 ${evidence} 条证据 · n=${row.sample_count} 条帖子 · 窗口 ${row.observation_window}`
}

/**
 * 判据那一行。`validation.valid === false` 的提案**不该出现在列表里**
 * （后端在生成阶段就丢弃它们），但真出现了必须显眼——它是"某道闸门漏了"的
 * 唯一可见信号，静默跳过会让漏掉的那一条看起来像从来没有过。
 */
export function validationLine(row: LearningProposalRow): string | null {
  if (row.validation.valid) return null
  const reasons = row.validation.reasons.join('；')
  return `这条建议没有通过判据，不应被采纳：${reasons || '未给出原因'}`
}

/**
 * 提案一览的标题行。
 *
 * 三种状态必须长得不一样：没有数据 / 闸门关着所以**零条** / 有 N 条可裁决。
 * 把"零条"渲染成"没有发现可以改进的地方"，是把一个"数据不够"的结论说成了
 * 一个"看过了，没问题"的结论——这正是工作单第二十八节要防的误读。
 */
export function proposalsHeadline(overview: ProposalsHeadlineInput): string {
  if (!overview.available) {
    return overview.reason || '还没有任何已发布内容可供观察，因此提不出建议。'
  }
  if (!overview.learning_ready) {
    return (
      '数据还不足以提出建议（观察窗口 ' +
      `${overview.observation_window}）。零条建议是一个结论，不是一次失败。`
    )
  }
  if (overview.proposal_count === 0) {
    return `在当前样本中没有值得尝试的方向（窗口 ${overview.observation_window}）。`
  }
  // 裁决计数是可选的：Run Detail 里那句话来自 `GET /api/admin/learning`，
  // 那个响应**只带条数、不带裁决明细**。缺了就不说，而不是拿 0 顶上——
  // "待裁决 0 条"和"不知道有几条待裁决"是两件事，前者是假的。
  const decided =
    overview.reviewable_count === undefined || overview.decided_count === undefined
      ? ''
      : `（待裁决 ${overview.reviewable_count} 条，已裁决 ${overview.decided_count} 条）`
  return (
    `可以尝试的方向 ${overview.proposal_count} 条${decided}` +
    ` · 窗口 ${overview.observation_window}`
  )
}

/**
 * 一条提案的紧凑标题。目标 + 改成什么 + 状态。
 *
 * **不带**"应当/推荐"字样：这里说的是"可以尝试什么"，不是"应该做什么"。
 */
export function proposalHeadline(row: LearningProposalRow): string {
  return `${targetLabel(row)}：${changeLine(row)} · ${proposalStatusLabel(row.status)}`
}

/** 这条提案现在能不能被裁决。裁决过的不再可裁决（后端 409）。 */
export function isReviewable(row: LearningProposalRow): boolean {
  return row.reviewable && row.status === 'proposed'
}

/**
 * 这一块的**口径说明**，恒为一句。
 *
 * 最后两句是这一阶段最要紧的：本层不执行建议，而且批准不等于应用。
 * 与 `overview.limitations`（这次数据的具体缺口）分开——混在一起，
 * 具体缺口会被当成又一句免责声明略过去。
 */
export const PROPOSALS_DISCLAIMER =
  '这些是"可以尝试什么"的建议，不是"应该做什么"的结论；' +
  '它们只是观察到的方向，不构成因果。批准一条建议不会改变 Agent 的任何设置——' +
  '本层不执行建议，只把人的决定记下来供下一阶段参考。'

/**
 * 一条提案的边界（`limitations`），原样显示。
 *
 * 前端不筛也不改写：被筛掉的那条通常恰好是最该被读到的那条（"当前取值未知"、
 * "没有比较过基准的那个取值"都在这里面）。
 */
export function proposalLimitations(row: LearningProposalRow): string[] {
  return row.limitations
}

/** 裁决动作的按钮/结果文案。 */
export const REVIEW_ACTION_LABELS: Record<string, string> = {
  approve: '批准（只记录，不应用）',
  reject: '否决',
}

/**
 * 裁决之后给使用者看的那句话。
 *
 * 必须**明说没有发生变化**——否则"批准"这个词本身就会让人以为系统动了。
 */
export function reviewOutcomeLine(row: LearningProposalRow): string {
  if (row.status === 'approved') {
    return '已记录你的批准。这只是供下一阶段参考的决定，本系统现在没有因此改变任何设置。'
  }
  if (row.status === 'rejected') {
    return '已记录你的否决。这条建议不会再出现在待裁决列表里。'
  }
  return `已记录（${proposalStatusLabel(row.status)}）。`
}
