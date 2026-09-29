import { EMPTY, dateTime } from '../../utils/format'
import type { LifecycleStep } from '../../utils/lifecycle'

/**
 * Run 生命周期：创建 → 研究 → 生成 → 规则检查 → 审核 → 发布。
 *
 * 打开一条 Run 时，人最先要回答的问题是"它走到哪了、卡在哪、下一步该谁动"。
 * 六个阶段的完整状态 + 时刻 + 一句话摘要，一屏之内答完；再往下才是研究、草稿、
 * 发布记录这些**材料**。
 *
 * 状态 → 视觉的映射全部来自 `utils/lifecycle.ts`，这个组件不做任何判断：
 * 它只负责把 `state` 贴成类名。判断留在那个纯函数里，是因为它错了不会报错——
 * 界面只会安静地说假话。
 *
 * 时刻列没有值时显示 `—`（和全站一致：空值是"没有这个值"，不是渲染失败）。
 * 规则检查那一行永远没有时刻：checkpoint 里就没有记，不拿别的时间凑。
 */
export function LifecycleTimeline({ steps }: { steps: LifecycleStep[] }) {
  return (
    <ol className="lifecycle">
      {steps.map((step) => (
        <li key={step.key} className={`lifecycle__step lifecycle__step--${step.state}`}>
          <span className="lifecycle__dot" aria-hidden="true" />
          <span className="lifecycle__label">{step.label}</span>
          <span className="lifecycle__time">{step.at ? dateTime(step.at) : EMPTY}</span>
          {/* 状态本身是用颜色 + 圆点表达的，对屏幕阅读器不可见，
              所以把状态写进无障碍名里。 */}
          <span className="lifecycle__note">
            <span className="sr-only">{STATE_LABELS[step.state]}：</span>
            {step.note}
          </span>
        </li>
      ))}
    </ol>
  )
}

const STATE_LABELS: Record<LifecycleStep['state'], string> = {
  done: '已走过',
  current: '当前停在',
  ok: '成功',
  unknown: '结果未知',
  failed: '失败',
  pending: '未开始',
  skipped: '不会发生',
}
