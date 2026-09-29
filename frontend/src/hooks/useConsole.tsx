import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'

import { api, ApiError } from '../api/client'
import { useAsync } from './useAsync'
import type { OverviewResponse } from '../types/api'

/**
 * 全控制台共享的一份总览数据。
 *
 * 它同时服务三处，而它们**必须看到同一份数字**：
 *
 *   1. 侧边导航上的两个待办数（待审核 / 待裁决）—— 最常被瞟一眼的东西。
 *   2. 顶栏上的同一组数字与系统状态。
 *   3. 首页正文（KPI、趋势、两个队列）。
 *
 * 之前是 Provider 自己调一次 `getOverview(7)`、首页再调一次 `getOverview(days)`：
 * 打开首页会**同时发出两个请求**，而且两个请求的窗口可能不同（Provider 固定 7 天，
 * 首页用下拉框选的天数），于是侧栏数字和首页数字在切窗口后会短暂对不上。
 * 现在只有一份数据、一个窗口：首页改窗口，导航上的数字跟着一起改——因为它们本来
 * 说的就是同一件事。
 *
 * 刻意不引入 Redux/Zustand —— 这里共享的状态只有"一份总览 + 一个刷新函数"，
 * 一个 context 已经是最小解。
 */
interface ConsoleState {
  overview: OverviewResponse | null
  loading: boolean
  error: ApiError | null
  /** 趋势图与队列的时间窗口，单位天。7 / 14 / 30 / 90。 */
  days: number
  setDays: (days: number) => void
  /** 写操作成功后调用，重新拉取总览（计数与队列一起更新）。 */
  refresh: () => void
  pendingReviews: number
  pendingResolutions: number
  /** 数据是否已经到过一次。首次加载完成前不要在导航上显示数字。 */
  ready: boolean
}

const ConsoleContext = createContext<ConsoleState | null>(null)

export function ConsoleProvider({ children }: { children: ReactNode }) {
  const [days, setDays] = useState(7)

  const { data, loading, error, reload } = useAsync(() => api.getOverview(days), [days])

  // 刷新按钮、"写操作后刷新计数"、"切窗口"最终都落到 useAsync 的同一处重取上，
  // 不额外维护一个 nonce —— 两条并行的刷新路径迟早会有一条忘记带上窗口参数。
  const refresh = useCallback(() => reload(), [reload])

  const value = useMemo<ConsoleState>(
    () => ({
      overview: data,
      loading,
      error,
      days,
      setDays,
      refresh,
      pendingReviews: data?.review_queue_total ?? 0,
      pendingResolutions: data?.resolution_queue_total ?? 0,
      ready: data !== null,
    }),
    [data, loading, error, days, refresh],
  )

  return <ConsoleContext.Provider value={value}>{children}</ConsoleContext.Provider>
}

export function useConsole(): ConsoleState {
  const value = useContext(ConsoleContext)
  if (value === null) {
    throw new Error('useConsole must be used inside <ConsoleProvider>')
  }
  return value
}
