from __future__ import annotations

from pathlib import Path

import capnp

from op_usage.qlog import (
    CONTROLS_SOURCE,
    GEAR_SOURCE,
    SELFDRIVE_SOURCE,
    EnabledSample,
    encode_synthetic_qlog,
    engaged_seconds,
    extract_engaged_time,
    load_event_module,
    pick_source,
    _gear_is_park,
    _load_stub_schema,
)

NS = 1_000_000_000

# Independent of the bundled stub: cereal Event.valid @67 is outside the union.
_CEREAL_LIKE_SS_ID = "@0xaaaaaaaaaaaaaaa1;"
_CEREAL_LIKE_CS_ID = "@0xaaaaaaaaaaaaaaa2;"
_DEPRECATED_FILE_ID = "@0xbbbbbbbbbbbbbbbb;"


def _union_members(max_ordinal: int = 152) -> str:
    parts: list[str] = []
    for i in range(1, max_ordinal + 1):
        if i == 67:
            continue
        if i == 7:
            parts.append("    controlsState @7 :ControlsState;")
        elif i == 130:
            parts.append("    selfdriveState @130 :SelfdriveState;")
        else:
            parts.append(f"    u{i} @{i} :Void;")
    return "\n".join(parts)


def _controls_fields(*, deprecated_group: bool) -> str:
    prefix = """
  vEgo @0 :Float32;
  aEgo @1 :Float32;
  vPid @2 :Float32;
  vTargetLead @3 :Float32;
  upAccelCmd @4 :Float32;
  uiAccelCmd @5 :Float32;
  yActual @6 :Float32;
  yDes @7 :Float32;
  upSteer @8 :Float32;
  uiSteer @9 :Float32;
  aTargetMin @10 :Float32;
  aTargetMax @11 :Float32;
  jerkFactor @12 :Float32;
  angleSteers @13 :Float32;
  hudLead @14 :Int32;
  cumLagMs @15 :Float32;
  canMonoTime @16 :UInt64;
  radarStateMonoTime @17 :UInt64;
  mdMonoTime @18 :UInt64;
"""
    if deprecated_group:
        return prefix + "  deprecated :group {\n    enabled @19 :Bool;\n  }\n"
    return prefix + "  enabled @19 :Bool;\n"


def _write_schema(path: Path, *, file_id: str, deprecated_group: bool) -> None:
    path.write_text(
        f"""
{file_id}

struct SelfdriveState {{
  state @0 :UInt16;
  enabled @1 :Bool;
  active @2 :Bool;
}}

struct ControlsState {{
{_controls_fields(deprecated_group=deprecated_group)}
}}

struct Event {{
  logMonoTime @0 :UInt64;
  valid @67 :Bool = true;
  union {{
{_union_members()}
  }}
}}
""",
        encoding="utf-8",
    )


def _load_schema(path: Path):
    capnp.remove_import_hook()
    return capnp.load(str(path)).Event


def _samples(pattern: list[tuple[float, bool]], source: str = SELFDRIVE_SOURCE, dt: float = 1.0) -> list[EnabledSample]:
    out: list[EnabledSample] = []
    t = 0.0
    for duration, enabled in pattern:
        end = t + duration
        while t < end - 1e-9:
            out.append(EnabledSample(int(round(t * NS)), enabled, source))
            t += dt
    out.append(EnabledSample(int(round(t * NS)), False, source))
    return out


def test_integral_counts_enabled_spans_and_skips_gaps() -> None:
    samples = _samples([(10, True), (5, False), (8, True)])
    assert abs(engaged_seconds(samples) - 18.0) < 1e-6
    gap = [
        EnabledSample(0, True, SELFDRIVE_SOURCE),
        EnabledSample(int(20 * NS), True, SELFDRIVE_SOURCE),
    ]
    assert engaged_seconds(gap, max_gap_s=5.0) == 0.0


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


def test_stub_schema_keeps_valid_out_of_union() -> None:
    event_cls = _load_stub_schema()
    msg = event_cls.new_message()
    msg.logMonoTime = 1
    msg.valid = True
    ss = msg.init("selfdriveState")
    ss.enabled = True
    assert msg.which() == "selfdriveState"
    assert msg.valid is True


def test_stub_reads_cereal_like_selfdrive_state(tmp_path: Path) -> None:
    schema = tmp_path / "cereal_like.capnp"
    _write_schema(schema, file_id=_CEREAL_LIKE_SS_ID, deprecated_group=False)
    writer = _load_schema(schema)
    samples = _samples([(3.0, True), (1.0, False)])
    parts: list[bytes] = []
    for sample in samples:
        msg = writer.new_message()
        msg.logMonoTime = sample.log_mono_ns
        msg.valid = True
        ss = msg.init("selfdriveState")
        ss.enabled = sample.enabled
        parts.append(msg.to_bytes())
    result = extract_engaged_time(b"".join(parts), event_mod=_load_stub_schema())
    assert result.source == SELFDRIVE_SOURCE
    assert result.sample_count == len(samples)
    assert abs(result.engaged_time_s - 3.0) < 1e-6


def test_stub_reads_cereal_like_controls_state_enabled_19(tmp_path: Path) -> None:
    schema = tmp_path / "cereal_like_cs.capnp"
    _write_schema(schema, file_id=_CEREAL_LIKE_CS_ID, deprecated_group=False)
    writer = _load_schema(schema)
    samples = _samples([(2.0, True)], source=CONTROLS_SOURCE)
    parts: list[bytes] = []
    for sample in samples:
        msg = writer.new_message()
        msg.logMonoTime = sample.log_mono_ns
        cs = msg.init("controlsState")
        cs.enabled = sample.enabled
        parts.append(msg.to_bytes())
    result = extract_engaged_time(b"".join(parts), event_mod=_load_stub_schema())
    assert result.source == CONTROLS_SOURCE
    assert abs(result.engaged_time_s - 2.0) < 1e-6


def test_controls_deprecated_enabled_group(tmp_path: Path) -> None:
    schema = tmp_path / "deprecated_cs.capnp"
    _write_schema(schema, file_id=_DEPRECATED_FILE_ID, deprecated_group=True)
    writer = _load_schema(schema)
    samples = _samples([(4.0, True)], source=CONTROLS_SOURCE)
    parts: list[bytes] = []
    for sample in samples:
        msg = writer.new_message()
        msg.logMonoTime = sample.log_mono_ns
        cs = msg.init("controlsState")
        cs.deprecated.enabled = sample.enabled
        parts.append(msg.to_bytes())
    blob = b"".join(parts)
    result = extract_engaged_time(blob, event_mod=writer)
    assert result.source == CONTROLS_SOURCE
    assert abs(result.engaged_time_s - 4.0) < 1e-6
    via_stub = extract_engaged_time(blob, event_mod=_load_stub_schema())
    assert via_stub.source == CONTROLS_SOURCE
    assert abs(via_stub.engaged_time_s - 4.0) < 1e-6


def test_load_event_module_falls_back_to_stub() -> None:
    mod = load_event_module(openpilot_path=None, cereal_path=None)
    assert mod is not None
    msg = mod.new_message()
    msg.init("selfdriveState").enabled = True
    assert msg.which() == "selfdriveState"


def test_extract_not_in_park_from_synthetic_qlog() -> None:
    engaged = _samples([(5.0, True)], source=SELFDRIVE_SOURCE)
    gear = _samples([(3.0, False), (7.0, True)], source=GEAR_SOURCE)
    result = extract_engaged_time(encode_synthetic_qlog(engaged + gear, compress=None))
    assert result.source == SELFDRIVE_SOURCE
    assert abs(result.engaged_time_s - 5.0) < 1e-6
    assert result.gear_source == GEAR_SOURCE
    assert result.not_in_park_time_s is not None
    assert abs(result.not_in_park_time_s - 7.0) < 1e-6


def test_extract_without_car_state_leaves_not_in_park_none() -> None:
    result = extract_engaged_time(encode_synthetic_qlog(_samples([(2.0, True)]), compress=None))
    assert result.not_in_park_time_s is None
    assert result.gear_sample_count == 0


def test_single_gear_sample_is_missing_not_zero() -> None:
    engaged = _samples([(2.0, True)], source=SELFDRIVE_SOURCE)
    gear = [EnabledSample(0, True, GEAR_SOURCE)]
    result = extract_engaged_time(encode_synthetic_qlog(engaged + gear, compress=None))
    assert result.engaged_time_s > 0
    assert result.gear_sample_count == 1
    assert result.not_in_park_time_s is None


def test_zero_park_integral_with_engaged_is_missing() -> None:
    engaged = _samples([(2.0, True)], source=SELFDRIVE_SOURCE)
    gear = _samples([(2.0, False)], source=GEAR_SOURCE)
    result = extract_engaged_time(encode_synthetic_qlog(engaged + gear, compress=None))
    assert result.engaged_time_s > 0
    assert result.gear_sample_count >= 2
    assert result.not_in_park_time_s is None


def test_unknown_and_reverse_count_as_not_in_park() -> None:
    event_cls = _load_stub_schema()

    def _gear(mono_ns: int, name: str):
        msg = event_cls.new_message()
        msg.logMonoTime = mono_ns
        msg.init("carState").gearShifter = name
        return msg.to_bytes()

    parts = [
        _gear(0, "unknown"),
        _gear(4 * NS, "reverse"),
        _gear(9 * NS, "park"),
        _gear(12 * NS, "drive"),
        _gear(15 * NS, "drive"),
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
