import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import { EmptyState } from './States'
import { shortDate } from '../utils/format'
import type { TrendPoint } from '../types/api'

interface Props {
  points: TrendPoint[]
  /** 无数据时的说明文案，由调用方给出——图表不知道自己在画什么。 */
  emptyHint: string
}

/**
 * 自然日趋势图。
 *
 * 后端保证返回的是**补齐过的连续日期序列**（窗口内没有活动的日子是 0，不是缺失），
 * 所以横轴不会因为某天没数据而压缩。这点很重要：压低的天数会让"连续三天零产出"
 * 看起来像"三天并作一天"。
 *
 * 窗口内全为 0 时不画图——一条贴着底边的直线看起来像"有数据且平稳"，而事实是
 * 什么都没有。这里直接说明。
 */
export function TrendChart({ points, emptyHint }: Props) {
  const total = points.reduce((sum, point) => sum + point.count, 0)

  if (points.length === 0 || total === 0) {
    return <EmptyState title="No activity in this window" hint={emptyHint} />
  }

  return (
    <div className="chart-box">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
          <CartesianGrid stroke="#e2e5ea" strokeDasharray="3 3" vertical={false} />
          <XAxis
            dataKey="date"
            tickFormatter={(value) => shortDate(String(value))}
            tick={{ fontSize: 11, fill: '#667085' }}
            tickMargin={6}
          />
          <YAxis
            allowDecimals={false}
            tick={{ fontSize: 11, fill: '#667085' }}
            width={44}
          />
          <Tooltip
            labelFormatter={(label) => shortDate(String(label))}
            formatter={(value) => [String(value), 'count']}
          />
          <Area
            type="monotone"
            dataKey="count"
            stroke="#2563eb"
            strokeWidth={2}
            fill="#eff4ff"
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}
