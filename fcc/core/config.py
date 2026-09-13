"""Application configuration.

Everything that varies between the home PC and the seedbox lives here, so the
deploy is a `git pull` plus environment variables — never a code edit.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Platform = Literal["yahoo", "sleeper", "espn", "espn_pickem"]


class LeagueConfig(BaseModel):
    """One league/pool, as declared in config/leagues.yml."""

    key: str = Field(description="Stable local identifier, e.g. 'sleeper_main'")
    platform: Platform
    league_id: str = Field(description="Platform-native league or challenge id")
    name: str = ""
    season: int
    enabled: bool = True

    #: Which entry in the league is yours. Each platform answers that
    #: differently, so this field is generic on purpose:
    #:   sleeper -> your USER id (rosters are matched by owner_id)
    #:   yahoo   -> the team key, e.g. "449.l.123456.t.4"
    #:   espn    -> the numeric team id within the league
    team_id: str | None = None

    #: Sleeper waivers clear at 3am ET Wednesday in the user's main league;
    #: other leagues differ, so this is per-league rather than global.
    waiver_clear_cron: str | None = None

    #: Free-form platform extras (challenge slug, scoring quirks, pool rules).
    extra: dict = Field(default_factory=dict)


class Settings(BaseSettings):
    """Process-wide settings, sourced from environment (prefix FCC_) and .env."""

    model_config = SettingsConfigDict(
        env_prefix="FCC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Runtime posture -------------------------------------------------
    #: Master safety switch. When true, no write ever leaves the process; the
    #: exact payload is recorded instead. Default ON so a fresh checkout, a
    #: fresh seedbox, or a forgotten env var can never touch a live league.
    dry_run: bool = True

    #: Timezone for every schedule. Pinned in config rather than inherited from
    #: the host so the seedbox can't quietly run the 3am job at the wrong 3am.
    timezone: str = "America/New_York"

    #: Where mutable runtime state lives (db, caches, captured HTML, cookies).
    #: Relative paths resolve against the repo root, never against $HOME, so
    #: the layout is identical on the home PC and the seedbox.
    data_dir: Path = Path("data")

    log_level: str = "INFO"

    # --- Autonomy --------------------------------------------------------
    #: Low-tier actions (pick'em submits, benching an OUT player) execute
    #: without asking. Set false to force everything through approval.
    auto_execute_low_tier: bool = True

    #: Money-tier actions (FAAB, survivor, drops, trades) ALWAYS require an
    #: explicit approval. There is deliberately no setting to disable this.

    # --- Notifications ---------------------------------------------------
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"
    discord_webhook_url: str | None = None

    # --- LLM (structuring FantasyGuru prose only) ------------------------
    anthropic_model: str = "claude-opus-5"

    # --- Secrets ---------------------------------------------------------
    #: urlsafe-base64 32-byte Fernet key. Provisioned out of band on each host;
    #: never committed, never logged.
    master_key: str | None = None

    # --- Config file locations -------------------------------------------
    leagues_file: Path = Path("config/leagues.yml")

    @property
    def repo_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    def resolve(self, p: Path) -> Path:
        """Resolve a possibly-relative config path against the repo root."""
        return p if p.is_absolute() else self.repo_root / p

    @property
    def data_path(self) -> Path:
        d = self.resolve(self.data_dir)
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_path / 'fcc.db'}"

    def leagues(self) -> list[LeagueConfig]:
        """Load league declarations. Missing file is not an error yet."""
        path = self.resolve(self.leagues_file)
        if not path.exists():
            return []
        raw = yaml.safe_load(path.read_text()) or {}
        return [LeagueConfig(**item) for item in raw.get("leagues", [])]

    def league(self, key: str) -> LeagueConfig:
        for lg in self.leagues():
            if lg.key == key:
                return lg
        raise KeyError(f"No league configured with key {key!r}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
