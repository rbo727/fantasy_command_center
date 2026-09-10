"""Encrypted secret storage.

Every credential this app holds — Yahoo OAuth tokens, ESPN cookies, the Sleeper
bearer token, FantasyGuru login — lives in a single Fernet-encrypted blob on
disk. The key never lives beside it: it comes from ``FFM_MASTER_KEY`` in the
environment, provisioned separately on each host.

Two rules this module exists to enforce:

1. A secret value is never written to a log, a traceback, or stdout. Callers get
   the value; everything else gets the *name*.
2. The encrypted blob is gitignored and the plaintext never touches disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ffm.core.config import get_settings


class SecretsError(RuntimeError):
    """Raised for missing keys, missing secrets, or a bad master key."""


class SecretStore:
    """Read/write access to the encrypted secret blob."""

    def __init__(self, path: Path | None = None, master_key: str | None = None) -> None:
        settings = get_settings()
        self.path = path or (settings.data_path / "secrets.enc")
        self._key = master_key or settings.master_key or os.environ.get("FFM_MASTER_KEY")

    # -- key handling -----------------------------------------------------
    @staticmethod
    def generate_key() -> str:
        """Mint a new master key. Store it in your password manager, not here."""
        return Fernet.generate_key().decode()

    def _fernet(self) -> Fernet:
        if not self._key:
            raise SecretsError(
                "FFM_MASTER_KEY is not set. Generate one with `ffm secrets init` "
                "and export it in the environment (or .env) before continuing."
            )
        try:
            return Fernet(self._key.encode() if isinstance(self._key, str) else self._key)
        except (ValueError, TypeError) as exc:
            raise SecretsError("FFM_MASTER_KEY is not a valid Fernet key.") from exc

    # -- storage ----------------------------------------------------------
    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self._fernet().decrypt(self.path.read_bytes()).decode())
        except InvalidToken as exc:
            raise SecretsError(
                f"Could not decrypt {self.path.name} — wrong FFM_MASTER_KEY for this file."
            ) from exc

    def _save(self, data: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        blob = self._fernet().encrypt(json.dumps(data, indent=None).encode())
        # Write via a temp file in the same dir so an interrupted write can't
        # truncate a working secret store.
        tmp = self.path.with_suffix(".enc.tmp")
        tmp.write_bytes(blob)
        tmp.replace(self.path)
        self.path.chmod(0o600)

    # -- public API -------------------------------------------------------
    def get(self, name: str, default: str | None = None) -> str | None:
        """Fetch a secret. Env var FFM_SECRET_<NAME> wins, for CI and one-offs."""
        env = os.environ.get(f"FFM_SECRET_{name.upper()}")
        if env:
            return env
        return self._load().get(name, default)

    def require(self, name: str) -> str:
        value = self.get(name)
        if not value:
            raise SecretsError(
                f"Required secret {name!r} is not set. Add it with `ffm secrets set {name}`."
            )
        return value

    def set(self, name: str, value: str) -> None:
        data = self._load()
        data[name] = value
        self._save(data)

    def delete(self, name: str) -> None:
        data = self._load()
        data.pop(name, None)
        self._save(data)

    def names(self) -> list[str]:
        """Secret *names* only — deliberately never the values."""
        return sorted(self._load())

    def has(self, name: str) -> bool:
        return bool(self.get(name))


#: Canonical secret names, so a typo fails loudly at import rather than at 3am.
class Keys:
    YAHOO_CONSUMER_KEY = "yahoo_consumer_key"
    YAHOO_CONSUMER_SECRET = "yahoo_consumer_secret"
    YAHOO_OAUTH_JSON = "yahoo_oauth_json"

    ESPN_S2 = "espn_s2"
    ESPN_SWID = "espn_swid"

    SLEEPER_TOKEN = "sleeper_token"
    SLEEPER_USER_ID = "sleeper_user_id"

    FANTASYGURU_USERNAME = "fantasyguru_username"
    FANTASYGURU_PASSWORD = "fantasyguru_password"
    FANTASYGURU_COOKIES = "fantasyguru_cookies"

    ANTHROPIC_API_KEY = "anthropic_api_key"
    ODDS_API_KEY = "odds_api_key"


def get_store() -> SecretStore:
    return SecretStore()
