import Foundation

enum RadioAPIError: LocalizedError {
    case invalidConfiguration
    case invalidResponse
    case authenticationFailed
    case authenticationRequired
    case server(String)

    var errorDescription: String? {
        switch self {
        case .invalidConfiguration:
            return "Invalid server configuration."
        case .invalidResponse:
            return "Invalid server response."
        case .authenticationFailed:
            return "Authentication failed. Check username and password."
        case .authenticationRequired:
            return "Authentication session is no longer valid. Reconnect to log in again."
        case .server(let message):
            return message
        }
    }
}

final class RadioAPIClient {
    private static let sessionCookieName = "FTM150SESSION"
    private static let loginPath = "/ftm150-login"
    private static let loginPagePath = "/login.html"

    private let session: URLSession
    private let cookieStorage: HTTPCookieStorage

    init(
        session: URLSession = .shared,
        cookieStorage: HTTPCookieStorage = .shared
    ) {
        self.session = session
        self.cookieStorage = cookieStorage
    }

    /// Authenticates against Apache mod_auth_form and stores FTM150SESSION
    /// in the shared HTTP cookie storage used by the application.
    func authenticate(config: ConnectionConfig) async throws {
        clearAuthentication(config: config)

        let url = config.endpoint(path: Self.loginPath)
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 30
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        request.setValue("text/html,application/xhtml+xml", forHTTPHeaderField: "Accept")

        var form = URLComponents()
        form.queryItems = [
            URLQueryItem(name: "httpd_username", value: config.username),
            URLQueryItem(name: "httpd_password", value: config.password)
        ]

        guard let body = form.percentEncodedQuery?.data(using: .utf8) else {
            throw RadioAPIError.invalidConfiguration
        }
        request.httpBody = body

        let (_, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw RadioAPIError.invalidResponse
        }

        guard (200 ..< 400).contains(http.statusCode) else {
            throw RadioAPIError.authenticationFailed
        }

        // URLSession follows Apache redirects. A failed form login normally
        // returns the public login page, while a successful login creates the
        // FTM150SESSION cookie and redirects to the application root.
        if http.url?.path == Self.loginPagePath {
            clearAuthentication(config: config)
            throw RadioAPIError.authenticationFailed
        }

        guard sessionCookie(for: config) != nil else {
            throw RadioAPIError.authenticationFailed
        }
    }

    func clearAuthentication(config: ConnectionConfig) {
        let host = config.baseURL.host?.lowercased()

        for cookie in cookieStorage.cookies ?? [] {
            guard cookie.name == Self.sessionCookieName else { continue }

            if let host {
                let cookieDomain = cookie.domain
                    .trimmingCharacters(in: CharacterSet(charactersIn: "."))
                    .lowercased()
                guard host == cookieDomain || host.hasSuffix(".\(cookieDomain)") else {
                    continue
                }
            }

            cookieStorage.deleteCookie(cookie)
        }
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
        try applySessionCookie(to: &request, config: config, path: "/api/state.ws")
        return session.webSocketTask(with: request)
    }

    func makeTXAudioWebSocket(config: ConnectionConfig) throws -> URLSessionWebSocketTask {
        let url = config.webSocketEndpoint(path: "/audio-tx.ws")
        var request = URLRequest(url: url)
        request.timeoutInterval = 30
        try applySessionCookie(to: &request, config: config, path: "/audio-tx.ws")
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
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        try applySessionCookie(to: &request, config: config, path: path)
        return request
    }

    private func makeRequest<Body: Encodable>(config: ConnectionConfig, path: String, method: String, body: Body) throws -> URLRequest {
        var request = try makeRequest(config: config, path: path, method: method)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(body)
        return request
    }

    private func sessionCookie(for config: ConnectionConfig) -> HTTPCookie? {
        let url = config.endpoint(path: "/")
        if let cookie = cookieStorage.cookies(for: url)?.first(where: { $0.name == Self.sessionCookieName }) {
            return cookie
        }

        // Fallback for stores that do not return a Secure cookie through
        // cookies(for:) in every URLSession configuration.
        guard let host = config.baseURL.host?.lowercased() else { return nil }
        return cookieStorage.cookies?.first { cookie in
            guard cookie.name == Self.sessionCookieName else { return false }
            let cookieDomain = cookie.domain
                .trimmingCharacters(in: CharacterSet(charactersIn: "."))
                .lowercased()
            return host == cookieDomain || host.hasSuffix(".\(cookieDomain)")
        }
    }

    private func applySessionCookie(
        to request: inout URLRequest,
        config: ConnectionConfig,
        path: String
    ) throws {
        guard let cookie = sessionCookie(for: config) else {
            throw RadioAPIError.authenticationRequired
        }

        // Use the HTTPS URL to select the cookie, then attach it explicitly.
        // This is especially important for URLSessionWebSocketTask, whose
        // request URL uses wss:// rather than https://.
        let cookieURL = config.endpoint(path: path)
        let matchingCookies = cookieStorage.cookies(for: cookieURL) ?? [cookie]
        let sessionCookies = matchingCookies.filter { $0.name == Self.sessionCookieName }
        let cookiesToSend = sessionCookies.isEmpty ? [cookie] : sessionCookies
        let headers = HTTPCookie.requestHeaderFields(with: cookiesToSend)

        guard let value = headers["Cookie"], !value.isEmpty else {
            throw RadioAPIError.authenticationRequired
        }
        request.setValue(value, forHTTPHeaderField: "Cookie")
    }

    private func validate(response: URLResponse, data: Data) throws {
        guard let http = response as? HTTPURLResponse else {
            throw RadioAPIError.invalidResponse
        }

        // URLSession follows the redirect produced by mod_auth_form. If the
        // session expired, a protected API call can therefore end as a 200
        // response from /login.html instead of returning JSON.
        if http.url?.path == Self.loginPagePath {
            throw RadioAPIError.authenticationRequired
        }

        if let mimeType = http.mimeType?.lowercased(),
           mimeType.contains("text/html"),
           let text = String(data: data, encoding: .utf8),
           text.range(of: "httpd_username", options: .caseInsensitive) != nil,
           text.range(of: "httpd_password", options: .caseInsensitive) != nil {
            throw RadioAPIError.authenticationRequired
        }

        guard (200 ..< 300).contains(http.statusCode) else {
            if http.statusCode == 401 || http.statusCode == 403 {
                throw RadioAPIError.authenticationRequired
            }
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
