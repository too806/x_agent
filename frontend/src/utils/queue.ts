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

/**
 * 待审队列的排序方向。
 *
 * `oldest` 是默认的先进先出：人工审核是工作队列，积压按到达顺序清。`newest` 服务的
 * 是另一条路径——**刚跑完就想审这一条**。队列几十条深时，那一条在 FIFO 顺序里排在
 * 最后一页，而"我刚跑的去哪了"是最常见的一问。
 *
 * 这是**两个正当需求**，所以做成开关而不是把默认改掉：默认一改，积压就再没人顺手
 * 从头清。后端的 `order` 参数与这里的取值一一对应。
 */
export type QueueOrder = 'oldest' | 'newest'

/**
 * 把 URL 里的 `order` 解析成排序方向。
 *
 * **未知值一律落回 `oldest`**，与后端默认值一致。这条不是防御性编程：URL 是可以被
 * 收藏、被贴进聊天、被手改的，一个拼错的 `?order=newestt` 不应该让审核页打不开，
 * 而应该只是没生效——所以这里不抛错、不返回 undefined 让调用方去猜。
 * 盘面上也因此**看不到 422**：前端永远只发这两个值之一（后端那道 `Literal` 是给
 * 直接调 API 的人准备的）。
 *
 * 抽成纯函数是为了能被 `node --test` 直接覆盖，理由同 `nextSelection`（见文件头）。
 */
export function parseQueueOrder(raw: string | null | undefined): QueueOrder {
  return raw === 'newest' ? 'newest' : 'oldest'
}
