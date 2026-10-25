import Foundation
import Observation
import UIKit
@preconcurrency import MSAL

/// One signed-in account as the screen shows it.
struct AccountState: Identifiable {
    let id: String
    var username: String
    var record: AccountRecord?
    var claims: TokenClaims?
    /// The idp claim of the ID token, in case the access token has none.
    var idTokenIdp: String?

    var title: String {
        let method = record?.method.map { "\($0.label) · " } ?? ""
        return method + username
    }
}

/// Everything the spike does with MSAL. MainActor-isolated (the target's default);
/// every MSAL completion is delivered on the main queue to match.
@Observable
final class SpikeModel {
    private(set) var accounts: [AccountState] = []
    private(set) var log: [LogEntry] = []
    private(set) var busy = false
    private(set) var setupError: String?

    let msalVersion = MSALPublicClientApplication.sdkVersion ?? "unknown"
    let appVersion: String = {
        let info = Bundle.main.infoDictionary
        let version = info?["CFBundleShortVersionString"] as? String ?? "?"
        let build = info?["CFBundleVersion"] as? String ?? "?"
        return "\(version) (\(build))"
    }()

    @ObservationIgnored private var application: MSALPublicClientApplication?
    @ObservationIgnored private var records: [String: AccountRecord]
    @ObservationIgnored private var claims: [String: TokenClaims] = [:]
    @ObservationIgnored private var didLaunchRefresh = false
    @ObservationIgnored private let store: SpikeStore

    init() {
        let store = SpikeStore()
        self.store = store
        records = store.loadAccounts()
        log = store.loadLog()
        // MSAL's log goes nowhere but the sniffer, which keeps AADSTS codes only. The
        // closure is @Sendable because MSAL calls it on its own logging queue.
        MSALGlobalConfig.loggerConfig.logLevel = .verbose
        MSALGlobalConfig.loggerConfig.logMaskingLevel = .settingsMaskEUIIOnly
        MSALGlobalConfig.loggerConfig.setLogCallback { @Sendable _, message, _ in
            if let message { STSCodeSniffer.shared.observe(message) }
        }
        do {
            // CIAM authorities never use the broker; this makes the
            // ASWebAuthenticationSession path explicit rather than inferred.
            MSALGlobalConfig.brokerAvailability = MSALBrokeredAvailability.none
            guard let url = URL(string: SpikeConfig.authority) else { throw SpikeError("bad authority URL") }
            let authority = try MSALCIAMAuthority(url: url)
            let config = MSALPublicClientApplicationConfig(
                clientId: SpikeConfig.clientID,
                redirectUri: SpikeConfig.redirectURI,
                authority: authority
            )
            // The app's own keychain group instead of com.microsoft.adalcache, so no
            // keychain-sharing entitlement is needed.
            config.cacheConfig.keychainSharingGroup = Bundle.main.bundleIdentifier ?? "com.vonmentlen.mynews"
            application = try MSALPublicClientApplication(configuration: config)
        } catch {
            setupError = ErrorSummary(error).text
        }
        reloadAccounts()
    }

    var logText: String {
        let header = "MyNews auth spike · app \(appVersion) · MSAL \(msalVersion) · iOS \(UIDevice.current.systemVersion)"
        return ([header] + log.map(\.text)).joined(separator: "\n")
    }

    // MARK: - Interactive

    func signIn(_ method: SignInMethod, forceLogin: Bool, ephemeral: Bool) async {
        guard let application, !busy else { return }
        var options = ["prompt=\(forceLogin ? "login" : "if-necessary")", "ephemeral=\(ephemeral ? "on" : "off")"]
        if method == .apple { options.append("domain_hint=\(SpikeConfig.appleDomainHint)") }
        let summary = options.joined(separator: " ")

        guard let presenter = UIApplication.shared.topViewController else {
            append(.interactive, .button, account: "\(method.label) button", ok: false,
                   result: "\(summary) → no view controller to present from", hours: nil)
            return
        }
        let webview = MSALWebviewParameters(authPresentationViewController: presenter)
        webview.webviewType = .authenticationSession
        webview.prefersEphemeralWebBrowserSession = ephemeral

        let parameters = MSALInteractiveTokenParameters(scopes: SpikeConfig.scopes, webviewParameters: webview)
        // Never .create: this tenant rejects prompt=create (AADSTS50021). Sign-up
        // starts from the hosted page's "Create one" link instead.
        parameters.promptType = forceLogin ? .login : .promptIfNecessary
        if method == .apple { parameters.domainHint = SpikeConfig.appleDomainHint }
        parameters.completionBlockQueue = DispatchQueue.main

        busy = true
        _ = STSCodeSniffer.shared.drain()
        // MSAL's completion blocks are imported as @Sendable, so they only hand back
        // Sendable facts; the state changes happen here, on the main actor.
        var outcome = await withCheckedContinuation { continuation in
            application.acquireToken(with: parameters) { result, error in
                continuation.resume(returning: TokenOutcome(result: result, error: error))
            }
        }
        await outcome.addLoggedSTSCodes()
        busy = false
        finishInteractive(method, summary: summary, outcome: outcome)
    }

    private func finishInteractive(_ method: SignInMethod, summary: String, outcome: TokenOutcome) {
        guard outcome.error == nil, let id = outcome.accountID else {
            append(.interactive, .button, account: "\(method.label) button", ok: false,
                   result: "\(summary) → \(outcome.error?.text ?? "no account in the result")", hours: nil)
            return
        }
        let now = Date()
        let previous = records[id]
        var record = previous ?? AccountRecord()
        record.method = method
        record.username = outcome.username
        record.lastInteractive = now
        record.lastFromServer = now
        record.lastIssuedAt = outcome.claims?.iat
        records[id] = record
        store.saveAccounts(records)
        claims[id] = outcome.claims

        let idp = outcome.claims?.idp ?? outcome.idTokenIdp
        append(.interactive, .button, account: label(id), ok: true,
               result: "\(summary) → \(SpikeFormat.expiry(outcome.expiry)) · idp \(idp ?? "none (local account)")",
               hours: previous?.lastInteractive.map { SpikeFormat.hours(from: $0, to: now) })
        reloadAccounts()
    }

    // MARK: - Silent

    /// Launch and foreground: a non-forced silent call for every cached account, so
    /// opening the app after 25 h or 7 d is the whole test step.
    func autoRefresh(_ trigger: AttemptTrigger) async {
        if trigger == .launch {
            guard !didLaunchRefresh else { return }
            didLaunchRefresh = true
        }
        await refreshAll(force: false, trigger: trigger)
    }

    func refreshAll(force: Bool, trigger: AttemptTrigger) async {
        // busy also covers an open browser sheet: coming back from Mail with a
        // sign-up code must not start a refresh underneath it.
        guard let application, !busy else { return }
        let kind: AttemptKind = force ? .forced : .silent

        let cached: [MSALAccount]
        do {
            cached = try application.allAccounts()
        } catch {
            append(kind, trigger, account: "MSAL cache", ok: false, result: ErrorSummary(error).text, hours: nil)
            return
        }

        // An account the spike signed in but MSAL no longer has is a finding of its own
        // (cache loss), different from a refresh token Entra refused. Logged once.
        let cachedIDs = Set(cached.compactMap(\.identifier))
        for (id, record) in records where !cachedIDs.contains(id) {
            append(kind, trigger, account: label(id), ok: false,
                   result: "account is no longer in the MSAL cache (keychain); dropped from the spike's records",
                   hours: record.lastInteractive.map { SpikeFormat.hours(from: $0) })
            records[id] = nil
            store.saveAccounts(records)
        }
        if cached.isEmpty {
            if trigger == .button && records.isEmpty {
                append(kind, trigger, account: "none", ok: false, result: "no signed-in account", hours: nil)
            }
            return
        }

        busy = true
        for account in cached {
            let outcome = await acquireSilently(application, account: account, force: force)
            recordSilent(outcome, accountID: account.identifier ?? "unknown", kind: kind, trigger: trigger)
        }
        busy = false
        reloadAccounts()
    }

    private func acquireSilently(_ application: MSALPublicClientApplication, account: MSALAccount, force: Bool) async -> TokenOutcome {
        _ = STSCodeSniffer.shared.drain()
        var outcome = await withCheckedContinuation { continuation in
            let parameters = MSALSilentTokenParameters(scopes: SpikeConfig.scopes, account: account)
            // false: MSAL returns the cached access token while it is valid and redeems
            // the refresh token once it is not. true: always redeem the refresh token.
            parameters.forceRefresh = force
            parameters.completionBlockQueue = DispatchQueue.main
            application.acquireTokenSilent(with: parameters) { result, error in
                continuation.resume(returning: TokenOutcome(result: result, error: error))
            }
        }
        await outcome.addLoggedSTSCodes()
        return outcome
    }

    private func recordSilent(_ outcome: TokenOutcome, accountID id: String, kind: AttemptKind, trigger: AttemptTrigger) {
        let now = Date()
        var record = records[id] ?? AccountRecord()
        let sinceInteractive = record.lastInteractive.map { SpikeFormat.hours(from: $0, to: now) }
        let refreshTokenAge = record.lastFromServer.map {
            "refresh token \(SpikeFormat.duration(hours: SpikeFormat.hours(from: $0, to: now))) old"
        }

        if let error = outcome.error {
            append(kind, trigger, account: label(id), ok: false,
                   result: [error.text, refreshTokenAge].compactMap { $0 }.joined(separator: " · "),
                   hours: sinceInteractive)
            return
        }

        // force always goes to Entra. Otherwise a newer iat than the last token seen
        // means MSAL redeemed the refresh token rather than returning its cached one.
        let fromServer: Bool
        if kind == .forced {
            fromServer = true
        } else if let iat = outcome.claims?.iat, let last = record.lastIssuedAt {
            fromServer = iat > last
        } else if let iat = outcome.claims?.iat {
            fromServer = abs(iat.timeIntervalSince(now)) < 15 * 60
        } else {
            fromServer = false
        }
        let source = fromServer
            ? "new token from Entra" + (refreshTokenAge.map { " (\($0))" } ?? "")
            : "cached access token, no network"

        if fromServer { record.lastFromServer = now }
        record.lastIssuedAt = outcome.claims?.iat ?? record.lastIssuedAt
        record.username = outcome.username ?? record.username
        records[id] = record
        store.saveAccounts(records)
        claims[id] = outcome.claims

        append(kind, trigger, account: label(id), ok: true,
               result: "\(SpikeFormat.expiry(outcome.expiry)) · \(source)", hours: sinceInteractive)
    }

    // MARK: - Sign-out

    /// Removes the account's tokens from this app's cache. The browser session cookie
    /// stays, which is why sign-in defaults to prompt=login.
    func signOut(_ id: String) {
        guard let application else { return }
        let name = label(id)
        let hours = records[id]?.lastInteractive.map { SpikeFormat.hours(from: $0) }
        do {
            let account = try application.account(forIdentifier: id)
            try application.remove(account)
            records[id] = nil
            claims[id] = nil
            store.saveAccounts(records)
            append(.signout, .button, account: name, ok: true, result: "tokens removed from the MSAL cache", hours: hours)
        } catch {
            append(.signout, .button, account: name, ok: false, result: ErrorSummary(error).text, hours: hours)
        }
        reloadAccounts()
    }

    // MARK: - Helpers

    private func reloadAccounts() {
        let cached = (try? application?.allAccounts()) ?? []
        accounts = cached.compactMap { account -> AccountState? in
            guard let id = account.identifier else { return nil }
            return AccountState(
                id: id,
                username: account.username ?? records[id]?.username ?? "(no username)",
                record: records[id],
                claims: claims[id],
                idTokenIdp: account.accountClaims?["idp"] as? String
            )
        }
        .sorted { ($0.record?.lastInteractive ?? .distantPast) < ($1.record?.lastInteractive ?? .distantPast) }
    }

    private func label(_ id: String) -> String {
        let record = records[id]
        let name = record?.username ?? String(id.prefix(8))
        return record?.method.map { "\($0.label) · \(name)" } ?? name
    }

    private func append(_ kind: AttemptKind, _ trigger: AttemptTrigger, account: String, ok: Bool, result: String, hours: Double?) {
        let entry = LogEntry(at: Date(), kind: kind, trigger: trigger, account: account, ok: ok,
                             result: result, hoursSinceInteractive: hours)
        log.insert(entry, at: 0)
        if log.count > SpikeStore.maxEntries { log.removeLast(log.count - SpikeStore.maxEntries) }
        store.saveLog(log)
    }
}

/// The Sendable facts the spike keeps from an MSAL result or error. The access token
/// is decoded here and then dropped.
nonisolated struct TokenOutcome: Sendable {
    var accountID: String?
    var username: String?
    var claims: TokenClaims?
    var expiry: Date?
    var idTokenIdp: String?
    var error: ErrorSummary?

    init(result: MSALResult?, error: Error?) {
        guard let result else {
            self.error = ErrorSummary(error)
            return
        }
        accountID = result.account.identifier
        username = result.account.username
        claims = TokenClaims(jwt: result.accessToken)
        expiry = claims?.exp ?? result.expiresOn
        idTokenIdp = result.account.accountClaims?["idp"] as? String
    }

    /// Adds the AADSTS codes MSAL logged for a failed attempt but left out of the
    /// error. MSAL delivers its log asynchronously, hence the short wait.
    mutating func addLoggedSTSCodes() async {
        guard let error else { return }
        try? await Task.sleep(for: .milliseconds(500))
        let codes = STSCodeSniffer.shared.drain().filter { !error.text.contains($0) }
        if !codes.isEmpty {
            self.error = ErrorSummary(text: error.text + " · MSAL log: " + codes.joined(separator: ","))
        }
    }
}

/// An MSAL (or other) error flattened to one line: domain, code, the MSAL userInfo
/// fields that explain it, and the server's description. Never the userInfo as a
/// whole: MSALInvalidResultKey can carry a result with tokens in it.
nonisolated struct ErrorSummary: Sendable, Equatable {
    let text: String

    init(text: String) {
        self.text = text
    }

    init(_ error: Error?) {
        guard let error else {
            text = "no result and no error"
            return
        }
        let ns = error as NSError
        let info = ns.userInfo
        let name = ns.domain == "MSALErrorDomain" ? Self.names[ns.code] : nil
        var parts = ["\(ns.domain) \(ns.code)" + (name.map { " \($0)" } ?? "")]
        // The keys spelled out: MSAL declares them as mutable C globals, which
        // Swift 6 rejects. Their values are their names (MSAL/src/MSALError.m).
        if let internalCode = (info["MSALInternalErrorCodeKey"] as? NSNumber)?.intValue {
            parts.append("internal \(internalCode)" + (Self.internalNames[internalCode].map { " \($0)" } ?? ""))
        }
        if let oauth = info["MSALOAuthErrorKey"] as? String { parts.append("oauth \(oauth)") }
        if let sub = info["MSALOAuthSubErrorKey"] as? String, !sub.isEmpty { parts.append("suberror \(sub)") }
        if let sts = info["MSALSTSErrorCodesKey"] as? [Any], !sts.isEmpty {
            parts.append("STS " + sts.map { "\($0)" }.joined(separator: ","))
        }
        if let status = info["MSALHTTPResponseCodeKey"] { parts.append("HTTP \(status)") }
        if let underlying = info[NSUnderlyingErrorKey] as? NSError {
            parts.append("underlying \(underlying.domain) \(underlying.code)")
        }
        parts.append(info["MSALErrorDescriptionKey"] as? String ?? ns.localizedDescription)
        if let correlation = info["MSALCorrelationIDKey"] { parts.append("correlation \(correlation)") }
        text = parts.joined(separator: " · ")
    }

    private static let names: [Int: String] = [
        -50000: "internal",
        -50002: "interactionRequired",
        -50003: "serverDeclinedScopes",
        -50004: "serverProtectionPoliciesRequired",
        -50005: "userCanceled",
        -50006: "serverError",
    ]

    private static let internalNames: [Int: String] = [
        -42001: "redirectSchemeNotRegistered",
        -42002: "invalidRequest",
        -42003: "invalidClient",
        -42004: "invalidGrant",
        -42005: "invalidScope",
        -42006: "unauthorizedClient",
        -42008: "unexpected",
        -42011: "invalidRedirectURI",
        -42401: "sessionCanceled",
        -42402: "interactiveSessionAlreadyRunning",
        -42403: "noViewController",
    ]
}

nonisolated struct SpikeError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

extension UIApplication {
    /// What MSAL presents ASWebAuthenticationSession from: the top of the key window.
    var topViewController: UIViewController? {
        let windows = connectedScenes.compactMap { $0 as? UIWindowScene }.flatMap(\.windows)
        var top = (windows.first(where: \.isKeyWindow) ?? windows.first)?.rootViewController
        while let presented = top?.presentedViewController {
            top = presented
        }
        return top
    }
}
