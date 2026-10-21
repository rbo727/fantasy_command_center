import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { StatusBadge } from './StatusBadge'
import { NeedsPanel } from './NeedsPanel'

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
const range = (r) => (r ? `${money(r.low)}–${money(r.high)}` : '—')

const STATUS_TONE = { available: 'good', yours: 'good', rostered: 'neutral', unmatched: 'warning' }
const STATUS_LABEL = {
  available: 'available',
  yours: 'on your team',
  rostered: 'rostered',
  unmatched: 'not matched',
}

const faabRange = (lo, hi, unit) =>
  lo == null ? '—' : lo === hi ? `${unit(lo)}` : `${unit(lo)}–${unit(hi)}`
const pctUnit = (v) => `${v}%`
const dollarUnit = (v) => `$${v}`

/** Two lines of the write-up, full text on hover. */
function Blurb({ text }) {
  if (!text) return null
  return (
    <div
      title={text}
      style={{
        color: 'var(--text-muted)',
        fontSize: 12,
        marginTop: 2,
        display: '-webkit-box',
        WebkitLineClamp: 2,
        WebkitBoxOrient: 'vertical',
        overflow: 'hidden',
        whiteSpace: 'normal',
      }}
    >
      {text}
    </div>
  )
}

function TargetRows({ rows, withFaab }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Player</th>
            <th>Team</th>
            <th style={{ textAlign: 'right' }}>Owned*</th>
            {withFaab && <th style={{ textAlign: 'right' }}>FAAB %</th>}
            {withFaab && <th style={{ textAlign: 'right' }}>Your $</th>}
            <th>Here</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((t, i) => (
            <tr key={`${t.name}-${i}`}>
              <td style={{ whiteSpace: 'normal', minWidth: 220 }}>
                <strong>{t.name}</strong>
                <Blurb text={t.blurb} />
              </td>
              <td>{t.team || ''}</td>
              <td className="num">{t.owned_pct != null ? `${t.owned_pct}%` : '—'}</td>
              {withFaab && (
                <td className="num">{faabRange(t.faab_pct_low, t.faab_pct_high, pctUnit)}</td>
              )}
              {withFaab && (
                <td className="num">{faabRange(t.faab_low, t.faab_high, dollarUnit)}</td>
              )}
              <td>
                <StatusBadge tone={STATUS_TONE[t.status]} label={STATUS_LABEL[t.status]} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * FantasyGuru's weekly waiver report. FAAB % is the author's figure for
 * 12-team leagues, of the starting budget; "Your $" converts it at this
 * league's budget without rescaling for league size.
 */
function WaiverReport({ leagueKey }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['waiverReport', leagueKey],
    queryFn: () => api.waiverReport(leagueKey),
    enabled: Boolean(leagueKey),
    retry: false,
  })

  if (isLoading) return <div className="empty">Loading the waiver report…</div>
  if (error) return <div className="error">{String(error.message)}</div>
  if (!data?.available) {
    return (
      <div className="card">
        <div className="platform">FantasyGuru</div>
        <h3>Waiver report</h3>
        <div className="empty">
          Not fetched yet - run <code>fcc fantasyguru refresh</code>.
        </div>
      </div>
    )
  }

  const positions = ['QB', 'RB', 'WR', 'TE', 'K', 'DEF']
  const targets = data.targets.filter((t) => t.tier === 'target')
  const deeper = data.targets.filter((t) => t.tier === 'deeper')
  const byPos = (rows) =>
    [...positions, null]
      .map((pos) => [pos, rows.filter((t) => (t.position || null) === pos)])
      .filter(([, rs]) => rs.length)

  return (
    <div className="card">
      <div className="platform">FantasyGuru · Tyler Buecher</div>
      <h3>
        <a href={data.url} target="_blank" rel="noreferrer">
          {data.title}
        </a>
      </h3>
      <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>
        Fetched {new Date(data.fetched_at).toLocaleString()}. FAAB % is his figure for 12-team
        leagues; Your $ applies it to your ${data.budget_total} budget, not scaled for league size.
        *Owned is his Yahoo figure, not this league.
      </div>

      {data.priority.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>Favorite priority adds</div>
          <div className="table-wrap">
            <table>
              <tbody>
                {data.priority.map((p, i) => (
                  <tr key={`${p.name}-${i}`}>
                    <td>
                      <strong>{p.name}</strong>
                    </td>
                    <td>{p.position}</td>
                    <td>{p.team}</td>
                    <td className="num">{faabRange(p.faab_pct_low, p.faab_pct_high, pctUnit)}</td>
                    <td className="num">{faabRange(p.faab_low, p.faab_high, dollarUnit)}</td>
                    <td>
                      <StatusBadge tone={STATUS_TONE[p.status]} label={STATUS_LABEL[p.status]} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {byPos(targets).map(([pos, rows]) => (
        <div key={pos || 'other'} style={{ marginTop: 16 }}>
          <div style={{ fontWeight: 600, marginBottom: 6 }}>{pos || 'Other'}</div>
          <TargetRows rows={rows} withFaab />
        </div>
      ))}

      {deeper.length > 0 && (
        <details style={{ marginTop: 16 }}>
          <summary style={{ cursor: 'pointer', color: 'var(--text-secondary)' }}>
            Deeper formats ({deeper.length}) - named without a FAAB figure
          </summary>
          <div style={{ marginTop: 10 }}>
            <TargetRows rows={deeper} withFaab={false} />
          </div>
        </details>
      )}

      {data.defenses.length > 0 && (
        <details style={{ marginTop: 12 }}>
          <summary style={{ cursor: 'pointer', color: 'var(--text-secondary)' }}>
            D/ST streamers ({data.defenses.length})
          </summary>
          <ul style={{ paddingLeft: 18, marginTop: 8 }}>
            {data.defenses.map((d) => (
              <li key={d.name} style={{ marginBottom: 6 }}>
                <strong>
                  {d.name} {d.where} {d.opp}
                </strong>
                <Blurb text={d.note} />
              </li>
            ))}
          </ul>
        </details>
      )}

      {data.unparsed.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <StatusBadge tone="warning" label="Lines the parser didn't recognise" />
          <ul style={{ paddingLeft: 18, marginTop: 6, color: 'var(--text-secondary)' }}>
            {data.unparsed.map((l) => (
              <li key={l}>{l}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

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
function FreeAgents({ byPosition, position, expert, fgProjections }) {
  const positions = Object.keys(byPosition).sort()
  if (!positions.length) return null
  const shown = position && byPosition[position] ? [position] : positions

  return (
    <div className="card">
      <div className="platform">Free agents, this week's projection</div>
      <h3>What to spend right now</h3>
      <div style={{ color: 'var(--text-muted)', fontSize: 12 }}>
        {expert
          ? `FG rank: Jeff Mans' PPR board, fetched ${new Date(expert.fetched_at).toLocaleString()}. Blank = outside his board.`
          : 'FG rank unavailable - run `fcc fantasyguru refresh` to fetch the board.'}{' '}
        {fgProjections
          ? 'FG proj/ceiling use FantasyGuru\'s weekly projections, priced the same way as Sleeper\'s.'
          : 'FG projections not fetched yet.'}
      </div>

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
                  <th style={{ textAlign: 'right' }}>FG rank</th>
                  <th style={{ textAlign: 'right' }}>Proj (SLP)</th>
                  <th style={{ textAlign: 'right' }}>Proj (FG)</th>
                  <th style={{ textAlign: 'right' }}>VOR</th>
                  <th style={{ textAlign: 'right' }}>Market (65%)</th>
                  <th style={{ textAlign: 'right' }}>Ceiling (SLP)</th>
                  <th style={{ textAlign: 'right' }}>Ceiling (FG)</th>
                </tr>
              </thead>
              <tbody>
                {byPosition[pos].map((p) => (
                  <tr key={p.player_id}>
                    <td>{p.name}</td>
                    <td>{p.team || ''}</td>
                    <td className="num">{p.expert_rank ? `${pos}${p.expert_rank}` : '—'}</td>
                    <td className="num">{p.projected_points?.toFixed(1) ?? '—'}</td>
                    <td className="num">{p.fg_projected_points?.toFixed(1) ?? '—'}</td>
                    <td className="num">{p.vor != null ? p.vor.toFixed(1) : '—'}</td>
                    <td className="num">{money(p.market_price)}</td>
                    <td className="num">{range(p.recommend)}</td>
                    <td className="num">{range(p.fg_recommend)}</td>
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

      {/* Your own roster's holes come first: every other section here is a
          board of players, and which of them matter depends on what you need. */}
      <NeedsPanel leagueKey={key} position={position} />
      <PendingClaims leagueKey={key} />
      <WaiverReport leagueKey={key} />

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
            <FreeAgents
              byPosition={data.free_agents.by_position}
              position={position}
              expert={data.free_agents.expert}
              fgProjections={data.free_agents.fg_projections}
            />
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
