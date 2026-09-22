import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from './api'
import { OverviewTab } from './components/OverviewTab'
import { LeaguesTab } from './components/LeaguesTab'
import { WaiversTab } from './components/WaiversTab'
import { ActionsTab } from './components/ActionsTab'

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'leagues', label: 'Leagues' },
  { id: 'waivers', label: 'Waivers' },
  { id: 'actions', label: 'Action log' },
]

export default function App() {
  const [tab, setTab] = useState('overview')
  const [busyId, setBusyId] = useState(null)
  const qc = useQueryClient()

  const overview = useQuery({ queryKey: ['overview'], queryFn: api.overview, refetchInterval: 60_000 })
  const actions = useQuery({ queryKey: ['actions'], queryFn: api.actions, enabled: tab === 'actions' })

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['overview'] })
    qc.invalidateQueries({ queryKey: ['actions'] })
  }

  const approve = useMutation({
    mutationFn: api.approve,
    onSettled: () => {
      setBusyId(null)
      refresh()
    },
  })
  const reject = useMutation({
    mutationFn: ({ id, reason }) => api.reject(id, reason),
    onSettled: () => {
      setBusyId(null)
      refresh()
    },
  })

  const onApprove = (id) => {
    setBusyId(id)
    approve.mutate(id)
  }
  const onReject = (id, reason) => {
    setBusyId(id)
    reject.mutate({ id, reason })
  }

  const data = overview.data
  const pendingCount = data?.needs_approval?.length ?? 0

  return (
    <div className="shell">
      <header className="masthead">
        <h1>Fantasy Command Center</h1>
        <span className="sub">
          {overview.isLoading ? 'loading…' : `${data?.leagues?.length ?? 0} leagues`}
        </span>
      </header>

      {data && (
        <div className={`banner ${data.dry_run ? '' : 'live'}`}>
          <strong>{data.dry_run ? 'Dry run' : 'LIVE'}</strong>
          <span>
            {data.dry_run
              ? 'No writes leave this process. Payloads are recorded so you can inspect them.'
              : 'Writes go to your real leagues. Approved actions will be submitted.'}
          </span>
        </div>
      )}

      {(approve.error || reject.error) && (
        <div className="error">{String((approve.error || reject.error).message)}</div>
      )}
      {overview.error && <div className="error">{String(overview.error.message)}</div>}

      <nav className="tabs" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => setTab(t.id)}
          >
            {t.label}
            {t.id === 'overview' && pendingCount > 0 && <span className="count">{pendingCount}</span>}
          </button>
        ))}
      </nav>

      {tab === 'overview' &&
        (data ? (
          <OverviewTab data={data} onApprove={onApprove} onReject={onReject} busyId={busyId} />
        ) : (
          <div className="empty">Loading…</div>
        ))}
      {tab === 'leagues' && <LeaguesTab leagues={data?.leagues} />}
      {tab === 'waivers' && <WaiversTab leagues={data?.leagues} />}
      {tab === 'actions' &&
        (actions.isLoading ? (
          <div className="empty">Loading…</div>
        ) : (
          <ActionsTab
            actions={actions.data}
            onApprove={onApprove}
            onReject={onReject}
            busyId={busyId}
          />
        ))}
    </div>
  )
}
