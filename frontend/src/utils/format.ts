/**
 * 展示层的格式化。
 *
 * 一个贯穿全文件的约定：**没有值时显示 `—`，不显示 `null` / `undefined` / `NaN`**。
 * 控制台里"没有这个值"本身就是信息（比如"这个 Run 没有发布记录"），它不该长得
 * 像渲染出错。所有 `—` 都只是空值的表示，绝不代表某个猜测出来的默认值。
 */

import type {
  AgentQualityMetrics,
  PublishStatus,
  ReviewStatus,
  RunStatus,
} from '../types/api'

export const EMPTY = '—'

export function text(value: string | null | undefined): string {
  if (value === null || value === undefined) return EMPTY
  const trimmed = value.trim()
  return trimmed ? trimmed : EMPTY
}

/** 后端返回的时间是 ISO 字符串（带时区）。按浏览器本地时区展示，秒级精度。 */
export function dateTime(value: string | null | undefined): string {
  if (!value) return EMPTY
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return EMPTY
  return parsed.toLocaleString(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function dateOnly(value: string | null | undefined): string {
  if (!value) return EMPTY
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return EMPTY
  return parsed.toLocaleDateString()
}

/** 趋势图横轴的短日期：`09-25`。整年显示会把刻度挤没。 */
export function shortDate(value: string): string {
  const parsed = new Date(`${value}T00:00:00Z`)
  if (Number.isNaN(parsed.getTime())) return value
  return `${String(parsed.getUTCMonth() + 1).padStart(2, '0')}-${String(parsed.getUTCDate()).padStart(2, '0')}`
}

/**
 * 相对时间（"3 分钟前"），用于队列里判断"等了多久"。
 *
 * 只做粗粒度：队列关心的是量级（几分钟 / 几小时 / 几天），不是秒。绝对时间仍然
 * 由调用方在 `title` 上给出——相对时间会随渲染时间漂移，不能作为唯一的时间表述。
 *
 * 时钟回拨或服务端时间略快时会算出负数，一律收敛成 `刚刚`，不显示"未来时间"。
 */
export function relativeTime(value: string | null | undefined, now = Date.now()): string {
  if (!value) return EMPTY
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return EMPTY

  const seconds = Math.floor((now - parsed.getTime()) / 1000)
  if (seconds < 60) return '刚刚'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} 天前`
  const months = Math.floor(days / 30)
  if (months < 12) return `${months} 个月前`
  return `${Math.floor(months / 12)} 年前`
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return EMPTY
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  if (minutes < 60) return `${minutes} 分 ${rest} 秒`
  const hours = Math.floor(minutes / 60)
  return `${hours} 小时 ${minutes % 60} 分`
}

/**
 * 状态 → 中文展示文案。
 *
 * **后端的状态值一个都不改**，仍然是英文枚举（`waiting_for_review` 等），
 * 中文只存在于这一层。三个枚举分开映射，理由见 `StatusBadge` 的说明：
 * `agent_run.status` 与 `published_post.status` 有四个**同名**的值，含义却不同，
 * 所以 `failed` 在运行状态下是"失败"、在发布状态下是"发布失败"。
 *
 * 唯一的例外是 `completed`：Phase 4b.1 的中文词典没有覆盖它，但它是真实的
 * Run 状态（`types/api.ts` 的 `RUN_STATUSES` 里有），漏掉会让筛选下拉里冒出
 * 一个英文项，所以补成"已完成"。
 */
const RUN_STATUS_LABELS: Record<RunStatus, string> = {
  running: '运行中',
  waiting_for_review: '待审核',
  approved: '已批准',
  edited: '已编辑',
  rejected: '已拒绝',
  failed: '失败',
  completed: '已完成',
  publishing: '发布中',
  publish_unknown: '发布结果未知',
  published: '已发布',
}

/**
 * 审核决定。`other` 不是后端的状态值，是 Analytics 为了"四个数加起来等于总数"
 * 而设的兜底桶（见 `app/services/admin.py` 的 `analytics()`），一并在这里给中文。
 */
const REVIEW_STATUS_LABELS: Record<ReviewStatus | 'other', string> = {
  approve: '批准',
  edit: '编辑',
  reject: '拒绝',
  pending: '待审核',
  other: '其他',
}

const PUBLISH_STATUS_LABELS: Record<PublishStatus, string> = {
  published: '已发布',
  failed: '发布失败',
  publishing: '发布中',
  publish_unknown: '发布结果未知',
}

/**
 * `draft.status`。
 *
 * 后端只在一处写入这个值——`app/repositories.py` 建草稿时给的 `"draft"`，
 * 没有任何地方把它推进到别的状态（已核对源码，并查过库里现存 5 行，全部是 `draft`）。
 * 这里仍然做成映射表而不是写死"草稿"两个字：后端哪天真的加了状态，落在这个
 * 表格之外的会按原样显示，是个看得见的缺口，不会静默变成错误的中文。
 */
const DRAFT_STATUS_LABELS: Record<string, string> = {
  draft: '草稿',
}

/**
 * 状态 → 展示文案。查不到就原样返回原值（大写）。
 *
 * 保留原值而不是显示"未知"：后端加了新状态而前端还没跟上时，使用者至少能看到
 * 真实的状态字符串，而不是一个把信息抹掉的占位符。这条对中文化尤其重要——
 * 一个没翻译的英文枚举是**看得见的缺口**，一句"未知"则会把缺口藏起来。
 */
export function runStatusLabel(status: string | null | undefined): string {
  if (!status) return EMPTY
  return RUN_STATUS_LABELS[status as RunStatus] ?? status.toUpperCase()
}

export function reviewStatusLabel(status: string | null | undefined): string {
  if (!status) return EMPTY
  const label = REVIEW_STATUS_LABELS[status as ReviewStatus]
  return label ?? status.toUpperCase()
}

export function draftStatusLabel(status: string | null | undefined): string {
  if (!status) return EMPTY
  return DRAFT_STATUS_LABELS[status] ?? status
}

export function publishStatusLabel(status: string | null | undefined): string {
  if (!status) return EMPTY
  return PUBLISH_STATUS_LABELS[status as PublishStatus] ?? status.toUpperCase()
}

/**
 * Agent 的动作名（`AgentAction` 枚举值）→ 中文。
 *
 * 与状态映射同一套规矩：查不到就原样返回英文枚举。Agent 加了新动作而前端还没跟上时，
 * 时间线上会出现一个没翻译的英文词——那是**看得见的缺口**，比一句"未知步骤"有用。
 *
 * Phase 5B.1 有两处调整，都不改后端枚举，只是让中文名跟得上动作的**实际含义**：
 *
 * - `discover_topics` 从"发现候选"改成"发现并评估话题"。它现在不只列候选，
 *   还会给每个候选算出确定性分数并给出达标判定（`app/agent/trends.py`）。
 *   沿用旧名会让人以为这一步只是收集，看不出分数是这里产生的。
 * - 新增 `select_topic`。**它不调 LLM**，是在已打分的候选里按确定性排序选定一个；
 *   与 `select_angle`（选观点）是**两个不同的判断**，所以中文名必须能分开。
 */
const AGENT_ACTION_LABELS: Record<string, string> = {
  discover_topics: '发现并评估话题',
  select_topic: '选定话题',
  research_topic: '研究主题',
  deepen_research: '深入研究',
  extract_evidence: '提炼证据',
  generate_angles: '生成观点角度',
  select_angle: '选择观点角度',
  generate_candidates: '生成候选',
  criticize: '内容评估',
  revise: '修改内容',
  finish: '完成',
}

export function agentActionLabel(action: string | null | undefined): string {
  if (!action) return EMPTY
  return AGENT_ACTION_LABELS[action] ?? action
}

/**
 * 把 Critic 的三种结论压成一行："评了 7 条：通过 3 / 要求改稿 3 / 否决 1"。
 *
 * 三种计数**必须一直显示**，包括 0——"这次一条都没否决"和"这次压根没评过"
 * 是两件完全不同的事，把 0 省掉就分不出来了（总数 0 时才是真的没评过）。
 */
export function criticOutcomeSummary(quality: AgentQualityMetrics): string {
  const accept = quality.critic_accept_count
  const revise = quality.critic_revise_count
  const reject = quality.critic_reject_count
  const total = accept + revise + reject
  if (total === 0) return '这次运行没有产生评审结论'
  return `评了 ${total} 条：通过 ${accept} / 要求改稿 ${revise} / 否决 ${reject}`
}

/**
 * 按**码点**计数，而不是 JS 默认的 UTF-16 码元。
 *
 * `'😀'.length === 2`，因为它在 JS 里是一对代理项；而后端的 `len('😀') === 1`。
 * 用 `.length` 去对照后端下发的内容长度上限，含 emoji 的正文会提前几个字就被标红，
 * 而计数存在的唯一意义就是如实预告后端会不会打回。
 */
export function codePointLength(value: string): number {
  return Array.from(value).length
}

/** 把可能的 null 压成空串，供搜索框等受控输入使用。 */
export function orEmpty(value: string | null | undefined): string {
  return value ?? ''
}

/**
 * 一条研究素材的三个时间，**各显示各的，一个都不顶替另一个**。
 *
 * 后端把三者的语义分得很清（见 `app/schemas.py::ResearchItemRecord`）：
 *
 *   - `published_at` 发布——原帖在 X 上的发布时间
 *   - `observed_at`  抓取——我们从 X API 取回它的时刻（Phase 5B.1 新增）
 *   - `created_at`   入库——这行写进本地库的时刻
 *
 * 这三个数值在正常路径上常常只差几秒，所以很容易被"反正差不多"合并成一个。
 * 但它们回答的是不同的问题：一条三小时前发布的帖子如果是一分钟前才抓到的，
 * 那么"这个话题现在还有人在说"这个判断依据的是三小时前的数据——**只看入库时间
 * 会把它误读成刚刚发生的事**。缺失的那个显示 `—`，不拿另一个顶上。
 *
 * 全部返回字符串而不是数组：调用方要的就是一行字，中间用什么分隔是这里的事。
 */
export function researchItemTimes(item: {
  published_at: string | null
  observed_at: string | null
  created_at: string | null
}): string {
  return [
    `发布 ${dateTime(item.published_at)}`,
    `抓取 ${dateTime(item.observed_at)}`,
    `入库 ${dateTime(item.created_at)}`,
  ].join(' · ')
}

/**
 * 列表"主题"列显示什么。
 *
 * Agent 模式（Phase 5A）的 Run 在选出主题之前 `topic` 是 null，而 `goal` 是
 * 人给的那句话。这里**不把 goal 当主题**：返回的字符串带"目标："前缀，是
 * 为了让屏幕上永远能看出"这一格显示的不是主题，是它还没选主题时的输入"。
 *
 * 不带前缀地直接显示 goal 会让人以为那就是 Agent 选定的题目——一个还在选题的
 * Run 和一个已经定了题的 Run 在列表里长得一样，是比空白更糟的误导。
 *
 * 两者都没有时返回 null，由调用方决定显示 `—` 还是别的。
 */
export function runTitle(row: { topic: string | null; goal: string | null }): string | null {
  if (row.topic) return row.topic
  if (row.goal) return `目标：${row.goal}`
  return null
}

/**
 * 长 ID 的短形式：只取前 8 位。
 *
 * 用于列表里"能一眼区分两条记录、又不撑爆列宽"的场合。**不能拿它去查询**——
 * 它只是显示形式，完整值始终在 `title` 属性和详情里。ID 短于 8 位时原样返回，
 * 免得把别的东西（比如一个数字 ID）截成看起来正常其实残缺的样子。
 */
export function shortId(value: string | null | undefined): string {
  if (!value) return EMPTY
  return value.length <= 8 ? value : value.slice(0, 8)
}

/**
 * 错误的抬头文案：`status === 0` 表示请求根本没发出去（网络层失败），
 * 不是某个 HTTP 状态码。
 *
 * 收在这里是因为四处错误块原先各写了一遍同样的三元表达式，中文化时若漏掉一处，
 * 界面上就会同时出现"网络错误"和"network error"两种说法。
 */
export function httpStatusLabel(status: number): string {
  return status === 0 ? '网络错误' : `HTTP ${status}`
}

/**
 * 后端错误详情 → 中文。**后端一个字节都没改**，这是前端认得出的那几条的对照表。
 *
 * 为什么只做"认得出"的：后端 `detail` 是纯文本，没有错误码可依赖，只能按字面匹配。
 * 匹配不上的**原样显示**（见 `backendErrorLabel` 的返回值约定），而不是塞一句
 * "发生了未知错误"——后者会把后端真正说的话盖掉，那才是更难查的问题。
 *
 * 这几条值得翻，是因为它们不是理论上的错误：409 那两条正好是"这个 Run 已经被
 * 别处决定过了"（另一个标签页、或同一个人点了两次），是唯一会在正常使用中撞上的
 * 英文。后端里同样的文案共 5 处（`app/main.py` 4 处 `run not found` + `app/api/admin.py` 1 处）。
 */
const BACKEND_ERROR_LABELS: Record<string, string> = {
  'run not found': '找不到这个 Run。',
  'run review was already submitted': '这个 Run 的审核已经由别处提交过了。',
}

/** 带状态值的那条：`run is not waiting for review (status=approved)`。 */
const NOT_WAITING_FOR_REVIEW = /^run is not waiting for review \(status=([A-Za-z_]+)\)$/

/**
 * 已知的后端错误 → 中文；**不认识的一律返回 `null`**。
 *
 * 返回 `null` 而不是兜底文案，是为了让调用方能把后端原文照旧显示出来。
 */
export function backendErrorLabel(message: string): string | null {
  const trimmed = message.trim()
  const known = BACKEND_ERROR_LABELS[trimmed]
  if (known) return known

  const match = NOT_WAITING_FOR_REVIEW.exec(trimmed)
  if (match) {
    // 状态值本身仍按后端枚举渲染成中文（approved → 已批准），和界面上别处的说法一致。
    const status = match[1]
    return `这个 Run 已经不在等待审核了（当前状态：${status ? runStatusLabel(status) : EMPTY}）。`
  }
  return null
}
