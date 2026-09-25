import { useCallback, useEffect, useRef, useState } from 'react'

import { ApiError } from '../api/client'

/**
 * 一次性读取的加载状态。
 *
 * 刻意不引入任何状态管理库：控制台的数据流是"进页面读一次、筛选变化重读一次、
 * 操作成功后手动重读一次"，用组件内的 state + effect 就能表达完整。引入
 * Redux/Zustand 只会给这个规模的界面增加一层需要维护的间接。
 *
 * 竞态处理是这里唯一有点分量的部分：筛选条件变得快时，先发出的请求可能后返回，
 * 把旧结果盖在新结果上。用序号标记每次请求，只接受"最后一次"的结果。
 */
export interface AsyncResult<T> {
  data: T | null
  loading: boolean
  error: ApiError | null
  reload: () => void
}

export function useAsync<T>(loader: () => Promise<T>, deps: readonly unknown[]): AsyncResult<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<ApiError | null>(null)
  const [nonce, setNonce] = useState(0)

  // loader 每次渲染都是新函数，直接放进依赖会无限循环。用 ref 持有最新的那份，
  // 由调用方通过 deps 决定"什么时候该重新加载"。
  const loaderRef = useRef(loader)
  loaderRef.current = loader

  const generation = useRef(0)

  useEffect(() => {
    const current = ++generation.current
    setLoading(true)
    setError(null)

    loaderRef
      .current()
      .then((result) => {
        if (current !== generation.current) return // 已被更新的请求取代
        setData(result)
        setLoading(false)
      })
      .catch((cause: unknown) => {
        if (current !== generation.current) return
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, 'Unexpected error while loading data.'),
        )
        setLoading(false)
      })

    return () => {
      // 卸载或依赖变化时让在途结果作废，避免对已卸载组件 setState
      generation.current += 1
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce])

  const reload = useCallback(() => setNonce((value) => value + 1), [])

  return { data, loading, error, reload }
}
