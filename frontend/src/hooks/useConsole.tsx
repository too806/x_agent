import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'

import { api } from '../api/client'
import { useAsync } from './useAsync'

/**
 * 全局的"待办计数"。
 *
 * 侧边导航上的两个数字（待审核 / 待裁决）是控制台最常被瞟一眼的东西，
 * 它们必须**跨页面一致**：在 Review 页审掉一个 Run 之后，导航上的数字要立刻少 1，
 * 而不是等用户切页面才更新。
 *
 * 因此把它放在 context 里：任何页面完成一次写操作后调用 `refresh()`，整个界面的
 * 计数一起重取。刻意不引入 Redux/Zustand —— 这里共享的状态只有"两个数字 + 一个
 * 刷新函数"，一个 context 已经是最小解。
 */
interface ConsoleState {
  pendingReviews: number
  pendingResolutions: number
  /** 计数是否已经有值。首次加载完成前不要在导航上显示数字。 */
  ready: boolean
  /** 写操作成功后调用，重新拉取计数。 */
  refresh: () => void
}

const ConsoleContext = createContext<ConsoleState | null>(null)

export function ConsoleProvider({ children }: { children: ReactNode }) {
  const [nonce, setNonce] = useState(0)

  const { data } = useAsync(() => api.getOverview(7), [nonce])

  const refresh = useCallback(() => setNonce((value) => value + 1), [])

  const value = useMemo<ConsoleState>(
    () => ({
      pendingReviews: data?.review_queue_total ?? 0,
      pendingResolutions: data?.resolution_queue_total ?? 0,
      ready: data !== null,
      refresh,
    }),
    [data, refresh],
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
