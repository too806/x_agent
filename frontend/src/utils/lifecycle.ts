/**
 * Run 的生命周期：创建 → 研究 → 生成 → 规则检查 → 审核 → 发布。
 *
 * 这是一个**纯函数**，专门从 `node --test` 里跑（见 `frontend/tests/`）：
 * 它的每一条分支都对应"记录里有什么"和"能说它怎么样"之间的一个判断，
 * 而这些判断错了不会报错，只会安静地在界面上说假话。
 *
 * 贯穿全文件的一条规矩：
 *
 *   **每个阶段的状态由它自己的证据决定，不由 Run 的整体状态反推。**
 *
 * 反推会立刻开始骗人。举个真实的例子：`waiting_for_review` 说明"研究做完了"，
 * 但如果这一轮没往库里存素材，反推会显示"研究 ✓ 7 条"，而实际上一条都没有。
 * 所以只要本阶段的证据缺失，就老实说"走过这一步，但没留下记录"（`unknown`），
 * 而不是替它填一个好看的结论。
 */

/** 阶段的状态。取值刻意比"完成 / 未完成"多——界面上要靠它区分四件事。 */
export type LifecycleState =
  /** 走过且留下了证据。 */
  | 'done'
  /** Run 正停在这里。 */
  | 'current'
  /** 走过且结果明确是好的（规则检查通过、已发布）。 */
  | 'ok'
  /** 走过，但读不到结果 / 没有记录。**不等于成功，也不等于失败**。 */
  | 'unknown'
  /** 走过了，结果是坏的（规则检查未通过、发布失败）。 */
  | 'failed'
  /** 还没走到。 */
  | 'pending'
  /** 不会走到了（内容被拒绝，发布这一阶段永远不会发生）。 */
  | 'skipped'

export type LifecycleKey = 'created' | 'research' | 'generate' | 'guardrail' | 'review' | 'publish'

export interface LifecycleStep {
  key: LifecycleKey
  label: string
  state: LifecycleState
  /** 这一步的时刻。**没有可靠时刻时为 null** —— 不用别的字段凑一个。 */
  at: string | null
  /** 一句话说明"这一步留下了什么"。永远有值。 */
  note: string
}

/**
 * 构造生命周期所需的输入。
 *
 * 刻意是一个**扁平的窄类型**，而不是直接吃 `AdminRunDetail`：这样测试可以只造
 * 它关心的那几个字段，也让"这个函数依赖哪些事实"一眼可见。
 */
export interface LifecycleInput {
  run_status: string
  created_at: string
  /** Agent 选定主题、真正开跑的时刻。 */
  started_at: string | null
  research_count: number
  /** 是否存下了草稿。 */
  has_draft: boolean
  /** 草稿版本号，用于摘要（"v2"）。没有草稿时为 null。 */
  draft_version: number | null
  /** 来自 checkpoint。null = **读不到**，不是"没通过"。 */
  guardrail_passed: boolean | null
  /** 人工决定（approve / edit / reject）。null = 还没有人决定。 */
  review_action: string | null
  review_at: string | null
  /** 发布记录的状态。null = **没有发布记录**。 */
  publish_status: string | null
  /** 已发布时刻 / 尝试更新时间。 */
  publish_at: string | null
}

/**
 * Run 走到了流程的第几段。
 *
 * 只用来决定"某个还没有证据的阶段，是'正在做'还是'还没到'"。**不用它反推
 * 任何一个已经能靠自身证据判断的阶段**（见文件头）。
 */
const PROGRESS: Record<string, number> = {
  running: 1,
  waiting_for_review: 2,
  rejected: 3,
  completed: 3,
  approved: 3,
  edited: 3,
  publishing: 4,
  publish_unknown: 4,
  published: 5,
  // 失败可能发生在任何一步，所以它**没有**一个统一的位置。这里给 1（最靠前），
  // 让下面每一段的判断退回"看它自己的证据"：有素材就显示有素材，有草稿就显示
  // 有草稿，而不是因为整体叫 failed 就把所有阶段抹成未完成。
  failed: 1,
}

function progressOf(runStatus: string): number {
  return PROGRESS[runStatus] ?? 1
}

const REVIEW_ACTION_LABELS: Record<string, string> = {
  approve: '人工已批准',
  edit: '人工已编辑',
  reject: '人工已拒绝',
}

export function buildLifecycle(input: LifecycleInput): LifecycleStep[] {
  const rank = progressOf(input.run_status)
  const started = input.started_at !== null
  const rejected = input.review_action === 'reject'

  const steps: LifecycleStep[] = []

  steps.push({
    key: 'created',
    label: '创建',
    state: 'done',
    at: input.created_at,
    note: '任务已入库。',
  })

  // ---------------------------------------------------------------- 研究
  if (input.research_count > 0) {
    steps.push({
      key: 'research',
      label: '研究',
      state: 'done',
      at: input.started_at,
      note: `存下 ${input.research_count} 条素材。`,
    })
  } else if (rank >= 2) {
    // 走到了人工审核，说明这一步必然执行过；但库里没有素材。
    // 说"研究 ✓"是撒谎，说"还没研究"也是撒谎（它明显已经过了）。
    steps.push({
      key: 'research',
      label: '研究',
      state: 'unknown',
      at: input.started_at,
      note: '已走过这一步，但没有存下素材。',
    })
  } else {
    steps.push({
      key: 'research',
      label: '研究',
      state: started ? 'current' : 'pending',
      at: input.started_at,
      note: started ? '正在检索与整理素材。' : '还没开始。',
    })
  }

  // ---------------------------------------------------------------- 生成
  if (input.has_draft) {
    steps.push({
      key: 'generate',
      label: '生成',
      state: 'done',
      at: null,
      note:
        input.draft_version !== null ? `已存下草稿 v${input.draft_version}。` : '已存下草稿。',
    })
  } else if (rank >= 2) {
    steps.push({
      key: 'generate',
      label: '生成',
      state: 'unknown',
      at: null,
      note: '已走过这一步，但没有存下草稿。',
    })
  } else {
    steps.push({
      key: 'generate',
      label: '生成',
      state: input.research_count > 0 ? 'current' : 'pending',
      at: null,
      note: input.research_count > 0 ? '正在根据素材写稿。' : '还没开始。',
    })
  }

  // ------------------------------------------------------------ 规则检查
  // 时间戳：checkpoint 里没有记，所以这一格永远不给时间。不给比给个近似值好。
  if (input.guardrail_passed === true) {
    steps.push({
      key: 'guardrail',
      label: '规则检查',
      state: 'ok',
      at: null,
      note: '通过。',
    })
  } else if (input.guardrail_passed === false) {
    steps.push({
      key: 'guardrail',
      label: '规则检查',
      state: 'failed',
      at: null,
      note: '未通过。原因见下方。',
    })
  } else if (rank >= 2) {
    steps.push({
      key: 'guardrail',
      label: '规则检查',
      state: 'unknown',
      at: null,
      note: '结果读不到（checkpoint 缺失）。这不等于通过。',
    })
  } else {
    steps.push({
      key: 'guardrail',
      label: '规则检查',
      state: 'pending',
      at: null,
      note: '还没开始。',
    })
  }

  // ---------------------------------------------------------------- 审核
  if (input.review_action !== null) {
    steps.push({
      key: 'review',
      label: '审核',
      state: rejected ? 'failed' : 'ok',
      at: input.review_at,
      note: REVIEW_ACTION_LABELS[input.review_action] ?? `人工已处理（${input.review_action}）。`,
    })
  } else if (input.run_status === 'waiting_for_review') {
    steps.push({
      key: 'review',
      label: '审核',
      state: 'current',
      at: null,
      note: '正等着人做决定。',
    })
  } else if (rank >= 3) {
    steps.push({
      key: 'review',
      label: '审核',
      state: 'unknown',
      at: null,
      note: '已走过这一步，但没有留下人工决定记录。',
    })
  } else {
    steps.push({
      key: 'review',
      label: '审核',
      state: 'pending',
      at: null,
      note: '还没开始。',
    })
  }

  // ---------------------------------------------------------------- 发布
  steps.push(publishStep(input, rejected, rank))

  return steps
}

function publishStep(input: LifecycleInput, rejected: boolean, rank: number): LifecycleStep {
  const base = { key: 'publish' as const, label: '发布' }

  switch (input.publish_status) {
    case 'published':
      return { ...base, state: 'ok', at: input.publish_at, note: 'X 已接受这条帖子。' }
    case 'failed':
      return {
        ...base,
        state: 'failed',
        at: input.publish_at,
        note: '确定没发出去，重发是安全的。',
      }
    case 'publishing':
      return {
        ...base,
        state: 'current',
        at: input.publish_at,
        note: '已发起，结果还没记下来。',
      }
    case 'publish_unknown':
      return {
        ...base,
        state: 'unknown',
        at: input.publish_at,
        note: 'X 上可能已经有这条帖子，需要人工裁决。',
      }
    default:
      break
  }

  // 走到这里意味着**没有发布记录**。三种原因，说法完全不同。
  if (rejected) {
    return { ...base, state: 'skipped', at: null, note: '内容已被拒绝，不会发布。' }
  }
  if (input.run_status === 'approved' || input.run_status === 'edited') {
    return { ...base, state: 'current', at: null, note: '已通过审核，等待发布。' }
  }
  if (input.run_status === 'waiting_for_review' || rank < 3) {
    return { ...base, state: 'pending', at: null, note: '还没开始。' }
  }
  return { ...base, state: 'pending', at: null, note: '还没有发布记录。' }
}
