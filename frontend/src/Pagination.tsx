import { Form, Pagination as BootstrapPagination } from 'react-bootstrap'

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
    <nav className="console-pagination" aria-label={`${itemLabel}分页`}>
      <span className="pagination-range">第 {firstItem.toLocaleString('zh-CN')}–{lastItem.toLocaleString('zh-CN')} / {total.toLocaleString('zh-CN')} 条</span>
      <BootstrapPagination size="sm" className="pagination-pages">
        <BootstrapPagination.Prev
          onClick={() => onPageChange(safePage - 1)}
          disabled={safePage <= 1}
          aria-label="上一页"
        />
        <li className="pagination-mobile-page">第 {safePage} / {totalPages} 页</li>
        {pageTokens(safePage, totalPages).map((token) => typeof token === 'number' ? (
            <BootstrapPagination.Item
              key={token}
              active={token === safePage}
              onClick={() => onPageChange(token)}
              aria-label={`第 ${token} 页`}
              aria-current={token === safePage ? 'page' : undefined}
            >
              {token}
            </BootstrapPagination.Item>
          ) : <BootstrapPagination.Ellipsis key={token} disabled />)}
        <BootstrapPagination.Next
          onClick={() => onPageChange(safePage + 1)}
          disabled={safePage >= totalPages}
          aria-label="下一页"
        />
      </BootstrapPagination>
      <label className="pagination-size">
        <span>每页</span>
        <Form.Select size="sm" value={pageSize} onChange={(event) => onPageSizeChange(Number(event.target.value))} aria-label={`每页显示${itemLabel}数量`}>
          {[20, 50, 100].map((value) => <option key={value} value={value}>{value}</option>)}
        </Form.Select>
      </label>
    </nav>
  )
}
