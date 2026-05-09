# Yaesu FTM-150 Remote Panel Controller

<p align="center">
  <img src="https://github.com/emanuelelaface/FTM150/blob/main/images/screenshot1.png" alt="Schematic" style="width: 100%;">
</p>

Experimental Python controller and web front panel for the **Yaesu FTM-150** transceiver.

The application emulates the detachable front-panel data link, continuously sends Yaesu panel-to-body idle frames, injects key/knob/microphone events, decodes the body-to-panel display stream, can power on the radio body through a captured GPIO wake waveform, and optionally exposes a browser-based control surface with RX/TX audio through a USB sound card.

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
- Cold power-on support using a Raspberry Pi GPIO replay of the captured CH2/pin-6 waveform.
- RX-frame watchdog: when body→panel frames stop, the GUI treats the radio as off, greys the display, and disables all controls except POWER.

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

Recommended full-duplex test wiring with one USB-TTL adapter, when the radio is already on:

```text
USB-TTL TX  -> radio BODY RX / panel→body line, preferably through 1 kΩ–4.7 kΩ
USB-TTL RX  <- radio BODY TX / body→panel line, preferably through 4.7 kΩ–10 kΩ
USB-TTL GND -> radio GND
```

For cold power-on from the web GUI, the USB-TTL TX line must not be tied directly to pin 6 during the wake replay. Use a 74LVC157A or equivalent 2-to-1 multiplexer so the radio pin 6 can be switched between the Raspberry Pi wake GPIO and the USB-TTL TX output:

```text
Raspberry GPIO18  -> 74LVC157A 1I0  # captured CH2/pin-6 wake waveform
USB-TTL TX        -> 74LVC157A 1I1  # normal UART TX after power-on
74LVC157A 1Y      -> radio pin 6 / body RX
Raspberry GPIO23  -> 74LVC157A S    # LOW = GPIO18 replay, HIGH = USB-TTL TX
74LVC157A /E      -> GND            # output always enabled
74LVC157A VCC     -> 3.3 V
All grounds       -> common GND

USB-TTL RX        <- radio pin 7 / body TX
```

The wake GPIO is released as input/high-impedance after the replay, then GPIO23 selects the normal USB-TTL TX path.

Important safety notes:

- Use a **3.3 V TTL** serial adapter. Do not use RS-232 voltage levels.
- Do not connect two push-pull TX outputs to the same line. If the wake GPIO and USB-TTL TX both need to reach pin 6, use the 74LVC157A mux wiring above or another real switch/buffer solution.
- Pin 5 is +13 V. Keep it away from TTL adapters and sound-card inputs.
- The audio is analog and separate from the UART protocol. This project captures/playbacks audio through a USB sound card such as a CM108/CM119 interface.

## Software requirements

Runtime Python dependencies are intentionally small:

```bash
python3 -m pip install pyserial pigpio
```

On Linux/Raspberry Pi, the GPIO wake replay uses the `pigpiod` daemon, and the audio features use standard ALSA tools:

```bash
sudo apt install pigpio alsa-utils
sudo pigpiod -s 1
```

Browser TX audio needs a browser that supports `getUserMedia()` and WebSocket binary audio. Microphone access usually requires `localhost` or HTTPS.

## Quick start: web GUI

With the GPIO18/GPIO23 power-on mux hardware installed:

```bash
sudo pigpiod -s 1
python3 ftm150.py --port /dev/ttyUSB0 --web-port 8080
```

The default GPIOs are:

```text
GPIO18 = captured CH2 wake replay to radio pin 6 through the mux
GPIO23 = 74LVC157A S select, LOW=replay GPIO, HIGH=USB-TTL TX
```

If the radio is already on when the program starts, use:

```bash
python3 ftm150.py --port /dev/ttyUSB0 --web-port 8080 --radio-start-on
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
| `--power-gpio` | `18` | Raspberry Pi BCM GPIO that replays the captured CH2/pin-6 wake waveform. |
| `--uart-select-gpio` | `23` | Raspberry Pi BCM GPIO connected to the 74LVC157A `S` pin; LOW selects GPIO replay, HIGH selects USB-TTL TX. |
| `--radio-start-on` | off | Initial hint that the radio is already on; the RX watchdog still becomes authoritative once frames are seen or lost. |
| `--rx-power-timeout` | `1.2` | Seconds without valid body→panel RX frames before the GUI considers the radio off. |

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
| `/api/power_start` | POST | Run the GPIO CH2 wake replay, switch the mux to USB-TTL TX, then wait for RX frames. |
| `/api/radio_off` | POST | Refresh/mark radio-off state from the RX watchdog and select the replay GPIO path if RX is absent. |

`/api/state` also includes power-watchdog fields such as `radio_powered`, `powering_on`, `power_message`, `rx_power_alive`, `rx_power_age_s`, and the configured GPIO numbers.

Example command POST:

```bash
curl -X POST http://127.0.0.1:8080/api/command \
  -H 'Content-Type: application/json' \
  -d '{"command":"band","duration":"80f"}'
```

## Power control behavior

The web GUI derives the visible power state from live body→panel RX frames, not from a remembered button state. If valid RX frames stop for `--rx-power-timeout`, the display is greyed, all controls except POWER are disabled, and GPIO23 selects the GPIO replay path.

A long press on POWER while the GUI is off calls `/api/power_start`. The application sets GPIO23 LOW, replays the captured CH2 waveform on GPIO18, releases GPIO18 as high-impedance, sets GPIO23 HIGH, and then waits for valid RX frames before marking the UI as on.

The normal `power` command in the command table is still a panel-key command for an already-communicating radio; cold startup uses the GPIO replay path instead.

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
- Pin 4 is still not used/confirmed by this project.
- Several menu/setup values are decoded from observed captures; unobserved values are intentionally left as raw/unknown instead of guessed.
- The original front panel must not drive the same TX line while this program drives it.
- Cold power-on depends on the captured CH2 waveform and on the GPIO/74LVC157A mux hardware; a normal USB-TTL TX output alone is not high-impedance and can prevent the wake replay from working.
- The GPIO wake replay requires `pigpiod` running with enough timing resolution, for example `sudo pigpiod -s 1`.
- Transmitting is radio hardware control. Follow local amateur-radio regulations and test into a dummy load where appropriate.

## Suggested GitHub metadata

Suggested short description:

> Experimental Yaesu FTM-150 detachable-panel emulator, web control surface, audio bridge, and reverse-engineered protocol notes.

Suggested topics:

```text
yaesu ftm-150 ham-radio amateur-radio reverse-engineering serial ttl cm108 raspberry-pi
```
