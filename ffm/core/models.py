"""Database schema.

The centrepiece is :class:`Action`. Every mutation this app makes to any
platform — a pick'em submission, a lineup swap, a FAAB bid — is a row here that
moves through a lifecycle and keeps its payload, its rationale, and the
post-write verification. If it isn't in this table, it didn't happen.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """A timezone-aware datetime that survives SQLite.

    SQLite has no native timestamp type and silently discards tzinfo, so a value
    stored as aware comes back naive and every ``expires_at <= now`` comparison
    raises TypeError. Since those comparisons guard the waiver and lineup
    deadlines, that failure would land at 3am on a Wednesday.

    Bind: coerce to UTC. Result: re-attach UTC. Naive input is assumed UTC
    rather than guessed at from the host's clock.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    pass


class ActionKind(enum.StrEnum):
    PICKEM_SUBMIT = "pickem_submit"
    SURVIVOR_PICK = "survivor_pick"
    LINEUP_SWAP = "lineup_swap"
    WAIVER_CLAIM = "waiver_claim"
    FREE_AGENT_ADD = "free_agent_add"
    DROP_PLAYER = "drop_player"
    TRADE = "trade"


class Tier(enum.StrEnum):
    """Autonomy tier. LOW may auto-execute; MONEY always needs approval."""

    LOW = "low"
    MONEY = "money"


class ActionStatus(enum.StrEnum):
    PROPOSED = "proposed"       # engine produced it; awaiting gate
    APPROVED = "approved"       # cleared to execute (auto or by you)
    REJECTED = "rejected"       # you said no
    SUBMITTED = "submitted"     # sent to the platform, not yet confirmed
    VERIFIED = "verified"       # re-read the platform and it agrees
    FAILED = "failed"           # submission errored
    UNVERIFIED = "unverified"   # submitted, but read-back disagreed or errored
    SKIPPED = "skipped"         # no longer applicable (game locked, player rostered)
    DRY_RUN = "dry_run"         # payload built and recorded; nothing was sent


#: Which kinds may ever auto-execute. Mirrors the plan's policy table.
#: Anything absent from this mapping is treated as MONEY (fail safe).
TIER_POLICY: dict[ActionKind, Tier] = {
    ActionKind.PICKEM_SUBMIT: Tier.LOW,
    ActionKind.LINEUP_SWAP: Tier.LOW,
    ActionKind.SURVIVOR_PICK: Tier.MONEY,
    ActionKind.WAIVER_CLAIM: Tier.MONEY,
    ActionKind.FREE_AGENT_ADD: Tier.MONEY,
    ActionKind.DROP_PLAYER: Tier.MONEY,
    ActionKind.TRADE: Tier.MONEY,
}


def tier_for(kind: ActionKind) -> Tier:
    return TIER_POLICY.get(kind, Tier.MONEY)


class League(Base):
    """A league or pool we manage, mirrored from config/leagues.yml."""

    __tablename__ = "leagues"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    platform: Mapped[str] = mapped_column(String(32))
    league_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(200), default="")
    season: Mapped[int] = mapped_column(Integer)
    team_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    actions: Mapped[list[Action]] = relationship(back_populates="league")

    def __repr__(self) -> str:
        return f"<League {self.key} {self.platform}:{self.league_id}>"


class Action(Base):
    """A single intended mutation, with its full audit trail."""

    __tablename__ = "actions"
    __table_args__ = (
        Index("ix_actions_status_kind", "status", "kind"),
        Index("ix_actions_league_week", "league_id", "week"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[ActionKind] = mapped_column(Enum(ActionKind))
    tier: Mapped[Tier] = mapped_column(Enum(Tier))
    status: Mapped[ActionStatus] = mapped_column(
        Enum(ActionStatus), default=ActionStatus.PROPOSED, index=True
    )

    league_id: Mapped[int | None] = mapped_column(ForeignKey("leagues.id"), nullable=True)
    league: Mapped[League | None] = relationship(back_populates="actions")

    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: Stable natural key for this intent. Two runs that mean the same thing
    #: produce the same key, so a retried scheduler run cannot double-submit.
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    #: Exactly what we intend to send (or did send) to the platform.
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    #: Human-readable why. This is what shows up in the notification and the
    #: dashboard; an action you can't explain is an action you shouldn't take.
    rationale: Mapped[str] = mapped_column(Text, default="")

    #: What the platform said, and what a follow-up read-back observed.
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    #: Don't act on this after the game/waiver window locks.
    execute_after: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    def __repr__(self) -> str:
        return f"<Action {self.id} {self.kind.value} {self.status.value}>"

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            ActionStatus.VERIFIED,
            ActionStatus.REJECTED,
            ActionStatus.FAILED,
            ActionStatus.SKIPPED,
            ActionStatus.DRY_RUN,
        }


class Recommendation(Base):
    """A raw input from a data source, before any decision is made.

    Kept separate from Action so we can audit *why* a pick was made even after
    the source page changes or the subscription lapses.
    """

    __tablename__ = "recommendations"
    __table_args__ = (
        UniqueConstraint("source", "kind", "subject", "season", "week", name="uq_reco"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)   # fantasyguru, odds, espn_public
    kind: Mapped[str] = mapped_column(String(64))                 # pickem, survivor, faab, ranking
    subject: Mapped[str] = mapped_column(String(200))             # "BAL@KC", player id, team code
    season: Mapped[int] = mapped_column(Integer)
    week: Mapped[int | None] = mapped_column(Integer, nullable=True)

    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    def __repr__(self) -> str:
        return f"<Reco {self.source}/{self.kind} {self.subject} w{self.week}>"


class RunLog(Base):
    """One scheduled job execution. The dashboard's heartbeat comes from here."""

    __tablename__ = "run_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(100), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="running")  # running|ok|error
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<RunLog {self.job} {self.status}>"
