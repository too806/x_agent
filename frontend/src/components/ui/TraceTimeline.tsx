import type { DecisionTraceEntry } from '../../types/api'
import { agentActionLabel, dateTime } from '../../utils/format'
import { traceKind, traceReason } from '../../utils/trace'

/**
 * Agent 决策轨迹 —— 整个控制台里最能说明"这是一个 Agent"的地方。
 *
 * 后端给的是一张表（每一步的 action / reason / target / 输入摘要 / 结果摘要），
 * 直接铺开就是一坨 JSON。这里把它读成**一条推理链**：
 *
 *   每一步长什么样由它自己决定——研究是蓝的、生成是紫的、评审是黄的。
 *   节点用序号而不是图标，因为顺序本身就是信息（第 7 步是在第 6 步之后才做出的）。
 *
 * 三层信息的取舍是这个组件存在的全部理由：
 *
 *   1. **理由**（`reason`）—— 唯一由 Agent 自己写的字段，也是唯一回答"为什么"
 *      的字段。给它正文的字号和行高，放在最容易读到的地方。
 *   2. **摘要**（`result_summary`）与**作用于**（`target`）—— 一步做成了什么、
 *      作用在谁身上。都默认可见：轨迹只有动机没有结果的话，读起来像一段心里活动。
 *   3. **输入摘要与后端枚举值** —— 机器记录（"当时手上有什么"），给排查用的。
 *      折进 `<details>`：默认展开会把上面两层淹掉，而人打开轨迹想先看懂的是过程。
 *
 * `action` 的中文只存在于显示层（`format.ts` 的映射表），原始英文枚举在这两种
 * 情况下照样出现：映射表里没有的新动作（原样显示），以及"技术细节"里的
 * `action` 字段（排查时要和日志对得上）。
 */
export function TraceTimeline({ entries }: { entries: DecisionTraceEntry[] }) {
  return (
    <ol className="trace">
      {entries.map((entry) => (
        <TraceItem key={entry.step} entry={entry} />
      ))}
    </ol>
  )
}

function TraceItem({ entry }: { entry: DecisionTraceEntry }) {
  const kind = traceKind(entry.action)

  return (
    <li className={`trace__item trace__item--${kind}`}>
      {/* 序号节点。它是这条线的"骨"：看得出走到第几步、还有没有下一步。 */}
      <span className="trace__marker" aria-hidden="true">
        {entry.step}
      </span>

      <div className="trace__body">
        <div className="trace__head">
          <span className="trace__action">
            {/* 序号已经在左边的节点上了，这里不再重复"第 N 步"——那行小字会
                和动作名抢同一行，而动作名才是要读的。 */}
            {agentActionLabel(entry.action)}
          </span>
          <span className="trace__time">
            <span className="sr-only">第 {entry.step} 步，</span>
            {dateTime(entry.created_at)}
          </span>
        </div>

        {/* 为什么这么做。 */}
        <div className="trace__reason">{traceReason(entry.reason)}</div>

        {entry.target ? (
          <div className="trace__target">作用于：{entry.target}</div>
        ) : null}

        {/* 这一步**做成了什么**。默认可见而不是折起来：理由回答"为什么"，
            摘要回答"然后呢"，两个都是一眼要看到的；折起来的话轨迹就只剩动机、
            没有结果，读起来像一段心里活动而不是一次工作过程。
            `result_summary` 是 Agent 写完之后回填的，最后一步有可能还没写回来
            —— 这时说"还没回填"，不说空。 */}
        <div className="trace__result">
          {entry.result_summary || '这一步的结果还没有回填。'}
        </div>

        <details className="disclosure trace__details">
          <summary>技术细节（后端枚举值与输入摘要）</summary>
          <div className="disclosure__body stack stack--tight">
            <div className="meta">
              <div className="meta__label">动作（后端枚举值）</div>
              <div className="mono">{entry.action}</div>
            </div>
            {/* 存的是"当时手上有什么"，不是完整 prompt —— 所以这一行很短。 */}
            <div className="meta">
              <div className="meta__label">当时手里</div>
              <div>{entry.input_summary}</div>
            </div>
          </div>
        </details>
      </div>
    </li>
  )
}
