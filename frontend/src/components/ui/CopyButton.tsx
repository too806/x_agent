import { useEffect, useRef, useState } from 'react'

import { IconCheck, IconCopy } from './icons'

/**
 * 复制按钮。
 *
 * 存在的理由是这个控制台里有两串**人必须原样抄走**的 ID：Run ID（拿去查日志、
 * 对 API）和 X Post ID（"标记为已发布"时要贴回去，抄错一位就记到一条不存在的
 * 帖子上了）。让用户用鼠标去选一串 36 字符的 UUID 是没道理的。
 *
 * 复制成功后在原地给一次反馈，**不弹 Toast**：复制是极轻的动作，为它弹一个会
 * 自己消失的提示条，比它本身还重。反馈 1.5 秒后自己退回。
 */
export function CopyButton({
  value,
  label,
  title,
}: {
  /** 要写入剪贴板的原文。 */
  value: string
  /** 无障碍名（"复制 Run ID"）。纯图标按钮必须给。 */
  label: string
  title?: string
}) {
  const [copied, setCopied] = useState(false)
  const timer = useRef<number | null>(null)

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current)
    },
    [],
  )

  async function copy() {
    const ok = await writeClipboard(value)
    if (!ok) return
    setCopied(true)
    if (timer.current !== null) window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setCopied(false), 1500)
  }

  return (
    <button
      type="button"
      className="icon-btn"
      onClick={() => void copy()}
      aria-label={copied ? `${label}（已复制）` : label}
      title={copied ? '已复制' : (title ?? label)}
    >
      {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
      {/* 视觉上的变化对屏幕阅读器不可见，所以另外播报一次。 */}
      <span className="sr-only" role="status">
        {copied ? '已复制到剪贴板' : ''}
      </span>
    </button>
  )
}

/**
 * 写剪贴板。
 *
 * 优先用异步 Clipboard API；它在 `127.0.0.1` / HTTPS 下可用，但**不是所有环境都可用**
 * （非安全上下文、iframe、旧浏览器）。失败时退回 `execCommand`，两条路都不行就
 * 安静地返回 false —— 界面保持原样，不弹错误：复制失败不是需要打断人的事情，
 * 用户可以自己选中文本。
 */
async function writeClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 落到下面的兜底路径
    }
  }

  try {
    const area = document.createElement('textarea')
    area.value = text
    area.setAttribute('readonly', '')
    // 固定在视口内但不可见：`display:none` 的节点选不中，会复制失败。
    area.style.position = 'fixed'
    area.style.top = '0'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(area)
    return ok
  } catch {
    return false
  }
}
