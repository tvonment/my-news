import SwiftUI

struct ContentView: View {
    let model: SpikeModel

    @Environment(\.scenePhase) private var scenePhase
    @AppStorage("authSpike.forceLogin") private var forceLogin = true
    @AppStorage("authSpike.ephemeral") private var ephemeral = false
    @State private var wasInBackground = false
    @State private var confirmSignOut = false

    var body: some View {
        NavigationStack {
            List {
                if let setupError = model.setupError {
                    Section("MSAL setup failed") {
                        Text(setupError).font(.caption.monospaced()).foregroundStyle(.red)
                    }
                }
                interactiveSection
                silentSection
                accountSections
                logSection
                aboutSection
            }
            .navigationTitle("Auth spike")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    if model.busy { ProgressView() }
                }
            }
            .confirmationDialog("Sign out which account?", isPresented: $confirmSignOut, titleVisibility: .visible) {
                ForEach(model.accounts) { account in
                    Button("Sign out \(account.title)", role: .destructive) { model.signOut(account.id) }
                }
            } message: {
                Text("Removes the account's tokens from this app. Its refresh-token soak ends here.")
            }
        }
        .task { await model.autoRefresh(.launch) }
        .onChange(of: scenePhase) { _, phase in
            // background → inactive → active; inactive alone (a system alert, Face ID,
            // the browser sheet) is not a return to the app.
            switch phase {
            case .background:
                wasInBackground = true
            case .active where wasInBackground:
                wasInBackground = false
                Task { await model.autoRefresh(.foreground) }
            default:
                break
            }
        }
    }

    // MARK: - Sections

    private var interactiveSection: some View {
        Section {
            Button("Continue with Apple", systemImage: "apple.logo") {
                Task { await model.signIn(.apple, forceLogin: forceLogin, ephemeral: ephemeral) }
            }
            Button("Continue with email", systemImage: "envelope") {
                Task { await model.signIn(.email, forceLogin: forceLogin, ephemeral: ephemeral) }
            }
            Toggle("Ask for credentials (prompt=login)", isOn: $forceLogin)
            Toggle("Private browser session", isOn: $ephemeral)
        } header: {
            Text("Interactive")
        } footer: {
            Text("""
            Apple sends domain_hint=apple, which should open Apple's page directly; email opens \
            the hosted page (sign up there with "Create one"). prompt=login keeps the browser's \
            session cookie from signing you straight back into the previous account. A private \
            session shares no cookies with Safari.
            """)
        }
        .disabled(model.busy)
    }

    private var silentSection: some View {
        Section {
            Button("Refresh silently (no force)", systemImage: "arrow.clockwise") {
                Task { await model.refreshAll(force: false, trigger: .button) }
            }
            Button("Refresh silently (force)", systemImage: "arrow.trianglehead.2.clockwise") {
                Task { await model.refreshAll(force: true, trigger: .button) }
            }
            Button("Sign out", systemImage: "rectangle.portrait.and.arrow.right", role: .destructive) {
                confirmSignOut = true
            }
            .disabled(model.accounts.isEmpty)
        } header: {
            Text("Silent")
        } footer: {
            Text("""
            Every signed-in account is refreshed, one log line each. No force returns the cached \
            access token while it is valid and redeems the refresh token after it expires; force \
            always redeems it. Launch and every return to the foreground run "no force" by themselves.
            """)
        }
        .disabled(model.busy)
    }

    @ViewBuilder
    private var accountSections: some View {
        if model.accounts.isEmpty {
            Section("Accounts") {
                Text("No signed-in account.").foregroundStyle(.secondary)
            }
        }
        ForEach(model.accounts) { account in
            Section {
                AccountDetails(account: account)
            } header: {
                Text(account.title)
            }
        }
    }

    private var logSection: some View {
        Section {
            if model.log.isEmpty {
                Text("No attempts yet.").foregroundStyle(.secondary)
            }
            ForEach(model.log) { entry in
                LogRow(entry: entry)
            }
        } header: {
            HStack {
                Text("Log · newest first · \(model.log.count)/\(SpikeStore.maxEntries)")
                Spacer()
                ShareLink(item: model.logText) {
                    Label("Share", systemImage: "square.and.arrow.up")
                }
                .disabled(model.log.isEmpty)
            }
        }
    }

    private var aboutSection: some View {
        Section("About") {
            Field(name: "app", value: model.appVersion)
            Field(name: "MSAL", value: model.msalVersion)
            Field(name: "iOS", value: UIDevice.current.systemVersion)
            Field(name: "authority", value: SpikeConfig.authority)
            Field(name: "client", value: SpikeConfig.clientID)
            Field(name: "scope", value: SpikeConfig.apiScope)
        }
    }
}

// MARK: - Rows

private struct AccountDetails: View {
    let account: AccountState

    var body: some View {
        Field(name: "username", value: account.username)
        if let last = account.record?.lastInteractive {
            Field(name: "signed in", value: "\(SpikeFormat.timestamp(last)) · \(SpikeFormat.duration(hours: SpikeFormat.hours(from: last))) ago")
        }
        if let last = account.record?.lastFromServer {
            Field(name: "RT issued", value: "\(SpikeFormat.timestamp(last)) · \(SpikeFormat.duration(hours: SpikeFormat.hours(from: last))) ago")
        }
        if let claims = account.claims {
            Field(name: "iss", value: claims.iss, ok: claims.issuerOK)
            Field(name: "aud", value: claims.aud.joined(separator: ", "), ok: claims.audienceOK)
            Field(name: "tid", value: claims.tid, ok: claims.tenantOK)
            Field(name: "azp", value: claims.azp, ok: claims.clientOK)
            Field(name: "scp", value: claims.scp, ok: claims.scopeOK)
            Field(name: "roles", value: claims.roles.isEmpty ? "none" : claims.roles.joined(separator: ", "))
            Field(name: "oid", value: claims.oid)
            Field(name: "idp", value: claims.idp ?? account.idTokenIdp ?? "none (local account)")
            Field(name: "exp", value: SpikeFormat.expiry(claims.exp))
        } else {
            Text("No access token decoded in this session yet: tap a Refresh button.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
}

private struct Field: View {
    let name: String
    let value: String?
    var ok: Bool?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(name)
                .foregroundStyle(.secondary)
                .frame(width: 80, alignment: .leading)
            Text(value ?? "missing")
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            if let ok {
                Image(systemName: ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                    .foregroundStyle(ok ? .green : .red)
                    .accessibilityLabel(ok ? "matches what the backend expects" : "does not match what the backend expects")
            }
        }
        .font(.caption.monospaced())
    }
}

private struct LogRow: View {
    let entry: LogEntry

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline) {
                Image(systemName: entry.ok ? "checkmark.circle.fill" : "xmark.octagon.fill")
                    .foregroundStyle(entry.ok ? .green : .red)
                Text("\(entry.kind.rawValue) · \(entry.trigger.rawValue)").bold()
                Spacer()
                Text(SpikeFormat.timestamp(entry.at)).foregroundStyle(.secondary)
            }
            Text(entry.account)
            Text(entry.result).foregroundStyle(entry.ok ? Color.primary : Color.red)
            Text(sinceText).foregroundStyle(.secondary)
        }
        .font(.caption2.monospaced())
        .textSelection(.enabled)
    }

    private var sinceText: String {
        let hours = entry.hoursSinceInteractive.map { SpikeFormat.duration(hours: $0) }
        if entry.kind == .interactive {
            return hours.map { "previous interactive sign-in \($0) before" } ?? "first interactive sign-in for this account"
        }
        return hours.map { "last interactive sign-in \($0) ago" } ?? "last interactive sign-in unknown"
    }
}
