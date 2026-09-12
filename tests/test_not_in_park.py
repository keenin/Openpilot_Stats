from __future__ import annotations

from pathlib import Path

import capnp

from op_usage.qlog import (
    GEAR_SOURCE,
    SELFDRIVE_SOURCE,
    EnabledSample,
    encode_synthetic_qlog,
    engaged_seconds,
    extract_engaged_time,
    _gear_is_park,
    _load_stub_schema,
)

NS = 1_000_000_000


def _samples(pattern: list[tuple[float, bool]], source: str = GEAR_SOURCE, dt: float = 1.0) -> list[EnabledSample]:
    out: list[EnabledSample] = []
    t = 0.0
    for duration, enabled in pattern:
        end = t + duration
        while t < end - 1e-9:
            out.append(EnabledSample(int(round(t * NS)), enabled, source))
            t += dt
    out.append(EnabledSample(int(round(t * NS)), False, source))
    return out


def test_integral_skips_park_counts_drive_gear() -> None:
    """Park spans are excluded; drive (and other non-park) spans are included."""
    samples = _samples([(8, False), (10, True), (4, False), (6, True)])
    # 10s drive + 6s drive; 8s+4s park ignored
    assert abs(engaged_seconds(samples) - 16.0) < 1e-6


def test_integral_park_gaps_above_max_are_skipped() -> None:
    samples = [
        EnabledSample(0, True, GEAR_SOURCE),
        EnabledSample(int(20 * NS), True, GEAR_SOURCE),
    ]
    assert engaged_seconds(samples, max_gap_s=5.0) == 0.0


def test_extract_not_in_park_from_synthetic_qlog() -> None:
    engaged = _samples([(5.0, True)], source=SELFDRIVE_SOURCE)
    gear = _samples([(3.0, False), (7.0, True)], source=GEAR_SOURCE)
    blob = encode_synthetic_qlog(engaged + gear, compress=None)
    result = extract_engaged_time(blob)
    assert result.source == SELFDRIVE_SOURCE
    assert abs(result.engaged_time_s - 5.0) < 1e-6
    assert result.gear_source == GEAR_SOURCE
    assert result.not_in_park_time_s is not None
    assert abs(result.not_in_park_time_s - 7.0) < 1e-6


def test_extract_without_car_state_leaves_not_in_park_none() -> None:
    samples = _samples([(2.0, True)], source=SELFDRIVE_SOURCE)
    blob = encode_synthetic_qlog(samples, compress=None)
    result = extract_engaged_time(blob)
    assert result.not_in_park_time_s is None
    assert result.gear_sample_count == 0


def _gear_event(event_cls, mono_ns: int, gear_name: str):
    msg = event_cls.new_message()
    msg.logMonoTime = mono_ns
    msg.init("carState").gearShifter = gear_name
    return msg


def test_unknown_and_reverse_count_as_not_in_park() -> None:
    event_cls = _load_stub_schema()
    parts = [
        _gear_event(event_cls, 0, "unknown").to_bytes(),
        _gear_event(event_cls, 4 * NS, "reverse").to_bytes(),
        _gear_event(event_cls, 9 * NS, "park").to_bytes(),
        _gear_event(event_cls, 12 * NS, "drive").to_bytes(),
        _gear_event(event_cls, 15 * NS, "drive").to_bytes(),
    ]
    result = extract_engaged_time(b"".join(parts), event_mod=event_cls)
    assert result.gear_source == GEAR_SOURCE
    # unknown 4s + reverse 5s + drive 3s = 12s; park 3s ignored
    assert result.not_in_park_time_s is not None
    assert abs(result.not_in_park_time_s - 12.0) < 1e-6


def test_gear_is_park_matches_raw_and_name() -> None:
    event_cls = _load_stub_schema()
    park = event_cls.new_message()
    park.init("carState").gearShifter = "park"
    drive = event_cls.new_message()
    drive.init("carState").gearShifter = "drive"
    unknown = event_cls.new_message()
    unknown.init("carState").gearShifter = "unknown"
    assert _gear_is_park(park.carState.gearShifter) is True
    assert _gear_is_park(drive.carState.gearShifter) is False
    assert _gear_is_park(unknown.carState.gearShifter) is False
    assert park.carState.gearShifter.raw == 1


def test_stub_reads_cereal_like_car_state_gear(tmp_path: Path) -> None:
    """Real qlogs write Event.carState @22 with gearShifter @14."""
    union = "\n".join(
        "    carState @22 :CarState;"
        if i == 22
        else "    selfdriveState @130 :SelfdriveState;"
        if i == 130
        else f"    u{i} @{i} :Void;"
        for i in range(1, 131)
        if i != 67
    )
    schema = tmp_path / "cereal_like_gear.capnp"
    schema.write_text(
        f"""
@0xaaaaaaaaaaaaaaa3;

enum GearShifter {{
  unknown @0;
  park @1;
  drive @2;
  neutral @3;
  reverse @4;
}}

struct CarState {{
  errors @0 :AnyPointer;
  vEgo @1 :Float32;
  wheelSpeeds @2 :AnyPointer;
  gas @3 :Float32;
  gasPressed @4 :Bool;
  brake @5 :Float32;
  brakePressed @6 :Bool;
  steeringAngleDeg @7 :Float32;
  steeringTorque @8 :Float32;
  steeringPressed @9 :Bool;
  cruiseState @10 :AnyPointer;
  buttonEvents @11 :AnyPointer;
  canMonoTimes @12 :AnyPointer;
  events @13 :AnyPointer;
  gearShifter @14 :GearShifter;
}}

struct SelfdriveState {{
  state @0 :UInt16;
  enabled @1 :Bool;
}}

struct Event {{
  logMonoTime @0 :UInt64;
  valid @67 :Bool = true;
  union {{
{union}
  }}
}}
""",
        encoding="utf-8",
    )
    capnp.remove_import_hook()
    writer = capnp.load(str(schema)).Event
    parts: list[bytes] = []
    for t, gear in [(0.0, "park"), (2.0, "drive"), (5.0, "drive"), (8.0, "drive")]:
        msg = writer.new_message()
        msg.logMonoTime = int(round(t * NS))
        msg.valid = True
        msg.init("carState").gearShifter = gear
        parts.append(msg.to_bytes())
    result = extract_engaged_time(b"".join(parts), event_mod=_load_stub_schema())
    assert result.gear_source == GEAR_SOURCE
    assert result.not_in_park_time_s is not None
    assert abs(result.not_in_park_time_s - 6.0) < 1e-6
