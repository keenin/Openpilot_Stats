from __future__ import annotations

from op_usage.qlog import (
    CONTROLS_SOURCE,
    SELFDRIVE_SOURCE,
    EnabledSample,
    encode_synthetic_qlog,
    engaged_seconds,
    extract_engaged_time,
    pick_source,
)

NS = 1_000_000_000


def _samples(pattern: list[tuple[float, bool]], source: str = SELFDRIVE_SOURCE, dt: float = 1.0) -> list[EnabledSample]:
    """pattern is (duration_s, enabled) spans, sampled every dt seconds."""
    out: list[EnabledSample] = []
    t = 0.0
    for duration, enabled in pattern:
        end = t + duration
        while t < end - 1e-9:
            out.append(EnabledSample(int(round(t * NS)), enabled, source))
            t += dt
    out.append(EnabledSample(int(round(t * NS)), False, source))
    return out


def test_integral_counts_enabled_spans_only() -> None:
    samples = _samples([(10, True), (5, False), (8, True)])
    # 10s engaged + 8s engaged
    assert abs(engaged_seconds(samples) - 18.0) < 1e-6


def test_gaps_above_max_are_skipped() -> None:
    samples = [
        EnabledSample(0, True, SELFDRIVE_SOURCE),
        EnabledSample(int(20 * NS), True, SELFDRIVE_SOURCE),
    ]
    assert engaged_seconds(samples, max_gap_s=5.0) == 0.0


def test_selfdrive_state_wins_over_controls() -> None:
    mixed = [
        EnabledSample(0, True, CONTROLS_SOURCE),
        EnabledSample(NS, True, SELFDRIVE_SOURCE),
        EnabledSample(2 * NS, False, SELFDRIVE_SOURCE),
    ]
    chosen, source = pick_source(mixed)
    assert source == SELFDRIVE_SOURCE
    assert all(s.source == SELFDRIVE_SOURCE for s in chosen)


def test_roundtrip_synthetic_qlog_bz2() -> None:
    samples = _samples([(2.0, True), (1.0, False), (3.0, True)])
    blob = encode_synthetic_qlog(samples, compress="bz2")
    assert blob.startswith(b"BZh")
    result = extract_engaged_time(blob)
    assert result.source == SELFDRIVE_SOURCE
    assert abs(result.engaged_time_s - 5.0) < 1e-6


def test_controls_state_fallback_on_synthetic_qlog() -> None:
    samples = _samples([(4.0, True), (1.0, True)], source=CONTROLS_SOURCE)
    blob = encode_synthetic_qlog(samples, compress=None)
    result = extract_engaged_time(blob)
    assert result.source == CONTROLS_SOURCE
    assert abs(result.engaged_time_s - 5.0) < 1e-6
