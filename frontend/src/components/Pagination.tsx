import type { Page } from '../types/api'
import { IconChevron } from './ui/icons'

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
 *
 * 这一版加了页码按钮。只有"上一页 / 下一页"时，从第 1 页去第 5 页要按四次，
 * 而且看不出总共在哪一段。页码最多显示 7 个，中间用省略号跳——Run 列表翻到
 * 几十页时不该把一整行按钮铺出来。当前页用 `aria-current="page"` 标出来，
 * 不只靠颜色（加粗 + 边框 + 属性，三条通道）。
 */
export function Pagination({ page, onChange }: Props) {
  const from = page.total === 0 ? 0 : (page.page - 1) * page.page_size + 1
  const to = Math.min(page.page * page.page_size, page.total)
  const items = pageItems(page.page, page.pages)

  return (
    <nav className="pagination" aria-label="分页">
      <div>
        {page.total === 0 ? '共 0 条' : `共 ${page.total} 条（${from}–${to}）`}
      </div>
      <div className="pagination__controls">
        <button
          type="button"
          className="btn btn--sm pagination__step"
          disabled={page.page <= 1}
          onClick={() => onChange(page.page - 1)}
          aria-label="上一页"
        >
          <IconChevron size={14} />
          上一页
        </button>
        {items.map((item, index) =>
          item === null ? (
            // key 用位置 + 序号：省略号可能出现两次，不能用页码当 key（它是 null）。
            <span className="pagination__gap" key={`gap-${index}`} aria-hidden="true">
              …
            </span>
          ) : (
            <button
              key={item}
              type="button"
              className={item === page.page ? 'btn btn--sm pagination__num is-active' : 'btn btn--sm pagination__num'}
              onClick={() => onChange(item)}
              aria-current={item === page.page ? 'page' : undefined}
              aria-label={`第 ${item} 页`}
            >
              {item}
            </button>
          ),
        )}
        <button
          type="button"
          className="btn btn--sm pagination__step pagination__step--next"
          disabled={page.pages === 0 || page.page >= page.pages}
          onClick={() => onChange(page.page + 1)}
        >
          下一页
          <IconChevron size={14} />
        </button>
      </div>
    </nav>
  )
}

/**
 * 页码序列，`null` 表示省略号。
 *
 * 页数少时全列；多了就固定"首 / 尾 / 当前左右各一"，其余折叠。返回 null 而不是
 * 页码字符串，是为了让渲染层不可能把它误当成一个可点的页。
 */
function pageItems(current: number, pages: number): (number | null)[] {
  if (pages <= 0) return []
  if (pages <= 7) return Array.from({ length: pages }, (_, index) => index + 1)

  const wanted = new Set([1, pages, current, current - 1, current + 1])
  const list = Array.from(wanted)
    .filter((value) => value >= 1 && value <= pages)
    .sort((a, b) => a - b)

  const out: (number | null)[] = []
  let previous = 0
  for (const value of list) {
    if (previous && value - previous > 1) out.push(null)
    out.push(value)
    previous = value
  }
  return out
}
