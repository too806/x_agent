import { EMPTY, publishStatusLabel, reviewStatusLabel, runStatusLabel } from '../utils/format'

type Kind = 'run' | 'review' | 'publish'

interface Props {
  status: string | null | undefined
  kind: Kind
  /** 覆盖显示文案，用于队列里的顺带展示（例如把 publish_unknown 标成 "UNRESOLVED"）。 */
  label?: string
}

function labelFor(kind: Kind, status: string): string {
  if (kind === 'run') return runStatusLabel(status)
  if (kind === 'review') return reviewStatusLabel(status)
  return publishStatusLabel(status)
}

/**
 * 状态徽标。
 *
 * `kind` 是必须的，不是装饰参数：`agent_run.status` 与 `published_post.status`
 * 有四个**同名**的值（published / failed / publishing / publish_unknown），
 * 两者含义却不同（前者是运行镜像，后者是发布权威）。要求调用方显式说明自己在
 * 显示哪一个，就不会出现"随手把 Run 状态当成发布状态渲染"的情况。
 */
export function StatusBadge({ status, kind, label }: Props) {
  if (!status) {
    // `—` 表示"没有这个值"（比如这个 Run 没有发布记录），不是"状态未知"。
    return <span className="badge badge--none">{EMPTY}</span>
  }
  const className = status.replace(/[^a-zA-Z0-9_]/g, '')
  return <span className={`badge badge--${className}`}>{label ?? labelFor(kind, status)}</span>
}
