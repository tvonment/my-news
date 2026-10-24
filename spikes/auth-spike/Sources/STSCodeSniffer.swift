import Foundation
import Synchronization

/// Keeps the AADSTS codes that appear in MSAL's own log, nothing else.
///
/// MSAL does not always put the server's code into the NSError it returns (see
/// AzureAD/microsoft-authentication-library-for-objc#3082), and the code is what
/// tells an expired refresh token (AADSTS700082) from a revoked one (AADSTS50173)
/// or a Conditional Access limit (AADSTS70043). The rest of each log line is
/// discarded on the spot; MSAL masks tokens in its logs at every level anyway.
nonisolated final class STSCodeSniffer: Sendable {
    static let shared = STSCodeSniffer()

    private let codes = Mutex<[String]>([])

    func observe(_ message: String) {
        guard message.contains("AADSTS") else { return }
        let found = message.matches(of: /AADSTS[0-9]+/).map { String($0.output) }
        codes.withLock { list in
            for code in found where !list.contains(code) {
                list.append(code)
            }
            if list.count > 8 { list.removeFirst(list.count - 8) }
        }
    }

    /// The codes seen since the last call, oldest first.
    func drain() -> [String] {
        codes.withLock { list in
            defer { list.removeAll() }
            return list
        }
    }
}
