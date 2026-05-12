import AVFoundation
import Foundation

@MainActor
final class RadioViewModel: ObservableObject {
    enum BackendStatus: String {
        case disconnected
        case connecting
        case websocket
        case polling

        var label: String {
            switch self {
            case .disconnected: return "Off"
            case .connecting: return "Connecting"
            case .websocket: return "WebSocket"
            case .polling: return "Polling"
            }
        }
    }

    @Published var radioState: RadioState?
    @Published var audioState: AudioStateResponse?
    @Published var backendStatus: BackendStatus = .disconnected
    @Published var lastError: String?
    @Published var diagnosticsLog: [String] = []
    @Published var isRXAudioRunning = false
    @Published var isTXAudioRunning = false
    @Published var heldCommands: Set<String> = []

    let settings: AppSettings

    private let client: RadioAPIClient
    private let rxPlayer: PCMStreamPlayer
    private let txStreamer: MicrophoneStreamer

    private var stateSocket: URLSessionWebSocketTask?
    private var stateSocketTask: Task<Void, Never>?
    private var pollingTask: Task<Void, Never>?
    private var connectTask: Task<Void, Never>?
    private var commandFollowupTask: Task<Void, Never>?
    private var audioMonitorTask: Task<Void, Never>?
    private var rxStopTask: Task<Void, Never>?
    private var stateSocketDisabledForSession = false
    private var lastLoggedError: (message: String, time: Date)?

    init(
        settings: AppSettings,
        client: RadioAPIClient = RadioAPIClient(),
        rxPlayer: PCMStreamPlayer = PCMStreamPlayer(),
        txStreamer: MicrophoneStreamer = MicrophoneStreamer()
    ) {
        self.settings = settings
        self.client = client
        self.rxPlayer = rxPlayer
        self.txStreamer = txStreamer
    }

    func startIfNeeded() {
        guard settings.autoConnect, backendStatus == .disconnected else { return }
        connect()
    }

    func reconnect() {
        disconnect()
        connect()
    }

    func connect() {
        connectTask?.cancel()
        stateSocketDisabledForSession = false
        connectTask = Task {
            await connectFlow()
        }
    }

    func disconnect() {
        connectTask?.cancel()
        connectTask = nil
        commandFollowupTask?.cancel()
        commandFollowupTask = nil
        rxStopTask?.cancel()
        rxStopTask = nil
        pollingTask?.cancel()
        pollingTask = nil
        audioMonitorTask?.cancel()
        audioMonitorTask = nil
        stateSocketTask?.cancel()
        stateSocketTask = nil
        stateSocket?.cancel(with: .goingAway, reason: nil)
        stateSocket = nil
        stopRXAudio()
        stopTXAudio()
        backendStatus = .disconnected
        stateSocketDisabledForSession = false
    }

    func clearDiagnosticsLog() {
        diagnosticsLog.removeAll()
    }

    private func connectFlow() async {
        guard let config = settings.snapshot() else {
            setError("Set server URL, username, and password.")
            return
        }

        backendStatus = .connecting
        lastError = nil

        do {
            try await fetchAndApplyState(config: config, updateBackendStatus: false)
        } catch {
            setError(error.localizedDescription)
        }

        do {
            audioState = try await client.fetchAudioState(config: config)
        } catch {
            setError(error.localizedDescription)
        }

        openStateSocket(config: config)
        await syncRXAudioToState(config: config)
    }

    private func openStateSocket(config: ConnectionConfig) {
        guard stateSocket == nil, !stateSocketDisabledForSession else { return }

        do {
            let socket = try client.makeStateWebSocket(config: config)
            stateSocket = socket
            socket.resume()
            stateSocketTask?.cancel()
            stateSocketTask = Task { [weak self] in
                await self?.consumeStateSocket(socket: socket, config: config)
            }
        } catch {
            setError(error.localizedDescription)
            startPolling(config: config)
        }
    }

    private func consumeStateSocket(socket: URLSessionWebSocketTask, config: ConnectionConfig) async {
        do {
            while !Task.isCancelled {
                let message = try await socket.receive()
                switch message {
                case .string(let text):
                    try await handleStateSocketText(text, config: config)
                case .data(let data):
                    let text = String(decoding: data, as: UTF8.self)
                    try await handleStateSocketText(text, config: config)
                @unknown default:
                    break
                }
            }
        } catch is CancellationError {
        } catch {
            if self.stateSocket === socket {
                self.stateSocket = nil
                backendStatus = .polling
                if isBadServerResponse(error) {
                    stateSocketDisabledForSession = true
                    appendLog("Server does not expose the state WebSocket, using polling.")
                } else {
                    setError("State stream: \(error.localizedDescription)")
                }
                startPolling(config: config)
            }
        }
    }

    private func handleStateSocketText(_ text: String, config: ConnectionConfig) async throws {
        let envelope = try JSONDecoder.radioAPI.decode(StateSocketEnvelope.self, from: Data(text.utf8))
        switch envelope.type {
        case "hello":
            backendStatus = .websocket
            pollingTask?.cancel()
            pollingTask = nil
        case "state":
            if let state = envelope.state {
                radioState = state
                backendStatus = .websocket
                pollingTask?.cancel()
                pollingTask = nil
                await syncRXAudioToState(config: config)
                lastError = nil
            }
        default:
            if let state = envelope.state {
                radioState = state
                await syncRXAudioToState(config: config)
            }
        }
    }

    private func startPolling(config: ConnectionConfig) {
        guard pollingTask == nil else { return }
        pollingTask = Task { [weak self] in
            guard let self else { return }
            var iteration = 0
            while !Task.isCancelled {
                do {
                    try await self.fetchAndApplyState(config: config)
                } catch {
                    self.setError(error.localizedDescription)
                }

                iteration += 1
                if iteration % 5 == 0, self.stateSocket == nil {
                    self.openStateSocket(config: config)
                }

                try? await Task.sleep(for: .seconds(1))
            }
        }
    }

    func refreshState() {
        Task {
            guard let config = settings.snapshot() else { return }
            do {
                try await fetchAndApplyState(config: config)
                await refreshAudioState(config: config)
            } catch {
                setError(error.localizedDescription)
            }
        }
    }

    func sendPulse(_ command: String, duration: String? = nil) {
        Task {
            guard let config = settings.snapshot() else { return }
            do {
                try await client.sendCommand(command, duration: duration, config: config)
                scheduleCommandFollowupStateBurst(config: config)
                lastError = nil
            } catch {
                setError(error.localizedDescription)
            }
        }
    }

    func topButton(_ command: String, long: Bool) {
        if long {
            // The real radio setup menu is more reliable with a longer F hold
            // than the generic top-button long press.
            let duration = command == "f" ? "1200ms" : "700ms"
            sendPulse(command, duration: duration)
        } else {
            sendPulse(command, duration: "200ms")
        }
    }

    func knobPress(_ command: String, long: Bool) {
        sendPulse(command, duration: long ? "700ms" : "200ms")
    }

    func dial(_ command: String) {
        sendPulse(command, duration: "5ms")
    }

    func microphoneKey(_ command: String) {
        sendPulse(command)
    }

    func powerButton(long: Bool) {
        if radioState?.radioPowered == true {
            sendPulse("power", duration: long ? "900ms" : "200ms")
        } else {
            startPower()
        }
    }

    func startPower() {
        Task {
            guard let config = settings.snapshot() else { return }
            do {
                try await client.startPower(config: config)
                scheduleCommandFollowupStateBurst(config: config)
                lastError = nil
            } catch {
                setError(error.localizedDescription)
            }
        }
    }

    func hold(_ command: String) {
        guard !heldCommands.contains(command) else { return }
        heldCommands.insert(command)
        Task {
            guard let config = settings.snapshot() else { return }
            do {
                try await client.holdCommand(command, config: config)
                scheduleCommandFollowupStateBurst(config: config)
            } catch {
                heldCommands.remove(command)
                setError(error.localizedDescription)
            }
        }
    }

    func release(_ command: String) {
        guard heldCommands.contains(command) else { return }
        heldCommands.remove(command)
        Task {
            guard let config = settings.snapshot() else { return }
            do {
                try await client.releaseCommand(command, config: config)
                scheduleCommandFollowupStateBurst(config: config)
            } catch {
                setError(error.localizedDescription)
            }
        }
    }

    func releaseAllHeldCommands() {
        let commands = heldCommands
        heldCommands.removeAll()
        Task {
            guard let config = settings.snapshot() else { return }
            for command in commands {
                try? await client.releaseCommand(command, config: config)
            }
        }
    }

    func togglePTT() {
        Task {
            guard let config = settings.snapshot() else { return }
            do {
                _ = try await client.togglePTT(config: config)
                radioState = try await client.fetchState(config: config)
                await refreshAudioState(config: config)
                startAudioMonitorIfNeeded(config: config)
                lastError = nil
            } catch {
                setError(error.localizedDescription)
            }
        }
    }

    func toggleRXAudio() {
        if isRXAudioRunning {
            stopRXAudio()
            return
        }

        Task {
            guard let config = settings.snapshot() else { return }
            await syncRXAudioToState(config: config)
        }
    }

    func stopRXAudio() {
        rxStopTask?.cancel()
        rxStopTask = nil
        rxPlayer.stop()
        isRXAudioRunning = false
        stopAudioMonitorIfIdle()
    }

    func toggleTXAudio() {
        if isTXAudioRunning {
            endPushToTalk()
            return
        }
        beginPushToTalk()
    }

    func stopTXAudio() {
        txStreamer.stop()
        isTXAudioRunning = false
        Task {
            if backendStatus != .disconnected, let config = settings.snapshot() {
                await refreshAudioState(config: config)
            }
            stopAudioMonitorIfIdle()
        }
    }

    func ensureListening() {
        Task {
            guard let config = settings.snapshot() else { return }
            await ensureRXTransport(config: config)
        }
    }

    func beginPushToTalk() {
        guard !isTXAudioRunning else { return }

        Task {
            guard let config = settings.snapshot() else { return }

            do {
                let allowed = await requestMicrophonePermission()
                guard allowed else {
                    setError("Microphone permission denied.")
                    return
                }

                rxStopTask?.cancel()
                rxStopTask = nil
                if isRXAudioRunning {
                    stopRXAudio()
                }

                let audio = try await currentAudioState(config: config)
                let socket = try client.makeTXAudioWebSocket(config: config)
                let rate = Double(audio.tx?.rate ?? 48_000)
                let processorSize = audio.tx?.processorSize ?? 1024
                let leadTimeMs = audio.tx?.pttLeadMs ?? 120

                try txStreamer.start(socket: socket, targetRate: rate, processorSize: processorSize, leadTimeMs: leadTimeMs) { [weak self] message in
                    self?.isTXAudioRunning = false
                    self?.setError(message)
                    if let config = self?.settings.snapshot() {
                        Task { @MainActor [weak self] in
                            guard let self else { return }
                            await self.refreshAudioState(config: config)
                            await self.syncRXAudioToState(config: config)
                        }
                    }
                }

                audioState = audio
                isTXAudioRunning = true
                startAudioMonitorIfNeeded(config: config)
                lastError = nil
            } catch {
                setError(error.localizedDescription)
            }
        }
    }

    func endPushToTalk() {
        guard isTXAudioRunning else { return }
        txStreamer.stop()
        isTXAudioRunning = false

        Task {
            guard backendStatus != .disconnected, let config = settings.snapshot() else { return }
            await refreshAudioState(config: config)
            await syncRXAudioToState(config: config)
        }
    }

    private func refreshAudioState(config: ConnectionConfig) async {
        do {
            audioState = try await client.fetchAudioState(config: config)
        } catch {
            setError(error.localizedDescription)
        }
    }

    private func fetchAndApplyState(config: ConnectionConfig, updateBackendStatus: Bool = true) async throws {
        let state = try await client.fetchState(config: config)
        radioState = state
        if updateBackendStatus, backendStatus != .websocket {
            backendStatus = .polling
        }
        await syncRXAudioToState(config: config)
        lastError = nil
    }

    private func scheduleCommandFollowupStateBurst(config: ConnectionConfig) {
        commandFollowupTask?.cancel()
        commandFollowupTask = Task { [weak self] in
            guard let self else { return }
            defer {
                Task { @MainActor [weak self] in
                    self?.commandFollowupTask = nil
                }
            }

            for _ in 0 ..< 11 {
                if Task.isCancelled { return }
                do {
                    try await self.fetchAndApplyState(config: config)
                } catch {
                    if Task.isCancelled { return }
                }
                try? await Task.sleep(for: .milliseconds(120))
            }
        }
    }

    private func startListeningIfPossible(config: ConnectionConfig) async {
        guard !isTXAudioRunning, !isRXAudioRunning else { return }

        do {
            let audio = try await currentAudioState(config: config)
            let rate = Double(audio.rx?.rate ?? audio.rate ?? 48_000)
            let request = try client.makeRXAudioRequest(config: config)

            audioState = audio
            rxPlayer.start(request: request, sampleRate: rate) { [weak self] message in
                self?.isRXAudioRunning = false
                self?.setError(message)
                if let config = self?.settings.snapshot() {
                    Task { @MainActor [weak self] in
                        guard let self else { return }
                        await self.refreshAudioState(config: config)
                        self.stopAudioMonitorIfIdle()
                    }
                }
            }
            isRXAudioRunning = true
            lastError = nil
        } catch {
            setError(error.localizedDescription)
        }
    }

    private func ensureRXTransport(config: ConnectionConfig) async {
        await syncRXAudioToState(config: config)
    }

    private func syncRXAudioToState(config: ConnectionConfig) async {
        if isTXAudioRunning {
            rxStopTask?.cancel()
            rxStopTask = nil
            if isRXAudioRunning {
                stopRXAudio()
            }
            return
        }

        if isRadioReceiving {
            rxStopTask?.cancel()
            rxStopTask = nil
            if !isRXAudioRunning {
                await startListeningIfPossible(config: config)
            }
            return
        }

        scheduleRXStopIfNeeded()
    }

    private func scheduleRXStopIfNeeded() {
        guard isRXAudioRunning, rxStopTask == nil else { return }
        rxStopTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(280))
            await MainActor.run {
                guard let self else { return }
                self.rxStopTask = nil
                guard self.isRXAudioRunning, !self.isRadioReceiving, !self.isTXAudioRunning else { return }
                self.stopRXAudio()
            }
        }
    }

    private func currentAudioState(config: ConnectionConfig) async throws -> AudioStateResponse {
        if let audioState {
            return audioState
        }
        let fresh = try await client.fetchAudioState(config: config)
        audioState = fresh
        return fresh
    }

    private func setError(_ message: String) {
        lastError = message
        appendLog(message)
    }

    private func appendLog(_ message: String) {
        let trimmed = message.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        if let lastLoggedError,
           lastLoggedError.message == trimmed,
           Date().timeIntervalSince(lastLoggedError.time) < 8 {
            return
        }
        let timestamp = DateFormatter.logTimestamp.string(from: Date())
        diagnosticsLog.append("[\(timestamp)] \(trimmed)")
        self.lastLoggedError = (trimmed, Date())
        if diagnosticsLog.count > 120 {
            diagnosticsLog.removeFirst(diagnosticsLog.count - 120)
        }
    }

    private func startAudioMonitorIfNeeded(config: ConnectionConfig) {
        guard isTXAudioRunning || radioState?.pttLatched == true else { return }
        guard audioMonitorTask == nil else { return }

        audioMonitorTask = Task { [weak self] in
            guard let self else { return }

            while !Task.isCancelled {
                await self.refreshAudioState(config: config)

                if !self.isRXAudioRunning, !self.isTXAudioRunning, self.radioState?.pttLatched != true {
                    self.audioMonitorTask = nil
                    return
                }

                try? await Task.sleep(for: .milliseconds(900))
            }
        }
    }

    private func stopAudioMonitorIfIdle() {
        guard !isRXAudioRunning, !isTXAudioRunning, radioState?.pttLatched != true else { return }
        audioMonitorTask?.cancel()
        audioMonitorTask = nil
    }

    private func requestMicrophonePermission() async -> Bool {
        if #available(iOS 17.0, *) {
            return await withCheckedContinuation { continuation in
                AVAudioApplication.requestRecordPermission { granted in
                    continuation.resume(returning: granted)
                }
            }
        } else {
            return await withCheckedContinuation { continuation in
                AVAudioSession.sharedInstance().requestRecordPermission { granted in
                    continuation.resume(returning: granted)
                }
            }
        }
    }

    private func isBadServerResponse(_ error: Error) -> Bool {
        let nsError = error as NSError
        if nsError.domain == NSURLErrorDomain, nsError.code == URLError.badServerResponse.rawValue {
            return true
        }
        return false
    }

    private var isRadioReceiving: Bool {
        radioState?.left.rxActive == true || radioState?.right.rxActive == true
    }
}

private extension DateFormatter {
    static let logTimestamp: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss"
        return formatter
    }()
}
