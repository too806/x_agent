import type { RunStatus } from '../types/api'

/**
 * 发布入口可见性契约（Publish Action Visibility Contract）。
 *
 * 这里是"什么时候可以显示哪个发布按钮"的**唯一**判断点。它此前散落在若干处
 * JSX 里（抽屉里的 `canPublish`、发布记录页里的一串三元表达式），各自看起来都
 * 合理，但放在一起就对不齐了 —— 而这种对不齐的代价不是"界面不统一"，
 * 是**多发一条推文**。
 *
 * 所以这个模块只做一件事：给定 Run 状态与发布记录状态，返回四种有限动作之一。
 * 它是纯的 —— 不调 API、不导航、不读 state / localStorage / window、不看时钟。
 * 要发帖或要跳转的是调用方，不是它。
 *
 * 安全原则（§八）：**宁可少一个发布按钮，也不要多一个可能造成重复帖子的按钮。**
 * 任何读不到、认不出、说不清的情况一律 HIDDEN，绝不猜。
 *
 * 幂等性由 `published_post.agent_run_id` 的唯一约束保证 —— 一个 Run 至多一条
 * 发布记录，因此"哪一条是当前有效记录"不需要在这里判断。
 */

/** 四种有限动作。顺序即优先级，见 `getPublishAction` 里的注释。 */
export const PUBLISH_ACTIONS = ['FIRST_PUBLISH', 'REPUBLISH', 'RESOLVE', 'HIDDEN'] as const

export type PublishAction = (typeof PUBLISH_ACTIONS)[number]

/**
 * 具备发布资格的 Run 状态。
 *
 * 只有这两个 —— 它们等价于"人工审核已经给出结论，可以进入发布"。
 * `waiting_for_review` 不在其中：还没人看过的东西不能发。
 *
 * 注意 `RunStatus` 里还有 `publishing` / `publish_unknown` / `published` 三个值，
 * 那是 Run 在发布期间被镜像推进的状态。它们**不是**新的发布资格，因此不在这里 ——
 * 否则一次"结果未知"就能把自己论证成一个可发布状态。
 */
export const PUBLISH_ELIGIBLE_RUN_STATUSES: readonly RunStatus[] = ['approved', 'edited']

const ELIGIBLE = new Set<string>(PUBLISH_ELIGIBLE_RUN_STATUSES)

/**
 * Run 是否具备发布资格。
 *
 * 参数类型故意放宽到 `string | null | undefined`：真实调用点传的是 `RunStatus`
 * 联合类型的成员（编译期仍然受约束），但这个函数必须是**全函数** —— 后端哪天加了
 * 一个新状态而前端还没跟上时，它必须返回 false，而不是抛错或默认放行。
 */
export function isRunPublishEligible(runStatus: string | null | undefined): boolean {
  return runStatus != null && ELIGIBLE.has(runStatus)
}

/**
 * 唯一的判断入口。
 *
 * ```text
 * 1. 发布记录是 publish_unknown？   → RESOLVE（不受 Run 资格约束，见下）
 * 2. Run 具备发布资格？  否         → HIDDEN
 * 3. 发布记录不存在（显式 null）？  → FIRST_PUBLISH
 * 4. 发布记录状态：
 *      failed          → REPUBLISH
 *      publishing      → HIDDEN
 *      published       → HIDDEN
 *      其它 / 读不到    → HIDDEN
 * ```
 *
 * 三处刻意的决定，都值得写下来：
 *
 * **① `publish_unknown` 排在资格判断之前。** 契约的 §三 要求"无论 Run 当前是什么
 * 状态都返回 RESOLVE"，而 §七 的层级示例里 `rejected + failed → HIDDEN`。两者并不
 * 冲突：§六 要求隐藏的是**发布动作**，而 RESOLVE 不是发布动作，它是一个"去别处
 * 处理这条未决记录"的入口。让资格判断先跑会把 `rejected + publish_unknown` 变成
 * HIDDEN —— 那等于把一条可能已经在 X 上存在的帖子从界面上藏起来，比多一个按钮更糟
 * （多一个按钮违反 §八，藏起来违反 §三）。所以这里让 §三 优先。
 *
 * **② `publishing` 是 HIDDEN，不是 RESOLVE。** 这看起来和 `types/api.ts` 里的
 * `isUnresolved()`（它把两者都算"未决"）矛盾，其实是两个不同的问题：
 * `isUnresolved` 回答"发布记录页要不要给这一行开裁决入口"（后端 `UNRESOLVED_STATUSES`
 * 确实接受两者，见 `app/repositories.py:confirm_published_manually` 的注释），
 * 而这个函数回答"要不要在这里放一个会调 X 的按钮"。`publishing` 意味着第一个请求
 * 可能还在飞，此时任何"再发一次"的入口都可能是第二条帖子。所以这里**不要改用
 * `isUnresolved`**。
 *
 * **③ `null` 与 `undefined` 是两件事。**
 *   - `null`   = 后端明确地回答了"这个 Run 没有发布记录" → 可以首次发布
 *   - `undefined` = 这个字段压根没读到（接口变了、字段被删了）→ 不知道，HIDDEN
 * 把两者当成一回事，就会在"读不到"的时候给出一个发布按钮。
 */
export function getPublishAction(
  runStatus: string | null | undefined,
  publishStatus: string | null | undefined,
): PublishAction {
  // ① 结果未知 → 去裁决。不看 Run 资格，理由见上面的注释①。
  if (publishStatus === 'publish_unknown') return 'RESOLVE'

  // ② 发布记录不能凌驾于人工审核资格。
  if (!isRunPublishEligible(runStatus)) return 'HIDDEN'

  // ③ 明确没有发布记录。
  if (publishStatus === null) return 'FIRST_PUBLISH'

  // ④ 读不到 / 认不出 —— 安全默认。绝不在这里猜一个状态出来。
  if (publishStatus === undefined) return 'HIDDEN'

  switch (publishStatus) {
    case 'failed':
      return 'REPUBLISH'
    case 'publishing':
      return 'HIDDEN'
    case 'published':
      return 'HIDDEN'
    default:
      return 'HIDDEN'
  }
}

// ---------------------------------------------------------------------------
// 动作 → 界面
// ---------------------------------------------------------------------------

/**
 * 动作在界面上的样子。
 *
 * 把"显示什么"也做成纯数据，是为了让界面层**没有自己的判断**：组件只会照着这个
 * 对象渲染，一个 if 都不写。这样"契约里写的行为"和"屏幕上出现的东西"之间没有
 * 第二个可能出错的地方 —— 也让契约可以被穷举地测（本项目 `node --test` 不能
 * 转换 `.tsx`，组件渲染测不了，但这份视图模型就是组件渲染的全部依据）。
 */
export interface PublishActionView {
  /** 是否渲染发布按钮。false 时 `publishLabel` 必为 null。 */
  showPublish: boolean
  /** 发布按钮文案：首次发布 / 重发。 */
  publishLabel: string | null
  /** 走"重发"那套确认文案（复用同一个 ConfirmDialog，不另造一个）。 */
  isRetry: boolean
  /** 是否渲染「去裁决 →」入口。它是导航，不是发布动作。 */
  showResolve: boolean
  resolveLabel: string | null
  /**
   * 紧跟在入口旁边的一句话。`null` 表示不必解释。
   *
   * 两种用途，都不能省：
   *
   * - RESOLVE：说清楚"为什么这里只有一个去裁决的入口，没有发布按钮"。
   *   这是整段契约里最需要被读懂的一条，光给一个按钮不够。
   * - HIDDEN：一块写着「发布失败 —— 这一条可以重试」的记录下面空空如也，
   *   会让人以为界面坏了，或者以为重试被自己漏掉了。
   */
  note: string | null
  /**
   * 那句话的色调。`unknown` = 橙色 —— **只给 `publish_unknown` 用**。
   * `publishing` / 读不到 / 资格不足一律走 `neutral`：它们都不是"结果未知"，
   * 借用橙色会让几种完全不同的状态长得一样。
   */
  noteTone: 'neutral' | 'unknown'
}

export const FIRST_PUBLISH_LABEL = '首次发布'
export const REPUBLISH_LABEL = '重发'
export const RESOLVE_LABEL = '去裁决'

/**
 * 「待裁决」筛选的取值，与 `Published` 页的 URL 约定一致。
 *
 * 放在这里是因为它现在是**契约的一部分**：RESOLVE 这个动作要落到这个地址上。
 * 同一个字面量散在三处（这里、发布记录页、总览页）迟早会漂移，而漂移的表现是
 * "点了去裁决，落到一个没有筛选的列表页"——人以为自己看的是未决队列，其实是全部。
 */
export const UNRESOLVED_FILTER = '__unresolved__'

/** 待裁决视图的地址。RESOLVE 动作与总览的「立即处理」都指向它。 */
export const UNRESOLVED_PUBLISHED_PATH = `/published?status=${UNRESOLVED_FILTER}`

/**
 * 读不到发布记录时的说明。措辞直接对应 §八 的安全原则 —— 让人知道这里少一个
 * 按钮是**故意的**，不是坏了。
 */
const NOTE_UNREADABLE =
  '读不到这个 Run 的发布状态，因此这里不提供发布入口。宁可少一个按钮，也不会多发一条帖子。'

const NOTE_RUN_NOT_ELIGIBLE =
  '这个 Run 还不具备发布资格：只有审核已给出结论（已批准 / 已编辑）的 Run 才能发布。' +
  '发布记录不能凌驾于人工审核之上。'

const NOTE_PUBLISHING =
  '这次发布还在进行中。第一个请求可能已经到达 X，只是本地还没有拿到最终状态 —— ' +
  '在它落定之前，这里不提供任何发布入口。'

/**
 * RESOLVE 旁边那句。**这是整段契约里最需要被读懂的一句话**：它解释了为什么这里
 * 只有一个"去裁决"的入口，而没有像别处那样给一个发布按钮。
 *
 * 措辞沿用被替换掉的那段旧提示，一个字都没有减轻 —— 橙色块 + 这句结论是这个界面
 * 唯一一次要求人"先去 X 上看一眼"，说得含糊就等于没说。
 */
const NOTE_UNRESOLVED =
  '这个 Run 有一次结果未定的发布尝试。系统无法确认 X 是否已经创建帖子 —— ' +
  '请先到 X 上确认账号，再裁决。自动重试可能导致重复发布，这个抽屉不会代你发布。'

/**
 * HIDDEN 时那句解释。四种原因分开说，因为"这里为什么什么都没有"的答案不一样：
 *
 * - `publishing`      → 发布在飞，等它落定
 * - `published`       → 上面那块发布记录（含 X Post ID 与时间）本身就是全部答案
 * - `failed`          → 记录说"可以重试"却没有按钮，唯一原因是 Run 没资格
 * - `null`            → 明确没有记录，上面写着「没有发布记录」，不必再补
 * - 其余（读不到）     → 说清楚这是读不到，不是没有
 */
function hiddenNote(
  runStatus: string | null | undefined,
  publishStatus: string | null | undefined,
): string | null {
  if (publishStatus === 'publishing') return NOTE_PUBLISHING
  if (publishStatus === 'published') return null
  if (publishStatus === null) return null
  if (publishStatus === 'failed') {
    // 走到这里时 Run 必然没有资格 —— 有资格的话 `getPublishAction` 会返回 REPUBLISH。
    return isRunPublishEligible(runStatus) ? null : NOTE_RUN_NOT_ELIGIBLE
  }
  return NOTE_UNREADABLE
}

/**
 * 给定两个状态，返回界面该长什么样。
 *
 * 界面组件只照这个对象渲染，自己不做任何判断 —— 这样"契约规定的行为"和"屏幕上
 * 出现的东西"之间没有第二个可能出错的地方，也让契约可以被穷举地测。
 *
 * 注意它内部**调用** `getPublishAction`，不重复那套判断：动作只有一个来源。
 */
export function publishActionView(
  runStatus: string | null | undefined,
  publishStatus: string | null | undefined,
): PublishActionView & { action: PublishAction } {
  const action = getPublishAction(runStatus, publishStatus)

  if (action === 'FIRST_PUBLISH') {
    return {
      action,
      showPublish: true,
      publishLabel: FIRST_PUBLISH_LABEL,
      isRetry: false,
      showResolve: false,
      resolveLabel: null,
      note: null,
      noteTone: 'neutral',
    }
  }

  if (action === 'REPUBLISH') {
    return {
      action,
      showPublish: true,
      publishLabel: REPUBLISH_LABEL,
      isRetry: true,
      showResolve: false,
      resolveLabel: null,
      note: null,
      noteTone: 'neutral',
    }
  }

  if (action === 'RESOLVE') {
    return {
      action,
      showPublish: false,
      publishLabel: null,
      isRetry: false,
      showResolve: true,
      resolveLabel: RESOLVE_LABEL,
      // "为什么没有发布按钮"这句话必须留在按钮旁边，不能只给一个按钮。
      note: NOTE_UNRESOLVED,
      noteTone: 'unknown',
    }
  }

  return {
    action,
    showPublish: false,
    publishLabel: null,
    isRetry: false,
    showResolve: false,
    resolveLabel: null,
    note: hiddenNote(runStatus, publishStatus),
    noteTone: 'neutral',
  }
}

/**
 * 把 Run 详情里那两个**可能互相矛盾**的字段收成一个"当前有效的发布状态"。
 *
 * `AdminRunDetail` 同时给了 `publish`（记录对象，权威）与 `publish_status`（镜像）。
 * 后端里两者出自同一个 `published` 行（`app/services/admin.py:975` 与 `:1061`），
 * 所以正常情况下必然一致 —— 这个函数是给**契约漂移**准备的，不是给正常路径准备的。
 *
 * 不一致时**不猜哪一个对**，返回 `undefined`，让契约落进 HIDDEN。
 * 两个方向的风险不对称：多渲染一个按钮可能是重复推文，少渲染一个只是少点一下。
 */
export function effectivePublishStatus(
  record: { status: string } | null | undefined,
  mirrored: string | null | undefined,
): string | null | undefined {
  // 两边都说"没有记录" → 明确没有。
  if (record == null && mirrored == null) return null

  // 两边都有且一致 → 就是它。
  if (record && mirrored != null && record.status === mirrored) return record.status

  // 其余（一边有一边没有、或者两个值不一样）= 说不清。
  return undefined
}
