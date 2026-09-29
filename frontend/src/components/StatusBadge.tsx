import { EMPTY, publishStatusLabel, reviewStatusLabel, runStatusLabel } from '../utils/format'

type Kind = 'run' | 'review' | 'publish'

interface Props {
  status: string | null | undefined
  kind: Kind
  /** 覆盖显示文案。当前没有任何调用方使用（保留为扩展点，见下方说明）。 */
  label?: string
  /** 不显示前面的圆点。用于已经靠上下文说明状态、再加点就啰嗦的地方。 */
  bare?: boolean
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
 *
 * 每个徽标前面有一个圆点（和文字同色）。这不是装饰：状态信息此前**只靠颜色**
 * 传递，色觉障碍用户或黑白打印下，同一形状的徽标只能靠文字区分——文字确实还在，
 * 但"一眼扫过去分辨状态"这件事就没有了。点提供了第二个通道，也顺带让
 * "发布结果未知"（实心橙 + 点）在列表里跳出来。
 *
 * `title` 给出**后端的原始枚举值**。显示是中文，但排查时要能和日志、接口对上，
 * 所以原始值不该在界面上完全消失。它只是补充——意思由文字和颜色表达，
 * 不依赖悬停才看得到。
 */
export function StatusBadge({ status, kind, label, bare = false }: Props) {
  if (!status) {
    // `—` 表示"没有这个值"（比如这个 Run 没有发布记录），不是"状态未知"。
    return <span className="badge badge--none">{EMPTY}</span>
  }
  const className = status.replace(/[^a-zA-Z0-9_]/g, '')
  return (
    <span className={`badge badge--${className}`} title={status}>
      {bare ? null : <span className="badge__dot" aria-hidden="true" />}
      {label ?? labelFor(kind, status)}
    </span>
  )
}
