import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import { IconAlert, IconCheck, IconClose, IconInfo, IconXCircle } from './icons'

/**
 * 统一的提示条（Toast）。
 *
 * 在这之前，"操作成功了"这件事有四种写法：Review 页一段绿色 notice、发布结果一块
 * 内嵌区块、抽屉里各写各的、其余地方干脆没有反馈。同一个动作在不同页面长得不一样，
 * 人就不知道该往哪看。
 *
 * 三条刻意的设计：
 *
 * 1. **成功提示会自己走，失败提示不会。** 5000ms vs 8000ms，而且失败的那条
 *    语气更重。成功是"知道了，继续"，失败是"你得看一眼"——用同一个停留时间
 *    等于把后者也当成噪声。
 * 2. **Toast 只做"结果通知"，不做"唯一信息源"。** 发布结果、审核错误这类
 *    **必须能反复读**的东西，仍然留在页面里（`PublishOutcomeNotice`、
 *    表单下方的错误块）。Toast 到点就消失，把关键结论只放在它会消失的地方
 *    是错的。
 * 3. **不引入第三方 toast 库。** 这里要的只是"一个队列 + 一个计时器"。
 */
export type ToastTone = 'ok' | 'danger' | 'warn' | 'info'

interface ToastItem {
  id: number
  tone: ToastTone
  title: string
  detail?: ReactNode
}

interface ToastApi {
  push: (tone: ToastTone, title: string, detail?: ReactNode) => void
  success: (title: string, detail?: ReactNode) => void
  /** 失败。停留时间更长，且用 assertive 播报。 */
  error: (title: string, detail?: ReactNode) => void
  warning: (title: string, detail?: ReactNode) => void
  info: (title: string, detail?: ReactNode) => void
}

const ToastContext = createContext<ToastApi | null>(null)

/** 停留时间。失败的那条留得更久——它更需要被读到。 */
const TTL: Record<ToastTone, number> = {
  ok: 5000,
  info: 5000,
  warn: 8000,
  danger: 8000,
}

const ICONS: Record<ToastTone, ReactNode> = {
  ok: <IconCheck size={16} />,
  danger: <IconXCircle size={16} />,
  warn: <IconAlert size={16} />,
  info: <IconInfo size={16} />,
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const nextId = useRef(1)
  // 计时器按 id 存。卸载或手动关闭时要清掉，否则会对已经不存在的条目 setState。
  const timers = useRef(new Map<number, number>())

  const dismiss = useCallback((id: number) => {
    const timer = timers.current.get(id)
    if (timer !== undefined) {
      window.clearTimeout(timer)
      timers.current.delete(id)
    }
    setItems((current) => current.filter((item) => item.id !== id))
  }, [])

  const push = useCallback<ToastApi['push']>(
    (tone, title, detail) => {
      const id = nextId.current++
      setItems((current) => [...current, { id, tone, title, detail }])
      timers.current.set(
        id,
        window.setTimeout(() => dismiss(id), TTL[tone]),
      )
    },
    [dismiss],
  )

  // 卸载时清理所有计时器。App 级组件通常活到页面关闭，但热更新会真的卸载它。
  useEffect(() => {
    const pending = timers.current
    return () => {
      for (const timer of pending.values()) window.clearTimeout(timer)
      pending.clear()
    }
  }, [])

  const api = useMemo<ToastApi>(
    () => ({
      push,
      success: (title, detail) => push('ok', title, detail),
      error: (title, detail) => push('danger', title, detail),
      warning: (title, detail) => push('warn', title, detail),
      info: (title, detail) => push('info', title, detail),
    }),
    [push],
  )

  return (
    <ToastContext.Provider value={api}>
      {children}
      <ToastViewport items={items} onDismiss={dismiss} />
    </ToastContext.Provider>
  )
}

function ToastViewport({
  items,
  onDismiss,
}: {
  items: ToastItem[]
  onDismiss: (id: number) => void
}) {
  return (
    <div className="toast-viewport">
      {/*
        播报策略按严重程度分开：
          失败（danger）用 role="alert"（assertive）——它会打断当前的朗读；
          其余用 aria-live="polite"，等用户停下来再说。
        只用 polite 的话，一条"发布失败"可能被念在几段无关内容之后。
        容器始终存在（即使没有条目）——动态插入的 live region 有时不会被播报。
      */}
      <div className="toast-viewport__group" aria-live="polite" aria-atomic="false">
        {items
          .filter((item) => item.tone !== 'danger')
          .map((item) => (
            <ToastRow key={item.id} item={item} onDismiss={onDismiss} />
          ))}
      </div>
      <div className="toast-viewport__group" aria-live="assertive" aria-atomic="false">
        {items
          .filter((item) => item.tone === 'danger')
          .map((item) => (
            <ToastRow key={item.id} item={item} onDismiss={onDismiss} />
          ))}
      </div>
    </div>
  )
}

function ToastRow({ item, onDismiss }: { item: ToastItem; onDismiss: (id: number) => void }) {
  return (
    <div className={`toast toast--${item.tone}`}>
      <span className="toast__icon">{ICONS[item.tone]}</span>
      <div className="toast__body">
        <div className="toast__title">{item.title}</div>
        {item.detail ? <div className="toast__detail">{item.detail}</div> : null}
      </div>
      <button
        type="button"
        className="icon-btn"
        onClick={() => onDismiss(item.id)}
        aria-label="关闭提示"
      >
        <IconClose size={14} />
      </button>
    </div>
  )
}

export function useToast(): ToastApi {
  const value = useContext(ToastContext)
  if (value === null) {
    throw new Error('useToast must be used inside <ToastProvider>')
  }
  return value
}
