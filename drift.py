"""
Protocol drift detection for the MC5000.

WHY
---
The protocol is undocumented, reverse-engineered, and demonstrably changes
between firmware versions (FW 1.82 sends 23-byte status frames; FW 2.02
sends 24). SkyRC has no obligation to keep it stable and no knowledge that
this tool exists.

The failure mode that matters is not a crash. It is a frame that still
parses, still passes a checksum, and yields a plausible-looking number that
is quietly wrong - exactly what happened when a superseded current encoding
produced 876mA instead of 1499mA. That number was wrong by 40% and looked
entirely reasonable.

So: capture a fingerprint of how the device talks, check every frame against
it, and check decoded values against physics. Drift should be loud.

USE
---
    from drift import Fingerprint, FrameValidator

    fp = Fingerprint.builtin("2.02")    # or .load(path) when validating new firmware
    v = FrameValidator(fp)

    for raw in frames:
        result = v.check(raw)
        if result.severity == "error":
            ...stop...

    print(v.report())
    # v.current is what this run observed - the raw material for a NEW
    # firmware's reference, once someone has actually verified that firmware.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any

from protocol import parse_status_response, verify_checksum

# Physically sensible bounds. Deliberately generous - these are meant to
# catch decode errors, not to police battery behavior.
LIMITS = {
    "voltage_v": (0.0, 5.0),        # highest supported chemistry tops out ~4.35V
    "current_ma": (0, 5100),        # device is rated 5000mA max
    "capacity_mah": (0, 60000),
    # 7 days. NOT a run-length expectation - it is the point past which a value
    # can only be a decode fault. The old bound was 24 h, which looked safe when
    # elapsed was read as 16 bits (wrapping at 18 h 12 min, so 24 h was
    # unreachable) and stayed behind when the field was widened to 32 bits.
    # Measured on hardware: a front-panel Break-In counts elapsed
    # CUMULATIVELY across all its phases, crossed 86400 s 24 h into a 37 h
    # cycle, and every frame afterward was flagged an error - 34833 of them,
    # all with valid checksums and sane values. A multi-cycle break-in can
    # legitimately run for days, while a genuine 32-bit misparse lands in the
    # hundreds of millions, far above this.
    "elapsed_seconds": (0, 60 * 60 * 24 * 7),
    "resistance_mohm": (0, 10000),
}


# The protocol reference for each VERIFIED firmware version, embedded in code
# rather than read from baseline.json. A file can be lost (a shortcut with a
# different working directory) or, worse, written by whatever charger happens
# to be connected - which once let unknown firmware certify itself after a
# single clean run. A new version gets its own entry here once it is verified;
# capture.SUPPORTED_FIRMWARE is the policy that says which ones may start runs.
#
# 2.02 is transcribed from a bench baseline captured on hardware, with 0x55
# added after the temperature opcode was verified. `product_code` is the
# SkyRC product number (SK-100214), not a unit serial.
FIRMWARE_FINGERPRINTS: dict[str, dict] = {
    "2.02": {
        "firmware": "2.02",
        "hardware": "2.00",
        "product_code": "100214",
        "captured_at": "2026-08-16T16:54:57",
        "frame_lengths": {
            "0x06": [6], "0x55": [36], "0x57": [21], "0x65": [6],
            "0x74": [6], "0x91": [24], "0x94": [6], "0xfe": [6],
        },
        "length_bytes": {
            "0x06": [4], "0x55": [34], "0x57": [19], "0x65": [4],
            "0x74": [4], "0x91": [22], "0x94": [4], "0xfe": [4],
        },
        "commands_seen": ["0x06", "0x55", "0x57", "0x65", "0x74", "0x91",
                          "0x94", "0xfe"],
        "checksum_at_end": True,
    },
}


@dataclass
class CheckResult:
    ok: bool
    severity: str  # "ok" | "note" | "warn" | "error"
    messages: list[str] = field(default_factory=list)
    parsed: Any = None


@dataclass
class Fingerprint:
    """How this device's protocol looks, so we can notice when it changes."""

    firmware: str | None = None
    hardware: str | None = None
    # SkyRC's product number from 0x57 (the same on every MC5000). Older
    # fingerprints call it `serial`; load() still reads that key.
    product_code: str | None = None
    captured_at: str | None = None

    # command byte -> sorted list of frame lengths seen for it
    frame_lengths: dict[str, list[int]] = field(default_factory=dict)
    # command byte -> sorted list of length-byte values seen
    length_bytes: dict[str, list[int]] = field(default_factory=dict)
    # every command byte we have ever seen in a response
    commands_seen: list[str] = field(default_factory=list)
    # index at which the checksum validates (relative to frame end: -1)
    checksum_at_end: bool = True

    def observe(self, raw: bytes) -> list[str]:
        """Record a frame's shape. Returns notes about anything new."""
        notes = []
        if len(raw) < 3:
            return notes
        cmd = f"0x{raw[2]:02x}"

        if cmd not in self.commands_seen:
            self.commands_seen.append(cmd)
            self.commands_seen.sort()
            notes.append(f"first time seeing command {cmd}")

        lens = self.frame_lengths.setdefault(cmd, [])
        if len(raw) not in lens:
            lens.append(len(raw))
            lens.sort()
            if len(lens) > 1:
                notes.append(f"{cmd} now seen at multiple frame lengths: {lens}")

        lbs = self.length_bytes.setdefault(cmd, [])
        if raw[1] not in lbs:
            lbs.append(raw[1])
            lbs.sort()

        return notes

    def known(self, raw: bytes) -> bool:
        cmd = f"0x{raw[2]:02x}"
        return cmd in self.frame_lengths and len(raw) in self.frame_lengths[cmd]

    def set_device_info(self, firmware=None, hardware=None, product_code=None):
        self.firmware = firmware or self.firmware
        self.hardware = hardware or self.hardware
        self.product_code = product_code or self.product_code

    def save(self, path: str):
        self.captured_at = self.captured_at or datetime.now().isoformat(timespec="seconds")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)

    @classmethod
    def load(cls, path: str) -> "Fingerprint":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # An older fingerprint calls the product code "serial".
        if "serial" in data:
            data.setdefault("product_code", data.pop("serial"))
        return cls(**data)

    @classmethod
    def builtin(cls, firmware: str) -> "Fingerprint":
        """The embedded reference for a verified firmware version (KeyError if
        there is none). A fresh copy each call, so nothing can mutate the
        constant through a baseline it was handed."""
        return cls(**json.loads(json.dumps(FIRMWARE_FINGERPRINTS[firmware])))

    def diff(self, other: "Fingerprint", ignore_absent=()) -> list[str]:
        """
        What changed between a stored baseline (self) and a new capture.

        ignore_absent lists command bytes whose absence is explained by what the
        caller chose not to send, rather than by the device changing. Without it
        every read-only attach reports the init responses as missing, and a
        report that cries wolf routinely is one nobody reads when it matters.
        """
        out = []
        if self.firmware and other.firmware and self.firmware != other.firmware:
            out.append(f"FIRMWARE CHANGED: {self.firmware} -> {other.firmware}")
        if (self.product_code and other.product_code
                and self.product_code != other.product_code):
            out.append(f"different model: product code {self.product_code} -> "
                       f"{other.product_code}")

        for cmd, lens in other.frame_lengths.items():
            if cmd not in self.frame_lengths:
                out.append(f"new command in responses: {cmd} (lengths {lens})")
            elif lens != self.frame_lengths[cmd]:
                out.append(
                    f"FRAME LENGTH CHANGED for {cmd}: "
                    f"baseline {self.frame_lengths[cmd]} -> now {lens}"
                )
        for cmd in self.frame_lengths:
            if cmd not in other.frame_lengths and cmd not in ignore_absent:
                out.append(f"command no longer seen: {cmd}")
        return out


class FrameValidator:
    """
    Checks each frame three ways:
      1. structural - checksum, length byte consistency, known shape
      2. range      - decoded values physically possible
      3. physical   - capacity consistent with current x elapsed
    """

    def __init__(self, baseline: Fingerprint | None = None, strict: bool = True,
                 expected_absent=()):
        # The baseline is READ-ONLY. Observations accumulate into a separate
        # `current` fingerprint. Mixing the two was a bug: a baseline that
        # updates itself to match incoming frames can never detect drift,
        # because it silently absorbs whatever it is supposed to catch.
        self.baseline = baseline
        # What THIS run observed. Its identity fields stay empty until the
        # charger has actually been asked (0x57): copying them from the
        # baseline made meta.json claim "firmware 2.02" for a run that never
        # read the version - misleading in exactly the reports from other
        # firmware that matter most.
        self.current = Fingerprint()
        self.strict = strict
        # Commands the caller knows it will not trigger this run, so their
        # absence is not evidence of drift. See Fingerprint.diff.
        self.expected_absent = tuple(expected_absent)
        self.counts = {"ok": 0, "note": 0, "warn": 0, "error": 0}
        self.issues: list[str] = []
        self._last_by_slot: dict[int, Any] = {}
        # rolling per-slot history for the windowed consistency check
        self._history: dict[int, list] = {}
        self.window_s = 300      # look back this far
        self.min_span_s = 120    # need at least this much span to judge

    def check(self, raw: bytes) -> CheckResult:
        msgs: list[str] = []
        severity = "ok"

        def escalate(level, msg):
            nonlocal severity
            order = ["ok", "note", "warn", "error"]
            if order.index(level) > order.index(severity):
                severity = level
            msgs.append(msg)

        if len(raw) < 4:
            escalate("error", f"frame too short: {len(raw)} bytes")
            return self._finish(severity, msgs, None)

        # --- 1. structural ---
        if not verify_checksum(raw):
            escalate("error", f"CHECKSUM FAILED (len {len(raw)})")

        # length byte should describe the rest of the frame
        implied = raw[1] + 2
        if implied != len(raw):
            escalate(
                "error",
                f"length byte says {raw[1]} (implying {implied} bytes) "
                f"but frame is {len(raw)} - framing has changed",
            )

        for note in self.current.observe(raw):
            escalate("note", note)
        if self.baseline is not None and not self.baseline.known(raw):
            escalate(
                "error" if self.strict else "warn",
                f"frame shape not in baseline: cmd 0x{raw[2]:02x}, {len(raw)} bytes "
                f"(baseline has {self.baseline.frame_lengths.get(f'0x{raw[2]:02x}', 'nothing')})",
            )

        if raw[2] != 0x91:
            return self._finish(severity, msgs, None)

        # --- 2. range ---
        try:
            st = parse_status_response(raw)
        except Exception as e:
            escalate("error", f"parse failed: {e}")
            return self._finish(severity, msgs, None)

        for fieldname, (lo, hi) in LIMITS.items():
            val = getattr(st, fieldname)
            if not (lo <= val <= hi):
                escalate("error", f"{fieldname}={val} outside plausible range [{lo}, {hi}]")

        # Internal resistance is measured ONCE, a few seconds into a run, and
        # then reported unchanged for the rest of it. That single measurement
        # can land badly - a real run produced 1 mOhm for an 18650, latched
        # for 3900 samples. Anything implausibly low for a real cell means the
        # figure is a bad sample, not a good cell.
        if st.current_ma > 0 and 0 < st.resistance_mohm < 5:
            escalate(
                "warn",
                f"S{st.slot} resistance reads {st.resistance_mohm} mOhm - "
                f"implausibly low for a real cell. The charger samples "
                f"resistance once at start and holds it; this run's figure "
                f"should be discarded, not reported.",
            )

        # --- 3. physical consistency, over a WINDOW ---
        #
        # Capacity is the INTEGRAL of current over time. Comparing cumulative
        # capacity against instantaneous_current x total_elapsed is only valid
        # while current is constant - it produces a flood of false errors the
        # moment a charger enters CV taper and current falls.
        #
        # So instead: over a rolling window, the CHANGE in reported capacity
        # should match the trapezoidal integral of current across that same
        # window. That holds regardless of how current varies.
        hist = self._history.setdefault(st.slot, [])
        if st.current_ma > 0 and st.elapsed_seconds > 0:
            hist.append((st.elapsed_seconds, st.current_ma, st.capacity_mah))
            # drop anything older than the window
            while len(hist) > 2 and st.elapsed_seconds - hist[0][0] > self.window_s:
                hist.pop(0)

            span = st.elapsed_seconds - hist[0][0]
            if span >= self.min_span_s:
                integral = 0.0
                for (t0, i0, _), (t1, i1, _) in zip(hist, hist[1:]):
                    dt = (t1 - t0) / 3600
                    if dt > 0:
                        integral += (i0 + i1) / 2 * dt
                delta_cap = st.capacity_mah - hist[0][2]
                if integral > 0:
                    diff = abs(integral - delta_cap)
                    err = diff / integral * 100
                    # whole-mAh quantization at both ends of the window
                    if diff < 3.0:
                        err = 0.0
                    if err > 40:
                        escalate(
                            "error",
                            f"over {span}s the device added {delta_cap}mAh but the "
                            f"logged current integrates to {integral:.1f}mAh "
                            f"({err:.0f}% off) - a field is probably decoded at "
                            f"the wrong offset",
                        )
                    elif err > 20:
                        escalate("warn", f"capacity/current integral {err:.0f}% inconsistent over {span}s")
        else:
            hist.clear()

        # --- 4. continuity against the previous frame for this slot ---
        prev = self._last_by_slot.get(st.slot)
        if prev is not None:
            # elapsed running backward (other than a reset to 0) is suspicious
            if 0 < st.elapsed_seconds < prev.elapsed_seconds:
                escalate(
                    "warn",
                    f"S{st.slot} elapsed went backward "
                    f"{prev.elapsed_seconds}s -> {st.elapsed_seconds}s",
                )
            # the transition artifact seen on stop: current still flowing but
            # counters zeroed. Real, but must not be treated as a live sample.
            if st.current_ma > 0 and st.elapsed_seconds == 0 and prev.elapsed_seconds > 0:
                escalate(
                    "note",
                    f"S{st.slot} transition frame (current {st.current_ma}mA but "
                    f"counters reset) - exclude from integration",
                )
        self._last_by_slot[st.slot] = st

        return self._finish(severity, msgs, st)

    def _finish(self, severity, msgs, parsed):
        self.counts[severity] = self.counts.get(severity, 0) + 1
        for m in msgs:
            if severity in ("warn", "error"):
                self.issues.append(f"[{severity}] {m}")
        return CheckResult(ok=(severity != "error"), severity=severity,
                           messages=msgs, parsed=parsed)

    def report(self) -> str:
        total = sum(self.counts.values())
        lines = [
            "=" * 62,
            "PROTOCOL DRIFT REPORT",
            "=" * 62,
            f"frames checked : {total}",
            f"  clean        : {self.counts['ok']}",
            f"  notes        : {self.counts['note']}",
            f"  warnings     : {self.counts['warn']}",
            f"  errors       : {self.counts['error']}",
        ]
        if self.current.firmware:
            lines.append(f"firmware       : {self.current.firmware}")
        if self.baseline is not None:
            drift = self.baseline.diff(self.current,
                                       ignore_absent=self.expected_absent)
            if drift:
                lines.append("")
                lines.append("DRIFT vs BASELINE:")
                for d in drift:
                    lines.append(f"  {d}")
        if self.issues:
            lines.append("")
            lines.append("issues:")
            seen = set()
            for i in self.issues:
                if i not in seen:
                    seen.add(i)
                    lines.append(f"  {i}")
        else:
            lines.append("")
            lines.append("no structural or physical inconsistencies detected")
        lines.append("=" * 62)
        return "\n".join(lines)
