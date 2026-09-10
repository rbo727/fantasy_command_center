// Status is never colour alone: every badge carries a glyph and a word, so it
// survives colourblindness, greyscale printing and forced-colours mode.
// Each tone gets a distinct *shape* as well as a colour, so the badge still
// reads in greyscale. The glyph must not repeat the dot, or "good" renders as
// two identical dots and the shape channel carries nothing.
const TONES = {
  good: { glyph: '\u2713', tone: 'good' }, //      check
  warning: { glyph: '\u25B2', tone: 'warning' }, // triangle
  serious: { glyph: '\u25C6', tone: 'serious' }, // diamond
  critical: { glyph: '\u2715', tone: 'critical' }, // cross
  neutral: { glyph: '\u2013', tone: 'neutral' }, //  dash
}

// Player availability -> tone. Anything unrecognised is a warning, never "good".
const PLAYER_TONES = {
  active: 'good',
  questionable: 'warning',
  doubtful: 'serious',
  out: 'critical',
  ir: 'critical',
  suspended: 'critical',
  pup: 'critical',
  inactive: 'critical',
  bye: 'critical',
  not_on_roster: 'critical',
  unknown: 'warning',
}

const ACTION_TONES = {
  verified: 'good',
  approved: 'good',
  dry_run: 'neutral',
  proposed: 'warning',
  submitted: 'warning',
  unverified: 'critical',
  failed: 'critical',
  rejected: 'neutral',
  skipped: 'neutral',
}

export function StatusBadge({ tone = 'neutral', label }) {
  const spec = TONES[tone] || TONES.neutral
  return (
    <span className="status" data-tone={spec.tone}>
      <span className="dot" aria-hidden="true" />
      <span className="glyph" aria-hidden="true">
        {spec.glyph}
      </span>
      <span>{label}</span>
    </span>
  )
}

export function PlayerStatusBadge({ status }) {
  return <StatusBadge tone={PLAYER_TONES[status] || 'warning'} label={status.replace(/_/g, ' ')} />
}

export function ActionStatusBadge({ status }) {
  return <StatusBadge tone={ACTION_TONES[status] || 'neutral'} label={status.replace(/_/g, ' ')} />
}
