import Foundation

enum RadioAPIError: LocalizedError {
    case invalidConfiguration
    case invalidResponse
    case server(String)

    var errorDescription: String? {
        switch self {
        case .invalidConfiguration:
            return "Invalid server configuration."
        case .invalidResponse:
            return "Invalid server response."
        case .server(let message):
            return message
        }
    }
}

final class RadioAPIClient {
    private let session: URLSession

    init(session: URLSession = .shared) {
        self.session = session
    }

    func fetchState(config: ConnectionConfig) async throws -> RadioState {
        let request = try makeRequest(config: config, path: "/api/state", method: "GET")
        let (data, response) = try await session.data(for: request)
        try validate(response: response, data: data)
        return try JSONDecoder.radioAPI.decode(RadioState.self, from: data)
    }

    func fetchAudioState(config: ConnectionConfig) async throws -> AudioStateResponse {
        let request = try makeRequest(config: config, path: "/api/audio", method: "GET")
        let (data, response) = try await session.data(for: request)
        try validate(response: response, data: data)
        return try JSONDecoder.radioAPI.decode(AudioStateResponse.self, from: data)
    }

    func sendCommand(_ command: String, duration: String? = nil, config: ConnectionConfig) async throws {
        let body = ["command": command, "duration": duration] as [String: String?]
        _ = try await performJSONCommand(path: "/api/command", body: body, config: config)
    }

    func holdCommand(_ command: String, config: ConnectionConfig) async throws {
        _ = try await performJSONCommand(path: "/api/command_hold", body: ["command": command], config: config)
    }

    func releaseCommand(_ command: String, config: ConnectionConfig) async throws {
        _ = try await performJSONCommand(path: "/api/command_release", body: ["command": command], config: config)
    }

    func startPower(config: ConnectionConfig) async throws {
        _ = try await performJSONCommand(path: "/api/power_start", body: [String: String](), config: config)
    }

    func togglePTT(config: ConnectionConfig) async throws -> CommandResponse {
        try await performJSONCommand(path: "/api/ptt_toggle", body: [String: String](), config: config)
    }

    func makeStateWebSocket(config: ConnectionConfig) throws -> URLSessionWebSocketTask {
        let url = config.webSocketEndpoint(path: "/api/state.ws")
        var request = URLRequest(url: url)
        request.timeoutInterval = 30
        request.setValue(config.basicAuthorizationHeader, forHTTPHeaderField: "Authorization")
        return session.webSocketTask(with: request)
    }

    func makeTXAudioWebSocket(config: ConnectionConfig) throws -> URLSessionWebSocketTask {
        let url = config.webSocketEndpoint(path: "/audio-tx.ws")
        var request = URLRequest(url: url)
        request.timeoutInterval = 30
        request.setValue(config.basicAuthorizationHeader, forHTTPHeaderField: "Authorization")
        return session.webSocketTask(with: request)
    }

    func makeRXAudioRequest(config: ConnectionConfig) throws -> URLRequest {
        var request = try makeRequest(config: config, path: "/audio.pcm", method: "GET")
        request.setValue("application/octet-stream", forHTTPHeaderField: "Accept")
        return request
    }

    private func performJSONCommand<Body: Encodable>(path: String, body: Body, config: ConnectionConfig) async throws -> CommandResponse {
        let request = try makeRequest(config: config, path: path, method: "POST", body: body)
        let (data, response) = try await session.data(for: request)
        try validate(response: response, data: data)
        return try JSONDecoder.radioAPI.decode(CommandResponse.self, from: data)
    }

    private func makeRequest(config: ConnectionConfig, path: String, method: String) throws -> URLRequest {
        let url = config.endpoint(path: path)
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 30
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue(config.basicAuthorizationHeader, forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return request
    }

    private func makeRequest<Body: Encodable>(config: ConnectionConfig, path: String, method: String, body: Body) throws -> URLRequest {
        var request = try makeRequest(config: config, path: path, method: method)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(body)
        return request
    }

    private func validate(response: URLResponse, data: Data) throws {
        guard let http = response as? HTTPURLResponse else {
            throw RadioAPIError.invalidResponse
        }

        guard (200 ..< 300).contains(http.statusCode) else {
            if let commandResponse = try? JSONDecoder.radioAPI.decode(CommandResponse.self, from: data),
               let message = commandResponse.error ?? commandResponse.message {
                throw RadioAPIError.server(message)
            }
            if let text = String(data: data, encoding: .utf8), !text.isEmpty {
                throw RadioAPIError.server(text)
            }
            throw RadioAPIError.server("HTTP \(http.statusCode)")
        }
    }
}
