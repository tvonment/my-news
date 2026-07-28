import SwiftData
import SwiftUI

@main
struct MyNewsApp: App {
    /// The article cache is deliberately disposable: everything in it can be
    /// re-fetched from the backend, so the store is configured to be deleted and
    /// rebuilt rather than migrated. That removes migration risk entirely.
    private let container: ModelContainer = {
        let schema = Schema([Article.self])
        do {
            return try ModelContainer(for: schema)
        } catch {
            // A cache that won't open is not worth crashing over — wipe it and
            // start clean; the next refresh repopulates from the API.
            let url = URL.applicationSupportDirectory.appending(path: "default.store")
            try? FileManager.default.removeItem(at: url)
            return try! ModelContainer(for: schema)
        }
    }()

    var body: some Scene {
        WindowGroup {
            RootView()
        }
        .modelContainer(container)
    }
}

struct RootView: View {
    @State private var settings = AppSettings()

    var body: some View {
        FeedView()
            .environment(settings)
    }
}
