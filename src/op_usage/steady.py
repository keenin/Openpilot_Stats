"""Steady-speed weighting for engaged time.

Long holds at constant speed (freeway sits) decay toward a speed-dependent
floor after a fuse. Faster → shorter fuse, harder bite, lower floor.
Engage % stays raw; this only produces weighted_engaged_time_s.

Longitudinal only: speed, standstill, cruise set speed. Steering is ignored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MS_TO_MPH = 2.2369362920544
KPH_TO_MPH = 0.62137119223733

# openpilot VCruiseHelper: 255 kph means "unset".
V_CRUISE_UNSET_KPH = 250.0

MAX_GAP_S = 5.0
HOLD_CONFIRM_S = 2.0
TINY_ABS_MPH = 1.5
HOLD_BAND_MPH = 2.0
FULL_REL = 0.20
FULL_ABS_MPH = 5.0
PARTIAL_REL = 0.10
PARTIAL_AGE_PUSH = 0.30
STANDSTILL_MPH = 1.0
ROLLING_MPH = 3.0
FLOOR_MIN = 0.04
# 2.60 e^{-0.0497 v} fits 25→0.75, 45→0.28, 55→0.17, 70→0.08, 80→0.05.
FLOOR_AMP = 2.60
FLOOR_K = 0.0497
CRAWL_MPH = 15.0
FUSE_MIN_MINUTES = 0.5
FUSE_MAX_MINUTES = 15.0
DECAY_TAUS_PER_FUSE = 4.0


@dataclass(frozen=True)
class Tick:
    """One point on the engaged/speed timeline. steering_deg is ignored."""

    t_s: float
    enabled: bool
    speed_mph: float | None
    set_mph: float | None = None
    steering_deg: float = 0.0


def fuse_s(mph: float) -> float:
    """Seconds at full weight before the bite. Faster → shorter fuse."""
    if mph <= 0:
        return FUSE_MAX_MINUTES * 60.0
    minutes = 350.0 / mph - 4.0
    minutes = min(FUSE_MAX_MINUTES, max(FUSE_MIN_MINUTES, minutes))
    return minutes * 60.0


def floor_weight(mph: float) -> float:
    """Asymptotic per-second weight after a long hold. Never negative."""
    if mph <= CRAWL_MPH:
        return 1.0
    return max(FLOOR_MIN, min(1.0, FLOOR_AMP * math.exp(-FLOOR_K * mph)))


def decay_tau_s(mph: float) -> float:
    """Time constant of the post-fuse decay toward floor_weight(mph)."""
    return DECAY_TAUS_PER_FUSE * fuse_s(mph)


def integrate_weight(age_s: float, dt: float, mph: float) -> float:
    """Weighted seconds for a hold-age interval [age, age+dt) at `mph`."""
    if dt <= 0:
        return 0.0
    fuse = fuse_s(mph)
    floor = floor_weight(mph)
    tau = decay_tau_s(mph)
    t0, t1 = age_s, age_s + dt
    total = 0.0
    a, b = max(t0, 0.0), min(t1, fuse)
    if b > a:
        total += b - a
    a, b = max(t0, fuse), t1
    if b > a:
        if floor >= 1.0 or tau <= 0:
            total += (b - a) * floor
        else:
            # ∫ floor + (1-floor) exp(-(t-fuse)/tau) dt
            total += floor * (b - a) + (1.0 - floor) * tau * (
                math.exp(-(a - fuse) / tau) - math.exp(-(b - fuse) / tau)
            )
    return max(0.0, total)


def change_kind(old_mph: float, new_mph: float) -> str | None:
    """'full', 'partial', or None for a confirmed speed/set-speed jump."""
    delta = abs(new_mph - old_mph)
    if delta < TINY_ABS_MPH:
        return None
    base = max(abs(old_mph), 1.0)
    rel = delta / base
    if rel >= FULL_REL and delta >= FULL_ABS_MPH:
        return "full"
    if rel >= PARTIAL_REL:
        return "partial"
    return None


def _stronger(a: str | None, b: str | None) -> str | None:
    if a == "full" or b == "full":
        return "full"
    if a == "partial" or b == "partial":
        return "partial"
    return None


def set_speed_mph(v_cruise_kph: float | None, cruise_speed_ms: float | None) -> float | None:
    """Cruise set in mph from cereal fields. None if unset / unreadable.

    Prefer carState.vCruise (kph; 255 = unset). Fallback cruiseState.speed (m/s).
    """
    if v_cruise_kph is not None and 0.0 < v_cruise_kph < V_CRUISE_UNSET_KPH:
        return v_cruise_kph * KPH_TO_MPH
    if cruise_speed_ms is not None and cruise_speed_ms > 0.0:
        return cruise_speed_ms * MS_TO_MPH
    return None


def weighted_engaged_seconds(ticks: list[Tick], max_gap_s: float = MAX_GAP_S) -> float | None:
    """Integrate enabled time with the steady-speed nerf. None if no vEgo."""
    ordered = sorted(ticks, key=lambda t: t.t_s)
    if len(ordered) < 2:
        return None
    saw_speed = any(t.speed_mph is not None for t in ordered)
    if not saw_speed:
        return None

    hold_speed: float | None = None
    age_s = 0.0
    pending_speed: float | None = None
    pending_age = 0.0
    pending_kind: str | None = None
    pending_set: float | None = None
    pending_set_age = 0.0
    pending_set_kind: str | None = None
    last_set: float | None = None
    was_standstill = False
    weighted = 0.0
    used_speed = False

    def reset_pending() -> None:
        nonlocal pending_speed, pending_age, pending_kind
        pending_speed = None
        pending_age = 0.0
        pending_kind = None

    def reset_set_pending() -> None:
        nonlocal pending_set, pending_set_age, pending_set_kind
        pending_set = None
        pending_set_age = 0.0
        pending_set_kind = None

    def full_reset(speed: float) -> None:
        nonlocal hold_speed, age_s
        hold_speed = speed
        age_s = 0.0
        reset_pending()

    def partial_credit(speed: float) -> None:
        nonlocal hold_speed, age_s
        age_s = max(0.0, age_s - PARTIAL_AGE_PUSH * fuse_s(speed))
        hold_speed = speed
        reset_pending()

    def apply_kind(kind: str | None, speed: float) -> None:
        if kind == "full":
            full_reset(speed)
        elif kind == "partial":
            partial_credit(speed)

    for a, b in zip(ordered, ordered[1:]):
        dt = b.t_s - a.t_s
        if dt <= 0:
            continue
        if dt > max_gap_s:
            hold_speed = None
            age_s = 0.0
            was_standstill = False
            last_set = None
            reset_pending()
            reset_set_pending()
            continue
        if not a.enabled:
            hold_speed = None
            age_s = 0.0
            was_standstill = False
            reset_pending()
            reset_set_pending()
            continue

        speed = a.speed_mph
        if speed is None:
            continue
        used_speed = True

        if speed < STANDSTILL_MPH:
            was_standstill = True
            reset_pending()
            if hold_speed is None:
                hold_speed = speed
                age_s = 0.0
            weighted += integrate_weight(age_s, dt, hold_speed)
            age_s += dt
            if a.set_mph is not None:
                last_set = a.set_mph
            continue

        if was_standstill and speed >= ROLLING_MPH:
            full_reset(speed)
            was_standstill = False
        elif was_standstill:
            # still crawling off the line; keep the standstill flag
            pass
        else:
            was_standstill = False

        if hold_speed is None:
            full_reset(speed)

        planned_kind: str | None = None
        set_mph = a.set_mph
        if set_mph is not None:
            if last_set is None:
                last_set = set_mph
            else:
                skind = change_kind(last_set, set_mph)
                if skind is None or abs(set_mph - last_set) <= HOLD_BAND_MPH:
                    reset_set_pending()
                    if abs(set_mph - last_set) <= HOLD_BAND_MPH:
                        last_set = set_mph
                elif pending_set is not None and abs(set_mph - pending_set) <= HOLD_BAND_MPH:
                    pending_set_age += dt
                    if pending_set_age >= HOLD_CONFIRM_S and pending_set_kind:
                        planned_kind = pending_set_kind
                        last_set = pending_set
                        reset_set_pending()
                else:
                    pending_set = set_mph
                    pending_set_age = dt
                    pending_set_kind = skind
                    if pending_set_age >= HOLD_CONFIRM_S and pending_set_kind:
                        planned_kind = pending_set_kind
                        last_set = pending_set
                        reset_set_pending()

        assert hold_speed is not None
        speed_kind: str | None = None
        if abs(speed - hold_speed) <= HOLD_BAND_MPH:
            reset_pending()
        else:
            kind = change_kind(hold_speed, speed)
            if kind is None:
                reset_pending()
            elif pending_speed is not None and abs(speed - pending_speed) <= HOLD_BAND_MPH:
                pending_age += dt
                if pending_age >= HOLD_CONFIRM_S:
                    speed_kind = pending_kind
            else:
                pending_speed = speed
                pending_age = dt
                pending_kind = kind
                if pending_age >= HOLD_CONFIRM_S:
                    speed_kind = pending_kind

        chosen = _stronger(planned_kind, speed_kind)
        if chosen:
            apply_kind(chosen, speed)

        weighted += integrate_weight(age_s, dt, hold_speed)
        age_s += dt

    if not used_speed:
        return None
    return weighted
