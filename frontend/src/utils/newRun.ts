/**
 * 「新建 Run」的输入契约。
 *
 * 这个模块存在的理由和 `utils/queue.ts`、`utils/publishAction.ts` 一样：界面层
 * 不该有自己的判断。组件只照着这里返回的对象渲染，"契约规定的行为"与"屏幕上
 * 出现的东西"之间因此没有第二个可能出错的地方。
 *
 * **无依赖、无副作用**（不读 window / localStorage / 时钟，不调 API），所以
 * `node --test` 可以直接跑它——本项目不能转换 `.tsx`，组件渲染测不了，而这份
 * 纯逻辑就是组件渲染的全部依据。
 *
 * ## 三件事，一个都不多
 *
 * 1. **怎么说清"人给的是题目还是任务"**。这不是同一个输入框换个标签：
 *    `topic` 是"我已经决定写什么"，链路是 research → draft；`goal` 是"我今天想
 *    关注什么"，链路是 planner → discover → 自己选题。说不清这一点的界面，
 *    用户会以为自己在改一条已有 Run 的题目。
 * 2. **提交什么**。恰好一个字段，且**不可能是空串**（见 `toRunPayload`）。
 * 3. **创建完之后说什么**。成功与"Run 建好了但跑失败"是两件事，后者必须说成
 *    失败（见 `createRunFailureMessage`）——这个系统里最不能承受的错误就是
 *    让人以为一次没有产出内容的运行成功了。
 *
 * ## 刻意不做的事
 *
 * 没有"修改当前 Run 的题目"这条路径。换题目**不是** PATCH 当前那条 Run，
 * 而是新建一条：`Run A(topic=AI)` 与 `Run B(topic=美股)` 各自独立，A 的
 * checkpoint / 草稿 / 材料不会被 B 动到（后端由 `thread_id == run_id` 保证，
 * 前端只要不提供 patch 入口就不会破坏它）。
 */

/** 两种运行方式。顺序即界面上从左到右的顺序（默认选中第一个）。 */
export const RUN_MODES = ['topic', 'goal'] as const

export type RunMode = (typeof RUN_MODES)[number]

/**
 * 默认方式：指定一个题目。
 *
 * 选它而不是"让 Agent 自己选题"，是因为它**更可预期**：用户点进来时多数已经
 * 知道要写什么，而 Agent 选题要跑完 discovery 才知道题是什么。相对更重的那个
 * 方式应该是明确的选择，不是默认。
 */
export const DEFAULT_RUN_MODE: RunMode = 'topic'

export const RUN_MODE_LABELS: Record<RunMode, string> = {
  topic: '指定一个题目',
  goal: 'Agent 自动选题',
}

/**
 * 每种方式下输入框上方那句话——它要回答的是"我该在这里写什么"，
 * 所以两种方式的问法**必须不一样**。
 */
const QUESTIONS: Record<RunMode, string> = {
  topic: '你想写什么？',
  goal: '告诉 Agent 今天想关注什么',
}

/** 例子比说明有效：两种方式给的例子就是两条链路各自真实的输入形状。 */
const PLACEHOLDERS: Record<RunMode, string> = {
  topic: '例如：英伟达最近为什么上涨？',
  goal: '例如：今天关注 AI，找一个最近最值得讨论的话题',
}

export interface RunForm {
  mode: RunMode
  text: string
}

/**
 * 提交给 `POST /api/runs` 的请求体。
 *
 * 判别联合而不是 `{topic?: string; goal?: string}`：这两个字段**恰好**给一个，
 * 多给一个或少给一个后端都会 422（`app/schemas.py::RunRequest`），
 * 而用可选字段描述它，等于把那个 422 从编译期推到运行期。
 */
export type RunPayload = { topic: string } | { goal: string }

/**
 * 输入 → 请求体。**空（或只有空白）返回 `null`**，调用方据此禁用提交。
 *
 * 三处刻意的决定：
 *
 * - **空白串不算"填了"**。`{"topic": "   "}` 后端会 422（`RunRequest` 里那条
 *   空白校验），但前端不该把一次注定失败的请求发出去——错误提示应当发生在
 *   输入框旁边，而不是绕一圈从服务端回来。
 * - **提交前 `trim()`**。前后空格对检索没有任何意义，而它会原样进 X 的 query。
 *   注意这与后端"不 trim、输入什么就存什么"**不冲突**：那是 API 的契约
 *   （调用方给什么就存什么），这里是本界面自己决定不制造这种输入。
 * - **不跨字段**：`topic` 模式下返回的对象里**没有 `goal` 这个键**，反之亦然。
 *   两个都带上（哪怕一个是空串）会命中后端那条"有歧义就拒收"的规则。
 */
export function toRunPayload(form: RunForm): RunPayload | null {
  const value = form.text.trim()
  if (!value) return null
  return form.mode === 'goal' ? { goal: value } : { topic: value }
}

/**
 * 弹窗的表面。组件照着它渲染，一个 if 都不写。
 *
 * `canSubmit` 与 `payload !== null` 是同一件事的两个说法：**按钮能不能按，与
 * "按下去会发出什么"必须由同一个判断决定**，否则会出现"按钮亮着但发不出去"。
 */
export interface NewRunView {
  mode: RunMode
  /** 输入框上方那句话：你想写什么？ / 告诉 Agent 今天想关注什么。 */
  question: string
  placeholder: string
  canSubmit: boolean
  /** `canSubmit` 为 false 时是 null。 */
  payload: RunPayload | null
}

export function newRunView(form: RunForm): NewRunView {
  const payload = toRunPayload(form)
  return {
    mode: form.mode,
    question: QUESTIONS[form.mode],
    placeholder: PLACEHOLDERS[form.mode],
    canSubmit: payload !== null,
    payload,
  }
}

/** 成功提示里那行字。`title` 固定，`detail` 说清"建的是什么"。 */
export interface CreatedRunNotice {
  title: string
  detail: string
}

/**
 * 提示里最多显示多少个字。
 *
 * 目标可以写成一大段（"今天关注 AI，顺便看看……"），原样塞进提示条会变成一个
 * 遮住半个屏幕的方块。截断只影响**提示**——完整内容在输入框和运行记录里。
 */
const NOTICE_MAX_CHARS = 60

function summarize(value: string): string {
  const chars = Array.from(value)
  return chars.length <= NOTICE_MAX_CHARS ? value : `${chars.slice(0, NOTICE_MAX_CHARS).join('')}…`
}

/**
 * 创建成功的提示。
 *
 * 两种方式的话术必须分开：`goal` 模式下 Agent 还要自己选题，此时**不能**说
 * "已创建 Run：今天关注 AI…"，那会让人以为题目就是这句话（运行记录列表里
 * 那句前面也带着"目标："前缀，同一个理由，见 `utils/format.ts::runTitle`）。
 */
export function createdRunNotice(payload: RunPayload): CreatedRunNotice {
  if ('goal' in payload) {
    return {
      title: '已创建 Run',
      detail: `目标：${summarize(payload.goal)}。题目由 Agent 自己发现。`,
    }
  }
  return { title: '已创建 Run', detail: `题目：${summarize(payload.topic)}` }
}

/**
 * 失败信息需要的最小形状。
 *
 * 写成结构类型而不是 `ApiError`，是为了让这个模块**一个 import 都不需要**
 * （见文件头）。`ApiError` 恰好满足它。
 */
export interface CreateRunFailure {
  status: number
  message: string
  raw: unknown
}

/**
 * 后端写给**这次运行**的那句失败原因；不是这种形状时返回 null。
 *
 * 只在 `run_id` 与 `error` **同时**是字符串时才认：那是 `_failure_response`
 * 的形状（Run 行已经建好了）。只认其中一个会把别的错误体也当成它。
 */
function runFailureReason(raw: unknown): string | null {
  if (!raw || typeof raw !== 'object') return null
  const body = raw as { run_id?: unknown; error?: unknown }
  if (typeof body.run_id !== 'string' || typeof body.error !== 'string') return null
  const reason = body.error.trim()
  return reason || null
}

/**
 * 创建失败时给人看的那句话。
 *
 * 这里要分开两种失败，因为**它们对用户意味着完全不同的下一步**：
 *
 * - `Run` 已经建好、只是这次运行跑失败了（5xx + `{run_id, status:"failed",
 *   error}`）：必须说出来"它已经在运行记录里了"，否则人会以为什么都没发生，
 *   再点一次——于是库里多一条同样失败的 Run。原因用 **后端原文**（脱敏过的
 *   `str(exc)`）：通用兜底只会显示"请求失败：HTTP 502。"，把真正的原因
 *   （哪一步炸了）吞掉。
 * - 其余（请求根本没被接受、服务没起来）：原因就是 `ApiError.message`
 *   那句话本身——它是客户端从响应体里挑出来的，已经能看了。
 *
 * **绝不返回"成功"之类的字眼**：这个对话框上的任何一句话都不许让人以为
 * 一次没有产出内容的运行成功了。
 */
export function createRunFailureMessage(error: CreateRunFailure): string {
  const reason = runFailureReason(error.raw)
  if (reason === null) return error.message
  return `Run 已经建好了，但这次运行失败了：${reason}（它已经在运行记录里，可以打开查看，或再发起一次）`
}
