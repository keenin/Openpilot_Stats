from __future__ import annotations

from pathlib import Path

import capnp

from op_usage.qlog import (
    CONTROLS_SOURCE,
    SELFDRIVE_SOURCE,
    EnabledSample,
    encode_synthetic_qlog,
    engaged_seconds,
    extract_engaged_time,
    load_event_module,
    pick_source,
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
    """Real qlogs are written with cereal (valid @67 outside the union)."""
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
    blob = b"".join(parts)
    result = extract_engaged_time(blob, event_mod=_load_stub_schema())
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
    """Modern cereal exposes ControlsState.enabled as deprecated.enabled (@19)."""
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
    # Same module for write+read: Python must follow deprecated.enabled.
    result = extract_engaged_time(blob, event_mod=writer)
    assert result.source == CONTROLS_SOURCE
    assert abs(result.engaged_time_s - 4.0) < 1e-6
    # Wire-compatible with the stub's top-level enabled @19.
    via_stub = extract_engaged_time(blob, event_mod=_load_stub_schema())
    assert via_stub.source == CONTROLS_SOURCE
    assert abs(via_stub.engaged_time_s - 4.0) < 1e-6


def test_load_event_module_falls_back_to_stub() -> None:
    mod = load_event_module(openpilot_path=None, cereal_path=None)
    assert mod is not None
    msg = mod.new_message()
    msg.init("selfdriveState").enabled = True
    assert msg.which() == "selfdriveState"
