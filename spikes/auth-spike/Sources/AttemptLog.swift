import Foundation

nonisolated enum SignInMethod: String, Codable, Sendable {
    case apple, email

    var label: String {
        switch self {
        case .apple: "Apple"
        case .email: "email"
        }
    }
}

nonisolated enum AttemptKind: String, Codable, Sendable {
    case interactive, silent, forced, signout
}

nonisolated enum AttemptTrigger: String, Codable, Sendable {
    case launch, foreground, button
}

/// One line of the persistent log. Holds only claims-derived facts and MSAL error
/// fields, never a token.
nonisolated struct LogEntry: Codable, Sendable, Identifiable {
    var id = UUID()
    var at: Date
    var kind: AttemptKind
    var trigger: AttemptTrigger
    var account: String
    var ok: Bool
    var result: String
    /// Silent and forced: hours since this account's last interactive sign-in.
    /// Interactive: hours since its previous one (nil on the first).
    var hoursSinceInteractive: Double?

    var text: String {
        let since = hoursSinceInteractive.map { SpikeFormat.duration(hours: $0) } ?? "n/a"
        return "\(SpikeFormat.timestamp(at)) · \(kind.rawValue) (\(trigger.rawValue)) · \(account) · "
            + "\(ok ? "ok" : "FAILED") · \(result) · since interactive: \(since)"
    }
}

/// What the spike remembers per account between launches (keyed by MSALAccount.identifier).
nonisolated struct AccountRecord: Codable, Sendable {
    /// The button that started the interactive sign-in. The idp claim is the proof.
    var method: SignInMethod?
    var username: String?
    var lastInteractive: Date?
    /// When Entra last issued tokens for this account (interactive or a refresh),
    /// which is how old the refresh token in the cache is.
    var lastFromServer: Date?
    /// iat of the newest access token seen; a later iat means MSAL went to Entra.
    var lastIssuedAt: Date?
}

/// UserDefaults persistence: the log (newest first, capped) and the account records.
/// Neither holds a token; the tokens stay in MSAL's keychain cache.
nonisolated struct SpikeStore {
    static let maxEntries = 200
    private static let logKey = "authSpike.log.v1"
    private static let accountsKey = "authSpike.accounts.v1"

    func loadLog() -> [LogEntry] {
        guard let data = UserDefaults.standard.data(forKey: Self.logKey) else { return [] }
        return (try? JSONDecoder().decode([LogEntry].self, from: data)) ?? []
    }

    func saveLog(_ entries: [LogEntry]) {
        let capped = Array(entries.prefix(Self.maxEntries))
        if let data = try? JSONEncoder().encode(capped) {
            UserDefaults.standard.set(data, forKey: Self.logKey)
        }
    }

    func loadAccounts() -> [String: AccountRecord] {
        guard let data = UserDefaults.standard.data(forKey: Self.accountsKey) else { return [:] }
        return (try? JSONDecoder().decode([String: AccountRecord].self, from: data)) ?? [:]
    }

    func saveAccounts(_ records: [String: AccountRecord]) {
        if let data = try? JSONEncoder().encode(records) {
            UserDefaults.standard.set(data, forKey: Self.accountsKey)
        }
    }
}

nonisolated enum SpikeFormat {
    /// Local time with its UTC offset, e.g. 2026-10-22 14:03:12+02:00.
    static func timestamp(_ date: Date) -> String {
        let style = Date.ISO8601FormatStyle(
            dateSeparator: .dash,
            dateTimeSeparator: .space,
            timeSeparator: .colon,
            timeZoneSeparator: .colon,
            includingFractionalSeconds: false,
            timeZone: .current
        )
        return date.formatted(style)
    }

    static func duration(hours: Double) -> String {
        if hours >= 48 {
            return String(format: "%.1f h (%.1f d)", hours, hours / 24)
        }
        return String(format: "%.1f h", hours)
    }

    static func hours(from start: Date, to end: Date = Date()) -> Double {
        end.timeIntervalSince(start) / 3600
    }

    /// "exp 15:08:21 (in 64 min)" or "exp 13:01:10 (expired 5 min ago)".
    static func expiry(_ exp: Date?, now: Date = Date()) -> String {
        guard let exp else { return "exp unknown" }
        let parts = Calendar.current.dateComponents([.hour, .minute, .second], from: exp)
        let clock = String(format: "%02d:%02d:%02d", parts.hour ?? 0, parts.minute ?? 0, parts.second ?? 0)
        let minutes = Int((exp.timeIntervalSince(now) / 60).rounded())
        let relative = minutes >= 0 ? "in \(minutes) min" : "expired \(-minutes) min ago"
        return "exp \(clock) (\(relative))"
    }
}
