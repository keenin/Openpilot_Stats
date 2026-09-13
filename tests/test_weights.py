from __future__ import annotations

from pathlib import Path

import requests

from helpers import drive_row
from op_usage.cache import Cache
from op_usage.config import Settings
from op_usage.pipeline import generate_from_cache
from op_usage.weights import (
    MISSING_WEIGHTS,
    fingerprint_from_contents,
    github_repo_from_remote,
    is_driving_weight,
    make_weights_lookup,
    GitHubWeightsClient,
)


def test_repo_from_remote() -> None:
    assert github_repo_from_remote("git@github.com:commaai/openpilot.git") == "commaai/openpilot"
    assert github_repo_from_remote("https://github.com/foo/bar.git") == "foo/bar"
    assert github_repo_from_remote("") == "commaai/openpilot"


def test_driving_weight_names() -> None:
    assert is_driving_weight("driving_supercombo.onnx")
    assert is_driving_weight("big_driving_supercombo.onnx")
    assert is_driving_weight("driving_tinygrad.pkl")
    assert not is_driving_weight("dmonitoring_model.onnx")
    assert not is_driving_weight("README.md")
    assert not is_driving_weight("__init__.py")


def test_fingerprint_ignores_dmonitoring_and_docs() -> None:
    a = fingerprint_from_contents(
        [
            {"type": "file", "name": "README.md", "sha": "r1"},
            {"type": "file", "name": "dmonitoring_model.onnx", "sha": "d1"},
            {"type": "file", "name": "driving_supercombo.onnx", "sha": "w1"},
        ]
    )
    b = fingerprint_from_contents(
        [
            {"type": "file", "name": "driving_supercombo.onnx", "sha": "w1"},
            {"type": "file", "name": "dmonitoring_model.onnx", "sha": "d2"},
        ]
    )
    c = fingerprint_from_contents([{"type": "file", "name": "driving_supercombo.onnx", "sha": "w2"}])
    assert a and a == b
    assert c and c != a
    assert fingerprint_from_contents([{"type": "file", "name": "README.md", "sha": "r1"}]) is None


def _gh_resp(status: int, payload=None):
    class _Resp:
        def __init__(self) -> None:
            self.status_code = status
            self.ok = 200 <= status < 300
            self._payload = payload if payload is not None else {}

        def json(self):
            return self._payload

    return _Resp()


def test_github_client_tries_live_then_legacy_path() -> None:
    class _Sess:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def get(self, url, headers=None, params=None, timeout=None):
            self.urls.append(url)
            live = url.endswith("selfdrive/modeld/models") and "openpilot/selfdrive" not in url
            if live:
                return _gh_resp(404, {"message": "Not Found"})
            return _gh_resp(200, [{"type": "file", "name": "driving_supercombo.onnx", "sha": "abc"}])

    sess = _Sess()
    fp, confirmed = GitHubWeightsClient(token=None, session=sess).lookup("commaai/openpilot", "deadbeef")
    assert fp and confirmed
    assert "selfdrive/modeld/models" in sess.urls[0]
    assert "openpilot/selfdrive" not in sess.urls[0]
    assert any("openpilot/selfdrive/modeld/models" in u for u in sess.urls)


class _SeqSess:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.n = 0

    def get(self, url, headers=None, params=None, timeout=None):
        status = self.statuses[min(self.n, len(self.statuses) - 1)]
        self.n += 1
        if status == 200:
            return _gh_resp(200, [{"type": "file", "name": "README.md", "sha": "r1"}])
        return _gh_resp(status)


def test_lookup_confirm_rules() -> None:
    fp, confirmed = GitHubWeightsClient(token=None, session=_SeqSess([404, 404])).lookup("commaai/openpilot", "deadbeef")
    assert fp is None and confirmed is True

    fp, confirmed = GitHubWeightsClient(token=None, session=_SeqSess([200])).lookup("commaai/openpilot", "deadbeef")
    assert fp is None and confirmed is True

    fp, confirmed = GitHubWeightsClient(token=None, session=_SeqSess([404, 503])).lookup("commaai/openpilot", "deadbeef")
    assert fp is None and confirmed is False

    fp, confirmed = GitHubWeightsClient(token=None, session=_SeqSess([503])).lookup("commaai/openpilot", "deadbeef")
    assert fp is None and confirmed is False

    class _Timeout:
        def get(self, url, headers=None, params=None, timeout=None):
            raise requests.Timeout("boom")

    fp, confirmed = GitHubWeightsClient(token=None, session=_Timeout()).lookup("commaai/openpilot", "deadbeef")
    assert fp is None and confirmed is False

    limited = GitHubWeightsClient(token=None, session=_SeqSess([429]))
    fp, confirmed = limited.lookup("commaai/openpilot", "deadbeef")
    assert fp is None and confirmed is False
    assert limited.disabled is True


def test_weights_cache_confirmed_miss_only(tmp_path) -> None:
    class _Boom:
        def lookup(self, repo, sha):
            raise AssertionError("cache hit must not call GitHub")

    class _Client:
        def __init__(self, confirmed: bool) -> None:
            self.disabled = False
            self.calls = 0
            self.confirmed = confirmed

        def lookup(self, repo, sha):
            self.calls += 1
            return None, self.confirmed

    with Cache(tmp_path / "hit.sqlite") as cache:
        cache.set_commit_weights("commaai/openpilot", "abc", "fp-1")
        cache.commit()
        lookup = make_weights_lookup(cache, client=_Boom())
        assert lookup("ABC", "git@github.com:commaai/openpilot.git") == "fp-1"

    miss = _Client(True)
    with Cache(tmp_path / "miss.sqlite") as cache:
        lookup = make_weights_lookup(cache, client=miss)
        assert lookup("abc", "git@github.com:commaai/openpilot.git") is None
        assert miss.calls == 1
        assert cache.get_commit_weights("commaai/openpilot", "abc") == MISSING_WEIGHTS
        again = make_weights_lookup(cache, client=miss)
        assert again("abc", "git@github.com:commaai/openpilot.git") is None
        assert miss.calls == 1

    transient = _Client(False)
    with Cache(tmp_path / "transient.sqlite") as cache:
        lookup = make_weights_lookup(cache, client=transient)
        assert lookup("abc", "git@github.com:commaai/openpilot.git") is None
        assert cache.get_commit_weights("commaai/openpilot", "abc") is None
        again = make_weights_lookup(cache, client=transient)
        assert again("abc", "git@github.com:commaai/openpilot.git") is None
        assert transient.calls == 2


def test_generate_merges_master_via_lookup(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "op_usage.pipeline.make_weights_lookup",
        lambda cache, **k: (lambda commit, remote: "era"),
    )
    settings = Settings(
        comma_jwt=None,
        dongle_id=None,
        api_base="https://example.invalid",
        cache_path=tmp_path / "c.sqlite",
        site_dir=tmp_path / "site",
        display_tz="UTC",
        owner_name="t",
        backfill_start="2026-09-01",
        openpilot_path=None,
        cereal_path=None,
        cf_pages_project="op-usage",
        chunk_days=30,
        recheck_hours=24,
        files_min_interval_s=13,
        request_timeout_s=60,
    )
    with Cache(settings.cache_path) as cache:
        drives = [
            drive_row(
                route_name=f"m{i}",
                git_commit="aaaaaaa111111111111111111111111111111111" if i < 2 else "bbbbbbb222222222222222222222222222222222",
                git_branch="master",
                git_remote="git@github.com:commaai/openpilot.git",
                start_time_utc_ms=1000 + i,
            )
            for i in range(3)
        ]
        cache.replace_all(drives)
        path = generate_from_cache(settings, cache)
    html = Path(path).read_text(encoding="utf-8")
    assert "master" in html
    assert "aaaaaaa…bbbbbbb" in html
    assert html.count('class="commit"') == 1
    assert "aaaaaaa111111111111111111111111111111111 … bbbbbbb222222222222222222222222222222222" in html

