/**
 * 待审队列的「决定之后选哪一条」规则。
 *
 * 单独抽成纯函数，是因为这条规则有一个不显眼的边界：队列里**只有一条**时，决定完它
 * 之后没有任何"下一条"可跳。这时若把选中项清空，右侧详情会跟着一起消失 —— 而人此刻
 * 最想看的恰恰是刚刚那次决定的结果：编辑稿存下来了没有、现在是什么状态、能不能发布。
 * 清空之后这些全都看不到，只能去运行记录里把这条 Run 重新找出来。
 *
 * 无依赖、无副作用，所以 `node --test` 可以直接跑（见 `frontend/tests/`）。
 */
export function nextSelection<T extends { run_id: string }>(
  list: readonly T[],
  selected: string | null,
): string | null {
  if (selected === null) return null

  const index = list.findIndex((item) => item.run_id === selected)

  // 选中项不在这一页里（翻页了，或队列刚刷新过）—— 不动它。这时既没有"下一条"的
  // 概念，也不该把人正在看的详情换成一条无关的 Run。
  if (index < 0) return selected

  // 有下一条就跳过去：审核是连续决策，处理完一条立刻接着看下一条。
  // **没有下一条时保留当前这条**，而不是清空 —— 见文件头。
  return list[index + 1]?.run_id ?? selected
}
