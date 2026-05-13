import AVFoundation
import Foundation

final class PCMStreamPlayer {
    private let session: URLSession
    private var engine: AVAudioEngine?
    private var playerNode: AVAudioPlayerNode?
    private var outputFormat: AVAudioFormat?
    private var streamTask: Task<Void, Never>?
    private let state = PlaybackState()
    private var chunkBytes = 3840
    private var startThresholdBuffers = 2

    init(session: URLSession = .shared) {
        self.session = session
    }

    var isRunning: Bool {
        streamTask != nil
    }

    func start(request: URLRequest, sampleRate: Double, onError: @escaping @MainActor (String) -> Void) {
        stop()

        let samplesPerChunk = max(960, Int(sampleRate * 0.04))
        chunkBytes = samplesPerChunk * MemoryLayout<Int16>.size
        startThresholdBuffers = 2

        let engine = AVAudioEngine()
        let playerNode = AVAudioPlayerNode()
        let format = AVAudioFormat(standardFormatWithSampleRate: sampleRate, channels: 1)!

        engine.attach(playerNode)
        engine.connect(playerNode, to: engine.mainMixerNode, format: format)

        do {
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(.playback, mode: .default)
            try audioSession.setPreferredSampleRate(sampleRate)
            try audioSession.setActive(true)
            engine.prepare()
            try engine.start()
        } catch {
            Task { @MainActor in
                onError("RX audio: \(error.localizedDescription)")
            }
            return
        }

        self.engine = engine
        self.playerNode = playerNode
        self.outputFormat = format
        playerNode.volume = 1
        state.reset()

        streamTask = Task { [weak self] in
            guard let self else { return }
            do {
                let (bytes, response) = try await self.session.bytes(for: request)
                guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                    throw RadioAPIError.invalidResponse
                }

                var pending = Data()
                for try await byte in bytes {
                    if Task.isCancelled { break }
                    pending.append(byte)

                    while pending.count >= self.chunkBytes {
                        let chunk = pending.prefix(self.chunkBytes)
                        pending.removeFirst(self.chunkBytes)
                        self.schedulePCM16Chunk(chunk)
                    }
                }

                let evenCount = pending.count - (pending.count % 2)
                if evenCount > 0 {
                    self.schedulePCM16Chunk(pending.prefix(evenCount))
                }
            } catch is CancellationError {
            } catch {
                await MainActor.run {
                    onError("RX audio: \(error.localizedDescription)")
                }
            }
        }
    }

    func stop() {
        streamTask?.cancel()
        streamTask = nil
        state.reset()
        playerNode?.stop()
        engine?.stop()
        playerNode = nil
        engine = nil
        outputFormat = nil
    }

    private func schedulePCM16Chunk(_ data: Data) {
        guard let outputFormat, let playerNode else { return }

        let sampleCount = data.count / MemoryLayout<Int16>.size
        guard sampleCount > 0 else { return }
        guard let buffer = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: AVAudioFrameCount(sampleCount)),
              let channel = buffer.floatChannelData?.pointee else {
            return
        }

        buffer.frameLength = AVAudioFrameCount(sampleCount)
        data.withUnsafeBytes { rawBuffer in
            let samples = rawBuffer.bindMemory(to: Int16.self)
            for index in 0 ..< sampleCount {
                channel[index] = max(-1.0, min(1.0, Float(samples[index]) / 32768.0))
            }
        }

        let queued = state.incrementQueue()
        playerNode.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            self?.state.decrementQueue()
        }

        if !playerNode.isPlaying, queued >= startThresholdBuffers {
            playerNode.play()
            state.markStarted()
        } else if state.hasStarted, !playerNode.isPlaying {
            playerNode.play()
        }
    }
}

final class MicrophoneStreamer {
    private let engine = AVAudioEngine()
    private let sendGate = SendGate()
    private let sendErrorState = SendErrorState()

    private var socket: URLSessionWebSocketTask?
    private var socketWriter: PCMWebSocketWriter?
    private var receiveTask: Task<Void, Never>?
    private var openGateTask: Task<Void, Never>?
    private var converter: AVAudioConverter?
    private var targetFormat: AVAudioFormat?

    private(set) var isRunning = false

    func start(
        socket: URLSessionWebSocketTask,
        targetRate: Double,
        processorSize: Int,
        leadTimeMs: Int,
        onError: @escaping @MainActor (String) -> Void
    ) throws {
        stop()

        let audioSession = AVAudioSession.sharedInstance()
        try audioSession.setCategory(.playAndRecord, mode: .default, options: [.defaultToSpeaker, .allowBluetoothA2DP])
        try audioSession.setPreferredSampleRate(targetRate)
        try? audioSession.setPreferredInputNumberOfChannels(1)
        try audioSession.setPreferredIOBufferDuration(0.02)
        if let builtInMic = audioSession.availableInputs?.first(where: { $0.portType == .builtInMic }) {
            try? audioSession.setPreferredInput(builtInMic)
        }
        try audioSession.setActive(true)

        self.socket = socket
        self.socketWriter = PCMWebSocketWriter(socket: socket)
        self.sendErrorState.reset()
        self.sendGate.close()
        socket.resume()

        receiveTask = Task { [weak self] in
            do {
                while self != nil, !Task.isCancelled {
                    _ = try await socket.receive()
                }
            } catch is CancellationError {
            } catch {
                await MainActor.run {
                    onError("TX audio: \(error.localizedDescription)")
                }
            }
        }

        openGateTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(max(0, leadTimeMs)))
            self?.sendGate.open()
        }

        let inputNode = engine.inputNode
        let inputFormat = inputNode.inputFormat(forBus: 0)
        let targetFormat = AVAudioFormat(standardFormatWithSampleRate: targetRate, channels: 1)!
        let converter = AVAudioConverter(from: inputFormat, to: targetFormat)

        if converter == nil,
           (inputFormat.sampleRate != targetRate ||
               inputFormat.channelCount != 1 ||
               inputFormat.commonFormat != .pcmFormatFloat32) {
            throw RadioAPIError.server("Unsupported iOS microphone format.")
        }

        self.targetFormat = targetFormat
        self.converter = converter

        inputNode.removeTap(onBus: 0)
        inputNode.installTap(onBus: 0, bufferSize: AVAudioFrameCount(max(256, processorSize)), format: inputFormat) { [weak self] buffer, _ in
            self?.send(buffer: buffer, onError: onError)
        }

        engine.prepare()
        try engine.start()
        isRunning = true
    }

    func stop() {
        let currentSocket = socket
        openGateTask?.cancel()
        openGateTask = nil
        receiveTask?.cancel()
        receiveTask = nil
        sendGate.close()
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        if let currentSocket {
            Task {
                try? await currentSocket.send(.string("stop"))
                currentSocket.cancel(with: .normalClosure, reason: nil)
            }
        }
        socket = nil
        socketWriter = nil
        converter = nil
        targetFormat = nil
        sendErrorState.reset()
        isRunning = false
    }

    private func send(buffer: AVAudioPCMBuffer, onError: @escaping @MainActor (String) -> Void) {
        guard sendGate.tryBeginSend() else { return }
        guard let socketWriter else { return }
        guard let samples = normalizedSamples(from: buffer) else { return }

        let payload = encodePCM16(samples)
        guard !payload.isEmpty else { return }

        Task { [weak self] in
            defer { self?.sendGate.finishSend() }
            do {
                try await socketWriter.send(payload)
            } catch {
                guard let self, self.sendErrorState.markIfNeeded() else { return }
                await MainActor.run {
                    onError("TX audio: \(error.localizedDescription)")
                }
            }
        }
    }

    private func normalizedSamples(from buffer: AVAudioPCMBuffer) -> [Float]? {
        if buffer.format.commonFormat == .pcmFormatFloat32,
           buffer.format.channelCount == 1,
           let channel = buffer.floatChannelData?.pointee {
            let frames = Int(buffer.frameLength)
            return Array(UnsafeBufferPointer(start: channel, count: frames))
        }

        guard let targetFormat, let converter else { return nil }

        let capacityRatio = targetFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(max(32, Int(Double(buffer.frameLength) * capacityRatio) + 32))
        guard let outputBuffer = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: capacity) else {
            return nil
        }

        var error: NSError?
        var didFeedInput = false
        let status = converter.convert(to: outputBuffer, error: &error) { _, outStatus in
            if didFeedInput {
                outStatus.pointee = .noDataNow
                return nil
            }
            didFeedInput = true
            outStatus.pointee = .haveData
            return buffer
        }

        guard error == nil else { return nil }
        guard status == .haveData || status == .inputRanDry else { return nil }
        guard let channel = outputBuffer.floatChannelData?.pointee else { return nil }
        let frames = Int(outputBuffer.frameLength)
        return Array(UnsafeBufferPointer(start: channel, count: frames))
    }

    private func encodePCM16(_ samples: [Float]) -> Data {
        guard !samples.isEmpty else { return Data() }

        var output = Data(capacity: samples.count * MemoryLayout<Int16>.size)
        for sample in samples {
            let limited = max(-1.0, min(1.0, sample))
            var value = limited < 0
                ? Int16(limited * 32768.0)
                : Int16(limited * 32767.0)
            withUnsafeBytes(of: &value) { output.append(contentsOf: $0) }
        }
        return output
    }
}

private final class PlaybackState {
    private let lock = NSLock()
    private var queuedBuffers = 0
    private(set) var hasStarted = false

    func incrementQueue() -> Int {
        lock.lock()
        defer { lock.unlock() }
        queuedBuffers += 1
        return queuedBuffers
    }

    func decrementQueue() {
        lock.lock()
        queuedBuffers = max(0, queuedBuffers - 1)
        lock.unlock()
    }

    func markStarted() {
        lock.lock()
        hasStarted = true
        lock.unlock()
    }

    func reset() {
        lock.lock()
        queuedBuffers = 0
        hasStarted = false
        lock.unlock()
    }
}

private actor PCMWebSocketWriter {
    private let socket: URLSessionWebSocketTask

    init(socket: URLSessionWebSocketTask) {
        self.socket = socket
    }

    func send(_ data: Data) async throws {
        try await socket.send(.data(data))
    }
}

private final class SendGate {
    private let lock = NSLock()
    private var openValue = false
    private var sendInFlight = false

    func open() {
        lock.lock()
        openValue = true
        lock.unlock()
    }

    func close() {
        lock.lock()
        openValue = false
        sendInFlight = false
        lock.unlock()
    }

    func tryBeginSend() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard openValue, !sendInFlight else { return false }
        sendInFlight = true
        return true
    }

    func finishSend() {
        lock.lock()
        sendInFlight = false
        lock.unlock()
    }
}

private final class SendErrorState {
    private let lock = NSLock()
    private var hasReported = false

    func reset() {
        lock.lock()
        hasReported = false
        lock.unlock()
    }

    func markIfNeeded() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard !hasReported else { return false }
        hasReported = true
        return true
    }
}
