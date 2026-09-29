/**
 * Agent 决策轨迹的展示逻辑。
 *
 * `agent_decision.action` 是**后端枚举**（`app/agent/schemas.py::AgentAction`），
 * 前端不新增也不重命名任何取值；这里只做两件事：
 *
 *   1. 把动作归到一类，用来决定时间线节点长什么样（研究是蓝的、评审是黄的……）。
 *      分类纯粹是**视觉分组**，不参与任何业务判断。
 *   2. 处理理由为空的情况。
 *
 * 纯函数，跟着 `node --test` 跑（见 `frontend/tests/trace.test.ts`）。
 */

/**
 * 节点类别。取值对应 `app.css` 里 `.trace__item--*` 的修饰类。
 * `plan` 是默认值：认不出的动作落到这里，用中性灰显示，**不隐藏**它。
 */
export type TraceKind = 'plan' | 'research' | 'generate' | 'critic' | 'revise' | 'finish'

/**
 * 动作 → 类别。
 *
 * 认不出的动作返回 `'plan'`（中性）。这与 `format.ts` 里"认不出的动作原样显示
 * 英文枚举"是同一条原则的两半：前端不认识新动作时，**照样把它排在时间线上**，
 * 只是没有专属配色。悄悄丢掉一步比配色不对严重得多。
 */
const TRACE_KINDS: Record<string, TraceKind> = {
  discover_topics: 'plan',
  // `select_topic` 归到 plan 而不是 research：它是一次**方向选择**
  // （在已打分的候选里选定一个），到这一步还没有开始研究。归到 research
  // 会让"选话题"和"研究话题"在时间线上看起来是同一件事。
  select_topic: 'plan',
  research_topic: 'research',
  deepen_research: 'research',
  extract_evidence: 'research',
  generate_angles: 'generate',
  select_angle: 'generate',
  generate_candidates: 'generate',
  criticize: 'critic',
  revise: 'revise',
  finish: 'finish',
}

export function traceKind(action: string | null | undefined): TraceKind {
  if (!action) return 'plan'
  return TRACE_KINDS[action] ?? 'plan'
}

/**
 * 理由文本。
 *
 * 空理由要说成一句话，不能留白：留白看起来像渲染出错，而这恰恰是整条轨迹里
 * **唯一由 Agent 自己写**的字段——它空着本身就是一条信息（"这一步没解释自己"）。
 */
export function traceReason(reason: string | null | undefined): string {
  const trimmed = (reason ?? '').trim()
  return trimmed || '这一步没有留下理由。'
}

/**
 * 一次运行的轨迹概览，用在区块标题上的那行小字。
 *
 * 只数步数和区间，不做评价——"9 步、12 分钟"是事实，"效率高"不是。
 */
export function traceSummary(entries: readonly { step: number }[]): string | null {
  if (entries.length === 0) return null
  if (entries.length === 1) return '1 步'
  const steps = entries.map((entry) => entry.step).filter((step) => Number.isFinite(step))
  if (steps.length === 0) return `${entries.length} 步`
  const min = Math.min(...steps)
  const max = Math.max(...steps)
  // 步号不连续（比如 1 → 3）时说清是"第 1–3 步"而不是"3 步"：
  // 中间缺的那一步是有信息量的（它没有被记下来）。
  const contiguous = max - min + 1 === steps.length
  return contiguous ? `${entries.length} 步` : `第 ${min}–${max} 步，共 ${entries.length} 条`
}
