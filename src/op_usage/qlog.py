"""Engaged-time extraction from openpilot qlogs.

Access path (live):
  1. GET /v1/route/{routeName}/files  → payload["qlogs"] signed URLs
  2. HTTP GET each URL (commadata blob; JWT is in the signed query)
  3. Decompress bz2 or zstd (magic-byte detect, same as openpilot LogReader)
  4. Parse concatenated Cap'n Proto Event messages

Field used:
  Prefer  selfdriveState.enabled   (openpilot ~0.9.7+, Event union @130)
  Fallback controlsState.enabled   (older logs; cereal field @19, now under
                                    ControlsState.deprecated.enabled)

Engage time is the integral of enabled over logMonoTime (nanoseconds), not a
sample count. Gaps larger than MAX_GAP_S are skipped (segment holes / dropout).

If an openpilot checkout is on OPENPILOT_PATH, cereal.log.Event is used.
Otherwise the bundled stub schema (schemas/engaged.capnp) is used. The stub
must keep Event.valid @67 *outside* the union — cereal does that, and putting
@67 in the union makes which() report selfdriveState as u129.
"""

from __future__ import annotations

import bz2
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

log = logging.getLogger(__name__)

# qlogs are decimated; 5s covers typical 1–10 Hz selfdriveState without
# counting a new ignition as engaged time.
MAX_GAP_S = 5.0
NS = 1_000_000_000.0

SELFDRIVE_SOURCE = "selfdriveState.enabled"
CONTROLS_SOURCE = "controlsState.enabled"
NONE_SOURCE = "none"


@dataclass(frozen=True)
class EnabledSample:
    log_mono_ns: int
    enabled: bool
    source: str


@dataclass(frozen=True)
class EngagedResult:
    engaged_time_s: float
    source: str
    sample_count: int


def decompress_qlog(data: bytes) -> bytes:
    """Match openpilot tools.lib.logreader._LogFileReader decompression."""
    if data.startswith(b"BZh"):
        return bz2.decompress(data)
    if data.startswith(b"\x28\xb5\x2f\xfd"):
        import zstandard as zstd

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
    selfdrive = [s for s in samples if s.source == SELFDRIVE_SOURCE]
    if selfdrive:
        return selfdrive, SELFDRIVE_SOURCE
    controls = [s for s in samples if s.source == CONTROLS_SOURCE]
    if controls:
        return controls, CONTROLS_SOURCE
    return [], NONE_SOURCE


def extract_engaged_time(qlog_bytes: bytes, event_mod: Any | None = None) -> EngagedResult:
    decompressed = decompress_qlog(qlog_bytes)
    if event_mod is None:
        event_mod = load_event_module()
    samples = list(_iter_samples(decompressed, event_mod))
    chosen, source = pick_source(samples)
    return EngagedResult(
        engaged_time_s=engaged_seconds(chosen),
        source=source,
        sample_count=len(chosen),
    )


def extract_engaged_time_from_qlogs(blobs: Iterable[bytes], event_mod: Any | None = None) -> EngagedResult:
    """Concatenate samples across a route's qlog segments."""
    if event_mod is None:
        event_mod = load_event_module()
    samples: list[EnabledSample] = []
    for blob in blobs:
        decompressed = decompress_qlog(blob)
        samples.extend(_iter_samples(decompressed, event_mod))
    chosen, source = pick_source(samples)
    return EngagedResult(
        engaged_time_s=engaged_seconds(chosen),
        source=source,
        sample_count=len(chosen),
    )


def load_event_module(openpilot_path: Path | None = None, cereal_path: Path | None = None) -> Any:
    """Return a module/class with Event.read_multiple_bytes and Event.new_message."""
    cereal_event = _try_cereal(openpilot_path, cereal_path)
    if cereal_event is not None:
        log.info("qlog parser: using cereal.log.Event")
        return cereal_event
    stub = _load_stub_schema()
    log.info(
        "qlog parser: using bundled stub schema "
        "(Event.valid @67, selfdriveState @130, controlsState.enabled @19)"
    )
    return stub


def _try_cereal(openpilot_path: Path | None, cereal_path: Path | None) -> Any | None:
    extra: list[str] = []
    if openpilot_path:
        extra.append(str(openpilot_path))
        extra.append(str(openpilot_path.parent))
    if cereal_path:
        extra.append(str(cereal_path))
        extra.append(str(cereal_path.parent))
    for path in extra:
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        from cereal import log as capnp_log  # type: ignore

        return capnp_log.Event
    except Exception:
        pass
    try:
        from openpilot.cereal import log as capnp_log  # type: ignore

        return capnp_log.Event
    except Exception:
        return None


def _load_stub_schema() -> Any:
    import capnp

    schema = Path(__file__).resolve().parent / "schemas" / "engaged.capnp"
    capnp.remove_import_hook()
    mod = capnp.load(str(schema))
    return mod.Event


def _iter_samples(decompressed: bytes, event_cls: Any) -> Iterator[EnabledSample]:
    try:
        events = event_cls.read_multiple_bytes(decompressed)
    except Exception:
        log.warning("failed to start Event.read_multiple_bytes; empty result")
        return
    try:
        for event in events:
            sample = _event_to_sample(event)
            if sample is not None:
                yield sample
    except Exception as exc:
        # Trailing corruption is common in truncated uploads.
        log.warning("stopped reading qlog events early: %s", exc)


def _event_to_sample(event: Any) -> EnabledSample | None:
    try:
        which = event.which()
    except Exception:
        return None
    try:
        mono = int(event.logMonoTime)
    except Exception:
        return None
    if which == "selfdriveState":
        try:
            enabled = _read_enabled(event.selfdriveState)
        except Exception:
            return None
        if enabled is None:
            return None
        return EnabledSample(mono, enabled, SELFDRIVE_SOURCE)
    if which == "controlsState":
        try:
            enabled = _read_enabled(event.controlsState)
        except Exception:
            return None
        if enabled is None:
            return None
        return EnabledSample(mono, enabled, CONTROLS_SOURCE)
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
        else:
            ss = msg.init("selfdriveState")
            ss.enabled = sample.enabled
        parts.append(msg.to_bytes())
    raw = b"".join(parts)
    if compress == "bz2":
        return bz2.compress(raw)
    if compress == "zst":
        import zstandard as zstd

        return zstd.ZstdCompressor().compress(raw)
    return raw
