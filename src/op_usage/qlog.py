"""Engaged-time and not-in-park-time extraction from openpilot qlogs.

Access path:
  Parse/backfill reads `{qlog_dir}/{dongle}/{route_id}/{seg}.qlog` only.
  Comma GET /v1/route/{routeName}/files + CDN GET happen in `sync-qlogs`,
  not inside the parse loop.

  1. (sync) GET /v1/route/{routeName}/files  → payload["qlogs"] signed URLs
  2. (sync) HTTP GET each URL (commadata blob; JWT is in the signed query)
     Atomic write to the local store.
  3. (parse) Read local bytes. Decompress bz2 or zstd (magic-byte detect,
     same as openpilot LogReader)
  4. Parse concatenated Cap'n Proto Event messages

Fields used:
  Prefer  selfdriveState.enabled   (openpilot ~0.9.7+, Event union @130)
  Fallback controlsState.enabled   (older logs; cereal field @19, now under
                                    ControlsState.deprecated.enabled)
  Park    carState.gearShifter     (Event union @22, CarState field @14)
            cereal GearShifter.park @1 (opendbc car.capnp). Only `park` is
            excluded from the engage-% denominator; unknown and every other
            gear count as not-in-park. parkingBrake is a different signal.
  Speed   carState.vEgo            (m/s → mph) while engaged, for weighted time
  Set     carState.vCruise (kph) if the reader schema has it; else
          carState.cruiseState.speed (m/s). 255 kph is unset.

Engage time and not-in-park time are integrals over logMonoTime (nanoseconds),
not sample counts. Gaps larger than MAX_GAP_S are skipped (segment holes).
Weighted engaged time applies the steady-speed nerf to enabled spans only.

If an openpilot checkout is on OPENPILOT_PATH, cereal.log.Event is used.
Otherwise the bundled stub schema (schemas/engaged.capnp) is used. The stub
must keep Event.valid @67 *outside* the union — cereal does that, and putting
@67 in the union makes which() report selfdriveState as u129.
"""

from __future__ import annotations

import bz2
import importlib
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import zstandard as zstd

from op_usage.steady import MS_TO_MPH, Tick, set_speed_mph, weighted_engaged_seconds

log = logging.getLogger(__name__)

# Bumped when the qlog → sqlite mapping changes (weighted engaged time).
PARSER_VERSION = 2

# qlogs are decimated; 5s covers typical 1–10 Hz selfdriveState without
# counting a new ignition as engaged time.
MAX_GAP_S = 5.0
NS = 1_000_000_000.0

SELFDRIVE_SOURCE = "selfdriveState.enabled"
CONTROLS_SOURCE = "controlsState.enabled"
GEAR_SOURCE = "carState.gearShifter"
NONE_SOURCE = "none"

# cereal / opendbc CarState.GearShifter.park @1 (unknown @0, drive @2, …).
PARK_GEAR_RAW = 1
PARK_GEAR_NAMES = frozenset({"park"})


@dataclass(frozen=True)
class EnabledSample:
    log_mono_ns: int
    enabled: bool
    source: str
    v_ego_ms: float | None = None
    cruise_speed_ms: float | None = None
    v_cruise_kph: float | None = None


@dataclass(frozen=True)
class MotionSample:
    log_mono_ns: int
    v_ego_ms: float | None
    set_mph: float | None = None


@dataclass(frozen=True)
class EngagedResult:
    engaged_time_s: float
    source: str
    sample_count: int
    not_in_park_time_s: float | None = None
    gear_sample_count: int = 0
    gear_source: str = NONE_SOURCE
    weighted_engaged_time_s: float | None = None
    steady_frac: float | None = None
    parser_version: int = PARSER_VERSION
    speed_sample_count: int = 0


def decompress_qlog(data: bytes) -> bytes:
    """Match openpilot tools.lib.logreader._LogFileReader decompression."""
    if data.startswith(b"BZh"):
        return bz2.decompress(data)
    if data.startswith(b"\x28\xb5\x2f\xfd"):
        dctx = zstd.ZstdDecompressor()
        with dctx.stream_reader(data) as reader:
            return reader.read()
    return data


def engaged_seconds(samples: Iterable[EnabledSample], max_gap_s: float = MAX_GAP_S) -> float:
    """Integrate enabled across consecutive samples. Pure function — no capnp."""
    ordered = sorted(samples, key=lambda s: s.log_mono_ns)
    if len(ordered) < 2:
        return 0.0
    total = 0.0
    for a, b in zip(ordered, ordered[1:]):
        if not a.enabled:
            continue
        dt = (b.log_mono_ns - a.log_mono_ns) / NS
        if 0 < dt <= max_gap_s:
            total += dt
    return total


def pick_source(samples: list[EnabledSample]) -> tuple[list[EnabledSample], str]:
    """If any selfdriveState samples exist, ignore controlsState entirely."""
    for source in (SELFDRIVE_SOURCE, CONTROLS_SOURCE):
        chosen = [s for s in samples if s.source == source]
        if chosen:
            return chosen, source
    return [], NONE_SOURCE


def extract_engaged_time(qlog_bytes: bytes, event_mod: Any | None = None) -> EngagedResult:
    return extract_engaged_time_from_qlogs((qlog_bytes,), event_mod)


def extract_engaged_time_from_qlogs(blobs: Iterable[bytes], event_mod: Any | None = None) -> EngagedResult:
    """Concatenate samples across a route's qlog segments."""
    if event_mod is None:
        event_mod = load_event_module()
    samples: list[EnabledSample] = []
    motion: list[MotionSample] = []
    for blob in blobs:
        for event in _iter_events(decompress_qlog(blob), event_mod):
            samples.extend(_event_to_samples(event))
            m = _event_to_motion(event)
            if m is not None:
                motion.append(m)
    return _result_from_samples(samples, motion)


def _usable_not_in_park_s(gear: list[EnabledSample], engaged_time_s: float) -> float | None:
    """Gear integral, or None when it is not a usable denominator.

    Fewer than two samples cannot integrate. 0.0 while engaged_time_s > 0
    is also unusable (always-park, or a degenerate integral).
    """
    if len(gear) < 2:
        return None
    value = engaged_seconds(gear)
    if value == 0.0 and engaged_time_s > 0:
        return None
    return value


def _result_from_samples(
    samples: list[EnabledSample],
    motion: list[MotionSample] | None = None,
) -> EngagedResult:
    chosen, source = pick_source(samples)
    gear = [s for s in samples if s.source == GEAR_SOURCE]
    engaged = engaged_seconds(chosen)
    motion = list(motion or [])
    weighted = None
    if engaged > 0 and motion:
        ticks = ticks_from_streams(chosen, motion)
        weighted = weighted_engaged_seconds(ticks, max_gap_s=MAX_GAP_S)
        if weighted is not None:
            weighted = min(engaged, max(0.0, weighted))
    steady = None
    if weighted is not None and engaged > 0:
        steady = 1.0 - weighted / engaged
    return EngagedResult(
        engaged_time_s=engaged,
        source=source,
        sample_count=len(chosen),
        not_in_park_time_s=_usable_not_in_park_s(gear, engaged),
        gear_sample_count=len(gear),
        gear_source=GEAR_SOURCE if gear else NONE_SOURCE,
        weighted_engaged_time_s=weighted,
        steady_frac=steady,
        parser_version=PARSER_VERSION,
        speed_sample_count=sum(1 for m in motion if m.v_ego_ms is not None),
    )


def ticks_from_streams(enabled: list[EnabledSample], motion: list[MotionSample]) -> list[Tick]:
    """Join enabled flags with last-known vEgo / set speed."""
    events: list[tuple[int, int, str, object]] = []
    for sample in enabled:
        events.append((sample.log_mono_ns, 1, "en", sample.enabled))
    for sample in motion:
        events.append((sample.log_mono_ns, 0, "mo", sample))
    events.sort()
    flag = False
    speed_mph: float | None = None
    set_mph: float | None = None
    ticks: list[Tick] = []
    last_t: float | None = None
    for t_ns, _ord, kind, payload in events:
        if kind == "mo":
            m = payload  # type: MotionSample
            if m.v_ego_ms is not None:
                speed_mph = m.v_ego_ms * MS_TO_MPH
            if m.set_mph is not None:
                set_mph = m.set_mph
        else:
            flag = bool(payload)
        t_s = t_ns / NS
        tick = Tick(t_s=t_s, enabled=flag, speed_mph=speed_mph, set_mph=set_mph)
        if last_t is not None and t_s == last_t and ticks:
            ticks[-1] = tick
        else:
            ticks.append(tick)
            last_t = t_s
    return ticks


def load_event_module(openpilot_path: Path | None = None, cereal_path: Path | None = None) -> Any:
    """Return a module/class with Event.read_multiple_bytes and Event.new_message."""
    cereal_event = _try_cereal(openpilot_path, cereal_path)
    if cereal_event is not None:
        log.info("qlog parser: using cereal.log.Event")
        return cereal_event
    stub = _load_stub_schema()
    log.info(
        "qlog parser: using bundled stub schema "
        "(Event.valid @67, selfdriveState @130, controlsState.enabled @19, "
        "carState.vEgo @1, cruiseState.speed @10/@1, gearShifter @22/@14 park@1)"
    )
    return stub


def _try_cereal(openpilot_path: Path | None, cereal_path: Path | None) -> Any | None:
    extra: list[str] = []
    if openpilot_path:
        extra.extend((str(openpilot_path), str(openpilot_path.parent)))
    if cereal_path:
        extra.extend((str(cereal_path), str(cereal_path.parent)))
    for path in extra:
        if path not in sys.path:
            sys.path.insert(0, path)
    for name in ("cereal.log", "openpilot.cereal.log"):
        try:
            return importlib.import_module(name).Event
        except Exception:
            continue
    return None


def _load_stub_schema() -> Any:
    import capnp

    schema = Path(__file__).resolve().parent / "schemas" / "engaged.capnp"
    capnp.remove_import_hook()
    return capnp.load(str(schema)).Event


def _iter_events(decompressed: bytes, event_cls: Any) -> Iterator[Any]:
    try:
        events = event_cls.read_multiple_bytes(decompressed)
    except Exception:
        log.warning("failed to start Event.read_multiple_bytes; empty result")
        return
    try:
        for event in events:
            yield event
    except Exception as exc:
        # Trailing corruption is common in truncated uploads.
        log.warning("stopped reading qlog events early: %s", exc)


def _iter_samples(decompressed: bytes, event_cls: Any) -> Iterator[EnabledSample]:
    for event in _iter_events(decompressed, event_cls):
        yield from _event_to_samples(event)


def _event_to_samples(event: Any) -> list[EnabledSample]:
    try:
        which = event.which()
        mono = int(event.logMonoTime)
    except Exception:
        return []
    try:
        if which == "selfdriveState":
            flag, source = _read_enabled(event.selfdriveState), SELFDRIVE_SOURCE
        elif which == "controlsState":
            flag, source = _read_enabled(event.controlsState), CONTROLS_SOURCE
        elif which == "carState":
            flag, source = _read_not_in_park(event.carState), GEAR_SOURCE
        else:
            return []
    except Exception:
        return []
    if flag is None:
        return []
    return [EnabledSample(mono, flag, source)]


def _event_to_motion(event: Any) -> MotionSample | None:
    try:
        if event.which() != "carState":
            return None
        mono = int(event.logMonoTime)
        car = event.carState
    except Exception:
        return None
    v_ego = _read_float(car, "vEgo")
    v_cruise = _read_float(car, "vCruise")
    cruise_ms = None
    try:
        cruise_ms = _read_float(car.cruiseState, "speed")
    except Exception:
        cruise_ms = None
    set_mph = set_speed_mph(v_cruise, cruise_ms)
    if v_ego is None and set_mph is None:
        return None
    return MotionSample(mono, v_ego, set_mph)


def _read_float(obj: Any, name: str) -> float | None:
    try:
        return float(getattr(obj, name))
    except Exception:
        return None


def _read_enabled(obj: Any) -> bool | None:
    """cereal moved ControlsState.enabled into deprecated :group (same ordinal @19)."""
    try:
        return bool(obj.enabled)
    except Exception:
        pass
    try:
        return bool(obj.deprecated.enabled)
    except Exception:
        return None


def _read_not_in_park(car_state: Any) -> bool | None:
    """True when gearShifter is not park. None if the field cannot be read."""
    try:
        gear = car_state.gearShifter
    except Exception:
        return None
    return not _gear_is_park(gear)


def _gear_is_park(gear: Any) -> bool:
    """Match cereal GearShifter.park by raw ordinal (@1) or enum name."""
    raw = getattr(gear, "raw", None)
    if raw is not None:
        try:
            return int(raw) == PARK_GEAR_RAW
        except (TypeError, ValueError):
            pass
    text = str(gear).rsplit(".", 1)[-1].strip().lower()
    if text in PARK_GEAR_NAMES:
        return True
    try:
        return int(text) == PARK_GEAR_RAW
    except (TypeError, ValueError):
        return False


def encode_synthetic_qlog(
    samples: list[EnabledSample],
    event_cls: Any | None = None,
    compress: str | None = "bz2",
) -> bytes:
    """Build a fixture qlog from samples (tests / demo). Uses the stub schema."""
    if event_cls is None:
        event_cls = _load_stub_schema()
    parts: list[bytes] = []
    for sample in samples:
        msg = event_cls.new_message()
        msg.logMonoTime = sample.log_mono_ns
        if sample.source == CONTROLS_SOURCE:
            cs = msg.init("controlsState")
            cs.enabled = sample.enabled
        elif sample.source == GEAR_SOURCE:
            car = msg.init("carState")
            car.gearShifter = "drive" if sample.enabled else "park"
            if sample.v_ego_ms is not None:
                car.vEgo = float(sample.v_ego_ms)
            if sample.cruise_speed_ms is not None:
                car.cruiseState.speed = float(sample.cruise_speed_ms)
            if sample.v_cruise_kph is not None:
                try:
                    car.vCruise = float(sample.v_cruise_kph)
                except Exception:
                    pass
        else:
            ss = msg.init("selfdriveState")
            ss.enabled = sample.enabled
        parts.append(msg.to_bytes())
    raw = b"".join(parts)
    if compress == "bz2":
        return bz2.compress(raw)
    if compress == "zst":
        return zstd.ZstdCompressor().compress(raw)
    return raw
