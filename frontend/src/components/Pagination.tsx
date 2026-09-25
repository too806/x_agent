import type { Page } from '../types/api'

interface Props {
  page: Page
  onChange: (page: number) => void
  /** 分页控件在客户端算页数，但要显示的是后端的真实总数。 */
  itemLabel?: string
}

/**
 * 服务器端分页控件。
 *
 * 页数来自后端的 `page.pages` / `page.total`，**不在前端根据当前数组长度推算**——
 * 那样算出来的只会是"这一页有几条"，翻页器会永远显示「第 1 页，共 1 页」。
 */
export function Pagination({ page, onChange, itemLabel = 'items' }: Props) {
  const from = page.total === 0 ? 0 : (page.page - 1) * page.page_size + 1
  const to = Math.min(page.page * page.page_size, page.total)

  return (
    <div className="pagination">
      <div>
        {from}–{to} of {page.total} {itemLabel}
      </div>
      <div className="pagination__controls">
        <button
          type="button"
          className="btn btn--sm"
          disabled={page.page <= 1}
          onClick={() => onChange(page.page - 1)}
        >
          Previous
        </button>
        <span className="num">
          Page {page.pages === 0 ? 0 : page.page} / {page.pages}
        </span>
        <button
          type="button"
          className="btn btn--sm"
          disabled={page.pages === 0 || page.page >= page.pages}
          onClick={() => onChange(page.page + 1)}
        >
          Next
        </button>
      </div>
    </div>
  )
}
