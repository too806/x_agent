/**
 * 加载占位（骨架屏）。
 *
 * 之前"加载中"是一行灰字：数据到了以后内容撑开，页面会跳一下；切换筛选时
 * 每切一次跳一次。骨架屏把这跳去掉——它占住内容将要出现的位置和大致高度。
 *
 * 宽度用内联 style 传：那是一个**几何值**，不是设计决策，没必要为每一档宽度
 * 起一个类名。颜色、圆角、动效仍然全部来自 tokens。
 */

interface LineProps {
  /** 百分比宽度。不传就是占满。 */
  width?: string
  /** 用较高的一档，用于标题 / KPI 数字的位置。 */
  tall?: boolean
}

export function SkeletonLine({ width, tall = false }: LineProps) {
  return (
    <span
      className={`skeleton ${tall ? 'skeleton--title' : 'skeleton--line'}`}
      style={width ? { width } : undefined}
    />
  )
}

/** 若干行文本的占位。行宽递减，读起来像一段话而不是一排等长条。 */
export function SkeletonLines({ count = 3 }: { count?: number }) {
  const widths = ['100%', '92%', '74%', '86%', '64%']
  return (
    <div className="skeleton-stack">
      {Array.from({ length: count }, (_, index) => (
        <SkeletonLine key={index} width={widths[index % widths.length]} />
      ))}
    </div>
  )
}

/**
 * 表格占位。
 *
 * 行数与真实分页一致地固定住；列宽用等分网格——精确复刻每列的宽度需要
 * 把每张表的列定义也搬过来，而骨架屏的职责只是"别让页面跳"，不是"预览内容"。
 */
export function SkeletonTable({ rows = 6, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <div className="skeleton-table" aria-hidden="true">
      {Array.from({ length: rows }, (_, row) => (
        <div
          className="skeleton-table__row"
          key={row}
          style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
        >
          {Array.from({ length: cols }, (_, col) => (
            <SkeletonLine key={col} width={col === 0 ? '80%' : '55%'} />
          ))}
        </div>
      ))}
    </div>
  )
}

/** 一排卡片（KPI）的占位。 */
export function SkeletonCards({ count = 4 }: { count?: number }) {
  return (
    <div className="kpi-grid" aria-hidden="true">
      {Array.from({ length: count }, (_, index) => (
        <div className="kpi stack stack--tight" key={index}>
          <SkeletonLine width="45%" />
          <SkeletonLine width="30%" tall />
        </div>
      ))}
    </div>
  )
}
