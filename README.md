# Yaesu FTM-150 Remote Panel Controller

<p align="center">
  <img src="https://github.com/emanuelelaface/FTM150/blob/main/images/screenshot1.png" alt="Schematic" style="width: 100%;">
</p>

Experimental Python controller and web front panel for the **Yaesu FTM-150** transceiver.

The application emulates the detachable front-panel data link, continuously sends Yaesu panel-to-body idle frames, injects key/knob/microphone events, decodes the body-to-panel display stream, and optionally exposes a browser-based control surface with RX/TX audio through a USB sound card.

> Status: this is a reverse-engineered project. The protocol map is partial, but unknown fields are kept visible through raw dumps, diffs, and capture files so more frames can be documented over time.

## Main features

- Web GUI that resembles the FTM-150 front panel.
- Serial panel emulator: **500000 baud, 8N1, 3.3 V TTL**.
- Continuous panel-to-radio idle frame transmission.
- Momentary and held commands for front-panel keys, rotary encoders, PTT, microphone keys, and microphone keypad digits.
- Display/status decoder for normal dual-frequency screens.
- Separate `display2` / menu stream handling for setup and alternate screens.
- RX audio streaming from ALSA/CM108 to the browser.
- Browser microphone TX audio back to ALSA/CM108.
- PTT options through serial microphone emulation, CM108/CM119 GPIO, or disabled PTT.
- Capture recorder for reverse engineering (`save start`, `save stop` workflow in the GUI/decoder tools).

## Repository layout

```text
.
├── ftm150.py      # main application
├── README.md      # project overview and usage
└── PROTOCOL.md    # hardware and protocol notes
```

## Hardware overview

The radio body and detachable front panel communicate over an 8-pin cable. The serial data lines are independent from the analog audio path.

| Pin | Signal | Notes |
|---:|---|---|
| 1 | Microphone | Analog microphone path. |
| 2 | Audio GND | Audio ground. |
| 3 | Speaker | Analog speaker/audio path. |
| 4 | +3.3V | Not used (?). |
| 5 | +13 V | Radio supply line. Treat as power, not logic. |
| 6 | Data: front panel → radio body | 3.3 V TTL UART, 500000 baud. |
| 7 | Data: radio body → front panel | 3.3 V TTL UART, 500000 baud. |
| 8 | GND | Common ground. |

Recommended full-duplex test wiring with one USB-TTL adapter:

```text
USB-TTL TX  -> radio BODY RX / panel→body line, preferably through 1 kΩ–4.7 kΩ
USB-TTL RX  <- radio BODY TX / body→panel line, preferably through 4.7 kΩ–10 kΩ
USB-TTL GND -> radio GND
```

Important safety notes:

- Use a **3.3 V TTL** serial adapter. Do not use RS-232 voltage levels.
- Do not connect two push-pull TX outputs to the same line. If this script drives the body RX line, disconnect the original front-panel TX from that line.
- Pin 5 is +13 V. Keep it away from TTL adapters and sound-card inputs.
- The audio is analog and separate from the UART protocol. This project captures/playbacks audio through a USB sound card such as a CM108/CM119 interface.

## Software requirements

Runtime Python dependencies are intentionally small:

```bash
python3 -m pip install pyserial
```

On Linux/Raspberry Pi, the audio features use standard ALSA tools:

```bash
sudo apt install alsa-utils
```

Browser TX audio needs a browser that supports `getUserMedia()` and WebSocket binary audio. Microphone access usually requires `localhost` or HTTPS.

## Quick start: web GUI

```bash
python3 ftm150.py --port /dev/ttyUSB0 --web-port 8080
```

Open:

```text
http://127.0.0.1:8080/
```

LAN access is enabled by default because the web server binds to `0.0.0.0`. Use a firewall, VPN, SSH tunnel, or HTTPS reverse proxy if you expose it outside your trusted network.

### Demo mode

```bash
python3 ftm150.py --demo --web-port 8080
```

Demo mode starts the GUI without opening the serial port or talking to a radio.

### Typical Raspberry Pi + CM108 command

```bash
python3 ftm150.py \
  --port /dev/ttyUSB0 \
  --web-port 8080 \
  --audio-device plughw:0,0 \
  --tx-audio-device plughw:0,0 \
  --tx-ptt-mode serial
```

For CM108/CM119 GPIO PTT instead of serial microphone PTT:

```bash
python3 ftm150.py \
  --port /dev/ttyUSB0 \
  --web-port 8080 \
  --audio-device plughw:0,0 \
  --tx-audio-device plughw:0,0 \
  --tx-ptt-mode cm108 \
  --cm108-hidraw auto \
  --cm108-gpio 3
```

## Useful command-line options

| Option | Default | Description |
|---|---:|---|
| `--port` | `/dev/ttyUSB0` | Serial adapter connected to the radio body data lines. |
| `--baud` | `500000` | UART speed. The mapped protocol uses 500000 baud. |
| `--web-port` | `8080` | HTTP port for the GUI. |
| `--demo` | off | Run GUI without serial/radio. |
| `--no-rx` | off | Disable body→panel display RX decoding. |
| `--no-tx` | off | Disable panel→body frame transmission. |
| `--decode` | off | Show the decode/save panel in the GUI. |
| `--no-audio` | off | Disable RX audio streaming. |
| `--audio-device` | `plughw:0,0` | ALSA capture device for RX audio. |
| `--no-tx-audio` | off | Disable browser microphone TX audio. |
| `--tx-audio-device` | `plughw:0,0` | ALSA playback device for TX audio. |
| `--tx-ptt-mode` | `serial` | `serial`, `cm108`, or `none`. |
| `--ssl-cert` / `--ssl-key` | unset | Enable HTTPS/WSS when both are supplied. |

## Browser/API endpoints

The web interface is self-contained in `ftm150.py`.

| Endpoint | Method | Purpose |
|---|---|---|
| `/` | GET | Web front panel. |
| `/api/state` | GET | Current decoded radio state. |
| `/api/audio` | GET | RX/TX audio status. |
| `/api/commands` | GET | Command and alias list. |
| `/audio.pcm` | GET | Streaming raw RX PCM audio. |
| `/audio-tx.ws` | WebSocket | Browser microphone TX PCM stream. |
| `/api/command` | POST | Momentary command pulse. |
| `/api/command_hold` | POST | Named command hold. |
| `/api/command_release` | POST | Clear named command hold. |
| `/api/ptt_toggle` | POST | Toggle latched PTT. |
| `/api/save` | POST | Capture-recorder control. |

Example command POST:

```bash
curl -X POST http://127.0.0.1:8080/api/command \
  -H 'Content-Type: application/json' \
  -d '{"command":"band","duration":"80f"}'
```

## Command names

See [`PROTOCOL.md`](PROTOCOL.md) for the byte-level command table. Common commands include:

```text
band f pmg vm sdx power
ul_press ur_press bl_press br_press
ul_left ul_right ur_left ur_right bl_left bl_right br_left br_right
mic_ptt mic_ptt_hold mic_up mic_down mic_mute
mic_p1 mic_p2 mic_p3 mic_p4
mic_0 ... mic_9 mic_star mic_hash mic_a mic_b mic_c mic_d
```

Durations can be given as:

```text
80f      # exact frame count
250ms    # milliseconds
250      # milliseconds, legacy shorthand
```

## Capture and reverse engineering workflow

Use the decode/save panel or console save commands to record screen changes and command events. A capture contains raw `.bin` RX frames, decoded `.txt` companions, `events.jsonl`, and `summary.json`.

This is the recommended way to document unknown bytes:

1. Start a save capture.
2. Perform one radio action at a time.
3. Stop the capture.
4. Compare the raw frames and update `PROTOCOL.md` when the mapping is understood.

## Known limitations

- The protocol is only partially mapped.
- Pins 4 and 8 are still unknown.
- Several menu/setup values are decoded from observed captures; unobserved values are intentionally left as raw/unknown instead of guessed.
- The original front panel must not drive the same TX line while this program drives it.
- Transmitting is radio hardware control. Follow local amateur-radio regulations and test into a dummy load where appropriate.

## Suggested GitHub metadata

Suggested short description:

> Experimental Yaesu FTM-150 detachable-panel emulator, web control surface, audio bridge, and reverse-engineered protocol notes.

Suggested topics:

```text
yaesu ftm-150 ham-radio amateur-radio reverse-engineering serial ttl cm108 raspberry-pi
```
