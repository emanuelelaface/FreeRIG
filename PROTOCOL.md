# Free RIG Front Panel Protocol Notes

This document describes the currently reverse-engineered hardware and data protocol created from the Yaesu FTM-150 radio body and detachable front panel, as implemented by `freerig.py`.

The map is intentionally conservative: fields that are not understood are documented as unknown or raw. Offsets are **zero-based decimal offsets** from the start of the frame unless explicitly written in hexadecimal.

## 1. Hardware layer

### 1.1 Interconnect pinout

| Pin | Signal | Direction / type | Notes |
|---:|---|---|---|
| 1 | Microphone | Analog | Microphone audio. Not part of the UART data stream. |
| 2 | Audio GND | Audio Ground | Common reference for audio. |
| 3 | Speaker | Analog | Speaker/RX audio. Captured by a USB sound card in this project. |
| 4 | +3.3V | Power | Not used (?). |
| 5 | +13 V | Power | Radio supply. Do not connect to TTL or audio inputs. |
| 6 | Data transmission: front panel → radio body | 3.3 V TTL UART plus cold-start wake waveform | Panel command stream, called panel→body or TX frame in this document. During cold startup this line is driven by a captured CH2 GPIO replay before normal UART takes over. |
| 7 | Data transmission: radio body → front panel | 3.3 V TTL UART | Display/status stream, called body→panel or RX frame in this document. |
| 8 | GND | Ground | Common reference for serial. |

### 1.2 Electrical format

- UART: **500000 baud, 8 data bits, no parity, 1 stop bit**.
- Logic level: **3.3 V TTL**.
- The link is full-duplex at the electrical level because panel→body and body→panel use separate data pins.
- UART byte time is 10 bit-times because the line uses 8N1 framing.
- Audio is not serialized in these frames. RX/TX audio is analog and is handled separately through a USB audio interface, for example a CM108/CM119 dongle.

### 1.3 Safe test wiring, radio already on

```text
USB-TTL TX  -> Pin 6 / radio BODY RX line, preferably through 1 kΩ–4.7 kΩ
USB-TTL RX  <- Pin 7 / radio BODY TX line, preferably through 4.7 kΩ–10 kΩ
USB-TTL GND -> Pin 2 / GND
```

Do not leave the original front-panel TX output connected to the same body RX line while the USB-TTL adapter is driving it. Two push-pull TX outputs must not fight each other.

### 1.4 Cold power-on hardware

Cold power-on is not handled by simply sending the normal 210-byte UART frame. The working implementation replays the captured CH2/pin-6 electrical waveform from a Raspberry Pi GPIO, then switches the line back to the USB-TTL TX output for normal UART operation.

A tested wiring uses one channel of a 74LVC157A 2-to-1 multiplexer:

```text
Raspberry GPIO18  -> 74LVC157A 1I0    # CH2/pin-6 wake waveform replay
USB-TTL TX        -> 74LVC157A 1I1    # normal panel→body UART
74LVC157A 1Y      -> radio pin 6 / body RX
Raspberry GPIO23  -> 74LVC157A S      # LOW=1I0 replay, HIGH=1I1 USB-TTL TX
74LVC157A /E      -> GND              # output enabled; do not use /E as Hi-Z here
74LVC157A VCC     -> 3.3 V
All grounds       -> common GND

USB-TTL RX        <- radio pin 7 / body TX
```

Use a local 100 nF decoupling capacitor between the 74LVC157A VCC and GND. The USB-TTL TX output should not be directly tied to the same radio pin as the replay GPIO because its idle HIGH push-pull driver can load or fight the replay waveform.

The application defaults are:

| Function | Default BCM GPIO | Meaning |
|---|---:|---|
| `--power-gpio` | 18 | Replays the captured CH2/pin-6 waveform. |
| `--uart-select-gpio` | 23 | Drives the 74LVC157A `S` pin: LOW selects GPIO18 replay, HIGH selects USB-TTL TX. |

After the wake replay, GPIO18 is released as input with pull-up/down disabled, so it is effectively high-impedance. GPIO23 is then driven HIGH to select the normal USB-TTL TX path.

## 2. UART framing summary

| Direction | Length | Structure | Time at 500000 8N1 | Approx. rate |
|---|---:|---|---:|---:|
| Panel → body | 210 bytes | One fixed-length command/idle frame | 4.20 ms | 238.1 frames/s |
| Body → panel | 1100 bytes | 5 blocks × 220 bytes | 22.00 ms | 45.45 frames/s |

The implementation continuously sends panel→body frames once the radio is awake and the USB-TTL TX path is selected. Commands are created by starting from the idle frame and applying one or more byte operations for a defined number of frames.

### 2.1 Cold-start waveform and power-state detection

The captured front-panel startup waveform on CH2/pin 6 is treated as a raw timing waveform, not as a normal UART frame sequence. The working replay starts at the first LOW→HIGH edge of CH2. In the current capture this has these useful landmarks:

| Landmark | Approx. timing from first LOW→HIGH | Notes |
|---|---:|---|
| Initial HIGH hold | 666.851 ms | CH2 remains high before the first startup exchange. |
| Critical replay end | 1452.096 ms | The embedded GPIO replay reproduces the timing-critical part up to this point. |
| Optional compact tail | about 430 ms | A UART-like repeated 210-byte tail may be sent after the critical replay. |

The software power-on sequence is:

1. Select GPIO replay path with GPIO23 LOW.
2. Release any active serial command holds.
3. Replay the embedded CH2 waveform on GPIO18, including the optional compact tail.
4. Release GPIO18 to input/high-impedance.
5. Select USB-TTL TX with GPIO23 HIGH.
6. Wait for valid body→panel RX frames before declaring the radio on.

The GUI does not treat power state as a simple remembered button state. It is driven by an RX-frame watchdog: valid body→panel frames mean on, while no valid RX frames for `--rx-power-timeout` seconds means off. The default timeout is 1.2 seconds.

## 3. Panel → body frame

### 3.1 Idle frame

The idle frame is 210 bytes long. It is sent continuously while no command is active.

```text
0000: 80 00 00 00 00 00 00 00 00 00 00 00 00 7C 7B 20
0010: 00 00 00 0F 00 00 00 00 00 00 00 00 00 00 00 00
0020: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0030: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 01 02
0040: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0050: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0060: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0070: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0080: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0090: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00A0: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00B0: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00C0: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00D0: 00 00
```

### 3.2 Command operation model

Each command is a set of operations applied to the idle frame:

- `set`: replace one byte, e.g. `+0002 = 0x7F`.
- `or`: set one or more bits without clearing the idle value, e.g. `+0005 |= 0x01`.

Multiple active holds and pulses are composed on top of the idle frame. A pulse lasts for a finite number of TX frames; a hold remains active until released.

Default durations:

| Command family | Default frames | Approx. duration |
|---|---:|---:|
| Front-panel key | 60 | 252.0 ms |
| Encoder detent | 1 | 4.2 ms |
| Power | 120 | 504.0 ms |
| Microphone command | 70 | 294.0 ms |

Duration syntax accepted by the software:

| Syntax | Meaning |
|---|---|
| `80f` | Exactly 80 TX frames. |
| `250ms` | Milliseconds converted to the nearest TX frame count. |
| `250` | Milliseconds, legacy shorthand. |

### 3.3 Command byte map

### Front-panel keys

| Command | Frame operation | Default duration |
|---|---:|---:|
| `band` | `+0005 |= 0x01` | 60 frames / 252.0 ms |
| `f` | `+0005 |= 0x04` | 60 frames / 252.0 ms |
| `pmg` | `+0005 |= 0x08` | 60 frames / 252.0 ms |
| `vm` | `+0005 |= 0x20` | 60 frames / 252.0 ms |
| `sdx` | `+0007 |= 0x40` | 60 frames / 252.0 ms |
| `power` | `+0006 |= 0x01` | 120 frames / 504.0 ms |

The `power` command above is the normal panel POWER-key state inside the 210-byte UART frame. It is not sufficient by itself for cold startup from a fully off body; cold startup uses the raw CH2 GPIO replay described in section 2.1.

### Knob pushes

| Command | Frame operation | Default duration |
|---|---:|---:|
| `ul_press` | `+0006 |= 0x02` | 60 frames / 252.0 ms |
| `ur_press` | `+0006 |= 0x08` | 60 frames / 252.0 ms |
| `bl_press` | `+0006 |= 0x10` | 60 frames / 252.0 ms |
| `br_press` | `+0006 |= 0x20` | 60 frames / 252.0 ms |

### Encoder detents

| Command | Frame operation | Default duration |
|---|---:|---:|
| `ul_left` | `+0002 = 0x7F` | 1 frames / 4.2 ms |
| `ul_right` | `+0002 = 0x01` | 1 frames / 4.2 ms |
| `ur_left` | `+0003 = 0x7F` | 1 frames / 4.2 ms |
| `ur_right` | `+0003 = 0x01` | 1 frames / 4.2 ms |
| `bl_left` | `+0000 = 0xFF` | 1 frames / 4.2 ms |
| `bl_right` | `+0000 = 0x81` | 1 frames / 4.2 ms |
| `br_left` | `+0001 = 0x7F` | 1 frames / 4.2 ms |
| `br_right` | `+0001 = 0x01` | 1 frames / 4.2 ms |

### Microphone direct signals

| Command | Frame operation | Default duration |
|---|---:|---:|
| `mic_ptt` | `+0008 |= 0x01` | 70 frames / 294.0 ms |
| `mic_ptt_hold` | `+0008 |= 0x01`<br>`+0015 = 0x21` | 70 frames / 294.0 ms |
| `mic_up` | `+0013 = 0x07`<br>`+0014 = 0x1F` | 70 frames / 294.0 ms |
| `mic_down` | `+0013 = 0x07`<br>`+0014 = 0x36` | 70 frames / 294.0 ms |
| `mic_mute` | `+0013 = 0x02`<br>`+0014 = 0x63` | 70 frames / 294.0 ms |
| `mic_p1` | `+0013 = 0x1C`<br>`+0014 = 0x63` | 70 frames / 294.0 ms |
| `mic_p2` | `+0013 = 0x34`<br>`+0014 = 0x63` | 70 frames / 294.0 ms |
| `mic_p3` | `+0013 = 0x4D`<br>`+0014 = 0x63` | 70 frames / 294.0 ms |
| `mic_p4` | `+0013 = 0x67`<br>`+0014 = 0x63` | 70 frames / 294.0 ms |

### Microphone keypad

| Command | Frame operation | Default duration |
|---|---:|---:|
| `mic_1` | `+0013 = 0x1C`<br>`+0014 = 0x02` | 70 frames / 294.0 ms |
| `mic_2` | `+0013 = 0x33`<br>`+0014 = 0x02` | 70 frames / 294.0 ms |
| `mic_3` | `+0013 = 0x4C`<br>`+0014 = 0x02` | 70 frames / 294.0 ms |
| `mic_4` | `+0013 = 0x1C`<br>`+0014 = 0x19` | 70 frames / 294.0 ms |
| `mic_5` | `+0013 = 0x33`<br>`+0014 = 0x19` | 70 frames / 294.0 ms |
| `mic_6` | `+0013 = 0x4C`<br>`+0014 = 0x19` | 70 frames / 294.0 ms |
| `mic_7` | `+0013 = 0x1C`<br>`+0014 = 0x32` | 70 frames / 294.0 ms |
| `mic_8` | `+0013 = 0x33`<br>`+0014 = 0x32` | 70 frames / 294.0 ms |
| `mic_9` | `+0013 = 0x4C`<br>`+0014 = 0x32` | 70 frames / 294.0 ms |
| `mic_star` | `+0013 = 0x1C`<br>`+0014 = 0x4B` | 70 frames / 294.0 ms |
| `mic_0` | `+0013 = 0x33`<br>`+0014 = 0x4B` | 70 frames / 294.0 ms |
| `mic_hash` | `+0013 = 0x4C`<br>`+0014 = 0x4B` | 70 frames / 294.0 ms |
| `mic_a` | `+0013 = 0x65`<br>`+0014 = 0x02` | 70 frames / 294.0 ms |
| `mic_b` | `+0013 = 0x66`<br>`+0014 = 0x19` | 70 frames / 294.0 ms |
| `mic_c` | `+0013 = 0x66`<br>`+0014 = 0x32` | 70 frames / 294.0 ms |
| `mic_d` | `+0013 = 0x66`<br>`+0014 = 0x4B` | 70 frames / 294.0 ms |

### Alternative observed microphone values

| Command | Frame operation | Default duration |
|---|---:|---:|
| `mic_0_alt` | `+0013 = 0x34`<br>`+0014 = 0x4B` | 70 frames / 294.0 ms |
| `mic_1_alt` | `+0013 = 0x1B`<br>`+0014 = 0x02` | 70 frames / 294.0 ms |
| `mic_2_alt` | `+0013 = 0x33`<br>`+0014 = 0x01` | 70 frames / 294.0 ms |
| `mic_4_alt` | `+0013 = 0x1B`<br>`+0014 = 0x19` | 70 frames / 294.0 ms |
| `mic_6_alt` | `+0013 = 0x4C`<br>`+0014 = 0x1A` | 70 frames / 294.0 ms |
| `mic_a_alt` | `+0013 = 0x65`<br>`+0014 = 0x01` | 70 frames / 294.0 ms |
| `mic_b_alt` | `+0013 = 0x66`<br>`+0014 = 0x1A` | 70 frames / 294.0 ms |
| `mic_d_alt` | `+0013 = 0x66`<br>`+0014 = 0x4C` | 70 frames / 294.0 ms |
| `mic_hash_alt` | `+0013 = 0x4C`<br>`+0014 = 0x4C` | 70 frames / 294.0 ms |


### 3.4 Aliases

Aliases are accepted by the UI/API and resolved to canonical command names.

| Alias group | Aliases |
|---|---|
| Band | `band/scope`, `scope` → `band` |
| F | `f/back`, `back` → `f` |
| PMG | `pmg/pw`, `pw` → `pmg` |
| V/M | `v/m`, `mw`, `v/mw`, `vm/mw`, `vmmw` → `vm` |
| S-DX | `s-dx`, `sd-x` → `sdx` |
| Knobs | `upper_left_*`, `upper_right_*`, `bottom_left_*`, `bottom_right_*` aliases map to `ul_*`, `ur_*`, `bl_*`, `br_*` |
| Microphone | `ptt`, `up`, `down`, `mute`, `p1`..`p4`, `0`..`9`, `*`, `#`, `a`..`d` |

## 4. Body → panel frame

### 4.1 Frame sync

A body→panel frame is 1100 bytes long and consists of five 220-byte blocks.

Observed sync rules used by the decoder:

- Frame offset `+0000` must be `0xF1` or `0xF3`.
- Block starts at `+0220`, `+0440`, `+0660`, and `+0880` must contain `0xFF`.
- The decoder searches for the next `0xF1` or `0xF3` that satisfies the block-start rule when synchronization is lost.

### 4.2 Blank / keepalive frame

A conservative blank/keepalive test is used:

- first two bytes: `F1 60`
- bytes `+0002` through `+0219` are all zero

Such frames are ignored for the normal display decoder.

### 4.3 Normal display vs. `display2` / menu frames

The decoder separates normal dual-frequency display frames from alternate/menu frames.

A frame is treated as menu/alternate display when:

- first bytes match one of `F1 60`, `F1 21`, `F1 23`, `F1 29`, or `F3 20`, or
- the menu text area `+0060..+0154` contains known menu-like labels such as `RPT SFT`, `RPT FRQ`, `SQL TYP`, `CLONETX`, `CLONERX`, `BACKUP`, `AUTO DIALER`, `TX POWER`, `MIC GAIN`, or `VOX`.

Normal display commands use the latest non-menu data frame. `display2` keeps the alternate/menu frame stream available for inspection and reverse engineering.

## 5. Normal display field map

### 5.1 Side and source fields

| Offset | Meaning | Values / notes |
|---:|---|---|
| `+0003` | Main side | `0x02` = left main, `0x01` = right main. |
| `+0006` | Left source code | See source table below. |
| `+0007` | Right source code | See source table below. |
| `+0008` | Left mode/shift | Low bits encode mode, high bits encode repeater shift. |
| `+0009` | Right mode/shift | Same format as left. |
| `+0012` | Left memory group | Valid when left source is memory. |
| `+0031` | Right memory group | Valid when right source is memory. |
| `+0019..+0021` | Left memory number | Three digit slots. |
| `+0022..+0024` | Right memory number | Three digit slots. |
| `+0027` | Left tone mode | See tone table below. |
| `+0029` | Right tone mode | See tone table below. |
| `+0032..+0039` | Left memory/name text | Latin-1/ASCII padded with NUL/spaces. |
| `+0064..+0071` | Right memory/name text | Latin-1/ASCII padded with NUL/spaces. |
| `+0096..+0103` | Left frequency digits | Eight digit slots, rendered as `XXX.xxxxx`. |
| `+0108..+0115` | Right frequency digits | Eight digit slots, rendered as `XXX.xxxxx`. |

Source codes:

| Code | Meaning |
|---:|---|
| `0x08` | VFO/main |
| `0x0A` | VFO/sub |
| `0x20` | HOME/main |
| `0x40` | MEM/main |
| `0x42` | MEM/sub |
| `0x44` | MEM/empty |
| `0x46` | MEM/empty/sub |

Memory group codes:

| Code | Meaning |
|---:|---|
| `0x00` | M-ALL |
| `0x02` | M-VHF |
| `0x03` | M-UHF |
| `0x09` | M-GRP |

Tone mode codes:

| Code | Meaning |
|---:|---|
| `0x00` | none |
| `0x01` | TN |
| `0x02` | TSQ |
| `0x03` | RTN |
| `0x04` | DCS |
| `0x05` | PR |
| `0x06` | PAG |

Mode/shift byte:

- `byte & 0x1F` gives the base receive mode.
- `0x09` = FM.
- `0x0A` = AM.
- `byte & 0x60` gives the shift marker: `0x00` none, `0x20` negative shift, `0x40` positive shift, `0x60` unknown/combined shift.

Digit fields:

- `0x00..0x09` = decimal digit.
- `0x64` = blank digit.

### 5.2 Lower status / volume / squelch area

| Offset | Meaning | Notes |
|---:|---|---|
| `+0010` | Lower label/status, left side | `S`, `SQL`, `VOL`, `S-DX`, `ASP`, `AUTO-A` plus style bits. |
| `+0011` | Lower label/status, right side | Same format as left. |
| `+0013` | Lower value/bar candidate, left side | Confirmed for left SQL/VOL overlays. |
| `+0014` | Lower value/bar candidate, right side | Inferred/symmetric right-side value. |
| `+0015` | TX/RX activity meter | Not right-side SQL/VOL. |
| `+0017` | Left VOL display raw value/segments | Numeric/raw, not ASCII. |
| `+0018` | Right VOL display raw value/segments | Inferred and shown raw. |

Base lower label values:

| Code | Label |
|---:|---|
| `0x00` | S |
| `0x01` | SQL |
| `0x02` | VOL |
| `0x20` | S-DX |
| `0x40` | ASP |
| `0x60` | AUTO-A |

The lower-label byte also carries S-meter symbol style bits. The high bits select the visible base label, while low style bits such as `0x04`, `0x08`, and `0x0C` may change the S-meter drawing style without changing the base label.

### 5.3 Activity / TX / RX meter

| Offset | Meaning | Observed values |
|---:|---|---|
| `+0004` | Activity byte | `0x00` idle, `0x02` TX/PTT, `0x04` RX/audio. |
| `+0015` | Meter raw value | Examples: `0x03` TX meter, `0x0A` RX meter. |
| `+0192` | TX flag | Idle commonly `0x10`; `0x11` indicates TX/PTT, including observed right-side TX when `+0004` remains `0x00`. |
| `+0193` | RX flag | `0x08` observed with RX/audio. |
| `+0222`, `+0442`, `+0662`, `+0882` | Repeated RX flags in blocks 1..4 | `0x08` observed during RX/audio. |

### 5.4 LCD overlays

| Offset | Meaning | Notes |
|---:|---|---|
| `+0155` | Overlay flag | `0x03` for compact LCD overlay text such as MUTE/LOCK/UNLOCK. |
| `+0157` | Declared text length | 1..6 for compact text. |
| `+0159..+0164` | Compact overlay text | Letter codes use `A=0x0A`, `B=0x0B`, ..., `Z=0x23`; printable ASCII is also passed through. |

Known compact overlay examples:

| Text | Raw compact bytes |
|---|---|
| `LOCK` | `15 18 0C 14` |
| `UNLOCK` | `1E 17 15 18 0C 14` |
| `MUTE` | May be explicit text, or inferred from `+0155 = 0x03` when text is absent. |

### 5.5 Confirmation popups

Several confirmation dialogs are carried on the normal display path instead of only on `display2`.

| Dialog | Flag | Selection | Text fields |
|---|---:|---:|---|
| PMG CLEAR | `+0155 = 0x09` | `+0156`: `0` OK, `1` CANCEL | `+0159..+0168` = `PMG MEMORY`, `+0174..+0178` = `CLEAR` |
| This radio → Other | `+0155 = 0x09` | `+0156`: `0` OK, `1` CANCEL | compact text at `+0159` and `+0174` |
| Other → This radio | `+0155 = 0x09` | `+0156`: `0` OK, `1` CANCEL | compact text at `+0159` and `+0174` |
| FACTORY RESET | `+0155 = 0x07` | `+0156`: `0` OK, `1` CANCEL | title at `+0159` |
| MEMORY LIST DELETE?/OVER WRITE? | `+0155 = 0x07` | `+0156`: `0` OK, `1` CANCEL | `+0157` length, text starts at `+0159` |

For compact prompt text, `0x64` is treated as a visible space. The byte pair `0x4A 0x51` is rendered as `->` in clone prompts.

## 6. `display2` / menu frame map

### 6.1 Menu text area

The menu decoder focuses on the block-0 menu area:

| Range | Meaning |
|---:|---|
| `+0060..+0150` | Main menu text/control area preview. |
| `+0060..+0154` | Menu-label detection area. |

Text decoding rules used by the helper decoder:

- `0x00` and `0x64` are rendered as spaces.
- Printable ASCII `0x20..0x7E` is rendered directly.
- Other bytes are shown as `\xNN`.

### 6.2 Numbered menu-list layout

Numbered menu rows have this pattern:

```text
[item_number] [attribute] [printable label...]
```

Recognized attribute bytes:

```text
0x10 0x11 0x20 0x21 0x30 0x31
```

The scanner searches `+0060..+0154` for `1..99`, then one recognized attribute byte, then a printable non-digit label. This layout was observed for setup/menu lists such as:

```text
+0061: 07 10 'TX POWER'
+0096: 08 10 'MIC GAIN'
+0131: 09 30 'VOX'
```

### 6.3 F-menu grid layout

The F-menu-like grid uses nine 9-byte text cells. For cell index `i = 0..8`:

```text
text_offset   = +0061 + 10*i
prefix_offset = text_offset - 1
text_length   = 9 bytes
```

The visible label may be shorter than the raw 9-byte cell. For example, raw text like `M->V   WE` is displayed as `M->V`.

Known quick-menu labels:

| Slot | Label |
|---:|---|
| 0 | `M->V` |
| 1 | `RPT SFT` |
| 2 | `RPT FRQ` |
| 3 | `STEP` |
| 4 | `SQL TYP` |
| 5 | `TONE` |
| 6 | `(blank)` |
| 7 | `CLONETX` |
| 8 | `CLONERX` |

## 7. Setup menu knowledge base

The web UI renders setup item names from a fixed table and uses radio frame data mainly for item IDs, selected rows, and learned values. Unknown/unlearned values are intentionally left blank or raw.

| No. | Category | Item | Options / notes |
|---:|---|---|---|
| 01 | DISPLAY | KEYPAD | submenu / second-level page observed |
| 02 | DISPLAY | LCD DIMMER | MAX / MID / OFF |
| 03 | DISPLAY | LCD CONTRAST | 1 - 5 - 9 |
| 04 | DISPLAY | BAND SCOPE | WIDE / NARROW |
| 05 | DISPLAY | S-METER SYMBOL | BARS / SCALE / CONTINUE / FULL SIZE |
| 06 | DISPLAY | BACKLIGHT COLOR | AMBER / WHITE |
| 07 | TX | TX POWER | LOW / MID / HIGH |
| 08 | TX | MIC GAIN | MIN / LOW / NORMAL / HIGH / MAX |
| 09 | TX | VOX | submenu / second-level page observed |
| 10 | TX | AUTO DIALER | ON / OFF |
| 11 | TX | TOT | OFF / 1 / 2 / 3 / 5 / 10 / 15 / 20 / 30min |
| 12 | RX | FM BANDWIDTH | WIDE / NARROW |
| 13 | RX | RX MODE | AUTO / FM / AM |
| 14 | RX | SUB BAND | submenu / second-level page observed |
| 15 | MEMORY | HOME CH | to HOME CH / Return to MEMORY |
| 16 | MEMORY | MEMORY LIST | submenu / second-level page observed |
| 17 | MEMORY | MEMORY LIST MODE | ON / OFF |
| 18 | MEMORY | PMG | submenu / second-level page observed |
| 19 | CONFIG | BEEP | OFF / LOW / HIGH |
| 20 | CONFIG | BAND SKIP | submenu / second-level page observed |
| 21 | CONFIG | RPT ARS | OFF / AUTO |
| 22 | CONFIG | RPT SHIFT | AUTO / -RPT / +RPT |
| 23 | CONFIG | RPT SHIFT FREQ | 0.00MHz to 99.95MHz |
| 24 | CONFIG | RPT REVERSE | NORMAL / REVERSE |
| 25 | CONFIG | MIC PROGRAM KEY | submenu / second-level page observed |
| 26 | CONFIG | STEP | AUTO / 5.00 / 6.25 / 8.33 / 10.00 / 12.5 / 15.00 / 20.00 / 25.00 / 50.00 / 100.00 kHz |
| 27 | CONFIG | CLOCK TYPE | A / B |
| 28 | CONFIG | APO | OFF / 0.5h ... 12.0h |
| 29 | AUDIO | REAR SP OUT | 0% to 100% |
| 30 | AUDIO | FRONT SP MUTE | CONTINUE / AUTO MUTE |
| 31 | SIGNALING | DTMF | DTMF memory |
| 32 | SIGNALING | DTMF MEMORY | 1 to 10 |
| 33 | SIGNALING | SQL TYPE | OFF / TN / TSQ / RTN / DCS / PR / PAGER ... |
| 34 | SIGNALING | TONE SQL FREQ | CTCSS 67.0-254.1Hz / DCS 023-754 |
| 35 | SIGNALING | SQL EXPANSION | ON / OFF |
| 36 | SIGNALING | PAGER CODE | submenu / second-level page observed |
| 37 | SIGNALING | PR FREQUENCY | 300Hz to 3000Hz |
| 38 | SIGNALING | BELL RINGER | OFF / 1 / 3 / 5 / 8 / CONTINUOUS |
| 39 | SIGNALING | WX ALERT | ON / OFF |
| 40 | SCAN | SCAN |  |
| 41 | SCAN | DUAL RECEIVE MODE | OFF / PRIORITY SCAN |
| 42 | SCAN | DUAL RX INTERVAL | 0.5 / 1 / 2 / 3 / 5 / 7 / 10sec |
| 43 | SCAN | PRIORITY REVERT | OFF / ON |
| 44 | SCAN | SCAN RESUME | BUSY / HOLD / 1 / 3 / 5sec |
| 45 | DATA | DATA BAND | MAIN BAND / SUB BAND / A-BAND FIX / B-BAND FIX |
| 46 | DATA | DATA SPEED | 1200 bps / 9600 bps |
| 47 | SD CARD | BACKUP | listed but not learned/implemented in the GUI yet |
| 48 | SD CARD | SD INFORMATION | listed but not learned/implemented in the GUI yet |
| 49 | SD CARD | SD FORMAT | listed but not learned/implemented in the GUI yet |
| 50 | OPTION | Bluetooth | listed but not learned/implemented in the GUI yet |
| 51 | OPTION | VOICE MEMORY | listed but not learned/implemented in the GUI yet |
| 52 | OPTION | FVS REC | listed but not learned/implemented in the GUI yet |
| 53 | OPTION | TRACK SELECT | ALL / 1 - 8 |
| 54 | OPTION | FVS PLAY | listed but not learned/implemented in the GUI yet |
| 55 | OPTION | FVS STOP | listed but not learned/implemented in the GUI yet |
| 56 | OPTION | FVS CLEAR | listed but not learned/implemented in the GUI yet |
| 57 | OPTION | VOICE GUIDE | listed but not learned/implemented in the GUI yet |
| 58 | CLONE/RESET | This -> Other | action / confirmation page observed |
| 59 | CLONE/RESET | Other -> This | action / confirmation page observed |
| 60 | CLONE/RESET | SOFTWARE VERSION | Main Ver. / Sub Ver. |
| 61 | CLONE/RESET | MEMORY CH RESET | action / confirmation page observed |
| 62 | CLONE/RESET | FACTORY RESET | action / confirmation page observed |

Real second-level pages currently treated as observed submenus:

```text
01 KEYPAD, 09 VOX, 14 SUB BAND, 16 MEMORY LIST, 18 PMG, 20 BAND SKIP, 25 MIC PROGRAM KEY, 32 DTMF MEMORY, 36 PAGER CODE
```

Action/status pages currently recognized:

```text
58 This -> Other, 59 Other -> This, 60 SOFTWARE VERSION, 61 MEMORY CH RESET, 62 FACTORY RESET
```

Items `47..57` are visible in the setup list but are kept inert/blank until their frames are learned well enough.

## 8. Power control used by the application

### 8.1 Web/UI power state

The web UI exposes an off state based on the RX-frame watchdog. When the radio is considered off, the display is greyed, all controls are disabled except POWER, and API command attempts are rejected with a radio-off message. A long POWER press runs the GPIO wake sequence.

Important state fields returned by `/api/state` include:

| Field | Meaning |
|---|---|
| `radio_powered` | Current UI/application power state inferred from valid RX frames. |
| `powering_on` | True while the GPIO replay sequence is running. |
| `power_message` | Human-readable power/mux status. |
| `power_gpio` | BCM GPIO used for CH2 replay, default 18. |
| `uart_select_gpio` | BCM GPIO used for the 74LVC157A `S` pin, default 23. |
| `rx_power_alive` | True when recent valid RX frames are present. |
| `rx_power_age_s` | Age of the latest valid RX frame. |
| `rx_power_timeout_s` | Timeout used to decide off state. |

For low-latency remote clients, the same decoded state is also available as a WebSocket text stream on `/api/state.ws`. The server sends an initial `hello` object, then `state` objects whenever the payload changes, plus periodic keepalive refreshes.

Power-related API endpoints:

| Endpoint | Method | Meaning |
|---|---|---|
| `/api/power_start` | POST | Runs the CH2 GPIO replay, switches the mux to USB-TTL TX, then waits for RX frames. |
| `/api/radio_off` | POST | Refreshes the watchdog state and selects the replay path when RX is absent. |

### 8.2 GPIO and mux behavior

At process startup, unless `--radio-start-on` is supplied, GPIO23 is driven LOW before opening the serial port. This isolates the USB-TTL TX path so its idle HIGH level cannot interfere with the wake replay. When RX frames are later seen, the application drives GPIO23 HIGH to select USB-TTL TX. When RX frames disappear, it releases active holds and drives GPIO23 LOW again.

The GPIO replay requires the `pigpio` Python module and a running `pigpiod` daemon. The recommended daemon command on Raspberry Pi is:

```bash
sudo pigpiod -s 1
```

## 9. Audio transport used by the application

The Yaesu panel cable carries analog audio separately from the serial protocol. The application uses ALSA and browser streaming for audio convenience.

### 9.1 RX audio

Default path:

```text
CM108 analog input -> ALSA arecord -> HTTP /audio.pcm -> browser AudioContext
```

Default capture format:

```text
S16_LE, mono, 48000 Hz, 10 ms chunks
```

The HTTP stream sets:

```text
Content-Type: application/octet-stream
X-Audio-Format: S16_LE
X-Audio-Rate: <rate>
X-Audio-Channels: <channels>
```

### 9.2 TX audio

Default path:

```text
browser microphone -> WebSocket /audio-tx.ws -> ALSA aplay -> CM108 analog output -> radio audio input
```

The WebSocket receives binary PCM frames. The TX audio sink can duplicate mono browser audio to stereo ALSA playback channels for common USB audio dongles.

PTT modes:

| Mode | Meaning |
|---|---|
| `serial` | Uses the serial microphone PTT command (`mic_ptt_hold`). |
| `cm108` | Uses CM108/CM119 GPIO through `/dev/hidrawN`. |
| `none` | Sends audio without keying the radio. |

Default timing:

| Parameter | Default |
|---|---:|
| PTT lead | 120 ms |
| PTT tail | 80 ms |

## 10. Capture files

The save recorder writes a reverse-engineering capture folder/zip containing:

| File | Meaning |
|---|---|
| `events.jsonl` | Chronological screen/command log. |
| `*.bin` | Raw 1100-byte RX frame when the screen changed. |
| `*.txt` | Decoded/hexdump/diff companion for each raw frame. |
| `summary.json` | Capture summary written at stop/end. |
| `README.txt` | Capture-folder notes. |

Use captures to extend this protocol file. The safest workflow is one user action per capture segment, then compare the changed byte ranges against a known baseline.

## 11. Reverse-engineering conventions

- All frame offsets are zero-based.
- `+NNNN` means decimal byte offset from the frame start.
- Hex bytes are written `0xNN` or as two hex digits in raw dumps.
- `display` means the normal dual-frequency body→panel frame stream.
- `display2` means alternate/menu/config/scope-style frames.
- Unknown fields should remain raw until at least one capture explains their behavior.
- Do not infer ASCII from numeric-looking bytes unless a field is known to be text; some values such as VOL raw `0x40` are deliberately numeric/raw even though they are printable as ASCII `@`.
