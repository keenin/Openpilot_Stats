from __future__ import annotations

from op_usage.steady import (
    Tick,
    floor_weight,
    fuse_s,
    weighted_engaged_seconds,
)

DT = 1.0


def _hold(
    duration_s: float,
    mph: float,
    *,
    t0: float = 0.0,
    enabled: bool = True,
    set_mph: float | None = None,
    dt: float = DT,
    steering: float | None = None,
) -> list[Tick]:
    n = int(round(duration_s / dt))
    ticks: list[Tick] = []
    for i in range(n + 1):
        steer = 0.0 if steering is None else steering
        ticks.append(
            Tick(
                t_s=t0 + i * dt,
                enabled=enabled,
                speed_mph=mph,
                set_mph=mph if set_mph is None else set_mph,
                steering_deg=steer,
            )
        )
    return ticks


def _concat(*parts: list[Tick]) -> list[Tick]:
    out: list[Tick] = []
    for part in parts:
        if not part:
            continue
        if out and abs(part[0].t_s - out[-1].t_s) < 1e-9:
            out.extend(part[1:])
        else:
            out.extend(part)
    return out


def _weighted(ticks: list[Tick]) -> float:
    value = weighted_engaged_seconds(ticks)
    assert value is not None
    return value


def test_fuse_and_floor_anchors() -> None:
    assert abs(fuse_s(70) / 60.0 - 1.0) < 1e-6
    assert abs(fuse_s(25) / 60.0 - 10.0) < 1e-6
    assert abs(fuse_s(15) / 60.0 - 15.0) < 1e-6
    assert abs(fuse_s(80) / 60.0 - 0.5) < 1e-6
    assert abs(fuse_s(45) / 60.0 - (350 / 45 - 4)) < 1e-6
    assert 2.2 < fuse_s(55) / 60.0 < 2.6
    assert floor_weight(15) == 1.0
    assert floor_weight(5) == 1.0
    assert abs(floor_weight(25) - 0.75) < 0.03
    assert abs(floor_weight(45) - 0.30) < 0.05
    assert abs(floor_weight(55) - 0.15) < 0.04
    assert abs(floor_weight(70) - 0.08) < 0.015
    assert abs(floor_weight(80) - 0.05) < 0.02
    assert floor_weight(120) >= 0.0
    assert floor_weight(120) <= floor_weight(80)


def test_90_min_flat_70_is_heavily_nerfed() -> None:
    w = _weighted(_hold(90 * 60, 70))
    assert 10 * 60 <= w <= 15 * 60, w
    assert w < 20 * 60


def test_20_min_25mph_with_changes_stays_near_raw() -> None:
    t0 = 0.0
    parts: list[list[Tick]] = []
    # Lights / speed changes every ~3 min: 25 → stop → 25 → 20 → 25 …
    pattern = [
        (180, 25.0),
        (20, 0.0),
        (160, 25.0),
        (180, 20.0),
        (180, 25.0),
        (20, 0.0),
        (160, 25.0),
        (180, 22.0),
    ]
    # 180+20+160+180+180+20+160+180 = 1080s = 18 min; pad to 20
    pattern.append((120, 25.0))
    for dur, mph in pattern:
        parts.append(_hold(dur, mph, t0=t0, set_mph=max(mph, 25.0) if mph else 25.0))
        t0 = parts[-1][-1].t_s
    ticks = _concat(*parts)
    raw = ticks[-1].t_s - ticks[0].t_s
    assert abs(raw - 20 * 60) < 2.0, raw
    w = _weighted(ticks)
    assert abs(w - raw) < 45.0, (w, raw)


def test_20_min_flat_25_only_a_little_off() -> None:
    raw = 20 * 60
    w = _weighted(_hold(raw, 25))
    assert raw - 90 <= w <= raw, w


def test_70_to_63_is_partial_not_full_reset() -> None:
    prior = 10 * 60
    after = 20 * 60
    partial = _weighted(
        _concat(_hold(prior, 70), _hold(after, 63, t0=prior))
    )
    via_disengage = _concat(
        _hold(prior, 70),
        _hold(2.0, 70, t0=prior, enabled=False),
        _hold(after, 63, t0=prior + 2.0),
    )
    full = _weighted(via_disengage)
    continue_70 = _weighted(_hold(prior + after, 70))
    assert partial > continue_70
    # Small bump: far closer to "no reset" than to a disengage full reset.
    assert (partial - continue_70) < 0.45 * (full - continue_70), (
        partial,
        continue_70,
        full,
    )
    assert partial < full


def test_70_to_56_is_full_reset() -> None:
    prior = 10 * 60
    after = 20 * 60
    stepped = _weighted(_concat(_hold(prior, 70), _hold(after, 56, t0=prior)))
    via_disengage = _weighted(
        _concat(
            _hold(prior, 70),
            _hold(2.0, 70, t0=prior, enabled=False),
            _hold(after, 56, t0=prior + 2.0),
        )
    )
    assert abs(stepped - via_disengage) / via_disengage < 0.08, (stepped, via_disengage)


def test_set_speed_steps_stay_expensive() -> None:
    t = 0.0
    parts = []
    for mph in (60.0, 50.0, 35.0):
        parts.append(_hold(45.0, mph, t0=t, set_mph=mph))
        t = parts[-1][-1].t_s
    ticks = _concat(*parts)
    raw = ticks[-1].t_s - ticks[0].t_s
    w = _weighted(ticks)
    assert w / raw > 0.95, (w, raw)


def test_set_70_to_60_then_40_min_gets_60_nerf() -> None:
    change = 30.0
    cruise = 40 * 60
    ticks = _concat(
        _hold(change, 70, set_mph=70),
        _hold(cruise, 60, t0=change, set_mph=60),
    )
    w = _weighted(ticks)
    flat_60 = _weighted(_hold(cruise, 60))
    raw = change + cruise
    assert w < 0.45 * raw, w
    # Change itself counts (not dropped); then the 60-mph bite applies.
    assert w > flat_60
    assert abs(w - (change + flat_60)) < 90.0, (w, flat_60)


def test_engaged_stop_then_go_resets() -> None:
    cruise = 8 * 60
    stop = 15.0
    flat = _weighted(_hold(2 * cruise + stop, 40))
    reset = _weighted(
        _concat(
            _hold(cruise, 40),
            _hold(stop, 0.0, t0=cruise, set_mph=40),
            _hold(cruise, 40, t0=cruise + stop),
        )
    )
    assert reset > flat * 1.05, (reset, flat)


def test_steering_wiggle_at_constant_70_does_not_reset() -> None:
    duration = 15 * 60
    flat = _hold(duration, 70)
    wiggly = []
    for i, tick in enumerate(flat):
        wiggly.append(
            Tick(
                t_s=tick.t_s,
                enabled=True,
                speed_mph=70.0,
                set_mph=70.0,
                steering_deg=30.0 if i % 2 == 0 else -25.0,
            )
        )
    assert abs(_weighted(flat) - _weighted(wiggly)) < 1e-9


def test_gap_over_5s_does_not_stitch_hold() -> None:
    piece = 5 * 60
    stitched = _weighted(_hold(2 * piece, 70))
    gapped = _concat(
        _hold(piece, 70),
        _hold(piece, 70, t0=piece + 6.0),
    )
    split = _weighted(gapped)
    assert split > stitched * 1.15, (split, stitched)


def test_1s_speed_blip_does_not_reset() -> None:
    prior = 8 * 60
    blip = [
        Tick(t_s=prior + 1.0, enabled=True, speed_mph=56.0, set_mph=70.0),
    ]
    rest = _hold(8 * 60, 70, t0=prior + 2.0)
    with_blip = _concat(_hold(prior, 70), blip, rest)
    flat = _weighted(_hold(prior + 2.0 + 8 * 60, 70))
    # One-second 70→56→70 must not look like a full reset.
    assert abs(_weighted(with_blip) - flat) / flat < 0.05


def test_no_speed_returns_none() -> None:
    ticks = [
        Tick(0.0, True, None),
        Tick(10.0, True, None),
    ]
    assert weighted_engaged_seconds(ticks) is None
