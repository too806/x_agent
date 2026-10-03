/**
 * 图表用色。
 *
 * Recharts 把颜色写进 SVG 属性，浏览器不会在那里解析 `var(--accent)`，
 * 所以图表是全站唯一没法直接吃 CSS 变量的地方。既然躲不开硬编码，就让它
 * **只硬编码一次**——取值与 `tokens.css` 里对应的变量保持一致，改主题时
 * 两边一起改，而不是散落在每个图表的 JSX 里。
 *
 * 每个常量都注明它对应哪个 token，方便对照。
 */
export const CHART_COLORS = {
  /** --border */
  grid: '#e2e5ea',
  /** --text-muted */
  tick: '#667085',
  /** --accent */
  stroke: '#2563eb',
  /** --accent-soft */
  fill: '#eff4ff',
} as const

/** 坐标轴刻度文字的统一样式：字号跟 --font-caption 对齐。 */
export const CHART_TICK = { fontSize: 11, fill: CHART_COLORS.tick } as const
