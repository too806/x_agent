import { useEffect } from 'react'

/**
 * 当前页空了就退一页。
 *
 * 这不是洁癖，是一个真实会发生的场景：在第 2 页上重发最后一条 failed 的记录，
 * 它成功后就不再满足筛选条件，于是第 2 页变成空的。留在那里显示"没有发布记录"
 * 是错的——第 1 页还有记录，只是这个数字被读成了"全都没有了"。
 *
 * 三个列表页都需要同一条规则（Review / Runs / Published），所以收在这里。
 * 单独写三遍的风险不在于重复几行，而在于那个 `page > 1` 的守卫：漏掉它，
 * 第 1 页的空结果会让人不停地往回翻到第 0 页。
 *
 * `settled` 是"这一页的结果已经拿到了"，必须由调用方给：加载中时 `items` 还是
 * 上一份数据或空数组，据此判断会误退一页。
 */
export function usePageClamp(
  page: number,
  setPage: (page: number) => void,
  isEmpty: boolean,
  settled: boolean,
): void {
  useEffect(() => {
    if (settled && isEmpty && page > 1) {
      setPage(page - 1)
    }
  }, [settled, isEmpty, page, setPage])
}
