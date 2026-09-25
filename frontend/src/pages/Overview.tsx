import { EmptyState } from '../components/States'

/**
 * 首页。Phase 4b-1 只交付外壳，内容在 4b-2 落地。
 *
 * 这里刻意**不**渲染半成品：一个看起来能用、数字却是占位的仪表盘，比一个明说
 * "还没接上"的页面危险得多——前者会让人相信它显示的东西。
 */
export function Overview() {
  return (
    <EmptyState
      title="Overview is not wired up yet"
      hint="Phase 4b-2 会在这里接入 KPI、趋势、Review Queue 与 Publish Resolution Queue。"
    />
  )
}
