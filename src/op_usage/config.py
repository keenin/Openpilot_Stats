"""Load settings from env / ~/.config/op-usage/credentials.env (never from git)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _expand(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def load_credential_files() -> list[Path]:
    """Load first-found config files. Existing process env always wins."""
    loaded: list[Path] = []
    seen: set[Path] = set()
    explicit = os.environ.get("OP_USAGE_CONFIG")
    candidates: list[Path] = []
    if explicit:
        candidates.append(_expand(explicit))
    candidates.append(_expand("~/.config/op-usage/credentials.env"))
    candidates.append(Path.cwd() / ".env")
    for path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        _load_env_file(path)
        loaded.append(path)
    return loaded


@dataclass(frozen=True)
class Settings:
    comma_jwt: str | None
    dongle_id: str | None
    api_base: str
    cache_path: Path
    site_dir: Path
    display_tz: str
    owner_name: str
    backfill_start: str
    openpilot_path: Path | None
    cereal_path: Path | None
    cf_pages_project: str
    chunk_days: int
    recheck_hours: int
    files_min_interval_s: float
    request_timeout_s: float

    @property
    def has_auth(self) -> bool:
        return bool(self.comma_jwt and self.dongle_id)


def load_settings() -> Settings:
    load_credential_files()
    jwt = os.environ.get("COMMA_JWT") or None
    dongle = os.environ.get("DONGLE_ID") or None
    op_path = os.environ.get("OPENPILOT_PATH")
    cereal_path = os.environ.get("CEREAL_PATH")
    return Settings(
        comma_jwt=jwt.strip() if jwt else None,
        dongle_id=dongle.strip() if dongle else None,
        api_base=os.environ.get("COMMA_API_BASE", "https://api.commadotai.com").rstrip("/"),
        cache_path=_expand(os.environ.get("CACHE_PATH", "~/.cache/op-usage/op-usage.sqlite")),
        site_dir=_expand(os.environ.get("SITE_DIR", "./site")),
        display_tz=os.environ.get("DISPLAY_TZ", "America/Los_Angeles"),
        owner_name=os.environ.get("OWNER_NAME", "one driver"),
        backfill_start=os.environ.get("BACKFILL_START", "2018-01-01"),
        openpilot_path=_expand(op_path) if op_path else None,
        cereal_path=_expand(cereal_path) if cereal_path else None,
        cf_pages_project=os.environ.get("CF_PAGES_PROJECT", "op-usage"),
        chunk_days=int(os.environ.get("CHUNK_DAYS", "14")),
        recheck_hours=int(os.environ.get("RECHECK_HOURS", "24")),
        files_min_interval_s=float(os.environ.get("FILES_MIN_INTERVAL_S", "13")),
        request_timeout_s=float(os.environ.get("REQUEST_TIMEOUT_S", "60")),
    )
