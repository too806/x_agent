import type { Page } from '../types/api'

interface Props {
  page: Page
  onChange: (page: number) => void
}

/**
 * 服务器端分页控件。
 *
 * 页数来自后端的 `page.pages` / `page.total`，**不在前端根据当前数组长度推算**——
 * 那样算出来的只会是"这一页有几条"，翻页器会永远显示「第 1 页，共 1 页」。
 *
 * 原先还有一个 `itemLabel` 参数（调用方传 "runs" / "records"），中文化时删掉了：
 * 中文里这些列表的单位都是"条"，留着它只会让某个调用方漏改、界面上冒出英文名词。
 */
export function Pagination({ page, onChange }: Props) {
  const from = page.total === 0 ? 0 : (page.page - 1) * page.page_size + 1
  const to = Math.min(page.page * page.page_size, page.total)

  return (
    <div className="pagination">
      <div>
        共 {page.total} 条（{from}–{to}）
      </div>
      <div className="pagination__controls">
        <button
          type="button"
          className="btn btn--sm"
          disabled={page.page <= 1}
          onClick={() => onChange(page.page - 1)}
        >
          上一页
        </button>
        <span className="num">
          第 {page.pages === 0 ? 0 : page.page} / {page.pages} 页
        </span>
        <button
          type="button"
          className="btn btn--sm"
          disabled={page.pages === 0 || page.page >= page.pages}
          onClick={() => onChange(page.page + 1)}
        >
          下一页
        </button>
      </div>
    </div>
  )
}
