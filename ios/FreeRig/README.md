# FreeRIG

SwiftUI iOS client for the `ftm150.py` backend.

## Current Status

- Backend authentication through Apache `mod_auth_form`.
- Login is performed with `POST /ftm150-login` using the configured username and password.
- The Apache `FTM150SESSION` cookie is reused for REST requests, RX audio and WebSocket handshakes.
- Credentials remain stored as before: username in `UserDefaults`, password in the iOS Keychain.
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

## Authentication Flow

1. The user configures server URL, username and password in the app.
2. On connection the app sends the credentials to `/ftm150-login` as `httpd_username` and `httpd_password`.
3. Apache validates the credentials using the server-side password file and creates `FTM150SESSION`.
4. The app stores the cookie through `HTTPCookieStorage` and explicitly attaches it to protected requests.
5. For `wss://` WebSocket handshakes the cookie is explicitly copied into the `Cookie` header.
6. On disconnect/reconnect the old FTM150 session cookie is removed and a fresh login is performed.

The app no longer sends HTTP Basic Authentication headers.

## Notes

- The app emulates the physical controls of the radio. This already makes it possible to navigate menus and setup without immediately duplicating the full web frontend rendering logic.
- Menu rendering in the app is still summarized for now, not a 1:1 copy of every special browser view.
