import { useState } from 'react'
import { BarChart3, Rows3 } from 'lucide-react'
import { Button, ButtonGroup } from 'react-bootstrap'
import ReviewDashboardPage from './ReviewDashboardPage'
import ReviewIssuesPage from './ReviewIssuesPage'

interface ReviewWorkspaceProps {
  onOpenSettings: () => void
  onOpenTask: (taskId: string) => void
}

export default function ReviewWorkspace({ onOpenSettings, onOpenTask }: ReviewWorkspaceProps) {
  const [view, setView] = useState<'dashboard' | 'ledger'>('dashboard')

  return (
    <>
      <div className="review-workspace-switcher">
        <span>检视工作区</span>
        <ButtonGroup aria-label="切换检视视图">
          <Button variant={view === 'dashboard' ? 'primary' : 'outline-secondary'} onClick={() => setView('dashboard')}><BarChart3 size={16} />聚合看板</Button>
          <Button variant={view === 'ledger' ? 'primary' : 'outline-secondary'} onClick={() => setView('ledger')}><Rows3 size={16} />批次台账</Button>
        </ButtonGroup>
      </div>
      {view === 'dashboard'
        ? <ReviewDashboardPage onOpenTask={onOpenTask} onOpenSettings={onOpenSettings} />
        : <ReviewIssuesPage onOpenTask={onOpenTask} onOpenSettings={onOpenSettings} />}
    </>
  )
}
