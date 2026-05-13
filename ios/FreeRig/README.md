# FreeRIG

SwiftUI iOS client for the `ftm150.py` backend.

## Current Status

- Backend connection with Basic Auth.
- Live state via `state.ws`, with internal polling fallback in the view model.
- Radio commands via REST:
  - `/api/command`
  - `/api/command_hold`
  - `/api/command_release`
  - `/api/power_start`
  - `/api/ptt_toggle`
- RX audio from `/audio.pcm`.
- Microphone TX to `/audio-tx.ws`.

## Open The Project

Open in Xcode:

- `ios/FreeRIG/FreeRIG.xcodeproj`

## Notes

- The app emulates the physical controls of the radio. This already makes it possible to navigate menus and setup without immediately duplicating the full web frontend rendering logic.
- Menu rendering in the app is still summarized for now, not a 1:1 copy of every special browser view.
