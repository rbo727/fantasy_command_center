"""Normalized shapes every platform is translated into.

Yahoo, Sleeper and ESPN each describe a roster differently, and each spells
injury status its own way. The dashboard, the lineup guardian and the FAAB
engine should never learn three vocabularies, so every connector translates into
the types here.

The important one is :class:`PlayerStatus`. Stage 3 decides whether to bench
someone by asking "is this player unavailable?", and that question has to mean
the same thing whether the answer came from Sleeper's ``"Sus"``, Yahoo's
``"O"``, or ESPN's ``"INJURY_RESERVE"``. :func:`normalize_status` is where those
meet, and an unrecognised value becomes ``UNKNOWN`` — never ``ACTIVE`` — so a
new spelling makes the guardian ask rather than silently start someone.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from fcc.core.teams import normalize as normalize_team


class PlayerStatus(enum.StrEnum):
    """Availability, in the only vocabulary the engines use."""

    ACTIVE = "active"
    QUESTIONABLE = "questionable"
    DOUBTFUL = "doubtful"
    OUT = "out"
    INJURED_RESERVE = "ir"
    SUSPENDED = "suspended"
    PUP = "pup"
    INACTIVE = "inactive"          # declared inactive ~90 min before kickoff
    BYE = "bye"
    NOT_ON_ROSTER = "not_on_roster"
    UNKNOWN = "unknown"

    @property
    def is_unavailable(self) -> bool:
        """Definitely cannot play. These are safe to bench automatically."""
        return self in {
            PlayerStatus.OUT,
            PlayerStatus.INJURED_RESERVE,
            PlayerStatus.SUSPENDED,
            PlayerStatus.PUP,
            PlayerStatus.INACTIVE,
            PlayerStatus.BYE,
            PlayerStatus.NOT_ON_ROSTER,
        }

    @property
    def is_doubtful(self) -> bool:
        """Might not play. Worth a warning, never an automatic swap."""
        return self in {PlayerStatus.QUESTIONABLE, PlayerStatus.DOUBTFUL, PlayerStatus.UNKNOWN}


#: Platform spellings -> canonical status. Keys are lowercased and stripped of
#: punctuation before lookup, so "Injury Reserve" and "INJURY_RESERVE" both land.
_STATUS_ALIASES: dict[str, PlayerStatus] = {
    # healthy
    "": PlayerStatus.ACTIVE,
    "active": PlayerStatus.ACTIVE,
    "a": PlayerStatus.ACTIVE,
    "healthy": PlayerStatus.ACTIVE,
    "ok": PlayerStatus.ACTIVE,
    # questionable / doubtful
    "q": PlayerStatus.QUESTIONABLE,
    "questionable": PlayerStatus.QUESTIONABLE,
    "gtd": PlayerStatus.QUESTIONABLE,
    "d": PlayerStatus.DOUBTFUL,
    "doubtful": PlayerStatus.DOUBTFUL,
    # out
    "o": PlayerStatus.OUT,
    "out": PlayerStatus.OUT,
    # injured reserve
    "ir": PlayerStatus.INJURED_RESERVE,
    "injuryreserve": PlayerStatus.INJURED_RESERVE,
    "injuredreserve": PlayerStatus.INJURED_RESERVE,
    "irr": PlayerStatus.INJURED_RESERVE,
    "injuryreservedesignatedforreturn": PlayerStatus.INJURED_RESERVE,
    # suspension / PUP / NFI
    "sus": PlayerStatus.SUSPENDED,
    "susp": PlayerStatus.SUSPENDED,
    "suspended": PlayerStatus.SUSPENDED,
    "suspension": PlayerStatus.SUSPENDED,
    "pup": PlayerStatus.PUP,
    "nfi": PlayerStatus.PUP,
    "nfim": PlayerStatus.PUP,
    "physicallyunabletoperform": PlayerStatus.PUP,
    # inactive / not playing
    "inactive": PlayerStatus.INACTIVE,
    "dnp": PlayerStatus.INACTIVE,
    "na": PlayerStatus.INACTIVE,
    "notactive": PlayerStatus.INACTIVE,
    "bye": PlayerStatus.BYE,
    # off a roster entirely
    "fa": PlayerStatus.NOT_ON_ROSTER,
    "freeagent": PlayerStatus.NOT_ON_ROSTER,
    "waivers": PlayerStatus.NOT_ON_ROSTER,
}


def normalize_status(raw: str | None) -> PlayerStatus:
    """Translate any platform's status string into a :class:`PlayerStatus`.

    Unrecognised input is ``UNKNOWN``, never ``ACTIVE``. That asymmetry is the
    whole point: a status spelling we've never seen must make the lineup
    guardian ask a human, not quietly conclude the player is fine to start.
    """
    if raw is None:
        return PlayerStatus.ACTIVE
    key = "".join(ch for ch in str(raw).lower() if ch.isalnum())
    return _STATUS_ALIASES.get(key, PlayerStatus.UNKNOWN)


@dataclass
class Player:
    player_id: str
    name: str
    position: str | None = None
    team: str | None = None                      # canonical NFL code
    status: PlayerStatus = PlayerStatus.ACTIVE
    status_detail: str = ""                      # the platform's raw wording
    bye_week: int | None = None
    projected_points: float | None = None

    #: Every position this player is eligible at. A dual-eligible RB/WR can
    #: fill slots a single `position` string would rule out, so the lineup
    #: guardian needs the full set rather than the primary label.
    positions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Normalize on construction so no connector can leak a raw abbreviation.
        if self.team:
            self.team = normalize_team(self.team) or self.team
        self.positions = [p.upper() for p in self.positions if p]
        if not self.positions and self.position:
            self.positions = [self.position.upper()]

    @property
    def available(self) -> bool:
        return not self.status.is_unavailable

    def eligible_at(self, allowed: set[str]) -> bool:
        return any(p in allowed for p in self.positions)


@dataclass
class RosterSlot:
    slot: str                      # QB, RB, WR, TE, FLEX, K, DEF, BN, IR
    player: Player | None = None
    starter: bool = False

    @property
    def empty(self) -> bool:
        return self.player is None


@dataclass
class Roster:
    league_key: str
    team_id: str
    team_name: str = ""
    week: int | None = None
    slots: list[RosterSlot] = field(default_factory=list)

    @property
    def starters(self) -> list[RosterSlot]:
        return [s for s in self.slots if s.starter]

    @property
    def bench(self) -> list[RosterSlot]:
        return [s for s in self.slots if not s.starter and s.slot != "IR"]

    def problem_starters(self) -> list[RosterSlot]:
        """Started players who cannot play. Stage 3's input."""
        return [s for s in self.starters if s.player and s.player.status.is_unavailable]

    def questionable_starters(self) -> list[RosterSlot]:
        return [s for s in self.starters if s.player and s.player.status.is_doubtful]


@dataclass
class Matchup:
    week: int | None = None
    opponent_name: str = ""
    points_for: float | None = None
    points_against: float | None = None
    projected_for: float | None = None
    projected_against: float | None = None


@dataclass
class LeagueSummary:
    key: str
    platform: str
    name: str = ""
    season: int | None = None
    week: int | None = None
    team_name: str = ""
    wins: int | None = None
    losses: int | None = None
    ties: int | None = None
    rank: int | None = None
    faab_remaining: int | None = None
    waiver_position: int | None = None

    @property
    def record(self) -> str:
        if self.wins is None or self.losses is None:
            return ""
        base = f"{self.wins}-{self.losses}"
        return f"{base}-{self.ties}" if self.ties else base


@runtime_checkable
class ReadConnector(Protocol):
    """What the dashboard needs from every platform.

    Deliberately read-only. Writes stay on the concrete connectors so nothing
    can mutate a league through the aggregation layer by accident.
    """

    platform: str

    def league_summary(self) -> LeagueSummary: ...

    def roster(self, week: int | None = None) -> Roster: ...

    def matchup(self, week: int | None = None) -> Matchup | None: ...
