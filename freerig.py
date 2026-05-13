#!/home/ema/venv/bin/python
"""
Free RIG panel I/O emulator (experimental)

Important:
  Do NOT leave the original panel TX connected to the same body RX line while this
  script drives it. Two push-pull TX outputs must not fight each other.

Notes:
  The display decoder is based on the fields mapped so far by reverse engineering.
  Unknown bytes are still left available via display raw / display diff raw.
"""

from __future__ import annotations

import argparse
import audioop
import base64
import re
import select
import shlex
import socket
import struct
import sys
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import serial
except ModuleNotFoundError:
    serial = None  # pyserial required unless --demo is used

try:
    import pigpio  # type: ignore
except ModuleNotFoundError:
    pigpio = None  # required only for GPIO power-on replay

BUILD_ID = "v111-pmg-latched-render-20260512"
BAUD = 500000
TX_FRAME_LEN = 210
RX_FRAME_LEN = 1100
RX_BLOCK_LEN = 220
BITS_PER_UART_BYTE = 10
TX_FRAME_TIME_S = TX_FRAME_LEN * BITS_PER_UART_BYTE / BAUD
TX_FRAME_TIME_MS = TX_FRAME_TIME_S * 1000.0
RX_FRAME_TIME_MS = RX_FRAME_LEN * BITS_PER_UART_BYTE / BAUD

# Runtime recorder used by the save start/save stop workflow.
# It is initialized after helper functions are defined; TX/RX paths only call it when active.
SAVE_RECORDER = None

# Baseline learned from line_panel_to_body_idle.bin.
BASE_FRAME = bytes.fromhex(
    "80 00 00 00 00 00 00 00 00 00 00 00 00 7c 7b 20"
    " 00 00 00 0f 00 00 00 00 00 00 00 00 00 00 00 00"
    " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 01 02"
    " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    " 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
    " 00 00"
)
assert len(BASE_FRAME) == TX_FRAME_LEN, len(BASE_FRAME)

# Operation format:
#   ("set", offset, value)    -> frame[offset] = value
#   ("or",  offset, bitmask)  -> frame[offset] |= bitmask
Op = Tuple[str, int, int]
State = List[Op]


def ops_set(*pairs: Tuple[int, int]) -> State:
    return [("set", off, val) for off, val in pairs]


def ops_or(*pairs: Tuple[int, int]) -> State:
    return [("or", off, mask) for off, mask in pairs]


# Canonical commands only. Aliases are hidden in ALIASES.
COMMANDS: Dict[str, State] = {
    # Front keys.
    "band": ops_or((5, 0x01)),       # Band / Scope
    "f": ops_or((5, 0x04)),          # F / Back
    "pmg": ops_or((5, 0x08)),        # PMG / PW
    "vm": ops_or((5, 0x20)),         # V/M / MW
    "sdx": ops_or((7, 0x40)),        # S-DX
    "power": ops_or((6, 0x01)),

    # Knob press.
    "ul_press": ops_or((6, 0x02)),   # upper-left knob press
    "ur_press": ops_or((6, 0x08)),   # upper-right knob press
    "bl_press": ops_or((6, 0x10)),   # bottom-left knob press
    "br_press": ops_or((6, 0x20)),   # bottom-right knob press

    # Encoder rotations. 0x7f = left, 0x01 = right.
    # Bottom-left uses byte 0; 0x80 is the idle marker/state byte.
    "ul_left": ops_set((2, 0x7f)),
    "ul_right": ops_set((2, 0x01)),
    "ur_left": ops_set((3, 0x7f)),
    "ur_right": ops_set((3, 0x01)),
    "bl_left": ops_set((0, 0xff)),
    "bl_right": ops_set((0, 0x81)),
    "br_left": ops_set((1, 0x7f)),
    "br_right": ops_set((1, 0x01)),

    # Microphone direct signals.
    "mic_ptt": ops_or((8, 0x01)),
    "mic_ptt_hold": [("or", 8, 0x01), ("set", 15, 0x21)],
    "mic_up": ops_set((13, 0x07), (14, 0x1f)),
    "mic_down": ops_set((13, 0x07), (14, 0x36)),
    "mic_mute": ops_set((13, 0x02), (14, 0x63)),
    "mic_p1": ops_set((13, 0x1c), (14, 0x63)),
    "mic_p2": ops_set((13, 0x34), (14, 0x63)),
    "mic_p3": ops_set((13, 0x4d), (14, 0x63)),
    "mic_p4": ops_set((13, 0x67), (14, 0x63)),

    # Microphone keypad. For keys with two observed values, the canonical value is
    # one of the observed valid dumps; *_alt commands are also provided below.
    "mic_1": ops_set((13, 0x1c), (14, 0x02)),
    "mic_2": ops_set((13, 0x33), (14, 0x02)),
    "mic_3": ops_set((13, 0x4c), (14, 0x02)),
    "mic_4": ops_set((13, 0x1c), (14, 0x19)),
    "mic_5": ops_set((13, 0x33), (14, 0x19)),
    "mic_6": ops_set((13, 0x4c), (14, 0x19)),
    "mic_7": ops_set((13, 0x1c), (14, 0x32)),
    "mic_8": ops_set((13, 0x33), (14, 0x32)),
    "mic_9": ops_set((13, 0x4c), (14, 0x32)),
    "mic_star": ops_set((13, 0x1c), (14, 0x4b)),
    "mic_0": ops_set((13, 0x33), (14, 0x4b)),
    "mic_hash": ops_set((13, 0x4c), (14, 0x4b)),
    "mic_a": ops_set((13, 0x65), (14, 0x02)),
    "mic_b": ops_set((13, 0x66), (14, 0x19)),
    "mic_c": ops_set((13, 0x66), (14, 0x32)),
    "mic_d": ops_set((13, 0x66), (14, 0x4b)),

    # Alternative observed mic values.
    "mic_1_alt": ops_set((13, 0x1b), (14, 0x02)),
    "mic_2_alt": ops_set((13, 0x33), (14, 0x01)),
    "mic_4_alt": ops_set((13, 0x1b), (14, 0x19)),
    "mic_6_alt": ops_set((13, 0x4c), (14, 0x1a)),
    "mic_0_alt": ops_set((13, 0x34), (14, 0x4b)),
    "mic_hash_alt": ops_set((13, 0x4c), (14, 0x4c)),
    "mic_a_alt": ops_set((13, 0x65), (14, 0x01)),
    "mic_b_alt": ops_set((13, 0x66), (14, 0x1a)),
    "mic_d_alt": ops_set((13, 0x66), (14, 0x4c)),
}

ALIASES = {
    # Front-panel aliases.
    "band/scope": "band", "scope": "band",
    "f/back": "f", "back": "f",
    "pmg/pw": "pmg", "pw": "pmg",
    "v/m": "vm", "mw": "vm", "v/mw": "vm", "vm/mw": "vm", "vmmw": "vm",
    "s-dx": "sdx", "sd-x": "sdx",

    # Verbose knob aliases.
    "upper_left_press": "ul_press", "upper_right_press": "ur_press",
    "bottom_left_press": "bl_press", "bottom_right_press": "br_press",
    "upper_left_left": "ul_left", "upper_left_right": "ul_right",
    "upper_right_left": "ur_left", "upper_right_right": "ur_right",
    "bottom_left_left": "bl_left", "bottom_left_right": "bl_right",
    "bottom_right_left": "br_left", "bottom_right_right": "br_right",

    # Mic aliases.
    "ptt": "mic_ptt", "up": "mic_up", "down": "mic_down", "mute": "mic_mute",
    "p1": "mic_p1", "p2": "mic_p2", "p3": "mic_p3", "p4": "mic_p4",
    "1": "mic_1", "2": "mic_2", "3": "mic_3", "4": "mic_4", "5": "mic_5",
    "6": "mic_6", "7": "mic_7", "8": "mic_8", "9": "mic_9", "0": "mic_0",
    "*": "mic_star", "star": "mic_star", "#": "mic_hash", "hash": "mic_hash",
    "a": "mic_a", "b": "mic_b", "c": "mic_c", "d": "mic_d",
}

# Larger defaults than v1. 60 frames ~= 252 ms. Encoder default reduced in v22: 4 frames ~= 17 ms for one detent.
DEFAULT_KEY_FRAMES = 60
DEFAULT_ENCODER_FRAMES = 1
DEFAULT_POWER_FRAMES = 120
DEFAULT_MIC_FRAMES = 70


def canon(name: str) -> str:
    n = name.strip().lower().replace(" ", "_")
    return ALIASES.get(n, n)


def default_frames_for(name: str) -> int:
    if name == "power":
        return DEFAULT_POWER_FRAMES
    if name.endswith("_left") or name.endswith("_right"):
        return DEFAULT_ENCODER_FRAMES
    if name.startswith("mic_"):
        return DEFAULT_MIC_FRAMES
    return DEFAULT_KEY_FRAMES


def parse_duration_token(token: str, default_frames: int) -> int:
    """
    Accepts:
      80f     explicit frame count
      250ms   milliseconds
      250     milliseconds, for backward compatibility
    Returns number of frames, at least 1.
    """
    t = token.strip().lower()
    if t.endswith("f"):
        frames = int(t[:-1], 0)
    else:
        if t.endswith("ms"):
            ms = float(t[:-2])
        else:
            ms = float(t)
        frames = round(ms / TX_FRAME_TIME_MS)
    return max(1, int(frames)) if token else default_frames


def frames_to_ms(frames: int) -> float:
    return frames * TX_FRAME_TIME_MS


def apply_ops(frame: bytearray, ops: Iterable[Op]) -> None:
    for mode, off, val in ops:
        if not (0 <= off < TX_FRAME_LEN):
            raise ValueError(f"offset out of frame: {off}")
        if not (0 <= val <= 0xff):
            raise ValueError(f"invalid byte value: {val}")
        if mode == "set":
            frame[off] = val
        elif mode == "or":
            frame[off] |= val
        else:
            raise ValueError(f"unknown operation: {mode}")


@dataclass
class Pulse:
    states: List[State]
    frames_left: int
    total_frames: int
    label: str

    def current_state(self) -> State:
        if not self.states:
            return []
        elapsed = self.total_frames - self.frames_left
        idx = int(elapsed * len(self.states) / max(1, self.total_frames))
        if idx >= len(self.states):
            idx = len(self.states) - 1
        return self.states[idx]


class PanelTx:
    def __init__(self, ser: serial.Serial, verbose: bool = False):
        self.ser = ser
        self.verbose = verbose
        self.lock = threading.Lock()
        self.pulses: List[Pulse] = []
        self.hold_ops: List[Op] = []
        # Named holds are used by the web UI so manual PTT and audio-TX PTT
        # can coexist without one release clearing the other.
        self.named_holds: Dict[str, State] = {}
        self.stop = threading.Event()
        # TX writer gate.  When the radio is OFF there is no reason to keep
        # pushing 210-byte idle frames into the USB serial adapter.  The power
        # watchdog enables this again as soon as the radio wakes up.
        self.tx_enabled = threading.Event()
        self.tx_enabled.set()
        self.frames_sent = 0

    def set_enabled(self, enabled: bool, reason: str = "") -> None:
        """Enable/disable continuous panel->body frame transmission."""
        enabled = bool(enabled)
        was = self.tx_enabled.is_set()
        if enabled:
            self.tx_enabled.set()
        else:
            self.tx_enabled.clear()
        if was != enabled:
            state = "ON" if enabled else "OFF"
            suffix = f" ({reason})" if reason else ""
            print(f"[tx] continuous TX {state}{suffix}")
        # Drop any stale bytes still buffered by the OS/USB driver when changing
        # state, so the line does not get old frames after the mux reconnects.
        try:
            self.ser.reset_output_buffer()
        except Exception:
            pass

    def pulse(self, label: str, ops: State, frames: int) -> None:
        with self.lock:
            self.pulses.append(Pulse([list(ops)], frames, frames, label))
        print(f"[tx] {label}: {frames} frame ≈ {frames_to_ms(frames):.0f} ms")
        rec = globals().get("SAVE_RECORDER")
        if rec is not None:
            rec.record_command("pulse", label, frames=frames, ops=ops)

    def hold(self, label: str, ops: State) -> None:
        with self.lock:
            self.hold_ops.extend(ops)
        print(f"[hold] {label}; use 'release' to return to idle")
        rec = globals().get("SAVE_RECORDER")
        if rec is not None:
            rec.record_command("hold", label, ops=ops)

    def named_hold(self, key: str, label: str, ops: State) -> None:
        """Set/replace a persistent hold without disturbing other holds."""
        with self.lock:
            self.named_holds[key] = list(ops)
        print(f"[hold] {label}; named={key}")
        rec = globals().get("SAVE_RECORDER")
        if rec is not None:
            rec.record_command("named_hold", label, key=key, ops=ops)

    def clear_named_hold(self, key: str) -> None:
        with self.lock:
            existed = key in self.named_holds
            self.named_holds.pop(key, None)
        if existed:
            print(f"[hold] clear named={key}")
        rec = globals().get("SAVE_RECORDER")
        if rec is not None:
            rec.record_command("clear_named_hold", key, existed=existed)

    def release(self) -> None:
        with self.lock:
            self.hold_ops.clear()
            self.named_holds.clear()
            self.pulses.clear()
        print("[release] idle")
        rec = globals().get("SAVE_RECORDER")
        if rec is not None:
            rec.record_command("release", "idle")

    def current_frame(self) -> bytes:
        frame = bytearray(BASE_FRAME)
        with self.lock:
            if self.hold_ops:
                apply_ops(frame, self.hold_ops)
            for ops in self.named_holds.values():
                apply_ops(frame, ops)

            alive: List[Pulse] = []
            for p in self.pulses:
                if p.frames_left <= 0:
                    continue
                apply_ops(frame, p.current_state())
                p.frames_left -= 1
                if p.frames_left > 0:
                    alive.append(p)
            self.pulses = alive
        return bytes(frame)

    def writer_loop(self) -> None:
        last_report = time.monotonic()
        last_count = 0
        while not self.stop.is_set():
            if not self.tx_enabled.is_set():
                # Radio OFF: do not send idle frames into the USB serial adapter.
                # Sleep via Event.wait so re-enable is immediate.
                self.tx_enabled.wait(0.05)
                continue
            frame = self.current_frame()
            try:
                self.ser.write(frame)
                # Prevent huge OS buffering; we want command changes to appear quickly.
                self.ser.flush()
            except serial.SerialException as e:
                print(f"[serial] TX error: {e}", file=sys.stderr)
                self.stop.set()
                return
            self.frames_sent += 1

            if self.verbose:
                now = time.monotonic()
                if now - last_report >= 2.0:
                    fps = (self.frames_sent - last_count) / (now - last_report)
                    print(f"[stat] TX {fps:.1f} frame/s, nominal ≈ {1000.0 / TX_FRAME_TIME_MS:.1f} frame/s")
                    last_report = now
                    last_count = self.frames_sent


class BodyRx:
    def __init__(
        self,
        ser: serial.Serial,
        enabled: bool = True,
        verbose: bool = False,
        ignore_menu: bool = True,
    ):
        self.ser = ser
        self.enabled = enabled
        self.verbose = verbose
        self.ignore_menu = ignore_menu
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.latest_frame: Optional[bytes] = None
        self.latest_frame_at: Optional[float] = None
        self.latest_data_frame: Optional[bytes] = None
        self.latest_data_at: Optional[float] = None
        # Last frame that is definitely a normal VFO/MEM display, not PMG/scope/menu.
        # Used as a final safety net if a PMG family frame ever slips into latest_data_frame.
        self.latest_normal_data_frame: Optional[bytes] = None
        self.latest_normal_data_at: Optional[float] = None
        self.latest_menu_frame: Optional[bytes] = None
        self.latest_menu_at: Optional[float] = None
        # PMG mode sends an F1 25 display2 frame plus an F1 00 companion frame
        # carrying the selected PMG channel/frequency.  Keep that companion out
        # of latest_data_frame, otherwise the normal VFO/MEM decoder paints it
        # as a corrupted normal screen while PMG is active.
        self.latest_pmg_data_frame: Optional[bytes] = None
        self.latest_pmg_data_at: Optional[float] = None
        # Keep the graphical PMG F1 25 frame separate from generic latest_menu_frame.
        # Some radios emit F1 60 refresh frames around PMG; if those overwrite
        # latest_menu_frame, the browser briefly falls back to the normal LCD.
        self.latest_pmg_menu_frame: Optional[bytes] = None
        self.latest_pmg_menu_at: Optional[float] = None
        self.pmg_active_until: float = 0.0
        # Short LCD overlays such as LOCK/UNLOCK can disappear between
        # browser polls. Cache the last seen one briefly for the web UI.
        self.latest_overlay_text: Optional[str] = None
        self.latest_overlay_at: Optional[float] = None
        self.frames_seen = 0
        self.data_frames_seen = 0
        self.menu_frames_seen = 0
        self.ignored_frames_seen = 0
        self.sync_losses = 0
        self._buf = bytearray()

    @staticmethod
    def is_rx_frame_start(buf: bytearray, pos: int) -> bool:
        if pos + RX_FRAME_LEN > len(buf):
            return False
        if buf[pos] not in (0xF1, 0xF3):
            return False
        return all(buf[pos + RX_BLOCK_LEN * k] == 0xFF for k in range(1, 5))

    def find_sync(self) -> Optional[int]:
        search_from = 0
        while search_from < len(self._buf):
            pos_f1 = self._buf.find(b"\xF1", search_from)
            pos_f3 = self._buf.find(b"\xF3", search_from)
            positions = [p for p in (pos_f1, pos_f3) if p >= 0]
            if not positions:
                return None
            pos = min(positions)
            if self.is_rx_frame_start(self._buf, pos):
                return pos
            search_from = pos + 1
        return None

    @staticmethod
    def is_blank_or_keepalive(frame: bytes) -> bool:
        # Observed blank frame starts f1 60 and has no useful display payload.
        # Keep the test conservative: only skip the known tiny heartbeat.
        return frame[:2] == b"\xF1\x60" and all(b == 0 for b in frame[2:RX_BLOCK_LEN])

    @staticmethod
    @staticmethod
    def is_menu_display_frame(frame: bytes) -> bool:
        """Return True for menu/config/scope-style display frames.

        Initially we only split out f1 60, but field captures showed that some
        menu/scope pages arrive as f1 21 and otherwise look like the normal
        display path. Classify by:
          - known screen-type byte at +1: 0x60, 0x21, 0x23, 0x25 or 0x29
          - or menu-like labels in the menu text area +60..+150

        This keeps display/decode focused on the dual-frequency screen while
        display2 gets all alternate/menu-looking pages.  PMG uses F1 25: if it
        is allowed into latest_data_frame, the normal VFO decoder sees the PMG
        bar bytes as frequency/status bytes and the web display jumps/glitches.
        """
        if len(frame) < RX_BLOCK_LEN or frame[0] not in (0xF1, 0xF3):
            return False
        if (frame[0], frame[1]) in ((0xF1, 0x60), (0xF1, 0x21), (0xF1, 0x23), (0xF1, 0x25), (0xF1, 0x29), (0xF3, 0x20)):
            return True

        # Conservative content fallback: menu pages contain these labels in the
        # text area. Normal VFO/MEM display should not.
        area = frame[60:155]
        menu_needles = (
            b"RPT SFT", b"RPT FRQ", b"SQL TYP", b"CLONETX", b"CLONERX",
            b"BACKUP", b"AUTO DIALER", b"TX POWER", b"MIC GAIN", b"VOX",
        )
        return any(n in area for n in menu_needles)

    @staticmethod
    def is_pmg_companion_data_frame(frame: bytes) -> bool:
        """Return True for the F1 00 frame paired with PMG F1 25.

        PMG is a split screen: F1 25 carries the PMG bar graph, while a
        neighbouring F1 00 frame carries the selected PMG channel frequency.
        That F1 00 frame is not a normal dual VFO screen; feeding it to the
        normal decoder makes the web LCD jump/glitch.  Do not require a recent
        F1 25 here: some captures show the recorder/status path seeing these
        F1 00 frames as ordinary display frames, so the signature must be a
        hard exclusion from the normal snapshot.
        """
        if len(frame) < RX_BLOCK_LEN or frame[:2] != b"\xF1\x00":
            return False
        # Signature seen in all PMG captures: while the radio is on the PMG
        # graph, the paired F1 00 frame keeps the normal left header as MEM
        # at +0006, switches the right/source byte at +0007 to 0x08, uses
        # +0012 as the selected PMG slot 1..5, and blanks +0021..+0024.
        # Keep this deliberately structural rather than timing-based.
        return (
            int(frame[6]) in (0x40, 0x42, 0x44, 0x46)
            and int(frame[7]) == 0x08
            and 1 <= int(frame[12]) <= 5
            and bytes(frame[21:25]) == b"\x64\x64\x64\x64"
        )

    @staticmethod
    def is_pmg_family_frame(frame: Optional[bytes]) -> bool:
        """True for frames that belong to the PMG screen and must not paint normal VFO."""
        if frame is None or len(frame) < 2:
            return False
        return frame[:2] == b"\xF1\x25" or BodyRx.is_pmg_companion_data_frame(frame)

    def reader_loop(self) -> None:
        if not self.enabled:
            return
        last_report = time.monotonic()
        last_count = 0
        while not self.stop.is_set():
            try:
                data = self.ser.read(4096)
            except serial.SerialException as e:
                print(f"[serial] RX error: {e}", file=sys.stderr)
                self.stop.set()
                return
            if data:
                self._buf.extend(data)

            # Avoid unbounded growth if not wired or unsynced.
            if len(self._buf) > RX_FRAME_LEN * 10:
                pos = self.find_sync()
                if pos is None:
                    del self._buf[:-RX_FRAME_LEN]
                elif pos:
                    del self._buf[:pos]

            while len(self._buf) >= RX_FRAME_LEN:
                if not self.is_rx_frame_start(self._buf, 0):
                    pos = self.find_sync()
                    if pos is None:
                        # Keep last bytes in case a frame starts across reads.
                        del self._buf[:-RX_FRAME_LEN]
                        self.sync_losses += 1
                        break
                    if pos:
                        del self._buf[:pos]
                    if len(self._buf) < RX_FRAME_LEN:
                        break

                frame = bytes(self._buf[:RX_FRAME_LEN])
                del self._buf[:RX_FRAME_LEN]
                self.frames_seen += 1
                now = time.time()
                overlay_text = lcd_overlay_text_from_frame(frame)

                is_blank = self.is_blank_or_keepalive(frame)
                is_pmg_graph = bool(len(frame) >= 2 and frame[:2] == b"\xF1\x25")
                is_menu = bool(is_pmg_graph or self.is_menu_display_frame(frame))
                # PMG companion F1 00 frames have a stable signature and must
                # never replace latest_data_frame.  The earlier recent-F1-25
                # guard was too fragile: if the timing/order changed, the web
                # display still decoded PMG bytes as a normal VFO screen.
                is_pmg_companion = (
                    not is_blank
                    and not is_menu
                    and self.is_pmg_companion_data_frame(frame)
                )
                is_pmg_family = bool(is_pmg_graph or is_pmg_companion)
                is_pmg_aux_refresh = bool(
                    not is_pmg_family
                    and len(frame) >= 2
                    and frame[:2] == b"\xF1\x60"
                    and time.time() <= float(getattr(self, "pmg_active_until", 0.0) or 0.0)
                )

                with self.lock:
                    self.latest_frame = frame
                    self.latest_frame_at = now
                    if is_pmg_family:
                        # Hard latch so PMG cannot flicker back to the normal
                        # screen between the alternating F1 25 and F1 00 frames.
                        self.pmg_active_until = max(self.pmg_active_until, now + 1.25)
                        if is_pmg_graph:
                            self.latest_pmg_menu_frame = frame
                            self.latest_pmg_menu_at = now
                    if overlay_text:
                        self.latest_overlay_text = overlay_text
                        self.latest_overlay_at = now

                    if is_blank:
                        self.ignored_frames_seen += 1
                    elif is_pmg_companion:
                        # Keep the PMG companion available for the PMG renderer,
                        # but do not let it replace the normal VFO/MEM snapshot.
                        self.latest_pmg_data_frame = frame
                        self.latest_pmg_data_at = now
                        self.ignored_frames_seen += 1
                    elif is_pmg_aux_refresh:
                        # F1 60 refresh/blank-ish frames can arrive around PMG.
                        # Do not let them overwrite latest_menu_frame while the
                        # PMG latch is active, otherwise the web LCD exits PMG
                        # for one poll and appears to scramble/flicker.
                        self.ignored_frames_seen += 1
                    elif is_menu:
                        # Always keep the alternate/menu display stream available
                        # as display2. By default it is ignored by the normal
                        # display/decode/diff path, but it can still be inspected
                        # and diffed independently.
                        self.latest_menu_frame = frame
                        self.latest_menu_at = now
                        self.menu_frames_seen += 1
                        if self.ignore_menu:
                            self.ignored_frames_seen += 1
                        else:
                            self.latest_data_frame = frame
                            self.latest_data_at = now
                            self.data_frames_seen += 1
                    else:
                        self.latest_data_frame = frame
                        self.latest_data_at = now
                        self.latest_normal_data_frame = frame
                        self.latest_normal_data_at = now
                        self.data_frames_seen += 1

                rec = globals().get("SAVE_RECORDER")
                if rec is not None:
                    try:
                        if is_blank:
                            kind = "blank"
                        elif is_menu:
                            kind = "display2"
                        elif is_pmg_companion:
                            kind = "pmgdata"
                        else:
                            kind = "display"
                        rec.record_frame(kind, frame, rx_time=now, info={
                            "frames": self.frames_seen,
                            "data": self.data_frames_seen,
                            "menu": self.menu_frames_seen,
                            "ignored": self.ignored_frames_seen,
                            "sync_loss": self.sync_losses,
                            "header": frame[:16].hex(" "),
                        })
                    except Exception as e:
                        if self.verbose:
                            print(f"[save] RX recorder error: {e}", file=sys.stderr)

            if self.verbose:
                now_mono = time.monotonic()
                if now_mono - last_report >= 2.0:
                    fps = (self.frames_seen - last_count) / (now_mono - last_report)
                    print(f"[stat] RX {fps:.1f} frame/s, data={self.data_frames_seen}, menu_ignored={self.menu_frames_seen}, sync_loss={self.sync_losses}")
                    last_report = now_mono
                    last_count = self.frames_seen

    def snapshot(self) -> Tuple[Optional[bytes], Optional[float], int, int, int]:
        with self.lock:
            frame = self.latest_data_frame
            ts = self.latest_data_at
            if self.is_pmg_family_frame(frame):
                frame = self.latest_normal_data_frame
                ts = self.latest_normal_data_at
            return frame, ts, self.frames_seen, self.data_frames_seen, self.sync_losses

    def activity_snapshot(self) -> Tuple[Optional[float], int]:
        """Return timestamp/count of the latest valid RX frame of any kind.

        This is intentionally based on latest_frame_at, not latest_data_at: menu
        and alternate display frames also prove that the radio body is alive.
        The web UI power state is derived from this watchdog.
        """
        with self.lock:
            return self.latest_frame_at, self.frames_seen

    def counters(self) -> Dict[str, int]:
        with self.lock:
            return {
                "frames": self.frames_seen,
                "data": self.data_frames_seen,
                "menu_ignored": self.menu_frames_seen,
                "ignored": self.ignored_frames_seen,
                "sync_loss": self.sync_losses,
            }

    def menu_snapshot(self) -> Tuple[Optional[bytes], Optional[float], int]:
        with self.lock:
            # While PMG is active, always return the last real F1 25 PMG graph
            # frame even if a generic F1 60/menu refresh arrived afterwards.
            # This makes the web renderer stay on the PMG screen until the radio
            # really leaves PMG, instead of alternating PMG/normal LCD.
            if (
                self.latest_pmg_menu_frame is not None
                and time.time() <= float(self.pmg_active_until or 0.0)
            ):
                return self.latest_pmg_menu_frame, self.latest_pmg_menu_at, self.menu_frames_seen
            return self.latest_menu_frame, self.latest_menu_at, self.menu_frames_seen

    def pmg_data_snapshot(self) -> Tuple[Optional[bytes], Optional[float]]:
        with self.lock:
            frame = self.latest_pmg_data_frame
            ts = self.latest_pmg_data_at
            if frame is None and self.is_pmg_companion_data_frame(self.latest_data_frame or b""):
                frame = self.latest_data_frame
                ts = self.latest_data_at
            return frame, ts

    def pmg_menu_snapshot(self) -> Tuple[Optional[bytes], Optional[float]]:
        with self.lock:
            return self.latest_pmg_menu_frame, self.latest_pmg_menu_at

    def pmg_active_snapshot(self) -> bool:
        with self.lock:
            return bool(time.time() <= float(self.pmg_active_until or 0.0))

    def recent_overlay(self, max_age_s: float = 0.9) -> Tuple[Optional[str], Optional[float]]:
        with self.lock:
            text = self.latest_overlay_text
            ts = self.latest_overlay_at
        if not text or not ts:
            return None, None
        if time.time() - ts > max_age_s:
            return None, ts
        return text, ts


def clean_ascii(raw: bytes) -> str:
    # Field strings often have NUL padding and space padding.
    s = raw.rstrip(b"\x00").decode("latin1", errors="replace")
    return s.rstrip()


def ascii_runs(frame: bytes, min_len: int = 3) -> List[Tuple[int, bytes]]:
    # Runs of printable ASCII, allowing spaces. Filter out pure-space runs later.
    return [(m.start(), m.group()) for m in re.finditer(rb"[ -~]{%d,}" % min_len, frame)]


def hexdump(data: bytes, base: int = 0, width: int = 16) -> str:
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hx = " ".join(f"{b:02x}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{base + off:04x}: {hx:<{width*3}} {asc}")
    return "\n".join(lines)


def nonzero_ranges(frame: bytes) -> List[Tuple[int, int]]:
    ranges: List[Tuple[int, int]] = []
    start = last = None
    for i, b in enumerate(frame):
        if b not in (0x00, 0xFF):
            if start is None:
                start = last = i
            elif i == last + 1:
                last = i
            else:
                ranges.append((int(start), int(last)))
                start = last = i
    if start is not None:
        ranges.append((int(start), int(last)))
    return ranges


def diff_ranges(a: bytes, b: bytes) -> List[Tuple[int, int]]:
    """Return contiguous ranges where two same-length byte strings differ."""
    ranges: List[Tuple[int, int]] = []
    start: Optional[int] = None
    last: Optional[int] = None
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            if start is None:
                start = last = i
            elif last is not None and i == last + 1:
                last = i
            else:
                ranges.append((int(start), int(last)))
                start = last = i
    if start is not None and last is not None:
        ranges.append((int(start), int(last)))
    return ranges


def bytes_ascii(bs: bytes) -> str:
    return "".join(chr(x) if 32 <= x < 127 else "." for x in bs)


def format_display_diff(base: bytes, frame: bytes, raw: bool = False, context: int = 0) -> str:
    """Human-readable diff between two RX display frames."""
    out: List[str] = []
    if len(base) != len(frame):
        return f"frame length mismatch: base={len(base)} current={len(frame)}"

    ranges = diff_ranges(base, frame)
    if not ranges:
        return "no differences from the display reference"

    nbytes = sum(b - a + 1 for a, b in ranges)
    out.append(f"display differences: {nbytes} bytes across {len(ranges)} ranges")

    for idx, (a, b) in enumerate(ranges[:80], 1):
        block = a // RX_BLOCK_LEN
        inner_a = a % RX_BLOCK_LEN
        inner_b = b % RX_BLOCK_LEN
        old = base[a:b+1]
        new = frame[a:b+1]
        old_hex = old.hex(" ")
        new_hex = new.hex(" ")
        old_asc = bytes_ascii(old)
        new_asc = bytes_ascii(new)
        if block == b // RX_BLOCK_LEN:
            loc = f"+{a:04d}..+{b:04d} / block {block} +{inner_a:03d}..+{inner_b:03d}"
        else:
            loc = f"+{a:04d}..+{b:04d} / crosses blocks"
        out.append(f"  {idx:02d}. {loc}")
        out.append(f"      base: {old_hex:<47} {old_asc!r}")
        out.append(f"      now : {new_hex:<47} {new_asc!r}")

        if raw:
            ca = max(0, a - context)
            cb = min(len(frame), b + 1 + context)
            out.append(f"      hexdump base +{ca:04d}..+{cb-1:04d}:")
            out.append("      " + hexdump(base[ca:cb], ca).replace("\n", "\n      "))
            out.append(f"      hexdump now  +{ca:04d}..+{cb-1:04d}:")
            out.append("      " + hexdump(frame[ca:cb], ca).replace("\n", "\n      "))

    if len(ranges) > 80:
        out.append(f"  ... {len(ranges) - 80} more ranges")
    return "\n".join(out)


def decode_display_snapshot(frame: bytes, raw: bool = False) -> str:
    out: List[str] = []
    out.append(f"RX frame: {len(frame)} bytes = 5 blocks × 220 bytes")

    # Known/observed ASCII fields in block 0.
    field_32 = clean_ascii(frame[32:40])
    field_64 = clean_ascii(frame[64:72])
    if field_32:
        out.append(f"field +032: {field_32!r}")
    if field_64:
        out.append(f"field +064: {field_64!r}")

    runs = []
    for off, raw_s in ascii_runs(frame, 3):
        text = clean_ascii(raw_s)
        if not text or text.isspace():
            continue
        # Ignore long strings made only of repeated same char if they look like binary artifacts,
        # but keep them visible under raw mode.
        if not raw and len(set(text)) == 1 and len(text) >= 3:
            continue
        runs.append((off, text))

    if runs:
        out.append("ASCII strings found:")
        for off, text in runs:
            block = off // RX_BLOCK_LEN
            inner = off % RX_BLOCK_LEN
            out.append(f"  +{off:04d} / block {block} +{inner:03d}: {text!r}")
    else:
        out.append("no visible ASCII strings found in the data frame")

    # Compact binary status, useful while mapping display/icons/frequency.
    ranges = nonzero_ranges(frame)
    if ranges:
        out.append("non-empty/non-FF binary fields:")
        pieces = []
        for a, b in ranges[:40]:
            pieces.append(f"+{a:03d}..+{b:03d}")
        out.append("  " + "  ".join(pieces))
        if len(ranges) > 40:
            out.append(f"  ... {len(ranges) - 40} more ranges")

    if raw:
        out.append("\nblock 0 hexdump:")
        out.append(hexdump(frame[:RX_BLOCK_LEN], 0))
        # Also show headers of the other blocks so you can confirm sync.
        out.append("\nblock headers:")
        for k in range(5):
            off = k * RX_BLOCK_LEN
            out.append(f"  block {k}, offset +{off}: " + frame[off:off+16].hex(" "))

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Human-readable RX display decode, mapped so far from Free RIG captures.
# This is intentionally partial: unknown values are printed in hex instead of
# being guessed. v6 adds lower-left VOL/SQL/S status decoding and guards the
# decoder so transient/unknown display states do not crash the command loop.
# v7 treats +0017 as a numeric/raw VOL display value instead of ASCII text.
# ---------------------------------------------------------------------------

BLANK_DIGIT = 0x64

SIDE_LEFT = "L"
SIDE_RIGHT = "R"

TONE_MODE = {
    0x00: "",
    0x01: "TN",
    0x02: "TSQ",
    0x03: "RTN",
    0x04: "DCS",
    0x05: "PR",
    0x06: "PAG",
}

MEM_GROUP = {
    0x00: "M-ALL",
    0x02: "M-VHF",
    0x03: "M-UHF",
    0x09: "M-GRP",
}

# Header/source codes seen at +0006 left and +0007 right.
# These combine source and visual role. Values beyond this table are shown raw.
SOURCE_CODE = {
    0x08: "VFO/main",
    0x0A: "VFO/sub",
    # Observed after Setup Menu item 15 HOME CH: display returns to normal
    # frequency screen with source byte +0006 = 0x20.
    0x20: "HOME/main",
    0x40: "MEM/main",
    0x42: "MEM/sub",
    0x44: "MEM/empty",
    0x46: "MEM/empty/sub",
}

# Lower-left display label/status. This is the area where you observed:
# S, SQL, VOL, S-DX, ASP, AUTO-A.
BOTTOM_LABEL = {
    (0x00, 0x00): "S",
    (0x01, 0x00): "SQL",
    (0x02, 0x00): "VOL",
    (0x20, 0x20): "S-DX",
    (0x40, 0x40): "ASP",
    (0x60, 0x60): "AUTO-A",
}


def hx(b: int) -> str:
    return f"0x{b:02x}"


def byte_printable(b: int) -> str:
    return chr(b) if 32 <= b < 127 else ""


def decode_digit_byte(b: int) -> str:
    if 0 <= b <= 9:
        return str(b)
    if b == BLANK_DIGIT:
        return ""
    return "?"


def decode_digit_field(bs: bytes) -> str:
    return "".join(decode_digit_byte(b) for b in bs)


def decode_freq_field(bs: bytes) -> str:
    """Decode 8 digit slots as XXX.xxxxx, with 0x64 as blank."""
    digits = [decode_digit_byte(b) for b in bs]
    if not any(digits):
        return ""
    left = "".join(digits[:3])
    right = "".join(digits[3:])
    if not left:
        return ""
    return f"{left}.{right}" if right else left


def decode_memno_field(bs: bytes) -> str:
    return decode_digit_field(bs)


def decode_ascii_field(bs: bytes) -> str:
    # Names are usually ASCII padded with spaces. A field of dashes is meaningful.
    return bs.decode("latin1", errors="replace").rstrip("\x00").rstrip()


def decode_mode_shift(x: int) -> str:
    if x == 0x00:
        return ""
    # Observed: 0x09 FM, 0x0A AM, 0x20 shift -, 0x40 shift +.
    base = x & 0x1F
    shift_bits = x & 0x60
    mode = {
        0x09: "FM",
        0x0A: "AM",
    }.get(base, f"mode?{hx(base)}")
    shift = {
        0x00: "",
        0x20: "-",
        0x40: "+",
        0x60: "+/-?",
    }.get(shift_bits, f" shift?{hx(shift_bits)}")
    return mode + shift


def decode_source(code: int) -> str:
    return SOURCE_CODE.get(code, f"src?{hx(code)}")


def decode_tone(code: int) -> str:
    return TONE_MODE.get(code, "" if code == 0 else f"tone?{hx(code)}")


def byte_bits(b: int) -> str:
    return format(b, "08b")


def raw_byte_desc(name: str, b: int) -> str:
    return f"{name}={b} ({hx(b)}, b{byte_bits(b)})"


def decode_bottom_status(frame: bytes) -> str:
    """Decode the lower-left status/overlay area.

    Observed:
      +0010/+0011 = label/status code
        00 00 S
        01 00 SQL
        02 00 VOL
        20 20 S-DX
        40 40 ASP
        60 60 AUTO-A

      +0013 = bar/value while SQL/VOL overlay is active.
      +0017 = VOL display value/segments. Observed examples include
              0x08 and 0x40, so do NOT treat it as ASCII text even when
              it happens to be printable, e.g. 0x40 '@'.
    """
    b10 = frame[10]
    b11 = frame[11]
    b13 = frame[13]
    b17 = frame[17]

    label = BOTTOM_LABEL.get((b10, b11))
    if label is None:
        label = f"label?{hx(b10)}/{hx(b11)}"

    if label == "VOL":
        parts = ["VOL"]
        if b13 not in (0x00, BLANK_DIGIT):
            parts.append(raw_byte_desc("bar_raw", b13))
        if b17 not in (0x00, BLANK_DIGIT):
            parts.append(raw_byte_desc("vol_raw", b17))
        if len(parts) == 1:
            parts.append("vol_raw=0")
        return " ".join(parts)

    if label == "SQL":
        parts = ["SQL"]
        if b13 not in (0x00, BLANK_DIGIT):
            parts.append(raw_byte_desc("sql_raw", b13))
        if b17 not in (0x00, BLANK_DIGIT):
            parts.append(raw_byte_desc("detail_raw", b17))
        if len(parts) == 1:
            parts.append("sql_raw=0")
        return " ".join(parts)

    # When label is S/S-DX/ASP/AUTO-A we still surface unexpected value bytes.
    extras: List[str] = []
    if b13 not in (0x00, BLANK_DIGIT):
        extras.append(raw_byte_desc("bar_raw", b13))
    if b17 not in (0x00, BLANK_DIGIT):
        extras.append(raw_byte_desc("detail_raw", b17))
    return label + ((" " + " ".join(extras)) if extras else "")


# v10: split lower display status into left/right.
# v11: +0015 is also used by the TX/RX meter/activity overlay, so when
#      +0004 says TX/RX active we suppress it from LOWER R and decode it
#      separately as ACTIVITY.
# v12: right-side TX can leave +0004 at 0x00 but still set +0192=0x11;
#      treat that as activity too, otherwise decode showed "idle tx_flag=0x11".
# Observed/inferred layout around block-0 offsets +0010..+0018:
#   +0010 = lower label/status, left side
#   +0011 = lower label/status, right side
#   +0013 = lower value/bar candidate, left side  (confirmed for left SQL/VOL)
#   +0014 = lower value/bar candidate, right side (v28/v29: +0014 candidate for right value; +0015 is activity meter)
#   +0015 = TX/RX activity meter, not right SQL/VOL
#   +0017 = VOL display value/segments, left side (confirmed)
#   +0018 = VOL display value/segments, right side (inferred, surfaced as raw)
LOWER_LABEL_BYTE = {
    0x00: "S",
    0x01: "SQL",
    0x02: "VOL",
    0x20: "S-DX",
    0x40: "ASP",
    0x60: "AUTO-A",
}


def decode_lower_label_byte(b: int) -> str:
    # The lower-display label byte carries two things at once:
    #   high bits 0x00/0x20/0x40/0x60 = S / S-DX / ASP / AUTO-A
    #   low bits  0x01/0x02 can temporarily override that base label with
    #   SQL / VOL while the user is adjusting squelch or volume.
    #   style bits 0x00/0x04/0x08/0x0c encode the S-meter symbol style.
    # Menu 05 changes those style bits. They must not turn the lower label
    # into UNKNOWN, and mixed values like 0x22 must still decode as SQL/VOL.
    if b in LOWER_LABEL_BYTE:
        return LOWER_LABEL_BYTE[b]
    overlay = b & 0x03
    if overlay == 0x01:
        return "SQL"
    if overlay == 0x02:
        return "VOL"
    if (b & 0x0C) in (0x00, 0x04, 0x08, 0x0C):
        base = b & 0x60
        if base in LOWER_LABEL_BYTE:
            return LOWER_LABEL_BYTE[base]
    return f"label?{hx(b)}"


def decode_lower_side(frame: bytes, side: str) -> str:
    """Decode lower S/SQL/VOL area for one side.

    Left side is confirmed from captures. Right side label at +0011 is strongly
    implied by the S-DX/ASP/AUTO-A captures where +0010/+0011 move together;
    the right-side value offsets are still shown as raw/inferred so future
    captures can confirm them without breaking the decoder.
    """
    if side == SIDE_LEFT:
        label_off = 10
        value_off = 13
        vol_off = 17
        tag = "L"
    else:
        label_off = 11
        # v28: +0015 is the global TX/RX meter; right lower value is
        # much more likely the symmetric +0014. Using +0015 made SQL R
        # stay at zero or collide with RX meter.
        value_off = 14
        vol_off = 18
        tag = "R"

    label_b = frame[label_off]
    value_b = frame[value_off]
    vol_b = frame[vol_off]
    label = decode_lower_label_byte(label_b)

    # +0015 is the TX/RX meter, so it is deliberately not used as a
    # lower-right value anymore. value_b is +0013 for left, +0014 for right.
    value_b_for_lower = value_b

    parts = [f"LOWER {tag}: {label}"]

    if label == "VOL":
        if value_b_for_lower not in (0x00, BLANK_DIGIT):
            parts.append(raw_byte_desc("bar_raw", value_b_for_lower))
        if vol_b not in (0x00, BLANK_DIGIT):
            parts.append(raw_byte_desc("vol_raw", vol_b))
        if len(parts) == 1:
            parts.append("vol_raw=0")
    elif label == "SQL":
        if value_b_for_lower not in (0x00, BLANK_DIGIT):
            parts.append(raw_byte_desc("sql_raw", value_b_for_lower))
        if vol_b not in (0x00, BLANK_DIGIT):
            parts.append(raw_byte_desc("detail_raw", vol_b))
        if len(parts) == 1:
            parts.append("sql_raw=0")
    else:
        # Surface non-zero candidates even when the label is S/S-DX/ASP/AUTO-A.
        # This is useful while mapping right-side squelch/volume.
        if value_b_for_lower not in (0x00, BLANK_DIGIT):
            parts.append(raw_byte_desc("value_raw", value_b_for_lower))
        if vol_b not in (0x00, BLANK_DIGIT):
            parts.append(raw_byte_desc("detail_raw", vol_b))

    return " ".join(parts)


def decode_lower_statuses(frame: bytes) -> str:
    return decode_lower_side(frame, SIDE_LEFT) + "\n" + decode_lower_side(frame, SIDE_RIGHT)


ACTIVITY_CODE = {
    0x00: "idle",
    0x02: "TX/PTT",
    0x04: "RX/audio",
}


def decode_activity(frame: bytes) -> str:
    """Decode the observed TX/RX bar/activity overlay.

    Observed from captures:
      idle:
        +0004 = 0x00
        +0015 = 0x00
        +0192 = 0x10
        +0193 = 0x00

      PTT/TX, left-side capture:
        +0004 = 0x02
        +0015 = 0x03     meter/bar raw value
        +0192 = 0x11     TX flag/indicator bit changed from 0x10

      PTT/TX, right-side capture reported later:
        +0004 can remain 0x00
        +0192 = 0x11     still means TX/PTT active
        Older decoder printed: ACTIVITY: idle tx_flag=0x11

      RX audio / squelch open:
        +0004 = 0x04
        +0015 = 0x0a     meter/bar raw value
        +0193 = 0x08
        +0222/+0442/+0662/+0882 = 0x08 repeated in blocks 1..4
    """
    activity_b = frame[4]
    meter_b = frame[15]
    tx_flag = frame[192]
    rx_flag = frame[193]
    repeated_rx_flags = [frame[RX_BLOCK_LEN * k + 2] for k in range(1, 5)]

    main_side = "LEFT" if frame[3] == 0x02 else "RIGHT" if frame[3] == 0x01 else f"?{hx(frame[3])}"
    rx_repeated_active = any(b != 0x00 for b in repeated_rx_flags)

    # Prefer explicit activity byte when present, but allow the later-observed
    # flag-only cases. This fixes right-side TX where +0004 stays 0x00 but
    # +0192 becomes 0x11.
    if activity_b == 0x02 or tx_flag == 0x11:
        status = "TX/PTT"
    elif activity_b == 0x04 or rx_flag != 0x00 or rx_repeated_active:
        status = "RX/audio"
    elif activity_b == 0x00:
        status = "idle"
    else:
        status = f"activity?{hx(activity_b)}"

    # Keep the idle line compact but still useful if unusual flags appear.
    if status == "idle":
        extras: List[str] = []
        if meter_b not in (0x00, BLANK_DIGIT):
            extras.append(raw_byte_desc("meter_raw", meter_b))
        if tx_flag != 0x10:
            extras.append(f"tx_flag={hx(tx_flag)}")
        if rx_flag != 0x00:
            extras.append(f"rx_flag={hx(rx_flag)}")
        if rx_repeated_active:
            extras.append("rx_rep=" + "/".join(hx(b) for b in repeated_rx_flags))
        return "ACTIVITY: idle" + ((" " + " ".join(extras)) if extras else "")

    parts = [f"ACTIVITY: {status}"]
    if status == "TX/PTT":
        parts.append(f"side={main_side}")
    if meter_b not in (0x00, BLANK_DIGIT):
        parts.append(raw_byte_desc("meter_raw", meter_b))
    parts.append(f"activity={hx(activity_b)}")
    parts.append(f"tx_flag={hx(tx_flag)}")
    parts.append(f"rx_flag={hx(rx_flag)}")
    if rx_repeated_active:
        parts.append("rx_rep=" + "/".join(hx(b) for b in repeated_rx_flags))
    return " ".join(parts)


LCD_OVERLAY_FLAG_OFF = 155
LCD_OVERLAY_LEN_OFF = 157
LCD_OVERLAY_TEXT_OFF = 159
LCD_OVERLAY_TEXT_LEN = 6


def lcd_overlay_char(b: int) -> str:
    """Decode the radio's compact LCD text codes used by MUTE/LOCK overlays.

    Mapped from captures:
      LOCK   = 15 18 0c 14
      UNLOCK = 1e 17 15 18 0c 14
    This matches A=0x0a, B=0x0b, ... Z=0x23.
    """
    if b == 0x00:
        return ""
    if 0x0A <= b <= 0x23:
        return chr(ord("A") + b - 0x0A)
    if 32 <= b < 127:
        return chr(b)
    return "?"


def lcd_overlay_raw_word(frame: bytes) -> str:
    if len(frame) <= LCD_OVERLAY_TEXT_OFF:
        return ""
    declared_len = frame[LCD_OVERLAY_LEN_OFF] if len(frame) > LCD_OVERLAY_LEN_OFF else 0
    raw = frame[LCD_OVERLAY_TEXT_OFF:LCD_OVERLAY_TEXT_OFF + LCD_OVERLAY_TEXT_LEN]
    if 1 <= declared_len <= LCD_OVERLAY_TEXT_LEN:
        raw = raw[:declared_len]
    return "".join(lcd_overlay_char(b) for b in raw).strip()


def lcd_overlay_text_from_frame(frame: bytes) -> str:
    """Return visible LCD overlay text, or empty when no overlay is active.

    +0155 is the common overlay enable flag. The old decoder treated every
    +0155=0x03 as MUTE. Short Power presses show LOCK/UNLOCK using the same
    overlay graphic, with text encoded at +0159..+0164.
    """
    if len(frame) <= LCD_OVERLAY_FLAG_OFF or frame[LCD_OVERLAY_FLAG_OFF] != 0x03:
        return ""
    word = lcd_overlay_raw_word(frame)
    if word in ("LOCK", "UNLOCK", "MUTE"):
        return word
    # PMG? appears in several dumps as a stale compact overlay word while the
    # radio is actually on a different screen (for example during memory-add
    # flows). Do not render it as a visible popup in the web UI.
    if word == "PMG?":
        return ""
    # Keep backward compatibility with the original MUTE detection. Some
    # captures only needed +0155=0x03 to identify MUTE.
    return word or "MUTE"




def _compact_overlay_text(raw: bytes) -> str:
    """Decode compact overlay/prompt text where 0x64 is a visible space.

    Some confirmation prompts encode a visual right-arrow as 0x4a 0x51.
    In normal ASCII 0x51 is 'Q', but on this LCD prompt layer it renders as
    the arrow head.  Decode that pair as ``->`` so clone prompts do not show
    a bogus Q.
    """
    chars: List[str] = []
    i = 0
    bs = bytes(raw)
    while i < len(bs):
        b = int(bs[i])
        if b == 0x00:
            chars.append(" ")
            i += 1
            continue
        if b == 0x4A and i + 1 < len(bs) and int(bs[i + 1]) == 0x51:
            chars.append("->")
            i += 2
            continue
        if b == 0x51:
            chars.append(">")
            i += 1
            continue
        ch = _lcd_menu_value_char(b)
        if ch == "?":
            ch = " "
        chars.append(ch)
        i += 1
    return re.sub(r"\s+", " ", "".join(chars)).strip()


def pmg_clear_confirm_overlay_from_frame(frame: Optional[bytes]) -> Optional[dict]:
    """Decode the PMG CLEAR confirmation prompt seen on the normal display path.

    In the menu 18 save, pressing PMG CLEAR does not update display2; the body
    overlays a prompt on the normal F3 13 display.  The observed fields are:
      +0155 = 0x09          prompt active
      +0156 = 0/1           selected soft choice, moved by BR_LEFT/BR_RIGHT
      +0159..+0168          compact text "PMG MEMORY"
      +0174..+0178          compact text "CLEAR"

    The radio UI defaults to the right-hand safe choice in the capture, so the
    GUI maps 0 -> OK and 1 -> CANCEL.
    """
    if frame is None or len(frame) < 184:
        return None
    if frame[155] != 0x09:
        return None
    title = _compact_overlay_text(frame[159:169])
    message = _compact_overlay_text(frame[174:179])
    if title != "PMG MEMORY" or message != "CLEAR":
        return None
    selected = int(frame[156]) if frame[156] in (0x00, 0x01) else 1
    options = [
        {"text": "OK", "selected": selected == 0},
        {"text": "CANCEL", "selected": selected == 1},
    ]
    return {
        "active": True,
        "kind": "confirm",
        "text": f"{title} {message}",
        "title": title,
        "message": message,
        "selected_index": selected,
        "options": options,
        "raw": "+0155..+0183: " + frame[155:184].hex(" "),
    }


def setup_action_confirm_overlay_from_frame(frame: Optional[bytes]) -> Optional[dict]:
    """Decode one-shot CLONE/RESET confirmation prompts from the normal display.

    Captured action items:
      58 This -> Other     flag +0155=0x09, text at +0159 and +0174
      59 Other -> This     flag +0155=0x09, text at +0159 and +0174
      62 FACTORY RESET     flag +0155=0x07, title at +0159

    They use the same soft choice byte as PMG CLEAR: +0156 = 0 for OK,
    +0156 = 1 for CANCEL.  Unknown text is deliberately ignored so we do not
    invent prompts for unrelated overlays.
    """
    if frame is None or len(frame) < 184:
        return None
    flag = int(frame[155])
    if flag not in (0x07, 0x09):
        return None

    selected = int(frame[156]) if frame[156] in (0x00, 0x01) else 1
    options = [
        {"text": "OK", "selected": selected == 0},
        {"text": "CANCEL", "selected": selected == 1},
    ]

    title = ""
    message = ""
    action = ""

    if flag == 0x09:
        left = _compact_overlay_text(frame[159:169])
        right = _compact_overlay_text(frame[174:184])
        full = _compact_overlay_text(frame[159:184])
        if left == "This radio" and right == "Other":
            title = "This radio"
            message = "→ Other"
            action = "This radio -> Other"
        elif (left == "Other" or left.startswith("Other ->")) and right == "This radio":
            title = "Other"
            message = "→ This radio"
            action = "Other -> This radio"
        elif full.startswith("Other ->") and "This radio" in full:
            title = "Other"
            message = "→ This radio"
            action = "Other -> This radio"
        else:
            return None
    elif flag == 0x07:
        # FACTORY RESET has a single compact title and no second line.  Keep a
        # MEMORY CH RESET branch for the same observed prompt format if that
        # action is captured later.
        text = _compact_overlay_text(frame[159:174])
        if text.startswith("FACTORY RESET"):
            title = "FACTORY RESET"
            message = ""
            action = title
        elif text.startswith("MEMORY CH RESET"):
            title = "MEMORY CH RESET"
            message = ""
            action = title
        else:
            return None

    return {
        "active": True,
        "kind": "confirm",
        "text": (title + (" " + message if message else "")).strip(),
        "title": title,
        "message": message,
        "selected_index": selected,
        "options": options,
        "action": action,
        "raw": "+0155..+0189: " + frame[155:190].hex(" "),
    }


def memory_channel_confirm_overlay_from_frame(frame: Optional[bytes]) -> Optional[dict]:
    """Decode the real menu 16 DELETE?/OVER WRITE? confirmation popup.

    Learned from save_20260506_195940.zip:
      +0155 = 0x07          popup active
      +0156 = selected soft choice, 0 OK / 1 CANCEL
      +0157 = prompt length
      +0159 = prompt text, e.g. DELETE? encoded as 0d 0e 15 0e 1d 0e 52

    The DELETE? text may remain in normal F1 00 buffers with +0155=0x00 before
    and after the popup.  Never show the dialog unless +0155 is exactly 0x07.
    """
    if frame is None or len(frame) < 184:
        return None
    if frame[0] not in (0xF1, 0xF3):
        return None
    if int(frame[155]) != 0x07:
        return None

    def _popup_text(raw: bytes) -> str:
        chars: List[str] = []
        for b in raw:
            b = int(b)
            if b in (0x00, 0x64):
                chars.append(" ")
                continue
            if b == 0x52:
                chars.append("?")
                continue
            chars.append(_lcd_menu_value_char(b).replace("?", " "))
        return re.sub(r"\s+", " ", "".join(chars)).strip()

    declared_len = int(frame[157]) if len(frame) > 157 else 0
    if not (1 <= declared_len <= 24):
        return None
    prompt = _popup_text(frame[159:159 + declared_len])
    if prompt not in ("DELETE?", "OVER WRITE?"):
        return None

    selected = int(frame[156]) if frame[156] in (0x00, 0x01) else 1
    src_name = clean_ascii(frame[32:40]).strip()
    dst_name = clean_ascii(frame[64:72]).strip()
    message = ""
    if prompt == "OVER WRITE?":
        if src_name and dst_name:
            message = f"{src_name} -> {dst_name}"
        else:
            message = src_name or dst_name
    else:
        # DELETE? on the radio prompt is title-only.  Do not append a memory
        # name from stale/nearby bytes; it can be wrong and is not shown there.
        message = ""

    return {
        "active": True,
        "kind": "confirm",
        "text": (prompt + (f" {message}" if message else "")).strip(),
        "title": prompt,
        "message": message,
        "selected_index": selected,
        "options": [
            {"text": "OK", "selected": selected == 0},
            {"text": "CANCEL", "selected": selected == 1},
        ],
        "raw": "+0155..+0169: " + frame[155:170].hex(" "),
    }


def setup_confirm_overlay_from_frame(frame: Optional[bytes]) -> Optional[dict]:
    """Decode any known confirmation prompt, preserving specific decoders first."""
    return (
        pmg_clear_confirm_overlay_from_frame(frame)
        or setup_action_confirm_overlay_from_frame(frame)
    )


def decode_lcd_overlay(frame: bytes) -> str:
    text = lcd_overlay_text_from_frame(frame)
    if not text:
        return "OVERLAY: off"
    raw = frame[LCD_OVERLAY_TEXT_OFF:LCD_OVERLAY_TEXT_OFF + LCD_OVERLAY_TEXT_LEN]
    declared_len = frame[LCD_OVERLAY_LEN_OFF] if len(frame) > LCD_OVERLAY_LEN_OFF else 0
    return f"OVERLAY: {text} flag={hx(frame[LCD_OVERLAY_FLAG_OFF])} len={declared_len} text_raw={raw.hex(' ')}"


def decode_mute(frame: bytes) -> str:
    # Kept for compatibility with old command output name.
    return decode_lcd_overlay(frame)


@dataclass
class DecodedSide:
    side: str
    is_main: bool
    source_code: int
    source: str
    mem_group: str
    mem_no: str
    name: str
    freq: str
    mode: str
    tone: str

    def render(self) -> str:
        star = "*" if self.is_main else " "
        parts: List[str] = [f"{star}{self.side}:"]
        parts.append(self.source)

        show_mem_fields = self.source.startswith("MEM")
        if show_mem_fields:
            if self.mem_group:
                parts.append(self.mem_group)
            if self.mem_no:
                parts.append(self.mem_no)
            if self.name:
                parts.append(repr(self.name))
        else:
            # Some non-memory fields may still contain mapped/ASCII artifacts.
            # Do not print them in the main compact line, otherwise VFO can look
            # like MEM when the raw display frame reuses those areas.
            pass

        if self.freq:
            parts.append(self.freq)
        if self.mode:
            parts.append(self.mode)
        if self.tone:
            parts.append(self.tone)
        return " ".join(parts)


def decode_side(frame: bytes, side: str) -> DecodedSide:
    if side == SIDE_LEFT:
        source_off = 6
        mode_off = 8
        mem_group_off: Optional[int] = 12
        mem_no_slice = slice(19, 22)
        tone_off = 27
        name_slice = slice(32, 40)
        freq_slice = slice(96, 104)
        is_main = frame[3] == 0x02
    else:
        source_off = 7
        mode_off = 9
        # Right-side memory group mapped from display diffs:
        #   +0031 = 00 M-ALL, 02 M-VHF, 03 M-UHF, 09 M-GRP.
        # Do not use +0013: it belongs to lower display / VOL/SQL activity.
        mem_group_off = 31
        mem_no_slice = slice(22, 25)
        tone_off = 29
        name_slice = slice(64, 72)
        freq_slice = slice(108, 116)
        is_main = frame[3] == 0x01

    source_code = frame[source_off]
    mem_group = ""
    if mem_group_off is not None:
        g = frame[mem_group_off]
        # Only show a memory group when the side is actually in memory mode.
        # The right group byte (+0013) can overlap with lower-display values
        # in other states, so unknown values are left blank rather than shown
        # as a false group.
        if source_code in (0x40, 0x42, 0x44, 0x46):
            mem_group = MEM_GROUP.get(g, "")

    return DecodedSide(
        side=side,
        is_main=is_main,
        source_code=source_code,
        source=decode_source(source_code),
        mem_group=mem_group,
        mem_no=decode_memno_field(frame[mem_no_slice]),
        name=decode_ascii_field(frame[name_slice]),
        freq=decode_freq_field(frame[freq_slice]),
        mode=decode_mode_shift(frame[mode_off]),
        tone=decode_tone(frame[tone_off]),
    )


def decode_display_human(frame: bytes, raw: bool = False) -> str:
    """Return a compact human-readable view of mapped display fields."""
    if len(frame) < RX_BLOCK_LEN:
        return f"frame too short: {len(frame)} bytes"

    left = decode_side(frame, SIDE_LEFT)
    right = decode_side(frame, SIDE_RIGHT)
    main = "LEFT" if frame[3] == 0x02 else "RIGHT" if frame[3] == 0x01 else f"?{hx(frame[3])}"

    out: List[str] = []
    out.append("DISPLAY DECODE, campi mappati finora")
    out.append(f"MAIN: {main}")
    out.append(left.render())
    out.append(right.render())
    out.append(decode_lower_statuses(frame))
    out.append(decode_activity(frame))
    if lcd_overlay_text_from_frame(frame) or raw:
        out.append(decode_lcd_overlay(frame))

    if raw:
        out.append("")
        out.append("Dettaglio campi:")
        out.append(f"  +0003 main side        = {hx(frame[3])}")
        out.append(f"  +0006/+0007 sources   = {hx(frame[6])} / {hx(frame[7])}")
        out.append(f"  +0008/+0009 mode      = {hx(frame[8])} / {hx(frame[9])}")
        out.append(f"  +0010/+0011 lower labels L/R = {hx(frame[10])} / {hx(frame[11])}")
        out.append(f"  +0012/+0031 mem group L/R = {hx(frame[12])} / {hx(frame[31])}")
        out.append(f"  +0013/+0014 lower values L/R = {hx(frame[13])} ({frame[13]}) / {hx(frame[14])} ({frame[14]})")
        out.append(f"  +0015 activity meter      = {hx(frame[15])} ({frame[15]}, b{byte_bits(frame[15])})")
        out.append(f"  +0017/+0018 lower VOL/detail L/R = {hx(frame[17])} ({frame[17]}, b{byte_bits(frame[17])}) / {hx(frame[18])} ({frame[18]}, b{byte_bits(frame[18])})")
        out.append(f"  +0004 activity        = {hx(frame[4])}")
        out.append(f"  +0155 overlay flag    = {hx(frame[155])}")
        out.append(f"  +0157 overlay len     = {hx(frame[157])} ({frame[157]})")
        out.append(f"  +0159..0164 overlay text = {frame[159:165].hex(' ')} -> {lcd_overlay_raw_word(frame)!r}")
        out.append(f"  +0192/+0193 TX/RX flags = {hx(frame[192])} / {hx(frame[193])}")
        out.append("  +0222/+0442/+0662/+0882 RX repeated flags = " + " / ".join(hx(frame[RX_BLOCK_LEN * k + 2]) for k in range(1, 5)))
        out.append(f"  +0019..0021 L mem no  = {frame[19:22].hex(' ')}")
        out.append(f"  +0022..0024 R mem no  = {frame[22:25].hex(' ')}")
        out.append(f"  +0027/+0029 tone      = {hx(frame[27])} / {hx(frame[29])}")
        out.append(f"  +0032..0039 L name    = {frame[32:40].hex(' ')}")
        out.append(f"  +0064..0071 R name    = {frame[64:72].hex(' ')}")
        out.append(f"  +0096..0103 L freq    = {frame[96:104].hex(' ')}")
        out.append(f"  +0108..0115 R freq    = {frame[108:116].hex(' ')}")

    return "\n".join(out)


def print_display_decode(rx: Optional[BodyRx], raw: bool = False) -> None:
    if rx is None or not rx.enabled:
        print("[rx] RX disabled")
        return
    frame, ts, seen, data_seen, sync_losses = rx.snapshot()
    if frame is None:
        print("[rx] no normal display frame received yet. Check RX/GND and verify the body is transmitting.")
        print(f"     stats: frames={seen}, data={data_seen}, sync_loss={sync_losses}")
        return
    age = time.time() - ts if ts else -1
    extra = ""
    if rx is not None:
        c = rx.counters()
        if c.get("menu_ignored", 0):
            extra = f", menu_ignored={c['menu_ignored']}"
    print(f"[rx] last normal display frame: {age:.2f}s ago; frames={seen}, data={data_seen}, sync_loss={sync_losses}{extra}")
    try:
        print(decode_display_human(frame, raw=raw))
    except Exception as e:
        print(f"[decode] error: {type(e).__name__}: {e}")
        print("[decode] printing raw snapshot for debugging anyway:")
        print(decode_display_snapshot(frame, raw=True))



def menu_byte_to_text(b: int) -> str:
    """Human-friendly representation of one byte in the menu text area."""
    if b in (0x00, 0x64):
        return " "
    if 32 <= b <= 126:
        return chr(b)
    return f"\\x{b:02x}"


def menu_text(raw: bytes) -> str:
    """Decode a small menu-text field without pretending it is C-string text."""
    return "".join(menu_byte_to_text(b) for b in raw).rstrip()


def menu_visible_guess(text: str) -> str:
    """Best-effort visible label for fixed menu cells.

    Some quick-menu cells contain extra bytes after a padded short label, e.g.
    raw text like 'M->V   WE' or 'STEP   MI'. On the real display the visible
    soft-key label appears to be only the first part. Keep the raw text
    available, but use this shorter guess for the human grid.
    """
    t = text.rstrip()
    if "   " in t:
        head = t.split("   ", 1)[0].rstrip()
        if head:
            return head
    return t


def menu_area_preview(frame: bytes, start: int = 60, end: int = 151) -> str:
    """Single-line view of the menu text/control area.

    Printable bytes are literal; 0x00/0x64 are spaces; other control bytes are
    rendered as \\xNN. This makes it visible when a menu item is split by our
    guessed cell boundaries.
    """
    return "".join(menu_byte_to_text(b) for b in frame[start:end]).rstrip()


MENU_ITEM_ATTRS = {0x10, 0x11, 0x20, 0x21, 0x30, 0x31}


def is_printable_menu_text_start(b: int) -> bool:
    return 32 <= b <= 126 and chr(b) not in "0123456789"


def find_numbered_menu_items(frame: bytes, start: int = 60, end: int = 155) -> List[Tuple[int, int, int, str]]:
    """Find numbered menu-list items such as: 07 10 'TX POWER'.

    Observed in the long/general menu:
      +0061: 07 10 'TX POWER'
      +0096: 08 10 'MIC GAIN'
      +0131: 09 30 'VOX'

    The items are not on 10-byte boundaries, so the old 3x3 cell parser split
    'TX POWER' into 'TX POWE' + 'R'. This scanner treats number+attribute as
    a start marker and then reads the printable label that follows.
    """
    items: List[Tuple[int, int, int, str]] = []
    i = start
    n = min(end, len(frame) - 3)
    while i < n:
        num = frame[i]
        attr = frame[i + 1]
        if 1 <= num <= 99 and attr in MENU_ITEM_ATTRS and is_printable_menu_text_start(frame[i + 2]):
            # Avoid matching a byte inside a printable label.
            if i > start and 32 <= frame[i - 1] <= 126:
                i += 1
                continue

            j = i + 2
            chars: List[str] = []
            # Labels observed so far fit in <= 10 chars, but allow a bit more.
            while j < min(i + 18, len(frame)):
                b = frame[j]
                if b in (0x00, 0x64):
                    break
                if 32 <= b <= 126:
                    chars.append(chr(b))
                    j += 1
                    continue
                break

            text = "".join(chars).strip()
            if len(text) >= 2:
                items.append((i, num, attr, text))
                i = max(j, i + 1)
                continue
        i += 1

    return items


def decode_menu_grid_slots(frame: bytes) -> List[Tuple[int, int, int, str, str, bytes]]:
    """Decode the F-menu-like 3x3 grid.

    The text starts at +61, +71, +81, ...; the byte before each text field is a
    prefix/status byte. Each raw cell is 9 bytes, but the visible label may be
    shorter: captures show cells such as 'M->V   WE' where the display appears
    to show only 'M->V'. Return both visible guess and raw decoded text.
    """
    cells: List[Tuple[int, int, int, str, str, bytes]] = []
    for idx in range(9):
        text_off = 61 + 10 * idx
        prefix_off = text_off - 1
        if text_off + 9 > len(frame):
            break
        raw = frame[text_off:text_off + 9]
        prefix = frame[prefix_off]
        txt_raw = menu_text(raw)
        txt_visible = menu_visible_guess(txt_raw)
        cells.append((idx, text_off, prefix, txt_visible, txt_raw, raw))
    return cells


def decode_menu_layout(frame: bytes, raw: bool = False) -> str:
    """Menu/config display helper, intentionally pre-semantic.

    It does not yet decode menu parameters. It only separates two layouts we
    have observed:
      1) numbered list: number byte + attribute byte + printable label
      2) F-menu grid: nine 9-byte text cells starting at +61, +71, ...
    """
    out: List[str] = []
    out.append(f"RX display2/menu frame: {len(frame)} bytes = 5 blocks × 220 bytes")
    out.append(f"header: {frame[:16].hex(' ')}")

    area = menu_area_preview(frame)
    if area:
        out.append("menu text area +0060..+0150:")
        out.append(f"  {area!r}")

    numbered = find_numbered_menu_items(frame)
    if numbered:
        out.append("numbered menu list detected:")
        for off, num, attr, text in numbered:
            # Observed attr 0x30 looks like selected/current; attr 0x10 like normal.
            mark = "*" if attr in (0x30, 0x31, 0x20, 0x21) else " "
            out.append(f"  {mark} item {num:02d} @+{off:04d}: attr={hx(attr)} text={text!r}")

    cells = decode_menu_grid_slots(frame)
    # Show grid when there are useful cell labels, or always in raw mode.
    useful_cells = [(idx, off, prefix, txt, raw_txt, rb) for idx, off, prefix, txt, raw_txt, rb in cells if txt.strip() or raw_txt.strip()]
    if raw or not numbered or len(useful_cells) >= 4:
        out.append("candidate 3×3 grid cells, text from +61,+71,...:")
        for idx, off, prefix, txt, raw_txt, raw_bytes in cells:
            row = idx // 3
            col = idx % 3
            extra = "" if txt == raw_txt else f" raw_text={raw_txt!r}"
            out.append(
                f"  cell {idx} r{row}c{col} text@+{off:04d}: "
                f"prefix@+{off-1:04d}={hx(prefix)} text={txt!r}{extra} raw={raw_bytes.hex(' ')}"
            )

        out.append("candidate 3×3 grid, using the estimated visible text:")
        for r in range(3):
            row_cells = cells[r * 3:(r + 1) * 3]
            rendered = []
            for _, _, prefix, txt, raw_txt, _ in row_cells:
                mark = "*" if prefix not in (0x00, 0x64) else " "
                rendered.append(f"{mark}{txt:<9}")
            out.append("  " + " | ".join(rendered))

    ctrl_ranges = nonzero_ranges(frame[0:60])
    if ctrl_ranges:
        out.append("control/graphics area +0000..+0059 non-zero:")
        out.append("  " + "  ".join(f"+{a:03d}..+{b:03d}" for a, b in ctrl_ranges))

    if raw:
        out.append("\nblock 0 hexdump:")
        out.append(hexdump(frame[:RX_BLOCK_LEN], 0))
        out.append("\nblock headers:")
        for k in range(5):
            off = k * RX_BLOCK_LEN
            out.append(f"  block {k}, offset +{off}: " + frame[off:off+16].hex(" "))

    return "\n".join(out)


def print_menu_display(rx: Optional[BodyRx], raw: bool = False) -> None:
    if rx is None or not rx.enabled:
        print("[rx] RX disabled")
        return
    frame, ts, menu_seen = rx.menu_snapshot()
    if frame is None:
        print("[rx] no display2/menu frame received yet")
        return
    age = time.time() - ts if ts else -1
    print(f"[rx] last display2/menu frame: {age:.2f}s ago; menu_frames={menu_seen}")
    if age > 2.0:
        print("[rx] note: this display2 frame is old; the current page may not be arriving as display2")
    print(decode_menu_layout(frame, raw=raw))


def print_menu_cells(rx: Optional[BodyRx], raw: bool = False) -> None:
    # Kept for compatibility with the existing command name. In v15 it uses the
    # improved auto layout instead of the old fixed +60/+70 parser.
    print_menu_display(rx, raw=raw)

def get_rx_menu_frame(rx: Optional[BodyRx]) -> Tuple[Optional[bytes], Optional[float], int, int, int]:
    if rx is None or not rx.enabled:
        return None, None, 0, 0, 0
    frame, ts, menu_seen = rx.menu_snapshot()
    c = rx.counters()
    return frame, ts, c.get("frames", 0), menu_seen, c.get("sync_loss", 0)


def display2_watchdiff(rx: Optional[BodyRx], base: Optional[bytes], seconds: float = 10.0, raw: bool = False) -> None:
    if rx is None or not rx.enabled:
        print("[rx] RX disabled")
        return
    if base is None:
        frame, ts, seen, menu_seen, sync_losses = get_rx_menu_frame(rx)
        if frame is None:
            print("[rx] no display2/menu frame available to use as a reference")
            return
        base = frame
        print("[rx] temporary display2 reference learned from the current frame")
    print(f"[rx] display2 watchdiff for {seconds:.1f}s; Ctrl-C to interrupt")
    end = time.monotonic() + seconds
    last: Optional[bytes] = None
    try:
        while time.monotonic() < end:
            frame, ts, seen, menu_seen, sync_losses = get_rx_menu_frame(rx)
            if frame is not None and frame != last:
                last = frame
                if frame != base:
                    age = time.time() - ts if ts else -1
                    print(f"\n--- display2 diff, age={age:.2f}s, rx frames={seen}, menu={menu_seen} ---")
                    print(format_display_diff(base, frame, raw=raw, context=8 if raw else 0))
            time.sleep(0.03)
    except KeyboardInterrupt:
        print()


def display2_watch(rx: Optional[BodyRx], seconds: float = 10.0) -> None:
    if rx is None or not rx.enabled:
        print("[rx] RX disabled")
        return
    print(f"[rx] watch display2/menu for {seconds:.1f}s; Ctrl-C to interrupt")
    end = time.monotonic() + seconds
    last: Optional[bytes] = None
    try:
        while time.monotonic() < end:
            frame, ts, seen, menu_seen, sync_losses = get_rx_menu_frame(rx)
            if frame is not None and frame != last:
                last = frame
                print("\n--- display2 changed ---")
                print_menu_display(rx, raw=False)
            time.sleep(0.05)
    except KeyboardInterrupt:
        print()

def parse_raw(tokens: List[str]) -> Tuple[State, int]:
    """
    raw 13=0x1c 14=0x4b [duration]
    raw 0x0d=0x1c 0x0e=0x4b 80f
    raw 13=0x1c 14=0x4b 250ms
    """
    if not tokens:
        raise ValueError("usage: raw offset=val [offset=val ...] [ms|framesf]")

    frames = DEFAULT_KEY_FRAMES
    if "=" not in tokens[-1]:
        frames = parse_duration_token(tokens[-1], DEFAULT_KEY_FRAMES)
        tokens = tokens[:-1]

    ops: State = []
    for tok in tokens:
        if "=" not in tok:
            raise ValueError(f"invalid raw argument: {tok}")
        left, right = tok.split("=", 1)
        off = int(left, 0)
        val = int(right, 0)
        ops.append(("set", off, val))
    return ops, frames


def print_help() -> None:
    print(
        f"""
Commands:
  list                         show available names
  <name> [duration]            send a pulse; e.g. band, vm 300ms, ul_left 20f
  press <name> [duration]      same as <name> [duration]
  hold <name>                  keep that button/state active
  release                      clear hold/pulse and return to idle
  raw off=val [off=val] [dur]  modify arbitrary bytes; e.g. raw 13=0x1c 14=0x4b 80f
  save start [label] [outdir]  start recording changed screens + TX commands
  save stop|end                stop recording and create a zip file
  save status                  recording status
  display                      print a raw snapshot from the normal RX body→panel frame
  display raw                  same as above, but with a block-0 hexdump
  display menu                 legacy alias: show the last display2/menu frame
  display2                     print a raw snapshot of the menu/config frame
  display2 raw                 same as above, but with a block-0 hexdump
  display2 cells               show menu layout: numbered list or candidate grid
  display2 cells raw           menu layout + block-0 hexdump
  display2 learn [name]        save the current display2 frame as a reference
  display2 diff                differences between current and reference display2
  display2 diff raw            display2 diff with hexdump around changed bytes
  display2 watch [sec]         print when display2 changes for N seconds
  display2 watchdiff [sec]     print only display2 differences vs reference
  display decode               print a human-readable version of mapped fields
  display decode raw           decode + offset/byte details used
  display learn [name]         save the current frame as the display reference
  display diff                 differences between current frame and reference
  display diff raw             diff with hexdump around changed bytes
  display watch [sec]          print when the display frame changes for N seconds, default 10
  display watchdiff [sec]      print only differences vs reference when they change
  rxstat                       RX statistics
  idle                         return to idle
  quit                         exit

Duration:
  80f       = 80 actual frames
  300ms     = milliseconds
  300       = milliseconds, compatibility with the old script

Panel→body TX frame:
  1 frame = {TX_FRAME_LEN} byte ≈ {TX_FRAME_TIME_MS:.2f} ms a {BAUD} baud.

Body→panel RX frame:
  1 frame = {RX_FRAME_LEN} byte ≈ {RX_FRAME_TIME_MS:.2f} ms a {BAUD} baud.

Front panel:
  band, f, pmg, vm, sdx, power
  ul_press, ur_press, bl_press, br_press
  ul_left, ul_right, ur_left, ur_right, bl_left, bl_right, br_left, br_right

Microphone:
  mic_ptt, mic_ptt_hold, mic_up, mic_down, mic_mute
  mic_p1, mic_p2, mic_p3, mic_p4
  mic_0..mic_9, mic_star, mic_hash, mic_a, mic_b, mic_c, mic_d
""".strip()
    )


def print_list() -> None:
    groups = [
        ("front panel", ["band", "f", "pmg", "vm", "sdx", "power"]),
        ("knob press", ["ul_press", "ur_press", "bl_press", "br_press"]),
        ("encoder", ["ul_left", "ul_right", "ur_left", "ur_right", "bl_left", "bl_right", "br_left", "br_right"]),
        ("microphone", [
            "mic_ptt", "mic_ptt_hold", "mic_up", "mic_down", "mic_mute",
            "mic_p1", "mic_p2", "mic_p3", "mic_p4",
            "mic_1", "mic_2", "mic_3", "mic_4", "mic_5", "mic_6", "mic_7", "mic_8", "mic_9",
            "mic_star", "mic_0", "mic_hash", "mic_a", "mic_b", "mic_c", "mic_d",
        ]),
        ("alternate microphone", sorted(n for n in COMMANDS if n.endswith("_alt"))),
    ]
    for title, names in groups:
        print(f"{title}:")
        for i in range(0, len(names), 5):
            print("  " + "  ".join(f"{n:14s}" for n in names[i:i+5]))


def run_named_command(tx: PanelTx, name_token: str, duration_token: Optional[str] = None, hold: bool = False) -> None:
    name = canon(name_token)
    if name not in COMMANDS:
        print(f"unknown command: {name_token}; use 'list'")
        return

    ops = COMMANDS[name]
    if hold:
        tx.hold(name, ops)
        return

    default_frames = default_frames_for(name)
    frames = parse_duration_token(duration_token, default_frames) if duration_token else default_frames
    tx.pulse(name, ops, frames)


def print_display(rx: Optional[BodyRx], raw: bool = False) -> None:
    if rx is None or not rx.enabled:
        print("[rx] RX disabled")
        return
    frame, ts, seen, data_seen, sync_losses = rx.snapshot()
    if frame is None:
        print("[rx] no display frame received yet. Check RX/GND and verify the body is transmitting.")
        print(f"     stats: frames={seen}, data={data_seen}, sync_loss={sync_losses}")
        return
    age = time.time() - ts if ts else -1
    extra = ""
    if rx is not None:
        c = rx.counters()
        if c.get("menu_ignored", 0):
            extra = f", menu_ignored={c['menu_ignored']}"
    print(f"[rx] last normal display frame: {age:.2f}s ago; frames={seen}, data={data_seen}, sync_loss={sync_losses}{extra}")
    print(decode_display_snapshot(frame, raw=raw))


def get_rx_frame(rx: Optional[BodyRx]) -> Tuple[Optional[bytes], Optional[float], int, int, int]:
    if rx is None or not rx.enabled:
        return None, None, 0, 0, 0
    return rx.snapshot()


def display_watchdiff(rx: Optional[BodyRx], base: Optional[bytes], seconds: float = 10.0, raw: bool = False) -> None:
    if rx is None or not rx.enabled:
        print("[rx] RX disabled")
        return
    if base is None:
        frame, ts, seen, data_seen, sync_losses = rx.snapshot()
        if frame is None:
            print("[rx] no display frame available to use as a reference")
            return
        base = frame
        print("[rx] temporary reference learned from the current frame")
    print(f"[rx] watchdiff for {seconds:.1f}s; Ctrl-C to interrupt")
    end = time.monotonic() + seconds
    last: Optional[bytes] = None
    try:
        while time.monotonic() < end:
            frame, ts, seen, data_seen, sync_losses = rx.snapshot()
            if frame is not None and frame != last:
                last = frame
                if frame != base:
                    age = time.time() - ts if ts else -1
                    print(f"\n--- display diff, age={age:.2f}s, rx frames={seen}, data={data_seen} ---")
                    print(format_display_diff(base, frame, raw=raw, context=8 if raw else 0))
            time.sleep(0.03)
    except KeyboardInterrupt:
        print()


def display_watch(rx: Optional[BodyRx], seconds: float = 10.0) -> None:
    if rx is None or not rx.enabled:
        print("[rx] RX disabled")
        return
    print(f"[rx] watch display per {seconds:.1f}s; Ctrl-C per interrompere")
    end = time.monotonic() + seconds
    last: Optional[bytes] = None
    try:
        while time.monotonic() < end:
            frame, ts, seen, data_seen, sync_losses = rx.snapshot()
            if frame is not None and frame != last:
                last = frame
                print("\n--- display changed ---")
                print_display(rx, raw=False)
            time.sleep(0.05)
    except KeyboardInterrupt:
        print()




# ---------------------------------------------------------------------------
# Continuous capture recorder for mapping menus with submenus.
# Usage from CLI: save start [label|outdir] ... save stop/end.
# The recorder stores every changed RX display/display2 frame and every TX
# command event with timestamps so a later analysis can reconstruct the path.
# ---------------------------------------------------------------------------


def _save_safe_name(s: str, fallback: str = "capture") -> str:
    s = (s or "").strip()
    s = re.sub(r"[^A-Za-z0-9_.+-]+", "_", s)
    s = s.strip("._-")
    return s[:80] or fallback


def _save_frame_kind(frame: Optional[bytes]) -> str:
    if frame is None or len(frame) < 2:
        return "none"
    return f"{frame[0]:02x}{frame[1]:02x}"


class SaveRecorder:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = False
        self.root: Optional[str] = None
        self.started_at: Optional[float] = None
        self.label = ""
        self.seq = 0
        self.events = 0
        self.screens = 0
        self.commands = 0
        self.last_frame_by_kind: Dict[str, bytes] = {}
        self.last_path: Optional[str] = None
        self.zip_path: Optional[str] = None

    def _status_unlocked(self) -> dict:
        return {
            "active": self.active,
            "root": self.root,
            "label": self.label,
            "started_at": self.started_at,
            "elapsed_s": None if not self.active or self.started_at is None else max(0.0, time.time() - self.started_at),
            "events": self.events,
            "screens": self.screens,
            "commands": self.commands,
            "last_path": self.last_path,
            "zip_path": self.zip_path,
        }

    def status(self) -> dict:
        with self.lock:
            return self._status_unlocked()

    def _event_path(self) -> Optional[str]:
        return None if not self.root else os.path.join(self.root, "events.jsonl")

    def _write_jsonl_unlocked(self, obj: dict) -> None:
        if not self.root:
            return
        obj = dict(obj)
        obj.setdefault("time", time.time())
        obj.setdefault("seq", self.seq)
        with open(os.path.join(self.root, "events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
        self.events += 1

    def _write_text(self, path: str, text: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
            if not text.endswith("\n"):
                f.write("\n")

    def _normalize_frame_kind(self, kind: str, frame: bytes, info: Optional[dict]) -> Tuple[str, dict]:
        """Hard safety net for PMG frames before they are saved/logged.

        The reader normally classifies F1 25 as display2 and the paired F1 00
        PMG companion as pmgdata.  The real dumps showed display_f125 files,
        which means some path can still call record_frame(kind="display") with
        a PMG frame.  Normalize here too, so the recorder and any later analysis
        cannot mislabel PMG as the normal display.
        """
        out_info = dict(info or {})
        original = str(kind or "display")
        new_kind = original
        try:
            if frame is not None and len(frame) >= 2:
                if BodyRx.is_pmg_companion_data_frame(frame):
                    new_kind = "pmgdata"
                elif BodyRx.is_menu_display_frame(frame):
                    new_kind = "display2"
        except Exception:
            new_kind = original
        if new_kind != original:
            out_info.setdefault("original_kind", original)
            out_info.setdefault("forced_kind", new_kind)
            out_info.setdefault("forced_reason", "pmg_hard_guard")
        return new_kind, out_info

    def _frame_text(self, kind: str, frame: bytes, previous: Optional[bytes]) -> str:
        parts: List[str] = []
        parts.append(f"kind={kind} len={len(frame)} header={frame[:16].hex(' ')}")
        parts.append("")
        if previous is not None and len(previous) == len(frame):
            parts.append("diff vs previous same kind:")
            try:
                parts.append(format_display_diff(previous, frame, raw=True, context=8))
            except Exception as e:
                parts.append(f"diff error: {type(e).__name__}: {e}")
            parts.append("")
        try:
            if kind == "display2" or (len(frame) >= 2 and frame[0] in (0xF1, 0xF3) and frame[1] in (0x60, 0x21, 0x25, 0x20)):
                parts.append(decode_menu_layout(frame, raw=True))
            else:
                parts.append(decode_display_snapshot(frame, raw=True))
                parts.append("")
                parts.append(decode_display_human(frame, raw=True))
        except Exception as e:
            parts.append(f"decode error: {type(e).__name__}: {e}")
            parts.append("")
            parts.append(hexdump(frame[:RX_BLOCK_LEN], 0))
        return "\n".join(parts)

    def start(self, label: str = "", outdir: str = ".", rx: Optional[BodyRx] = None) -> Tuple[bool, str, dict]:
        with self.lock:
            if self.active:
                return False, f"save already active: {self.root}", self._status_unlocked()
            stamp = time.strftime("%Y%m%d_%H%M%S")
            clean = _save_safe_name(label or "session")
            root = os.path.abspath(os.path.join(outdir or ".", f"save_{stamp}_{clean}"))
            os.makedirs(root, exist_ok=False)
            self.active = True
            self.root = root
            self.started_at = time.time()
            self.label = label or clean
            self.seq = 0
            self.events = 0
            self.screens = 0
            self.commands = 0
            self.last_frame_by_kind = {}
            self.last_path = None
            self.zip_path = None
            meta = {
                "type": "start",
                "label": self.label,
                "root": root,
                "time": self.started_at,
                "note": "RX screen changes are saved as *.bin/*.txt; TX commands are timestamped in events.jsonl.",
                "rx_frame_len": RX_FRAME_LEN,
                "rx_block_len": RX_BLOCK_LEN,
                "tx_frame_len": TX_FRAME_LEN,
                "baud": BAUD,
                "build_id": BUILD_ID,
                "pmg_guard": "F1 25 is forced to display2; PMG companion F1 00 is forced to pmgdata; PMG F1 25 is latched for web render",
            }
            self._write_jsonl_unlocked(meta)
            self._write_text(os.path.join(root, "README.txt"), "\n".join([
                "Free RIG save capture",
                "",
                "Use this folder/zip for submenu reconstruction.",
                "Files:",
                "  events.jsonl              chronological screen/command log",
                "  *.bin                     raw 1100-byte RX frame when the screen changed",
                "  *.txt                     decoded/hexdump/diff companion for each raw frame",
                "  summary.json              written by save stop/end",
                "",
                "Important: command events and screen frames are associated by timestamps and seq numbers.",
            ]))

        # Save current visible frames immediately after enabling the recorder.
        if rx is not None and getattr(rx, "enabled", False):
            try:
                frame, ts, seen, data_seen, sync_losses = rx.snapshot()
                if frame is not None:
                    self.record_frame("display_initial", frame, rx_time=ts, info={"frames": seen, "data": data_seen, "sync_loss": sync_losses})
                frame2, ts2, menu_seen = rx.menu_snapshot()
                if frame2 is not None:
                    self.record_frame("display2_initial", frame2, rx_time=ts2, info={"menu": menu_seen})
            except Exception as e:
                self.record_note("initial_snapshot_error", str(e))
        return True, f"save start: {root}", self.status()

    def record_note(self, name: str, text: str) -> None:
        with self.lock:
            if not self.active:
                return
            self.seq += 1
            self._write_jsonl_unlocked({"type": "note", "name": name, "text": text})

    def record_command(self, action: str, label: str, **extra) -> None:
        with self.lock:
            if not self.active:
                return
            self.seq += 1
            self.commands += 1
            clean_extra = {}
            for k, v in extra.items():
                if k == "ops" and v is not None:
                    clean_extra[k] = [[mode, int(off), int(val)] for mode, off, val in v]
                else:
                    try:
                        json.dumps(v)
                        clean_extra[k] = v
                    except Exception:
                        clean_extra[k] = repr(v)
            self._write_jsonl_unlocked({
                "type": "command",
                "action": action,
                "label": label,
                "extra": clean_extra,
            })

    def record_frame(self, kind: str, frame: Optional[bytes], rx_time: Optional[float] = None, info: Optional[dict] = None) -> None:
        if frame is None:
            return
        kind, info = self._normalize_frame_kind(kind, frame, info)
        with self.lock:
            if not self.active:
                return
            previous = self.last_frame_by_kind.get(kind)
            if previous == frame:
                return
            self.seq += 1
            self.screens += 1
            self.last_frame_by_kind[kind] = frame
            root = self.root
            if not root:
                return
            stem = f"{self.seq:06d}_{_save_safe_name(kind)}_{_save_frame_kind(frame)}"
            bin_path = os.path.join(root, stem + ".bin")
            txt_path = os.path.join(root, stem + ".txt")
            with open(bin_path, "wb") as f:
                f.write(frame)
            self._write_text(txt_path, self._frame_text(kind, frame, previous))
            self.last_path = bin_path
            ranges = []
            if previous is not None and len(previous) == len(frame):
                ranges = [[a, b] for a, b in diff_ranges(previous, frame)[:80]]
            self._write_jsonl_unlocked({
                "type": "screen",
                "kind": kind,
                "path": os.path.basename(bin_path),
                "text_path": os.path.basename(txt_path),
                "rx_time": rx_time,
                "rx_age_s": None if rx_time is None else max(0.0, time.time() - rx_time),
                "header": frame[:16].hex(" "),
                "frame_kind": _save_frame_kind(frame),
                "diff_ranges_vs_previous_same_kind": ranges,
                "info": info or {},
            })

    def stop(self) -> Tuple[bool, str, dict]:
        with self.lock:
            if not self.active or not self.root:
                return False, "save not active", self._status_unlocked()
            root = self.root
            elapsed = None if self.started_at is None else max(0.0, time.time() - self.started_at)
            summary = {
                "type": "stop",
                "root": root,
                "label": self.label,
                "started_at": self.started_at,
                "stopped_at": time.time(),
                "elapsed_s": elapsed,
                "events": self.events,
                "screens": self.screens,
                "commands": self.commands,
            }
            self.seq += 1
            self._write_jsonl_unlocked(summary)
            self._write_text(os.path.join(root, "summary.json"), json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            self.active = False
            self.zip_path = root + ".zip"
            zip_path = self.zip_path

        # Zip outside the lock so RX/TX threads do not block longer than needed.
        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for dirpath, _dirnames, filenames in os.walk(root):
                    for fn in sorted(filenames):
                        full = os.path.join(dirpath, fn)
                        arc = os.path.relpath(full, os.path.dirname(root))
                        zf.write(full, arc)
        except Exception as e:
            return False, f"save stop: folder saved but zip failed: {e}; folder={root}", self.status()
        return True, f"save stop: {root} ; zip={zip_path}", self.status()


SAVE_RECORDER = SaveRecorder()


def save_command(args: List[str], rx: Optional[BodyRx]) -> None:
    sub = args[0].lower() if args else "status"
    if sub in ("start", "on", "begin"):
        label = args[1] if len(args) > 1 else "session"
        outdir = args[2] if len(args) > 2 else "."
        ok, msg, _st = SAVE_RECORDER.start(label=label, outdir=outdir, rx=rx)
        print("[save] " + msg)
        return
    if sub in ("stop", "end", "off"):
        ok, msg, _st = SAVE_RECORDER.stop()
        print("[save] " + msg)
        return
    st = SAVE_RECORDER.status()
    if st.get("active"):
        print(f"[save] active: {st.get('root')} screens={st.get('screens')} commands={st.get('commands')} elapsed={st.get('elapsed_s'):.1f}s")
    else:
        print("[save] not active")

def command_loop(tx: PanelTx, rx: Optional[BodyRx]) -> None:
    display_base: Optional[bytes] = None
    display_base_name = "base"
    display2_base: Optional[bytes] = None
    display2_base_name = "base2"

    print_help()
    print("\n[run] TX idle frame active. If RX is connected, use 'display'.")

    while not tx.stop.is_set():
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            tx.stop.set()
            return

        line = line.strip()
        if not line:
            continue

        try:
            parts = shlex.split(line)
        except ValueError as e:
            print(f"parse error: {e}")
            continue

        cmd = parts[0].lower()
        args = parts[1:]

        try:
            if cmd in ("q", "quit", "exit"):
                tx.stop.set()
                return
            if cmd in ("h", "help", "?"):
                print_help()
                continue
            if cmd == "list":
                print_list()
                continue
            if cmd in ("idle", "clear", "release"):
                tx.release()
                continue
            if cmd == "raw":
                ops, frames = parse_raw(args)
                tx.pulse("raw", ops, frames)
                continue
            if cmd == "rxstat":
                if rx is None or not rx.enabled:
                    print("[rx] RX disabled")
                else:
                    frame, ts, seen, data_seen, sync_losses = rx.snapshot()
                    age = time.time() - ts if ts else None
                    age_s = f", age={age:.2f}s" if age is not None else ""
                    c = rx.counters()
                    print(
                        f"[rx] frames={c['frames']}, data={c['data']}, "
                        f"menu_ignored={c['menu_ignored']}, ignored={c['ignored']}, "
                        f"sync_loss={c['sync_loss']}{age_s}"
                    )
                continue
            if cmd == "save":
                save_command(args, rx)
                continue
            if cmd == "display2":
                sub = args[0].lower() if args else ""
                if sub in ("cells", "grid", "slots", "items", "text", "decode", "dec", "human"):
                    raw = len(args) > 1 and args[1].lower() in ("raw", "hex", "hexdump")
                    print_menu_cells(rx, raw=raw)
                elif sub in ("raw", "hex", "hexdump"):
                    print_menu_display(rx, raw=True)
                elif sub in ("learn", "base", "baseline", "mark", "ref"):
                    frame, ts, seen, menu_seen, sync_losses = get_rx_menu_frame(rx)
                    if frame is None:
                        print("[rx] no display2/menu frame available to save as a reference")
                    else:
                        display2_base = frame
                        display2_base_name = args[1] if len(args) > 1 else "base2"
                        age = time.time() - ts if ts else -1
                        print(f"[rx] display2 reference '{display2_base_name}' saved; age={age:.2f}s, frames={seen}, menu={menu_seen}")
                elif sub in ("diff", "compare", "cmp"):
                    raw = len(args) > 1 and args[1].lower() in ("raw", "hex", "hexdump")
                    if display2_base is None:
                        print("[rx] first run: display2 learn [name]")
                    else:
                        frame, ts, seen, menu_seen, sync_losses = get_rx_menu_frame(rx)
                        if frame is None:
                            print("[rx] no current display2/menu frame")
                        else:
                            age = time.time() - ts if ts else -1
                            print(f"[rx] display2 diff vs '{display2_base_name}'; age={age:.2f}s, frames={seen}, menu={menu_seen}")
                            print(format_display_diff(display2_base, frame, raw=raw, context=8 if raw else 0))
                elif sub in ("watch", "mon", "monitor"):
                    seconds = float(args[1]) if len(args) > 1 else 10.0
                    display2_watch(rx, seconds)
                elif sub in ("watchdiff", "diffwatch", "mondiff"):
                    seconds = float(args[1]) if len(args) > 1 else 10.0
                    raw = len(args) > 2 and args[2].lower() in ("raw", "hex", "hexdump")
                    display2_watchdiff(rx, display2_base, seconds, raw=raw)
                else:
                    print_menu_display(rx, raw=False)
                continue
            if cmd == "display":
                sub = args[0].lower() if args else ""
                if sub in ("decode", "dec", "human"):
                    raw = len(args) > 1 and args[1].lower() in ("raw", "hex", "hexdump")
                    print_display_decode(rx, raw=raw)
                elif sub in ("menu", "menuraw"):
                    raw = sub == "menuraw" or (len(args) > 1 and args[1].lower() in ("raw", "hex", "hexdump"))
                    print_menu_display(rx, raw=raw)
                elif sub in ("raw", "hex", "hexdump"):
                    print_display(rx, raw=True)
                elif sub in ("learn", "base", "baseline", "mark", "ref"):
                    frame, ts, seen, data_seen, sync_losses = get_rx_frame(rx)
                    if frame is None:
                        print("[rx] no display frame available to save as a reference")
                    else:
                        display_base = frame
                        display_base_name = args[1] if len(args) > 1 else "base"
                        age = time.time() - ts if ts else -1
                        print(f"[rx] display reference '{display_base_name}' saved; age={age:.2f}s, frames={seen}, data={data_seen}")
                elif sub in ("diff", "compare", "cmp"):
                    raw = len(args) > 1 and args[1].lower() in ("raw", "hex", "hexdump")
                    if display_base is None:
                        print("[rx] first run: display learn [name]")
                    else:
                        frame, ts, seen, data_seen, sync_losses = get_rx_frame(rx)
                        if frame is None:
                            print("[rx] no current display frame")
                        else:
                            age = time.time() - ts if ts else -1
                            print(f"[rx] diff vs '{display_base_name}'; age={age:.2f}s, frames={seen}, data={data_seen}")
                            print(format_display_diff(display_base, frame, raw=raw, context=8 if raw else 0))
                elif sub in ("watch", "mon", "monitor"):
                    seconds = float(args[1]) if len(args) > 1 else 10.0
                    display_watch(rx, seconds)
                elif sub in ("watchdiff", "diffwatch", "mondiff"):
                    seconds = float(args[1]) if len(args) > 1 else 10.0
                    raw = len(args) > 2 and args[2].lower() in ("raw", "hex", "hexdump")
                    display_watchdiff(rx, display_base, seconds, raw=raw)
                else:
                    print_display(rx, raw=False)
                continue
            if cmd == "press":
                if not args:
                    print("usage: press <name> [duration]")
                    continue
                run_named_command(tx, args[0], args[1] if len(args) > 1 else None)
                continue
            if cmd == "hold":
                if not args:
                    print("usage: hold <name>")
                    continue
                run_named_command(tx, args[0], hold=True)
                continue

            # Direct command: "band", "band 80f", "mic_5 300ms", etc.
            run_named_command(tx, cmd, args[0] if args else None)
        except Exception as e:
            print(f"error: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Free RIG panel I/O emulator, experimental v13")
    ap.add_argument("--port", default="/dev/cu.usbserial-0001")
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--no-rx", action="store_true", help="disable the RX display thread")
    ap.add_argument("--no-ignore-menu", action="store_true", help="do not filter menu/config frames from the normal display; display2 remains always available")
    args = ap.parse_args()

    try:
        ser = serial.Serial(
            args.port,
            args.baud,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=0.02,
            write_timeout=1,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
    except serial.SerialException as e:
        print(f"Failed to open {args.port}: {e}", file=sys.stderr)
        return 1

    print(f"[open] {args.port} @ {args.baud} 8N1")
    print(f"[info] TX frame {TX_FRAME_LEN} bytes, nominal {1000.0 / TX_FRAME_TIME_MS:.1f} frame/s")
    print(f"[info] RX frame {RX_FRAME_LEN} bytes, nominal ≈ {1000.0 / RX_FRAME_TIME_MS:.1f} frame/s")
    print("[safe] Original front-panel TX disconnected from the line you are driving.")

    tx = PanelTx(ser, verbose=args.verbose)
    tx_th = threading.Thread(target=tx.writer_loop, daemon=True)
    tx_th.start()

    rx: Optional[BodyRx] = None
    rx_th: Optional[threading.Thread] = None
    if not args.no_rx:
        rx = BodyRx(ser, enabled=True, verbose=args.verbose, ignore_menu=not args.no_ignore_menu)
        rx_th = threading.Thread(target=rx.reader_loop, daemon=True)
        rx_th.start()

    try:
        command_loop(tx, rx)
    finally:
        tx.stop.set()
        if rx is not None:
            rx.stop.set()
        tx_th.join(timeout=1.0)
        if rx_th is not None:
            rx_th.join(timeout=1.0)
        ser.close()
        print("[stop]")
    return 0



# ---------------------------------------------------------------------------
# Web GUI front-end - v48: clean audio UI, momentary Power, LOCK/UNLOCK overlay
# ---------------------------------------------------------------------------

import json
import zipfile
import collections
import subprocess
import array
import base64
import hashlib
import ssl
import math
import os
import glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


WEB_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Free RIG Web Panel</title>
<style>
  :root { --orange:#d75a20; --orange2:#e9752b; --lcdBottom:#c94c18; --lcdAccent:#d75a20; --lcdAccentGlow:rgba(214,86,31,.2); --lcdText:#24211d; --rx:#35d65a; --tx:#ff2438; }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif; background:radial-gradient(circle at 50% 10%,#333 0,#171717 45%,#050505 100%); color:#eee; display:flex; align-items:center; justify-content:center; padding:20px; }
  .wrap { width:min(1180px,100%); }
  .radio { position:relative; padding:20px 30px 32px; border-radius:34px 34px 26px 26px; background:linear-gradient(180deg,#1a1a1a 0%,#0d0d0d 58%,#050505 100%); box-shadow:inset 0 2px 2px rgba(255,255,255,.16), inset 0 -10px 20px rgba(0,0,0,.9), 0 28px 60px rgba(0,0,0,.65); border:1px solid #333; }
  .top-buttons { display:grid; grid-template-columns:repeat(6,1fr); gap:14px; margin:0 92px 16px; align-items:end; }
  button { cursor:pointer; user-select:none; border:0; color:#f4f4f4; background:linear-gradient(180deg,#2a2a2a,#080808); box-shadow:inset 0 1px rgba(255,255,255,.18),inset 0 -2px rgba(0,0,0,.8),0 4px 8px rgba(0,0,0,.6); transition:transform .05s,filter .12s,box-shadow .12s; }
  button:hover { filter:brightness(1.16); }
  button:active { transform:translateY(2px); box-shadow:inset 0 2px 5px rgba(0,0,0,.9); }
  button:disabled, button.disabled { cursor:not-allowed; opacity:.42; color:#777; background:linear-gradient(180deg,#202020,#0b0b0b); filter:none!important; box-shadow:inset 0 1px rgba(255,255,255,.08), inset 0 -2px rgba(0,0,0,.85); }
  button:disabled:active, button.disabled:active { transform:none; }
  .top-btn { height:34px; border-radius:7px; font-weight:800; letter-spacing:1px; line-height:1.0; font-size:15px; }
  .top-btn small { display:block; font-size:11px; letter-spacing:.3px; color:#ddd; }
  .power { color:#ff3345; font-size:22px; }
  .top-btn.holding { color:#fff; background:linear-gradient(180deg,#ff4352,#7c000d); box-shadow:0 0 18px rgba(255,35,55,.55), inset 0 1px rgba(255,255,255,.25), inset 0 -2px rgba(0,0,0,.55); }
  .face { display:grid; grid-template-columns:100px 1fr 100px; gap:28px; align-items:center; }
  .side { display:grid; grid-template-rows:auto 24px auto; gap:7px; align-items:center; justify-items:center; }
  .knob-block { width:88px; text-align:center; position:relative; }
  .knob-label { font-weight:900; color:#bbb; text-shadow:0 2px #000; letter-spacing:1px; margin-top:2px; font-size:11px; }
  .knob { width:42px; height:42px; border-radius:50%; margin:0 auto; background:radial-gradient(circle at 50% 50%,#3a3a3a 0 3px,transparent 4px), repeating-conic-gradient(from 0deg,#0b0b0b 0deg 8deg,#222 9deg 12deg), radial-gradient(circle,#333 0%,#0a0a0a 68%,#000 100%); box-shadow:inset 0 0 8px #000,inset 0 0 18px rgba(255,255,255,.12),0 6px 12px rgba(0,0,0,.8); border:2px solid #050505; }
  .knob-actions { display:flex; justify-content:center; gap:3px; margin-top:3px; }
  .mini-btn { border-radius:7px; min-width:23px; height:18px; font-size:10px; padding:0 4px; }
  .center { min-width:0; position:relative; }
  .brand { text-align:center; color:#d54316; font-size:12px; letter-spacing:2px; font-weight:800; margin:-2px 0 7px; }
  .lcd-frame { padding:14px; border-radius:20px; background:linear-gradient(180deg,#050505,#181818 40%,#050505); border:2px solid #2b2b2b; box-shadow:inset 0 0 14px #000,0 8px 18px rgba(0,0,0,.65); }
  .lcd { background:linear-gradient(180deg,var(--orange2),var(--orange) 56%,var(--lcdBottom)); color:var(--lcdText); min-height:230px; border-radius:10px; padding:12px 18px 10px; box-shadow:inset 0 0 16px rgba(0,0,0,.35), inset 0 0 0 2px rgba(50,20,8,.35); font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace; position:relative; overflow:hidden; }
  .lcd:after { content:""; position:absolute; inset:0; pointer-events:none; background:linear-gradient(90deg,rgba(255,255,255,.08),transparent 12%,transparent 88%,rgba(255,255,255,.05)); mix-blend-mode:screen; }
  .lcd-top { display:grid; grid-template-columns:1fr 1fr; gap:28px; align-items:start; font-weight:900; font-size:28px; line-height:.9; }
  .top-slot { display:flex; align-items:flex-start; gap:6px; min-width:0; }
  .tag { display:inline-flex; align-items:center; justify-content:center; min-width:96px; height:30px; background:rgba(0,0,0,.72); color:var(--lcdAccent); padding:0 8px; line-height:1; text-align:center; white-space:nowrap; vertical-align:middle; }
  .mini-ind { min-width:34px; padding-left:7px; padding-right:7px; }
  .mini-ind.tone { min-width:54px; }
  .mini-ind.empty { display:none; }
  .side-led { display:block; width:54px; height:18px; border-radius:999px; background:#221d18; border:2px solid rgba(0,0,0,.75); box-shadow:inset 0 0 8px rgba(0,0,0,.9), 0 4px 10px rgba(0,0,0,.65); position:relative; }
  .side-led:before { content:""; position:absolute; inset:3px 9px; border-radius:999px; background:#2b241e; box-shadow:inset 0 0 6px rgba(0,0,0,.9); }
  .side-led.rx:before { background:linear-gradient(90deg,#56ff73,#26b943); box-shadow:0 0 14px var(--rx), inset 0 0 5px rgba(255,255,255,.75); }
  .side-led.tx:before { background:linear-gradient(90deg,#ff6b70,#d30018); box-shadow:0 0 16px var(--tx), inset 0 0 5px rgba(255,255,255,.7); }
  .freq-row { display:grid; grid-template-columns:1fr 1fr; gap:28px; margin-top:28px; }
  .mute-overlay { position:absolute; z-index:5; left:50%; top:50%; transform:translate(-50%,-50%); display:none; min-width:210px; min-height:72px; align-items:center; justify-content:center; background:rgba(0,0,0,.76); color:var(--lcdAccent); border:4px solid rgba(35,30,25,.9); box-shadow:0 0 0 3px var(--lcdAccentGlow), inset 0 0 12px rgba(0,0,0,.85); font-weight:950; font-size:42px; letter-spacing:2px; }
  .mute-overlay.show { display:flex; }
  .dialog-overlay { position:absolute; z-index:6; left:50%; top:50%; transform:translate(-50%,-50%); display:none; min-width:350px; max-width:92%; padding:14px 18px 16px; background:rgba(0,0,0,.82); color:#fff; border:4px solid rgba(35,30,25,.9); box-shadow:0 0 0 3px var(--lcdAccentGlow), inset 0 0 12px rgba(0,0,0,.85); font-weight:950; text-align:center; text-shadow:0 1px 2px rgba(0,0,0,.9); }
  .dialog-overlay.show { display:block; }
  .dialog-title { font-size:24px; line-height:1.05; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .dialog-msg { font-size:22px; line-height:1; margin-top:6px; white-space:nowrap; }
  .dialog-options { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:14px; }
  .dialog-option { border:3px solid rgba(245,245,245,.70); min-height:34px; display:flex; align-items:center; justify-content:center; font-size:22px; white-space:nowrap; color:#fff; }
  .dialog-option.sel { background:rgba(255,255,255,.24); box-shadow:inset 0 0 0 999px rgba(255,255,255,.03); color:#fff; }
  .dtmf-edit { height:100%; display:flex; flex-direction:column; justify-content:center; gap:9px; padding:5px 8px 2px; font-weight:950; }
  .dtmf-entry-line { display:grid; grid-template-columns:repeat(16,1fr); gap:2px; margin:0 10px 2px; }
  .dtmf-entry-cell { position:relative; height:30px; display:flex; align-items:center; justify-content:center; font-size:25px; line-height:1; color:var(--lcdText); border-bottom:3px solid rgba(35,30,25,.72); }
  .dtmf-entry-cell.empty { color:transparent; opacity:.38; }
  .dtmf-entry-cell.space { background:rgba(0,0,0,.12); }
  .dtmf-entry-cell.cursor { background:rgba(0,0,0,.30); box-shadow:inset 0 0 0 2px rgba(35,30,25,.48); border-bottom-color:rgba(35,30,25,1); }
  .dtmf-entry-cell.cursor:after { content:""; position:absolute; left:50%; bottom:-7px; transform:translateX(-50%); width:0; height:0; border-left:5px solid transparent; border-right:5px solid transparent; border-bottom:6px solid rgba(35,30,25,.9); }
  .dtmf-entry-cell.cursor-after { box-shadow:inset -4px 0 0 rgba(35,30,25,.9); }
  .dtmf-keypad { display:flex; flex-direction:column; gap:5px; align-items:center; }
  .dtmf-key-row { display:grid; gap:4px; justify-content:center; }
  .dtmf-key-row.row5 { grid-template-columns:repeat(5,72px); }
  .dtmf-key-row.row10 { grid-template-columns:repeat(10,34px); }
  .dtmf-key { height:33px; display:flex; align-items:center; justify-content:center; border:3px solid rgba(35,30,25,.78); color:var(--lcdText); font-size:23px; line-height:1; background:rgba(255,255,255,.03); cursor:pointer; user-select:none; }
  .dtmf-key.tool { font-size:18px; }
  .dtmf-key.sel { background:rgba(0,0,0,.30); box-shadow:inset 0 0 0 999px rgba(0,0,0,.04); }
  .normal-screen.hidden { display:none; }
  .menu-screen { position:absolute; inset:12px 18px 10px; z-index:4; display:none; }
  .menu-screen.show { display:block; }
  .scope-screen { height:100%; display:grid; grid-template-rows:auto 1fr; gap:8px; padding:4px 2px 2px; color:var(--lcdText); font-weight:950; }
  .scope-top { display:grid; grid-template-columns:1fr auto; align-items:start; gap:8px; }
  .scope-badges { display:flex; flex-wrap:wrap; gap:4px; align-items:center; min-width:0; }
  .scope-badge { min-width:42px; min-height:19px; padding:2px 5px; border:2px solid rgba(35,30,25,.72); background:rgba(0,0,0,.10); display:inline-flex; align-items:center; justify-content:center; font-size:13px; line-height:1; white-space:nowrap; }
  .scope-freq { font-size:clamp(42px,8.5vw,70px); line-height:.78; letter-spacing:-5px; text-align:right; white-space:nowrap; }
  .scope-body { position:relative; min-height:126px; display:grid; grid-template-rows:1fr 20px; border-left:4px solid rgba(35,30,25,.86); border-right:4px solid rgba(35,30,25,.86); border-bottom:5px solid rgba(35,30,25,.92); background:linear-gradient(180deg,rgba(255,255,255,.03),rgba(0,0,0,.04)); overflow:hidden; }
  .scope-bars { position:relative; display:grid; align-items:end; gap:2px; padding:14px 6px 0; height:100%; }
  .scope-bar { align-self:end; min-height:2px; background:rgba(35,30,25,.92); border-radius:1px 1px 0 0; }
  .scope-bar.center { background:rgba(35,30,25,1); box-shadow:0 0 0 2px rgba(35,30,25,.28); }
  .scope-bar.strong { box-shadow:0 -2px 0 rgba(35,30,25,.22); }
  .scope-marker-line { position:absolute; left:50%; top:8px; bottom:14px; width:3px; transform:translateX(-50%); background:rgba(35,30,25,.82); box-shadow:0 0 0 2px rgba(216,90,32,.35); pointer-events:none; }
  .scope-marker-cap { position:absolute; left:50%; top:3px; transform:translateX(-50%); width:0; height:0; border-left:7px solid transparent; border-right:7px solid transparent; border-top:10px solid rgba(35,30,25,.9); pointer-events:none; }
  .scope-dots { display:grid; align-items:center; gap:2px; padding:0 6px 2px; }
  .scope-dot { width:4px; height:4px; border-radius:999px; background:rgba(35,30,25,.68); justify-self:center; }
  .scope-dot.center { width:7px; height:7px; background:rgba(35,30,25,.95); }
  .scope-footer { display:none; }
  .scope-interval { border:2px solid rgba(35,30,25,.72); padding:4px 12px; min-width:86px; text-align:center; }
  .pmg-screen { height:100%; display:grid; grid-template-rows:auto 1fr auto; gap:8px; padding:4px 2px 2px; color:var(--lcdText); font-weight:950; }
  .pmg-top { display:grid; grid-template-columns:1fr auto; align-items:start; gap:8px; }
  .pmg-badges { display:flex; flex-wrap:wrap; gap:4px; align-items:center; min-width:0; }
  .pmg-badge { min-width:36px; min-height:19px; padding:2px 5px; border:2px solid rgba(35,30,25,.72); background:rgba(0,0,0,.10); display:inline-flex; align-items:center; justify-content:center; font-size:13px; line-height:1; white-space:nowrap; }
  .pmg-freq { font-size:clamp(42px,8.4vw,70px); line-height:.78; letter-spacing:-5px; text-align:right; white-space:nowrap; }
  .pmg-body { position:relative; min-height:126px; display:grid; grid-template-rows:1fr 30px; border-left:4px solid rgba(35,30,25,.86); border-right:4px solid rgba(35,30,25,.86); border-bottom:5px solid rgba(35,30,25,.92); background:linear-gradient(180deg,rgba(255,255,255,.03),rgba(0,0,0,.04)); overflow:hidden; }
  .pmg-bars { display:grid; grid-template-columns:repeat(5,1fr); align-items:end; gap:11px; padding:13px 14px 0; height:100%; }
  .pmg-slot { height:100%; display:flex; flex-direction:column; justify-content:flex-end; align-items:center; gap:0; min-width:0; }
  .pmg-barwrap { position:relative; width:100%; height:100%; display:flex; align-items:flex-end; justify-content:center; }
  .pmg-bar-shadow, .pmg-bar { position:absolute; bottom:0; min-height:0; border-radius:1px 1px 0 0; }
  .pmg-bar-shadow { width:82%; background:rgba(35,30,25,.34); }
  .pmg-bar { width:68%; background:rgba(35,30,25,.92); }
  .pmg-bar.receiving { background:rgba(35,30,25,1); box-shadow:0 -2px 0 rgba(35,30,25,.24); }
  .pmg-bar.recent { background:rgba(35,30,25,.52); }
  .pmg-line { width:100%; height:3px; background:rgba(35,30,25,.85); margin-top:2px; }
  .pmg-line.auto { height:7px; }
  .pmg-labels { position:relative; z-index:3; display:grid; grid-template-columns:repeat(5,1fr); gap:11px; padding:3px 14px 3px; align-items:center; background:transparent; }
  .pmg-label { position:relative; z-index:3; height:22px; display:flex; align-items:center; justify-content:center; border:2px solid transparent; font-size:15px; line-height:1; background:transparent; color:var(--lcdText); overflow:visible; }
  .pmg-label-text { position:relative; z-index:2; display:inline-block; font-weight:1000; letter-spacing:-0.3px; }
  .pmg-label.sel { background:rgba(35,30,25,.92); color:rgba(238,150,84,.98); border-color:rgba(35,30,25,.92); }
  .pmg-label.flash:not(.sel) { animation:pmgLabelFlash .8s steps(1,end) infinite; }
  @keyframes pmgLabelFlash { 0%,49% { opacity:1; } 50%,100% { opacity:.18; } }
  .pmg-footer { display:none; }
  .pmg-mode-chip { border:2px solid rgba(35,30,25,.72); padding:3px 8px; }
  .quick-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:4px; }
  .quick-cell,.quick-footer,.full-row { border:3px solid rgba(35,30,25,.78); color:var(--lcdText); font-weight:950; }
  .quick-cell { min-height:44px; display:flex; align-items:center; justify-content:center; font-size:20px; line-height:1; padding:0 6px; }
  .quick-cell.sel,.full-row.sel,.full-row.edit { background:rgba(0,0,0,.28); box-shadow:inset 0 0 0 999px rgba(0,0,0,.04); }
  .submenu-row { grid-template-columns:176px 1fr 24px; gap:0; padding:0; overflow:hidden; }
  .submenu-row.sel,.submenu-row.edit { background:transparent; box-shadow:none; }
  .submenu-row .sub-key,.submenu-row .sub-val { height:100%; display:flex; align-items:center; padding:0 8px; min-width:0; }
  .submenu-row .sub-key { justify-content:flex-start; white-space:nowrap; overflow:visible; }
  .submenu-row .sub-val { justify-content:flex-start; overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
  .submenu-row.focus-key .sub-key,.submenu-row.focus-value .sub-val { background:rgba(0,0,0,.28); box-shadow:inset 0 0 0 999px rgba(0,0,0,.04); }
  .quick-footer { min-height:48px; margin-top:6px; display:flex; align-items:center; justify-content:center; font-size:26px; font-weight:950; }
  .quick-footer.sel { border-color:rgba(35,30,25,.78); background:rgba(0,0,0,.28); box-shadow:inset 0 0 0 999px rgba(0,0,0,.04); }
  .full-list { height:100%; display:flex; flex-direction:column; gap:4px; }
  .full-row { min-height:45px; display:grid; grid-template-columns:74px 1fr 24px; align-items:center; font-size:20px; line-height:1; padding:0 8px; cursor:pointer; }
  .full-row.footer { cursor:default; font-size:19px; }
  .full-row.read-only { cursor:default; }
  .full-row.submenu-title { grid-template-columns:74px 1fr 24px; min-height:40px; font-size:18px; opacity:.95; }
  .full-row.submenu-row { grid-template-columns:176px 1fr 24px; gap:0; padding:0; overflow:hidden; }
  .choice-cell { margin-top:auto; min-height:48px; border:3px solid rgba(35,30,25,.78); color:var(--lcdText); font-weight:950; display:flex; align-items:center; justify-content:center; text-align:center; font-size:26px; line-height:1; padding:0 12px; cursor:pointer; }
  .choice-cell.edit,.choice-cell.sel { background:rgba(0,0,0,.28); box-shadow:inset 0 0 0 999px rgba(0,0,0,.04); }
  .choice-cell.unknown { font-size:22px; flex-direction:column; gap:4px; }
  .choice-raw { font-size:11px; font-weight:700; opacity:.68; max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .memory-row { border:3px solid rgba(35,30,25,.78); color:var(--lcdText); font-weight:950; display:grid; align-items:center; font-size:20px; line-height:1; cursor:pointer; }
  .memory-row.sel,.memory-edit-row.sel { background:rgba(0,0,0,.28); box-shadow:inset 0 0 0 999px rgba(0,0,0,.04); }
  .memory-list-row { grid-template-columns:54px 118px 1fr 24px; min-height:40px; padding:0 8px; }
  .memory-list-row .slot,.memory-list-row .freq,.memory-list-row .name,.memory-list-row .arrow,
  .memory-edit-row .label,.memory-edit-row .value,.memory-edit-row .arrow { overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
  .memory-list-row .slot { font-size:18px; }
  .memory-list-row .freq { font-size:18px; letter-spacing:0; }
  .memory-list-row .name { padding-left:10px; }
  .memory-list-row .arrow,.memory-edit-row .arrow { text-align:right; font-size:26px; }
  .memory-edit-row { grid-template-columns:110px 1fr 24px; min-height:42px; padding:0 8px; }
  .memory-action-row { min-height:32px; }
  .memory-action-overlay { position:absolute; z-index:8; left:50%; top:50%; transform:translate(-50%,-50%); width:min(330px,88%); padding:10px 12px; border:4px solid rgba(35,30,25,.88); background:rgba(216,90,32,.96); box-shadow:0 0 0 3px rgba(35,30,25,.20), 0 12px 24px rgba(0,0,0,.26), inset 0 0 12px rgba(0,0,0,.16); color:var(--lcdText); font-weight:950; }
  .memory-action-title { font-size:18px; line-height:1; margin-bottom:7px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .memory-action-list { display:flex; flex-direction:column; gap:4px; }
  .memory-action-item { min-height:29px; border:3px solid rgba(35,30,25,.78); display:grid; grid-template-columns:1fr 24px; align-items:center; padding:0 8px; font-size:19px; line-height:1; cursor:pointer; background:rgba(255,255,255,.03); }
  .memory-action-item.sel { background:rgba(0,0,0,.30); box-shadow:inset 0 0 0 999px rgba(0,0,0,.04); }
  .memory-action-item .arrow { text-align:right; font-size:24px; }
  .memory-freq-overlay { position:absolute; z-index:8; left:50%; top:52%; transform:translate(-50%,-50%); width:min(360px,90%); padding:12px 14px; border:4px solid rgba(35,30,25,.88); background:rgba(216,90,32,.96); box-shadow:0 0 0 3px rgba(35,30,25,.20), 0 12px 24px rgba(0,0,0,.26), inset 0 0 12px rgba(0,0,0,.16); color:var(--lcdText); font-weight:950; }
  .memory-freq-title { font-size:18px; line-height:1; margin-bottom:7px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .memory-freq-value { min-height:42px; margin-bottom:10px; border:3px solid rgba(35,30,25,.78); display:flex; align-items:center; justify-content:center; gap:2px; background:rgba(255,255,255,.05); }
  .memory-freq-cell { position:relative; min-width:26px; min-height:34px; display:flex; align-items:center; justify-content:center; font-size:28px; line-height:1; letter-spacing:0; }
  .memory-freq-cell.cursor { background:rgba(0,0,0,.30); }
  .memory-freq-keys { display:flex; flex-direction:column; gap:7px; }
  .memory-freq-row { display:grid; gap:7px; }
  .memory-freq-row.row5 { grid-template-columns:repeat(5,1fr); }
  .memory-freq-row.delrow { grid-template-columns:repeat(5,1fr); }
  .memory-freq-key { min-height:34px; border:3px solid rgba(35,30,25,.78); display:flex; align-items:center; justify-content:center; font-size:22px; line-height:1; cursor:pointer; background:rgba(255,255,255,.03); }
  .memory-freq-key.sel { background:rgba(0,0,0,.30); box-shadow:inset 0 0 0 999px rgba(0,0,0,.04); }
  .memory-freq-key.blank { visibility:hidden; pointer-events:none; }
  .menu1-overlay { position:absolute; z-index:8; left:50%; top:52%; transform:translate(-50%,-50%); width:min(440px,94%); padding:14px 16px; border:4px solid rgba(35,30,25,.88); background:rgba(216,90,32,.96); box-shadow:0 0 0 3px rgba(35,30,25,.20), 0 12px 24px rgba(0,0,0,.26), inset 0 0 12px rgba(0,0,0,.16); color:var(--lcdText); font-weight:950; }
  .menu1-title { font-size:24px; line-height:1; margin-bottom:12px; text-align:left; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; display:flex; gap:12px; align-items:baseline; }
  .menu1-title .menu1-input { font-size:.88em; opacity:.96; letter-spacing:1px; }
  .menu1-keys { display:flex; flex-direction:column; gap:8px; }
  .menu1-row { display:grid; gap:8px; }
  .menu1-row.row5 { grid-template-columns:repeat(5,1fr); }
  .menu1-row.row3 { grid-template-columns:1.45fr 1.7fr 1.05fr; }
  .menu1-key { min-height:36px; border:3px solid rgba(35,30,25,.78); display:flex; align-items:center; justify-content:center; font-size:22px; line-height:1; cursor:pointer; background:rgba(255,255,255,.03); white-space:nowrap; }
  .menu1-key.wide { font-size:17px; }
  .menu1-key.sel { background:rgba(0,0,0,.30); box-shadow:inset 0 0 0 999px rgba(0,0,0,.04); }
  .memory-tag-overlay { position:absolute; z-index:8; left:50%; top:52%; transform:translate(-50%,-50%); width:min(520px,96%); padding:12px 14px; border:4px solid rgba(35,30,25,.88); background:rgba(216,90,32,.96); box-shadow:0 0 0 3px rgba(35,30,25,.20), 0 12px 24px rgba(0,0,0,.26), inset 0 0 12px rgba(0,0,0,.16); color:var(--lcdText); font-weight:950; }
  .memory-tag-title { font-size:18px; line-height:1; margin-bottom:7px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .memory-tag-value { min-height:40px; margin-bottom:10px; border:3px solid rgba(35,30,25,.78); display:flex; align-items:center; justify-content:center; gap:2px; background:rgba(255,255,255,.05); }
  .memory-tag-cell { position:relative; min-width:22px; min-height:32px; display:flex; align-items:center; justify-content:center; font-size:24px; line-height:1; }
  .memory-tag-cell.cursor { background:rgba(0,0,0,.30); }
  .memory-tag-keys { display:flex; flex-direction:column; gap:7px; }
  .memory-tag-row { display:grid; gap:5px; }
  .memory-tag-row.row13 { grid-template-columns:repeat(13,1fr); }
  .memory-tag-row.row7 { grid-template-columns:1.1fr 1.1fr 1.1fr .8fr 1.8fr .8fr 1.1fr; }
  .memory-tag-row.row10 { grid-template-columns:repeat(10,1fr); }
  .memory-tag-row.row11 { grid-template-columns:repeat(11,1fr); }
  .memory-tag-row.row12 { grid-template-columns:repeat(12,1fr); }
  .memory-tag-row.row6 { grid-template-columns:repeat(6,1fr); }
  .memory-tag-key { min-height:32px; border:3px solid rgba(35,30,25,.78); display:flex; align-items:center; justify-content:center; font-size:18px; line-height:1; cursor:pointer; background:rgba(255,255,255,.03); white-space:nowrap; }
  .memory-tag-key.sel { background:rgba(0,0,0,.30); box-shadow:inset 0 0 0 999px rgba(0,0,0,.04); }
  .memory-tag-key.blank { visibility:hidden; pointer-events:none; }
  .memory-edit-row .label { font-size:18px; }
  .memory-edit-row .value { font-size:24px; text-align:left; }
  .full-row .num { text-align:left; }
  .full-row .txt { overflow:hidden; white-space:nowrap; }
  .full-row .arrow { text-align:right; font-size:28px; }
  .raw-menu { height:100%; border:3px solid rgba(35,30,25,.78); padding:8px; font-size:17px; font-weight:900; overflow:hidden; }
  .raw-menu-head { font-size:11px; opacity:.75; margin-bottom:6px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .raw-menu-line { min-height:28px; display:flex; align-items:center; border-bottom:2px solid rgba(35,30,25,.25); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .vfo { position:relative; min-width:0; }
  .vfo.active .freq { text-shadow:2px 0 0 var(--lcdText); }
  .freq { font-size:clamp(42px,7vw,78px); line-height:.86; font-weight:950; letter-spacing:-6px; white-space:nowrap; }
  .freq .freq-small { font-size:.48em; letter-spacing:-2px; vertical-align:super; margin-left:2px; line-height:1; }
  .memline { min-height:22px; margin-top:4px; font-size:16px; font-weight:800; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .bottom-row { display:grid; grid-template-columns:1fr 1fr; gap:28px; margin-top:11px; }
  .bottom-cell { display:grid; grid-template-columns:112px 1fr 112px; align-items:end; gap:6px; }
  .lower-label,.mode-box { border:3px solid rgba(35,30,25,.75); min-height:42px; display:flex; align-items:center; justify-content:center; font-weight:900; font-size:27px; line-height:1; }
  .mode-box { font-size:28px; }
  .bar { height:38px; display:flex; align-items:end; gap:4px; border-bottom:4px solid rgba(35,30,25,.8); padding:0 2px 4px; }
  .seg { flex:1; max-width:9px; height:30px; background:rgba(30,25,20,.16); border-left:2px solid rgba(30,25,20,.25); }
  .seg.on { background:var(--lcdText); }
  .meter-fill { display:block; height:30px; align-self:flex-end; background:var(--lcdText); min-width:0; }
  .status-line { margin-top:7px; font-size:13px; color:#a9a9a9; text-align:center; min-height:18px; }
  .save-controls { display:flex; gap:8px; align-items:center; margin-bottom:8px; flex-wrap:wrap; }
  .save-controls button { border-radius:7px; height:28px; padding:0 10px; font-weight:800; font-size:12px; }
  .save-controls button.active { background:linear-gradient(180deg,#ff4352,#7c000d); box-shadow:0 0 12px rgba(255,35,55,.45), inset 0 1px rgba(255,255,255,.25); }
  .save-state { color:#bdbdbd; font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .web-controls { --mic-panel-h:292px; margin-top:18px; display:grid; grid-template-columns:minmax(300px,max-content) minmax(250px,360px); justify-content:center; gap:14px; align-items:start; }
  body.decode-enabled .web-controls { grid-template-columns:320px minmax(250px,300px) minmax(0,1fr); justify-content:stretch; }
  .pad,.rawbox,.audio-panel { background:rgba(0,0,0,.38); border:1px solid #333; border-radius:14px; padding:12px; }
  .pad { justify-self:end; width:max-content; max-width:100%; min-height:var(--mic-panel-h); }
  body.decode-enabled .pad { justify-self:start; }
  .audio-stack { display:grid; grid-template-columns:1fr; grid-template-rows:1fr 1fr; gap:12px; height:var(--mic-panel-h); width:100%; max-width:360px; }
  body.decode-enabled .audio-stack { max-width:none; }
  .audio-panel { margin-top:0; display:grid; grid-template-columns:1fr; gap:7px; align-items:stretch; min-height:0; }
  .pad h3,.rawbox h3,.audio-panel h3 { margin:0 0 4px; font-size:14px; color:#ccc; letter-spacing:.4px; }
  .audio-panel button { border-radius:10px; height:38px; padding:0 14px; font-weight:900; width:100%; }
  .audio-panel button.active { color:#fff; background:linear-gradient(180deg,#278e39,#0c4d19); box-shadow:0 0 18px rgba(53,214,90,.35), inset 0 1px rgba(255,255,255,.22), inset 0 -2px rgba(0,0,0,.55); }
  .audio-panel.tx button.active { background:linear-gradient(180deg,#ff4150,#9a0010); box-shadow:0 0 18px rgba(255,35,55,.55), inset 0 1px rgba(255,255,255,.25), inset 0 -2px rgba(0,0,0,.55); }
  .audio-status { color:#bdbdbd; font-size:12px; min-height:16px; }
  .audio-buffer { width:100%; height:12px; border-radius:999px; background:#111; overflow:hidden; border:1px solid #333; }
  .audio-buffer span { display:block; height:100%; width:0%; background:#777; transition:width .06s linear; }
  .audio-meter { width:100%; height:12px; border-radius:999px; background:#111; overflow:hidden; border:1px solid #333; }
  .audio-meter span { display:block; height:100%; width:0%; background:#777; transition:width .05s linear; }
  .audio-gain { display:grid; grid-template-columns:auto 1fr auto; align-items:center; gap:7px; color:#bdbdbd; font-size:12px; }
  .audio-gain input { width:100%; }
  .mic-grid { display:grid; grid-template-columns:repeat(4,64px); gap:8px; }
  .mic-grid button { height:34px; border-radius:10px; font-weight:900; }
  pre { white-space:pre-wrap; margin:0; font-size:12px; color:#bdbdbd; max-height:240px; overflow:auto; }
  .toast { position:fixed; left:50%; bottom:20px; transform:translateX(-50%); background:#111; border:1px solid #555; color:#eee; padding:10px 14px; border-radius:12px; opacity:0; transition:.2s; pointer-events:none; }
  .toast.show { opacity:1; }
  @media (max-width:900px) { .radio{padding:14px}.top-buttons{margin:0 20px 12px;gap:8px}.face{grid-template-columns:1fr}.side{grid-row:auto;grid-template-columns:1fr 1fr;grid-template-rows:auto}.left-side{order:2}.right-side{order:3}.center{order:1}.web-controls{grid-template-columns:1fr}.pad{width:auto}.audio-stack{height:auto}.mic-grid{grid-template-columns:repeat(4,1fr)} }

  body.radio-off .lcd { background:#8b8b8b !important; box-shadow:inset 0 0 0 4px rgba(80,80,80,.55), inset 0 0 42px rgba(0,0,0,.22) !important; }
  body.radio-off .lcd * { color:#424242 !important; border-color:rgba(70,70,70,.55) !important; text-shadow:none !important; }
  body.radio-off .lcd .seg.on, body.radio-off .lcd .meter-fill { background:#424242 !important; }
  body.radio-off #normalScreen, body.radio-off #menuScreen, body.radio-off #muteOverlay, body.radio-off #dialogOverlay { visibility:hidden !important; }
  body.radio-off .lcd::after { content:"POWER OFF"; position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:clamp(34px,6vw,72px); font-weight:950; letter-spacing:2px; color:#222; z-index:20; background:rgba(160,160,160,.28); }
  body.powering-on .lcd::after { content:"POWERING ON"; position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:clamp(30px,5vw,62px); font-weight:950; letter-spacing:1px; color:#222; z-index:20; background:rgba(160,160,160,.40); }
  body.radio-off .radio button:not(#powerBtn), body.radio-off .knob, body.radio-off .mic-grid, body.radio-off .web-controls, body.radio-off .audio-stack { pointer-events:none !important; opacity:.38; filter:grayscale(1); }
  body.radio-off #powerBtn { pointer-events:auto !important; opacity:1 !important; filter:none !important; }
  body.powering-on #powerBtn { opacity:.7 !important; }
  button:disabled { opacity:.38; cursor:not-allowed; }

</style>
</head>
<body>
<div class="wrap">
  <div class="radio">
    <div class="top-buttons">
      <button id="sdxBtn" class="top-btn" title="tap: S-DX, hold: alternate S-DX action">S-DX</button>
      <button id="bandBtn" class="top-btn" title="tap: BAND, hold: SCOPE">BAND<small>SCOPE</small></button>
      <button id="pmgBtn" class="top-btn" title="tap: PMG, hold: PW">PMG<small>PW</small></button>
      <button id="vmBtn" class="top-btn" title="tap: V/M, hold: MW">V/M<small>MW</small></button>
      <button id="fBtn" class="top-btn" title="tap: quick menu, hold: full menu">F<small>BACK</small></button>
      <button id="powerBtn" class="top-btn power" title="press briefly for lock/unlock, hold for power on/off">⏻</button>
    </div>
    <div class="face">
      <div class="side left-side">
        <div class="knob-block"><div id="ulPressKnob" class="knob" title="tap: short press, hold: long press"></div><div class="knob-label">VOL/SQL</div><div class="knob-actions"><button class="mini-btn" onclick="sendCmd('ul_left','5ms')">◀</button><button id="ulPressBtn" class="mini-btn" title="tap: short press, hold: long press">P</button><button class="mini-btn" onclick="sendCmd('ul_right','5ms')">▶</button></div></div>
        <span id="lled" class="side-led"></span>
        <div class="knob-block"><div id="blPressKnob" class="knob" title="tap: short press, hold: long press"></div><div class="knob-label">DIAL</div><div class="knob-actions"><button class="mini-btn" onclick="sendCmd('bl_left','5ms')">◀</button><button id="blPressBtn" class="mini-btn" title="tap: short press, hold: long press">P</button><button class="mini-btn" onclick="sendCmd('bl_right','5ms')">▶</button></div></div>
      </div>
      <div class="center">
        <div class="brand">DUAL BAND TRANSCEIVER Free RIG</div>
        <div class="lcd-frame"><div class="lcd">
          <div id="muteOverlay" class="mute-overlay">MUTE</div>
          <div id="dialogOverlay" class="dialog-overlay"></div>
          <div id="menuScreen" class="menu-screen"></div>
          <div id="normalScreen" class="normal-screen">
          <div class="lcd-top">
            <div class="top-slot"><span id="ltag" class="tag">VFO</span><span id="lshift" class="tag mini-ind empty"></span><span id="ltone" class="tag mini-ind tone empty"></span></div>
            <div class="top-slot"><span id="rtag" class="tag">VFO</span><span id="rshift" class="tag mini-ind empty"></span><span id="rtone" class="tag mini-ind tone empty"></span></div>
          </div>
          <div class="freq-row"><div id="leftVfo" class="vfo"><div id="lfreq" class="freq">---.---</div><div id="lmem" class="memline"></div></div><div id="rightVfo" class="vfo"><div id="rfreq" class="freq">---.---</div><div id="rmem" class="memline"></div></div></div>
          <div class="bottom-row"><div class="bottom-cell"><div id="llabel" class="lower-label">S</div><div id="lbar" class="bar"></div><div id="lmode" class="mode-box">FM</div></div><div class="bottom-cell"><div id="rlabel" class="lower-label">S</div><div id="rbar" class="bar"></div><div id="rmode" class="mode-box">FM</div></div></div>
          </div>
        </div></div>
        <div id="status" class="status-line">connecting...</div>
      </div>
      <div class="side right-side">
        <div class="knob-block"><div id="urPressKnob" class="knob" title="tap: short press, hold: long press"></div><div class="knob-label">VOL/SQL</div><div class="knob-actions"><button class="mini-btn" onclick="sendCmd('ur_left','5ms')">◀</button><button id="urPressBtn" class="mini-btn" title="tap: short press, hold: long press">P</button><button class="mini-btn" onclick="sendCmd('ur_right','5ms')">▶</button></div></div>
        <span id="rled" class="side-led"></span>
        <div class="knob-block"><div id="brPressKnob" class="knob" title="tap: short press, hold: long press"></div><div class="knob-label">DIAL</div><div class="knob-actions"><button class="mini-btn" onclick="sendDialCmd('br_left')">◀</button><button id="brPressBtn" class="mini-btn" title="tap: short press, hold: long press">P</button><button class="mini-btn" onclick="sendDialCmd('br_right')">▶</button></div></div>
      </div>
    </div>
  </div>
  <div class="web-controls">
    <div class="pad"><h3>Microphone</h3><div class="mic-grid" id="micgrid"></div></div>
    <div class="audio-stack">
      <div class="audio-panel rx">
        <h3>RX Audio</h3>
        <button id="audioBtn" onclick="toggleAudio()">START AUDIO</button>
        <label class="audio-gain"><span>rx gain</span><input id="audioGain" type="range" min="0" max="2" step="0.05" value="0.65"><span id="audioGainVal">0.65×</span></label>
        <div class="audio-buffer" title="browser audio queue"><span id="audioBuf"></span></div>
      </div>
      <div class="audio-panel tx">
        <h3>TX Audio</h3>
        <button id="txPttBtn">PTT</button>
        <label class="audio-gain"><span>mic gain</span><input id="txGain" type="range" min="0" max="24" step="0.25" value="5"><span id="txGainVal">5.0×</span></label>
        <div class="audio-meter" title="browser microphone level after mic gain"><span id="txLevel"></span></div>
      </div>
    </div>
    <div class="rawbox"><h3>Decode</h3><div class="save-controls"><button id="saveStartBtn" onclick="saveAction('start')">SAVE START</button><button id="saveStopBtn" onclick="saveAction('stop')">SAVE STOP</button><span id="saveState" class="save-state">save off</span></div><pre id="raw">waiting...</pre></div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
const mic=['mic_a','mic_b','mic_c','mic_d','mic_1','mic_2','mic_3','mic_p1','mic_4','mic_5','mic_6','mic_p2','mic_7','mic_8','mic_9','mic_p3','mic_star','mic_0','mic_hash','mic_p4','mic_up','mic_down','mic_mute'];
const labels={mic_star:'*',mic_hash:'#',mic_up:'UP',mic_down:'DOWN',mic_mute:'MUTE',mic_a:'A',mic_b:'B',mic_c:'C',mic_d:'D',mic_p1:'P1',mic_p2:'P2',mic_p3:'P3',mic_p4:'P4'};
let audioCtl=null;
let txCtl=null;
let radioPowered=false;
let powerBusy=false;
let displaySettings={lcd_dimmer:'MID',lcd_contrast:5,s_meter_symbol:'BARS',backlight_color:'AMBER'};
let menuLast=null;
function activeMenuForRender(menu){ return menu; }
function setAudioStatus(text){const el=document.getElementById('audioState'); if(el) el.textContent=text;}
function setAudioButton(active){const b=document.getElementById('audioBtn'); if(!b) return; b.classList.toggle('active',!!active); b.textContent=active?'STOP AUDIO':'START AUDIO';}
function setAudioBuf(ms){const el=document.getElementById('audioBuf'); if(!el) return; const pct=Math.max(0,Math.min(100,(ms/180)*100)); el.style.width=pct.toFixed(0)+'%';}
function makeRingPlayer(ctx){
  const ringSeconds=1.0;
  const ring=new Float32Array(Math.max(4096,Math.ceil(ctx.sampleRate*ringSeconds)));
  let rp=0, wp=0, avail=0;
  const maxQueued=Math.max(1024,Math.floor(ctx.sampleRate*0.18));
  const node=ctx.createScriptProcessor(512,0,1);
  function drop(n){n=Math.min(n,avail); rp=(rp+n)%ring.length; avail-=n;}
  function push(samples){
    if(samples.length>=ring.length){samples=samples.subarray(samples.length-ring.length+1); rp=0; wp=0; avail=0;}
    const overflow=avail+samples.length-maxQueued;
    if(overflow>0) drop(overflow);
    for(let i=0;i<samples.length;i++){ring[wp]=samples[i]; wp=(wp+1)%ring.length;}
    avail=Math.min(ring.length,avail+samples.length);
  }
  node.onaudioprocess=(ev)=>{
    const out=ev.outputBuffer.getChannelData(0);
    for(let i=0;i<out.length;i++){
      if(avail>0){out[i]=ring[rp]; rp=(rp+1)%ring.length; avail--;}
      else out[i]=0;
    }
  };
  node.connect(ctx.destination);
  return {node,push,queuedMs:()=>avail*1000/ctx.sampleRate,close:()=>{try{node.disconnect();}catch(e){}}};
}
function getAudioGain(){return Number(document.getElementById('audioGain')?.value||1);}
function updateAudioGainLabel(){const g=document.getElementById('audioGain'); const v=document.getElementById('audioGainVal'); if(g&&v) v.textContent=Number(g.value||0).toFixed(2)+'×';}
function pcmBytesToFloat32(bytes,gain=1){
  const n=bytes.byteLength>>1;
  const out=new Float32Array(n);
  const dv=new DataView(bytes.buffer,bytes.byteOffset,n*2);
  for(let i=0;i<n;i++){
    let x=(dv.getInt16(i*2,true)/32768)*gain;
    if(x>1) x=1; else if(x<-1) x=-1;
    out[i]=x;
  }
  return out;
}
async function startAudio(){
  if(audioCtl) return;
  const info=await fetch('/api/audio',{cache:'no-store'}).then(r=>r.json());
  if(!info.enabled){setAudioStatus('audio disabled'); return;}
  const Ctx=window.AudioContext||window.webkitAudioContext;
  if(!Ctx){setAudioStatus('Web Audio not supported'); return;}
  const ctx=new Ctx({latencyHint:'interactive',sampleRate:info.rate||48000});
  await ctx.resume();
  const player=makeRingPlayer(ctx);
  const abort=new AbortController();
  audioCtl={ctx,player,abort,rate:info.rate||48000,carry:new Uint8Array(0),running:true};
  setAudioButton(true);
  setAudioStatus('starting '+info.device+' @ '+info.rate+' Hz');
  const statTimer=setInterval(()=>{if(audioCtl) {setAudioBuf(audioCtl.player.queuedMs());}},100);
  audioCtl.statTimer=statTimer;
  try{
    const r=await fetch('/audio.pcm?ts='+Date.now(),{cache:'no-store',signal:abort.signal});
    if(!r.ok || !r.body) throw new Error('audio HTTP '+r.status);
    setAudioStatus('live direct PCM · buffer target < 180 ms');
    const reader=r.body.getReader();
    while(audioCtl && audioCtl.running){
      const {value,done}=await reader.read();
      if(done) break;
      let chunk=value;
      if(audioCtl.carry.length){const merged=new Uint8Array(audioCtl.carry.length+chunk.length); merged.set(audioCtl.carry,0); merged.set(chunk,audioCtl.carry.length); chunk=merged; audioCtl.carry=new Uint8Array(0);}
      if(chunk.length&1){audioCtl.carry=chunk.slice(chunk.length-1); chunk=chunk.slice(0,chunk.length-1);}
      if(chunk.length){player.push(pcmBytesToFloat32(chunk,getAudioGain()));}
    }
  }catch(e){
    if(audioCtl) setAudioStatus('audio error: '+e.message);
  }finally{
    stopAudio(false);
  }
}
function stopAudio(updateText=true){
  const a=audioCtl; audioCtl=null;
  if(!a) return;
  a.running=false;
  if(a.statTimer) clearInterval(a.statTimer);
  try{a.abort.abort();}catch(e){}
  try{a.player.close();}catch(e){}
  try{a.ctx.close();}catch(e){}
  setAudioButton(false); setAudioBuf(0);
  if(updateText) setAudioStatus('stopped');
}
function toggleAudio(){ if(audioCtl) stopAudio(); else startAudio(); }
function setTxStatus(text){const el=document.getElementById('txAudioState'); if(el) el.textContent=text;}
function setTxButton(active){
  const b=document.getElementById('txPttBtn');
  if(!b) return;
  b.classList.toggle('active',!!active);
  b.textContent=active?'PTT ON':'PTT';
}
function setTxLevel(v){const el=document.getElementById('txLevel'); if(!el) return; const pct=Math.max(0,Math.min(100,v*100)); el.style.width=pct.toFixed(0)+'%';}
function setTxServerLevel(v){const el=document.getElementById('txServerLevel'); if(!el) return; const pct=Math.max(0,Math.min(100,v*100)); el.style.width=pct.toFixed(0)+'%';}
function setTxDiag(text){const el=document.getElementById('txDiag'); if(el) el.textContent=text;}
function updateTxGainLabel(){const g=document.getElementById('txGain'); const v=document.getElementById('txGainVal'); if(g&&v) v.textContent=Number(g.value||0).toFixed(2).replace(/\.00$/,'')+'×';}
function sleep(ms){return new Promise(resolve=>setTimeout(resolve,ms));}
function wsUrl(path){const proto=location.protocol==='https:'?'wss':'ws'; return proto+'://'+location.host+path;}
function softLimitSample(x){
  // Speech preamp without the harsh square-wave clipping that made v43 distort.
  // Up to about -1 dBFS it is linear; above that it compresses smoothly.
  const s = x < 0 ? -1 : 1;
  const a = Math.abs(x);
  const knee = 0.88;
  if(a <= knee) return x;
  return s * (knee + (1 - knee) * Math.tanh((a - knee) / (1 - knee)));
}
function floatToPcm16(samples,gain){
  const out=new ArrayBuffer(samples.length*2);
  const dv=new DataView(out);
  let peak=0, limited=0;
  for(let i=0;i<samples.length;i++){
    const pre=samples[i]*gain;
    let x=softLimitSample(pre);
    if(x>1) x=1; else if(x<-1) x=-1;
    if(Math.abs(pre)>0.88) limited++;
    const ax=Math.abs(x); if(ax>peak) peak=ax;
    dv.setInt16(i*2, x<0 ? x*32768 : x*32767, true);
  }
  return {buf:out,peak,clipped:limited};
}
async function startTxAudio(){
  if(txCtl) return;
  const info=await fetch('/api/audio',{cache:'no-store'}).then(r=>r.json()).catch(()=>({}));
  const tx=info.tx||{};
  if(!tx.enabled){setTxStatus('TX audio disabled'); return;}
  if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){setTxStatus('microphone API not available'); return;}
  if(!window.isSecureContext){setTxStatus('microphone requires HTTPS or localhost'); return;}
  const rate=tx.rate||48000;
  const Ctx=window.AudioContext||window.webkitAudioContext;
  if(!Ctx){setTxStatus('Web Audio not supported'); return;}

  let stream=null, ctx=null, ws=null;
  try{
    setTxStatus('requesting microphone...');
    stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,sampleRate:rate,echoCancellation:false,noiseSuppression:false,autoGainControl:true}});
    ctx=new Ctx({latencyHint:'interactive',sampleRate:rate});
    await ctx.resume();
    const source=ctx.createMediaStreamSource(stream);
    const procSize=tx.processor_size||1024;
    const packetBytes=procSize*2;
    const maxBufferedBytes=Math.max(packetBytes, Math.min(tx.max_ws_buffer_bytes||65536, packetBytes*2));
    const proc=ctx.createScriptProcessor(procSize,1,1);
    ws=new WebSocket(wsUrl('/audio-tx.ws'));
    ws.binaryType='arraybuffer';
    txCtl={stream,ctx,source,proc,ws,sendEnabled:false,bytes:0,dropped:0,lastPeak:0,pendingBuf:null,maxBufferedBytes,packetBytes};
    setTxButton(true);
    setTxStatus('opening TX link...');

    function sendLatest(payload){
      if(!txCtl || !txCtl.sendEnabled) return false;
      if(ws.readyState!==1) return false;
      if(ws.bufferedAmount > maxBufferedBytes) return false;
      try{
        ws.send(payload);
        txCtl.bytes+=payload.byteLength||0;
        return true;
      }catch(_e){
        return false;
      }
    }

    function flushPending(){
      if(!txCtl || !txCtl.pendingBuf) return;
      if(sendLatest(txCtl.pendingBuf)) txCtl.pendingBuf=null;
    }

    proc.onaudioprocess=(ev)=>{
      const out=ev.outputBuffer.getChannelData(0);
      for(let i=0;i<out.length;i++) out[i]=0;
      if(!txCtl || !txCtl.sendEnabled) return;
      if(ws.readyState!==1) return;
      const gain=Number(document.getElementById('txGain')?.value||1);
      const pcm=floatToPcm16(ev.inputBuffer.getChannelData(0),gain);
      txCtl.lastPeak=pcm.peak;
      if(sendLatest(pcm.buf)) return;
      // Keep only the newest microphone block. If the network/server path
      // falls behind, replace any stale pending PCM instead of transmitting it
      // later with audible jitter/lag.
      txCtl.pendingBuf=pcm.buf;
      txCtl.dropped++;
    };

    source.connect(proc);
    proc.connect(ctx.destination);

    ws.onopen=async()=>{
      if(!txCtl) return;
      setTxStatus('PTT lead '+(tx.ptt_lead_ms||120)+' ms...');
      await sleep(tx.ptt_lead_ms||120);
      if(!txCtl || ws.readyState!==1) return;
      txCtl.sendEnabled=true;
      txCtl.flushTimer=setInterval(flushPending,12);
      setTxStatus('TX live · '+rate+' Hz PCM');
    };
    ws.onerror=()=>{setTxStatus('TX websocket error');};
    ws.onclose=()=>{stopTxAudio(false);};
    txCtl.levelTimer=setInterval(()=>{if(txCtl){setTxLevel(txCtl.lastPeak||0); txCtl.lastPeak*=0.82;}},50);
  }catch(e){
    setTxStatus('TX error: '+e.message);
    if(ws) try{ws.close();}catch(_e){}
    if(ctx) try{ctx.close();}catch(_e){}
    if(stream) stream.getTracks().forEach(t=>t.stop());
    setTxButton(false); setTxLevel(0); txCtl=null;
  }
}
function stopTxAudio(updateText=true){
  const t=txCtl; txCtl=null;
  if(!t) return;
  t.sendEnabled=false;
  if(t.levelTimer) clearInterval(t.levelTimer);
  if(t.flushTimer) clearInterval(t.flushTimer);
  try{if(t.ws.readyState===1) t.ws.send('stop');}catch(e){}
  try{t.ws.close(1000,'stop');}catch(e){}
  try{t.proc.disconnect();}catch(e){}
  try{t.source.disconnect();}catch(e){}
  try{t.ctx.close();}catch(e){}
  try{t.stream.getTracks().forEach(x=>x.stop());}catch(e){}
  setTxButton(false); setTxLevel(0);
  if(updateText) setTxStatus('stopped');
}
function toggleTxPtt(){ if(txCtl) stopTxAudio(); else startTxAudio(); }
async function updateTxServerDiag(){
  try{
    const info=await fetch('/api/audio',{cache:'no-store'}).then(r=>r.json());
    const tx=info.tx||{};
    setTxServerLevel(tx.output_peak||0);
    const g=(tx.total_gain||0).toFixed(1);
    const inpk=Math.round((tx.input_peak||0)*100);
    const outpk=Math.round((tx.output_peak||0)*100);
    const clip=tx.clipped_samples||0;
    const agc=tx.agc_enabled?(' AGC×'+(tx.agc_current_boost||1).toFixed(1)):'';
    const pc=tx.playback_channels||tx.channels||1;
    const run=tx.running?'run':'stopped';
    const err=tx.last_error?(' · ERR '+tx.last_error):'';
    const alsa=tx.alsa_message?(' · '+tx.alsa_message):'';
    const p=info.ptt||{};
    const ptt=' · PTT '+(p.mode||'none')+(p.hidraw?(' '+p.hidraw):'')+(p.active?' ON':'')+(p.last_error?(' · PTTERR '+p.last_error):'');
    setTxDiag('in '+inpk+'% · out '+outpk+'% · '+pc+'ch · '+run+' · gain×'+g+agc+(clip?' · clip '+clip:'')+ptt+err+alsa);
  }catch(e){}
}
function setupTxControls(){
  const ptt=document.getElementById('txPttBtn');
  if(ptt) ptt.addEventListener('click',e=>{e.preventDefault(); toggleTxPtt();});
  const gain=document.getElementById('txGain');
  if(gain) gain.addEventListener('input',updateTxGainLabel);
  updateTxGainLabel();
}
const grid=document.getElementById('micgrid');
mic.forEach(c=>{
  const b=document.createElement('button');
  b.textContent=labels[c]||c.replace('mic_','');
  b.onclick=()=>sendCmd(c);
  grid.appendChild(b);
});
function setupAudioControls(){
  const gain=document.getElementById('audioGain');
  if(gain) gain.addEventListener('input',updateAudioGainLabel);
  updateAudioGainLabel();
}
setupAudioControls();
setupTxControls();
setupPowerButton('powerBtn','power');
setupFButton('fBtn','f',200,700,450);
setupFButton('sdxBtn','sdx',200,700,450);
setupFButton('bandBtn','band',200,700,450);
setupFButton('pmgBtn','pmg',200,700,450);
setupFButton('vmBtn','vm',200,700,450);
function setupKnobPress(id,command){ setupFButton(id,command,200,700,450); }
setupKnobPress('ulPressKnob','ul_press');
setupKnobPress('ulPressBtn','ul_press');
setupKnobPress('urPressKnob','ur_press');
setupKnobPress('urPressBtn','ur_press');
setupKnobPress('blPressKnob','bl_press');
setupKnobPress('blPressBtn','bl_press');
setupKnobPress('brPressKnob','br_press');
setupKnobPress('brPressBtn','br_press');
function toast(msg){const t=document.getElementById('toast'); t.textContent=msg; t.classList.add('show'); setTimeout(()=>t.classList.remove('show'),900);}
async function sendCmd(command,duration){if(!radioPowered){toast('radio off'); return;} const r=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command,duration})}); const j=await r.json().catch(()=>({ok:false,error:'bad json'})); toast(j.ok?command:(j.error||'error'));}
async function saveAction(action){const label='menu25'; const r=await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,label})}); const j=await r.json().catch(()=>({ok:false,error:'bad json'})); toast(j.ok?(j.message||('save '+action)):(j.error||'save error')); updateSaveState(j.save||{});}
function updateSaveState(save){const st=document.getElementById('saveState'); const a=!!(save&&save.active); const b1=document.getElementById('saveStartBtn'); const b2=document.getElementById('saveStopBtn'); if(b1) b1.classList.toggle('active',a); if(b2) b2.classList.toggle('active',a); if(st){ if(a){const sec=save.elapsed_s==null?'':(' '+Number(save.elapsed_s).toFixed(0)+'s'); st.textContent='REC '+(save.screens||0)+' screens / '+(save.commands||0)+' cmd'+sec;} else {st.textContent=save.zip_path?('saved '+save.zip_path):'save off';}}}
async function sendDialCmd(command){
  let dur='5ms';
  if(command==='br_press') dur=null;
  await sendCmd(command,dur);
}
async function menuValueClick(){ await sendCmd('br_press'); }

const heldCmds=new Set();
async function holdCmd(command){
  if(!radioPowered){toast('radio off'); return;}
  if(heldCmds.has(command)) return;
  heldCmds.add(command);
  try{
    const r=await fetch('/api/command_hold',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command})});
    const j=await r.json().catch(()=>({ok:false,error:'bad json'}));
    if(!j.ok){heldCmds.delete(command); toast(j.error||'hold error');}
  }catch(e){heldCmds.delete(command); toast('hold error');}
}
async function releaseCmd(command){
  if(!heldCmds.has(command)) return;
  heldCmds.delete(command);
  try{
    const r=await fetch('/api/command_release',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command})});
    const j=await r.json().catch(()=>({ok:false,error:'bad json'}));
    if(!j.ok) toast(j.error||'release error');
  }catch(e){toast('release error');}
}
function setupMomentaryButton(id,command){
  const b=document.getElementById(id);
  if(!b) return;
  let down=false;
  const start=(e)=>{e.preventDefault(); if(down) return; down=true; b.classList.add('holding'); try{b.setPointerCapture&&b.setPointerCapture(e.pointerId);}catch(_e){} holdCmd(command);};
  const stop=(e)=>{if(!down) return; if(e) e.preventDefault(); down=false; b.classList.remove('holding'); releaseCmd(command);};
  b.addEventListener('pointerdown',start);
  b.addEventListener('pointerup',stop);
  b.addEventListener('pointercancel',stop);
  b.addEventListener('lostpointercapture',stop);
  b.addEventListener('contextmenu',e=>e.preventDefault());
  b.addEventListener('keydown',e=>{if((e.key===' '||e.key==='Enter')&&!down){start(e);}});
  b.addEventListener('keyup',e=>{if(e.key===' '||e.key==='Enter'){stop(e);}});
} 

function applyPowerState(j){
  const wasPowered=radioPowered;
  radioPowered=!!(j&&j.radio_powered);
  powerBusy=!!(j&&j.powering_on);
  if(wasPowered && !radioPowered){
    // Radio really went OFF according to RX watchdog: stop browser-side RX audio
    // so START AUDIO returns to the inactive state automatically.
    if(audioCtl) stopAudio(true);
    if(txCtl) stopTxAudio(true);
    releaseAllHeldKeepalive();
  }
  document.body.classList.toggle('radio-off',!radioPowered);
  document.body.classList.toggle('powering-on',powerBusy);
  document.querySelectorAll('button').forEach(btn=>{
    if(btn.id==='powerBtn'){ btn.disabled=powerBusy; return; }
    btn.disabled=!radioPowered || powerBusy;
  });
  document.querySelectorAll('input,select,textarea').forEach(el=>{ el.disabled=!radioPowered || powerBusy; });
}
async function powerStartFromUi(){
  if(powerBusy) return;
  powerBusy=true;
  applyPowerState({radio_powered:false,powering_on:true});
  toast('powering on...');
  try{
    const r=await fetch('/api/power_start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const j=await r.json().catch(()=>({ok:false,error:'bad json'}));
    toast(j.ok?(j.message||'power-on sequence sent'):(j.error||'power-on error'));
  }catch(e){
    toast('power-on error');
  }finally{
    powerBusy=false;
    try{ await poll(); }catch(_e){}
  }
}
function setupPowerButton(id,command){
  const b=document.getElementById(id);
  if(!b) return;
  let down=false, downAt=0, timer=null, startupSent=false;
  const thresholdMs=850;
  const clearTimer=()=>{ if(timer){clearTimeout(timer); timer=null;} };
  const start=(e)=>{
    e.preventDefault();
    if(down || powerBusy) return;
    down=true; startupSent=false; downAt=performance.now();
    b.classList.add('holding');
    try{b.setPointerCapture&&b.setPointerCapture(e.pointerId);}catch(_e){}
    if(!radioPowered){
      timer=setTimeout(()=>{
        if(down && !startupSent){ startupSent=true; powerStartFromUi(); }
      },thresholdMs);
    }else{
      holdCmd(command);
    }
  };
  const stop=(e)=>{
    if(!down) return;
    if(e) e.preventDefault();
    const dt=performance.now()-downAt;
    down=false; b.classList.remove('holding'); clearTimer();
    if(startupSent){
      return;
    }
    if(!radioPowered){
      toast('hold POWER');
      return;
    }
    releaseCmd(command);
    if(dt>=thresholdMs){
      // The real long press powers the radio off. OFF state is now decided
      // by the RX watchdog: when frames stop arriving, the UI turns grey and
      // S returns LOW automatically.
      toast('waiting for RX to stop...');
      setTimeout(async()=>{ try{ await poll(); }catch(_e){} },1200);
    }
  };
  b.addEventListener('pointerdown',start);
  b.addEventListener('pointerup',stop);
  b.addEventListener('pointercancel',stop);
  b.addEventListener('lostpointercapture',stop);
  b.addEventListener('contextmenu',e=>e.preventDefault());
  b.addEventListener('keydown',e=>{if((e.key===' '||e.key==='Enter')&&!down){start(e);}});
  b.addEventListener('keyup',e=>{if(e.key===' '||e.key==='Enter'){stop(e);}});
}

function releaseAllHeld(){
  document.querySelectorAll('.holding').forEach(el=>el.classList.remove('holding'));
  for(const c of Array.from(heldCmds)) releaseCmd(c);
}
function releaseAllHeldKeepalive(){
  const commands=Array.from(heldCmds);
  heldCmds.clear();
  document.querySelectorAll('.holding').forEach(el=>el.classList.remove('holding'));
  for(const command of commands){
    const payload=JSON.stringify({command});
    try{
      if(navigator.sendBeacon){
        navigator.sendBeacon('/api/command_release', new Blob([payload],{type:'application/json'}));
      }else{
        fetch('/api/command_release',{method:'POST',headers:{'Content-Type':'application/json'},body:payload,keepalive:true});
      }
    }catch(e){}
  }
}
window.addEventListener('blur',releaseAllHeld);
window.addEventListener('pagehide',releaseAllHeldKeepalive);
function segs(id,count,style='BARS'){
  const el=document.getElementById(id);
  if(!el) return;
  el.innerHTML='';
  count=Math.max(0,Math.min(16,Number(count)||0));
  style=(style||'BARS').toUpperCase().replace(/_/g,' ');
  if(style==='FULL SIZE') style='SOLID';
  if(style==='STAIRS' || style==='SCALE') style='RAMP';
  if(style==='CONTINUE') style='FINE';
  if(style==='SOLID'){
    const s=document.createElement('span');
    s.className='meter-fill';
    s.style.width=(count/16*100).toFixed(0)+'%';
    el.appendChild(s);
    return;
  }
  const n=style==='FINE'?32:16;
  const lit=Math.round(count/16*n);
  for(let i=0;i<n;i++){
    const s=document.createElement('span');
    s.className='seg'+(i<lit?' on':'');
    if(style==='RAMP'){
      s.style.height=(8+Math.round((i+1)/n*26))+'px';
    }
    if(style==='FINE'){
      s.style.maxWidth='4px';
      s.style.borderLeft='1px solid rgba(30,25,20,.35)';
    }
    el.appendChild(s);
  }
}
function normalizeToSegments(raw,maxRaw){
  raw=Number(raw)||0;
  maxRaw=Number(maxRaw)||1;
  if(raw<=0) return 0;
  raw=Math.max(0,Math.min(maxRaw,raw));
  return Math.max(1,Math.min(16,Math.round((raw/maxRaw)*16)));
}
function normalizeVol(raw){
  // Volume osservato: scala lineare 0..127.
  return normalizeToSegments(raw,127);
}
function normalizeSql(raw){
  // Squelch osservato: scala lineare 0..32.
  return normalizeToSegments(raw,32);
}
function normalizeRxMeter(raw){
  // RX S-meter osservato: scala 0..10, con 10 = barra piena.
  return normalizeToSegments(raw,10);
}
function normalizeTxMeter(raw){
  // TX/PO meter osservato: scala 0..10, con 10 = barra piena.
  return normalizeToSegments(raw,10);
}
function firstNumber(){
  for(const v of arguments){
    if(v !== undefined && v !== null) return Number(v)||0;
  }
  return 0;
}
function applyDisplaySettings(settings){
  displaySettings=Object.assign(displaySettings,settings||{});
  const root=document.documentElement;
  const color=(displaySettings.backlight_color||'AMBER').toUpperCase();
  if(color==='WHITE'){
    // Keep all LCD text/accent colors exactly like AMBER mode; only the
    // backlight/background changes from orange/white to light-blue/white.
    root.style.setProperty('--orange','#d9efff');
    root.style.setProperty('--orange2','#fffdf4');
    root.style.setProperty('--lcdBottom','#b7ddf4');
    root.style.setProperty('--lcdAccent','#d75a20');
    root.style.setProperty('--lcdAccentGlow','rgba(214,86,31,.2)');
  }else{
    root.style.setProperty('--orange','#d75a20');
    root.style.setProperty('--orange2','#e9752b');
    root.style.setProperty('--lcdBottom','#c94c18');
    root.style.setProperty('--lcdAccent','#d75a20');
    root.style.setProperty('--lcdAccentGlow','rgba(214,86,31,.2)');
  }
  const lcd=document.querySelector('.lcd');
  if(lcd){
    const dim=(displaySettings.lcd_dimmer||'MID').toUpperCase();
    // LCD DIMMER is attenuation: MAX = darkest, OFF = brightest.
    const brightness=dim==='MAX'?0.50:dim==='OFF'?1.12:0.82;
    const c=Math.max(1,Math.min(9,Number(displaySettings.lcd_contrast)||5));
    const contrast=0.68+((c-1)/8)*0.78;
    lcd.style.filter=`brightness(${brightness}) contrast(${contrast})`;
  }
  const radio=document.querySelector('.radio');
  if(radio){
    radio.dataset.dimmer=(displaySettings.lcd_dimmer||'MID').toLowerCase();
  }
}
function meterStyleForSide(side){
  return (side && (side.rx_active||side.tx_active)) ? (displaySettings.s_meter_symbol||'BARS') : 'BARS';
}
function lowerLevel(side){
  const l=side.lower||{}; const label=l.label||'S';
  // Quando il lato sta ricevendo o trasmettendo, la stessa barra del display
  // rappresenta il meter RF/audio, non VOL/SQL.
  if(side.tx_active){
    return normalizeTxMeter(side.s_meter_raw || 0);
  }
  if(side.rx_active){
    return normalizeRxMeter(side.s_meter_raw || 0);
  }
  if(label==='VOL') {
    return normalizeVol(firstNumber(l.vol_raw, l.bar_raw, l.value_raw, l.side_value_raw, 0));
  }
  if(label==='SQL') {
    return normalizeSql(firstNumber(l.sql_raw, l.bar_raw, l.value_raw, l.side_value_raw, 0));
  }
  return 0;
}
function sourceTag(side){if(side.source&&side.source.startsWith('MEM')) return side.mem_group||'MEM'; if(side.source&&side.source.startsWith('VFO')) return 'VFO'; if(side.source&&side.source.startsWith('HOME')) return 'HOME'; return side.source||'---';}
function memLine(side){let bits=[]; if(side.source&&side.source.startsWith('MEM')){if(side.mem_no) bits.push(side.mem_no); if(side.name) bits.push(side.name);} return bits.join(' ');}
function lowerLabel(side){
  // Use the radio-decoded lower display label directly: S / SQL / VOL / S-DX / ASP / AUTO-A.
  return side.lower?.label||'S';
}
function setBadge(id,text){const el=document.getElementById(id); el.textContent=text||''; el.classList.toggle('empty',!text);}
function setLed(id,state){const el=document.getElementById(id); if(!el) return; el.className='side-led'+(state==='rx'?' rx':state==='tx'?' tx':'');}
function escHtml(x){return String(x??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function renderDialogOverlay(ov){
  const dlg=document.getElementById('dialogOverlay');
  if(!dlg) return;
  if(!ov || !ov.active || ov.kind!=='confirm'){ dlg.classList.remove('show'); dlg.innerHTML=''; return; }
  const opts=(ov.options||[]).map((o,idx)=>`<div class="dialog-option ${o.selected?'sel':''}">${escHtml(o.text||'')}</div>`).join('');
  dlg.innerHTML=`<div class="dialog-title">${escHtml(ov.title||'')}</div><div class="dialog-msg">${escHtml(ov.message||'')}</div><div class="dialog-options">${opts}</div>`;
  dlg.classList.add('show');
}
function freqHtml(freq){
  const s=String(freq||'---.---');
  const esc=(x)=>String(x).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  const m=s.match(/^(\d{3}\.\d{3})(\d{2})$/);
  if(m) return esc(m[1])+`<span class="freq-small">${esc(m[2])}</span>`;
  return esc(s);
}
function updateSide(prefix,data){document.getElementById(prefix+'tag').textContent=sourceTag(data); setBadge(prefix+'shift',data.shift||''); setBadge(prefix+'tone',data.tone||''); document.getElementById(prefix+'freq').innerHTML=freqHtml(data.freq||'---.---'); document.getElementById(prefix+'mem').textContent=memLine(data); document.getElementById(prefix+'mode').textContent=data.mode||''; document.getElementById(prefix+'label').textContent=lowerLabel(data); segs(prefix+'bar',lowerLevel(data),meterStyleForSide(data));}
async function poll(){try{
  const r=await fetch('/api/state');
  const j=await r.json();
  applyPowerState(j);
  applyDisplaySettings(j.display_settings||{});
  const menuForRender=activeMenuForRender(j.menu||{});
  const showingPmg=!!(menuForRender && menuForRender.visible && menuForRender.type==='pmg');
  if(!showingPmg){
    updateSide('l',j.left);
    updateSide('r',j.right);
    document.getElementById('leftVfo').classList.toggle('active',j.main==='LEFT');
    document.getElementById('rightVfo').classList.toggle('active',j.main==='RIGHT');
  }

  let ll='off', rl='off';
  const a=j.activity||{};
  if(a.tx_left) ll='tx';
  if(a.tx_right) rl='tx';
  if(ll!=='tx' && a.rx_left) ll='rx';
  if(rl!=='tx' && a.rx_right) rl='rx';

  setLed('lled',ll);
  setLed('rled',rl);
  const mo=document.getElementById('muteOverlay');
  const ov=j.overlay||{};
  renderDialogOverlay(ov);
  const ovText=(ov.kind==='confirm') ? '' : (ov.text || (j.mute?'MUTE':''));
  if(mo){ mo.textContent=ovText || 'MUTE'; mo.classList.toggle('show',!!ovText); }
  renderMenu(menuForRender);
  let st=[];
  if(!j.radio_powered){ st.push(j.powering_on?'POWERING ON':'POWER OFF'); }
  st.push(j.demo?'DEMO':'RX age '+(j.age_s??0).toFixed(2)+'s');
  if(j.build_id) st.push(j.build_id);
  if(j.activity){
    let sides=[];
    if(a.rx_left) sides.push('RX-L');
    if(a.rx_right) sides.push('RX-R');
    if(a.tx_left) sides.push('TX-L');
    if(a.tx_right) sides.push('TX-R');
    st.push((a.status||'idle')+(sides.length?' '+sides.join('/') : ''));
  }
  if(ovText) st.push(ovText);
  updateSaveState(j.save||{});
  if(j.save&&j.save.active) st.push('SAVE '+(j.save.screens||0)+'/'+(j.save.commands||0));
  document.getElementById('status').textContent=st.join(' · ');
  const rawEl=document.getElementById('raw'); if(rawEl) rawEl.textContent=j.human||'';
}catch(e){document.getElementById('status').textContent='web error: '+e;}}

function renderMenu(menu){
  const screen=document.getElementById('menuScreen');
  const normal=document.getElementById('normalScreen');
  if(!screen || !normal) return;
  if(!menu || !menu.visible){
    screen.classList.remove('show');
    screen.innerHTML='';
    normal.classList.remove('hidden');
    return;
  }
  normal.classList.add('hidden');
  screen.classList.add('show');
  if(menu.type==='pmg'){
    const prevPmg=(menuLast && menuLast.type==='pmg') ? menuLast : null;
    menuLast=menu;
    const esc=(x)=>String(x??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    const channels=Array.isArray(menu.channels)?menu.channels:[];
    while(channels.length<5) channels.push({index:channels.length+1,label:'P'+(channels.length+1),registered:false,bar:0,shadow:0});
    const selected=Number(menu.selected||1);
    const modeText=(menu.mode||'MANUAL').toUpperCase();
    const isAuto=modeText==='AUTO';
    const totalMain=channels.reduce((a,ch)=>a+Math.max(0,Number(ch.bar||0)),0);
    let flashSet=new Set();
    if(totalMain===0){
      if(prevPmg && Array.isArray(prevPmg.channels)){
        prevPmg.channels.forEach(ch=>{
          const idx=Number(ch.index||0);
          if(idx!==selected && (Number(ch.bar||0)>0 || Number(ch.shadow||0)>0)) flashSet.add(idx);
        });
      }
      if(flashSet.size===0){
        channels.forEach(ch=>{
          const idx=Number(ch.index||0);
          if(idx!==selected && !!ch.registered && idx<selected) flashSet.add(idx);
        });
      }
    }
    const bars=channels.slice(0,5).map(ch=>{
      const reg=!!ch.registered;
      const bar=Math.max(0,Math.min(10,Number(ch.bar||0)));
      const shadow=Math.max(0,Math.min(10,Number(ch.shadow||0)));
      const h=reg && bar>0 ? Math.max(2,bar*10) : 0;
      const hs=reg && shadow>0 ? Math.max(2,shadow*10) : 0;
      const cls=['pmg-bar'];
      if(ch.receiving) cls.push('receiving');
      else if(ch.recent) cls.push('recent');
      return `<div class="pmg-slot"><div class="pmg-barwrap">${reg&&hs>0?`<span class="pmg-bar-shadow" style="height:${hs}%"></span>`:''}${reg&&h>0?`<span class="${cls.join(' ')}" style="height:${h}%"></span>`:''}</div><div class="pmg-line ${isAuto?'auto':''}"></div></div>`;
    }).join('');
    const labels=channels.slice(0,5).map(ch=>{
      const idx=Number(ch.index||0);
      const cls=['pmg-label'];
      if(idx===selected) cls.push('sel');
      else if(flashSet.has(idx)) cls.push('flash');
      return `<div class="${cls.join(' ')}"><span class="pmg-label-text">${esc(ch.label||('P'+idx))}</span></div>`;
    }).join('');
    const source=menu.source?`<span class="pmg-badge">${esc(menu.source)}</span>`:'';
    const mode=menu.rx_mode?`<span class="pmg-badge">${esc(menu.rx_mode)}</span>`:'';
    const shift=menu.shift?`<span class="pmg-badge">${esc(menu.shift)}</span>`:'';
    const tone=menu.tone?`<span class="pmg-badge">${esc(menu.tone)}</span>`:'';
    const pmg=`<span class="pmg-badge">PMG</span>`;
    const freq=freqHtml((menu.freq && menu.freq !== '---.---') ? menu.freq : (prevPmg && prevPmg.freq ? prevPmg.freq : '---.---'));
    screen.innerHTML=`<div class="pmg-screen">
      <div class="pmg-top"><div class="pmg-badges">${pmg}${source}${mode}${shift}${tone}</div><div class="pmg-freq">${freq}</div></div>
      <div class="pmg-body"><div class="pmg-bars">${bars}</div><div class="pmg-labels">${labels}</div></div>
    </div>`;
    return;
  }

  if(menu.type==='scope'){
    menuLast=menu;
    const esc=(x)=>String(x??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    const bars=Array.isArray(menu.bars)?menu.bars:[];
    const n=Math.max(1,bars.length||Number(menu.channels)||23);
    const center=Number.isFinite(Number(menu.marker_index))?Number(menu.marker_index):Math.floor(n/2);
    const gridStyle=`grid-template-columns:repeat(${n},1fr)`;
    const markerLeft=`${(((center+0.5)/n)*100).toFixed(4)}%`;
    const barHtml=Array.from({length:n},(_,i)=>{
      const raw=Number(bars[i]||0);
      const h=Math.max(2,Math.min(100,raw*10));
      const cls=['scope-bar'];
      if(i===center) cls.push('center');
      if(raw>=7) cls.push('strong');
      return `<span class="${cls.join(' ')}" style="height:${h}%"></span>`;
    }).join('');
    const dotHtml=Array.from({length:n},(_,i)=>{
      const cls=['scope-dot'];
      if(i===center) cls.push('center');
      return `<span class="${cls.join(' ')}"></span>`;
    }).join('');
    const tone=menu.tone?`<span class="scope-badge">${esc(menu.tone)}</span>`:'';
    const shift=menu.shift?`<span class="scope-badge">${esc(menu.shift)}</span>`:'';
    const source=menu.source?`<span class="scope-badge">${esc(menu.source)}</span>`:'';
    const mode=menu.mode?`<span class="scope-badge">${esc(menu.mode)}</span>`:'';
    const width=menu.width?String(menu.width).toUpperCase():'';
    const ch=menu.channels?`${menu.channels}CH`:'';
    const interval=menu.interval?String(menu.interval):'';
    const intervalBadge=interval?`<span class="scope-badge">${esc(interval)}</span>`:'';
    const freq=freqHtml(menu.freq||'---.---');
    screen.innerHTML=`<div class="scope-screen">
      <div class="scope-top"><div class="scope-badges">${source}${mode}${shift}${tone}${intervalBadge}</div><div class="scope-freq">${freq}</div></div>
      <div class="scope-body">
        <div class="scope-bars" style="${gridStyle}">${barHtml}<div class="scope-marker-line" style="left:${markerLeft}"></div><div class="scope-marker-cap" style="left:${markerLeft}"></div></div>
        <div class="scope-dots" style="${gridStyle}">${dotHtml}</div>
      </div>
    </div>`;
    return;
  }

  if(menu.type==='raw'){
    const lines=(menu.lines||[]).map(x=>`<div class="raw-menu-line">${String(x).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}</div>`).join('');
    screen.innerHTML=`<div class="raw-menu"><div class="raw-menu-head">${menu.header||''}</div>${lines}</div>`;
    return;
  }
  if(menu.type==='dtmf_edit'){
    menuLast=menu;
    const esc=(x)=>String(x).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    const chars=(menu.cells||[]).slice(0,16);
    while(chars.length<16) chars.push('');
    const selected=Number.isFinite(Number(menu.selected_key))?Number(menu.selected_key):(Number.isFinite(Number(menu.cursor_pos))?Number(menu.cursor_pos):-1);
    const editPos=Number.isFinite(Number(menu.edit_cursor_pos))?Number(menu.edit_cursor_pos):-1;
    const entry=chars.map((ch,idx)=>{
      const cls=['dtmf-entry-cell'];
      if(ch==='') cls.push('empty');
      if(ch===' ') cls.push('space');
      if(idx===editPos) cls.push('cursor');
      if(editPos>=16 && idx===15) cls.push('cursor-after');
      const shown = ch===' ' ? '&nbsp;' : esc(ch||' ');
      return `<span class="${cls.join(' ')}">${shown}</span>`;
    }).join('');
    const labels=(menu.keypad||['1','2','3','4','5','6','7','8','9','0','A','B','C','D','*','#','◀','SP','▶','DEL']).slice(0,20);
    while(labels.length<20) labels.push('');
    const button=(label,idx)=>{
      const tool=idx>=16?' tool':'';
      const sel=idx===selected?' sel':'';
      return `<button class="dtmf-key${tool}${sel}" onclick="dtmfKeyClick(${idx})">${esc(label)}</button>`;
    };
    const r1=labels.slice(0,5).map((x,i)=>button(x,i)).join('');
    const r2=labels.slice(5,10).map((x,i)=>button(x,i+5)).join('');
    const r3=labels.slice(10,20).map((x,i)=>button(x,i+10)).join('');
    screen.innerHTML=`<div class="dtmf-edit"><div class="dtmf-entry-line">${entry}</div><div class="dtmf-keypad"><div class="dtmf-key-row row5">${r1}</div><div class="dtmf-key-row row5">${r2}</div><div class="dtmf-key-row row10">${r3}</div></div></div>`;
    return;
  }
  if(menu.type==='submenu'){
    menuLast=menu;
    const esc=(x)=>String(x).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    const rows=(menu.rows||[]).slice(0,3);
    const readOnly=!!menu.read_only;
    const titleNum=(menu.parent_num===undefined || menu.parent_num===null)?'':String(menu.parent_num).padStart(2,'0');
    const htmlTitle=`<div class="full-row footer submenu-title"><span class="num">${titleNum}</span><span class="txt">${esc(menu.title||'')}</span><span class="arrow"></span></div>`;
    const htmlRows=rows.map((row,idx)=>{
      const num=(row.num===undefined || row.num===null)?'':esc(row.num);
      const cls=['full-row','submenu-row'];
      if(readOnly) cls.push('read-only');
      const isSel = !readOnly && idx===menu.selected_row;
      const valueFocus = !!(!readOnly && menu.editing && (row.editing || isSel));
      const keyFocus = !!(!readOnly && isSel && !valueFocus);
      if(keyFocus) cls.push('focus-key');
      if(valueFocus) cls.push('focus-value');
      const raw=(row.value_source==='unknown' && row.raw_value)?` <span class="choice-raw">${esc(row.raw_value)}</span>`:'';
      const click=readOnly?'':` onclick="menuFullRowClick(${idx})"`;
      const arrow=readOnly?'':'&rsaquo;';
      const rowEsc=esc(row.text||'');
      const rowText=(Number(menu.parent_num)===32 || Number(menu.parent_num)===31) ? rowEsc.replace(/ /g,'&nbsp;') : rowEsc;
      return `<div class="${cls.join(' ')}"${click}><span class="sub-key">${num}</span><span class="sub-val">${rowText}${raw}</span><span class="arrow">${arrow}</span></div>`;
    }).join('');
    screen.innerHTML=`<div class="full-list">${htmlTitle}${htmlRows}</div>`;
    return;
  }
  if(menu.type==='memory_list'){
    menuLast=menu;
    const esc=(x)=>String(x).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    const rows=(menu.rows||[]).slice(0,4);
    const titleNum=(menu.parent_num===undefined || menu.parent_num===null)?'':String(menu.parent_num).padStart(2,'0');
    const htmlTitle=`<div class="full-row footer submenu-title"><span class="num">${titleNum}</span><span class="txt">${esc(menu.title||'')}</span><span class="arrow"></span></div>`;
    const htmlRows=rows.map((row,idx)=>{
      const cls=['memory-row','memory-list-row'];
      if(idx===menu.selected_row) cls.push('sel');
      return `<div class="${cls.join(' ')}" onclick="memoryRowClick(${idx},${Number(row.mem_no??row.slot??0)})"><span class="slot">${esc(row.num||'')}</span><span class="freq">${esc(row.freq||row.value||'')}</span><span class="name">${esc(row.name||row.text||'')}</span><span class="arrow">&rsaquo;</span></div>`;
    }).join('');
    screen.innerHTML=`<div class="full-list">${htmlTitle}${htmlRows}</div>`;
    return;
  }
  if(menu.type==='memory_select'){
    menuLast=menu;
    const esc=(x)=>String(x).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    const memRows=(menu.memory_rows||[]).slice(0,4);
    const actionRows=(menu.rows||[]).slice(0,5);
    const titleNum=(menu.parent_num===undefined || menu.parent_num===null)?'':String(menu.parent_num).padStart(2,'0');
    const htmlTitle=`<div class="full-row footer submenu-title"><span class="num">${titleNum}</span><span class="txt">MEMORY LIST</span><span class="arrow"></span></div>`;
    const htmlRows=memRows.map((row,idx)=>{
      const cls=['memory-row','memory-list-row'];
      if(idx===menu.selected_memory_row) cls.push('sel');
      return `<div class="${cls.join(' ')}"><span class="slot">${esc(row.num||'')}</span><span class="freq">${esc(row.freq||row.value||'')}</span><span class="name">${esc(row.name||row.text||'')}</span><span class="arrow">&rsaquo;</span></div>`;
    }).join('');
    const memInfo=[menu.memory_num!==undefined && menu.memory_num!==null ? String(menu.memory_num).padStart(3,'0') : '', menu.memory_freq||'', menu.memory_name||''].filter(Boolean).join(' ');
    const actionTitle=`<div class="memory-action-title">${esc(memInfo||'MEMORY')}</div>`;
    const actions=actionRows.map((row,idx)=>{
      const cls=['memory-action-item'];
      if(idx===menu.selected_row) cls.push('sel');
      return `<div class="${cls.join(' ')}" onclick="menuFullRowClick(${idx})"><span>${esc(row.label||row.text||'')}</span><span class="arrow">&rsaquo;</span></div>`;
    }).join('');
    const overlay=`<div class="memory-action-overlay">${actionTitle}<div class="memory-action-list">${actions}</div></div>`;
    screen.innerHTML=`<div class="full-list">${htmlTitle}${htmlRows}</div>${overlay}`;
    return;
  }

  if(menu.type==='menu1_keypad'){
    menuLast=menu;
    const esc=(x)=>String(x).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    const labels=(menu.keypad||['1','2','3','4','5','6','7','8','9','0','MEM CH','MEM LIST','DEL']).slice(0,13);
    const selected=Number.isFinite(Number(menu.selected_key))?Number(menu.selected_key):-1;
    const keyHtml=(label,idx,extra='')=>{
      const cls=['menu1-key'];
      if(extra) cls.push(extra);
      if(idx===selected) cls.push('sel');
      return `<div class="${cls.join(' ')}" onclick="menu1KeyClick(${idx})">${esc(label||'')}</div>`;
    };
    const r1=labels.slice(0,5).map((x,i)=>keyHtml(x,i)).join('');
    const r2=labels.slice(5,10).map((x,i)=>keyHtml(x,i+5)).join('');
    const r3=labels.slice(10,13).map((x,i)=>keyHtml(x,i+10,'wide')).join('');
    const input=String(menu.input_value||'');
    const title=`<span>${esc(menu.mode_title||menu.title||'FREQUENCY')}</span>${input?`<span class="menu1-input">${esc(input)}</span>`:''}`;
    const overlay=`<div class="menu1-overlay"><div class="menu1-title">${title}</div><div class="menu1-keys"><div class="menu1-row row5">${r1}</div><div class="menu1-row row5">${r2}</div><div class="menu1-row row3">${r3}</div></div></div>`;
    screen.innerHTML=overlay;
    return;
  }

  if(menu.type==='memory_tag_keypad'){
    menuLast=menu;
    const esc=(x)=>String(x).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    const rows=(menu.rows||[]).slice(0,4);
    const titleNum=(menu.parent_num===undefined || menu.parent_num===null)?'':String(menu.parent_num).padStart(2,'0');
    const htmlTitle=`<div class="full-row footer submenu-title"><span class="num">${titleNum}</span><span class="txt">${esc(menu.title||'')}</span><span class="arrow"></span></div>`;
    const htmlRows=rows.map((row,idx)=>{
      const cls=['memory-row','memory-edit-row'];
      if(idx===menu.selected_row || row.editing) cls.push('sel');
      return `<div class="${cls.join(' ')}"><span class="label">${esc(row.label||row.text||'')}</span><span class="value">${esc(row.value||'')}</span><span class="arrow">&rsaquo;</span></div>`;
    }).join('');
    const labels=(menu.keypad||['A','B','C','D','E','F','G','H','I','J','K','L','M','N','O','P','Q','R','S','T','U','V','W','X','Y','Z','abc','123','#%^','->','SPACE','<-','DEL']).slice(0,33);
    const selected=Number.isFinite(Number(menu.selected_key))?Number(menu.selected_key):-1;
    const keyHtml=(label,idx)=>{
      const cls=['memory-tag-key'];
      const inactive=(label===null || label===undefined || String(label)==='');
      if(inactive) cls.push('blank');
      if(idx===selected) cls.push('sel');
      const click=inactive?'':` onclick="memoryTagKeyClick(${idx})"`;
      return `<div class="${cls.join(' ')}"${click}>${esc(label||'')}</div>`;
    };
    const inputCells=Array.isArray(menu.input_cells)?menu.input_cells:[];
    const inputValue=menu.input_value||menu.current_value||'';
    const cursorPos=Number.isFinite(Number(menu.input_cursor_pos))?Number(menu.input_cursor_pos):-1;
    const valueHtml=(inputCells.length?inputCells:String(inputValue).split('')).map((cell,idx)=>{
      const ch=(typeof cell==='string')?cell:(cell.text||'');
      const cls=['memory-tag-cell'];
      if(idx===cursorPos || (cell && cell.cursor)) cls.push('cursor');
      return `<span class="${cls.join(' ')}">${ch===' '||ch===''?'&nbsp;':esc(ch)}</span>`;
    }).join('');
    const rowsSpec=Array.isArray(menu.keypad_rows)&&menu.keypad_rows.length?menu.keypad_rows:[
      {cls:'row13', idx:[0,1,2,3,4,5,6,7,8,9,10,11,12]},
      {cls:'row13', idx:[13,14,15,16,17,18,19,20,21,22,23,24,25]},
      {cls:'row7', idx:[26,27,28,29,30,31,32]},
    ];
    const rowsHtml=rowsSpec.map(row=>{
      const idxs=Array.isArray(row.idx)?row.idx:[];
      const cls=row.cls||('row'+idxs.length);
      return `<div class="memory-tag-row ${esc(cls)}">${idxs.map(i=>keyHtml(labels[i]||'',i)).join('')}</div>`;
    }).join('');
    const overlay=`<div class="memory-tag-overlay"><div class="memory-tag-title">${esc(menu.target_label||'TAG')}</div><div class="memory-tag-value">${valueHtml||'&nbsp;'}</div><div class="memory-tag-keys">${rowsHtml}</div></div>`;
    screen.innerHTML=`<div class="full-list">${htmlTitle}${htmlRows}</div>${overlay}`;
    return;
  }

  if(menu.type==='memory_freq_keypad'){
    menuLast=menu;
    const esc=(x)=>String(x).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    const rows=(menu.rows||[]).slice(0,4);
    const titleNum=(menu.parent_num===undefined || menu.parent_num===null)?'':String(menu.parent_num).padStart(2,'0');
    const htmlTitle=`<div class="full-row footer submenu-title"><span class="num">${titleNum}</span><span class="txt">${esc(menu.title||'')}</span><span class="arrow"></span></div>`;
    const htmlRows=rows.map((row,idx)=>{
      const cls=['memory-row','memory-edit-row'];
      if(idx===menu.selected_row || row.editing) cls.push('sel');
      return `<div class="${cls.join(' ')}"><span class="label">${esc(row.label||row.text||'')}</span><span class="value">${esc(row.value||'')}</span><span class="arrow">&rsaquo;</span></div>`;
    }).join('');
    const labels=(menu.keypad||['1','2','3','4','5','6','7','8','9','0','DEL']).slice(0,11);
    const selected=Number.isFinite(Number(menu.selected_key))?Number(menu.selected_key):-1;
    const keyHtml=(label,idx,extra='')=>{
      const cls=['memory-freq-key'];
      if(extra) cls.push(extra);
      if(idx===selected) cls.push('sel');
      const click=extra==='blank'?'':` onclick="memoryFreqKeyClick(${idx})"`;
      return `<div class="${cls.join(' ')}"${click}>${esc(label||'')}</div>`;
    };
    const r1=labels.slice(0,5).map((x,i)=>keyHtml(x,i)).join('');
    const r2=labels.slice(5,10).map((x,i)=>keyHtml(x,i+5)).join('');
    const r3=[keyHtml('',-1,'blank'),keyHtml('',-1,'blank'),keyHtml('',-1,'blank'),keyHtml('',-1,'blank'),keyHtml(labels[10]||'DEL',10)].join('');
    const inputCells=Array.isArray(menu.input_cells)?menu.input_cells:[];
    const inputValue=menu.input_value||menu.current_value||'';
    const cursorPos=Number.isFinite(Number(menu.input_cursor_pos))?Number(menu.input_cursor_pos):-1;
    const valueHtml=(inputCells.length?inputCells:String(inputValue).split('')).map((cell,idx)=>{
      const ch=(typeof cell==='string')?cell:(cell.text||'');
      const cls=['memory-freq-cell'];
      if(idx===cursorPos || (cell && cell.cursor)) cls.push('cursor');
      return `<span class="${cls.join(' ')}">${ch===' '||ch===''?'&nbsp;':esc(ch)}</span>`;
    }).join('');
    const overlay=`<div class="memory-freq-overlay"><div class="memory-freq-title">${esc(menu.target_label||'FREQ')}</div><div class="memory-freq-value">${valueHtml||'&nbsp;'}</div><div class="memory-freq-keys"><div class="memory-freq-row row5">${r1}</div><div class="memory-freq-row row5">${r2}</div><div class="memory-freq-row delrow">${r3}</div></div></div>`;
    screen.innerHTML=`<div class="full-list">${htmlTitle}${htmlRows}</div>${overlay}`;
    return;
  }

  if(menu.type==='memory_edit'){
    menuLast=menu;
    const esc=(x)=>String(x).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    const rows=(menu.rows||[]).slice(0,4);
    const titleNum=(menu.parent_num===undefined || menu.parent_num===null)?'':String(menu.parent_num).padStart(2,'0');
    const htmlTitle=`<div class="full-row footer submenu-title"><span class="num">${titleNum}</span><span class="txt">${esc(menu.title||'')}</span><span class="arrow"></span></div>`;
    const htmlRows=rows.map((row,idx)=>{
      const cls=['memory-row','memory-edit-row'];
      if(idx===menu.selected_row || row.editing) cls.push('sel');
      return `<div class="${cls.join(' ')}" onclick="menuFullRowClick(${idx})"><span class="label">${esc(row.label||row.text||'')}</span><span class="value">${esc(row.value||'')}</span><span class="arrow">&rsaquo;</span></div>`;
    }).join('');
    screen.innerHTML=`<div class="full-list">${htmlTitle}${htmlRows}</div>`;
    return;
  }
  if(menu.type==='full'){
    menuLast=menu;
    const rows=(menu.rows||[]).slice(0,3);
    const htmlRows=rows.map((row,idx)=>{
      const rowNumRaw=(row.num===undefined || row.num===null)?null:Number(row.num);
      const num=(row.num===undefined || row.num===null)?'':String(row.num).padStart(2,'0');
      const cls=['full-row'];
      const inert=(menu.no_action_items||[]).includes(rowNumRaw);
      if(idx===menu.selected_row) cls.push('sel');
      if(row.editing) cls.push('edit');
      if(inert) cls.push('read-only');
      const arrow=inert?'':'&rsaquo;';
      return `<div class="${cls.join(' ')}" onclick="menuFullRowClick(${idx},${inert?'true':'false'})"><span class="num">${num}</span><span class="txt">${row.text||''}</span><span class="arrow">${arrow}</span></div>`;
    }).join('');
    const esc=(x)=>String(x).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    const valueText = menu.value || '';
    const rawValue = (menu.value_source==='unknown' && menu.raw_value) ? `<div class="choice-raw">${esc(menu.raw_value)}</div>` : '';
    const valueClasses=['choice-cell'];
    if(menu.value_selected || menu.editing) valueClasses.push('sel');
    if(menu.value_source==='unknown') valueClasses.push('unknown');
    screen.innerHTML=`<div class="full-list">${htmlRows}${valueText?`<div class="${valueClasses.join(' ')}" onclick="menuValueClick()">${esc(valueText)}${rawValue}</div>`:''}</div>`;
    return;
  }
  const cells=(menu.cells||[]).slice(0,9);
  while(cells.length<9) cells.push({index:cells.length,text:''});
  const htmlCells=cells.map((cell,idx)=>{
    const text=cell.text||'';
    const empty=!text;
    const cls=['quick-cell'];
    if(empty) cls.push('empty');
    if(idx===menu.selected_index && !menu.footer_selected) cls.push('sel');
    const click=(empty && !menu.assignment)?'':` onclick="quickCellClick(${idx})"`;
    return `<div class="${cls.join(' ')}"${click}>${text}</div>`;
  }).join('');
  const footerCls='quick-footer'+(menu.footer_selected?' sel':'');
  screen.innerHTML=`<div class="quick-grid">${htmlCells}</div><div class="${footerCls}" onclick="quickFooterClick()">${menu.footer||''}</div>`;
}
async function memoryRowClick(idx,slot){
  const menu=menuLast||{};
  const sel=Number(menu.selected_row||0);
  const diff=idx-sel;
  if(diff!==0){
    const cmd=diff>0?'br_right':'br_left';
    for(let i=0;i<Math.abs(diff);i++){
      await sendCmd(cmd,'5ms');
      await sleep(90);
    }
    await sleep(120);
  }
  // Physical command only: show the action overlay only when the
  // radio actually sends the F1 23 memory_select frame.
  await sendCmd('br_press','200ms');
}

async function quickCellClick(idx){
  if(!menuLast || menuLast.type!=='quick') return;
  const cells=Array.isArray(menuLast.cells)?menuLast.cells:[];
  const label=(cells[idx]&&cells[idx].text)||'';
  if(!label && !menuLast.assignment) return;
  const cur=Number.isFinite(Number(menuLast.selected_index))?Number(menuLast.selected_index):0;
  const target=Math.max(0,Math.min(8,Number(idx)));
  const diff=target-cur;
  if(diff!==0){
    const cmd=diff>0?'br_right':'br_left';
    for(let i=0;i<Math.abs(diff);i++){
      await sendCmd(cmd,'5ms');
      await sleep(70);
    }
    await sleep(110);
  }
  await sendCmd('br_press','200ms');
}
async function quickFooterClick(){
  if(!menuLast || menuLast.type!=='quick') return;
  await sendCmd('br_press','200ms');
}

async function menuFullRowClick(idx,inert=false){
  const menu=menuLast||{};
  if(menu.type==='menu1_keypad'){
    await menu1KeyClick(idx);
    return;
  }
  if(menu.type==='memory_tag_keypad'){
    await memoryTagKeyClick(idx);
    return;
  }
  if(menu.type==='memory_freq_keypad'){
    await memoryFreqKeyClick(idx);
    return;
  }
  if(menu.type==='memory_select'){
    const sel=Number(menu.selected_row||0);
    const diff=idx-sel;
    if(diff===0){
      await sendCmd('br_press','200ms');
    } else {
      // Action menu: no local selection and no burst commands.
      // A click away from the current selection equals a single physical step;
      // the highlight changes only when the updated radio frame arrives.
      const cmd=diff>0?'br_right':'br_left';
      await sendCmd(cmd,'5ms');
    }
    return;
  }
  const memoryMenu = (menu.type==='memory_list' || menu.type==='memory_edit');
  const sel=Number(menu.selected_row||0);
  const diff=idx-sel;
  if(diff!==0){
    const cmd=diff>0?'br_right':'br_left';
    for(let i=0;i<Math.abs(diff);i++){
      await sendCmd(cmd,'5ms');
      await sleep(90);
    }
    await sleep(120);
  }
  if(inert){ return; }
  await sendCmd('br_press', memoryMenu ? '200ms' : undefined);
}

async function menu1KeyClick(idx){
  if(!menuLast || menuLast.type!=='menu1_keypad') return;
  const cur=Number.isFinite(Number(menuLast.selected_key))?Number(menuLast.selected_key):0;
  const target=Math.max(0,Math.min(12,Number(idx)));
  const diff=target-cur;
  if(diff!==0){
    const cmd=diff>0?'br_right':'br_left';
    for(let i=0;i<Math.abs(diff);i++){
      await sendCmd(cmd,'5ms');
      await sleep(70);
    }
    await sleep(110);
  }
  await sendCmd('br_press','200ms');
}

async function memoryTagKeyClick(idx){
  if(!menuLast || menuLast.type!=='memory_tag_keypad') return;
  const cur=Number.isFinite(Number(menuLast.selected_key))?Number(menuLast.selected_key):0;
  const target=Math.max(0,Math.min(32,Number(idx)));
  const diff=target-cur;
  if(diff!==0){
    const cmd=diff>0?'br_right':'br_left';
    for(let i=0;i<Math.abs(diff);i++){
      await sendCmd(cmd,'5ms');
      await sleep(65);
    }
    await sleep(100);
  }
  await sendCmd('br_press','200ms');
}

async function memoryFreqKeyClick(idx){
  if(!menuLast || menuLast.type!=='memory_freq_keypad') return;
  const cur=Number.isFinite(Number(menuLast.selected_key))?Number(menuLast.selected_key):0;
  const target=Math.max(0,Math.min(10,Number(idx)));
  const diff=target-cur;
  if(diff!==0){
    const cmd=diff>0?'br_right':'br_left';
    for(let i=0;i<Math.abs(diff);i++){
      await sendCmd(cmd,'5ms');
      await sleep(70);
    }
    await sleep(110);
  }
  await sendCmd('br_press','200ms');
}

async function dtmfKeyClick(idx){
  if(!menuLast || menuLast.type!=='dtmf_edit') return;
  const cur=Number.isFinite(Number(menuLast.selected_key))?Number(menuLast.selected_key):(Number.isFinite(Number(menuLast.cursor_pos))?Number(menuLast.cursor_pos):0);
  let diff=idx-cur;
  if(diff!==0){
    // The radio exposes the DTMF edit keypad as 20 selectable positions.
    // Only send physical BR_LEFT/BR_RIGHT steps; never mutate the DTMF text locally.
    const cmd=diff>0?'br_right':'br_left';
    for(let i=0;i<Math.abs(diff);i++){
      await sendCmd(cmd,'5ms');
      await sleep(70);
    }
    await sleep(110);
  }
  await sendCmd('br_press');
}

function setupFButton(id,command,shortMs,longMs,thresholdMs){
  const b=document.getElementById(id);
  if(!b) return;
  let downAt=0;
  let down=false;
  const start=(e)=>{ e.preventDefault(); down=true; downAt=performance.now(); b.classList.add('holding'); try{b.setPointerCapture&&b.setPointerCapture(e.pointerId);}catch(_e){} };
  const stop=(e)=>{ if(!down) return; if(e) e.preventDefault(); down=false; b.classList.remove('holding'); const dt=performance.now()-downAt; sendCmd(command, (dt>=thresholdMs?longMs:shortMs)+'ms'); };
  b.addEventListener('pointerdown',start);
  b.addEventListener('pointerup',stop);
  b.addEventListener('pointercancel',stop);
  b.addEventListener('lostpointercapture',stop);
  b.addEventListener('contextmenu',e=>e.preventDefault());
}

setInterval(poll,100); poll();
</script>
</body>
</html>'''

DECODE_PANEL_HTML = '    <div class="rawbox"><h3>Decode</h3><div class="save-controls"><button id="saveStartBtn" onclick="saveAction(\'start\')">SAVE START</button><button id="saveStopBtn" onclick="saveAction(\'stop\')">SAVE STOP</button><span id="saveState" class="save-state">save off</span></div><pre id="raw">waiting...</pre></div>\n'

def build_web_html(decode_enabled: bool = False) -> str:
    html = WEB_HTML.replace("<body>", f"<body class=\"{'decode-enabled' if decode_enabled else 'decode-disabled'}\">", 1)
    if not decode_enabled:
        html = html.replace(DECODE_PANEL_HTML, "", 1)
    return html




def _json_response(handler: BaseHTTPRequestHandler, obj, status: int = 200) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def _html_response(handler: BaseHTTPRequestHandler, html: str) -> None:
    data = html.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


class AudioClient:
    def __init__(self, max_chunks: int = 12):
        self.max_chunks = max(2, int(max_chunks))
        self.cond = threading.Condition()
        self.queue = collections.deque()
        self.closed = False

    def push(self, chunk: bytes) -> None:
        with self.cond:
            if self.closed:
                return
            self.queue.append(chunk)
            # Low-latency policy: if the browser/network falls behind, drop old
            # audio instead of building seconds of delay.
            while len(self.queue) > self.max_chunks:
                self.queue.popleft()
            self.cond.notify()

    def get(self, timeout: float = 1.0) -> Optional[bytes]:
        with self.cond:
            if not self.queue and not self.closed:
                self.cond.wait(timeout)
            if self.queue:
                return self.queue.popleft()
            return None

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.queue.clear()
            self.cond.notify_all()


class AudioStreamer:
    """One shared low-latency ALSA capture stream, fanned out to browsers.

    ALSA is read continuously only while at least one browser is listening.
    Browser queues are deliberately tiny and old chunks are dropped if needed,
    because for radio RX audio being current is more important than preserving
    every sample with several seconds of delay.
    """

    def __init__(
        self,
        enabled: bool = True,
        device: str = "plughw:0,0",
        rate: int = 48000,
        channels: int = 1,
        chunk_ms: int = 10,
        buffer_time_us: int = 50000,
        period_time_us: int = 10000,
        alsa_card: Optional[str] = None,
        alsa_mic_volume: Optional[int] = None,
        alsa_agc_off: bool = False,
    ):
        self.enabled = enabled
        self.device = device
        self.rate = int(rate)
        self.channels = int(channels)
        self.chunk_ms = max(5, int(chunk_ms))
        self.buffer_time_us = int(buffer_time_us)
        self.period_time_us = int(period_time_us)
        self.alsa_card = str(alsa_card) if alsa_card not in (None, "") else self._card_from_device(device)
        self.alsa_mic_volume = None if alsa_mic_volume is None else max(0, min(100, int(alsa_mic_volume)))
        self.alsa_agc_off = bool(alsa_agc_off)
        self.last_alsa_message = ""
        self.lock = threading.Lock()
        self.clients: List[AudioClient] = []
        self.thread: Optional[threading.Thread] = None
        self.stop = threading.Event()
        self.proc: Optional[subprocess.Popen] = None
        self.last_error = ""
        self.bytes_sent = 0
        self.started_at: Optional[float] = None

    @staticmethod
    def _card_from_device(device: str) -> Optional[str]:
        m = re.search(r"(?:^|:)(?:plug)?hw:(\d+)(?:,|$)", device)
        if m:
            return m.group(1)
        return None

    @property
    def chunk_bytes(self) -> int:
        n = int(self.rate * self.channels * 2 * self.chunk_ms / 1000)
        # Keep reads aligned to complete S16 samples.
        return max(2, n - (n % 2))

    def status(self) -> dict:
        with self.lock:
            running = self.thread is not None and self.thread.is_alive()
            clients = len(self.clients)
            last_error = self.last_error
            last_alsa_message = self.last_alsa_message
            started_at = self.started_at
        return {
            "enabled": self.enabled,
            "device": self.device,
            "rate": self.rate,
            "channels": self.channels,
            "format": "S16_LE",
            "chunk_ms": self.chunk_ms,
            "buffer_time_us": self.buffer_time_us,
            "period_time_us": self.period_time_us,
            "alsa_card": self.alsa_card,
            "alsa_mic_volume": self.alsa_mic_volume,
            "alsa_agc_off": self.alsa_agc_off,
            "alsa_message": last_alsa_message,
            "clients": clients,
            "running": running,
            "age_s": None if started_at is None else max(0.0, time.time() - started_at),
            "last_error": last_error,
            "bytes_sent": self.bytes_sent,
        }

    def subscribe(self) -> AudioClient:
        if not self.enabled:
            raise RuntimeError("audio disabled")
        client = AudioClient(max_chunks=max(4, int(180 / self.chunk_ms)))
        with self.lock:
            self.clients.append(client)
            need_start = self.thread is None or not self.thread.is_alive()
            if need_start:
                self.stop.clear()
                self.thread = threading.Thread(target=self._reader_loop, daemon=True)
                self.thread.start()
        return client

    def unsubscribe(self, client: AudioClient) -> None:
        client.close()
        with self.lock:
            if client in self.clients:
                self.clients.remove(client)
            if not self.clients:
                self.stop.set()
                proc = self.proc
                if proc is not None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass

    def shutdown(self) -> None:
        self.stop.set()
        with self.lock:
            clients = list(self.clients)
            self.clients.clear()
            proc = self.proc
        for c in clients:
            c.close()
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        th = self.thread
        if th is not None:
            th.join(timeout=1.0)

    def _run_amixer(self, args: List[str]) -> str:
        if not self.alsa_card:
            return "alsa card unknown; use --rx-alsa-card 0"
        cmd = ["amixer", "-c", str(self.alsa_card)] + args
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1.5)
            lines = (r.stdout or r.stderr or "").strip().splitlines()
            summary = lines[-1] if lines else "ok"
            if r.returncode != 0:
                summary = f"amixer failed: {summary}"
            return summary[:180]
        except Exception as e:
            return f"amixer error: {e}"

    def _apply_alsa_capture_settings(self) -> None:
        msgs = []
        if self.alsa_mic_volume is not None:
            msgs.append("Mic=" + str(self.alsa_mic_volume) + "%: " + self._run_amixer(["sset", "Mic", f"{self.alsa_mic_volume}%"]))
        if self.alsa_agc_off:
            msgs.append("AGC off: " + self._run_amixer(["sset", "Auto Gain Control", "off"]))
        if msgs:
            with self.lock:
                self.last_alsa_message = " · ".join(msgs)[:260]

    def _command(self) -> List[str]:
        return [
            "arecord",
            "-q",
            "-D", self.device,
            "-f", "S16_LE",
            "-r", str(self.rate),
            "-c", str(self.channels),
            "-t", "raw",
            "--buffer-time", str(self.buffer_time_us),
            "--period-time", str(self.period_time_us),
            "-",
        ]

    def _reader_loop(self) -> None:
        while not self.stop.is_set():
            with self.lock:
                if not self.clients:
                    self.proc = None
                    self.started_at = None
                    return
            try:
                self._apply_alsa_capture_settings()
                proc = subprocess.Popen(
                    self._command(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
            except Exception as e:
                with self.lock:
                    self.last_error = f"arecord start failed: {e}"
                time.sleep(0.5)
                continue

            with self.lock:
                self.proc = proc
                self.started_at = time.time()
                self.last_error = ""

            try:
                assert proc.stdout is not None
                while not self.stop.is_set():
                    with self.lock:
                        if not self.clients:
                            break
                    chunk = proc.stdout.read(self.chunk_bytes)
                    if not chunk:
                        break
                    self._publish(chunk)
            except Exception as e:
                with self.lock:
                    self.last_error = f"audio read failed: {e}"
            finally:
                try:
                    proc.terminate()
                    proc.wait(timeout=0.5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                with self.lock:
                    if self.proc is proc:
                        self.proc = None
                    self.started_at = None

            # If clients are still connected and arecord died, retry briefly.
            with self.lock:
                still_needed = bool(self.clients)
            if still_needed and not self.stop.is_set():
                time.sleep(0.2)

    def _publish(self, chunk: bytes) -> None:
        with self.lock:
            clients = list(self.clients)
            self.bytes_sent += len(chunk) * len(clients)
        for client in clients:
            client.push(chunk)


class TxAudioSink:
    """Low-latency ALSA playback sink for browser microphone TX audio.

    Exactly one browser client is allowed to transmit at a time. Incoming chunks
    are S16_LE mono PCM from the browser. v41 adds server-side AGC/limiting. v42 sends playback as stereo by default
    and captures ALSA diagnostics so low/silent TX audio can be isolated.
    """

    def __init__(
        self,
        enabled: bool = True,
        device: str = "plughw:0,0",
        rate: int = 48000,
        channels: int = 1,
        buffer_time_us: int = 50000,
        period_time_us: int = 10000,
        ptt_lead_ms: int = 120,
        ptt_tail_ms: int = 80,
        processor_size: int = 1024,
        max_ws_buffer_bytes: int = 65536,
        output_gain: float = 0.35,
        playback_channels: int = 2,
        aplay_verbose: bool = False,
        agc_enabled: bool = False,
        agc_target: float = 0.45,
        agc_max_boost: float = 6.0,
        alsa_card: Optional[str] = None,
        alsa_speaker_volume: Optional[int] = None,
    ):
        self.enabled = bool(enabled)
        self.device = device
        self.rate = int(rate)
        self.channels = int(channels)
        self.buffer_time_us = int(buffer_time_us)
        self.period_time_us = int(period_time_us)
        self.ptt_lead_ms = max(0, int(ptt_lead_ms))
        self.ptt_tail_ms = max(0, int(ptt_tail_ms))
        self.processor_size = max(256, int(processor_size))
        self.max_ws_buffer_bytes = max(4096, int(max_ws_buffer_bytes))
        self.output_gain = max(0.0, float(output_gain))
        self.playback_channels = 2 if int(playback_channels) == 2 else 1
        self.aplay_verbose = bool(aplay_verbose)
        self.agc_enabled = bool(agc_enabled)
        self.agc_target = min(0.98, max(0.05, float(agc_target)))
        self.agc_max_boost = max(1.0, float(agc_max_boost))
        self.agc_current_boost = 1.0
        self.alsa_card = str(alsa_card) if alsa_card not in (None, "") else self._card_from_device(device)
        self.alsa_speaker_volume = None if alsa_speaker_volume is None else max(0, min(100, int(alsa_speaker_volume)))
        self.lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.active = False
        self.started_at: Optional[float] = None
        self.last_error = ""
        self.last_alsa_message = ""
        self.last_aplay_command = ""
        self.bytes_received = 0
        self.chunks_received = 0
        self.last_input_peak = 0.0
        self.last_output_peak = 0.0
        self.last_output_rms = 0.0
        self.last_total_gain = self.output_gain
        self.last_clipped_samples = 0
        self.total_clipped_samples = 0
        self.last_chunk_samples = 0
        self.last_meter_at = 0.0
        self.pending_chunk: Optional[Tuple[bytes, str]] = None
        self.pending_event = threading.Event()
        self.writer_stop = False
        self.writer_thread: Optional[threading.Thread] = None
        self.dropped_chunks = 0

    @staticmethod
    def _card_from_device(device: str) -> Optional[str]:
        # Accept common ALSA device strings such as hw:0,0 or plughw:0,0.
        m = re.search(r"(?:^|:)(?:plug)?hw:(\d+)(?:,|$)", device)
        if m:
            return m.group(1)
        m = re.search(r"(?:^|:)hw:(\d+)(?:,|$)", device)
        if m:
            return m.group(1)
        return None

    def status(self) -> dict:
        with self.lock:
            proc = self.proc
            active = self.active
            started_at = self.started_at
            last_error = self.last_error
            bytes_received = self.bytes_received
            chunks_received = self.chunks_received
            last_input_peak = self.last_input_peak
            last_output_peak = self.last_output_peak
            last_output_rms = self.last_output_rms
            last_total_gain = self.last_total_gain
            last_clipped_samples = self.last_clipped_samples
            total_clipped_samples = self.total_clipped_samples
            last_chunk_samples = self.last_chunk_samples
            last_alsa_message = self.last_alsa_message
            last_aplay_command = self.last_aplay_command
            agc_current_boost = self.agc_current_boost
            last_meter_at = self.last_meter_at
            dropped_chunks = self.dropped_chunks
        return {
            "enabled": self.enabled,
            "device": self.device,
            "rate": self.rate,
            "channels": self.channels,
            "playback_channels": self.playback_channels,
            "format": "S16_LE",
            "buffer_time_us": self.buffer_time_us,
            "period_time_us": self.period_time_us,
            "ptt_lead_ms": self.ptt_lead_ms,
            "ptt_tail_ms": self.ptt_tail_ms,
            "processor_size": self.processor_size,
            "max_ws_buffer_bytes": self.max_ws_buffer_bytes,
            "output_gain": self.output_gain,
            "agc_enabled": self.agc_enabled,
            "agc_target": self.agc_target,
            "agc_max_boost": self.agc_max_boost,
            "agc_current_boost": agc_current_boost,
            "alsa_card": self.alsa_card,
            "alsa_speaker_volume": self.alsa_speaker_volume,
            "alsa_message": last_alsa_message,
            "aplay_command": last_aplay_command,
            "active": active,
            "running": proc is not None and proc.poll() is None,
            "age_s": None if started_at is None else max(0.0, time.time() - started_at),
            "last_error": last_error,
            "bytes_received": bytes_received,
            "chunks_received": chunks_received,
            "dropped_chunks": dropped_chunks,
            "input_peak": last_input_peak,
            "output_peak": last_output_peak,
            "output_rms": last_output_rms,
            "total_gain": last_total_gain,
            "clipped_samples": last_clipped_samples,
            "total_clipped_samples": total_clipped_samples,
            "last_chunk_samples": last_chunk_samples,
            "meter_age_s": None if last_meter_at <= 0 else max(0.0, time.time() - last_meter_at),
        }

    def _command(self) -> List[str]:
        cmd = [
            "aplay",
            "-D", self.device,
            "-f", "S16_LE",
            "-r", str(self.rate),
            "-c", str(self.playback_channels),
            "-t", "raw",
            "--buffer-time", str(self.buffer_time_us),
            "--period-time", str(self.period_time_us),
        ]
        if self.aplay_verbose:
            cmd.append("-v")
        cmd.append("-")
        return cmd

    def _apply_alsa_speaker_volume(self) -> None:
        if self.alsa_speaker_volume is None:
            return
        if not self.alsa_card:
            with self.lock:
                self.last_alsa_message = "alsa card unknown; use --tx-alsa-card 0"
            return
        cmd = ["amixer", "-c", str(self.alsa_card), "sset", "Speaker", f"{self.alsa_speaker_volume}%", "unmute"]
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1.5)
            msg = (r.stdout or r.stderr or "").strip().splitlines()
            summary = msg[-1] if msg else "ok"
            if r.returncode != 0:
                summary = f"amixer failed: {summary}"
            else:
                summary = f"Speaker={self.alsa_speaker_volume}% on card {self.alsa_card}"
            with self.lock:
                self.last_alsa_message = summary[:220]
        except Exception as e:
            with self.lock:
                self.last_alsa_message = f"amixer error: {e}"

    def _stderr_reader(self, proc: subprocess.Popen) -> None:
        stream = proc.stderr
        if stream is None:
            return
        try:
            for raw in iter(stream.readline, b""):
                if not raw:
                    break
                msg = raw.decode("utf-8", errors="replace").strip()
                if not msg:
                    continue
                with self.lock:
                    self.last_alsa_message = msg[:220]
        except Exception as e:
            with self.lock:
                self.last_alsa_message = f"aplay stderr read error: {e}"

    def begin(self) -> None:
        if not self.enabled:
            raise RuntimeError("TX audio disabled")
        with self.lock:
            if self.active:
                raise RuntimeError("TX audio already in use by another browser")
        self._apply_alsa_speaker_volume()
        with self.lock:
            try:
                cmd = self._command()
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
                self.last_aplay_command = " ".join(shlex.quote(x) for x in cmd)
                threading.Thread(target=self._stderr_reader, args=(proc,), daemon=True).start()
            except Exception as e:
                self.last_error = f"aplay start failed: {e}"
                raise RuntimeError(self.last_error)
            self.proc = proc
            self.active = True
            self.started_at = time.time()
            self.last_error = ""
            self.last_alsa_message = self.last_alsa_message or "aplay started"
            self.bytes_received = 0
            self.chunks_received = 0
            self.last_input_peak = 0.0
            self.last_output_peak = 0.0
            self.last_output_rms = 0.0
            self.last_total_gain = self.output_gain
            self.last_clipped_samples = 0
            self.total_clipped_samples = 0
            self.last_chunk_samples = 0
            self.agc_current_boost = 1.0
            self.pending_chunk = None
            self.dropped_chunks = 0
            self.writer_stop = False
            self.pending_event.clear()
            self.writer_thread = threading.Thread(target=self._writer_loop, args=(proc,), daemon=True)
            self.writer_thread.start()

    def _apply_output_processing(self, data: bytes, channel: str = "both") -> bytes:
        if not data:
            return data
        if len(data) & 1:
            data = data[:-1]
        if not data:
            return data

        n = len(data) // 2

        # This path runs for every TX chunk. Keep it in C-level stdlib audioop
        # operations so Raspberry CPU does not become the bottleneck and create
        # multi-second backlog / late PTT release.
        input_peak = min(1.0, audioop.max(data, 2) / 32768.0)

        # AGC: if the browser mic is very low, boost it up to target peak.
        # Keep the boost bounded and smoothed to avoid pumping too violently.
        agc_boost = self.agc_current_boost
        if self.agc_enabled and input_peak > 0.0003:
            desired = self.agc_target / max(input_peak, 0.0003)
            desired = max(1.0, min(self.agc_max_boost, desired))
            # Reduce boost quickly when peaks get high; increase more gently.
            alpha = 0.35 if desired < agc_boost else 0.10
            agc_boost = agc_boost + (desired - agc_boost) * alpha
            agc_boost = max(1.0, min(self.agc_max_boost, agc_boost))
        elif not self.agc_enabled:
            agc_boost = 1.0

        total_gain = self.output_gain * agc_boost
        if total_gain <= 0.0:
            mono_out = b"\x00" * len(data)
            with self.lock:
                self.agc_current_boost = agc_boost
                self.last_input_peak = input_peak
                self.last_output_peak = 0.0
                self.last_output_rms = 0.0
                self.last_total_gain = total_gain
                self.last_clipped_samples = 0
                self.last_chunk_samples = n
                self.last_meter_at = time.time()
        elif abs(total_gain - 1.0) < 1e-6:
            mono_out = data
        else:
            mono_out = audioop.mul(data, 2, total_gain)

        output_peak = min(1.0, audioop.max(mono_out, 2) / 32768.0) if mono_out else 0.0
        output_rms = min(1.0, audioop.rms(mono_out, 2) / 32768.0) if mono_out else 0.0
        clipped = 0
        with self.lock:
            self.agc_current_boost = agc_boost
            self.last_input_peak = input_peak
            self.last_output_peak = output_peak
            self.last_output_rms = output_rms
            self.last_total_gain = total_gain
            self.last_clipped_samples = clipped
            self.total_clipped_samples += clipped
            self.last_chunk_samples = n
            self.last_meter_at = time.time()
        # Convert the mono browser stream to the real ALSA playback layout.
        # CM108-style USB dongles usually expose stereo playback; duplicating
        # mono to L+R avoids a silent TX if the radio cable is wired to the
        # channel that a mono stream was not feeding.
        channel = (channel or "both").lower()
        if self.playback_channels == 1:
            return mono_out
        if channel == "left":
            return audioop.tostereo(mono_out, 2, 1.0, 0.0)
        if channel == "right":
            return audioop.tostereo(mono_out, 2, 0.0, 1.0)
        return audioop.tostereo(mono_out, 2, 1.0, 1.0)

    def _writer_loop(self, proc: subprocess.Popen) -> None:
        while True:
            self.pending_event.wait(0.25)
            self.pending_event.clear()
            while True:
                with self.lock:
                    if self.writer_stop:
                        return
                    item = self.pending_chunk
                    self.pending_chunk = None
                if item is None:
                    break
                data, channel = item
                try:
                    self._write_processed(proc, data, channel=channel)
                except Exception as e:
                    with self.lock:
                        extra = f": {self.last_alsa_message}" if self.last_alsa_message else ""
                        self.last_error = f"aplay write failed: {e}{extra}"
                    return

    def _write_processed(self, proc: subprocess.Popen, data: bytes, channel: str = "both") -> None:
        out = self._apply_output_processing(data, channel=channel)
        if not out:
            return
        with self.lock:
            if not self.active or self.proc is not proc or proc.stdin is None:
                raise RuntimeError("TX audio not active")
            if proc.poll() is not None:
                extra = f": {self.last_alsa_message}" if self.last_alsa_message else ""
                self.last_error = f"aplay exited with code {proc.returncode}{extra}"
                raise RuntimeError(self.last_error)
            stdin = proc.stdin
        stdin.write(out)

    def write(self, data: bytes, channel: str = "both") -> None:
        if not data:
            return
        # Keep writes aligned to complete S16 samples.
        if len(data) & 1:
            data = data[:-1]
        if not data:
            return
        with self.lock:
            proc = self.proc
            if not self.active or proc is None:
                raise RuntimeError("TX audio not active")
            if proc.poll() is not None or proc.stdin is None:
                extra = f": {self.last_alsa_message}" if self.last_alsa_message else ""
                self.last_error = f"aplay exited with code {proc.returncode}{extra}"
                raise RuntimeError(self.last_error)
            if self.pending_chunk is not None:
                self.dropped_chunks += 1
            self.pending_chunk = (data, channel)
            self.bytes_received += len(data)
            self.chunks_received += 1
        self.pending_event.set()

    def write_tone(self, duration_ms: int = 1000, freq_hz: float = 1000.0, level: float = 0.85, channel: str = "both") -> None:
        """Write a local test tone through the same ALSA path, without browser mic."""
        duration_ms = max(100, min(5000, int(duration_ms)))
        freq_hz = max(100.0, min(3000.0, float(freq_hz)))
        level = max(0.01, min(1.0, float(level)))
        chunk_ms = 20
        frames_per_chunk = max(1, int(self.rate * chunk_ms / 1000))
        total_frames = int(self.rate * duration_ms / 1000)
        phase = 0.0
        step = 2.0 * math.pi * freq_hz / self.rate
        written = 0
        while written < total_frames:
            n = min(frames_per_chunk, total_frames - written)
            samples = array.array("h")
            for _ in range(n):
                v = int(math.sin(phase) * level * 32767)
                samples.append(v)
                phase += step
                if phase > 2.0 * math.pi:
                    phase -= 2.0 * math.pi
            if sys.byteorder != "little":
                samples.byteswap()
            # Local tone is already at requested level; bypass AGC but keep output_gain.
            raw = samples.tobytes()
            old_agc = self.agc_enabled
            try:
                self.agc_enabled = False
                self.write(raw, channel=channel)
            finally:
                self.agc_enabled = old_agc
            written += n
            time.sleep(chunk_ms / 1000.0 * 0.6)

    def end(self, tail_ms: Optional[int] = None) -> None:
        if tail_ms is None:
            tail_ms = self.ptt_tail_ms
        with self.lock:
            proc = self.proc
            self.proc = None
            self.active = False
            self.started_at = None
            self.writer_stop = True
            self.pending_chunk = None
            writer = self.writer_thread
            self.writer_thread = None
        self.pending_event.set()
        if writer is not None and writer is not threading.current_thread():
            try:
                writer.join(timeout=0.05)
            except Exception:
                pass
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                if tail_ms and tail_ms > 0:
                    silence_len = int(self.rate * self.playback_channels * 2 * tail_ms / 1000)
                    silence_len -= silence_len % 2
                    if silence_len > 0:
                        try:
                            proc.stdin.write(b"\x00" * silence_len)
                            proc.stdin.flush()
                            time.sleep(min(0.25, tail_ms / 1000.0))
                        except Exception:
                            pass
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            try:
                proc.terminate()
                proc.wait(timeout=0.4)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        finally:
            pass

    def shutdown(self) -> None:
        self.end(tail_ms=0)


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_read_exact(handler: BaseHTTPRequestHandler, n: int) -> Optional[bytes]:
    data = bytearray()
    while len(data) < n:
        chunk = handler.rfile.read(n - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


def _ws_send_frame(handler: BaseHTTPRequestHandler, opcode: int, payload: bytes = b"") -> None:
    first = 0x80 | (opcode & 0x0F)
    n = len(payload)
    if n < 126:
        header = bytes([first, n])
    elif n <= 0xFFFF:
        header = bytes([first, 126]) + n.to_bytes(2, "big")
    else:
        header = bytes([first, 127]) + n.to_bytes(8, "big")
    handler.wfile.write(header + payload)
    handler.wfile.flush()


def _ws_read_frame(handler: BaseHTTPRequestHandler) -> Optional[Tuple[int, bytes]]:
    head = _ws_read_exact(handler, 2)
    if head is None:
        return None
    b1, b2 = head
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        ext = _ws_read_exact(handler, 2)
        if ext is None:
            return None
        length = int.from_bytes(ext, "big")
    elif length == 127:
        ext = _ws_read_exact(handler, 8)
        if ext is None:
            return None
        length = int.from_bytes(ext, "big")
    if length > 1024 * 1024:
        raise RuntimeError("websocket frame too large")
    mask = b""
    if masked:
        mask = _ws_read_exact(handler, 4)
        if mask is None:
            return None
    payload = _ws_read_exact(handler, length) if length else b""
    if payload is None:
        return None
    if masked and payload:
        payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
    return opcode, payload


def _audio_tx_ws_response(handler: BaseHTTPRequestHandler) -> None:
    key = handler.headers.get("Sec-WebSocket-Key", "")
    if handler.headers.get("Upgrade", "").lower() != "websocket" or not key:
        handler.send_error(400, "websocket upgrade required")
        return

    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()

    started = False
    try:
        handler.connection.settimeout(3.0)
        handler.ctx.start_tx_audio_session()
        started = True
        _ws_send_frame(handler, 1, b"TX audio ready")
        while True:
            frame = _ws_read_frame(handler)
            if frame is None:
                break
            opcode, payload = frame
            if opcode == 0x8:  # close
                break
            if opcode == 0x9:  # ping
                _ws_send_frame(handler, 0xA, payload[:125])
                continue
            if opcode == 0x2:  # binary PCM
                handler.ctx.write_tx_audio(payload)
                continue
            if opcode == 0x1 and payload == b"stop":
                break
    except Exception:
        # Keep the HTTP server quiet during normal browser disconnects.
        pass
    finally:
        if started:
            handler.ctx.stop_tx_audio_session()
        try:
            _ws_send_frame(handler, 0x8, b"")
        except Exception:
            pass


def _state_ws_response(handler: BaseHTTPRequestHandler) -> None:
    key = handler.headers.get("Sec-WebSocket-Key", "")
    if handler.headers.get("Upgrade", "").lower() != "websocket" or not key:
        handler.send_error(400, "websocket upgrade required")
        return

    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()

    interval_s = 0.12
    heartbeat_s = 15.0
    last_state_payload = b""
    last_sent_at = 0.0

    hello = {
        "type": "hello",
        "version": 1,
        "transport": "state.ws",
        "interval_ms": int(interval_s * 1000),
        "heartbeat_s": heartbeat_s,
    }

    try:
        handler.connection.settimeout(None)
        _ws_send_frame(handler, 0x1, json.dumps(hello, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

        while True:
            now = time.time()
            readable, _, _ = select.select([handler.connection], [], [], interval_s)
            if readable:
                frame = _ws_read_frame(handler)
            else:
                frame = None

            if frame is not None:
                opcode, payload = frame
                if opcode == 0x8:  # close
                    break
                if opcode == 0x9:  # ping
                    _ws_send_frame(handler, 0xA, payload[:125])
                    continue
                if opcode == 0x1 and payload.strip().lower() in (b"stop", b"close", b"quit"):
                    break

            state_obj = handler.ctx.state()
            payload = json.dumps(
                {"type": "state", "state": state_obj},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")

            if payload != last_state_payload or (now - last_sent_at) >= heartbeat_s:
                _ws_send_frame(handler, 0x1, payload)
                last_state_payload = payload
                last_sent_at = now
    except Exception:
        pass
    finally:
        try:
            _ws_send_frame(handler, 0x8, b"")
        except Exception:
            pass

def _audio_pcm_response(handler: BaseHTTPRequestHandler) -> None:
    audio = getattr(handler.ctx, "audio", None)
    if audio is None or not audio.enabled:
        handler.send_error(404, "audio disabled")
        return
    try:
        client = audio.subscribe()
    except Exception as e:
        handler.send_error(503, str(e))
        return

    handler.send_response(200)
    handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Cache-Control", "no-store, no-transform")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Audio-Format", "S16_LE")
    handler.send_header("X-Audio-Rate", str(audio.rate))
    handler.send_header("X-Audio-Channels", str(audio.channels))
    handler.send_header("Connection", "close")
    handler.end_headers()

    try:
        while True:
            chunk = client.get(timeout=1.0)
            if chunk is None:
                if client.closed:
                    break
                continue
            handler.wfile.write(chunk)
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, TimeoutError):
        pass
    except Exception:
        pass
    finally:
        audio.unsubscribe(client)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    n = int(handler.headers.get("Content-Length", "0") or "0")
    if n <= 0:
        return {}
    raw = handler.rfile.read(n)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _split_mode_shift(mode: str) -> Tuple[str, str]:
    if mode.endswith("+/-?"):
        return mode[:-4], "+/-"
    if mode.endswith("+"):
        return mode[:-1], "+"
    if mode.endswith("-"):
        return mode[:-1], "-"
    return mode, ""


def _clean_raw_byte(b: int) -> int:
    return 0 if b in (0x00, BLANK_DIGIT) else b


def _lower_for_web(frame: bytes, side: str) -> dict:
    if side == SIDE_LEFT:
        label_b, value_b, vol_b = frame[10], frame[13], frame[17]
    else:
        # v28: right-side lower value is +0014. +0015 is the global
        # TX/RX meter and must not drive right SQL/VOL.
        label_b, value_b, vol_b = frame[11], frame[14], frame[18]
    label = decode_lower_label_byte(label_b)
    value_raw = _clean_raw_byte(value_b)
    vol_raw = _clean_raw_byte(vol_b)

    # Web v29: SQL/VOL are explicit scales. For SQL use the side value
    # byte first on both sides (+0013 left, +0014 right), because +0018 can
    # be zero while the real right SQL value changes.
    if label == "VOL":
        bar_raw = vol_raw or value_raw
        vol_out = bar_raw
        sql_out = 0
        bar_kind = "vol"
    elif label == "SQL":
        bar_raw = value_raw or vol_raw
        vol_out = 0
        sql_out = bar_raw
        bar_kind = "sql"
    else:
        bar_raw = 0
        vol_out = vol_raw
        sql_out = 0
        bar_kind = "none"

    return {
        "label": label,
        "label_raw": label_b,
        "value_raw": value_raw,
        "sql_raw": sql_out,
        "vol_raw": vol_out,
        "bar_raw": bar_raw,
        "bar_kind": bar_kind,
        "bar_max": 127 if bar_kind == "vol" else 32 if bar_kind == "sql" else 0,
        "value_candidate_raw": value_raw,
        "side_value_raw": vol_raw,
    }


def _activity_for_web(frame: bytes, main: str) -> dict:
    """Decode TX/RX activity for the web UI.

    Mapped from user diffs:
      RX left:  +0004 = 0x04, S-meter left  = +0015
      RX right: +0005 = 0x04, S-meter right = +0016

    Earlier global flags (+0193 and repeated block flags) indicate that some RX
    activity exists, but they do not identify the side, so they are kept only as
    raw/debug fields and no longer light both LEDs by themselves.
    """
    left_activity = frame[4]
    right_activity = frame[5]
    left_meter = _clean_raw_byte(frame[15])
    right_meter = _clean_raw_byte(frame[16])
    tx_flag = frame[192]
    rx_flag = frame[193]
    repeated_rx_flags = [frame[RX_BLOCK_LEN * k + 2] for k in range(1, 5)]

    rx_left = bool(left_activity & 0x04)
    rx_right = bool(right_activity & 0x04)

    # TX observed on left uses +0004=0x02; for right, keep the old main-side
    # fallback via +0192=0x11 because the side-specific TX byte has not been
    # mapped as clearly as RX yet.
    tx_left = bool(left_activity & 0x02)
    tx_right = bool(right_activity & 0x02)
    if tx_flag == 0x11 and not (tx_left or tx_right):
        tx_right = main == "RIGHT"
        tx_left = not tx_right

    if tx_left or tx_right:
        status = "TX/PTT"
    elif rx_left or rx_right:
        status = "RX/audio"
    elif rx_flag != 0x00 or any(b != 0x00 for b in repeated_rx_flags):
        status = "RX/audio-global"
    else:
        status = "idle"

    rx_sides = (["LEFT"] if rx_left else []) + (["RIGHT"] if rx_right else [])
    tx_sides = (["LEFT"] if tx_left else []) + (["RIGHT"] if tx_right else [])
    return {
        "status": status,
        "activity_raw": left_activity,
        "left_activity_raw": left_activity,
        "right_activity_raw": right_activity,
        "meter_raw": left_meter if (rx_left or tx_left) else right_meter if (rx_right or tx_right) else 0,
        "left_meter_raw": left_meter,
        "right_meter_raw": right_meter,
        "tx_flag": tx_flag,
        "rx_flag": rx_flag,
        "rx_rep": repeated_rx_flags,
        "rx_left": rx_left,
        "rx_right": rx_right,
        "rx_ambiguous": False,
        "tx_left": tx_left,
        "tx_right": tx_right,
        "rx_sides": rx_sides,
        "tx_sides": tx_sides,
        "side": tx_sides[0] if tx_sides else rx_sides[0] if len(rx_sides) == 1 else None,
    }


def _side_for_web(frame: bytes, side: str) -> dict:
    d = decode_side(frame, side)
    mode_text, shift = _split_mode_shift(d.mode)
    if side == SIDE_LEFT:
        activity_b = frame[4]
        meter_b = frame[15]
    else:
        activity_b = frame[5]
        meter_b = frame[16]
    rx_active = bool(activity_b & 0x04)
    tx_active = bool(activity_b & 0x02)
    return {
        "side": side,
        "is_main": d.is_main,
        "source": d.source,
        "source_code": d.source_code,
        "mem_group": d.mem_group,
        "mem_no": d.mem_no,
        "name": d.name.strip(),
        "freq": d.freq,
        "mode": mode_text,
        "mode_raw": d.mode,
        "shift": shift,
        "tone": d.tone,
        "rx_active": rx_active,
        "tx_active": tx_active,
        "activity_raw": activity_b,
        "s_meter_raw": _clean_raw_byte(meter_b),
        "lower": _lower_for_web(frame, side),
    }


def _ascii_menu_field(frame: bytes, start: int, max_len: int = 20) -> str:
    chars: List[str] = []
    for b in frame[start:start + max_len]:
        if b in (0x00, 0x64):
            break
        if 32 <= b <= 126:
            chars.append(chr(b))
        else:
            break
    return "".join(chars).strip()


def _full_menu_number(b0: int, b1: int) -> Optional[int]:
    if b0 == 0x64 and 0 <= b1 <= 9:
        return int(b1)
    if 0 <= b0 <= 9 and 0 <= b1 <= 9:
        return int(b0) * 10 + int(b1)
    return None


def _quick_cell_ascii(raw: bytes) -> str:
    chars: List[str] = []
    for b in bytes(raw):
        b = int(b)
        if b in (0x00, 0x64):
            # 0x00 terminates most cell labels; 0x64 is filler/padding here.
            break
        if 32 <= b <= 126:
            chars.append(chr(b))
        else:
            break
    return " ".join("".join(chars).split()).strip()


def _quick_cell_clean(text: str) -> str:
    t = (text or "").strip()
    u = t.upper()
    # The radio sometimes packs a small two-letter suffix into the same byte
    # cell (IM/LC/ST/SC).  Keep learned fixed labels clean, but preserve custom
    # assigned menu labels such as CNTRST exactly as the radio sends them.
    for label in ("RPT SFT", "RPT FRQ", "SQL TYP", "CLONETX", "CLONERX", "M->V", "STEP", "TONE"):
        if u.startswith(label):
            return label
    return t


def _quick_menu_cells_from_frame(frame: Optional[bytes]) -> List[dict]:
    """Read the 3x3 quick menu labels from the actual F3 20 payload.

    Learned slot starts from save_20260507_105417_menu.zip:
      61, 71, 81
      91, 101, 111
      121, 131, 141

    This makes the quick menu follow radio-side customization instead of using
    a rigid web table.
    """
    starts = [61, 71, 81, 91, 101, 111, 121, 131, 141]
    out: List[dict] = []
    for i, start in enumerate(starts):
        text = ""
        if frame is not None and start < len(frame):
            text = _quick_cell_clean(_quick_cell_ascii(frame[start:start + 9]))
        if not text and 0 <= i < len(QUICK_MENU_LABELS):
            # Fallback only if the frame lacks a cell.  For real empty cells,
            # the frame gives spaces/0x64 and the helper returns empty.
            text = ""
        out.append({"index": i, "text": text})
    return out


def _quick_assignment_text(menu_frame: Optional[bytes]) -> Optional[str]:
    text = _decode_lcd_declared_value_text(menu_frame, start=28, length_off=27, max_len=30) if menu_frame else None
    if text and text.strip().upper() == "WRITE TO FUNCTION MENU":
        return "Write to FUNCTION MENU"
    return None


def _quick_label_norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(text or "").upper())


def _quick_label_devowel(text: str) -> str:
    n = _quick_label_norm(text)
    if not n:
        return ""
    # Keep first char, then drop vowels.  This matches radio abbreviations such
    # as CONTRAST -> CNTRST without making tiny labels ambiguous.
    return n[:1] + re.sub(r"[AEIOU]", "", n[1:])


def _quick_label_acronym(text: str) -> str:
    words = re.findall(r"[A-Z0-9]+", str(text or "").upper())
    return "".join(w[:1] for w in words if w)


def _quick_dynamic_label_to_setup_num(label: str) -> Optional[int]:
    """Map a dynamically assigned quick-menu cell back to the setup item.

    The radio shortens labels in the 3x3 quick menu, e.g. LCD CONTRAST is sent
    as CNTRST.  Do not special-case only one function: try learned aliases, exact
    setup-menu names, devowelled forms and last-word abbreviations.
    """
    n = _quick_label_norm(label)
    if not n:
        return None

    aliases = {
        # DISPLAY
        "KEYPAD": 1,
        "DIMMER": 2, "LCDDIM": 2, "LCDDIMMER": 2,
        "CNTRST": 3, "CONTRST": 3, "CONTRAST": 3, "LCDCNTRST": 3, "LCDCONTRAST": 3,
        "BANDSCOPE": 4, "SCOPE": 4,
        "SMETER": 5, "SMETERSYMBOL": 5, "SMTR": 5,
        "COLOR": 6, "BKCOLOR": 6, "BACKLIGHTCOLOR": 6,
        # TX/RX/CONFIG common assignable items
        "TXPWR": 7, "TXPOWER": 7, "POWER": 7,
        "MICGAIN": 8,
        "VOX": 9,
        "AUTODIAL": 10, "ADIAL": 10,
        "TOT": 11,
        "FMBANDWIDTH": 12, "BANDWIDTH": 12, "WIDTH": 12,
        "RXMODE": 13,
        "SUBBAND": 14,
        "HOMECH": 15,
        "MEMORYLIST": 16, "MEMLIST": 16,
        "MEMORYLISTMODE": 17, "MEMMODE": 17,
        "PMG": 18,
        "BEEP": 19,
        "BANDSKIP": 20,
        "RPTARS": 21,
        "RPTSHIFT": 22, "RPTSFT": 22,
        "RPTSHIFTFREQ": 23, "RPTFRQ": 23,
        "RPTREVERSE": 24, "RPTREV": 24,
        "MICPROGRAMKEY": 25, "MICPKEY": 25,
        "STEP": 26,
        "CLOCKTYPE": 27, "CLOCK": 27,
        "APO": 28,
        "REARSPOUT": 29, "REARSP": 29,
        "FRONTSPMUTE": 30, "FRMUTE": 30,
        "DTMF": 31,
        "DTMFMEMORY": 32, "DTMFMEM": 32,
        "SQLTYPE": 33, "SQLTYP": 33,
        "TONESQLFREQ": 34, "TONE": 34,
        "SQLEXPANSION": 35, "SQLEXP": 35,
        "PAGERCODE": 36, "PAGER": 36,
        "PRFREQUENCY": 37, "PRFREQ": 37,
        "BELLRINGER": 38, "BELL": 38,
        "WXALERT": 39,
        "SCAN": 40,
        "DUALRECEIVEMODE": 41, "DUALRXMODE": 41,
        "DUALRXINTERVAL": 42, "DUALRXINT": 42,
        "PRIORITYREVERT": 43, "PRIREVERT": 43,
        "SCANRESUME": 44, "RESUME": 44,
        "DATABAND": 45,
        "DATASPEED": 46,
    }
    if n in aliases:
        return aliases[n]

    # Generic matching against setup labels.
    best: Optional[int] = None
    for num, name in SETUP_MENU_ITEMS.items():
        full = _quick_label_norm(name)
        words = re.findall(r"[A-Z0-9]+", str(name).upper())
        last = words[-1] if words else full
        variants = {
            full,
            _quick_label_devowel(full),
            _quick_label_acronym(name),
            _quick_label_norm(last),
            _quick_label_devowel(last),
        }
        # Also match without leading LCD/RPT/SQL/TONE prefixes when the radio
        # abbreviates only the distinctive word.
        if len(words) > 1:
            tail = " ".join(words[1:])
            variants.add(_quick_label_norm(tail))
            variants.add(_quick_label_devowel(tail))

        if n in variants:
            return int(num)

        if len(n) >= 4:
            for v in variants:
                if v and (v.startswith(n) or n.startswith(v) or n in v):
                    # Avoid returning very broad accidental matches immediately;
                    # keep first sensible match.
                    if best is None:
                        best = int(num)
    return best


def _quick_dynamic_footer(label: str, menu_frame: Optional[bytes], data_frame: Optional[bytes]) -> str:
    num = _quick_dynamic_label_to_setup_num(label)
    if num is not None:
        value = _setup_value_from_menu_frame(num, menu_frame)
        if value is None:
            value = _setup_value_from_radio_guess(num, data_frame)
        if value is not None:
            return value
        if num in SETUP_ENTER_ITEMS:
            return "›"

    # Last-resort generic value read: useful for newly assigned items that send
    # a simple compact value even before we have a dedicated alias.
    if menu_frame is not None:
        text = _decode_lcd_declared_value_text(menu_frame, start=28, length_off=27, max_len=30)
        if text and text.strip().upper() != "WRITE TO FUNCTION MENU":
            return text
        text2 = _decode_lcd_choice_text(menu_frame, 28, 18)
        if text2:
            return text2
    return ""


def _quick_menu_footer(selected_label: str, menu_frame: Optional[bytes], data_frame: Optional[bytes]) -> str:
    """Decode the quick-menu footer with the same values as the main menu."""
    assignment_text = _quick_assignment_text(menu_frame)
    if assignment_text:
        return assignment_text
    if not selected_label:
        return ""
    label = selected_label.strip().upper()

    if label == "RPT SFT":
        # The quick frame stores this as compact text in +0028..; in some
        # captures the sign glyph before RPT is not decoded by the generic LCD
        # table, so fall back to the normal display shift when available.
        if data_frame:
            main = "LEFT" if data_frame[3] == 0x02 else "RIGHT" if data_frame[3] == 0x01 else "LEFT"
            side = decode_side(data_frame, SIDE_LEFT if main == "LEFT" else SIDE_RIGHT)
            _mode_text, shift = _split_mode_shift(side.mode)
            if shift == '+':
                return '+RPT'
            if shift == '-':
                return '-RPT'
            return 'SIMPLEX'
        text = _decode_lcd_declared_value_text(menu_frame, start=28, length_off=27, max_len=18) if menu_frame else None
        if text:
            norm = text.upper().replace(" ", "")
            if norm.endswith("RPT"):
                # Unknown sign glyph in the first cell; use +RPT as observed in
                # the current quick-menu capture.
                return "+RPT"
            return text
        return ""

    if label == "RPT FRQ":
        return _decode_rpt_shift_freq_from_menu23_frame(menu_frame) or ""

    if label == "STEP":
        return _decode_step_from_menu26_frame(menu_frame) or ""

    if label == "SQL TYP":
        text = _decode_lcd_declared_value_text(menu_frame, start=28, length_off=27, max_len=18) if menu_frame else None
        if text:
            # Match the main menu naming.
            known = {
                "OFF": "OFF",
                "TONE ENC": "TONE ENC",
                "TONE SQL": "TONE SQL",
                "REV TONE": "REV TONE",
                "DCS": "DCS",
                "PR FREQ": "PR FREQ",
                "PAGER": "PAGER",
            }
            return known.get(text.upper(), text)
        if data_frame:
            main = "LEFT" if data_frame[3] == 0x02 else "RIGHT" if data_frame[3] == 0x01 else "LEFT"
            side = decode_side(data_frame, SIDE_LEFT if main == "LEFT" else SIDE_RIGHT)
            return (side.tone or "OFF").strip()
        return ""

    if label == "TONE":
        return _decode_tone_sql_freq_from_menu34_frame(menu_frame) or ""

    if label == "M->V":
        return "›"

    if label in ("DTMF", "CLONETX", "CLONERX"):
        return "›"

    return _quick_dynamic_footer(label, menu_frame, data_frame)


def _quick_menu_footer_selected(menu_frame: Optional[bytes]) -> bool:
    """Return True if the quick-menu value/footer bar is selected."""
    if _quick_assignment_text(menu_frame):
        return False
    if menu_frame is None or len(menu_frame) <= 26:
        return False
    return int(menu_frame[26]) == 0x06


QUICK_MENU_LABELS = [
    "M->V", "RPT SFT", "RPT FRQ",
    "STEP", "SQL TYP", "TONE",
    "", "CLONETX", "CLONERX",
]

# Setup menu labels/structure from the Free RIG manual and verified against
# the menu probe captures.  The frame payload often contains stale fragments
# from other screens ("RPT FRQ", "STEP", "BACKUP" etc.); for the web LCD we
# decode only the numeric item IDs from the frame and render these fixed labels.
SETUP_MENU_ITEMS = {
    1: "KEYPAD",
    2: "LCD DIMMER",
    3: "LCD CONTRAST",
    4: "BAND SCOPE",
    5: "S-METER SYMBOL",
    6: "BACKLIGHT COLOR",
    7: "TX POWER",
    8: "MIC GAIN",
    9: "VOX",
    10: "AUTO DIALER",
    11: "TOT",
    12: "FM BANDWIDTH",
    13: "RX MODE",
    14: "SUB BAND",
    15: "HOME CH",
    16: "MEMORY LIST",
    17: "MEMORY LIST MODE",
    18: "PMG",
    19: "BEEP",
    20: "BAND SKIP",
    21: "RPT ARS",
    22: "RPT SHIFT",
    23: "RPT SHIFT FREQ",
    24: "RPT REVERSE",
    25: "MIC PROGRAM KEY",
    26: "STEP",
    27: "CLOCK TYPE",
    28: "APO",
    29: "REAR SP OUT",
    30: "FRONT SP MUTE",
    31: "DTMF",
    32: "DTMF MEMORY",
    33: "SQL TYPE",
    34: "TONE SQL FREQ",
    35: "SQL EXPANSION",
    36: "PAGER CODE",
    37: "PR FREQUENCY",
    38: "BELL RINGER",
    39: "WX ALERT",
    40: "SCAN",
    41: "DUAL RECEIVE MODE",
    42: "DUAL RX INTERVAL",
    43: "PRIORITY REVERT",
    44: "SCAN RESUME",
    45: "DATA BAND",
    46: "DATA SPEED",
    47: "BACKUP",
    48: "SD INFORMATION",
    49: "SD FORMAT",
    50: "Bluetooth",
    51: "VOICE MEMORY",
    52: "FVS REC",
    53: "TRACK SELECT",
    54: "FVS PLAY",
    55: "FVS STOP",
    56: "FVS CLEAR",
    57: "VOICE GUIDE",
    58: "This -> Other",
    59: "Other -> This",
    60: "SOFTWARE VERSION",
    61: "MEMORY CH RESET",
    62: "FACTORY RESET",
}

SETUP_CATEGORIES = [
    (1, 6, "DISPLAY"),
    (7, 11, "TX"),
    (12, 14, "RX"),
    (15, 18, "MEMORY"),
    (19, 28, "CONFIG"),
    (29, 30, "AUDIO"),
    (31, 39, "SIGNALING"),
    (40, 44, "SCAN"),
    (45, 46, "DATA"),
    (47, 49, "SD CARD"),
    (50, 57, "OPTION"),
    (58, 62, "CLONE/RESET"),
]

SETUP_OPTIONS = {
    2: "MAX / MID / OFF",
    3: "1 - 5 - 9",
    4: "WIDE / NARROW",
    5: "BARS / SCALE / CONTINUE / FULL SIZE",
    6: "AMBER / WHITE",
    7: "LOW / MID / HIGH",
    8: "MIN / LOW / NORMAL / HIGH / MAX",
    10: "ON / OFF",
    11: "OFF / 1 / 2 / 3 / 5 / 10 / 15 / 20 / 30min",
    12: "WIDE / NARROW",
    13: "AUTO / FM / AM",
    15: "to HOME CH / Return to MEMORY",
    17: "ON / OFF",
    19: "OFF / LOW / HIGH",
    21: "OFF / AUTO",
    22: "AUTO / -RPT / +RPT",
    23: "0.00MHz to 99.95MHz",
    24: "NORMAL / REVERSE",
    26: "AUTO / 5.00 / 6.25 / 8.33 / 10.00 / 12.5 / 15.00 / 20.00 / 25.00 / 50.00 / 100.00 kHz",
    27: "A / B",
    28: "OFF / 0.5h ... 12.0h",
    29: "0% to 100%",
    30: "CONTINUE / AUTO MUTE",
    31: "DTMF memory",
    32: "1 to 10",
    33: "OFF / TN / TSQ / RTN / DCS / PR / PAGER ...",
    34: "CTCSS 67.0-254.1Hz / DCS 023-754",
    35: "ON / OFF",
    37: "300Hz to 3000Hz",
    38: "OFF / 1 / 3 / 5 / 8 / CONTINUOUS",
    39: "ON / OFF",
    41: "OFF / PRIORITY SCAN",
    42: "0.5 / 1 / 2 / 3 / 5 / 7 / 10sec",
    43: "OFF / ON",
    44: "BUSY / HOLD / 1 / 3 / 5sec",
    45: "MAIN BAND / SUB BAND / A-BAND FIX / B-BAND FIX",
    46: "1200 bps / 9600 bps",
    53: "ALL / 1 - 8",
    60: "Main Ver. / Sub Ver.",
}

SETUP_SUBMENUS = {
    9: ["VOX", "DELAY", "VOX MIC"],
    14: ["SUB BAND", "SUBBAND MUTE"],
    18: ["PMG TIMER", "PMG CLEAR", "PMG HOLD"],
    20: ["AIR", "VHF", "UHF", "OTHER"],
    25: ["P1", "P2", "P3", "P4"],
    34: ["CTCSS", "DCS"],
    36: ["RX CODE 1", "RX CODE 2", "TX CODE 1", "TX CODE 2"],
    47: ["WRITE TO SD", "READ FROM SD"],
    50: ["Bluetooth", "DEVICE", "AUDIO"],
    51: ["PLAY/REC", "ANNOUNCE", "LANGUAGE", "VOLUME", "RX MUTE"],
}

MIC_PROGRAM_KEY_VALUES = [
    # Learned from menu 25 F3 20 submenu frames.  No VOICE entry appeared in
    # the captured radio sequence; keep the list aligned with observed values.
    "OFF", "2nd PTT", "SCAN", "HOME CH", "RPT SHIFT", "REVERSE",
    "TX POWER", "SQL OFF", "T-CALL", "WX", "DW",
]
PAGER_CODE_VALUES = [f"{i:02d}" for i in range(1, 51)]
CTCSS_TONE_VALUES = ['67.0Hz', '69.3Hz', '71.9Hz', '74.4Hz', '77.0Hz', '79.7Hz', '82.5Hz', '85.4Hz', '88.5Hz', '91.5Hz', '94.8Hz', '97.4Hz', '100.0Hz', '103.5Hz', '107.2Hz', '110.9Hz', '114.8Hz', '118.8Hz', '123.0Hz', '127.3Hz', '131.8Hz', '136.5Hz', '141.3Hz', '146.2Hz', '151.4Hz', '156.7Hz', '158.8Hz', '162.2Hz', '165.5Hz', '167.9Hz', '171.3Hz', '173.8Hz', '177.3Hz', '179.9Hz', '183.5Hz', '186.2Hz', '189.9Hz', '192.8Hz', '196.6Hz', '199.5Hz', '203.5Hz', '206.5Hz', '210.7Hz', '218.1Hz', '225.7Hz', '229.1Hz', '233.6Hz', '241.8Hz', '250.3Hz', '254.1Hz']
DCS_CODE_VALUES = ['023', '025', '026', '031', '032', '036', '043', '047', '051', '053', '054', '065', '071', '072', '073', '074', '114', '115', '116', '122', '125', '131', '132', '134', '143', '145', '152', '155', '156', '162', '165', '172', '174', '205', '212', '223', '225', '226', '243', '244', '245', '246', '251', '252', '255', '261', '263', '265', '266', '271', '274', '306', '311', '315', '325', '331', '332', '343', '346', '351', '356', '364', '365', '371', '411', '412', '413', '423', '431', '432', '445', '446', '452', '454', '455', '462', '464', '465', '466', '503', '506', '516', '523', '526', '532', '546', '565', '606', '612', '624', '627', '631', '632', '654', '662', '664', '703', '712', '723', '731', '732', '734', '743', '754']

# Menu 23 RPT SHIFT FREQ: manual range 0.00 MHz to 99.95 MHz.
# The UI represents it as 0.05 MHz steps, matching the documented 99.95 upper bound.
RPT_SHIFT_FREQ_VALUES = [f"{i * 0.05:.2f}MHz" for i in range(0, 2000)]
RPT_SHIFT_FREQ_DEFAULT_INDEX = 12  # 0.60MHz, common VHF repeater offset

# Choices for second-level setup menu items.  Keys are (menu_number, submenu_name).
# This is local UI state used to draw the cell and keep the web behaviour aligned
# while the radio receives the real BR_LEFT/BR_RIGHT/BR_PRESS/F commands.
SETUP_SUBMENU_CHOICE_LISTS = {
    (9, "VOX"): ["OFF", "LOW", "HIGH"],
    (9, "DELAY"): ["0.5sec", "1.0sec", "1.5sec", "2.0sec", "2.5sec", "3.0sec"],
    (9, "VOX MIC"): ["FRONT", "REAR"],
    (14, "SUB BAND"): ["ON", "OFF"],
    (14, "SUBBAND MUTE"): ["OFF", "ON"],
    (18, "PMG TIMER"): ["0.5sec", "1sec", "2sec"],
    (18, "PMG CLEAR"): ["›"],
    (18, "PMG HOLD"): ["2sec", "5sec", "10sec", "20sec", "30sec"],
    (20, "AIR"): ["ON", "OFF"],
    (20, "VHF"): ["ON", "OFF"],
    (20, "UHF"): ["ON", "OFF"],
    (20, "OTHER"): ["ON", "OFF"],
    (25, "P1"): MIC_PROGRAM_KEY_VALUES,
    (25, "P2"): MIC_PROGRAM_KEY_VALUES,
    (25, "P3"): MIC_PROGRAM_KEY_VALUES,
    (25, "P4"): MIC_PROGRAM_KEY_VALUES,
    (34, "CTCSS"): CTCSS_TONE_VALUES,
    (34, "DCS"): DCS_CODE_VALUES,
    (36, "RX CODE 1"): PAGER_CODE_VALUES,
    (36, "RX CODE 2"): PAGER_CODE_VALUES,
    (36, "TX CODE 1"): PAGER_CODE_VALUES,
    (36, "TX CODE 2"): PAGER_CODE_VALUES,
    (47, "WRITE TO SD"): ["ALL", "MEMORY", "SETUP"],
    (47, "READ FROM SD"): ["ALL", "MEMORY", "SETUP"],
    (50, "Bluetooth"): ["OFF", "ON"],
    (50, "DEVICE"): ["-"],
    (50, "AUDIO"): ["AUTO", "FIX"],
}

SETUP_SUBMENU_DEFAULT_VALUE_INDEX = {
    (9, "VOX"): 0, (9, "DELAY"): 1, (9, "VOX MIC"): 0,
    (14, "SUB BAND"): 0, (14, "SUBBAND MUTE"): 0,
    (18, "PMG TIMER"): 0, (18, "PMG CLEAR"): 0, (18, "PMG HOLD"): 0,
    (20, "AIR"): 0, (20, "VHF"): 0, (20, "UHF"): 0, (20, "OTHER"): 0,
    (25, "P1"): 1, (25, "P2"): 3, (25, "P3"): 6, (25, "P4"): 10,
    (34, "CTCSS"): 12, (34, "DCS"): 0,
    (36, "RX CODE 1"): 4, (36, "RX CODE 2"): 46, (36, "TX CODE 1"): 4, (36, "TX CODE 2"): 46,
    (47, "WRITE TO SD"): 0, (47, "READ FROM SD"): 0,
    (50, "Bluetooth"): 0, (50, "DEVICE"): 0, (50, "AUDIO"): 0,
}

# Current-value choices used by the web UI while navigating the menu.  The radio
# still receives the real BR_LEFT/BR_RIGHT/BR_PRESS commands; this local table is
# only for drawing the value line when the frame does not expose the value text
# cleanly.  Defaults are the manual defaults or conservative safe values.
SETUP_CHOICE_LISTS = {
    2: ["MAX", "MID", "OFF"],
    3: ["1", "2", "3", "4", "5", "6", "7", "8", "9"],
    4: ["WIDE", "NARROW"],
    # Learned graphical S/PO meter symbols.
    5: ["BARS", "SCALE", "CONTINUE", "FULL SIZE"],
    6: ["AMBER", "WHITE"],
    7: ["LOW", "MID", "HIGH"],
    8: ["MIN", "LOW", "NORMAL", "HIGH", "MAX"],
    10: ["ON", "OFF"],
    11: ["OFF", "1min", "2min", "3min", "5min", "10min", "15min", "20min", "30min"],
    12: ["WIDE", "NARROW"],
    13: ["AUTO", "FM", "AM"],
    15: ["to HOME CH", "Return to MEMORY"],
    17: ["ON", "OFF"],
    19: ["OFF", "LOW", "HIGH"],
    21: ["OFF", "AUTO"],
    22: ["AUTO", "-RPT", "+RPT"],
    23: RPT_SHIFT_FREQ_VALUES,
    24: ["NORMAL", "REVERSE"],
    26: ["AUTO", "5.00 kHz", "6.25 kHz", "8.33 kHz", "10.00 kHz", "12.5 kHz", "15.00 kHz", "20.00 kHz", "25.00 kHz", "50.00 kHz", "100.00 kHz"],
    27: ["A", "B"],
    28: ["OFF", "0.5hour", "1.0hour", "1.5hour", "2.0hour", "3.0hour", "4.0hour", "5.0hour", "6.0hour", "7.0hour", "8.0hour", "9.0hour", "10hour", "11hour", "12hour"],
    29: ["0%", "10%", "20%", "30%", "40%", "50%", "60%", "70%", "80%", "90%", "100%"],
    30: ["CONTINUE", "AUTO MUTE"],
    33: ["OFF", "TONE ENC", "TONE SQL", "REV TONE", "DCS", "PR FREQ", "PAGER", "DCS ENC", "TONE DCS", "DCS TSQL"],
    35: ["ON", "OFF"],
    37: [f"{hz}Hz" for hz in range(300, 3001, 100)],
    38: ["OFF", "1 time", "3 times", "5 times", "8 times", "CONTINUOUS"],
    39: ["ON", "OFF"],
    41: ["OFF", "PRIORITY SCAN"],
    42: ["0.5sec", "1.0sec", "2.0sec", "3.0sec", "5.0sec", "7.0sec", "10sec"],
    43: ["OFF", "ON"],
    44: ["BUSY", "HOLD", "1 sec", "3 sec", "5 sec"],
    45: ["MAIN BAND", "SUB BAND", "A-BAND FIX", "B-BAND FIX"],
    46: ["1200 bps", "9600 bps"],
    53: ["ALL", "1", "2", "3", "4", "5", "6", "7", "8"],
    60: ["Main Ver.", "Sub Ver."],
}

SETUP_DEFAULT_VALUE_INDEX = {
    2: 1, 3: 4, 4: 0, 5: 0, 6: 0, 7: 2, 8: 2, 10: 1, 11: 0,
    12: 0, 13: 0, 15: 0, 17: 0, 19: 1, 21: 1, 22: 0, 23: RPT_SHIFT_FREQ_DEFAULT_INDEX, 24: 0,
    26: 0, 27: 0, 28: 0, 29: 10, 30: 0, 33: 0,
    35: 1, 37: 1, 38: 0, 39: 1, 41: 0, 42: 4, 43: 0,
    44: 0, 45: 0, 46: 0, 53: 0, 60: 0,
}

# Real second-level pages observed from save-recorder captures.  In the main
# In the main setup list these should show an enter/action indicator instead
# of UNKNOWN.  SETUP_REAL_SUBMENU_ITEMS open second-level list pages;
# SETUP_ACTION_ITEMS open an action/status popup or page when BR_PRESS is sent.
SETUP_REAL_SUBMENU_ITEMS = {1, 9, 14, 16, 18, 20, 25, 32, 36}
SETUP_ACTION_ITEMS = {58, 59, 60, 61, 62}
SETUP_ENTER_ITEMS = SETUP_REAL_SUBMENU_ITEMS | SETUP_ACTION_ITEMS

# Items 47..57 are not learned/implemented yet.  Keep them visible in the
# setup list but do not expose guessed values, UNKNOWN/raw text, or GUI enter
# actions for now.  Physical panel commands still pass through normally.
SETUP_INERT_BLANK_ITEMS = set(range(47, 58))

TONE_TO_SQL_TYPE_VALUE = {
    "": "OFF",
    "TN": "TONE ENC",
    "TSQ": "TONE SQL",
    "RTN": "REV TONE",
    "DCS": "DCS",
    "PR": "PR FREQ",
    "PAG": "PAGER",
}


def _is_live_menu_frame(frame: bytes) -> bool:
    # F3 20 is the live menu layer seen in captures.  F1 60 may be a live
    # value/submenu layer only while the normal display path has switched to F3
    # screen types; when the radio is back on VFO, F1 60 is usually only a stale
    # auxiliary/cache frame and must not keep the web menu visible by itself.
    return len(frame) >= RX_BLOCK_LEN and (
        (frame[0] == 0xF3 and frame[1] == 0x20) or
        (frame[0] == 0xF1 and frame[1] in (0x21, 0x22, 0x23, 0x25, 0x29)) or
        (frame[0] == 0xF1 and frame[1] == 0x60)
    )


def _is_menu_context(data_frame: Optional[bytes]) -> bool:
    return bool(data_frame and len(data_frame) >= 2 and data_frame[0] == 0xF3 and data_frame[1] in (0x02, 0x04, 0x13))


def _setup_item_number_from_pair(hi: int, lo: int) -> Optional[int]:
    # In the setup menu captures, two BCD-like bytes encode the item number:
    #   05 00 => 50, 01 06 => 16, 02 03 => 23.
    # Some one-digit legacy cases may use 64 xx; handle those too.
    if 0 <= hi <= 9 and 0 <= lo <= 9:
        n = hi * 10 + lo
        return n if 1 <= n <= 62 else None
    if hi == BLANK_DIGIT and 0 <= lo <= 9:
        return lo if 1 <= lo <= 9 else None
    return None


def _setup_category(num: Optional[int]) -> str:
    if num is None:
        return ""
    for a, b, name in SETUP_CATEGORIES:
        if a <= num <= b:
            return name
    return ""


def _setup_item_hint(num: Optional[int]) -> str:
    if num is None:
        return ""
    if num in SETUP_CHOICE_LISTS:
        return " / ".join(SETUP_CHOICE_LISTS[num])
    subs = SETUP_SUBMENUS.get(num)
    if subs:
        return " / ".join(subs)
    if num in SETUP_OPTIONS:
        return SETUP_OPTIONS[num]
    return "-"


def _active_decode_side(data_frame: Optional[bytes]) -> Optional[DecodedSide]:
    if not data_frame or len(data_frame) < RX_BLOCK_LEN:
        return None
    main = "LEFT" if data_frame[3] == 0x02 else "RIGHT" if data_frame[3] == 0x01 else "LEFT"
    return decode_side(data_frame, SIDE_LEFT if main == "LEFT" else SIDE_RIGHT)


def _setup_value_from_radio_guess(num: Optional[int], data_frame: Optional[bytes]) -> Optional[str]:
    """Best-effort live value from already mapped normal-display fields."""
    if num is None:
        return None
    side = _active_decode_side(data_frame)
    if side is None:
        return None
    mode_text, shift = _split_mode_shift(side.mode)
    if num == 13 and mode_text in ("FM", "AM"):
        return mode_text
    if num == 22:
        if shift == "+":
            return "+RPT"
        if shift == "-":
            return "-RPT"
        return "AUTO"
    if num == 24:
        # RPT REVERSE has no meaningful value when the active side is not in
        # repeater shift mode.  In that radio state the real panel leaves the
        # value area blank, so the web GUI must not show UNKNOWN.
        if shift not in ("+", "-"):
            return ""
        return None
    if num == 33:
        return TONE_TO_SQL_TYPE_VALUE.get(side.tone, None)
    return None


# v77: values learned with learn_menu for Setup items 02/03/04/06.
# In the F3 20 setup/value frame, the visible choice is encoded in block 0
# starting at +0028. Text uses the same compact LCD alphabet as overlays
# (A=0x0a ... Z=0x23), while LCD CONTRAST uses a raw numeric byte 1..9.
def _decode_lcd_choice_text(frame: bytes, start: int = 28, max_len: int = 18) -> str:
    """Decode the value field learned with learn_menu.

    The value field starts at +0028 in the F3 20 setup/value frame.  Text is
    usually in the compact LCD alphabet (A=0x0a ... Z=0x23), while numeric
    values may be raw digit bytes 0..9.  Stop on 0x64/NUL padding.
    """
    chars: List[str] = []
    for b in frame[start:start + max_len]:
        if b in (0x00, 0x64, 0xCA, 0xFF):
            break
        if 0 <= b <= 9:
            chars.append(str(b))
            continue
        ch = lcd_overlay_char(b)
        if not ch or ch == "?":
            break
        chars.append(ch)
    return "".join(chars).strip()



SMETER_SYMBOL_BY_MENU_GLYPH = {
    0xC0: "BARS",
    0xC1: "SCALE",
    0xC2: "CONTINUE",
}

SMETER_SYMBOL_BY_DISPLAY_STYLE = {
    0x00: "BARS",
    0x04: "SCALE",
    0x08: "CONTINUE",
    0x0C: "FULL SIZE",
}


def _smeter_symbol_from_menu05_frame(menu_frame: bytes) -> Optional[str]:
    """Decode Setup menu 05 S-METER SYMBOL from learned radio frames.

    Menu 05 is graphical.  The radio does not send plain text for the first
    three choices; it sends a glyph code.  Byte +0026 is only the value-bar
    selection state (0x04 in list mode, 0x06 while the bar is selected) and
    must be ignored when identifying the value.

    Learned captures:
      BARS      -> +0027..+0028 = 01 c0, with +0026 = 04 or 06
      SCALE     -> +0027..+0028 = 01 c1, with +0026 = 06
      CONTINUE  -> +0027..+0028 = 01 c2, with +0026 = 06
      FULL SIZE -> +0027 = 09 and text glyphs at +0028..+0036
    """
    if menu_frame is None or len(menu_frame) < 37:
        return None

    # Short graphical choices: +0027 declares one glyph byte at +0028.
    if menu_frame[27] == 0x01:
        return SMETER_SYMBOL_BY_MENU_GLYPH.get(menu_frame[28])

    # FULL SIZE is sent as a compact declared-length text field.
    text = _decode_lcd_declared_value_text(menu_frame, start=28, length_off=27, max_len=12)
    if text is not None and text.upper() == "FULL SIZE":
        return "FULL SIZE"

    # Conservative fallback for captures that keep the same full-size glyphs
    # but omit/alter the declared-length byte.
    if bytes(menu_frame[28:33]) == bytes([0x0F, 0x38, 0x2F, 0x2F, 0x64]):
        return "FULL SIZE"
    return None


def _smeter_symbol_from_display_frame(frame: Optional[bytes]) -> Optional[str]:
    """Decode the current S-meter symbol from the normal display lower-label byte.

    The low style bits carry menu-05 state while the high bits still carry
    S/S-DX/ASP/AUTO-A.  Learned display values: xx0/xx4/xx8/xxc.
    """
    if frame is None or len(frame) < 12:
        return None
    for off in (10, 11):
        b = frame[off]
        style = b & 0x0C
        if (b & 0x03) == 0x00 and style in SMETER_SYMBOL_BY_DISPLAY_STYLE:
            return SMETER_SYMBOL_BY_DISPLAY_STYLE[style]
    return None


def _decode_menu44_interval_from_frame(menu_frame: bytes) -> Optional[str]:
    """Decode menu 44 interval values from declared compact LCD text.

    save_20260507_094747_menu44.zip shows:
      BUSY / HOLD       -> compact text length 4
      1 sec / 3 sec / 5 sec -> compact text length 5 with a visible 0x64 space

    The generic _decode_lcd_choice_text stops at 0x64 padding, so it returned
    only "1", "3", or "5" and failed validation against the choice list.
    """
    text = _decode_lcd_declared_value_text(menu_frame, start=28, length_off=27, max_len=8)
    if not text:
        return None
    norm = re.sub(r"\s+", " ", text).strip().upper()
    return {
        "BUSY": "BUSY",
        "HOLD": "HOLD",
        "1 SEC": "1 sec",
        "3 SEC": "3 sec",
        "5 SEC": "5 sec",
    }.get(norm)



def _setup_value_from_menu_frame(num: Optional[int], menu_frame: Optional[bytes]) -> Optional[str]:
    if num is None or menu_frame is None or len(menu_frame) < 40:
        return None
    if not (menu_frame[0] == 0xF3 and menu_frame[1] == 0x20):
        return None

    # Menu 27 CLOCK TYPE is a declared one-glyph value.  When arriving from
    # menu 28 the radio leaves stale trailing bytes after the real value
    # (e.g. +0026..+0033 = 04 01 0a 0f 0f 64 42 64).  Respect +0027=1
    # and decode only +0028 so A does not become bogus AFF/UNKNOWN.
    if num == 27:
        return _decode_clock_type_from_menu27_frame(menu_frame)

    # Menu 31 DTMF points to a stored DTMF memory.  The radio sends the
    # memory slot plus the real stored digits in a compact value field.
    if num == 31:
        return _decode_menu31_dtmf_from_frame(menu_frame)

    # Menu 05 is graphical: decode by learned glyph/signature fields.
    if num == 5:
        return _smeter_symbol_from_menu05_frame(menu_frame)

    # Menu 15 HOME CH is a declared-length compact text field with lowercase glyphs.
    if num == 15:
        return _decode_home_ch_from_menu15_frame(menu_frame)

    # Menu 26 STEP is a numeric kHz field learned from F3 20 frames.
    # The dot byte 0x4B moves according to value width:
    #   05 4b 00 00       -> 5.00 kHz
    #   06 4b 02 05       -> 6.25 kHz
    #   01 00 4b 00 00    -> 10.00 kHz
    #   01 00 00 4b 00 00 -> 100.00 kHz
    if num == 26:
        return _decode_step_from_menu26_frame(menu_frame)

    # Menu 23 RPT SHIFT FREQ is a numeric frequency field, not compact LCD text.
    # It is learned from +0028..+0032 and continues in 0.05 MHz steps up to 99.95MHz.
    if num == 23:
        return _decode_rpt_shift_freq_from_menu23_frame(menu_frame)

    # Menu 29 REAR SP OUT is a numeric percentage field learned from F3 20 frames.
    if num == 29:
        return _decode_rear_sp_out_from_menu29_frame(menu_frame)

    # Menu 30 FRONT SP MUTE uses declared compact text.  AUTO MUTE contains
    # 0x64 as a visible space; the generic decoder treats 0x64 as padding and
    # would only read AUTO, causing UNKNOWN/raw to be displayed.
    if num == 30:
        return _decode_front_sp_mute_from_menu30_frame(menu_frame)

    # Menu 34 TONE SQL FREQ is a CTCSS tone value learned from F3 20 frames.
    if num == 34:
        return _decode_tone_sql_freq_from_menu34_frame(menu_frame)

    # Menu 37 PR FREQUENCY is a numeric Hz value learned from F3 20 frames.
    if num == 37:
        return _decode_pr_frequency_from_menu37_frame(menu_frame)

    # Menu 42 DUAL RX INTERVAL is a numeric seconds value learned from F3 20 frames.
    if num == 42:
        return _decode_dual_rx_interval_from_menu42_frame(menu_frame)

    # Menu 44 interval values include "1 sec" / "3 sec" / "5 sec" with a
    # visible 0x64 space; the generic decoder used to stop there and show UNKNOWN.
    if num == 44:
        return _decode_menu44_interval_from_frame(menu_frame)

    # Menu 45 DATA BAND is compact LCD text learned from F3 20 frames.
    if num == 45:
        return _decode_data_band_from_menu45_frame(menu_frame)

    # Menu 46 DATA SPEED is a compact numeric/text field learned from F3 20 frames.
    if num == 46:
        return _decode_data_speed_from_menu46_frame(menu_frame)

    # Special numeric case learned for menu 03: the contrast number is the raw
    # byte at +0028.  Missing steps in a capture are still covered by 1..9.
    if num == 3:
        v = menu_frame[28]
        if 1 <= v <= 9:
            return str(v)

    text = _decode_lcd_choice_text(menu_frame, 28, 18)
    if not text:
        return None

    # For learned menus with known option lists, return only a valid option.
    # This avoids rendering garbage fragments as real settings.
    known = SETUP_CHOICE_LISTS.get(num)
    if known:
        norm = {str(x).upper(): str(x) for x in known}
        if text.upper() in norm:
            return norm[text.upper()]
        # Menu 23 is numeric and may be partly learned.  Accept compact numeric
        # strings even when the exact entry is not in the list yet.
        if num == 23 and any(ch.isdigit() for ch in text):
            return text
        return None

    # For unmapped/future learned menus, expose the decoded value instead of
    # pretending UNKNOWN when the radio clearly sends text at +0028.
    return text






def _decode_clock_type_from_menu27_frame(menu_frame: bytes) -> Optional[str]:
    """Decode Setup menu 27 CLOCK TYPE from the declared one-byte field.

    Learned from live navigation: from menu 28 back to 27 the field can contain
    stale trailing glyphs after the real value:
      +0026..+0033 = 04 01 0a 0f 0f 64 42 64

    +0027 is the authoritative visible length; +0028 is the actual CLOCK TYPE
    glyph.  Decode only the declared byte and accept the two known values A/B.
    """
    text = _decode_lcd_declared_value_text(menu_frame, start=28, length_off=27, max_len=2)
    if text in ("A", "B"):
        return text
    # Conservative fallback for captures that may omit the declared length but
    # still put the selected clock type at +0028.
    if menu_frame is not None and len(menu_frame) > 28:
        ch = _lcd_menu_value_char(int(menu_frame[28])).strip()
        if ch in ("A", "B"):
            return ch
    return None


# Last stable DTMF edit cursor position.  The radio alternates +0019 with
# 0x80 phase frames; those frames must not move the visual edit marker to the
# end of the entered string.
DTMF_EDIT_LAST_CURSOR_POS: Optional[int] = None


def _decode_dtmf_digit_char(b: int) -> str:
    """Decode one stored DTMF digit/glyph.

    Observed menu 32 edit/list frames use raw numeric bytes 0..9 for digits,
    compact A..D glyphs when present, 0x4A as the visual dash placeholder,
    and 0xCA as an empty edit-cell placeholder.
    """
    if 0 <= b <= 9:
        return str(b)
    if 0x0A <= b <= 0x0D:
        return chr(ord("A") + b - 0x0A)
    if b == 0x4A:
        return "-"
    if b == 0x64:
        return " "
    if b in (0x00, 0xCA, 0xFF):
        return ""
    ch = _lcd_menu_value_char(int(b))
    return "" if ch == "?" else ch


def _decode_dtmf_digits(raw: bytes, trim_placeholders: bool = True) -> str:
    chars: List[str] = []
    for b in raw:
        ch = _decode_dtmf_digit_char(int(b))
        if not ch:
            if int(b) in (0xCA, 0x00, 0xFF):
                # In DTMF fields 0xCA fills unused edit cells. 0x64 is a real
                # space key and must not terminate the value.
                break
            continue
        chars.append(ch)
    text = "".join(chars)
    if trim_placeholders:
        text = text.rstrip("-")
    return text


def _decode_menu31_dtmf_from_frame(menu_frame: bytes) -> Optional[str]:
    """Decode Setup menu 31 DTMF selected memory from the F3 20 value field.

    Learned from save_20260506_092914_menu31:
      +0027 = 0x13            field length
      +0028 = memory slot     e.g. 03 / 04
      +0029 = 0x4d            separator glyph on the radio LCD
      +0030..+0045            stored DTMF digits followed by 0x4A dash fill

    The old generic decoder stopped/guessed and produced UNKNOWN.  Show the
    actual memory number and the stored digits; empty memories remain visibly
    dashed instead of being invented locally.
    """
    if menu_frame is None or len(menu_frame) < 46:
        return None
    if menu_frame[27] != 0x13:
        return None
    slot = int(menu_frame[28])
    sep = int(menu_frame[29])
    if not (1 <= slot <= 10) or sep != 0x4D:
        return None
    raw_digits = menu_frame[30:46]
    digits = _decode_dtmf_digits(raw_digits, trim_placeholders=True)
    if not digits:
        digits = "-" * 16
    return f"{slot:02d}: {digits}"

def _decode_step_from_menu26_frame(menu_frame: bytes) -> Optional[str]:
    """Decode menu 26 STEP from learned F3 20 value field.

    Learned examples at / around +0028:
      05 4b 00 00       -> 5.00 kHz
      06 4b 02 05       -> 6.25 kHz
      01 00 4b 00 00    -> 10.00 kHz
      01 02 4b 05 00    -> 12.50 kHz
      01 00 00 4b 00 00 -> 100.00 kHz

    The field is numeric, not compact text.  Decode by locating the 0x4B
    decimal separator in the learned value area and reading decimal digit
    bytes around it.
    """
    if menu_frame is None or len(menu_frame) < 36:
        return None

    # AUTO, when present, should decode through normal compact text first.
    txt = _decode_lcd_choice_text(menu_frame, 28, 10)
    if txt.upper() == "AUTO":
        return "AUTO"

    # Value starts at +0028 in all captures, but the decimal point shifts:
    # 5.00/6.25 use +0029, 10.00..50.00 use +0030, 100.00 uses +0031.
    area_start = 28
    area_end = min(len(menu_frame), 36)
    area = menu_frame[area_start:area_end]
    try:
        dot_rel = area.index(0x4B)
    except ValueError:
        return None
    dot = area_start + dot_rel

    def is_digit(x: int) -> bool:
        return 0 <= x <= 9

    left: List[int] = []
    i = dot - 1
    while i >= area_start and is_digit(menu_frame[i]):
        left.append(int(menu_frame[i]))
        i -= 1
    left.reverse()

    right: List[int] = []
    i = dot + 1
    while i < area_end and is_digit(menu_frame[i]) and len(right) < 2:
        right.append(int(menu_frame[i]))
        i += 1

    if not left or not right:
        return None
    while len(right) < 2:
        right.append(0)

    whole = int("".join(str(d) for d in left))
    frac = int("".join(str(d) for d in right[:2]))
    value = f"{whole}.{frac:02d} kHz"

    # Normalise the known Yaesu step spelling.  12.50 is commonly displayed as
    # 12.5 on the radio, but keeping two decimals matches the other learned
    # numeric values and the Setup choice list.
    known = {v.upper(): v for v in SETUP_CHOICE_LISTS.get(26, [])}
    return known.get(value.upper(), value)

def _decode_rpt_shift_freq_from_menu23_frame(menu_frame: bytes) -> Optional[str]:
    """Decode menu 23 RPT SHIFT FREQ from learned F3 20 value field.

    Learned field at +0028..+0032:
      64 00 4b 00 00 -> 0.00MHz
      64 00 4b 00 05 -> 0.05MHz
      64 00 4b 01 00 -> 0.10MHz
      64 00 4b 02 05 -> 0.25MHz
      09 09 4b 09 05 -> 99.95MHz

    Bytes are two digits before the decimal, 0x4b decimal point, and two
    digits after it; 0x64 is a leading blank.
    """
    if menu_frame is None or len(menu_frame) < 33:
        return None
    a, b, dot, c, d = menu_frame[28:33]
    if dot != 0x4B:
        return None

    def digit(x: int, allow_blank: bool = False) -> Optional[int]:
        if allow_blank and x == BLANK_DIGIT:
            return 0
        if 0 <= x <= 9:
            return int(x)
        return None

    hi = digit(a, allow_blank=True)
    lo = digit(b)
    t = digit(c)
    h = digit(d)
    if hi is None or lo is None or t is None or h is None:
        return None
    whole = hi * 10 + lo
    frac = t * 10 + h
    if whole > 99 or frac > 95:
        return None
    # The radio steps in 0.05 MHz increments, so the last digit is expected
    # to be 0 or 5; keep the decoder conservative.
    if h not in (0, 5):
        return None
    return f"{whole}.{frac:02d}MHz"



def _decode_rear_sp_out_from_menu29_frame(menu_frame: bytes) -> Optional[str]:
    """Decode menu 29 REAR SP OUT from learned F3 20 value field.

    Learned field at +0028..+0032:
      64 64 00 64 42 -> 0%
      64 01 00 64 42 -> 10%
      64 09 00 64 42 -> 90%
      01 00 00 64 42 -> 100%

    Bytes are three display digit slots, 0x64 is a leading blank, and
    +0032 is the percent glyph.  Keep this conservative: only accept the
    documented 0..100 range in 10% steps.
    """
    if menu_frame is None or len(menu_frame) < 33:
        return None
    a, b, c, blank, pct = menu_frame[28:33]
    if pct != 0x42 or blank != BLANK_DIGIT:
        return None

    def digit_or_blank(x: int) -> Optional[str]:
        if x == BLANK_DIGIT:
            return ""
        if 0 <= x <= 9:
            return str(int(x))
        return None

    chars: List[str] = []
    for x in (a, b, c):
        ch = digit_or_blank(x)
        if ch is None:
            return None
        chars.append(ch)

    text = "".join(chars).lstrip()
    if not text:
        return None
    try:
        value = int(text, 10)
    except ValueError:
        return None
    if value < 0 or value > 100 or value % 10 != 0:
        return None
    return f"{value}%"


def _decode_tone_sql_freq_from_menu34_frame(menu_frame: bytes) -> Optional[str]:
    """Decode menu 34 TONE SQL FREQ from learned F3 20 CTCSS value field.

    Learned field at +0028..+0035:
      64 06 07 4b 00 64 11 3d -> 67.0Hz
      64 08 05 4b 04 64 11 3d -> 85.4Hz
      01 05 06 4b 07 64 11 3d -> 156.7Hz
      01 05 09 4b 08 64 11 3d -> 158.8Hz per corrected learn note
      02 05 04 4b 01 64 11 3d -> 254.1Hz

    Bytes are three digit slots, 0x4b decimal separator, one fractional
    digit, 0x64 padding, then the learned Hz glyphs 0x11 0x3d.  Keep this
    decoder conservative and accept only the learned/known CTCSS tone list.
    """
    if menu_frame is None or len(menu_frame) < 36:
        return None
    a, b, c, dot, frac, pad, hz_h, hz_z = menu_frame[28:36]
    if dot != 0x4B or pad != BLANK_DIGIT or (hz_h, hz_z) != (0x11, 0x3D):
        return None

    def digit_or_blank(x: int) -> Optional[str]:
        if x == BLANK_DIGIT:
            return ""
        if 0 <= x <= 9:
            return str(int(x))
        return None

    whole_chars: List[str] = []
    for x in (a, b, c):
        ch = digit_or_blank(x)
        if ch is None:
            return None
        whole_chars.append(ch)
    if not (0 <= frac <= 9):
        return None

    whole_text = "".join(whole_chars).lstrip()
    if not whole_text:
        return None
    value_text = f"{int(whole_text, 10)}.{int(frac)}Hz"

    # The learn dump filename around this slot had an extra leading digit; the
    # project note says the visible/corrected value is 158.8, not 1158.8.
    # Match the exact radio field so the correction is limited to this one slot.
    if (a, b, c, dot, frac, pad, hz_h, hz_z) == (0x01, 0x05, 0x09, 0x4B, 0x08, BLANK_DIGIT, 0x11, 0x3D):
        value_text = "158.8Hz"

    known = {str(v).upper(): str(v) for v in CTCSS_TONE_VALUES}
    return known.get(value_text.upper())




def _lcd_menu_value_char(b: int) -> str:
    """Decode one compact setup-value glyph used in F3 20 value fields.

    This is related to the overlay alphabet, but setup values also use:
      0x24..0x3d = lowercase a..z
      0x4a       = '-'
      0x4b       = decimal point
      0x64       = visible space inside declared-length fields
    """
    if 0 <= b <= 9:
        return str(int(b))
    if 0x0A <= b <= 0x23:
        return chr(ord("A") + b - 0x0A)
    if 0x24 <= b <= 0x3D:
        return chr(ord("a") + b - 0x24)
    if b == 0x42:
        return "%"
    if b == 0x4A:
        return "-"
    if b == 0x4B:
        return "."
    if b in (0x00, 0x64):
        return " "
    if 32 <= b < 127:
        return chr(b)
    return "?"


def _decode_lcd_declared_value_text(frame: bytes, start: int = 28, length_off: int = 27, max_len: int = 18) -> Optional[str]:
    """Decode a compact LCD value when byte +0027 declares the field length."""
    if frame is None or len(frame) <= max(start, length_off):
        return None
    n = frame[length_off]
    if not (1 <= n <= max_len):
        return None
    if start + n > len(frame):
        return None
    chars: List[str] = []
    for b in frame[start:start + n]:
        ch = _lcd_menu_value_char(int(b))
        if ch == "?":
            return None
        chars.append(ch)
    text = "".join(chars).strip()
    return text or None



def _decode_home_ch_from_menu15_frame(menu_frame: bytes) -> Optional[str]:
    """Decode menu 15 HOME CH from its declared-length compact LCD field.

    Learned from save_20260506_080119_menu15:
      +0027 = 0x0a, +0028..+0031... -> "to HOME CH"
      +0027 = 0x10, +0028..+0037... -> "Return to MEMORY"

    This field is not plain ASCII: lowercase letters use 0x24..0x3d and
    0x64 is a visible space inside the declared field.  The older generic
    decoder stopped at the first 0x64 and misread lowercase bytes as ASCII,
    producing values such as "72" or "R(7851".
    """
    text = _decode_lcd_declared_value_text(menu_frame, start=28, length_off=27, max_len=18)
    if text is None:
        return None
    known = {str(v).upper(): str(v) for v in SETUP_CHOICE_LISTS.get(15, [])}
    return known.get(text.upper())



def _decode_front_sp_mute_from_menu30_frame(menu_frame: bytes) -> Optional[str]:
    """Decode Setup menu 30 FRONT SP MUTE from its declared LCD value field.

    Learned/observed values:
      CONTINUE  -> compact text without embedded spaces
      AUTO MUTE -> compact text with 0x64 as a visible space

    The generic _decode_lcd_choice_text() stops at 0x64 because that byte is
    padding for many older learned values.  For menu 30 it is part of the
    visible value, so use the declared length at +0027 and validate against the
    known choices.
    """
    text = _decode_lcd_declared_value_text(menu_frame, start=28, length_off=27, max_len=12)
    if text is None:
        return None
    norm = re.sub(r"\s+", " ", text).strip().upper()
    known = {str(v).upper(): str(v) for v in SETUP_CHOICE_LISTS.get(30, [])}
    return known.get(norm)


def _decode_pr_frequency_from_menu37_frame(menu_frame: bytes) -> Optional[str]:
    """Decode menu 37 PR FREQUENCY from learned F3 20 value field.

    Learned field uses declared length +0027 = 0x06 and value bytes +0028..+0033:
      64 03 00 00 11 3d -> 300Hz
      01 05 00 00 11 3d -> 1500Hz
      02 04 00 00 11 3d -> 2400Hz (frame value; filename dump around this slot was duplicated)
      03 00 00 00 11 3d -> 3000Hz
    """
    text = _decode_lcd_declared_value_text(menu_frame)
    if text is None or not text.endswith("Hz"):
        return None
    number = text[:-2]
    if not number.isdigit():
        return None
    hz = int(number, 10)
    if hz < 300 or hz > 3000 or hz % 100 != 0:
        return None
    return f"{hz}Hz"


def _decode_dual_rx_interval_from_menu42_frame(menu_frame: bytes) -> Optional[str]:
    """Decode menu 42 DUAL RX INTERVAL from learned F3 20 value field.

    Learned value bytes, declared by +0027 = 0x06:
      00 4b 05 36 28 26 -> 0.5sec
      01 4b 00 36 28 26 -> 1.0sec
      07 4b 00 36 28 26 -> 7.0sec
      64 01 00 36 28 26 -> 10sec
    """
    text = _decode_lcd_declared_value_text(menu_frame)
    if text is None:
        return None
    known = {str(v).upper(): str(v) for v in SETUP_CHOICE_LISTS.get(42, [])}
    return known.get(text.upper())


def _decode_data_band_from_menu45_frame(menu_frame: bytes) -> Optional[str]:
    """Decode menu 45 DATA BAND from learned compact LCD text field."""
    text = _decode_lcd_declared_value_text(menu_frame)
    if text is None:
        return None
    known = {str(v).upper(): str(v) for v in SETUP_CHOICE_LISTS.get(45, [])}
    return known.get(text.upper())


def _decode_data_speed_from_menu46_frame(menu_frame: bytes) -> Optional[str]:
    """Decode menu 46 DATA SPEED from learned compact numeric/text field."""
    text = _decode_lcd_declared_value_text(menu_frame)
    if text is None:
        return None
    known = {str(v).upper(): str(v) for v in SETUP_CHOICE_LISTS.get(46, [])}
    return known.get(text.upper())





def _decode_plain_menu_ascii(raw: bytes) -> str:
    """Decode plain ASCII title/list bytes used by submenu headers."""
    chars: List[str] = []
    for b in raw:
        if b in (0x00, BLANK_DIGIT, 0xFF):
            chars.append(" ")
        elif 32 <= b <= 126:
            chars.append(chr(b))
        else:
            chars.append(" ")
    return re.sub(r"\s+", " ", "".join(chars)).strip()


def _decode_compact_menu_text(raw: bytes) -> Optional[str]:
    """Decode compact LCD text used by second-level setup rows."""
    chars: List[str] = []
    for b in raw:
        ch = _lcd_menu_value_char(int(b))
        if ch == "?":
            return None
        chars.append(ch)
    text = re.sub(r"\s+", " ", "".join(chars)).strip()
    return text or None


TEXT_SETUP_SUBMENU_DEFS = {
    9: {
        "title": "VOX",
        "rows": ["VOX", "DELAY", "VOX MIC"],
        "choices": {
            "VOX": ["OFF", "LOW", "HIGH"],
            "DELAY": ["0.5sec", "1.0sec", "1.5sec", "2.0sec", "2.5sec", "3.0sec"],
            "VOX MIC": ["FRONT", "REAR"],
        },
    },
    14: {
        "title": "SUB BAND",
        "rows": ["SUB BAND", "SUBBAND MUTE"],
        "choices": {
            "SUB BAND": ["ON", "OFF"],
            "SUBBAND MUTE": ["OFF", "ON"],
        },
    },
    18: {
        "title": "PMG",
        "rows": ["PMG TIMER", "PMG CLEAR", "PMG HOLD"],
        "choices": {
            "PMG TIMER": ["0.5sec", "1sec", "2sec"],
            # The radio sends a single non-text action glyph 0x51 for PMG CLEAR.
            "PMG CLEAR": ["›"],
            "PMG HOLD": ["2sec", "5sec", "10sec", "20sec", "30sec"],
        },
    },
    20: {
        "title": "BAND SKIP",
        # Four logical rows exist, but only three are visible; the radio scrolls
        # the visible window (e.g. VHF/UHF/OTHER), so row labels are decoded from
        # the frame rather than fixed to physical row 0/1/2.
        "rows": ["AIR", "VHF", "UHF", "OTHER"],
        "choices": {
            "AIR": ["ON", "OFF"],
            "VHF": ["ON", "OFF"],
            "UHF": ["ON", "OFF"],
            "OTHER": ["ON", "OFF"],
        },
    },
    36: {
        "title": "PAGER CODE",
        # Four logical rows exist, but only three are visible; when TX CODE 2 is
        # reached, the radio scrolls the window to RX CODE 2 / TX CODE 1 / TX CODE 2.
        # Labels and values are therefore decoded from the frame, not inferred
        # from the physical row.
        "rows": ["RX CODE 1", "RX CODE 2", "TX CODE 1", "TX CODE 2"],
        "choices": {
            "RX CODE 1": PAGER_CODE_VALUES,
            "RX CODE 2": PAGER_CODE_VALUES,
            "TX CODE 1": PAGER_CODE_VALUES,
            "TX CODE 2": PAGER_CODE_VALUES,
        },
    },
}


def _normalise_menu_value_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().upper()


def _decode_text_submenu_row_label(raw: bytes, known_labels: Sequence[str], fallback: str) -> str:
    """Decode the left-hand label from a visible text-submenu row.

    Most submenus keep the same three physical rows, but menu 20 BAND SKIP
    scrolls a four-row list through three visible slots.  Therefore the label
    must come from the radio frame when possible.
    """
    # The left label starts right after the marker byte.  It is padded with
    # 0x64 spaces; value text starts much later, so 12 bytes is enough for the
    # longest observed row label (SUBBAND MUTE is still handled by fallback).
    text = _decode_compact_menu_text(raw[1:13]) if len(raw) > 1 else None
    if text:
        norm = _normalise_menu_value_text(text)
        for label in known_labels:
            if norm == _normalise_menu_value_text(label):
                return str(label)
    return fallback


def _decode_text_submenu_value(menu_num: int, row_label: str, raw: bytes) -> Tuple[Optional[str], str]:
    """Decode the right-hand value cell of text based submenu rows.

    The row body is 22 bytes.  The value is right-aligned after the row label,
    but its exact start shifts by one/two bytes across pages.  Try the learned
    tail offsets and accept only values present in the row's observed choice
    list.  This keeps the GUI tied to the radio frame instead of local state.
    """
    definition = TEXT_SETUP_SUBMENU_DEFS.get(menu_num, {})
    choices = list((definition.get("choices") or {}).get(row_label, []))
    choice_by_norm = {_normalise_menu_value_text(v): v for v in choices}

    # PMG CLEAR sends a graphic/action glyph.  Treat the exact learned glyph as
    # a submenu/action indicator instead of exposing the meaningless raw "Q".
    if menu_num == 18 and row_label == "PMG CLEAR" and 0x51 in raw:
        return "›", "learned-f3"

    for rel in range(13, 18):
        if rel >= len(raw):
            continue
        text = _decode_compact_menu_text(raw[rel:])
        if not text:
            continue
        norm = _normalise_menu_value_text(text)
        if norm in choice_by_norm:
            return choice_by_norm[norm], "learned-f3"

    # Conservative fallback: return compact text only if it is not just the
    # left label repeated/stale garbage.  The caller will mark it as unknown.
    text = _decode_compact_menu_text(raw[13:])
    return text, "unknown" if text else "unknown"


def _decode_text_setup_submenu_row(menu_num: int, menu_frame: bytes, start: int, physical_row: int, label: str) -> Optional[dict]:
    if menu_frame is None or start + 22 > len(menu_frame):
        return None
    marker = int(menu_frame[start])
    # 0x00 normal, 0x02 selected value/editing.  Menu 14 can mark dependent rows
    # with 0x40 while still displaying their radio-provided value.
    if marker not in (0x00, 0x02, 0x40):
        return None
    raw = menu_frame[start:start + 22]
    value, source = _decode_text_submenu_value(menu_num, label, raw)
    if value is None:
        value = "UNKNOWN"
        source = "unknown"
    elif source != "learned-f3":
        source = "unknown"
    return {
        "physical_row": physical_row,
        "row": physical_row,
        "num": label,
        "key": label,
        "text": value,
        "value": value,
        "value_source": source,
        "raw_value": f"+{start:04d}..+{start + 21:04d}: " + raw.hex(" "),
        "editing": marker == 0x02,
        "disabled": marker == 0x40,
    }




def _decode_software_version_row(raw: bytes) -> Optional[Tuple[str, str]]:
    """Decode one SOFTWARE VERSION row such as 'Main Ver. M 1.03'."""
    chars: List[str] = []
    for b in raw:
        ch = _lcd_menu_value_char(int(b))
        if ch == "?":
            ch = " "
        chars.append(ch)
    text = re.sub(r"\s+", " ", "".join(chars)).strip()
    m = re.match(r"^(Main Ver\.|Sub Ver\.)\s+(.+)$", text)
    if not m:
        return None
    return m.group(1), m.group(2).strip()


def _software_version_state_from_frame(menu_frame: Optional[bytes], age: float) -> Optional[dict]:
    """Decode menu 60 SOFTWARE VERSION, which opens a read-only display2 page."""
    if menu_frame is None or len(menu_frame) < 104:
        return None
    if not (menu_frame[0] == 0xF3 and menu_frame[1] == 0x20):
        return None
    title = _decode_plain_menu_ascii(menu_frame[28:46])
    if title != "SOFTWARE VERSION":
        return None

    # Learned from save_20260506_072933_menu25:
    # +0061..+0077 = Main Ver.  M 1.03
    # +0080..+0096 = Sub Ver.   M 1.02
    rows: List[dict] = []
    for physical_row, (start, end) in enumerate(((61, 78), (80, 97))):
        parsed = _decode_software_version_row(menu_frame[start:end])
        if parsed is None:
            continue
        key, value = parsed
        rows.append({
            "physical_row": physical_row,
            "row": len(rows),
            "num": key,
            "key": key,
            "text": value,
            "value": value,
            "value_source": "learned-f3",
            "raw_value": f"+{start:04d}..+{end - 1:04d}: " + menu_frame[start:end].hex(" "),
            "editing": False,
            "disabled": True,
        })
    if not rows:
        return None
    return {
        "visible": True,
        "type": "submenu",
        "age_s": age,
        "parent_num": 60,
        "title": "SOFTWARE VERSION",
        "category": _setup_category(60),
        "selected_row": -1,
        "selected_key": None,
        "selected_value": None,
        "editing": False,
        "read_only": True,
        "rows": rows,
    }


def _text_setup_submenu_state_from_frame(menu_frame: Optional[bytes], age: float) -> Optional[dict]:
    """Decode learned second-level text submenu pages: 09, 14, 18, 20 and 36."""
    if menu_frame is None or len(menu_frame) < 126:
        return None
    if not (menu_frame[0] == 0xF3 and menu_frame[1] == 0x20):
        return None

    title = _decode_plain_menu_ascii(menu_frame[28:46])
    matched_num: Optional[int] = None
    matched_def: Optional[dict] = None
    for num, definition in TEXT_SETUP_SUBMENU_DEFS.items():
        if title == definition.get("title"):
            matched_num = num
            matched_def = definition
            break
    if matched_num is None or matched_def is None:
        return None

    labels = list(matched_def.get("rows") or [])
    rows: List[dict] = []
    for physical_row, start in enumerate((60, 82, 104)):
        if physical_row >= len(labels) and matched_num != 20:
            continue
        fallback_label = labels[physical_row] if physical_row < len(labels) else ""
        raw = menu_frame[start:start + 22] if start + 22 <= len(menu_frame) else b""
        label = _decode_text_submenu_row_label(raw, labels, fallback_label) if raw else fallback_label
        if not label:
            continue
        row = _decode_text_setup_submenu_row(matched_num, menu_frame, start, physical_row, label)
        if row is not None:
            row["row"] = len(rows)
            rows.append(row)
    if not rows:
        return None

    selected_raw = int(menu_frame[13]) if len(menu_frame) > 13 else 0
    if not (0 <= selected_raw < len(rows)):
        selected_raw = 0
    editing = any(bool(r.get("editing")) for r in rows)
    selected_key = rows[selected_raw].get("key") if rows else None
    selected_value = rows[selected_raw].get("value") if rows else None
    return {
        "visible": True,
        "type": "submenu",
        "age_s": age,
        "parent_num": matched_num,
        "title": str(matched_def.get("title") or SETUP_MENU_ITEMS.get(matched_num, "")),
        "category": _setup_category(matched_num),
        "selected_row": selected_raw,
        "selected_key": selected_key,
        "selected_value": selected_value,
        "editing": editing,
        "rows": rows,
    }

def _decode_menu25_submenu_value_text(raw: bytes) -> Optional[str]:
    """Decode one MIC PROGRAM KEY value from the second-level menu rows."""
    chars: List[str] = []
    for b in raw:
        ch = _lcd_menu_value_char(int(b))
        if ch == "?":
            return None
        chars.append(ch)
    text = "".join(chars).replace("\x00", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None
    known = {str(v).upper(): str(v) for v in MIC_PROGRAM_KEY_VALUES}
    return known.get(text.upper(), text)


def _decode_menu25_submenu_row(menu_frame: bytes, start: int) -> Optional[dict]:
    """Decode one visible P1/P2/P3/P4 row from menu 25 MIC PROGRAM KEY.

    Learned row layout in the save recorder, for three visible rows at
    +0060, +0082 and +0104:
      +0      = 0x00 normal, 0x02 selected value/editing row
      +3..+6  = 54 19 NN 56, visually <P1>, <P2>, <P3>, <P4>
      +7..+21 = compact LCD text for the assigned function

    The screen cursor itself is still byte +0013, physical row 0..2.
    """
    if menu_frame is None or start + 22 > len(menu_frame):
        return None
    marker = menu_frame[start]
    if marker not in (0x00, 0x02):
        return None
    if not (menu_frame[start + 3] == 0x54 and menu_frame[start + 4] == 0x19 and menu_frame[start + 6] == 0x56):
        return None
    pnum = menu_frame[start + 5]
    if not (1 <= pnum <= 4):
        return None
    value_raw = menu_frame[start + 7:start + 22]
    value = _decode_menu25_submenu_value_text(value_raw)
    value_source = "learned-f3" if value is not None and value.upper() in {v.upper() for v in MIC_PROGRAM_KEY_VALUES} else "unknown"
    if value is None:
        value = "UNKNOWN"
    return {
        "physical_row": (start - 60) // 22,
        "row": (start - 60) // 22,
        "num": f"P{pnum}",
        "key": f"P{pnum}",
        "text": value,
        "value": value,
        "value_source": value_source,
        "raw_value": f"+{start + 7:04d}..+{start + 21:04d}: " + value_raw.hex(" "),
        "editing": marker == 0x02,
    }


def _menu25_submenu_state_from_frame(menu_frame: Optional[bytes], age: float) -> Optional[dict]:
    """Return structured web state for menu 25's P1/P2/P3/P4 submenu.

    This is not a normal setup list: after pressing BR_PRESS on menu 25 the
    radio sends a dedicated F3 20 page with title at +0025..+0045 and three
    visible P-key rows.  Values are decoded from the radio frame only.
    """
    if menu_frame is None or len(menu_frame) < 126:
        return None
    if not (menu_frame[0] == 0xF3 and menu_frame[1] == 0x20):
        return None
    # Title line: 02 05 20 "MIC PROGRAM KEY".
    if not (menu_frame[25] == 0x02 and menu_frame[26] == 0x05 and menu_frame[27] == 0x20):
        return None
    if b"MIC PROGRAM KEY" not in menu_frame[28:46]:
        return None

    rows: List[dict] = []
    for start in (60, 82, 104):
        row = _decode_menu25_submenu_row(menu_frame, start)
        if row is not None:
            row["row"] = len(rows)
            rows.append(row)
    if not rows:
        return None

    selected_raw = int(menu_frame[13]) if len(menu_frame) > 13 else 0
    if not (0 <= selected_raw < len(rows)):
        selected_raw = 0
    editing = any(bool(r.get("editing")) for r in rows)
    selected_key = rows[selected_raw].get("key") if rows else None
    selected_value = rows[selected_raw].get("value") if rows else None
    return {
        "visible": True,
        "type": "submenu",
        "age_s": age,
        "parent_num": 25,
        "title": "MIC PROGRAM KEY",
        "category": _setup_category(25),
        "selected_row": selected_raw,
        "selected_key": selected_key,
        "selected_value": selected_value,
        "editing": editing,
        "rows": rows,
    }





def _menu1_keypad_selected_index(menu_frame: bytes) -> Optional[int]:
    """Decode selected key from menu 1 KEY PAD F1 22 frame.

    Learned from save_20260507_100900_menu1.zip:
      +0160..+0169 = 0x20 selects digits 1..0
      +0174..+0176 = 0x21/0x22 selects the three bottom soft keys
    +0150 also carries a constant 0x20 and must be ignored.
    """
    if menu_frame is None or len(menu_frame) < 181:
        return None
    digit_area = menu_frame[160:170]
    if 0x20 in digit_area:
        return list(digit_area).index(0x20)
    for off, idx in ((174, 10), (175, 11), (176, 12)):
        if off < len(menu_frame) and int(menu_frame[off]) in (0x21, 0x22):
            return idx
    return None


def _menu1_keypad_state_from_frame(menu_frame: Optional[bytes], age: float) -> Optional[dict]:
    """Decode setup menu 01 KEY PAD popup.

    Radio popup layouts:
      FREQUENCY
      1 2 3 4 5
      6 7 8 9 0
      MEM CH   MEM LIST   DEL

      MEMORY CH
      1 2 3 4 5
      6 7 8 9 0
      FREQUENCY   MEM LIST   DEL

    The dump shows this as F1 22.  +0012 is 0 for FREQUENCY mode and 1 for
    MEMORY CH mode.  The printed glyphs are not present in the stale menu text
    areas, so use the learned fixed keyboard labels and the real cursor bytes.
    """
    if menu_frame is None or len(menu_frame) < 181:
        return None
    if not (menu_frame[0] == 0xF1 and menu_frame[1] == 0x22):
        return None
    selected = _menu1_keypad_selected_index(menu_frame)
    if selected is None:
        return None
    mode = "memory" if int(menu_frame[12]) == 0x01 else "frequency"
    mode_title = "MEMORY CH" if mode == "memory" else "FREQUENCY"
    switch_label = "FREQUENCY" if mode == "memory" else "MEM CH"
    labels = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", switch_label, "MEM LIST", "DEL"]
    return {
        "visible": True,
        "type": "menu1_keypad",
        "age_s": age,
        "parent_num": 1,
        "title": "KEYPAD",
        "mode": mode,
        "mode_title": mode_title,
        "category": _setup_category(1),
        "selected_key": int(selected),
        "keypad": labels,
        "editing": True,
        "raw_value": "+0012 +0160..+0176: " + f"{int(menu_frame[12]):02x} / " + menu_frame[160:177].hex(" "),
    }


def _menu32_dtmf_edit_state_from_frame(menu_frame: Optional[bytes], age: float) -> Optional[dict]:
    """Return structured web state for menu 32's DTMF digit edit screen.

    Learned from save_20260506_092456_menu32-sub after pressing a DTMF MEMORY
    row:
      +0019       = current digit count or 0x80 transient phase byte; digits
                    are decoded from +0020..+0035 to avoid UI flicker
      +0020..+0035 = stored/edited DTMF digits, 0xCA empty edit cells
      +0160..+0179 = selected keypad button; 0x20 marks one of 20 keys:
                    1 2 3 4 5 / 6 7 8 9 0 / A B C D * # plus 4 tools
      +0180       = constant 0x40 marker seen on the edit page

    This is not the normal 3-row DTMF MEMORY list and should not be rendered as
    UNKNOWN/raw while the user is entering digits.
    """
    if menu_frame is None or len(menu_frame) < 181:
        return None
    if not (menu_frame[0] == 0xF3 and menu_frame[1] == 0x20):
        return None
    # Avoid collision with ordinary setup/secondary-display F3 20 frames.  The
    # real menu-32 DTMF edit page from save_20260506_092456_menu32-sub has
    # +0006=01 and +0012=06.  The broken menu-1 dump has F3 20 frames with the
    # same CA/20/40 cursor pattern but +0006=00/+0012=00, so they must not be
    # decoded as DTMF edit.
    if not (len(menu_frame) > 13 and int(menu_frame[6]) == 0x01 and int(menu_frame[12]) == 0x06):
        return None
    edit_cells = menu_frame[20:36]
    cursor_area = menu_frame[160:180]
    if 0xCA not in edit_cells:
        return None
    if 0x20 not in cursor_area:
        return None
    if menu_frame[180] != 0x40:
        return None

    length_raw = int(menu_frame[19])
    # +0019 toggles 0x80 while the radio updates the edit screen.  Do not use
    # that phase byte to hide the digits: count the visible cells from the
    # payload itself so the web UI stays stable and follows the real frame.
    observed_count = 0
    for b in edit_cells:
        if int(b) in (0xCA, 0x00, 0xFF):
            break
        observed_count += 1
    if 0 <= length_raw <= len(edit_cells):
        digit_count = max(length_raw, observed_count)
    else:
        digit_count = observed_count

    cursor_pos = list(cursor_area).index(0x20)

    global DTMF_EDIT_LAST_CURSOR_POS
    if 0 <= length_raw <= 16:
        # Stable radio frame: +0019 is the edit position.
        edit_cursor_pos = int(length_raw)
        DTMF_EDIT_LAST_CURSOR_POS = edit_cursor_pos
    elif int(length_raw) == 0x80:
        # Phase/blink frame from the radio.  Keep the last stable edit cursor
        # instead of jumping to digit_count/end-of-line on every other frame.
        if observed_count == 0 and cursor_pos == 0:
            edit_cursor_pos = 0
            DTMF_EDIT_LAST_CURSOR_POS = 0
        elif DTMF_EDIT_LAST_CURSOR_POS is not None:
            edit_cursor_pos = int(DTMF_EDIT_LAST_CURSOR_POS)
        else:
            edit_cursor_pos = min(16, max(0, digit_count))
    else:
        edit_cursor_pos = min(16, max(0, digit_count))
        DTMF_EDIT_LAST_CURSOR_POS = edit_cursor_pos

    digits = _decode_dtmf_digits(edit_cells[:digit_count], trim_placeholders=True)
    cells: List[str] = []
    for i, b in enumerate(edit_cells):
        if i < digit_count:
            cells.append(_decode_dtmf_digit_char(int(b)) or "")
        else:
            cells.append("")
    return {
        "visible": True,
        "type": "dtmf_edit",
        "age_s": age,
        "parent_num": 32,
        "title": "DTMF MEMORY",
        "category": _setup_category(32),
        "value": digits,
        "digit_count": digit_count,
        # selected_key is the 20-position keypad selection from +0160..+0179,
        # not the 16-character input cursor. Keep cursor_pos as an alias for
        # compatibility with older browser code.
        "selected_key": cursor_pos,
        "cursor_pos": cursor_pos,
        # +0019 is the radio-provided edit position while stable; 0x80 is
        # only a phase frame, so keep the cached stable position.
        "edit_cursor_pos": edit_cursor_pos,
        "edit_cursor_phase": int(length_raw),
        "keypad": ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "A", "B", "C", "D", "*", "#", "◀", "SP", "▶", "DEL"],
        "cells": cells,
        "raw_value": "+0019..+0035 +0160..+0179: " + menu_frame[19:36].hex(" ") + " / " + menu_frame[160:180].hex(" "),
    }

def _decode_menu32_dtmf_memory_text(raw: bytes) -> Optional[str]:
    """Decode one DTMF MEMORY slot text from the learned F3 20 list rows.

    Empty entries are not blank: the radio renders them as a run of dashes
    (0x4A), so keep that visible.  Stored numeric DTMF memories use raw digit
    bytes 0..9; labels such as APRS use compact LCD glyphs.
    """
    chars: List[str] = []
    for b in raw:
        if int(b) in (0xCA, 0x00, 0xFF):
            break
        ch = _decode_dtmf_digit_char(int(b))
        if not ch:
            return None
        chars.append(ch)
    text = "".join(chars)
    if text == "":
        return None
    # Keep a truly empty memory as the radio-rendered dashed field, but trim
    # only the dash fill after real stored digits/labels/spaces.  Do not trim
    # 0x64 spaces: they are intentional DTMF blank positions.
    if any(ch != "-" for ch in text):
        text = text.rstrip("-")
    return text if text != "" else None


def _decode_menu32_dtmf_memory_row(menu_frame: bytes, start: int, physical_row: int) -> Optional[dict]:
    """Decode one visible row from menu 32 DTMF MEMORY.

    Learned from save_20260506_090856_menu32:
      title at +0028..+0045 = "DTMF MEMORY"
      rows at +0060, +0095 and +0130
      row header: 64 NN 00 for slots 1..10
      value text: +3..+18, with 0x4a shown as '-'

    The bytes around +0165 track a fourth/lookahead/cache slot in the capture,
    but the selectable LCD list still uses three physical rows and cursor byte
    +0013 = 0..2, so the web GUI renders only the real selectable rows.
    """
    if menu_frame is None or start + 19 > len(menu_frame):
        return None
    hi = int(menu_frame[start])
    slot = int(menu_frame[start + 1])
    marker = int(menu_frame[start + 2])
    if hi != BLANK_DIGIT or not (1 <= slot <= 10) or marker != 0x00:
        return None
    value_raw = menu_frame[start + 3:start + 19]
    value = _decode_menu32_dtmf_memory_text(value_raw)
    value_source = "learned-f3" if value is not None else "unknown"
    if value is None:
        value = "UNKNOWN"
    return {
        "physical_row": physical_row,
        "row": physical_row,
        "num": str(slot),
        "key": str(slot),
        "text": value,
        "value": value,
        "value_source": value_source,
        "raw_value": f"+{start:04d}..+{start + 18:04d}: " + menu_frame[start:start + 19].hex(" "),
        "editing": False,
    }


def _menu32_dtmf_memory_state_from_frame(menu_frame: Optional[bytes], age: float) -> Optional[dict]:
    """Return structured web state for menu 32's DTMF MEMORY list page."""
    if menu_frame is None or len(menu_frame) < 149:
        return None
    if not (menu_frame[0] == 0xF3 and menu_frame[1] == 0x20):
        return None
    # Title line: 03 02 20 "DTMF MEMORY".
    if _decode_plain_menu_ascii(menu_frame[28:46]) != "DTMF MEMORY":
        return None

    rows: List[dict] = []
    for physical_row, start in enumerate((60, 95, 130)):
        row = _decode_menu32_dtmf_memory_row(menu_frame, start, physical_row)
        if row is not None:
            row["row"] = len(rows)
            rows.append(row)
    if not rows:
        return None

    selected_raw = int(menu_frame[13]) if len(menu_frame) > 13 else 0
    if not (0 <= selected_raw < len(rows)):
        selected_raw = 0
    selected_key = rows[selected_raw].get("key") if rows else None
    selected_value = rows[selected_raw].get("value") if rows else None
    return {
        "visible": True,
        "type": "submenu",
        "age_s": age,
        "parent_num": 32,
        "title": "DTMF MEMORY",
        "category": _setup_category(32),
        "selected_row": selected_raw,
        "selected_key": selected_key,
        "selected_value": selected_value,
        "editing": False,
        "rows": rows,
    }


def _decode_menu16_memory_freq(raw: bytes) -> Optional[str]:
    if raw is None or len(raw) < 6:
        return None
    digits: List[str] = []
    for b in raw[:6]:
        if not (0 <= int(b) <= 9):
            return None
        digits.append(str(int(b)))
    return "".join(digits[:3]) + "." + "".join(digits[3:6])


def _decode_menu16_edit_freq(raw: bytes) -> Optional[str]:
    if raw is None or len(raw) < 6:
        return None
    area = list(int(x) for x in raw)
    try:
        dot = area.index(0x4B)
    except ValueError:
        return _decode_menu16_memory_freq(raw[:6])
    left = [str(x) for x in area[:dot] if 0 <= x <= 9]
    right = [str(x) for x in area[dot + 1:] if 0 <= x <= 9]
    if not left or len(right) < 2:
        return None
    whole = "".join(left).lstrip()
    if whole == "":
        return None
    frac = "".join(right[:3])
    while len(frac) < 3:
        frac += "0"
    return f"{whole}.{frac[:3]}"


def _decode_menu16_memory_row(menu_frame: bytes, start: int, physical_row: int) -> Optional[dict]:
    if menu_frame is None or start + 25 > len(menu_frame):
        return None
    slot = int(menu_frame[start])
    if not (0 <= slot <= 9):
        return None
    freq = _decode_menu16_memory_freq(menu_frame[start + 1:start + 7])
    name = clean_ascii(menu_frame[start + 17:start + 25]).strip()
    is_empty = freq is None and not name
    # Empty memories are valid selectable rows on the radio.  Their frequency
    # field is filled with 0xCA/blank cells, so do not drop the row just because
    # the frequency decoder cannot form NNN.NNN.
    return {
        "physical_row": physical_row,
        "row": physical_row,
        "num": f"{slot:03d}",
        "slot": slot,
        "slot_digit": slot,
        "freq": freq or "",
        "text": name,
        "name": name,
        "value": freq or "",
        "empty": is_empty,
        "editing": False,
        "raw_value": f"+{start:04d}..+{start + 24:04d}: " + menu_frame[start:start + 25].hex(" "),
    }


def _menu16_memory_list_state_from_frame(menu_frame: Optional[bytes], age: float) -> Optional[dict]:
    if menu_frame is None or len(menu_frame) < 156:
        return None
    if not (menu_frame[0] == 0xF1 and menu_frame[1] == 0x23):
        return None

    rows: List[dict] = []
    for physical_row, start in enumerate((16, 51, 86, 121)):
        row = _decode_menu16_memory_row(menu_frame, start, physical_row)
        if row is not None:
            row["row"] = len(rows)
            rows.append(row)
    if not rows:
        return None

    # The row header byte is the low decimal digit only.  The tens digit is in
    # +0015 for this page; when visible rows wrap 9 -> 0, subsequent rows are
    # in the next decade.  Example from the dump:
    #   +0015=00, row digits 8 9 0 1 -> 008 009 010 011
    #   +0015=01, row digits 8 9 0 1 -> 018 019 020 021
    decade = int(menu_frame[15]) if len(menu_frame) > 15 and 0 <= int(menu_frame[15]) <= 99 else 0
    previous_digit: Optional[int] = None
    for row in rows:
        digit = int(row.get("slot_digit", 0))
        if previous_digit is not None and digit < previous_digit:
            decade += 1
        full_num = decade * 10 + digit
        row["num"] = f"{full_num:03d}"
        row["slot"] = full_num
        previous_digit = digit

    # The F1 23 MEMORY LIST page uses +0013 as the physical highlighted row
    # (0..3).  +0005 changes for other state/scroll reasons and is not the
    # LCD shadow/selection.  Using +0005 made the web highlight drift while
    # scrolling through memories.
    selected_raw = int(menu_frame[13]) if len(menu_frame) > 13 else 0
    if not (0 <= selected_raw < len(rows)):
        selected_raw = max(0, min(len(rows) - 1, selected_raw))
    return {
        "visible": True,
        "type": "memory_list",
        "age_s": age,
        "parent_num": 16,
        "title": "MEMORY LIST",
        "category": _setup_category(16),
        "selected_row": selected_raw,
        "editing": False,
        "rows": rows,
    }


def _menu16_memory_action_index_from_frame(menu_frame: Optional[bytes]) -> Optional[int]:
    """Return the real action-cursor index from the F1 23 action frame.

    In save_20260506_183715.zip the overlay is driven by the F1 23 frame:
      +0006 = 0x01          action overlay active
      +0005 = 0..3          cursor bucket
      +0007 = 0/1           upper-page/shift bit for the fifth item

    Forward capture shows DELETE as +0005=3,+0007=1.  When rotating back from
    DELETE, the radio can keep +0007=1 while decrementing +0005; treating
    +0007=1 as "DELETE always" makes the web UI look stuck for several BR_LEFT
    steps.  The correct visible index is therefore +0005 + (+0007 ? 1 : 0):
      0/0 MR, 1/0 WRITE, 2/0 EDIT, 3/0 GRP ON, 3/1 DELETE
      and on reverse 2/1 GRP ON, 1/1 EDIT, 0/1 WRITE.
    """
    try:
        if menu_frame is None or len(menu_frame) < 16:
            return None
        if not (menu_frame[0] == 0xF1 and menu_frame[1] == 0x23):
            return None
        if int(menu_frame[6]) != 0x01:
            return None
        base = int(menu_frame[5])
        shift = 1 if int(menu_frame[7]) == 0x01 else 0
        idx = base + shift
        if 0 <= idx <= 4:
            return idx
        return None
    except Exception:
        return None


def _menu16_memory_select_state_from_frame(menu_frame: Optional[bytes], age: float, data_frame: Optional[bytes] = None) -> Optional[dict]:
    """Decode the real action menu shown after pressing a memory in menu 16.

    Learned from save_20260506_171519_menu25.zip (old filename, actual menu 16
    memory submenu).  The screen is F1 23 with +0005=0 and +0007=0.  The frame
    still carries the surrounding memory rows, but the visible function menu is
    the action layer MR / WRITE / EDIT / GRP ON / DELETE.  Do not render this as
    another memory list.
    """
    if menu_frame is None or len(menu_frame) < 156:
        return None
    if not (menu_frame[0] == 0xF1 and menu_frame[1] == 0x23):
        return None
    selected_action_idx = _menu16_memory_action_index_from_frame(menu_frame)
    if selected_action_idx is None:
        return None

    memory_rows: List[dict] = []
    for physical_row, start in enumerate((16, 51, 86, 121)):
        row = _decode_menu16_memory_row(menu_frame, start, physical_row)
        if row is not None:
            row["row"] = len(memory_rows)
            memory_rows.append(row)
    if not memory_rows:
        return None

    decade = int(menu_frame[15]) if len(menu_frame) > 15 and 0 <= int(menu_frame[15]) <= 99 else 0
    previous_digit: Optional[int] = None
    for row in memory_rows:
        digit = int(row.get("slot_digit", 0))
        if previous_digit is not None and digit < previous_digit:
            decade += 1
        full_num = decade * 10 + digit
        row["num"] = f"{full_num:03d}"
        row["slot"] = full_num
        previous_digit = digit

    selected_mem_row = int(menu_frame[13]) if len(menu_frame) > 13 else 0
    if not (0 <= selected_mem_row < len(memory_rows)):
        selected_mem_row = max(0, min(len(memory_rows) - 1, selected_mem_row))
    selected_memory = memory_rows[selected_mem_row]
    selected_action = int(selected_action_idx)
    labels = ["MR", "WRITE", "EDIT", "GRP ON", "DELETE"]
    rows = [{"row": i, "num": "", "label": x, "text": x, "value": "", "editing": False} for i, x in enumerate(labels)]
    return {
        "visible": True,
        "type": "memory_select",
        "age_s": age,
        "parent_num": 16,
        "title": f"MEMORY {selected_memory.get('num', '')}".strip(),
        "memory_num": selected_memory.get("slot"),
        "memory_name": selected_memory.get("name") or "",
        "memory_freq": selected_memory.get("freq") or "",
        "category": _setup_category(16),
        "selected_row": selected_action,
        "selected_memory_row": selected_mem_row,
        "editing": False,
        "rows": rows,
        "memory_rows": memory_rows,
        "raw_value": "+0000..+0015: " + menu_frame[:16].hex(" "),
    }


def _menu16_memory_edit_state_from_frame(menu_frame: Optional[bytes], age: float) -> Optional[dict]:
    if menu_frame is None or len(menu_frame) < 107:
        return None
    if not (menu_frame[0] == 0xF1 and menu_frame[1] == 0x29):
        return None

    mem_no = int(menu_frame[16]) if 0 <= int(menu_frame[16]) <= 99 else 0
    rx_label = _compact_overlay_text(menu_frame[26:33]) or "RX FREQ"
    tx_label = _compact_overlay_text(menu_frame[48:56]) or "TX FREQ"
    tag_label = _compact_overlay_text(menu_frame[72:75]) or "TAG"
    scan_label = _compact_overlay_text(menu_frame[93:97]) or "SCAN"

    # F1 29 layout:
    #   +0016..+0024 = memory/list header, not the editable RX value.
    #   +0026..+0032 = "RX FREQ" label, +0036.. = actual RX frequency.
    #   +0048..+0054 = "TX FREQ" label, +0056.. = actual TX frequency.
    # The previous decoder used +0017..+0022 as RX and +0036.. as TX, so TX
    # showed the RX frequency and RX showed the stale/list frequency.
    rx_value = _decode_menu16_edit_freq(menu_frame[36:48]) or "UNKNOWN"
    tx_decoded = _decode_menu16_edit_freq(menu_frame[56:68])
    tx_value = tx_decoded if tx_decoded is not None else ""
    tag_value = (_compact_overlay_text(menu_frame[80:90]) or "")
    scan_value = (_compact_overlay_text(menu_frame[104:112]) or "")

    rows = [
        {"num": "", "label": rx_label, "text": rx_label, "value": rx_value, "editing": False},
        {"num": "", "label": tx_label, "text": tx_label, "value": tx_value, "editing": False},
        {"num": "", "label": tag_label, "text": tag_label, "value": tag_value, "editing": False},
        {"num": "", "label": scan_label, "text": scan_label, "value": scan_value, "editing": False},
    ]
    selected_raw = int(menu_frame[13]) if len(menu_frame) > 13 else 0
    if not (0 <= selected_raw < len(rows)):
        selected_raw = 0
    rows[selected_raw]["editing"] = True
    return {
        "visible": True,
        "type": "memory_edit",
        "age_s": age,
        "parent_num": 16,
        "title": f"MEMORY {mem_no:03d}",
        "memory_num": mem_no,
        "category": _setup_category(16),
        "selected_row": selected_raw,
        "editing": True,
        "rows": rows,
        "raw_value": "+0016..+0106: " + menu_frame[16:107].hex(" "),
    }


def _decode_menu16_freq_input_cells(raw: bytes) -> Tuple[str, List[dict], int]:
    """Decode the exact editable frequency row from the keypad screen.

    The old value decoder interpreted the row as a number and padded missing
    decimals with zeroes.  That breaks after DEL.  Here the 7 visible frequency
    cells are preserved as cells: digits, dot, or blank.
    """
    chars: List[str] = []
    for b in bytes(raw[:7]):
        b = int(b)
        if 0 <= b <= 9:
            chars.append(str(b))
        elif b == 0x4B:
            chars.append(".")
        elif b in (0x00, 0x20, 0x64, 0xCA):
            chars.append(" ")
        else:
            chars.append(" ")
    # Best available cursor from the radio row: after DEL there is usually a
    # blank cell at the current edit position.  If the row is full, keep the
    # cursor on the first digit when entering the field.
    # The radio edit cursor is on the current character position.  Do not place
    # it on the first trailing blank just because the tag is shorter than 16
    # cells; that makes existing tags look like the cursor is always at the end.
    cursor = 0
    text = "".join(chars).rstrip()
    cells = [{"text": ch, "cursor": i == cursor} for i, ch in enumerate(chars)]
    return text, cells, cursor


def _decode_menu16_tag_input_cells(raw: bytes) -> Tuple[str, List[dict], int]:
    """Decode the exact visible TAG edit row as 16 cells.

    Keep blank cells as cells so the cursor can be drawn on the real editable
    position and not on a collapsed string.
    """
    chars: List[str] = []
    for b in bytes(raw[:16]):
        b = int(b)
        if 0 <= b <= 9:
            chars.append(str(b))
        elif 0x0A <= b <= 0x23:
            chars.append(chr(ord("A") + b - 0x0A))
        elif 0x24 <= b <= 0x3D:
            chars.append(chr(ord("a") + b - 0x24))
        elif b in (0x00, 0x20, 0x40, 0x64, 0xCA):
            chars.append(" ")
        elif b == 0x4A:
            chars.append("-")
        elif b == 0x4B:
            chars.append(".")
        else:
            chars.append(" ")
    cursor = 0
    for i, ch in enumerate(chars):
        if ch == " ":
            cursor = i
            break
    text = "".join(chars).rstrip()
    cells = [{"text": ch, "cursor": i == cursor} for i, ch in enumerate(chars)]
    return text, cells, cursor


def _menu16_tag_keypad_selected_index(menu_frame: bytes) -> Optional[int]:
    """Decode TAG keypad selection marker from +0160..+0192.

    The TAG keyboard uses one 0x20 cursor marker over 33 logical keys:
      0..25  A..Z
      26..32 abc, 123, #%^, ->, SPACE, <-, DEL
    Other 0x40/0x10 bytes are fixed graphics/indicators and are ignored.
    """
    if menu_frame is None or len(menu_frame) < 193:
        return None
    area = menu_frame[160:193]
    # 0x20 is the normal highlight marker.  0x21 appears while selecting/
    # pressing the page modifier (seen on the abc modifier in the dump); treat it
    # as the same selected key instead of dropping the whole TAG keypad state.
    for marker in (0x20, 0x21):
        for i, b in enumerate(area):
            if int(b) == marker:
                return i
    return None


def _menu16_tag_keypad_layout(page: int, lower: bool = False) -> Tuple[List[str], List[dict]]:
    """TAG keyboard labels and physical rows.

    These four grids are the exact layouts requested from the radio/manual:

      A..M / N..Z / abc 123 #%^ <- SPACE -> DEL
      a..m / n..z / ABC 123 #%^ <- SPACE -> DEL
      1..0 - / : / ; ( ) ¥ & @ " . , ? ! ' / ABC #%^ <- SPACE -> DEL
      [ ] { } # % ^ _ + = * \ | / ~ < > $ ` . / ABC 123 <- SPACE -> DEL

    The rows below use the radio marker indices, so clicks still send the
    physical knob steps to the correct highlighted item.
    """
    alpha_rows = [
        {"cls": "row13", "idx": list(range(0, 13))},
        {"cls": "row13", "idx": list(range(13, 26))},
        {"cls": "row7", "idx": list(range(26, 33))},
    ]

    if int(page) == 0x04:
        if lower:
            return (
                list("abcdefghijklm")
                + list("nopqrstuvwxyz")
                + ["ABC", "123", "#%^", "<-", "SPACE", "->", "DEL"],
                alpha_rows,
            )
        return (
            list("ABCDEFGHIJKLM")
            + list("NOPQRSTUVWXYZ")
            + ["abc", "123", "#%^", "<-", "SPACE", "->", "DEL"],
            alpha_rows,
        )

    if int(page) == 0x02:
        labels = [""] * 33
        row1 = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "-", "/", ":"]
        row2 = [";", "(", ")", "¥", "&", "@", "\"", ".", ",", "?", "!", "'"]
        # Active 123 page: keep the physical blank where 123 would be.
        # Otherwise #%^ / <- / SPACE / -> / DEL are all one slot too early.
        row3 = ["ABC", "", "#%^", "<-", "SPACE", "->", "DEL"]
        for i, v in enumerate(row1):
            labels[i] = v
        for i, v in enumerate(row2, start=13):
            labels[i] = v
        for i, v in zip(range(25, 32), row3):
            labels[i] = v
        return labels, [
            {"cls": "row13", "idx": list(range(0, 13))},
            {"cls": "row12", "idx": list(range(13, 25))},
            {"cls": "row7", "idx": list(range(25, 32))},
        ]


    if int(page) == 0x03:
        labels = [""] * 33
        row1 = ["[", "]", "{", "}", "#", "%", "^", "_", "+", "=", "*", "\\", "|"]
        row2 = ["~", "<", ">", "$", "`", "."]
        # The symbol page has a real blank position where the active #%^
        # modifier would be.  Without that blank, <- was one slot too early and
        # every following command was shifted.
        row3 = ["ABC", "123", "", "<-", "SPACE", "->", "DEL"]
        for i, v in enumerate(row1):
            labels[i] = v
        for i, v in enumerate(row2, start=13):
            labels[i] = v
        for i, v in zip(range(19, 26), row3):
            labels[i] = v
        return labels, [
            {"cls": "row13", "idx": list(range(0, 13))},
            {"cls": "row6", "idx": list(range(13, 19))},
            {"cls": "row7", "idx": list(range(19, 26))},
        ]

    return (
        list("ABCDEFGHIJKLM")
        + list("NOPQRSTUVWXYZ")
        + ["abc", "123", "#%^", "<-", "SPACE", "->", "DEL"],
        alpha_rows,
    )


def _menu16_tag_keypad_labels(page: int) -> List[str]:
    labels, _rows = _menu16_tag_keypad_layout(page)
    return labels


def _menu16_memory_tag_keypad_state_from_frame(menu_frame: Optional[bytes], age: float) -> Optional[dict]:
    """Decode the TAG alphanumeric keypad opened from menu 16 EDIT.

    Learned from save_20260506_201814_menu25.zip:
      frame header F1 23
      +0006 = 0x02
      +0013 = 0x02          TAG row selected
      +0002 = 0x04/0x02/0x03 keyboard page
      +0160..+0192          0x20 marks selected key among 33 positions

    Web renders the requested 3 rows:
      A..M
      N..Z
      abc 123 #%^ -> SPACE <- DEL
    """
    if menu_frame is None or len(menu_frame) < 193:
        return None
    if not (menu_frame[0] == 0xF1 and menu_frame[1] == 0x23):
        return None
    if int(menu_frame[6]) != 0x02:
        return None
    selected_row = int(menu_frame[13]) if len(menu_frame) > 13 else 0
    if selected_row != 2:
        return None

    selected_key = _menu16_tag_keypad_selected_index(menu_frame)
    if selected_key is None or not (0 <= int(selected_key) <= 32):
        return None

    mem_no = int(menu_frame[16]) if 0 <= int(menu_frame[16]) <= 99 else 0
    rx_label = _compact_overlay_text(menu_frame[26:33]) or "RX FREQ"
    tx_label = _compact_overlay_text(menu_frame[48:56]) or "TX FREQ"
    tag_label = _compact_overlay_text(menu_frame[72:75]) or "TAG"
    scan_label = _compact_overlay_text(menu_frame[93:97]) or "SCAN"

    rx_value = _decode_menu16_edit_freq(menu_frame[36:48]) or "UNKNOWN"
    tx_decoded = _decode_menu16_edit_freq(menu_frame[56:68])
    tx_value = tx_decoded if tx_decoded is not None else ""
    tag_value = (_compact_overlay_text(menu_frame[80:90]) or "")
    scan_value = (_compact_overlay_text(menu_frame[104:112]) or "")

    rows = [
        {"num": "", "label": rx_label, "text": rx_label, "value": rx_value, "editing": False},
        {"num": "", "label": tx_label, "text": tx_label, "value": tx_value, "editing": False},
        {"num": "", "label": tag_label, "text": tag_label, "value": tag_value, "editing": True},
        {"num": "", "label": scan_label, "text": scan_label, "value": scan_value, "editing": False},
    ]

    input_value, input_cells, input_cursor_pos = _decode_menu16_tag_input_cells(menu_frame[122:138])
    labels, keypad_rows = _menu16_tag_keypad_layout(int(menu_frame[2]))
    # Do not allow a stale/invalid marker outside the currently visible key
    # list to produce a broken render.
    if selected_key >= len(labels):
        return None
    return {
        "visible": True,
        "type": "memory_tag_keypad",
        "age_s": age,
        "parent_num": 16,
        "title": f"MEMORY {mem_no:03d}",
        "memory_num": mem_no,
        "category": _setup_category(16),
        "selected_row": 2,
        "target_label": tag_label or "TAG",
        "input_value": input_value,
        "current_value": input_value,
        "input_cells": input_cells,
        "input_cursor_pos": int(input_cursor_pos),
        "selected_key": int(selected_key),
        "keypad": labels,
        "keypad_rows": keypad_rows,
        "keyboard_page": int(menu_frame[2]),
        "editing": True,
        "rows": rows,
        "raw_value": "+0160..+0192: " + menu_frame[160:193].hex(" "),
    }


def _menu16_memory_freq_keypad_state_from_frame(menu_frame: Optional[bytes], age: float) -> Optional[dict]:
    """Decode the RX/TX frequency numeric keypad opened from menu 16 EDIT.

    Learned from save_20260506_201713_menu25.zip:
      frame header F1 23
      +0006 = 0x02          frequency keypad active
      +0013 = edit row      0 RX FREQ / 1 TX FREQ
      +0160..+0169          0x20 marks selected digit key 1..0
      +0179 = 0x21          DEL selected

    The keypad layout is:
      1 2 3 4 5
      6 7 8 9 0
              DEL
    """
    if menu_frame is None or len(menu_frame) < 180:
        return None
    if not (menu_frame[0] == 0xF1 and menu_frame[1] == 0x23):
        return None
    if int(menu_frame[6]) != 0x02:
        return None

    mem_no = int(menu_frame[16]) if 0 <= int(menu_frame[16]) <= 99 else 0
    rx_label = _compact_overlay_text(menu_frame[26:33]) or "RX FREQ"
    tx_label = _compact_overlay_text(menu_frame[48:56]) or "TX FREQ"
    tag_label = _compact_overlay_text(menu_frame[72:75]) or "TAG"
    scan_label = _compact_overlay_text(menu_frame[93:97]) or "SCAN"

    rx_value = _decode_menu16_edit_freq(menu_frame[36:48]) or "UNKNOWN"
    tx_decoded = _decode_menu16_edit_freq(menu_frame[56:68])
    tx_value = tx_decoded if tx_decoded is not None else ""
    tag_value = (_compact_overlay_text(menu_frame[80:90]) or "")
    scan_value = (_compact_overlay_text(menu_frame[104:112]) or "")

    rows = [
        {"num": "", "label": rx_label, "text": rx_label, "value": rx_value, "editing": False},
        {"num": "", "label": tx_label, "text": tx_label, "value": tx_value, "editing": False},
        {"num": "", "label": tag_label, "text": tag_label, "value": tag_value, "editing": False},
        {"num": "", "label": scan_label, "text": scan_label, "value": scan_value, "editing": False},
    ]
    selected_row = int(menu_frame[13]) if len(menu_frame) > 13 else 0
    if not (0 <= selected_row < len(rows)):
        selected_row = 0
    rows[selected_row]["editing"] = True

    # The keypad screen has a dedicated value row:
    #   +0120/+0121 = memory number digits
    #   +0122..+0128 = exact visible frequency cells currently edited.
    # Keep it as cells so DEL/blank positions are not collapsed or padded.
    input_value, input_cells, input_cursor_pos = _decode_menu16_freq_input_cells(menu_frame[122:129])

    selected_key: Optional[int] = None
    key_area = menu_frame[160:170]
    if 0x20 in key_area:
        selected_key = list(key_area).index(0x20)
    elif int(menu_frame[179]) == 0x21:
        selected_key = 10
    if selected_key is None:
        return None

    labels = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "DEL"]
    return {
        "visible": True,
        "type": "memory_freq_keypad",
        "age_s": age,
        "parent_num": 16,
        "title": f"MEMORY {mem_no:03d}",
        "memory_num": mem_no,
        "category": _setup_category(16),
        "selected_row": selected_row,
        "target_label": rows[selected_row].get("label") or "FREQ",
        "input_value": input_value,
        "current_value": input_value,
        "input_cells": input_cells,
        "input_cursor_pos": int(input_cursor_pos),
        "selected_key": int(selected_key),
        "keypad": labels,
        "editing": True,
        "rows": rows,
        "raw_value": "+0160..+0179: " + menu_frame[160:180].hex(" "),
    }


def _menu16_primary_state_from_frame(frame: Optional[bytes], age: float = 0.0, data_frame: Optional[bytes] = None) -> Optional[dict]:
    """Decode menu 16 from the primary display stream.

    MEMORY LIST is unusual: when it opens, the radio stops using the usual
    F3 20 display2 menu layer and paints the page on the primary display as
    F1 23 (list) or F1 29 (edit).  Keep this separated from the normal VFO
    decoder so those frames do not get interpreted as random frequencies.
    """
    if frame is None or len(frame) < RX_BLOCK_LEN:
        return None
    if frame[0] != 0xF1:
        return None
    if frame[1] == 0x23:
        tag_keypad = _menu16_memory_tag_keypad_state_from_frame(frame, age)
        if tag_keypad is not None:
            return tag_keypad
        freq_keypad = _menu16_memory_freq_keypad_state_from_frame(frame, age)
        if freq_keypad is not None:
            return freq_keypad
        select_state = _menu16_memory_select_state_from_frame(frame, age, data_frame)
        if select_state is not None:
            return select_state
        return _menu16_memory_list_state_from_frame(frame, age)
    if frame[1] == 0x29:
        return _menu16_memory_edit_state_from_frame(frame, age)
    return None


def _setup_value_raw_from_menu_frame(menu_frame: Optional[bytes]) -> str:
    if menu_frame is None or len(menu_frame) < 40:
        return ""
    return "+0026..+0033: " + menu_frame[26:34].hex(" ")


def _setup_rows_from_frame(frame: bytes) -> List[dict]:
    """Decode only the real visible setup-menu rows from the frame.

    The payload often keeps stale text/number fragments in unused areas.  The
    real list rows are consecutive menu numbers.  When a third slot contains an
    older item, e.g. 29, 30, 28 at the AUDIO boundary, drop the stale slot
    instead of rendering a bogus third row.
    """
    raw_rows: List[dict] = []
    for physical_row, start in enumerate((60, 95, 130)):
        if start + 3 >= len(frame):
            continue
        num = _setup_item_number_from_pair(frame[start], frame[start + 1])
        if num is None or num not in SETUP_MENU_ITEMS:
            continue
        raw_rows.append({
            "physical_row": physical_row,
            "num": num,
            "text": SETUP_MENU_ITEMS.get(num, f"ITEM {num:02d}"),
            "category": _setup_category(num),
            "hint": _setup_item_hint(num),
        })

    rows: List[dict] = []
    previous: Optional[int] = None
    for r in raw_rows:
        n = int(r["num"])
        if previous is not None and n <= previous:
            # Non-monotonic means stale data in an unused row.
            break
        if previous is not None and n != previous + 1:
            # Also avoid jumping across unrelated cached screens.
            break
        r = dict(r)
        r["row"] = len(rows)
        rows.append(r)
        previous = n
    return rows


def _setup_selected_row(frame: bytes, rows: List[dict]) -> int:
    # +0013 is the physical visible row cursor in the general setup captures: 0,1,2.
    b = frame[13] if len(frame) > 13 else 0
    if 0 <= b <= 2:
        for i, row in enumerate(rows):
            if row.get("physical_row") == b:
                return i
        return min(b, max(0, len(rows) - 1))
    # Fallback: some rows use attribute 0x30/0x31 on highlighted/action rows.
    for physical_row, start in enumerate((60, 95, 130)):
        if start + 2 < len(frame) and frame[start + 2] in (0x30, 0x31):
            for i, row in enumerate(rows):
                if row.get("physical_row") == physical_row:
                    return i
            return min(physical_row, max(0, len(rows) - 1))
    return 0


def _setup_value_bar_selected(frame: Optional[bytes]) -> bool:
    """Return True only when the radio says the setup value/footer bar is selected.

    Real-panel diff when entering the value bar from the menu list:
      +0026: 0x04 -> 0x06

    Do not infer this from the presence of a decoded value.  The web GUI must
    mirror the radio display state, not invent a local selected/editing state.
    """
    if frame is None or len(frame) <= 26:
        return False
    return frame[26] == 0x06


def _looks_like_quick_menu(frame: bytes) -> bool:
    if not _is_live_menu_frame(frame):
        return False
    # F1 21 is the Scope display2 frame.  It reuses/stales the same text area as
    # the quick menu, so accepting it here produces a broken quick menu while
    # rotating the scope marker.
    if len(frame) >= 2 and frame[0] == 0xF1 and frame[1] in (0x21, 0x25):
        return False
    area = frame[60:155]
    # F1 60/F3 20 secondary-display/status frames can contain one stale text
    # fragment such as CLONERX without being the real quick menu.  Require at
    # least two learned quick-menu labels before painting the web quick menu.
    markers = (b"M->V", b"RPT SFT", b"SQL TYP", b"DTMF", b"CLONERX")
    hits = sum(1 for m in markers if m in area)
    return hits >= 2


def _scope_resize_bars(raw: List[int], target: int) -> List[int]:
    vals = [max(0, min(10, int(x))) for x in (raw or [])]
    target = max(5, int(target or 23))
    if len(vals) == target:
        return vals
    if len(vals) > target:
        # Keep the center portion so the fixed center marker stays aligned.
        start = max(0, (len(vals) - target) // 2)
        return vals[start:start + target]
    pad_left = (target - len(vals)) // 2
    pad_right = target - len(vals) - pad_left
    return [0] * pad_left + vals + [0] * pad_right



def _scope_interval_label_from_frame(menu_frame: Optional[bytes]) -> str:
    """Decode the real BAND SCOPE dot interval from the F1 21 frame.

    Learned from real captures:
      save_20260507_131553.zip       F1 21 +0006 = 01 -> 10K
      save_20260507_133131_12.5k.zip F1 21 +0006 = 02 -> 12.5K
      save_20260507_132927_25k.zip   F1 21 +0006 = 05 -> 25K
      save_20260507_133820.zip       F1 21 +0006 = 08 -> 50K

    Do not infer this from band/frequency/mode: the radio sends the value.
    Unknown byte values are left blank instead of inventing a number.
    """
    if menu_frame is None or len(menu_frame) <= 6:
        return ""
    code = int(menu_frame[6])
    known = {
        0x01: "10K",
        0x02: "12.5K",
        0x05: "25K",
        0x08: "50K",
    }
    return known.get(code, "")


def _pmg_bar_level(raw: int) -> int:
    raw = max(0, int(raw or 0))
    if raw <= 10:
        return raw
    return max(1, min(10, raw // 3))


def _pmg_state_from_frame(menu_frame: Optional[bytes], age: float, data_frame: Optional[bytes]) -> Optional[dict]:
    """Decode PMG display2 frame F1 25.

    Grounded on the captured PMG dumps:
      +0004        = selected PMG channel P1..P5
      +0008..+0012 = secondary/peak bar slots for P1..P5
      +0013..+0017 = main bar slots for P1..P5
      +0018..+0022 = sparse present/latched channel flags in some frames
      F1 00        = carries the current selected PMG frequency.

    This matches the observed sequences:
      - save_20260507_160610_pmg.zip         -> P3 activity at +0010/+0015
      - save_20260507_164332_menu25.zip      -> P2 and P4 activity
      - save_20260507_181135_menu25.zip      -> P1 then P2, then P1 while P2 falls
    """
    if menu_frame is None or len(menu_frame) < RX_BLOCK_LEN:
        return None
    if not (menu_frame[0] == 0xF1 and menu_frame[1] == 0x25):
        return None

    selected = int(menu_frame[4])
    if not (1 <= selected <= 5):
        selected = 1

    main = "LEFT"
    if data_frame is not None and len(data_frame) >= 4:
        main = "LEFT" if data_frame[3] == 0x02 else "RIGHT" if data_frame[3] == 0x01 else "LEFT"
    side = _side_for_web(data_frame, SIDE_LEFT if main == "LEFT" else SIDE_RIGHT) if data_frame is not None else {}

    auto_mode = bool(int(menu_frame[3]) & 0x08)
    mode = "AUTO" if auto_mode else "MANUAL"

    flags = list(menu_frame[18:23]) if len(menu_frame) >= 23 else [0, 0, 0, 0, 0]
    while len(flags) < 5:
        flags.append(0)

    # In the PMG dumps these bytes toggle between 01 01 01 01 01 and sparse
    # states such as 00 01 00 00 00 while the radio remains on the same PMG
    # page.  Treating them as registration flags makes the web PMG graph flicker
    # between five slots and one slot.  They are activity/phase flags, not a
    # reliable registration mask, so keep all five PMG slots stable.
    present = set(range(1, 6))

    channels: List[dict] = []
    strongest_idx = 0
    strongest_raw = -1
    for idx in range(1, 6):
        peak_raw = int(menu_frame[7 + idx]) if len(menu_frame) > (7 + idx) else 0
        main_raw = int(menu_frame[12 + idx]) if len(menu_frame) > (12 + idx) else 0
        if peak_raw > 0 or main_raw > 0:
            present.add(idx)
        if main_raw > strongest_raw:
            strongest_raw = main_raw
            strongest_idx = idx
        channels.append({
            "index": idx,
            "label": f"P{idx}",
            "registered": idx in present,
            "bar": _pmg_bar_level(main_raw),
            "shadow": _pmg_bar_level(peak_raw),
            "raw": main_raw,
            "peak_raw": peak_raw,
            "recent": False,
            "receiving": False,
        })

    if not present:
        present = set(range(1, 6))
    present.add(selected)
    for ch in channels:
        ch["registered"] = int(ch["index"]) in present

    if strongest_raw > 0:
        for ch in channels:
            raw = int(ch.get("raw") or 0)
            idx = int(ch["index"])
            if raw > 0:
                ch["receiving"] = idx == strongest_idx
                ch["recent"] = idx != strongest_idx

    return {
        "visible": True,
        "type": "pmg",
        "age_s": age,
        "selected": selected,
        "mode": mode,
        "auto": auto_mode,
        "main": main,
        "source": side.get("source") or "PMG",
        "freq": side.get("freq") or "---.---",
        "rx_mode": side.get("mode") or "",
        "shift": side.get("shift") or "",
        "tone": side.get("tone") or "",
        "channels": channels,
        "raw_value": "+0004/+0008..+0012/+0013..+0017/+0018..+0022: " + f"{int(menu_frame[4]):02x} / " + menu_frame[8:13].hex(" ") + " / " + menu_frame[13:18].hex(" ") + " / " + menu_frame[18:23].hex(" "),
    }


def _scope_graph_raw_from_frame(menu_frame: bytes) -> List[int]:
    """Extract raw scope bar heights from F1 21.

    In save_20260507_112254_scope.zip, the active scope screen is F1 21 and
    the signal bar graph is in the graphic/control area before the stale quick
    menu text begins.  Values are 0..10; anything outside that range is ignored.
    """
    if not menu_frame:
        return []
    vals: List[int] = []
    for b in menu_frame[20:60]:
        x = int(b)
        vals.append(x if 0 <= x <= 10 else 0)
    return vals


def _scope_state_from_frame(menu_frame: Optional[bytes], age: float, data_frame: Optional[bytes]) -> Optional[dict]:
    if menu_frame is None or len(menu_frame) < RX_BLOCK_LEN:
        return None
    if not (menu_frame[0] == 0xF1 and menu_frame[1] == 0x21):
        return None
    # Scope frames use +0004 as marker position.  The previous build accepted
    # only 0x1d/0x1e/0x1f; the right knob actually moves this byte across a
    # wider range, e.g. 0x15..0x27 in save_20260507_113443.zip.
    marker_raw = int(menu_frame[4])
    if not (0x10 <= marker_raw <= 0x40):
        return None

    main = "LEFT"
    if data_frame is not None and len(data_frame) >= 4:
        main = "LEFT" if data_frame[3] == 0x02 else "RIGHT" if data_frame[3] == 0x01 else "LEFT"
    side = _side_for_web(data_frame, SIDE_LEFT if main == "LEFT" else SIDE_RIGHT) if data_frame is not None else {}
    source = str(side.get("source") or "")
    is_memory = source.startswith("MEM")
    raw = _scope_graph_raw_from_frame(menu_frame)

    # The session may later overwrite width/channels in WebContext from menu 04.
    channels = 23 if is_memory else 47
    bars = _scope_resize_bars(raw, channels)

    # In the captured WIDE VFO screen, marker_raw 0x1e is the center marker.
    # Convert raw marker byte to a visual index; the decorator clamps/recenters
    # again if WIDE/NARROW or memory mode changes the channel count.
    marker_index = max(0, min(channels - 1, marker_raw - 0x07))

    return {
        "visible": True,
        "type": "scope",
        "age_s": age,
        "main": main,
        "source": source or ("MEM" if is_memory else "VFO"),
        "freq": side.get("freq") or "---.---",
        "mode": side.get("mode") or "",
        "shift": side.get("shift") or "",
        "tone": side.get("tone") or "",
        "raw_bars": raw,
        "bars": bars,
        "channels": channels,
        "width": "WIDE",
        "memory_mode": bool(is_memory),
        "marker_raw": marker_raw,
        "marker_index": marker_index,
        "interval": _scope_interval_label_from_frame(menu_frame),
        "raw_value": "+0004/+0006/+0020..+0059: " + f"{marker_raw:02x} / {int(menu_frame[6]):02x} / " + menu_frame[20:60].hex(" "),
    }


def _menu_state_for_web(menu_frame: Optional[bytes], menu_ts: Optional[float], data_frame: Optional[bytes]) -> dict:
    if not menu_frame or menu_ts is None:
        return {"visible": False}
    age = max(0.0, time.time() - menu_ts)
    if not _is_live_menu_frame(menu_frame):
        return {"visible": False, "age_s": age}

    pmg_state = _pmg_state_from_frame(menu_frame, age, data_frame)
    if pmg_state is not None:
        return pmg_state

    scope_state = _scope_state_from_frame(menu_frame, age, data_frame)
    if scope_state is not None:
        return scope_state

    menu1_keypad = _menu1_keypad_state_from_frame(menu_frame, age)
    if menu1_keypad is not None:
        return menu1_keypad

    # Menu 16 MEMORY LIST lives on the primary-display family F1 23/F1 29,
    # not on the normal F3 20 setup layer.  Decode it before the stale-F1 guard.
    menu16_primary = _menu16_primary_state_from_frame(menu_frame, age, data_frame)
    if menu16_primary is not None:
        return menu16_primary

    # Stale F1 60 frames are sent even on the normal VFO screen.  They are only
    # allowed to paint the web menu when the normal display path confirms that
    # the radio is in an F3 menu/status screen.
    if menu_frame[0] == 0xF1 and menu_frame[1] == 0x60 and not _is_menu_context(data_frame):
        return {"visible": False, "age_s": age}

    # Short timeouts keep the web LCD from sticking on the menu after F/BACK.
    if age > (0.90 if menu_frame[0] == 0xF1 else 0.55):
        return {"visible": False, "age_s": age}

    software_version = _software_version_state_from_frame(menu_frame, age)
    if software_version is not None:
        return software_version

    text_submenu = _text_setup_submenu_state_from_frame(menu_frame, age)
    if text_submenu is not None:
        return text_submenu

    menu25_submenu = _menu25_submenu_state_from_frame(menu_frame, age)
    if menu25_submenu is not None:
        return menu25_submenu

    menu32_edit = _menu32_dtmf_edit_state_from_frame(menu_frame, age)
    if menu32_edit is not None:
        return menu32_edit

    menu32_submenu = _menu32_dtmf_memory_state_from_frame(menu_frame, age)
    if menu32_submenu is not None:
        return menu32_submenu

    setup_rows = _setup_rows_from_frame(menu_frame)
    if len(setup_rows) >= 1:
        sel = _setup_selected_row(menu_frame, setup_rows)
        selected_num = setup_rows[sel].get("num") if setup_rows else None
        value = _setup_value_from_menu_frame(selected_num, menu_frame)
        value_source = "learned-f3" if value is not None else "unknown"
        if value is None:
            value = _setup_value_from_radio_guess(selected_num, data_frame)
            value_source = "decoded" if value is not None else "unknown"
        selected_row = setup_rows[sel] if setup_rows and 0 <= sel < len(setup_rows) else {}
        physical_row = int(selected_row.get("physical_row", 0) or 0)
        raw_start = (60, 95, 130)[physical_row] if 0 <= physical_row <= 2 else 60
        raw_value = _setup_value_raw_from_menu_frame(menu_frame) or (f"+{raw_start:04d}: " + menu_frame[raw_start:raw_start + 24].hex(" "))
        if selected_num in SETUP_ENTER_ITEMS:
            # These items open a second-level page, so the main setup list must
            # show an enter/submenu indicator instead of UNKNOWN or a guessed
            # value.  The actual row values are decoded only on the dedicated
            # submenu frame from the radio.
            value = "›"
            value_source = "submenu"
            raw_value = ""
        elif selected_num in SETUP_INERT_BLANK_ITEMS:
            # Menus 47..57 are intentionally left inert until their real radio
            # frames have been learned.  Do not show UNKNOWN/raw placeholders
            # underneath the list and do not highlight a non-clickable value bar.
            value = ""
            value_source = "blank"
            raw_value = ""
        elif value is None:
            value = "UNKNOWN"
        value_selected = False if selected_num in SETUP_INERT_BLANK_ITEMS else _setup_value_bar_selected(menu_frame)
        return {
            "visible": True,
            "type": "full",
            "age_s": age,
            "selected_row": sel,
            "selected_num": selected_num,
            "category": _setup_category(selected_num),
            "rows": setup_rows,
            "no_action_items": sorted(SETUP_INERT_BLANK_ITEMS),
            "value": value,
            "value_source": value_source,
            "raw_value": raw_value,
            "value_selected": value_selected,
            "editing": value_selected,
        }

    # Quick menu: render fixed soft-key labels.  The payload contains extra
    # value/status fragments after some labels, so never use raw cell text here.
    if _looks_like_quick_menu(menu_frame):
        selected_raw = int(menu_frame[13]) if len(menu_frame) > 13 else 1
        # Captures and real-panel observation show 1 = second cell (RPT SFT).
        # Do not subtract 1.
        selected = selected_raw if 0 <= selected_raw <= 8 else 1
        cell_texts = _quick_menu_cells_from_frame(menu_frame)
        selected_label = cell_texts[selected].get("text", "") if 0 <= selected < len(cell_texts) else ""
        assignment_text = _quick_assignment_text(menu_frame)
        return {
            "visible": True,
            "type": "quick",
            "age_s": age,
            "selected_index": selected,
            "cells": cell_texts,
            "footer": _quick_menu_footer(selected_label, menu_frame, data_frame),
            "footer_selected": _quick_menu_footer_selected(menu_frame),
            "assignment": bool(assignment_text),
        }

    return {"visible": False, "age_s": age}


def _demo_frame() -> bytes:
    f = bytearray(RX_FRAME_LEN)
    f[0] = 0xF1
    for k in range(1, 5):
        f[k * RX_BLOCK_LEN] = 0xFF
    f[3] = 0x02
    f[6], f[7] = 0x08, 0x0A
    f[8], f[9] = 0x09, 0x09
    f[10], f[11] = 0x40, 0x40
    f[17] = 0x20
    f[96:104] = bytes([1, 4, 6, 5, 2, 0, BLANK_DIGIT, BLANK_DIGIT])
    f[108:116] = bytes([4, 4, 6, 5, 0, 0, BLANK_DIGIT, BLANK_DIGIT])
    f[192] = 0x10
    return bytes(f)



class BasePttController:
    def set_ptt(self, active: bool) -> None:
        raise NotImplementedError

    def status(self) -> dict:
        return {"mode": "none", "active": False, "last_error": ""}

    def shutdown(self) -> None:
        try:
            self.set_ptt(False)
        except Exception:
            pass


class SerialMicPttController(BasePttController):
    """Old front/microphone PTT path through the emulated panel frame."""

    def __init__(self, tx: Optional[PanelTx]):
        self.tx = tx
        self.active = False
        self.last_error = ""

    def set_ptt(self, active: bool) -> None:
        if self.tx is None:
            self.last_error = "Serial TX not available"
            raise RuntimeError(self.last_error)
        if active:
            self.tx.named_hold("web_audio_ptt", "serial_mic_ptt", COMMANDS["mic_ptt_hold"])
            self.active = True
            self.last_error = ""
        else:
            self.tx.clear_named_hold("web_audio_ptt")
            self.active = False

    def status(self) -> dict:
        return {"mode": "serial-mic", "active": self.active, "last_error": self.last_error}


def _sys_read(path: str) -> str:
    try:
        with open(path, "r", encoding="ascii", errors="ignore") as f:
            return f.read().strip()
    except Exception:
        return ""


def _usb_key_from_sys_path(path: str) -> Optional[str]:
    """Return the physical USB device key, e.g. '1-1.4', from a /sys path."""
    try:
        parts = os.path.realpath(path).split(os.sep)
    except Exception:
        return None
    # USB interface components are like 1-1.4:1.0; the parent device is 1-1.4.
    for part in reversed(parts):
        m = re.match(r"^(\d+-\d+(?:\.\d+)*)(?::\d+\.\d+)?$", part)
        if m:
            return m.group(1)
    return None


def _alsa_card_from_device(device: str) -> Optional[str]:
    # Accept hw:0,0, plughw:0,0, hw:CARD=Device,DEV=0 and plughw:CARD=Device,DEV=0.
    m = re.search(r"(?:^|:)(?:plug)?hw:(\d+)(?:,|$)", device or "")
    if m:
        return m.group(1)
    m = re.search(r"CARD=([A-Za-z0-9_\-]+)", device or "")
    if m:
        name = m.group(1)
        for card in glob.glob("/sys/class/sound/card*"):
            cid = _sys_read(os.path.join(card, "id"))
            if cid == name:
                return os.path.basename(card).replace("card", "")
    return None


def _hidraw_candidates() -> List[dict]:
    out = []
    for hp in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        dev = os.path.realpath(os.path.join(hp, "device"))
        # Walk upwards to find USB VID/PID/product.
        cur = dev
        vid = pid = product = ""
        for _ in range(8):
            vid = _sys_read(os.path.join(cur, "idVendor")) or vid
            pid = _sys_read(os.path.join(cur, "idProduct")) or pid
            product = _sys_read(os.path.join(cur, "product")) or product
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        name = os.path.basename(hp)
        out.append({
            "hidraw": "/dev/" + name,
            "sys": hp,
            "usb_key": _usb_key_from_sys_path(dev),
            "vid": vid.lower(),
            "pid": pid.lower(),
            "product": product,
        })
    return out


def find_cm108_hidraw(audio_device: str) -> Tuple[Optional[str], str, List[dict]]:
    """Best-effort mapping from an ALSA card/device to its C-Media HID GPIO device."""
    candidates = _hidraw_candidates()
    card = _alsa_card_from_device(audio_device)
    card_key = None
    if card is not None:
        card_dev = f"/sys/class/sound/card{card}/device"
        if os.path.exists(card_dev):
            card_key = _usb_key_from_sys_path(card_dev)

    cmedia = [c for c in candidates if c.get("vid") in ("0d8c", "0d8c".lower())]
    if card_key:
        matched = [c for c in cmedia if c.get("usb_key") == card_key]
        if len(matched) == 1:
            return matched[0]["hidraw"], f"auto: ALSA card {card} USB {card_key}", candidates
        if len(matched) > 1:
            return matched[0]["hidraw"], f"auto: multiple HID devices on the same card, using {matched[0]['hidraw']}", candidates
    if len(cmedia) == 1:
        return cmedia[0]["hidraw"], "auto: single C-Media hidraw device found", candidates
    if cmedia:
        return None, "multiple C-Media hidraw devices found; use --cm108-hidraw /dev/hidrawN", candidates
    if len(candidates) == 1:
        return candidates[0]["hidraw"], "auto: single hidraw device found, C-Media not verified", candidates
    return None, "no C-Media hidraw device found", candidates


class Cm108PttController(BasePttController):
    """PTT through a CM108/CM119 GPIO HID report, compatible with Direwolf/AllStar-style fobs."""

    def __init__(self, audio_device: str, hidraw: str = "auto", gpio: int = 3, invert: bool = False):
        self.audio_device = audio_device
        self.hidraw_arg = hidraw or "auto"
        self.hidraw: Optional[str] = None if self.hidraw_arg == "auto" else self.hidraw_arg
        self.gpio = max(1, min(8, int(gpio)))
        self.invert = bool(invert)
        self.active = False
        self.last_error = ""
        self.last_info = ""
        self.candidates: List[dict] = []
        if self.hidraw is None:
            self.hidraw, self.last_info, self.candidates = find_cm108_hidraw(audio_device)
        else:
            self.last_info = "manual"

    def _write(self, active: bool) -> None:
        if not self.hidraw:
            cand = "; ".join(f"{c['hidraw']} vid={c.get('vid') or '?'} pid={c.get('pid') or '?'} usb={c.get('usb_key') or '?'} {c.get('product') or ''}" for c in self.candidates)
            self.last_error = (self.last_info or "hidraw not found") + (f"; candidates: {cand}" if cand else "")
            raise RuntimeError(self.last_error)
        mask = 1 << (self.gpio - 1)
        asserted = bool(active) ^ self.invert
        data = mask if asserted else 0
        report = bytes([0x00, 0x00, data & 0xFF, mask & 0xFF, 0x00])
        try:
            with open(self.hidraw, "wb", buffering=0) as f:
                n = f.write(report)
            if n != len(report):
                raise OSError(f"short write {n}/{len(report)}")
        except PermissionError as e:
            self.last_error = f"permission denied on {self.hidraw}: add a udev rule or try sudo ({e})"
            raise RuntimeError(self.last_error)
        except Exception as e:
            self.last_error = f"CM108 PTT write failed su {self.hidraw}: {e}"
            raise RuntimeError(self.last_error)
        self.last_error = ""

    def set_ptt(self, active: bool) -> None:
        self._write(active)
        self.active = bool(active)

    def status(self) -> dict:
        return {
            "mode": "cm108",
            "active": self.active,
            "audio_device": self.audio_device,
            "hidraw": self.hidraw,
            "hidraw_source": self.last_info,
            "gpio": self.gpio,
            "invert": self.invert,
            "last_error": self.last_error,
        }



def _setup_choice_current(indices: Dict[int, int], num: int, fallback: str = "") -> str:
    choices = SETUP_CHOICE_LISTS.get(num) or []
    if not choices:
        return fallback
    idx = indices.get(num, SETUP_DEFAULT_VALUE_INDEX.get(num, 0)) % len(choices)
    return choices[idx]


def _display_settings_from_indices(indices: Dict[int, int]) -> dict:
    contrast_text = _setup_choice_current(indices, 3, "5")
    try:
        contrast = int(contrast_text)
    except Exception:
        contrast = 5
    return {
        "lcd_dimmer": _setup_choice_current(indices, 2, "MID"),
        "lcd_contrast": max(1, min(9, contrast)),
        "s_meter_symbol": _setup_choice_current(indices, 5, "BARS"),
        "backlight_color": _setup_choice_current(indices, 6, "AMBER"),
    }



def _strict_clean_menu_line(raw: bytes) -> str:
    s = []
    last_space = False
    for b in raw:
        if b in (0x00, 0x64, 0xFF):
            ch = " "
        elif 32 <= b <= 126:
            ch = chr(b)
        else:
            ch = " "
        if ch == " ":
            if not last_space:
                s.append(ch)
            last_space = True
        else:
            s.append(ch)
            last_space = False
    return "".join(s).strip()


def _strict_ascii_runs_for_web(frame: bytes, start: int = 0, end: int = 220) -> List[str]:
    lines: List[str] = []
    # Prefer the known LCD text region, but keep this generic for unknown F3 screens.
    for off, raw in ascii_runs(frame[start:end], min_len=2):
        abs_off = start + off
        txt = _strict_clean_menu_line(raw)
        if txt and not txt.isspace():
            # Drop obvious garbage made of a single repeated char.
            if len(set(txt.replace(" ", ""))) == 1 and len(txt.replace(" ", "")) > 4:
                continue
            lines.append(f"+{abs_off:04d} {txt}")
    # Some menu/value screens use fixed rows with non-ASCII separators.
    if not lines:
        for a, b in ((60, 92), (95, 127), (130, 160), (32, 60)):
            line = _strict_clean_menu_line(frame[a:b])
            if line:
                lines.append(line)
    # De-duplicate while preserving order.
    out: List[str] = []
    seen = set()
    for line in lines:
        if line not in seen:
            seen.add(line)
            out.append(line)
    return out[:6]


def _strict_raw_menu_state_from_frame(frame: Optional[bytes]) -> dict:
    if not frame or len(frame) < RX_BLOCK_LEN:
        return {"visible": False}
    if frame[0] != 0xF3:
        return {"visible": False}
    # F3 02/04/13 are menu/value/edit related in captures. If later we find
    # another F3 type, this still shows raw readable content instead of faking a value.
    lines = _strict_ascii_runs_for_web(frame, 0, RX_BLOCK_LEN)
    header = frame[:16].hex(" ")
    return {
        "visible": True,
        "type": "raw",
        "header": header,
        "lines": lines or ["unknown F3 display"],
    }



# ---------------------------------------------------------------------------
# GPIO CH2/pin6 power-on replay, embedded from the validated v9-hiz script.
# ---------------------------------------------------------------------------
START_LEVEL = 1
EVENT_COUNT = 44474
DURATION_US = 1877874
SOURCE_START_MS = -9.853615151209056
THRESHOLD_V = 1.65
TAIL_START_INDEX = 7436      # long HIGH delay at 226.060 ms before the continuous tail
TAIL_EDGE_INDEX = 7437       # continuous tail would start here, with a HIGH->LOW edge
TAIL_EDGE_US = 1452096       # time from first LOW->HIGH to the continuous tail
CRITICAL_END_US = 1452096    # default: replay exactly up to here and leave the line HIGH
_DELAYS_Z64 = """
eNrt3U9OE1EcwPGZFijljygXkBBCkLBwoTslhiggcaHGROM1vII77+HOjQchBIkXYO/KAzjoTDJpqJQU2vm9+SwmHykUSIl835t5
vDnfWcjyLPt79IqjWxwLxdEpH5sv7dScK4735dv15+W153TKozfk+V//8/zqY2drn6d6bGbI5/tcHI9fZdmXb3l2t/y41fL9V1kd
d0b8+EHnaq/ZZW8vlN/PzAjfx3zt9VvljTr4c65+HkteH5IN8tf3TMeoYyTD+vSNjlHHSMZ1X8eoYyQDu6tj1DGSgX2hY9QxkuZj
OqZjJOn6mI7pGEmaj1HHSLo+pmPUMZLmYzqmYyTZgutj9d+R83qmZyQZdF6mZ3pGkilcL9MzPSPJaczPVnVMx7w+JANfN9MxHfP6
kDQf0zEdi+G97N99AMnUjfr/dOlTV8eoYyTD+rtjPkYdIxnXza6OUcdIxnVdx6hjJCOvV9Qx6hjJwB7pGHWMZGCPrfOgjpEM7Lb5
GHWMpHUeOqZjJOn6mI7pGEle0z0ds3/REPPS5dLqWMzs30P7IbE5nlnnQfMxkoG1nwd1jGRkN3SMOkYysPbzoI6RjOxzHaOOkQzs
qXUe1DGSgd0xH6OOkQys/TyoYyTt56FjOkaS1nnomI6R5HU9sc6DOkYysFvmY9QxkoG1LxV1jKTrYzqmYySpYzqmYyTpvi3UMZLW
eegYdYyk+7bomI6RTNsm3E/8oPjHSuHsBL7exddYLI68fDsvH7uwX3u8Xz7eveLzVc/rZe4LfxvmtY5VP4uqY+5TT7IpHpqP0XyM
ZGDt50EdI2mdh47pGEla56FjOkaS/g6aOkayTe7rGHWMZGB/WudBHSNpnYeO6RhJTsU1HaOOkfR30DqmYyRpnYeO6RhJWudBHSPp
76B1jDpGMoTrOkYdIxnYDw3s2NLA+3RMx0hymI90bCzHub+XjpFkmus8zMd0jGyiKY9b7eehYzpGktYrTrNj44yzUh1v6RhJ+3mY
j+kY2ezzfc77uf+YjukYSU7T42DrPOrjp7w4+sXRM/7SMZKtdcd8jDpG6wAZ2HUdo46RDOzHETt2E+OlcTs2+HtVz/SMJCsfBpqX
6Zme0Xk/MoX9PfRMz0hymA+KgdRK4ewExlt6pmckeVuuOd9IPXPe0PlDJuC+nlHPSCbgSz2jnpFMwFPrQahn1hWSCbhtfkY9I5mA
9/WMekYyAd/pGfXM+kAyAZ/oGfWMZAL+sB6EekYyAbfMz9iCnjlfSLpPjJ7R/IxkhPHogZ5Rz0gm4KGecYI9c96P5G15Yj0Izc9I
Tvn69E2MV+0PQj0jaT2InrWtZ93asei8IckG+VrPaH5GOh+YwPhzV8+oZyQT8Mx6EOOxK8ZjekYygpvmZzQ/I90HLwE39Ix6RjIB
9/TMdVk9I5mAR3pGPSOd/0tA+4NQz0i6X0x7etbmfQf1jKT9QczPzM9I5w05Gd/qGfWMZAI+a3jP6uOivHT5ksf7xdFryPjqD5Vw
uNI=
"""


def load_delays() -> List[int]:
    raw = zlib.decompress(base64.b64decode(_DELAYS_Z64))
    return list(struct.unpack('<' + 'I' * (len(raw) // 4), raw))


def precise_sleep_us(us: int) -> None:
    """Sleep with decent precision for millisecond pauses; busy-wait only in the last 300 us."""
    if us <= 0:
        return
    target = time.monotonic_ns() + us * 1000
    if us > 800:
        time.sleep((us - 300) / 1_000_000.0)
    while time.monotonic_ns() < target:
        pass


def print_summary(delays: List[int]) -> None:
    print(f"[data] fronti incorporati: {EVENT_COUNT}")
    print(f"[data] durata CSV intera:  {DURATION_US/1000.0:.3f} ms")
    print(f"[data] start CSV:          {SOURCE_START_MS:.9f} ms")
    print(f"[data] original threshold: {THRESHOLD_V:.3f} V")
    print(f"[data] first segment:      HIGH for {delays[0]/1000.0:.3f} ms")
    print(f"[data] critical startup:   up to {CRITICAL_END_US/1000.0:.3f} ms, then line left HIGH")
    print(f"[data] CSV continuous tail: from {TAIL_EDGE_US/1000.0:.3f} to {DURATION_US/1000.0:.3f} ms")
    print(f"[data] min/max delay:      {min(delays)} us / {max(delays)} us")


def make_wave(pi, gpio: int, levels_and_delays: List[Tuple[int, int]], invert: bool) -> int:
    mask = 1 << gpio
    pulses = []
    for level, delay in levels_and_delays:
        out_level = level ^ (1 if invert else 0)
        on = mask if out_level else 0
        off = 0 if out_level else mask
        pulses.append(pigpio.pulse(on, off, max(1, int(delay))))
    pi.wave_clear()
    pi.wave_add_generic(pulses)
    wid = pi.wave_create()
    if wid < 0:
        raise RuntimeError(f"wave_create fallita, codice {wid}, pulse={len(pulses)}")
    return wid


def send_small_wave(pi, gpio: int, seq: List[Tuple[int, int]], invert: bool, verbose: bool = False) -> None:
    if not seq:
        return
    wid = make_wave(pi, gpio, seq, invert)
    try:
        if verbose:
            dur = sum(d for _, d in seq)
            print(f"[wave] {len(seq)} pulse, {dur} us")
        pi.wave_send_once(wid)
        while pi.wave_tx_busy():
            time.sleep(0.0005)
    finally:
        try:
            pi.wave_delete(wid)
        except Exception:
            pass


def play_edge_stream_until(pi, gpio: int, delays: List[int], stop_us: int, args) -> int:
    """Replay delays from t=0 up to stop_us.

    Long delays are handled with write+sleep, fast sections with small DMA
    waves. When stop_us is reached, the next edge is not generated:
    the line remains at the current level, which is HIGH for the critical sequence.
    """
    level = START_LEVEL
    t_us = 0
    i = 0
    small: List[Tuple[int, int]] = []
    small_start_t = 0

    def flush_small():
        nonlocal small
        if small:
            send_small_wave(pi, gpio, small, args.invert, args.verbose)
            small = []

    while i < len(delays) and t_us < stop_us:
        delay = int(delays[i])
        remaining = stop_us - t_us
        if delay > remaining:
            # Final partial segment: keep the level and stop without toggling.
            flush_small()
            pi.write(gpio, level ^ (1 if args.invert else 0))
            if args.verbose:
                print(f"[hold] level {level} for {remaining} us, stopping without edge")
            precise_sleep_us(remaining)
            t_us += remaining
            break

        # For a long pause, do not waste pigpio wave resources on it.
        if delay >= args.long_delay_us:
            flush_small()
            pi.write(gpio, level ^ (1 if args.invert else 0))
            if args.verbose:
                print(f"[sleep] t={t_us/1000:.3f} ms livello={level} delay={delay} us")
            precise_sleep_us(delay)
        else:
            if not small:
                small_start_t = t_us
            small.append((level, delay))
            if len(small) >= args.max_small_pulses:
                flush_small()

        t_us += delay
        level ^= 1
        i += 1

    flush_small()
    # Leave the current level consistent. For mode=startup we force it HIGH later.
    pi.write(gpio, level ^ (1 if args.invert else 0))
    return t_us


def build_uart_wave_for_bytes(data: bytes, bit_us: int = 2, gap_us: int = 800) -> List[Tuple[int, int]]:
    """Build an idle-HIGH TTL UART 8N1 waveform as level/duration pairs.
    Used only for the optional tail, not for the critical CSV section.
    """
    bits: List[int] = []
    for b in data:
        bits.append(0)  # start
        for n in range(8):
            bits.append((b >> n) & 1)
        bits.append(1)  # stop
    bits.extend([1] * max(1, gap_us // bit_us))
    # Compress consecutive identical bits.
    out: List[Tuple[int, int]] = []
    cur = bits[0]
    count = 0
    for bit in bits:
        if bit == cur:
            count += 1
        else:
            out.append((cur, count * bit_us))
            cur = bit
            count = 1
    out.append((cur, count * bit_us))
    return out


# First frame of the continuous tail decoded from the CSV, 210 bytes, byte +15 = 0x30.
TAIL_FRAME0_HEX = (
    "80 00 00 00 00 00 00 00 00 00 00 00 00 7c 7b 30 "
    "00 00 00 0f "
    + "00 " * 42 + "01 02 " + "00 " * (210 - 16 - 4 - 42 - 2)
).strip()


def tail_frame0() -> bytes:
    bs = bytes.fromhex(TAIL_FRAME0_HEX)
    if len(bs) != 210:
        raise RuntimeError(f"TAIL_FRAME0 len={len(bs)} instead of 210")
    return bs


def play_tail_repeated(pi, gpio: int, args) -> None:
    frame = tail_frame0()
    seq = build_uart_wave_for_bytes(frame, bit_us=2, gap_us=args.tail_gap_us)
    # The single-frame wave is small; repeat it from Python. It is not bit-identical
    # to the CSV tail, but avoids the 37000 edges that saturate pigpio CBs.
    repeats = max(1, int(args.tail_ms / ((4200 + args.tail_gap_us) / 1000.0)))
    print(f"[tail] repeating UART-like GPIO frame: {repeats} times, gap={args.tail_gap_us} us")
    for k in range(repeats):
        send_small_wave(pi, gpio, seq, args.invert, False)


def release_gpio_hiz(pi, gpio: int) -> None:
    """Release the GPIO as if disconnected: input, pull-up/down disabled."""
    try:
        pi.set_pull_up_down(gpio, pigpio.PUD_OFF)
    except Exception:
        pass
    pi.set_mode(gpio, pigpio.INPUT)


def play(args) -> int:
    delays = load_delays()
    print_summary(delays)
    if args.analyze:
        print("[stop] analyze only")
        return 0
    if pigpio is None:
        print("[error] pigpio module not found: ../venv/bin/pip install pigpio", file=sys.stderr)
        return 2
    pi = pigpio.pi()
    if not pi.connected:
        print("[error] pigpiod not reachable. Start it with: sudo pigpiod -s 1", file=sys.stderr)
        return 2

    try:
        pi.set_mode(args.gpio, pigpio.OUTPUT)
        pi.wave_clear()
        # Before t=0 the CSV is LOW, then the first event is LOW->HIGH.
        pi.write(args.gpio, 0 if not args.invert else 1)
        time.sleep(args.pre_idle_ms / 1000.0)

        if args.mode == "startup":
            stop_us = CRITICAL_END_US
        elif args.mode == "early":
            stop_us = 801041
        elif args.mode == "custom":
            stop_us = int(args.stop_ms * 1000)
        else:
            print(f"[error] unknown mode: {args.mode}", file=sys.stderr)
            return 2

        print("[play] START NOW: t=0 = first LOW->HIGH on CH2")
        start = time.monotonic_ns()
        elapsed_us = play_edge_stream_until(pi, args.gpio, delays, stop_us, args)

        # For startup, do not generate the tail HIGH->LOW edge: stay in idle HIGH.
        if args.leave_high:
            pi.write(args.gpio, 1 if not args.invert else 0)

        if args.tail_repeated:
            play_tail_repeated(pi, args.gpio, args)
            if args.leave_high:
                pi.write(args.gpio, 1 if not args.invert else 0)

        real_ms = (time.monotonic_ns() - start) / 1_000_000.0
        print(f"[play] done: replayed timeline up to {elapsed_us/1000.0:.3f} ms; real time {real_ms:.3f} ms")
        if not args.tail_repeated:
            print("[note] final continuous tail NOT sent: use --tail-repeated to send it in compact form")

        if args.release_input:
            release_gpio_hiz(pi, args.gpio)
            print(f"[gpio] GPIO{args.gpio} released: INPUT, pull-up/down OFF = high impedance")
        else:
            print(f"[gpio] GPIO{args.gpio} left as OUTPUT level {'HIGH' if args.leave_high else 'current'}")
        return 0
    finally:
        try:
            pi.wave_clear()
            # If an exception occurs, still try to release the pin when requested.
            if 'args' in locals() and getattr(args, 'release_input', False):
                try:
                    release_gpio_hiz(pi, args.gpio)
                except Exception:
                    pass
            pi.stop()
        except Exception:
            pass



def gpio_write_once(gpio: int, level: int) -> Tuple[bool, str]:
    """Write a BCM GPIO level through pigpio and return (ok, message)."""
    if gpio is None:
        return True, "GPIO not configured"
    if pigpio is None:
        return False, "pigpio module not found: ../venv/bin/pip install pigpio"
    pi = pigpio.pi()
    if not pi.connected:
        return False, "pigpiod not reachable. Start it with: sudo pigpiod -s 1"
    try:
        pi.set_mode(int(gpio), pigpio.OUTPUT)
        pi.write(int(gpio), 1 if int(level) else 0)
        return True, f"GPIO{gpio}={'HIGH' if int(level) else 'LOW'}"
    finally:
        try:
            pi.stop()
        except Exception:
            pass

def run_embedded_power_replay(gpio: int, tail_repeated: bool = True, verbose: bool = False) -> Tuple[bool, str]:
    """Run the known-good CH2 GPIO replay used to wake the Free RIG body."""
    ns = argparse.Namespace(
        gpio=int(gpio),
        mode="startup",
        stop_ms=1452.096,
        long_delay_us=10000,
        max_small_pulses=600,
        pre_idle_ms=50.0,
        invert=False,
        leave_high=True,
        release_input=True,
        tail_repeated=bool(tail_repeated),
        tail_ms=430.0,
        tail_gap_us=800,
        verbose=bool(verbose),
        analyze=False,
    )
    rc = play(ns)
    if rc == 0:
        return True, "sequenza GPIO CH2 completata"
    return False, f"sequenza GPIO CH2 fallita, rc={rc}"


class WebContext:
    def __init__(self, tx: Optional[PanelTx], rx: Optional[BodyRx], demo: bool = False, audio: Optional[AudioStreamer] = None, tx_audio: Optional[TxAudioSink] = None, ptt: Optional[BasePttController] = None, decode_enabled: bool = False, power_gpio: Optional[int] = 18, uart_select_gpio: Optional[int] = 23, radio_start_on: bool = False, rx_power_timeout_s: float = 1.2):
        self.tx = tx
        self.rx = rx
        self.demo = demo
        self.audio = audio
        self.tx_audio = tx_audio
        self.ptt = ptt
        self.decode_enabled = bool(decode_enabled)
        self.power_gpio = power_gpio
        self.uart_select_gpio = uart_select_gpio
        # radio_powered is now a watchdog state derived from live RX frames.
        # radio_start_on is only an initial hint; as soon as RX stops/starts,
        # _refresh_radio_power_from_rx() becomes authoritative.
        self.radio_powered = bool(radio_start_on or demo)
        self.powering_on = False
        self.rx_power_timeout_s = max(0.2, float(rx_power_timeout_s or 1.2))
        self.power_lock = threading.Lock()
        self.power_message = "radio on" if self.radio_powered else "radio off"
        self._uart_usb_selected: Optional[bool] = None
        self._power_watchdog_stop = threading.Event()
        self._web_html = build_web_html(self.decode_enabled)
        self.started = time.time()
        self.ptt_latched = False
        self.tx_audio_active = False
        self.tx_audio_lock = threading.Lock()
        # v73: menu values are not tracked locally anymore.  The web display
        # must show only values decoded from radio frames; otherwise UNKNOWN/raw.
        self.last_visible_menu: dict = {"visible": False}
        self.last_visible_menu_at: float = 0.0
        # Menu 16 has one radio layer that did not expose readable MR/WRITE/
        # DELETE labels in the available frames.  Keep a short web-side action
        # latch after the user presses a memory row, while the underlying radio
        # still receives the physical BR_PRESS.
        self.memory_action_menu: Optional[dict] = None
        self.memory_action_menu_at: float = 0.0
        # Last real menu-16 action overlay frame.  The overlay itself is F1 23,
        # but while the user rotates the knob the radio often updates only the
        # primary F1 00 cursor byte (+0015).  Keep this cached only while that
        # F1 00 byte continues to be one of the learned action states.
        self.last_memory_select_menu: dict = {"visible": False}
        self.last_memory_select_at: float = 0.0
        # MEMORY EDIT frequency keypad cursor state.  The F1 23 keypad frame
        # exposes the value row and selected key, but not a simple absolute
        # cursor byte.  Derive the cursor only from changes in the value row so
        # merely rotating across 1..0/DEL does not move it.
        self.last_memory_freq_cells: Optional[List[str]] = None
        self.last_memory_freq_cursor_pos: Optional[int] = None
        self.last_memory_freq_key: Optional[int] = None
        self.last_memory_tag_cells: Optional[List[str]] = None
        self.last_memory_tag_cursor_pos: Optional[int] = None
        self.last_memory_tag_key: Optional[int] = None
        # The alphabet TAG page uses the same F1 23 page id for ABC/abc; the
        # frame carries marker positions but not printed glyphs.  Track the
        # manual's ABC/abc toggle when BR_PRESS is sent on that modifier.
        self.memory_tag_alpha_lower: bool = False
        # Menu 1 KEY PAD input digits are not present as readable text in the
        # F1 22 frame captured so far.  Track only digits pressed through this
        # web UI so the popup title mirrors what the user is entering.
        self.menu1_inputs: dict = {"frequency": "", "memory": ""}
        # DISPLAY settings remembered during the current process.
        # They are updated only when the radio frame exposes a learned value.
        self.session_display_settings: dict = {}
        if self.uart_select_gpio is not None:
            ok, msg = gpio_write_once(int(self.uart_select_gpio), 1 if self.radio_powered else 0)
            if ok:
                self._uart_usb_selected = bool(self.radio_powered)
            self.power_message = msg if ok else msg
            if ok and self.radio_powered:
                print(f"[power] radio already on: S GPIO{self.uart_select_gpio}=HIGH, TX USB selected")
            elif ok:
                print(f"[power] radio off: S GPIO{self.uart_select_gpio}=LOW, GPIO replay path selected")
            else:
                print(f"[power] WARNING: failed to set S GPIO{self.uart_select_gpio}: {msg}", file=sys.stderr)
        if self.tx is not None:
            self.tx.set_enabled(bool(self.radio_powered or self.demo), "initial radio state")
        self._power_watchdog_thread = threading.Thread(target=self._power_watchdog_loop, name="power-watchdog", daemon=True)
        self._power_watchdog_thread.start()

    def web_html(self) -> str:
        return self._web_html

    def display_settings(self) -> dict:
        # Defaults are used only until the current session has seen real values
        # from the radio menu frames.  Once a learned value is read, it remains
        # active after leaving the menu instead of snapping back to defaults.
        out = {"lcd_dimmer": "MID", "lcd_contrast": 5, "s_meter_symbol": "BARS", "backlight_color": "AMBER", "band_scope": "WIDE"}
        try:
            for k, v in (self.session_display_settings or {}).items():
                if k in out and v not in (None, ""):
                    out[k] = v
        except Exception:
            pass
        return out

    def _remember_display_setting_from_menu(self, menu: dict) -> None:
        try:
            if not menu or menu.get("type") != "full":
                return
            if menu.get("value_source") not in ("learned-f3", "decoded"):
                return
            num = menu.get("selected_num")
            val = str(menu.get("value") or "").upper()
            if num == 2 and val in ("MAX", "MID", "OFF"):
                self.session_display_settings["lcd_dimmer"] = val
            elif num == 3:
                iv = int(val)
                if 1 <= iv <= 9:
                    self.session_display_settings["lcd_contrast"] = iv
            elif num == 4 and val in ("WIDE", "NARROW"):
                self.session_display_settings["band_scope"] = val
            elif num == 5 and val in ("BARS", "SCALE", "CONTINUE", "FULL SIZE"):
                self.session_display_settings["s_meter_symbol"] = val
            elif num == 6 and val in ("AMBER", "WHITE"):
                self.session_display_settings["backlight_color"] = val
        except Exception:
            pass

    def current_frame(self) -> Tuple[bytes, Optional[float], dict]:
        if self.demo or self.rx is None:
            return _demo_frame(), time.time(), {"frames": 0, "data": 0, "sync_loss": 0, "menu_ignored": 0}
        frame, ts, seen, data_seen, sync_losses = self.rx.snapshot()
        counters = self.rx.counters()
        if frame is None:
            return _demo_frame(), None, counters
        return frame, ts, counters

    def state(self) -> dict:
        self._refresh_radio_power_from_rx()
        frame, ts, counters = self.current_frame()
        # Absolute guard for the web path too: if any PMG-family frame ever
        # reaches current_frame(), never decode it as a normal VFO/MEM LCD.
        try:
            if self.rx is not None and BodyRx.is_pmg_family_frame(frame):
                clean_frame, clean_ts, _seen, _data_seen, _sync = self.rx.snapshot()
                if clean_frame is not None and not BodyRx.is_pmg_family_frame(clean_frame):
                    frame, ts = clean_frame, clean_ts
                else:
                    frame, ts = _demo_frame(), None
        except Exception:
            pass
        sm_style = _smeter_symbol_from_display_frame(frame)
        if sm_style in ("BARS", "SCALE", "CONTINUE", "FULL SIZE"):
            self.session_display_settings["s_meter_symbol"] = sm_style
        age = None if ts is None else max(0.0, time.time() - ts)
        main = "LEFT" if frame[3] == 0x02 else "RIGHT" if frame[3] == 0x01 else "UNKNOWN"
        human = decode_display_human(frame, raw=False) if self.decode_enabled else ""
        activity = _activity_for_web(frame, main)
        menu = self.current_menu_state(frame)
        # Do not decode MEMORY LIST confirmation text from the normal F1 00
        # display snapshot here.  In the menu 16 captures those bytes remain in
        # the normal display buffer even when no confirmation dialog is active,
        # which caused a DELETE?/OVER WRITE? popup to stick over every screen.
        # Menu-16 popups will be re-enabled only from a positively identified
        # active popup frame, not from the stale data snapshot.
        confirm_overlay = setup_confirm_overlay_from_frame(frame)
        if confirm_overlay is None:
            mem_confirm = memory_channel_confirm_overlay_from_frame(frame)
            if mem_confirm is not None and (
                menu.get("type") in ("memory_list", "memory_select", "memory_edit", "memory_freq_keypad", "memory_tag_keypad")
                or self.last_visible_menu.get("type") in ("memory_list", "memory_select", "memory_edit", "memory_freq_keypad", "memory_tag_keypad")
            ):
                confirm_overlay = mem_confirm
        overlay_text = "" if confirm_overlay else lcd_overlay_text_from_frame(frame)
        overlay_latched = False
        if not confirm_overlay and not overlay_text and self.rx is not None:
            recent_text, recent_ts = self.rx.recent_overlay()
            if recent_text:
                overlay_text = recent_text
                overlay_latched = True
        overlay = confirm_overlay or {"active": bool(overlay_text), "kind": "text" if overlay_text else "none", "text": overlay_text, "latched": overlay_latched}
        save_st = SAVE_RECORDER.status() if globals().get("SAVE_RECORDER") is not None else {"active": False}
        power_st = self.power_status()
        out = {"build_id": BUILD_ID, "demo": self.demo, "age_s": age if age is not None else 9999.0, "counters": counters, "main": main, "left": _side_for_web(frame, SIDE_LEFT), "right": _side_for_web(frame, SIDE_RIGHT), "activity": activity, "mute": overlay_text == "MUTE", "overlay": overlay, "menu": menu, "display_settings": self.display_settings(), "ptt_latched": self.ptt_latched, "tx_audio_active": self.tx_audio_active, "save": save_st, "human": human}
        out.update(power_st)
        return out

    def _copy_menu(self, menu: dict) -> dict:
        try:
            return json.loads(json.dumps(menu))
        except Exception:
            return dict(menu)

    def _value_for_setup_item(self, num: Optional[int], data_frame: Optional[bytes] = None) -> str:
        if num is None:
            return ""
        radio_value = _setup_value_from_radio_guess(num, data_frame)
        return radio_value if radio_value is not None else "UNKNOWN"

    def _stabilize_memory_freq_keypad_cursor(self, menu: dict) -> dict:
        """Make the frequency value cursor follow radio-frame changes only.

        The keypad selection at +0160..+0169/+0179 is not the same thing as the
        frequency cursor.  Do not move the frequency cursor just because the user
        rotates over another keypad item.  When the actual value row changes,
        infer the new cursor from the changed cell:
          * delete -> cursor on the blanked cell
          * digit write -> cursor advances to the next editable digit
        """
        if menu.get("type") != "memory_freq_keypad":
            return menu
        cells = menu.get("input_cells") or []
        current = [str((c or {}).get("text", " "))[:1] if isinstance(c, dict) else str(c)[:1] for c in cells]
        if not current:
            return menu

        def _next_editable(pos: int) -> int:
            j = max(0, pos + 1)
            while j < len(current) and current[j] == ".":
                j += 1
            return j if j < len(current) else max(0, min(len(current) - 1, pos))

        cursor = menu.get("input_cursor_pos")
        try:
            cursor = int(cursor)
        except Exception:
            cursor = 0

        previous = self.last_memory_freq_cells
        if previous is not None and len(previous) == len(current):
            changed = [i for i, (a, b) in enumerate(zip(previous, current)) if a != b]
            if changed:
                # Prefer the first actually changed visual cell.
                i = changed[0]
                if current[i] == " ":
                    cursor = i
                else:
                    cursor = _next_editable(i)
            elif self.last_memory_freq_cursor_pos is not None:
                # Rotating around keypad digits only changes selected_key; keep
                # the frequency cursor exactly where the last value-frame update
                # left it.
                cursor = int(self.last_memory_freq_cursor_pos)

        cursor = max(0, min(len(current) - 1, int(cursor)))
        for i, cell in enumerate(cells):
            if isinstance(cell, dict):
                cell["cursor"] = (i == cursor)
        menu["input_cells"] = cells
        menu["input_cursor_pos"] = cursor
        self.last_memory_freq_cells = list(current)
        self.last_memory_freq_cursor_pos = cursor
        try:
            self.last_memory_freq_key = int(menu.get("selected_key"))
        except Exception:
            self.last_memory_freq_key = None
        return menu

    def _stabilize_memory_tag_keypad_cursor(self, menu: dict) -> dict:
        """Make the TAG cursor follow only real changes in the TAG value row."""
        if menu.get("type") != "memory_tag_keypad":
            return menu
        cells = menu.get("input_cells") or []
        current = [str((c or {}).get("text", " "))[:1] if isinstance(c, dict) else str(c)[:1] for c in cells]
        if not current:
            return menu

        cursor = menu.get("input_cursor_pos")
        try:
            cursor = int(cursor)
        except Exception:
            cursor = 0

        previous = self.last_memory_tag_cells
        if previous is not None and len(previous) == len(current):
            changed = [i for i, (a, b) in enumerate(zip(previous, current)) if a != b]
            if changed:
                i = changed[0]
                if current[i] == " ":
                    cursor = i
                else:
                    cursor = min(len(current) - 1, i + 1)
            elif self.last_memory_tag_cursor_pos is not None:
                cursor = int(self.last_memory_tag_cursor_pos)

        cursor = max(0, min(len(current) - 1, int(cursor)))
        for i, cell in enumerate(cells):
            if isinstance(cell, dict):
                cell["cursor"] = (i == cursor)
        menu["input_cells"] = cells
        menu["input_cursor_pos"] = cursor
        self.last_memory_tag_cells = list(current)
        self.last_memory_tag_cursor_pos = cursor
        try:
            self.last_memory_tag_key = int(menu.get("selected_key"))
        except Exception:
            self.last_memory_tag_key = None
        return menu

    def _decorate_setup_menu(self, menu: dict, data_frame: Optional[bytes]) -> dict:
        if not menu.get("visible"):
            return menu
        menu = self._copy_menu(menu)
        if menu.get("type") == "scope":
            try:
                width = str(self.display_settings().get("band_scope", "WIDE")).upper()
                is_mem = bool(menu.get("memory_mode"))
                channels = 23 if (width == "WIDE" and is_mem) else 47 if width == "WIDE" else 13 if is_mem else 23
                menu["width"] = width
                menu["channels"] = channels
                menu["bars"] = _scope_resize_bars(menu.get("raw_bars") or [], channels)
                marker_raw = int(menu.get("marker_raw", 0x1E))
                # 0x1e is center in WIDE VFO.  Preserve movement across WIDE/
                # NARROW by mapping raw marker to offset from center.
                offset = marker_raw - 0x1E
                menu["marker_index"] = max(0, min(channels - 1, channels // 2 + offset))
                # Interval is decoded from the F1 21 frame (+0006) in
                # _scope_state_from_frame.  Do not override it from band/mode.
            except Exception:
                pass
        elif menu.get("type") == "menu1_keypad":
            mode = str(menu.get("mode") or "frequency")
            try:
                menu["input_value"] = str((self.menu1_inputs or {}).get(mode, ""))
            except Exception:
                menu["input_value"] = ""
        elif menu.get("type") == "memory_freq_keypad":
            menu = self._stabilize_memory_freq_keypad_cursor(menu)
            self.last_memory_tag_cells = None
            self.last_memory_tag_cursor_pos = None
            self.last_memory_tag_key = None
        elif menu.get("type") == "memory_tag_keypad":
            try:
                page = int(menu.get("keyboard_page", -1))
                labels, rows = _menu16_tag_keypad_layout(page, lower=bool(self.memory_tag_alpha_lower))
                menu["keypad"] = labels
                menu["keypad_rows"] = rows
            except Exception:
                pass
            menu = self._stabilize_memory_tag_keypad_cursor(menu)
            self.last_memory_freq_cells = None
            self.last_memory_freq_cursor_pos = None
            self.last_memory_freq_key = None
        else:
            if self.last_visible_menu.get("type") == "menu1_keypad":
                self.menu1_inputs = {"frequency": "", "memory": ""}
            self.last_memory_freq_cells = None
            self.last_memory_freq_cursor_pos = None
            self.last_memory_freq_key = None
            self.last_memory_tag_cells = None
            self.last_memory_tag_cursor_pos = None
            self.last_memory_tag_key = None
            self.memory_tag_alpha_lower = False
        now = time.time()
        self.last_visible_menu = self._copy_menu(menu)
        self.last_visible_menu_at = now
        if menu.get("type") == "memory_select":
            self.last_memory_select_menu = self._copy_menu(menu)
            self.last_memory_select_at = now
        elif menu.get("type") in ("memory_list", "memory_edit", "full", "submenu", "quick", "scope", "pmg", "raw"):
            self.last_memory_select_menu = {"visible": False}
            self.last_memory_select_at = 0.0
        # v73: do not overwrite value with local/default tables.
        # _menu_state_for_web already populated decoded value or UNKNOWN/raw.
        if menu.get("type") == "full":
            self._remember_display_setting_from_menu(menu)
        return menu

    def _memory_action_rows(self) -> List[dict]:
        labels = ["MR", "WRITE", "DELETE", "EDIT"]
        return [{"row": i, "num": "", "label": x, "text": x, "value": "", "editing": False} for i, x in enumerate(labels)]

    def open_memory_action_menu(self, row_index: int = 0, slot: Optional[int] = None) -> Tuple[bool, str]:
        try:
            slot_num = int(slot) if slot is not None else None
        except Exception:
            slot_num = None
        if slot_num is None:
            try:
                rows = self.last_visible_menu.get("rows") or []
                sel = int(row_index)
                if 0 <= sel < len(rows):
                    slot_num = int(rows[sel].get("slot"))
            except Exception:
                slot_num = None
        self.memory_action_menu = {
            "visible": True,
            "type": "memory_actions",
            "age_s": 0.0,
            "parent_num": 16,
            "title": f"MEMORY {slot_num:03d}" if slot_num is not None else "MEMORY",
            "memory_num": slot_num,
            "category": _setup_category(16),
            "selected_row": 0,
            "editing": False,
            "rows": self._memory_action_rows(),
        }
        self.memory_action_menu_at = time.time()
        return True, "memory action menu"

    def _memory_action_state(self) -> Optional[dict]:
        if not self.memory_action_menu:
            return None
        age = time.time() - float(self.memory_action_menu_at or 0.0)
        if age > 6.0:
            self.memory_action_menu = None
            return None
        menu = self._copy_menu(self.memory_action_menu)
        menu["age_s"] = age
        return menu

    def _clear_memory_action_menu(self) -> None:
        self.memory_action_menu = None
        self.memory_action_menu_at = 0.0

    def _latched_memory_select_state(self, data_frame: Optional[bytes]) -> Optional[dict]:
        # Disabled in radio-only mode.  The overlay is rendered only from the
        # current F1 23 menu frame plus current F1 00 +0015 cursor byte.
        return None

    def _adjust_memory_action_selection(self, command: str) -> None:
        # Disabled in radio-only mode: commands are physical only.  The web UI
        # updates when the radio sends updated F1 00/F1 23 frames.
        return

    def _menu16_context_active(self, menu_frame: Optional[bytes] = None, menu_ts: Optional[float] = None) -> bool:
        """Return True only when a primary F1 23/F1 29 frame belongs to menu 16.

        This prevents a normal/unknown primary display frame from being promoted
        to MEMORY LIST just because a few bytes happen to look parseable.
        """
        try:
            if self.last_visible_menu.get("visible"):
                t = self.last_visible_menu.get("type")
                if t in ("memory_list", "memory_select", "memory_edit", "memory_freq_keypad", "memory_tag_keypad"):
                    return True
                if t == "full" and int(self.last_visible_menu.get("selected_num") or -1) == 16:
                    if time.time() - float(self.last_visible_menu_at or 0.0) <= 2.5:
                        return True
            if menu_frame is not None and _is_live_menu_frame(menu_frame):
                rows = _setup_rows_from_frame(menu_frame)
                if rows:
                    sel = _setup_selected_row(menu_frame, rows)
                    if 0 <= sel < len(rows) and int(rows[sel].get("num") or -1) == 16:
                        return True
            if menu_ts is not None and time.time() - menu_ts <= 0.75:
                # When BodyRx has already classified F1 23/F1 29 as the menu
                # stream, _menu_state_for_web handles it directly.  This branch
                # is only for the old/primary path, so keep the window short.
                return bool(self.last_visible_menu.get("type") in ("memory_list", "memory_select", "memory_edit", "memory_freq_keypad", "memory_tag_keypad"))
        except Exception:
            pass
        return False

    def current_menu_state(self, data_frame: Optional[bytes] = None) -> dict:
        if self.rx is None:
            return {"visible": False}
        if data_frame is None:
            data_frame, _ts, _c = self.current_frame()

        menu_frame, menu_ts, _menu_seen = self.rx.menu_snapshot()
        menu_data_frame = data_frame
        if menu_frame is not None and len(menu_frame) >= 2 and menu_frame[:2] == b"\xF1\x25":
            try:
                pmg_frame, pmg_ts = self.rx.pmg_data_snapshot()
                if pmg_frame is not None and (pmg_ts is None or time.time() - pmg_ts <= 1.25):
                    menu_data_frame = pmg_frame
            except Exception:
                pass

        # Menu 16 is special: its actual screens are carried by the primary
        # display/menu stream (F1 23 list/action overlay / F1 29 edit).  Do not
        # invent a local action layer: render the action overlay only when the
        # radio actually sends the F1 23 action frame.
        primary_menu16 = _menu16_primary_state_from_frame(data_frame, 0.0, data_frame)
        menu_frame16 = _menu16_primary_state_from_frame(menu_frame, 0.0, data_frame) if menu_frame is not None else None

        # A real edit screen from the radio always wins and clears the modal.
        for candidate in (primary_menu16, menu_frame16):
            if candidate is not None and candidate.get("type") == "memory_edit":
                self._clear_memory_action_menu()
                return self._decorate_setup_menu(candidate, data_frame)

        # The action overlay is a real radio screen.  Once F1 23 is decoded as
        # memory_select, show it even if the stale setup context has already
        # timed out; this is not a client-side popup.
        for candidate in (primary_menu16, menu_frame16):
            if candidate is not None and candidate.get("type") == "memory_select":
                return self._decorate_setup_menu(candidate, data_frame)

        # Backward-compatible path for captures/running sessions where F1 23/F1
        # 29 still arrive as the primary display frame.  Prefer this over the
        # stale F3 20 setup list when the user has just entered menu 16.
        if primary_menu16 is not None and self._menu16_context_active(menu_frame, menu_ts):
            return self._decorate_setup_menu(primary_menu16, data_frame)
        if menu_frame16 is not None and self._menu16_context_active(menu_frame, menu_ts):
            return self._decorate_setup_menu(menu_frame16, data_frame)

        menu = _menu_state_for_web(menu_frame, menu_ts, menu_data_frame)
        if menu.get("visible"):
            if menu.get("type") == "memory_edit":
                self._clear_memory_action_menu()
            return self._decorate_setup_menu(menu, data_frame)

        # Menu 16 confirmation popups use the normal display path as a foreground
        # prompt while the MEMORY LIST / action overlay remains the correct
        # background.  Keep that background only when +0155 confirms a live popup.
        if (
            data_frame is not None
            and memory_channel_confirm_overlay_from_frame(data_frame) is not None
            and self.last_visible_menu.get("type") in ("memory_list", "memory_select", "memory_edit", "memory_freq_keypad", "memory_tag_keypad")
        ):
            return self._copy_menu(self.last_visible_menu)

        # During real value edit the radio often switches the data path to F3 13
        # while the last F3 20 menu layer stops updating.  Keep rendering the
        # last real menu row/value instead of falling back to VFO/raw, but keep
        # all knob commands physical.
        raw_menu = _strict_raw_menu_state_from_frame(data_frame)
        if raw_menu.get("visible"):
            # If a structured menu layer disappeared while the radio is showing an
            # F3 edit/value screen, do not invent the old value; show raw decoded
            # text from the radio frame instead.
            return raw_menu
        return {"visible": False}

    def _visible_setup_item_num(self) -> Optional[int]:
        try:
            frame, _ts, _c = self.current_frame()
            if self.rx is not None:
                menu_frame, menu_ts, _menu_seen = self.rx.menu_snapshot()
                menu = _menu_state_for_web(menu_frame, menu_ts, frame)
                if menu.get("visible") and menu.get("type") == "full":
                    menu = self._decorate_setup_menu(menu, frame)
                    n = menu.get("selected_num")
                    return int(n) if n is not None else None
            if self.last_visible_menu.get("visible") and self.last_visible_menu.get("type") == "full":
                n = self.last_visible_menu.get("selected_num")
                return int(n) if n is not None else None
        except Exception:
            pass
        return None

    def _advance_setup_value(self, delta: int) -> None:
        # v73: no local value tracking.
        return

    def _track_menu_command(self, c: str) -> None:
        # Most commands are pass-through only.  Exception: the TAG ABC/abc glyph
        # set is not encoded in the RX frame, only the radio page id and cursor
        # markers are.  The manual defines ABC/abc as a toggle, so track that
        # printed keyboard page locally from the real selected key before press.
        try:
            if c == "br_press" and self.last_visible_menu.get("type") == "menu1_keypad":
                mode = str(self.last_visible_menu.get("mode") or "frequency")
                key = int(self.last_visible_menu.get("selected_key", -1))
                labels = list(self.last_visible_menu.get("keypad") or [])
                label = labels[key] if 0 <= key < len(labels) else ""
                if label in ("0","1","2","3","4","5","6","7","8","9"):
                    limit = 8 if mode == "frequency" else 3
                    cur = str((self.menu1_inputs or {}).get(mode, ""))
                    if len(cur) < limit:
                        self.menu1_inputs[mode] = cur + label
                elif label == "DEL":
                    cur = str((self.menu1_inputs or {}).get(mode, ""))
                    self.menu1_inputs[mode] = cur[:-1]
            if c == "br_press" and self.last_visible_menu.get("type") == "memory_tag_keypad":
                page = int(self.last_visible_menu.get("keyboard_page", -1))
                key = int(self.last_visible_menu.get("selected_key", -1))
                labels = list(self.last_visible_menu.get("keypad") or [])
                label = labels[key] if 0 <= key < len(labels) else ""
                # ABC/abc changes case only on the alphabet page itself.
                # On numeric/symbol pages, ABC means "return to text page" and
                # the radio preserves the previous alphabet case.
                if page == 0x04 and label == "abc":
                    self.memory_tag_alpha_lower = True
                elif page == 0x04 and label == "ABC":
                    self.memory_tag_alpha_lower = False
                elif page in (0x02, 0x03) and label == "ABC":
                    pass
                elif page == 0x02 and label == "#%^":
                    pass
                elif page == 0x03 and label == "123":
                    pass
        except Exception:
            pass
        return

    def _pulse_for_sync(self, command: str, duration: str, settle_s: float = 0.10) -> None:
        if self.tx is None:
            raise RuntimeError("TX not available")
        c = canon(command)
        if c not in COMMANDS:
            raise RuntimeError(f"unknown command {command}")
        frames = parse_duration_token(duration, default_frames_for(c))
        self.tx.pulse(f"startup_sync_{c}", COMMANDS[c], frames)
        # Wait for the pulse to be transmitted and for the radio to answer.
        time.sleep(max(settle_s, frames_to_ms(frames) / 1000.0 + 0.08))

    def _read_selected_setup_value(self, expected_num: int, timeout_s: float = 1.2) -> Optional[str]:
        if self.rx is None:
            return None
        end = time.monotonic() + timeout_s
        best: Optional[str] = None
        while time.monotonic() < end:
            frame, _ts, _seen, _data_seen, _sync = self.rx.snapshot()
            menu_frame, menu_ts, _menu_seen = self.rx.menu_snapshot()
            if menu_frame is not None and menu_ts is not None:
                menu = _menu_state_for_web(menu_frame, menu_ts, frame)
                if menu.get("visible") and menu.get("type") == "full":
                    try:
                        n = int(menu.get("selected_num"))
                    except Exception:
                        n = -1
                    if n == expected_num:
                        v = _setup_value_from_menu_frame(expected_num, menu_frame)
                        if v:
                            best = v
                            break
            time.sleep(0.05)
        return best

    def startup_sync_display_settings(self) -> dict:
        """Read DISPLAY setup values 02/03/04/06 from the real radio menu.

        This intentionally drives the radio for a few seconds:
          F long -> Setup Menu
          many BR_LEFT -> item 01
          BR_RIGHT to 02,03,04,06
          read F3 20 value area +0028
          F short -> normal display
        """
        if self.demo or self.tx is None or self.rx is None:
            print("[startup-sync-removed] skipped: serial TX/RX not available")
            return {}

        print("[startup-sync-removed] reading DISPLAY settings from radio menu")
        found: Dict[int, str] = {}
        try:
            # Enter Setup Menu from the normal display.
            self._pulse_for_sync("f", "1200ms", settle_s=0.80)

            # Go to the beginning of the setup list. Encoder pulses must be short,
            # otherwise the radio can skip entries.
            for _ in range(80):
                self._pulse_for_sync("br_left", "10ms", settle_s=0.012)
            time.sleep(0.25)

            current = 1
            for target in (2, 3, 4, 6):
                steps = max(0, target - current)
                for _ in range(steps):
                    self._pulse_for_sync("br_right", "10ms", settle_s=0.045)
                current = target
                time.sleep(0.20)
                value = self._read_selected_setup_value(target, timeout_s=1.2)
                if value:
                    found[target] = value
                    print(f"[startup-sync-removed] {target:02d} {SETUP_MENU_ITEMS.get(target, '')}: {value}")
                else:
                    print(f"[startup-sync-removed] {target:02d} {SETUP_MENU_ITEMS.get(target, '')}: not decoded")

            # Back out to the normal screen.
            self._pulse_for_sync("f", "200ms", settle_s=0.45)
        except Exception as e:
            print(f"[startup-sync-removed] failed: {e}", file=sys.stderr)
            try:
                self._pulse_for_sync("f", "200ms", settle_s=0.2)
            except Exception:
                pass

        settings: dict = {}
        if found.get(2) in ("MAX", "MID", "OFF"):
            settings["lcd_dimmer"] = found[2]
        if found.get(3):
            try:
                c = int(str(found[3]))
                if 1 <= c <= 9:
                    settings["lcd_contrast"] = c
            except Exception:
                pass
        if found.get(4) in ("WIDE", "NARROW"):
            settings["band_scope"] = found[4]
        if found.get(6) in ("AMBER", "WHITE"):
            settings["backlight_color"] = found[6]
        self.synced_display_settings = settings
        print(f"[startup-sync-removed] applied baseline: {settings or 'none'}")
        return settings

    def _handle_radio_power_lost(self, reason: str = "") -> None:
        """Stop local sessions and continuous TX when the RX watchdog says OFF."""
        if self.tx is not None:
            try:
                self.tx.release()
            except Exception:
                pass
            try:
                self.tx.set_enabled(False, reason or "radio off")
            except Exception:
                pass
        # If browser TX audio/PTT was active, force it off too.  RX browser audio
        # is stopped by the web UI when it sees radio_powered=false.
        try:
            self.stop_tx_audio_session()
        except Exception:
            pass
        if self.ptt is not None:
            try:
                self.ptt.set_ptt(False)
            except Exception:
                pass
        self.ptt_latched = False

    def _handle_radio_power_alive(self, reason: str = "") -> None:
        """Allow normal continuous TX after RX frames prove the radio is alive."""
        if self.tx is not None:
            try:
                self.tx.set_enabled(True, reason or "radio on")
            except Exception:
                pass

    def _rx_power_info(self) -> Tuple[bool, Optional[float], int]:
        """Return (alive, age_seconds, frame_count) from the RX-frame watchdog."""
        if self.demo:
            return True, 0.0, 0
        if self.rx is None:
            # In --no-rx mode there is no reliable way to infer power from the
            # radio. Keep the manual/initial state.
            return bool(self.radio_powered), None, 0
        try:
            ts, frames = self.rx.activity_snapshot()
        except Exception:
            return False, None, 0
        if ts is None or frames <= 0:
            return False, None, int(frames or 0)
        age = max(0.0, time.time() - ts)
        return age <= self.rx_power_timeout_s, age, int(frames)

    def _refresh_radio_power_from_rx(self) -> bool:
        """Synchronize UI/electrical state with live RX frames.

        When valid RX frames disappear for rx_power_timeout_s, the radio is
        considered OFF and the 74LVC157A selects the GPIO replay source. When
        frames appear again, the radio is considered ON and USB-TTL TX is
        selected.
        """
        alive, age, frames = self._rx_power_info()
        if self.demo or self.rx is None:
            return bool(self.radio_powered)

        changed = False
        with self.power_lock:
            was = bool(self.radio_powered)
            if alive and not was:
                self.radio_powered = True
                self.power_message = f"radio on: RX frames active ({frames})"
                changed = True
            elif (not alive) and was and not self.powering_on:
                self.radio_powered = False
                if age is None:
                    self.power_message = "radio off: no RX frames"
                else:
                    self.power_message = f"radio off: RX missing for {age:.1f}s"
                changed = True

        if changed:
            if alive:
                ok, msg = self._set_uart_select_usb(True)
                self._handle_radio_power_alive("RX frames detected")
                if ok:
                    print(f"[power] RX frames detected: radio ON, S GPIO{self.uart_select_gpio}=HIGH, continuous TX ON")
                else:
                    print(f"[power] TX USB selection error after RX alive: {msg}", file=sys.stderr)
            else:
                self._handle_radio_power_lost("RX stopped")
                ok, msg = self._set_uart_select_usb(False)
                if ok:
                    print(f"[power] RX stopped: radio OFF, S GPIO{self.uart_select_gpio}=LOW, continuous TX OFF")
                else:
                    print(f"[power] TX USB isolation error after RX stop: {msg}", file=sys.stderr)
        return alive

    def _power_watchdog_loop(self) -> None:
        while not self._power_watchdog_stop.is_set():
            try:
                self._refresh_radio_power_from_rx()
            except Exception as e:
                if not getattr(self, "demo", False):
                    print(f"[power] watchdog error: {e}", file=sys.stderr)
            self._power_watchdog_stop.wait(0.20)

    def _set_uart_select_usb(self, usb_enabled: bool) -> Tuple[bool, str]:
        """Select which source reaches radio pin 6 through the 74LVC157A S pin.

        Wiring assumed here:
          S LOW  -> GPIO18 replay source selected
          S HIGH -> USB-TTL TX source selected
        """
        if self.uart_select_gpio is None:
            return True, "S GPIO not configured"
        usb_enabled = bool(usb_enabled)
        if self._uart_usb_selected is usb_enabled:
            return True, (
                f"S GPIO{self.uart_select_gpio}=HIGH, TX USB already selected"
                if usb_enabled else
                f"S GPIO{self.uart_select_gpio}=LOW, GPIO replay already selected"
            )
        level = 1 if usb_enabled else 0
        ok, msg = gpio_write_once(int(self.uart_select_gpio), level)
        if ok:
            self._uart_usb_selected = usb_enabled
            self.power_message = (
                f"S GPIO{self.uart_select_gpio}=HIGH, TX USB selected"
                if usb_enabled else
                f"S GPIO{self.uart_select_gpio}=LOW, GPIO replay selected"
            )
        else:
            self.power_message = msg
        return ok, msg

    def start_radio_power_sequence(self) -> Tuple[bool, str, dict]:
        """Run the validated GPIO CH2 power-on sequence, then reconnect USB TX."""
        with self.power_lock:
            if self.radio_powered:
                return True, "radio already on", self.power_status()
            if self.powering_on:
                return False, "power-on already in progress", self.power_status()
            self.powering_on = True
            self.power_message = "power on: selecting GPIO replay"

        try:
            if self.tx is not None:
                try:
                    self.tx.release()
                except Exception:
                    pass
                try:
                    self.tx.set_enabled(False, "preparing power-on replay")
                except Exception:
                    pass

            ok, msg = self._set_uart_select_usb(False)
            if not ok:
                return False, msg, self.power_status()

            if self.power_gpio is None:
                return False, "GPIO replay not configured", self.power_status()

            self.power_message = f"power on: replaying CH2 on GPIO{self.power_gpio}"
            ok, msg = run_embedded_power_replay(int(self.power_gpio), tail_repeated=True, verbose=False)
            if not ok:
                self.power_message = msg
                return False, msg, self.power_status()

            self.power_message = "power on: reconnecting TX USB and waiting for RX frames"
            ok, msg = self._set_uart_select_usb(True)
            if not ok:
                return False, msg, self.power_status()
            if self.tx is not None:
                # The radio has just been woken electrically; resume the normal
                # 210-byte idle frame stream immediately, then the RX watchdog
                # will confirm power as soon as display frames arrive.
                self.tx.set_enabled(True, "after power-on replay")

            deadline = time.time() + 3.0
            while time.time() < deadline:
                if self._refresh_radio_power_from_rx():
                    with self.power_lock:
                        self.power_message = "radio on: RX frames received"
                    return True, self.power_message, self.power_status()
                time.sleep(0.05)

            with self.power_lock:
                self.power_message = "power-on sequence sent; waiting for RX frames"
            return True, self.power_message, self.power_status()
        except Exception as e:
            self.power_message = f"power-on error: {e}"
            return False, self.power_message, self.power_status()
        finally:
            with self.power_lock:
                self.powering_on = False

    def mark_radio_off(self) -> Tuple[bool, str, dict]:
        """Deprecated manual off marker; RX watchdog is authoritative."""
        self._refresh_radio_power_from_rx()
        alive, age, frames = self._rx_power_info()
        if alive:
            return True, "radio still on: RX frames present", self.power_status()
        with self.power_lock:
            self.radio_powered = False
            self.powering_on = False
            self.power_message = "radio off: RX missing, isolating TX USB"
        self._handle_radio_power_lost("radio off manual/watchdog")
        ok, msg = self._set_uart_select_usb(False)
        if not ok:
            return False, msg, self.power_status()
        return True, "radio turned off by RX watchdog; S LOW, USB TX isolated", self.power_status()

    def power_status(self) -> dict:
        alive, age, frames = self._rx_power_info()
        return {
            "radio_powered": bool(self.radio_powered),
            "powering_on": bool(self.powering_on),
            "power_message": self.power_message,
            "power_gpio": self.power_gpio,
            "uart_select_gpio": self.uart_select_gpio,
            "rx_power_alive": bool(alive),
            "rx_power_age_s": age,
            "rx_power_frames": frames,
            "rx_power_timeout_s": self.rx_power_timeout_s,
        }

    def audio_state(self) -> dict:
        # Keep the old top-level RX fields for the v38 browser code, and add a
        # nested TX section for microphone transmission.
        rx_state = self.audio.status() if self.audio is not None else {"enabled": False}
        tx_state = self.tx_audio.status() if self.tx_audio is not None else {"enabled": False}
        out = dict(rx_state)
        out["rx"] = rx_state
        out["tx"] = tx_state
        out["ptt"] = self.ptt.status() if self.ptt is not None else {"mode": "none", "active": False}
        return out

    def pulse_command(self, name: str, duration: Optional[str] = None) -> Tuple[bool, str]:
        self._refresh_radio_power_from_rx()
        if self.tx is None:
            return False, "TX not available in demo/no-tx mode"
        c = canon(name)
        if c not in COMMANDS:
            return False, f"unknown command: {name}"
        if not self.radio_powered:
            return False, "radio off: hold POWER to turn it on"
        self._track_menu_command(c)
        frames = parse_duration_token(duration, default_frames_for(c)) if duration else default_frames_for(c)
        self.tx.pulse(c, COMMANDS[c], frames)
        return True, f"{c} {frames}f"

    def toggle_ptt(self) -> Tuple[bool, str, bool]:
        self._refresh_radio_power_from_rx()
        if not self.radio_powered:
            return False, "radio off", self.ptt_latched
        if self.ptt is None:
            return False, "PTT not configured", self.ptt_latched
        try:
            if self.ptt_latched:
                self.ptt.set_ptt(False)
                self.ptt_latched = False
                return True, "PTT OFF", self.ptt_latched
            self.ptt.set_ptt(True)
            self.ptt_latched = True
            return True, "PTT ON", self.ptt_latched
        except Exception as e:
            return False, str(e), self.ptt_latched

    def start_tx_audio_session(self) -> None:
        self._refresh_radio_power_from_rx()
        if not self.radio_powered:
            raise RuntimeError("radio off")
        if self.tx_audio is None:
            raise RuntimeError("TX audio not configured")
        if self.ptt is None:
            raise RuntimeError("PTT not configured")
        with self.tx_audio_lock:
            if self.tx_audio_active:
                raise RuntimeError("TX audio already active")
            self.tx_audio.begin()
            try:
                self.ptt.set_ptt(True)
            except Exception:
                self.tx_audio.end(tail_ms=0)
                raise
            self.tx_audio_active = True

    def write_tx_audio(self, data: bytes) -> None:
        if self.tx_audio is None:
            raise RuntimeError("TX audio not configured")
        self.tx_audio.write(data)

    def stop_tx_audio_session(self) -> None:
        with self.tx_audio_lock:
            if self.tx_audio is not None:
                self.tx_audio.end()
            if self.ptt is not None:
                try:
                    self.ptt.set_ptt(False)
                except Exception:
                    pass
            self.tx_audio_active = False


    def hold_command(self, name: str) -> Tuple[bool, str]:
        self._refresh_radio_power_from_rx()
        if self.tx is None:
            return False, "TX not available"
        c = canon(name)
        if c not in COMMANDS:
            return False, f"unknown command: {name}"
        if not self.radio_powered:
            return False, "radio off: hold POWER to turn it on"
        self.tx.hold(c, COMMANDS[c])
        return True, f"hold {c}"

    def named_hold_command(self, name: str) -> Tuple[bool, str]:
        self._refresh_radio_power_from_rx()
        if self.tx is None:
            return False, "TX not available"
        c = canon(name)
        if c not in COMMANDS:
            return False, f"unknown command: {name}"
        if not self.radio_powered:
            return False, "radio off: hold POWER to turn it on"
        self.tx.named_hold(f"web_button_{c}", c, COMMANDS[c])
        return True, f"hold {c}"

    def clear_named_hold_command(self, name: str) -> Tuple[bool, str]:
        if self.tx is None:
            return False, "TX not available"
        c = canon(name)
        if c not in COMMANDS:
            return False, f"unknown command: {name}"
        self.tx.clear_named_hold(f"web_button_{c}")
        return True, f"release {c}"

    def save_action(self, action: str, label: str = "session", outdir: str = ".") -> Tuple[bool, str, dict]:
        rec = globals().get("SAVE_RECORDER")
        if rec is None:
            return False, "save recorder not available", {"active": False}
        a = (action or "status").strip().lower()
        if a in ("start", "begin", "on"):
            return rec.start(label=label or "session", outdir=outdir or ".", rx=self.rx)
        if a in ("stop", "end", "off"):
            return rec.stop()
        return True, "save status", rec.status()

    def release(self) -> None:
        if self.tx is not None:
            self.tx.release()
        if self.ptt is not None:
            try:
                self.ptt.set_ptt(False)
            except Exception:
                pass
        self.ptt_latched = False


class FreeRigWebHandler(BaseHTTPRequestHandler):
    ctx: WebContext

    def log_message(self, fmt: str, *args) -> None:
        if getattr(self.server, "verbose_http", False):
            super().log_message(fmt, *args)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            _html_response(self, self.ctx.web_html())
            return
        if path == "/api/state":
            _json_response(self, self.ctx.state())
            return
        if path == "/api/state.ws":
            _state_ws_response(self)
            return
        if path == "/api/audio":
            _json_response(self, self.ctx.audio_state())
            return
        if path == "/audio.pcm":
            _audio_pcm_response(self)
            return
        if path == "/audio-tx.ws":
            _audio_tx_ws_response(self)
            return
        if path == "/api/commands":
            _json_response(self, {"commands": sorted(COMMANDS.keys()), "aliases": ALIASES})
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = _read_json(self)
        if path == "/api/save":
            ok, msg, st = self.ctx.save_action(str(body.get("action", "status")), str(body.get("label", "session")), str(body.get("outdir", ".")))
            _json_response(self, {"ok": ok, "message": msg, "save": st, "error": None if ok else msg}, 200 if ok else 400)
            return
        if path == "/api/power_start":
            ok, msg, st = self.ctx.start_radio_power_sequence()
            _json_response(self, {"ok": ok, "message": msg, "power": st, "error": None if ok else msg}, 200 if ok else 400)
            return
        if path == "/api/radio_off":
            ok, msg, st = self.ctx.mark_radio_off()
            _json_response(self, {"ok": ok, "message": msg, "power": st, "error": None if ok else msg}, 200 if ok else 400)
            return
        if path == "/api/command":
            ok, msg = self.ctx.pulse_command(str(body.get("command", "")), body.get("duration"))
            _json_response(self, {"ok": ok, "message": msg, "error": None if ok else msg}, 200 if ok else 400)
            return
        if path == "/api/command_hold":
            ok, msg = self.ctx.named_hold_command(str(body.get("command", "")))
            _json_response(self, {"ok": ok, "message": msg, "error": None if ok else msg}, 200 if ok else 400)
            return
        if path == "/api/command_release":
            ok, msg = self.ctx.clear_named_hold_command(str(body.get("command", "")))
            _json_response(self, {"ok": ok, "message": msg, "error": None if ok else msg}, 200 if ok else 400)
            return
        if path == "/api/ptt_toggle":
            ok, msg, active = self.ctx.toggle_ptt()
            _json_response(self, {"ok": ok, "message": msg, "ptt_latched": active, "error": None if ok else msg}, 200 if ok else 400)
            return
        if path == "/api/hold":
            ok, msg = self.ctx.hold_command(str(body.get("command", "")))
            _json_response(self, {"ok": ok, "message": msg, "error": None if ok else msg}, 200 if ok else 400)
            return
        if path == "/api/release":
            self.ctx.release()
            _json_response(self, {"ok": True})
            return
        self.send_error(404)



def _web_console_loop(rx: Optional[BodyRx], httpd: ThreadingHTTPServer) -> None:
    """Tiny stdin console while the web GUI is running.

    This is intentionally small: the web buttons still send all radio commands,
    while the terminal can be used for save start/save end during mapping.
    """
    print(f"[build] {BUILD_ID}")
    print("[console] comandi disponibili: save start [label] [outdir], save stop/end, save status, quit")
    while True:
        try:
            line = input("web> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not line:
            continue
        try:
            parts = shlex.split(line)
        except ValueError as e:
            print(f"[console] parse error: {e}")
            continue
        cmd = parts[0].lower()
        args = parts[1:]
        if cmd in ("q", "quit", "exit"):
            print("[console] stop web server")
            try:
                httpd.shutdown()
            except Exception:
                pass
            return
        if cmd == "save":
            save_command(args, rx)
            continue
        print("[console] command not handled here. Use the GUI for radio commands, or: save start/stop/status, quit")

def web_main() -> int:
    ap = argparse.ArgumentParser(description="Free RIG web panel GUI - v51 fixes live menu rendering and labels")
    ap.add_argument("--port", default="/dev/ttyUSB0", help="radio serial port")
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("--host", default="0.0.0.0", help="web host; default 0.0.0.0 = reachable from the LAN")
    ap.add_argument("--web-port", type=int, default=8080)
    ap.add_argument("--demo", action="store_true", help="start only the GUI without serial/radio")
    ap.add_argument("--no-rx", action="store_true", help="do not start RX display reading")
    ap.add_argument("--no-tx", action="store_true", help="do not send panel->body frames")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--verbose-http", action="store_true")
    ap.add_argument("--decode", action="store_true", help="show the Decode/Save panel in the GUI and enable human decode in the web state")
    ap.add_argument("--ssl-cert", default=None, help="TLS PEM certificate to enable HTTPS/WSS")
    ap.add_argument("--ssl-key", default=None, help="TLS PEM key to enable HTTPS/WSS")
    ap.add_argument("--no-audio", action="store_true", help="disable RX audio streaming in the browser")
    ap.add_argument("--audio-device", default="plughw:0,0", help="RX capture ALSA device, e.g. plughw:0,0 or hw:0,0")
    ap.add_argument("--audio-rate", type=int, default=48000, help="RX PCM audio sample rate toward the browser")
    ap.add_argument("--audio-chunk-ms", type=int, default=10, help="RX HTTP audio chunk size in milliseconds")
    ap.add_argument("--audio-buffer-time", type=int, default=50000, help="ALSA RX buffer-time in microseconds")
    ap.add_argument("--audio-period-time", type=int, default=10000, help="ALSA RX period-time in microseconds")
    ap.add_argument("--rx-alsa-card", default="0", help="ALSA card used to configure Mic/AGC for RX capture, default 0")
    ap.add_argument("--rx-alsa-mic-volume", type=int, default=45, help="set the ALSA 'Mic' mixer to this percentage when RX audio starts, default 45")
    ap.add_argument("--rx-alsa-agc-off", action="store_true", default=True, help="turn off the ALSA 'Auto Gain Control' control when RX audio starts; default enabled")
    ap.add_argument("--rx-alsa-agc-on", dest="rx_alsa_agc_off", action="store_false", help="leave the ALSA 'Auto Gain Control' control enabled for RX capture")
    ap.add_argument("--no-tx-audio", action="store_true", help="disable TX audio from the browser microphone")
    ap.add_argument("--tx-audio-device", default="plughw:0,0", help="TX playback ALSA device; default plughw:0,0")
    ap.add_argument("--tx-audio-rate", type=int, default=48000, help="TX PCM audio sample rate from the browser toward the CM108")
    ap.add_argument("--tx-playback-channels", type=int, choices=(1, 2), default=2, help="TX playback ALSA channels; default 2 duplicates browser mono to L+R")
    ap.add_argument("--tx-aplay-verbose", action="store_true", help="enable verbose aplay output in GUI diagnostics")
    ap.add_argument("--tx-audio-buffer-time", type=int, default=50000, help="ALSA TX buffer-time in microseconds")
    ap.add_argument("--tx-audio-period-time", type=int, default=10000, help="ALSA TX period-time in microseconds")
    ap.add_argument("--tx-output-gain", type=float, default=1.0, help="fixed Raspberry-side gain before aplay, 1.0 = unchanged; v45 default is balanced for voice on the DATA port")
    ap.add_argument("--tx-agc", action="store_true", help="enable Raspberry-side TX AGC/normalization; disabled by default to avoid distortion")
    ap.add_argument("--tx-agc-target", type=float, default=0.60, help="TX AGC target peak, 0.60 = anti-distortion headroom")
    ap.add_argument("--tx-agc-max-boost", type=float, default=10.0, help="maximum Raspberry-side TX AGC boost")
    ap.add_argument("--tx-alsa-card", default="0", help="ALSA card used to configure Speaker, default 0")
    ap.add_argument("--tx-alsa-speaker-volume", type=int, default=90, help="set the ALSA 'Speaker' mixer to this percentage when TX starts, default 90")
    ap.add_argument("--tx-ptt-lead-ms", type=int, default=120, help="delay between PTT ON and microphone audio transmission")
    ap.add_argument("--tx-ptt-tail-ms", type=int, default=80, help="silent tail before releasing PTT")
    ap.add_argument("--tx-ptt-mode", choices=("cm108", "serial", "none"), default="serial", help="PTT used by TX audio: cm108=DATA-card GPIO, serial=legacy mic PTT, none=audio only")
    ap.add_argument("--cm108-hidraw", default="auto", help="/dev/hidrawN for the CM108/CM119; default is auto based on --tx-audio-device")
    ap.add_argument("--cm108-gpio", type=int, default=3, help="CM108/CM119 GPIO used for PTT; typical AllStar/Direwolf = 3")
    ap.add_argument("--cm108-ptt-invert", action="store_true", help="invert HID GPIO logic if your interface requires 0 to transmit")
    ap.add_argument("--power-gpio", type=int, default=18, help="BCM GPIO that replays CH2/pin6 to power on the radio; default 18")
    ap.add_argument("--uart-select-gpio", type=int, default=23, help="BCM GPIO wired to S on the 74LVC157A: LOW=GPIO replay, HIGH=USB TX; default 23")
    ap.add_argument("--radio-start-on", action="store_true", help="initial hint: start the GUI as if the radio were already on; the RX watchdog will decide afterward")
    ap.add_argument("--rx-power-timeout", type=float, default=1.2, help="seconds without valid RX frames before considering the radio off; default 1.2")
    args = ap.parse_args()

    # Important at process startup: isolate the USB-TTL TX before opening the
    # serial port, otherwise its idle HIGH can disturb the GPIO replay line.
    if not args.radio_start_on and args.uart_select_gpio is not None:
        ok, msg = gpio_write_once(int(args.uart_select_gpio), 0)
        if ok:
            print(f"[power] pre-init: S GPIO{args.uart_select_gpio}=LOW, TX USB isolated")
        else:
            print(f"[power] warning pre-init S LOW failed: {msg}", file=sys.stderr)

    ser = None
    tx = None
    rx = None
    tx_th = None
    rx_th = None

    if not args.demo:
        if serial is None:
            raise SystemExit("pyserial not installed: run python3 -m pip install pyserial")
        ser = serial.Serial(args.port, args.baud, bytesize=8, parity="N", stopbits=1, timeout=0.02, write_timeout=1.0, xonxoff=False, rtscts=False, dsrdtr=False)
        print(f"[open] {args.port} @ {args.baud} 8N1")
        if not args.no_tx:
            tx = PanelTx(ser, verbose=args.verbose)
            tx.set_enabled(bool(args.radio_start_on), "initial state before watchdog")
            tx_th = threading.Thread(target=tx.writer_loop, daemon=True)
            tx_th.start()
            print("[tx] panel→body idle frame ready")
        if not args.no_rx:
            rx = BodyRx(ser, enabled=True, verbose=args.verbose, ignore_menu=True)
            rx_th = threading.Thread(target=rx.reader_loop, daemon=True)
            rx_th.start()
            print("[rx] body→panel display reading active")
    else:
        print("[demo] GUI without serial")

    audio = None
    tx_audio = None
    if not args.no_audio:
        audio = AudioStreamer(
            enabled=True,
            device=args.audio_device,
            rate=args.audio_rate,
            channels=1,
            chunk_ms=args.audio_chunk_ms,
            buffer_time_us=args.audio_buffer_time,
            period_time_us=args.audio_period_time,
            alsa_card=args.rx_alsa_card,
            alsa_mic_volume=args.rx_alsa_mic_volume,
            alsa_agc_off=args.rx_alsa_agc_off,
        )
        rxmix=[]
        if args.rx_alsa_mic_volume is not None: rxmix.append(f"Mic={args.rx_alsa_mic_volume}%")
        if args.rx_alsa_agc_off: rxmix.append("AGC off")
        if args.rx_alsa_card: rxmix.append(f"card={args.rx_alsa_card}")
        rxmix_txt=(", " + ", ".join(rxmix)) if rxmix else ""
        print(f"[audio] direct RX PCM: {args.audio_device}, S16_LE mono @ {args.audio_rate} Hz, chunk={args.audio_chunk_ms} ms{rxmix_txt}")

    if not args.no_tx_audio:
        tx_dev = args.tx_audio_device or args.audio_device
        tx_audio = TxAudioSink(
            enabled=True,
            device=tx_dev,
            rate=args.tx_audio_rate,
            channels=1,
            buffer_time_us=args.tx_audio_buffer_time,
            period_time_us=args.tx_audio_period_time,
            output_gain=args.tx_output_gain,
            playback_channels=args.tx_playback_channels,
            aplay_verbose=args.tx_aplay_verbose,
            agc_enabled=args.tx_agc,
            agc_target=args.tx_agc_target,
            agc_max_boost=args.tx_agc_max_boost,
            alsa_card=args.tx_alsa_card,
            alsa_speaker_volume=args.tx_alsa_speaker_volume,
            ptt_lead_ms=args.tx_ptt_lead_ms,
            ptt_tail_ms=args.tx_ptt_tail_ms,
        )
        agc_txt = "on" if args.tx_agc else "off"
        spk_txt = "unchanged" if args.tx_alsa_speaker_volume is None else f"{args.tx_alsa_speaker_volume}%"
        print(f"[audio] TX browser mic→ALSA: {tx_dev}, browser mono → ALSA {args.tx_playback_channels}ch S16_LE @ {args.tx_audio_rate} Hz, output_gain={args.tx_output_gain}x, agc={agc_txt}, speaker={spk_txt}, ptt_lead={args.tx_ptt_lead_ms} ms, tail={args.tx_ptt_tail_ms} ms")

    ptt = None
    if args.tx_ptt_mode == "cm108":
        tx_dev_for_ptt = args.tx_audio_device or args.audio_device
        ptt = Cm108PttController(audio_device=tx_dev_for_ptt, hidraw=args.cm108_hidraw, gpio=args.cm108_gpio, invert=args.cm108_ptt_invert)
        st = ptt.status()
        print(f"[ptt] CM108 GPIO{st['gpio']} via {st.get('hidraw') or 'NOT FOUND'} ({st.get('hidraw_source','')}) invert={st['invert']}")
        try:
            ptt.set_ptt(False)
        except Exception as e:
            print(f"[ptt] warning: {e}", file=sys.stderr)
    elif args.tx_ptt_mode == "serial":
        ptt = SerialMicPttController(tx)
        print("[ptt] serial/microphone: using the legacy mic_ptt_hold")
    else:
        print("[ptt] disabled: TX audio will not key the radio")

    ctx = WebContext(tx=tx, rx=rx, demo=args.demo, audio=audio, tx_audio=tx_audio, ptt=ptt, decode_enabled=args.decode, power_gpio=args.power_gpio, uart_select_gpio=args.uart_select_gpio, radio_start_on=args.radio_start_on, rx_power_timeout_s=args.rx_power_timeout)
    FreeRigWebHandler.ctx = ctx
    httpd = ThreadingHTTPServer((args.host, args.web_port), FreeRigWebHandler)
    httpd.verbose_http = args.verbose_http
    if getattr(sys.stdin, "isatty", lambda: False)():
        threading.Thread(target=_web_console_loop, args=(rx, httpd), daemon=True).start()
    scheme = "http"
    if args.ssl_cert or args.ssl_key:
        if not (args.ssl_cert and args.ssl_key):
            raise SystemExit("use --ssl-cert and --ssl-key together")
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(args.ssl_cert, args.ssl_key)
        httpd.socket = tls.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    
    def _lan_ip():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            sock.close()
            return ip
        except Exception:
            return None

    if args.host in ("0.0.0.0", "::"):
        ip = _lan_ip()
        print(f"[web] listening on all interfaces, port {args.web_port}")
        print(f"[web] locale: http://127.0.0.1:{args.web_port}/")
        if ip:
            print(f"[web] LAN:    http://{ip}:{args.web_port}/")
        print("[web] Internet access requires a VPN/tunnel or router port-forwarding")
    else:
        print(f"[web] open: http://{args.host}:{args.web_port}/")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[stop]")
    finally:
        httpd.shutdown()
        if audio is not None:
            audio.shutdown()
        if tx_audio is not None:
            tx_audio.shutdown()
        if 'ptt' in locals() and ptt is not None:
            ptt.shutdown()
        if tx is not None:
            tx.stop.set()
        if rx is not None:
            rx.stop.set()
        if tx_th is not None:
            tx_th.join(timeout=1.0)
        if rx_th is not None:
            rx_th.join(timeout=1.0)
        if ser is not None:
            ser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(web_main())
