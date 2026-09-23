import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { StatusBadge } from './StatusBadge'

const POSITIONS = ['All', 'QB', 'RB', 'WR', 'TE', 'K', 'DEF']

function Tile({ value, label }) {
  return (
    <div className="tile">
      <div className="value">{value ?? '—'}</div>
      <div className="label">{label}</div>
    </div>
  )
}

const money = (v) => (v == null ? '—' : `$${Math.round(v)}`)
const pct = (v) => (v == null ? '—' : `${Math.round(v * 100)}%`)

/**
 * In a guillotine league this is the section that decides most bids: if no
 * surviving rival can match your budget, the price of a guaranteed win is a
 * fact rather than an estimate.
 */
function Guillotine({ g }) {
  if (!g) return null
  if (g.error) return <div className="error">Budget state unavailable — {g.error}</div>

  return (
    <div className="card">
      <div className="platform">Guillotine</div>
      <h3>Who can still outbid you</h3>

      <div className="tiles">
        <Tile value={money(g.my_remaining)} label="Your FAAB" />
        <Tile value={money(g.max_rival_budget)} label="Richest rival" />
        <Tile value={g.teams_alive ?? '—'} label="Teams alive" />
        <Tile value={pct(g.my_share)} label="Your share of live FAAB" />
        <Tile value={money(g.spend_cap)} label="Pacing cap" />
      </div>

      <div style={{ marginTop: 14 }}>
        {g.can_outbid_anyone ? (
          <StatusBadge
            tone="good"
            label={`${money(g.price_to_guarantee)} wins any player outright`}
          />
        ) : (
          <StatusBadge tone="warning" label="A rival can outbid you — no guaranteed win" />
        )}
      </div>

      <ul style={{ color: 'var(--text-secondary)', marginTop: 12, paddingLeft: 18 }}>
        {g.advice.map((line) => (
          <li key={line} style={{ marginBottom: 4 }}>
            {line}
          </li>
        ))}
      </ul>

      <details style={{ marginTop: 12 }}>
        <summary style={{ cursor: 'pointer', color: 'var(--text-secondary)' }}>
          Every team's remaining budget
        </summary>
        <div className="table-wrap" style={{ marginTop: 10 }}>
          <table>
            <thead>
              <tr>
                <th>Team</th>
                <th style={{ textAlign: 'right' }}>FAAB left</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {g.rosters.map((r) => (
                <tr key={r.roster_id}>
                  <td>{r.team_name || r.roster_id}</td>
                  <td className="num">{money(r.budget_remaining)}</td>
                  <td>
                    {r.is_me ? (
                      <strong>you</strong>
                    ) : r.likely_chopped ? (
                      /* Sleeper has no eliminated flag; this is inferred. */
                      <span style={{ color: 'var(--text-muted)' }}>
                        chopped (inferred: empty roster)
                      </span>
                    ) : (
                      ''
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  )
}

function Market({ data, position, setPosition }) {
  const m = data.market
  return (
    <div className="card">
      <div className="platform">{m.segment}</div>
      <h3>What winning has actually cost</h3>

      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', margin: '10px 0 4px' }}>
        {POSITIONS.map((p) => (
          <button
            key={p}
            className="act"
            style={{
              padding: '4px 10px',
              fontWeight: (position || 'All') === p ? 600 : 400,
              borderColor: (position || 'All') === p ? 'var(--accent)' : 'var(--border)',
            }}
            onClick={() => setPosition(p === 'All' ? null : p)}
          >
            {p}
          </button>
        ))}
      </div>

      <div className="tiles">
        <Tile value={money(m.median_winning_bid)} label="Median winner" />
        <Tile value={money(m.median_runner_up)} label="Median runner-up" />
        <Tile value={money(m.max_winning_bid)} label="Biggest bid" />
        <Tile value={pct(m.contest_rate)} label="Claims contested" />
        <Tile value={pct(m.median_overpay)} label="Winner's premium" />
        <Tile value={m.resolved} label="Claims in sample" />
      </div>

      {!m.usable && (
        <div style={{ marginTop: 10 }}>
          <StatusBadge tone="warning" label="Thin sample — indicative only" />
        </div>
      )}

      {data.ladder.length > 0 && (
        <div className="table-wrap" style={{ marginTop: 16 }}>
          <table>
            <thead>
              <tr>
                <th>If you bid</th>
                <th>Would have won</th>
                <th style={{ textAlign: 'right' }}>Of budget left</th>
              </tr>
            </thead>
            <tbody>
              {data.ladder.map((row) => (
                <tr key={row.probability} className={row.over_budget ? 'problem' : ''}>
                  <td>
                    {money(row.price)}
                    {row.over_budget ? ' (over budget)' : ''}
                  </td>
                  <td>{pct(row.probability)} of comparable claims</td>
                  <td className="num">{pct(row.share_of_remaining)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

const names = (ps) => ps.map((p) => p.name).join(', ') || '—'

/**
 * Your own queue. Pending claims are private to your account, so this needs
 * the Sleeper token; without it, it says so instead of showing an empty queue
 * that would look like "nothing queued".
 */
function PendingClaims({ leagueKey }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['waiverClaims', leagueKey],
    queryFn: () => api.waiverClaims(leagueKey),
    enabled: Boolean(leagueKey),
    retry: false,
  })

  return (
    <div className="card">
      <div className="platform">Your queue</div>
      <h3>Pending waiver claims</h3>

      {isLoading && <div className="empty">Reading your claims…</div>}
      {error && <div className="error">{String(error.message)}</div>}
      {data && !data.configured && (
        <div className="empty">
          No Sleeper token stored — run <code>fcc secrets set sleeper_token</code> to see
          pending claims.
        </div>
      )}

      {data?.configured && (
        <>
          {data.pending.length === 0 ? (
            <div className="empty">Nothing queued for the next clear.</div>
          ) : (
            <div className="table-wrap" style={{ marginTop: 10 }}>
              <table>
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Add</th>
                    <th>Drop</th>
                    <th style={{ textAlign: 'right' }}>Bid</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {data.pending.map((c, i) => (
                    <tr key={`${c.seq}-${i}`}>
                      <td>{c.seq ?? i + 1}</td>
                      <td>{names(c.adds)}</td>
                      <td>{names(c.drops)}</td>
                      <td className="num">{money(c.bid)}</td>
                      <td>{c.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {data.last_clear && (
            <details style={{ marginTop: 14 }}>
              <summary style={{ cursor: 'pointer', color: 'var(--text-secondary)' }}>
                Last clear (week {data.last_clear.leg})
              </summary>
              <div className="table-wrap" style={{ marginTop: 10 }}>
                <table>
                  <thead>
                    <tr>
                      <th>Result</th>
                      <th>Add</th>
                      <th>Drop</th>
                      <th style={{ textAlign: 'right' }}>Bid</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.last_clear.claims.map((c, i) => (
                      <tr key={i} className={c.status === 'failed' ? 'problem' : ''}>
                        <td>
                          <StatusBadge
                            tone={c.status === 'complete' ? 'good' : 'warning'}
                            label={c.status === 'complete' ? 'won' : 'lost'}
                          />
                        </td>
                        <td>{names(c.adds)}</td>
                        <td>{names(c.drops)}</td>
                        <td className="num">{money(c.bid)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          )}
        </>
      )}
    </div>
  )
}

/**
 * What's actually available right now, and what it's worth. `market_price` is
 * what this league's own history says a 65%-confidence bid costs at that
 * position; `recommend` is the value ceiling - what the player is worth to
 * you, independent of what winning costs. A player with no `recommend` has no
 * Sleeper projection this week, not a value of zero.
 */
function FreeAgents({ byPosition, position }) {
  const positions = Object.keys(byPosition).sort()
  if (!positions.length) return null
  const shown = position && byPosition[position] ? [position] : positions

  return (
    <div className="card">
      <div className="platform">Free agents, this week's projection</div>
      <h3>What to spend right now</h3>

      {shown.map((pos) => (
        <div key={pos} style={{ marginTop: pos === shown[0] ? 10 : 20 }}>
          {shown.length > 1 && (
            <div style={{ fontWeight: 600, marginBottom: 6 }}>{pos}</div>
          )}
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Player</th>
                  <th>Team</th>
                  <th style={{ textAlign: 'right' }}>Proj pts</th>
                  <th style={{ textAlign: 'right' }}>VOR</th>
                  <th style={{ textAlign: 'right' }}>Market (65%)</th>
                  <th style={{ textAlign: 'right' }}>Value ceiling</th>
                </tr>
              </thead>
              <tbody>
                {byPosition[pos].map((p) => (
                  <tr key={p.player_id}>
                    <td>{p.name}</td>
                    <td>{p.team || ''}</td>
                    <td className="num">{p.projected_points?.toFixed(1) ?? '—'}</td>
                    <td className="num">{p.vor != null ? p.vor.toFixed(1) : '—'}</td>
                    <td className="num">{money(p.market_price)}</td>
                    <td className="num">
                      {p.recommend
                        ? `${money(p.recommend.low)}–${money(p.recommend.high)}`
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ))}
    </div>
  )
}

export function WaiversTab({ leagues }) {
  const sleeper = (leagues || []).filter((l) => l.platform === 'sleeper' && !l.error)
  const [leagueKey, setLeagueKey] = useState(sleeper[0]?.key)
  const [position, setPosition] = useState(null)
  const key = leagueKey || sleeper[0]?.key

  const { data, isLoading, error } = useQuery({
    queryKey: ['faab', key, position],
    queryFn: () => api.faab(key, position),
    enabled: Boolean(key),
    retry: false,
  })

  if (!sleeper.length) {
    return <div className="empty">No readable Sleeper league. FAAB history is Sleeper-only so far.</div>
  }

  return (
    <div className="stack">
      {sleeper.length > 1 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {sleeper.map((l) => (
            <button
              key={l.key}
              className="act"
              style={{ borderColor: l.key === key ? 'var(--accent)' : 'var(--border)' }}
              onClick={() => setLeagueKey(l.key)}
            >
              {l.name || l.key}
            </button>
          ))}
        </div>
      )}

      <PendingClaims leagueKey={key} />

      {isLoading && <div className="empty">Reading the league's transaction history…</div>}
      {error && <div className="error">{String(error.message)}</div>}

      {data && (
        <>
          {data.format === 'guillotine' && <Guillotine g={data.guillotine} />}
          <Market data={data} position={position} setPosition={setPosition} />

          {data.free_agents?.error && (
            <div className="error">Free-agent board unavailable — {data.free_agents.error}</div>
          )}
          {data.free_agents?.by_position && (
            <FreeAgents byPosition={data.free_agents.by_position} position={position} />
          )}

          {data.top_claims.length > 0 && (
            <div className="card">
              <h3>Priciest claims so far</h3>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Wk</th>
                      <th>Player</th>
                      <th>Pos</th>
                      <th style={{ textAlign: 'right' }}>Won</th>
                      <th style={{ textAlign: 'right' }}>Runner-up</th>
                      <th style={{ textAlign: 'right' }}>Bidders</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.top_claims.map((c, i) => (
                      <tr key={`${c.player}-${c.week}-${i}`}>
                        <td>{c.week ?? '?'}</td>
                        <td>{c.player}</td>
                        <td>{c.position || ''}</td>
                        <td className="num">{money(c.winning_bid)}</td>
                        <td className="num">{money(c.runner_up)}</td>
                        <td className="num">{c.bidders}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
