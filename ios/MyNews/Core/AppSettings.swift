import Foundation
import Observation
import Security

/// Backend connection settings.
///
/// The base URL is ordinary preference data and lives in `UserDefaults`. The
/// function key is a bearer secret and lives in the Keychain with
/// `ThisDeviceOnly` accessibility, so it is never included in an iCloud or
/// iTunes backup. Microsoft is explicit that a function key is not
/// authentication — it is a stopgap until Sign in with Apple lands, and it is
/// the reason this app is not distributed to anyone else.
@Observable
final class AppSettings {
    private enum Keys {
        static let baseURL = "baseURL"
        static let minRelevance = "minRelevance"
    }

    var baseURL: String {
        didSet { UserDefaults.standard.set(baseURL, forKey: Keys.baseURL) }
    }

    /// Hide anything the scorer rated below this. 0 shows everything it judged.
    var minRelevance: Int {
        didSet { UserDefaults.standard.set(minRelevance, forKey: Keys.minRelevance) }
    }

    var functionKey: String {
        didSet { Keychain.set(functionKey, for: "functionKey") }
    }

    var isConfigured: Bool {
        !baseURL.trimmingCharacters(in: .whitespaces).isEmpty
            && !functionKey.trimmingCharacters(in: .whitespaces).isEmpty
    }

    init() {
        baseURL = UserDefaults.standard.string(forKey: Keys.baseURL) ?? ""
        minRelevance = UserDefaults.standard.object(forKey: Keys.minRelevance) as? Int ?? 0
        functionKey = Keychain.get("functionKey") ?? ""

        #if DEBUG
        // Seed from the launch environment so a fresh simulator install can be
        // pointed at the backend without typing a function key on a simulated
        // keyboard. Debug builds only — never a path into a release binary.
        let env = ProcessInfo.processInfo.environment
        if let url = env["MYNEWS_BASE_URL"], !url.isEmpty { baseURL = url }
        if let key = env["MYNEWS_FUNCTION_KEY"], !key.isEmpty { functionKey = key }
        #endif
    }
}

enum Keychain {
    private static let service = "com.vonmentlen.mynews"

    static func set(_ value: String, for account: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)

        guard !value.isEmpty, let data = value.data(using: .utf8) else { return }

        var attributes = query
        attributes[kSecValueData as String] = data
        // Readable after first unlock so a background refresh can still reach the
        // API, but never synced off this device.
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(attributes as CFDictionary, nil)
    }

    static func get(_ account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var result: AnyObject?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data
        else { return nil }
        return String(data: data, encoding: .utf8)
    }
}
