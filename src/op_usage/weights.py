"""Map a git SHA to a driving-model weights fingerprint (GitHub, cached).

Used only for branch `master`. A fingerprint is the sorted blob SHAs of
driving weight files under selfdrive/modeld/models (live tree first,
then the pre-move openpilot/ prefix). dmonitoring / docs / code do not
count. Nightly caches hits and confirmed misses in sqlite so repeat
generates do not hammer GitHub.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Callable

import requests

from op_usage.cache import Cache

log = logging.getLogger(__name__)

DEFAULT_REPO = "commaai/openpilot"
GITHUB_API = "https://api.github.com"
MODELS_DIRS = (
    "selfdrive/modeld/models",
    "openpilot/selfdrive/modeld/models",
)
MISSING_WEIGHTS = "-"
_WEIGHT_SUFFIX = re.compile(r"\.(onnx|pkl|thneed|dlc|chunkmanifest)$", re.I)
_SSH = re.compile(r"^git@github\.com:(.+?)(?:\.git)?$")
_HTTPS = re.compile(r"^https://github\.com/(.+?)(?:\.git)?$")

WeightsLookup = Callable[[str, str], str | None]


def github_repo_from_remote(remote: str) -> str:
    raw = (remote or "").strip()
    matched = _SSH.match(raw) or _HTTPS.match(raw)
    if matched:
        return matched.group(1).strip("/")
    return DEFAULT_REPO


def is_driving_weight(name: str) -> bool:
    n = (name or "").rsplit("/", 1)[-1]
    if not n or "dmonitor" in n.lower():
        return False
    return bool(_WEIGHT_SUFFIX.search(n))


def fingerprint_from_contents(entries: list[dict]) -> str | None:
    parts: list[str] = []
    for item in entries:
        if str(item.get("type") or "file") not in ("file", ""):
            continue
        name = str(item.get("name") or "")
        sha = str(item.get("sha") or "")
        if not sha or not is_driving_weight(name):
            continue
        parts.append(f"{name.lower()}={sha}")
    if not parts:
        return None
    parts.sort()
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


class GitHubWeightsClient:
    def __init__(
        self,
        token: str | None = None,
        session: requests.Session | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.token = token if token is not None else (
            os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None
        )
        self._session = session or requests.Session()
        self.timeout_s = timeout_s
        self._disabled = False

    @property
    def disabled(self) -> bool:
        return self._disabled

    def fingerprint(self, repo: str, sha: str) -> str | None:
        found, _confirmed = self.lookup(repo, sha)
        return found

    def lookup(self, repo: str, sha: str) -> tuple[str | None, bool]:
        """Return (fingerprint, confirmed).

        `confirmed` is True for a hit, or for a miss only when every models
        path is a 404 or a 200 with no driving-weight files. Transient
        errors (timeouts, 5xx, RequestException) and rate-limit disable
        are not confirmed — do not persist those misses.
        """
        if self._disabled or not repo or not sha:
            return None, False
        kinds: list[str] = []
        for path in MODELS_DIRS:
            if self._disabled:
                return None, False
            kind, entries = self._list_dir(repo, path, sha)
            if kind == "ok":
                found = fingerprint_from_contents(entries or [])
                if found:
                    return found, True
                kinds.append("empty")
            elif kind == "missing":
                kinds.append("missing")
            else:
                kinds.append("transient")
        if any(kind == "transient" for kind in kinds):
            return None, False
        return None, True

    def _list_dir(self, repo: str, path: str, ref: str) -> tuple[str, list[dict] | None]:
        url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "op-usage/0.1 (personal; weights fingerprint)",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = self._session.get(
                url, headers=headers, params={"ref": ref}, timeout=self.timeout_s
            )
        except requests.RequestException as exc:
            log.warning("GitHub weights lookup failed for %s@%s: %s", repo, ref[:12], exc)
            return "transient", None
        if resp.status_code in (403, 429):
            self._disabled = True
            log.warning(
                "GitHub rate limited on weights lookup; master SHAs stay per-commit this run"
            )
            return "transient", None
        if resp.status_code == 404:
            return "missing", None
        if resp.status_code >= 500 or not resp.ok:
            log.warning(
                "GitHub weights lookup HTTP %s for %s@%s",
                resp.status_code,
                repo,
                ref[:12],
            )
            return "transient", None
        payload = resp.json()
        if isinstance(payload, dict):
            return "ok", [payload]
        if isinstance(payload, list):
            return "ok", [item for item in payload if isinstance(item, dict)]
        return "transient", None


def make_weights_lookup(
    cache: Cache,
    *,
    client: GitHubWeightsClient | None = None,
) -> WeightsLookup:
    """SHA → fingerprint. Cache hits (including confirmed misses) skip the network."""
    memo: dict[str, str | None] = {}
    gh = client

    def lookup(commit: str, remote: str) -> str | None:
        key = (commit or "").lower()
        if not key:
            return None
        if key in memo:
            return memo[key]
        repo = github_repo_from_remote(remote)
        cached = cache.get_commit_weights(repo, key)
        if cached is not None:
            found = None if cached == MISSING_WEIGHTS else cached
            memo[key] = found
            return found
        nonlocal gh
        if gh is None:
            gh = GitHubWeightsClient()
        found, confirmed = gh.lookup(repo, commit)
        if found:
            cache.set_commit_weights(repo, key, found)
            cache.commit()
        elif confirmed and not gh.disabled:
            cache.set_commit_weights(repo, key, MISSING_WEIGHTS)
            cache.commit()
        memo[key] = found
        return found

    return lookup
