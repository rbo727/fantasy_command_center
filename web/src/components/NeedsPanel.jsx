import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { StatusBadge } from './StatusBadge'

const money = (v) => (v == null ? '—' : `$${Math.round(v)}`)
const pts = (v) => (v == null ? '—' : v.toFixed(1))

/* A hole that lasts one week is a rental; one that lasts months is a purchase.
   Showing the duration beside the cause is the whole point of this panel —
   it's what stops a bye patch being priced like a starter. */
function causeTone(need) {
  if (need.cause === 'bye') return 'warning'
  if (need.is_rental) return 'warning'
  return 'critical'
}

function Needs({ needs }) {
  if (!needs.length) {
    return (
      <div className="card">
        <StatusBadge tone="good" label="No holes in your starting lineup" />
        <p style={{ color: 'var(--text-secondary)', margin: '8px 0 0' }}>
          Everyone you're starting can play this week. Anything below is an upgrade,
          not a patch.
        </p>
      </div>
    )
  }
  return (
    <div className="card">
      <div className="platform">This week</div>
      <h3>Holes in your starting lineup</h3>
      <div className="table-wrap" style={{ marginTop: 10 }}>
        <table>
          <thead>
            <tr>
              <th>Slot</th>
              <th>Who</th>
              <th>Why</th>
              <th>Lasts</th>
            </tr>
          </thead>
          <tbody>
            {needs.map((n, i) => (
              <tr key={`${n.slot}-${i}`} className={n.is_rental ? '' : 'problem'}>
                <td>{n.slot}</td>
                <td>{n.incumbent || <em style={{ color: 'var(--text-muted)' }}>empty</em>}</td>
                <td>
                  <StatusBadge tone={causeTone(n)} label={n.cause} />
                </td>
                <td>{n.is_rental ? 'this week only' : `~${n.weeks} weeks`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p style={{ color: 'var(--text-muted)', fontSize: 13, marginBottom: 0 }}>
        A one-week hole is a rental — pay rental money. A multi-week hole keeps paying
        every week it's filled.
      </p>
    </div>
  )
}

export function NeedsPanel({ leagueKey, position }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['targets', leagueKey, position],
    queryFn: () => api.targets(leagueKey, position),
    enabled: Boolean(leagueKey),
    retry: false,
  })

  if (isLoading) return <div className="empty">Working out what your lineup needs…</div>
  if (error) return <div className="error">{String(error.message)}</div>
  if (!data) return null

  return (
    <div className="stack">
      <Needs needs={data.needs} />

      <div className="card">
        <div className="platform">
          {data.available_count} available · {data.teams_alive} teams alive ·{' '}
          {money(data.budget.remaining)} left
        </div>
        <h3>Worth bidding on, for your roster</h3>

        {data.targets.length === 0 ? (
          <div className="empty">
            Nothing on the wire beats what you already have at the positions you need.
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Player</th>
                  <th>Pos</th>
                  <th style={{ textAlign: 'right' }}>Proj</th>
                  <th style={{ textAlign: 'right' }}>vs next free</th>
                  <th>Fills</th>
                  <th style={{ textAlign: 'right' }}>Typical bid</th>
                </tr>
              </thead>
              <tbody>
                {data.targets.map((t) => (
                  <tr key={`${t.name}-${t.position}`}>
                    <td>
                      {t.name}
                      {t.rental_warning && (
                        <div style={{ marginTop: 4 }}>
                          <StatusBadge tone="warning" label="rental — don't overpay" />
                        </div>
                      )}
                    </td>
                    <td>{t.position}</td>
                    <td className="num">{pts(t.projected)}</td>
                    <td className="num">
                      {t.value_over_free == null
                        ? '—'
                        : `${t.value_over_free >= 0 ? '+' : ''}${t.value_over_free.toFixed(1)}`}
                    </td>
                    <td>
                      {t.fills
                        ? `${t.fills.slot} (${t.fills.is_rental ? '1wk' : `${t.fills.weeks}wk`})`
                        : '—'}
                    </td>
                    <td className="num">{money(t.typical_bid)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {data.targets.slice(0, 5).map((t) => (
          <details key={`why-${t.name}`} style={{ marginTop: 10 }}>
            <summary style={{ cursor: 'pointer', color: 'var(--text-secondary)' }}>
              Why {t.name}
            </summary>
            <ul style={{ color: 'var(--text-secondary)', marginTop: 6, paddingLeft: 18 }}>
              {t.reasons.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
            {t.rental_warning && (
              <div className="error" style={{ marginTop: 8, marginBottom: 0 }}>
                {t.rental_warning}
              </div>
            )}
          </details>
        ))}
      </div>
    </div>
  )
}
