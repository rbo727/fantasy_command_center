import { useState } from 'react'
import { ActionStatusBadge } from './StatusBadge'

export function ActionCard({ action, onApprove, onReject, busy }) {
  const [reason, setReason] = useState('')
  const decidable = action.needs_approval

  return (
    <div className="card">
      <div className="action">
        <div className="body">
          <div className="kind">
            {action.kind.replace(/_/g, ' ')}
            {action.week ? <span className="platform"> · week {action.week}</span> : null}
            {action.tier === 'money' && <span className="platform"> · needs approval</span>}
          </div>
          {/* You cannot approve what you cannot see: rationale and the exact
              payload are always shown, never summarised away. */}
          <div className="rationale">{action.rationale || 'No rationale recorded.'}</div>
          <pre className="payload">{JSON.stringify(action.payload, null, 2)}</pre>
          {action.error && (
            <div className="error" style={{ marginTop: 10, marginBottom: 0 }}>
              {action.error}
            </div>
          )}
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, alignItems: 'flex-end' }}>
          <ActionStatusBadge status={action.status} />
          {decidable && (
            <div className="buttons">
              <button
                className="act primary"
                disabled={busy}
                onClick={() => onApprove(action.id)}
              >
                Approve
              </button>
              <button className="act" disabled={busy} onClick={() => onReject(action.id, reason)}>
                Reject
              </button>
            </div>
          )}
          {decidable && (
            <input
              className="text-input"
              style={{ width: 190 }}
              placeholder="Reason (optional)"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          )}
        </div>
      </div>
    </div>
  )
}

export function ActionsTab({ actions, onApprove, onReject, busyId }) {
  if (!actions?.length) {
    return <div className="empty">No actions recorded yet.</div>
  }
  return (
    <div className="stack">
      {actions.map((a) => (
        <ActionCard
          key={a.id}
          action={a}
          onApprove={onApprove}
          onReject={onReject}
          busy={busyId === a.id}
        />
      ))}
    </div>
  )
}
