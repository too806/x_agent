import { useId, useRef, useState } from 'react'

import { ApiError, api } from '../api/client'
import { useConsole } from '../hooks/useConsole'
import { useMutation } from '../hooks/useMutation'
import { httpStatusLabel } from '../utils/format'
import {
  DEFAULT_RUN_MODE,
  RUN_MODES,
  RUN_MODE_LABELS,
  createRunFailureMessage,
  createdRunNotice,
  newRunView,
} from '../utils/newRun'
import { ConfirmDialog } from './ConfirmDialog'
import { useToast } from './ui/Toast'
import type { RunMode, RunPayload } from '../utils/newRun'

interface Props {
  onClose: () => void
  /** 创建成功之后：把新 Run 交给页面去聚焦（刷新列表、打开它的详情）。 */
  onCreated: (runId: string) => void
}

/**
 * 新建 Run。
 *
 * **这不是"修改当前 Run 的题目"。** 换主题的方式是再建一条新的 Run：
 * 当前这条的 topic、草稿、材料、checkpoint 一个都不会被碰到。所以这里
 * 没有"当前值"、没有预填、没有 PATCH —— 每次打开都是一次空白的新建。
 *
 * 三个复用，没有一样是新造的：
 *
 * 1. **`ConfirmDialog`** —— 对话框骨架（焦点陷阱、Esc、错误块的位置）与发布/裁决
 *    那三个对话框完全同一套。**唯一的差别**是这里传了 `dismissibleWhileBusy`：
 *    那条"busy 时不许关"的规则是为不可撤销的操作写的（关掉会让人以为没发），
 *    而新建 Run 是相反的情形——请求要跑几分钟，关掉窗口不会取消它，人应当能走开。
 * 2. **`useMutation`** —— busy / error 的写法与别处的写操作一致。
 * 3. **`POST /api/runs`** —— 既有端点。**这条链路是同步的**：请求返回时运行
 *    已经跑到人工审核了，所以创建之后**没有第二步**，不要在这里追加任何
 *    "触发执行"的调用。
 *
 * 判断全在 `utils/newRun.ts` 里（纯函数，可被 `node --test` 覆盖），
 * 这个组件只负责渲染和把结果交出去。
 */
export function NewRunDialog({ onClose, onCreated }: Props) {
  const [mode, setMode] = useState<RunMode>(DEFAULT_RUN_MODE)
  const [text, setText] = useState('')
  const inputId = useId()
  const inputRef = useRef<HTMLInputElement | null>(null)

  // 失败原因同时留一份在这里。对话框可以在请求在途时被关掉（见下面的
  // `dismissibleWhileBusy`），那时组件已经卸载，`create.error` 跟着没了——而这次
  // 失败**不能就这么消失**，所以由动作本身把错误抄一份出来，交给 toast。
  const createFailure = useRef<ApiError | null>(null)
  const create = useMutation(async (payload: RunPayload) => {
    try {
      return await api.createRun(payload)
    } catch (cause) {
      createFailure.current =
        cause instanceof ApiError ? cause : new ApiError(0, '提交时发生未预期的错误。')
      throw cause
    }
  })
  const toast = useToast()
  const { refresh } = useConsole()

  // 用户是自己关掉窗口走开的，还是让流程把他带走的。前者意味着失败原因没有地方
  // 显示，得改用 toast。
  const dismissed = useRef(false)
  function close() {
    dismissed.current = true
    onClose()
  }

  const view = newRunView({ mode, text })

  async function submit() {
    const payload = view.payload
    // 按钮已经据此禁用了，这里再挡一次：`confirmDisabled` 与"真的会发出什么"
    // 必须由同一个判断决定，不能有第二处可能对不齐的地方。
    if (!payload) return

    const created = await create.run(payload)
    // 失败：弹窗留着、输入留着，原因显示在下面。**不关闭、不清空**——关掉等于
    // 把用户刚写的那句话弄丢了，而这正是他要重试的东西。
    if (!created) {
      // 但窗口已经被关掉时，那个位置不存在了：请求跑了几分钟，人早走开了。
      // 这时改用 toast 把结果送出去，否则"没建成"和"没见过这个窗口"没区别。
      const failure = createFailure.current
      if (dismissed.current && failure) {
        toast.error(httpStatusLabel(failure.status), createRunFailureMessage(failure))
      }
      return
    }

    // 新 Run 会立刻进入待审核队列，导航上的数字必须跟着变（与发布、审核同一套
    // 做法：写操作成功后刷一次总览）。
    refresh()
    const notice = createdRunNotice(payload)
    toast.success(notice.title, notice.detail)
    onCreated(created.run_id)
  }

  const failure = create.error

  return (
    <ConfirmDialog
      title="新建 Run"
      confirmLabel="开始运行"
      busy={create.busy}
      // 这条请求是同步的：它要等到整次运行跑到人工审核才返回，可能几分钟。
      // 对话框的默认规则是 busy 时不许关（发布 / 裁决需要那条规则），但把用户
      // 锁在一个要等几分钟的窗口里没有道理——关掉它不会取消已经开始的运行。
      dismissibleWhileBusy
      cancelLabel={create.busy ? '关闭' : '取消'}
      confirmDisabled={!view.canSubmit}
      // 这个窗口的唯一用途就是让人**写一句话**，所以焦点要落在输入框上，而不是
      // 沿用 `ConfirmDialog` 默认的取消键——后者是给"确认发帖"那类对话框准备的。
      initialFocusRef={inputRef}
      onCancel={close}
      onConfirm={submit}
    >
      {/* `.stack` 是仓库里的纵向排布工具类（见 app.css 的"通用排布"）：
          这一段不需要任何新的样式。 */}
      <div className="stack">
        <p className="modal-lede">你想怎么开始？</p>

        {/* 两种方式用一个 segmented 控件表达，和列表页的"舒适 / 紧凑"是同一个
            组件、同一套样式。用按钮 + aria-pressed 而不是 radio：项目里没有
            radio 的样式，而这个控件只需要表达"二选一"。 */}
        <div className="segmented" role="group" aria-label="运行方式">
          {RUN_MODES.map((value) => (
            <button
              key={value}
              type="button"
              className={value === mode ? 'segmented__item is-active' : 'segmented__item'}
              aria-pressed={value === mode}
              disabled={create.busy}
              onClick={() => setMode(value)}
            >
              {RUN_MODE_LABELS[value]}
            </button>
          ))}
        </div>

        <div className="field">
          <label className="field__label" htmlFor={inputId}>
            {view.question}
          </label>
          <input
            id={inputId}
            ref={inputRef}
            className="input"
            type="text"
            value={text}
            placeholder={view.placeholder}
            autoComplete="off"
            disabled={create.busy}
            onChange={(event) => setText(event.target.value)}
            // Enter 直接开始。高频操作不该要求人去够鼠标。没有用 `<form>`：
            // 表单要能按 Enter 提交就得放一个 submit 按钮，而那个按钮会被
            // ConfirmDialog 的焦点陷阱当成一个可 Tab 到的位置——一个看不见的
            // Tab 停靠点比少按一次 Enter 更糟。
            onKeyDown={(event) => {
              if (event.key !== 'Enter') return
              event.preventDefault()
              void submit()
            }}
          />
        </div>

        <p className="modal-note cell-muted">
          点「开始运行」后会立刻跑这一次运行（Agent 会自动选题、检索、写稿，可能几分钟），
          跑完进入人工审核。跑的过程中可以随时关掉这个窗口——关掉不会取消已经开始的运行，
          跑完会自动打开它的详情。
        </p>
      </div>

      {/* 用自己的错误块而不是 `ConfirmDialog` 的 `error` 槽：那条槽只显示
          `ApiError.message`，而"Run 建好了但跑失败"这种 5xx 的原因在响应体的
          `error` 字段里（见 `utils/newRun.ts::createRunFailureMessage`）。
          用 .notice / .modal__error 这两个既有类，位置与观感和别处一致。 */}
      {failure ? (
        <div className="notice notice--danger modal__error" role="alert">
          <div className="notice__title">{httpStatusLabel(failure.status)}</div>
          <p>{createRunFailureMessage(failure)}</p>
        </div>
      ) : null}
    </ConfirmDialog>
  )
}
