import { useEffect, useState } from 'react'

/**
 * 延迟跟随一个值。
 *
 * 只用在 topic 搜索框上：后端做的是模糊匹配，每敲一个字符发一次请求既没有意义，
 * 也会让"后发出的请求先返回"更容易发生。输入框本身仍然是即时响应的——被延迟的
 * 只是**查询条件**，不是输入框的值。
 *
 * 下拉框和日期不做延迟：那两种输入是一次性确定的，多等 400ms 只会显得迟钝。
 */
export function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value)

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs)
    return () => window.clearTimeout(timer)
  }, [value, delayMs])

  return debounced
}
