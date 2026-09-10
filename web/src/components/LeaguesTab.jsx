import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { PlayerStatusBadge, StatusBadge } from './StatusBadge'

function Tile({ value, label }) {
  return (
    <div className="tile">
      <div className="value">{value ?? '—'}</div>
      <div className="label">{label}</div>
    </div>
  )
}

function RosterTable({ leagueKey }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['roster', leagueKey],
    queryFn: () => api.roster(leagueKey),
    retry: false,
  })

  if (isLoading) return <div className="empty">Loading roster…</div>
  if (error) return <div className="error">{String(error.message)}</div>

  const starters = data.slots.filter((s) => s.starter)
  const bench = data.slots.filter((s) => !s.starter)

  const row = (slot, i) => (
    <tr key={`${slot.slot}-${i}`} className={slot.player && !slot.player.available ? 'problem' : ''}>
      <td>{slot.slot}</td>
      <td>{slot.player ? slot.player.name : <em style={{ color: 'var(--text-muted)' }}>empty</em>}</td>
      <td>{slot.player?.position || ''}</td>
      <td>{slot.player?.team || ''}</td>
      <td>{slot.player ? <PlayerStatusBadge status={slot.player.status} /> : null}</td>
    </tr>
  )

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Slot</th>
            <th>Player</th>
            <th>Pos</th>
            <th>Team</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {starters.map(row)}
          <tr>
            <td colSpan={5} style={{ color: 'var(--text-muted)', fontSize: 12, paddingTop: 14 }}>
              BENCH
            </td>
          </tr>
          {bench.map(row)}
        </tbody>
      </table>
    </div>
  )
}

export function LeaguesTab({ leagues }) {
  if (!leagues?.length) {
    return (
      <div className="empty">
        No leagues configured. Copy <code>config/leagues.example.yml</code> to{' '}
        <code>config/leagues.yml</code>.
      </div>
    )
  }

  return (
    <div className="stack">
      {leagues.map((lg) => (
        <div className="card" key={lg.key}>
          <div className="platform">{lg.platform}</div>
          <h3>{lg.name || lg.key}</h3>

          {/* "Not built yet" and "broken" must not look the same. */}
          {!lg.supported && (
            <div style={{ marginTop: 8 }}>
              <StatusBadge tone="neutral" label="Not wired up yet" />
              <div style={{ color: 'var(--text-muted)', fontSize: 13, marginTop: 4 }}>
                {lg.error}
              </div>
            </div>
          )}
          {lg.supported && lg.error && (
            <div className="error" style={{ marginTop: 10, marginBottom: 0 }}>
              {lg.error}
            </div>
          )}

          {lg.supported && !lg.error && (
            <>
              <div className="tiles">
                <Tile value={lg.record || '—'} label="Record" />
                <Tile value={lg.week ?? '—'} label="Week" />
                <Tile
                  value={lg.faab_remaining != null ? `$${lg.faab_remaining}` : '—'}
                  label="FAAB left"
                />
                <Tile value={lg.waiver_position ?? '—'} label="Waiver pos" />
              </div>
              <details style={{ marginTop: 12 }}>
                <summary style={{ cursor: 'pointer', color: 'var(--text-secondary)' }}>
                  Roster
                </summary>
                <div style={{ marginTop: 10 }}>
                  <RosterTable leagueKey={lg.key} />
                </div>
              </details>
            </>
          )}
        </div>
      ))}
    </div>
  )
}
