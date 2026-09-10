import { ActionStatusBadge, StatusBadge } from './StatusBadge'
import { ActionCard } from './ActionsTab'

export function OverviewTab({ data, onApprove, onReject, busyId }) {
  const { needs_approval = [], needs_attention = [], league_errors = [] } = data

  const nothingToDo =
    needs_approval.length === 0 && needs_attention.length === 0 && league_errors.length === 0

  return (
    <div className="stack">
      {league_errors.map((row) => (
        <div className="error" key={row.key}>
          <strong>{row.key}</strong> could not be read — {row.error}
        </div>
      ))}

      <section>
        <h2 style={{ fontSize: 15, margin: '0 0 10px' }}>
          Waiting on you{' '}
          {needs_approval.length > 0 && <span className="count">{needs_approval.length}</span>}
        </h2>
        {needs_approval.length === 0 ? (
          <div className="empty">Nothing needs approval.</div>
        ) : (
          <div className="stack">
            {needs_approval.map((a) => (
              <ActionCard
                key={a.id}
                action={a}
                onApprove={onApprove}
                onReject={onReject}
                busy={busyId === a.id}
              />
            ))}
          </div>
        )}
      </section>

      {needs_attention.length > 0 && (
        <section>
          <h2 style={{ fontSize: 15, margin: '0 0 10px' }}>Needs attention</h2>
          <div className="stack">
            {needs_attention.map((a) => (
              <div className="card" key={a.id}>
                <div className="action">
                  <div className="body">
                    <div className="kind">{a.kind.replace(/_/g, ' ')}</div>
                    <div className="rationale">
                      {a.error || 'Submitted but not confirmed by a read-back.'}
                    </div>
                  </div>
                  <ActionStatusBadge status={a.status} />
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {nothingToDo && (
        <div className="card">
          <StatusBadge tone="good" label="All clear" />
          <p style={{ color: 'var(--text-secondary)', margin: '8px 0 0' }}>
            No approvals pending, no failed or unverified writes, every configured league
            readable.
          </p>
        </div>
      )}
    </div>
  )
}
