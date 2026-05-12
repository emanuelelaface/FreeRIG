import Combine
import Foundation
import Security

struct ConnectionConfig: Equatable {
    let baseURL: URL
    let username: String
    let password: String

    var basicAuthorizationHeader: String {
        let token = Data("\(username):\(password)".utf8).base64EncodedString()
        return "Basic \(token)"
    }

    func endpoint(path: String) -> URL {
        if path.hasPrefix("/") {
            return baseURL.appending(path: String(path.dropFirst()))
        }
        return baseURL.appending(path: path)
    }

    func webSocketEndpoint(path: String) -> URL {
        var components = URLComponents(url: endpoint(path: path), resolvingAgainstBaseURL: false)
        if components?.scheme == "https" {
            components?.scheme = "wss"
        } else if components?.scheme == "http" {
            components?.scheme = "ws"
        }
        return components?.url ?? endpoint(path: path)
    }
}

@MainActor
final class AppSettings: ObservableObject {
    private enum Keys {
        static let serverURL = "freerig.ios.serverURL"
        static let username = "freerig.ios.username"
        static let autoConnect = "freerig.ios.autoConnect"
    }

    @Published var serverURLString: String {
        didSet { UserDefaults.standard.set(serverURLString, forKey: Keys.serverURL) }
    }

    @Published var username: String {
        didSet { UserDefaults.standard.set(username, forKey: Keys.username) }
    }

    @Published var password: String {
        didSet { KeychainStore.shared.set(password, for: Self.passwordKey) }
    }

    @Published var autoConnect: Bool {
        didSet { UserDefaults.standard.set(autoConnect, forKey: Keys.autoConnect) }
    }

    static let passwordKey = "FreeRig.iOS.password"

    init() {
        self.serverURLString = UserDefaults.standard.string(forKey: Keys.serverURL) ?? "https://ftm150.scumm.it"
        self.username = UserDefaults.standard.string(forKey: Keys.username) ?? "ema"
        self.password = KeychainStore.shared.get(Self.passwordKey) ?? ""
        self.autoConnect = UserDefaults.standard.object(forKey: Keys.autoConnect) as? Bool ?? true
    }

    var trimmedServerURLString: String {
        serverURLString.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func snapshot() -> ConnectionConfig? {
        let rawURL = trimmedServerURLString
        let rawUser = username.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !rawURL.isEmpty, !rawUser.isEmpty, !password.isEmpty else {
            return nil
        }

        let normalized = rawURL.hasSuffix("/") ? String(rawURL.dropLast()) : rawURL
        guard let url = URL(string: normalized), let scheme = url.scheme, ["http", "https"].contains(scheme.lowercased()) else {
            return nil
        }

        return ConnectionConfig(baseURL: url, username: rawUser, password: password)
    }
}

private final class KeychainStore {
    static let shared = KeychainStore()

    func get(_ key: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: key,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data else {
            return nil
        }
        return String(data: data, encoding: .utf8)
    }

    func set(_ value: String, for key: String) {
        let data = Data(value.utf8)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrAccount as String: key
        ]

        let attributes: [String: Any] = [
            kSecValueData as String: data
        ]

        let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            var add = query
            add[kSecValueData as String] = data
            SecItemAdd(add as CFDictionary, nil)
        }
    }
}
