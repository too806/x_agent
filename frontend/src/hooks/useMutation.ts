import { useCallback, useState } from 'react'

import { ApiError } from '../api/client'

/**
 * 写操作（审核 / 发布 / 裁决）的统一状态。
 *
 * 只维护三件事：正在忙、上一次是否失败、失败信息。刻意**不**在这里做乐观更新：
 * 发布状态必须以后端返回的权威结果为准，先在界面上假装成功，正是这个系统最不能
 * 承受的错误——人会以为已经发出去了。
 */
export interface MutationResult<TArgs extends unknown[], TResult> {
  busy: boolean
  error: ApiError | null
  /** 失败时返回 null（错误已记录在 `error` 里），不会抛给调用方。 */
  run: (...args: TArgs) => Promise<TResult | null>
  reset: () => void
}

export function useMutation<TArgs extends unknown[], TResult>(
  action: (...args: TArgs) => Promise<TResult>,
): MutationResult<TArgs, TResult> {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  const run = useCallback(
    async (...args: TArgs): Promise<TResult | null> => {
      setBusy(true)
      setError(null)
      try {
        return await action(...args)
      } catch (cause: unknown) {
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, 'Unexpected error while submitting.'),
        )
        return null
      } finally {
        setBusy(false)
      }
    },
    [action],
  )

  const reset = useCallback(() => setError(null), [])

  return { busy, error, run, reset }
}
