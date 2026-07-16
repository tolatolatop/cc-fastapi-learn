import { ChevronLeft, ChevronRight } from 'lucide-react'

interface PaginationProps {
  page: number
  pageSize: number
  total: number
  itemLabel: string
  onPageChange: (page: number) => void
  onPageSizeChange: (pageSize: number) => void
}

type PageToken = number | 'left-ellipsis' | 'right-ellipsis'

function pageTokens(page: number, totalPages: number): PageToken[] {
  if (totalPages <= 7) return Array.from({ length: totalPages }, (_, index) => index + 1)

  const pages = new Set([1, totalPages, page - 1, page, page + 1])
  const visible = [...pages].filter((value) => value >= 1 && value <= totalPages).sort((a, b) => a - b)
  const result: PageToken[] = []
  visible.forEach((value, index) => {
    const previous = visible[index - 1]
    if (previous && value - previous > 1) {
      result.push(previous === 1 ? 'left-ellipsis' : 'right-ellipsis')
    }
    result.push(value)
  })
  return result
}

export default function Pagination({ page, pageSize, total, itemLabel, onPageChange, onPageSizeChange }: PaginationProps) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const safePage = Math.min(page, totalPages)
  const firstItem = total ? (safePage - 1) * pageSize + 1 : 0
  const lastItem = Math.min(safePage * pageSize, total)

  return (
    <nav className="pagination" aria-label={`${itemLabel}分页`}>
      <span className="pagination-range">第 {firstItem.toLocaleString('zh-CN')}–{lastItem.toLocaleString('zh-CN')} / {total.toLocaleString('zh-CN')} 条</span>
      <div className="pagination-pages">
        <button
          className="pagination-arrow"
          onClick={() => onPageChange(safePage - 1)}
          disabled={safePage <= 1}
          aria-label="上一页"
        >
          <ChevronLeft size={15} />
        </button>
        <span className="pagination-mobile-page">第 {safePage} / {totalPages} 页</span>
        <div className="pagination-number-pages">
          {pageTokens(safePage, totalPages).map((token) => typeof token === 'number' ? (
            <button
              key={token}
              className={token === safePage ? 'active' : ''}
              onClick={() => onPageChange(token)}
              aria-label={`第 ${token} 页`}
              aria-current={token === safePage ? 'page' : undefined}
            >
              {token}
            </button>
          ) : <span key={token} aria-hidden="true">…</span>)}
        </div>
        <button
          className="pagination-arrow"
          onClick={() => onPageChange(safePage + 1)}
          disabled={safePage >= totalPages}
          aria-label="下一页"
        >
          <ChevronRight size={15} />
        </button>
      </div>
      <label className="pagination-size">
        <span>每页</span>
        <select value={pageSize} onChange={(event) => onPageSizeChange(Number(event.target.value))} aria-label={`每页显示${itemLabel}数量`}>
          {[20, 50, 100].map((value) => <option key={value} value={value}>{value}</option>)}
        </select>
      </label>
    </nav>
  )
}
