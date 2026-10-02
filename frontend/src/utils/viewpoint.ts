/**
 * 核心判断块的**纯函数**部分。
 *
 * 抽出来而不是写在组件里，是为了能被 `node --test` 直接测：这个仓库的前端
 * 测试只测纯函数（没有 Jest / Vitest，也没有 DOM 环境）。而这里每一句措辞
 * 都在做同一个判断——**把"算出来的"与"模型说的"分开**——那正是最容易在
 * 后续改动里被悄悄抹平的东西，值得用测试钉住。
 *
 * 措辞纪律（与 `app/agent/labels.py` 的分工一致）：
 *
 *   - `confidence` 回答"站不站得住"，`viewpoint_score` 回答"值不值得拿出来说"。
 *     两个数**不并排成一个**，也不互相换算。
 *   - `stance` 是**判断**有多笃定，不是表达有多硬（那是 `style.stance_strength`）。
 *   - `scope_exceeds_evidence` 是**参考信号**，不是扣分：要说成"这条判断主张的
 *     范围比证据大"，不能说成一个红色的分数。
 */

import type { RunViewpointSummary, ViewpointAssessment } from '../types/api'

/** 置信度：0.8345 → `0.83`。null / undefined → `—`。
 *
 * **不做成百分比**：`83%` 会被读成"83% 的概率是对的"，而这只是四个分项
 * 加权出来的一个 0–1 的数。小数形式更不容易被当成概率。 */
export function confidenceLabel(confidence: number | null | undefined): string {
  if (confidence === null || confidence === undefined) return '—'
  return confidence.toFixed(2)
}

/** 观点分：0–100，保留一位。null = 这次没有评估过（不是 0 分）。 */
export function scoreLabel(score: number | null | undefined): string | null {
  if (score === null || score === undefined) return null
  return score.toFixed(1)
}

/**
 * 立场那一句话。
 *
 * 三种情形要长得不一样，否则审核员分不清"模型主动保守"与"证据只允许到这里"：
 *
 * 1. 模型没申报 → 生效值就是证据上界（**不构成超证据承诺**，`angles.py` 的
 *    打分器在 reasons 里也是这么写的）；
 * 2. 申报了且没被压过 → 说"模型申报 X"；
 * 3. 申报了但被压过 → 两个值都要出现，并指出**是证据把它压下来的**。
 *    只说生效值会让人以为模型一开始就这么谨慎。
 */
export function stanceSentence(
  vp: Pick<RunViewpointSummary, 'stance' | 'stance_label' | 'stance_declared' | 'max_stance_label'>,
): string {
  if (!vp.stance_label) return '这次运行没有留下立场判断。'
  if (!vp.stance_declared) {
    return `${vp.stance_label}（模型没有单独申报立场，取证据允许的上界：${vp.max_stance_label}）`
  }
  if (vp.stance_declared === vp.stance) {
    return `${vp.stance_label}（模型申报的立场，与证据允许的范围一致）`
  }
  return `${vp.stance_label}（模型原本申报得更强，被证据上界 ${vp.max_stance_label} 压下来了）`
}

/**
 * 适用范围那一句话：申报范围 vs 证据范围。
 *
 * `scope_exceeds_evidence` 为真时必须**明确说这是参考信号**。它不扣分、不改
 * 分数（`viewpoint.py` 里只触发立场上限），写成一句警告会让人以为 Agent 因此
 * 被惩罚，从而去"修"一个本来正确的分数。
 */
export function scopeSentence(
  vp: Pick<
    RunViewpointSummary,
    'claim_scope' | 'claim_scope_label' | 'evidence_scope_label' | 'scope_exceeds_evidence' | 'scope_declared_missing'
  >,
): string {
  const supported = vp.evidence_scope_label || '未知'
  if (vp.scope_declared_missing || !vp.claim_scope_label) {
    // "没申报"与"申报了一个恰好合规的范围"是两件事，措辞必须分开。
    return `这条判断没有申报自己的适用范围；证据能支撑到「${supported}」。`
  }
  const declared = `这条判断主张的范围是「${vp.claim_scope_label}」，证据能支撑到「${supported}」`
  if (vp.scope_exceeds_evidence) {
    return `${declared}——主张的范围比证据大。这是一个参考信号，不是扣分：它不改变上面的任何分数，只是把立场上界压下来了一档。`
  }
  return `${declared}，没有超出。`
}

/**
 * 置信度是**怎么算出来的**，一句话列全四个分项。
 *
 * 没有这句话，`0.83` 就是一个无法追问的数——而工作单第十二节把"这个观点 9.4 分
 * 但无法复算"写成了明确的反例。四个分项的权重之和是 1，所以这里的乘加是完整的，
 * 不是摘录。
 *
 * `assessment` 为 null（Phase 5B.3 之前的运行）时返回 null：**那时的置信度是
 * 模型给的**，硬套现在的公式会伪造出一段并不存在的推导过程。
 */
export function confidenceBreakdown(
  assessment: ViewpointAssessment | null,
): string | null {
  if (!assessment) return null
  const parts: [string, number | null, number][] = [
    ['证据支撑度', assessment.evidence_support, 0.4],
    ['来源多样性', assessment.source_diversity, 0.25],
    ['反方意识', assessment.counterpoint_awareness, 0.2],
    ['未知的反面（1 − 未知压力）', null, 0.15],
  ]
  const terms = parts.map(([name, value, weight]) => {
    if (value === null) {
      const certainty = assessment.unknown_pressure
      const shown = certainty === null ? '—' : (1 - certainty).toFixed(4)
      return `${name} ${shown} × ${weight}`
    }
    return `${name} ${value.toFixed(4)} × ${weight}`
  })
  const raw =
    assessment.confidence_raw === null ? '' : `（封顶前 ${assessment.confidence_raw.toFixed(4)}）`
  return `confidence = ${terms.join(' + ')}${raw}`
}

/**
 * 分项里的"原始值"与被封顶后的值。
 *
 * 只在两者**不同**时返回一句说明：相同却恒显示，会让一次没有被压过的运行也
 * 背上一句"被压过"的解释，久了就没人读了——那时真正的封顶也会被忽略。
 */
export function capNote(
  name: string,
  raw: number | null,
  capped: number | null,
): string | null {
  if (raw === null || capped === null || raw === capped) return null
  return `${name}封顶前是 ${raw.toFixed(4)}，被上限压到 ${capped.toFixed(4)}`
}
