# FreeRig

Client iOS SwiftUI per il backend `ftm150.py`.

## Stato attuale

- Connessione backend con Basic Auth.
- Stato live via `state.ws`, con fallback interno a polling lato view model.
- Comandi radio via REST:
  - `/api/command`
  - `/api/command_hold`
  - `/api/command_release`
  - `/api/power_start`
  - `/api/ptt_toggle`
- RX audio da `/audio.pcm`.
- TX microfono verso `/audio-tx.ws`.

## Aprire il progetto

Apri in Xcode:

- `ios/FreeRig/FreeRig.xcodeproj`

## Note

- L'app emula i controlli fisici della radio. Questo permette già di navigare menu e setup senza dover duplicare subito tutta la logica grafica del frontend web.
- Il rendering dei menu nell'app è per ora riassuntivo, non una copia 1:1 delle viste speciali del browser.
