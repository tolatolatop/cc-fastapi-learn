import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Table } from 'react-bootstrap'

export default function IssueMarkdown({ children }: { children: string }) {
  return <div className="issue-markdown">
    <Markdown remarkPlugins={[remarkGfm]} components={{
      a: ({ children, href, title }) => <a href={href} title={title} target="_blank" rel="noreferrer">{children}</a>,
      table: ({ children }) => <Table responsive bordered size="sm">{children}</Table>,
    }}>{children}</Markdown>
  </div>
}
