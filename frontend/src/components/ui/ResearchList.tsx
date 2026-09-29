import { useState } from 'react'

import type { ResearchItemRecord } from '../../types/api'
import { dateTime } from '../../utils/format'

/**
 * 研究素材列表。
 *
 * 抽出来的原因很直接：同一个列表此前在**两个地方各写了一遍**（审核工作台的右栏、
 * Run 详情抽屉里），而且两边的字段还不一样——抽屉里显示抓取时间，审核面板里没有。
 * 同一批素材在两个页面长得不同，会让人以为它们不是同一份数据。
 *
 * `initialVisible` 让抽屉可以只先放前几条（它的正文里还有草稿、发布记录、
 * 决策轨迹要读），审核工作台则一次全给（人就是要读完才做决定）。
 * 无论折多少条，**总数一直是写出来的**，折叠不会让人少看到"这里有多少"。
 */
export function ResearchList({
  items,
  initialVisible,
}: {
  items: ResearchItemRecord[]
  /** 不传 = 全部显示。传了就先显示前 N 条，其余折起来。 */
  initialVisible?: number
}) {
  const [expanded, setExpanded] = useState(false)

  if (items.length === 0) {
    return <div className="cell-muted">这个 Run 没有存下研究内容。</div>
  }

  const collapsed = !expanded && initialVisible !== undefined && items.length > initialVisible
  const visible = collapsed ? items.slice(0, initialVisible) : items
  const hidden = items.length - visible.length

  return (
    <>
      <ul className="research-list">
        {visible.map((item) => (
          <ResearchItem key={item.item_id} item={item} />
        ))}
      </ul>

      {hidden > 0 || (initialVisible !== undefined && items.length > initialVisible) ? (
        <div className="row research-list__more">
          <button type="button" className="btn btn--sm" onClick={() => setExpanded((on) => !on)}>
            {expanded ? '收起' : `展开全部 ${items.length} 条`}
          </button>
          {collapsed ? (
            <span className="cell-muted">
              还有 {hidden} 条没有显示（共 {items.length} 条）
            </span>
          ) : null}
        </div>
      ) : null}
    </>
  )
}

function ResearchItem({ item }: { item: ResearchItemRecord }) {
  return (
    <li className="research-item">
      <div className="row row--between">
        <strong>{item.author_username ? `@${item.author_username}` : '作者未知'}</strong>
        <span className="research-item__stats">
          {item.likes} 点赞 · {item.reposts} 转发 · {item.replies} 回复
          {item.published_at ? ` · ${dateTime(item.published_at)}` : ''}
        </span>
      </div>
      <div className="body-block">{item.content}</div>
      <div className="research-item__source">
        {item.source_url ? (
          <a href={item.source_url} target="_blank" rel="noreferrer noopener">
            {item.platform_post_id}
          </a>
        ) : (
          <span className="cell-muted">{item.platform_post_id}</span>
        )}
      </div>
    </li>
  )
}
