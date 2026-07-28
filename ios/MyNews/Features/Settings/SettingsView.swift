import SwiftData
import SwiftUI

struct SettingsView: View {
    @Environment(AppSettings.self) private var settings
    @Environment(\.dismiss) private var dismiss
    @Environment(\.modelContext) private var context

    @State private var probe: ProbeResult?
    @State private var probing = false

    private enum ProbeResult: Equatable {
        case ok(articles: Int)
        case failed(String)
    }

    var body: some View {
        @Bindable var settings = settings

        NavigationStack {
            Form {
                Section {
                    TextField("https://your-app.azurewebsites.net", text: $settings.baseURL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                    SecureField("Function key", text: $settings.functionKey)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Backend")
                } footer: {
                    Text("The key is stored in the Keychain, on this device only, and is never included in a backup.")
                }

                Section {
                    Button {
                        Task { await testConnection() }
                    } label: {
                        HStack {
                            Text("Test connection")
                            Spacer()
                            if probing { ProgressView() }
                        }
                    }
                    .disabled(probing || !settings.isConfigured)

                    if let probe {
                        switch probe {
                        case .ok(let count):
                            Label("Connected — \(count) articles available", systemImage: "checkmark.circle.fill")
                                .foregroundStyle(.green)
                                .font(.footnote)
                        case .failed(let message):
                            Label(message, systemImage: "xmark.circle.fill")
                                .foregroundStyle(.red)
                                .font(.footnote)
                        }
                    }
                }

                Section {
                    Stepper(
                        "Hide below \(settings.minRelevance)",
                        value: $settings.minRelevance,
                        in: 0...90,
                        step: 5
                    )
                } header: {
                    Text("Filtering")
                } footer: {
                    Text("The scorer rates every article 0–100. Anything below this is left out of the feed.")
                }

                Section {
                    Button("Clear cached articles", role: .destructive) {
                        try? context.delete(model: Article.self)
                        try? context.save()
                    }
                } footer: {
                    Text("The cache is disposable — the next refresh rebuilds it from the backend. Local votes already sent are unaffected.")
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }

    private func testConnection() async {
        probing = true
        defer { probing = false }
        do {
            let client = try APIClient(settings: settings)
            // Health first: it is anonymous, so if it passes but the feed fails,
            // the problem is definitely the key rather than the backend.
            _ = try await client.health()
            let articles = try await client.feed(limit: 1, minRelevance: settings.minRelevance)
            probe = .ok(articles: articles.count)
        } catch {
            probe = .failed((error as? APIError)?.errorDescription ?? error.localizedDescription)
        }
    }
}
