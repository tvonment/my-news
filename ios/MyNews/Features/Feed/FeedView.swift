import SwiftData
import SwiftUI

struct FeedView: View {
    @Environment(\.modelContext) private var context
    @Environment(AppSettings.self) private var settings

    // Ranked by relevance, not recency — the pipeline already folded a recency
    // preference into the gate score, so ordering by date here would undo it.
    @Query(sort: [SortDescriptor(\Article.relevance, order: .reverse)])
    private var articles: [Article]

    @State private var store = FeedStore()
    @State private var showingSettings = false

    var body: some View {
        NavigationStack {
            Group {
                if !settings.isConfigured {
                    NotConfiguredView { showingSettings = true }
                } else if articles.isEmpty {
                    EmptyFeedView(state: store.state) {
                        await store.refresh(context: context, settings: settings)
                    }
                } else {
                    VStack(spacing: 0) {
                        CategoryChips(
                            categories: store.categories,
                            selected: store.selectedCategory,
                            articles: articles,
                            onSelect: { store.selectedCategory = $0 }
                        )
                        feedList
                    }
                }
            }
            .navigationTitle("MyNews")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Settings", systemImage: "gearshape") { showingSettings = true }
                }
            }
            .sheet(isPresented: $showingSettings) {
                SettingsView()
            }
        }
        .task {
            guard settings.isConfigured else { return }
            // Cached articles render immediately; this only tops them up.
            if articles.isEmpty {
                await store.refresh(context: context, settings: settings)
            } else {
                await store.loadCategories(settings: settings)
            }
        }
    }

    /// Articles matching the selected chip.
    ///
    /// Filtered in memory rather than by re-querying: the feed is at most a few
    /// hundred rows, and switching chips should be instant and work offline.
    private var visible: [Article] {
        guard let selected = store.selectedCategory else { return articles }
        return articles.filter { $0.categories.contains(selected) }
    }

    private var feedList: some View {
        List {
            if case .failed(let message) = store.state {
                Section {
                    Label(message, systemImage: "exclamationmark.triangle")
                        .font(.footnote)
                        .foregroundStyle(.orange)
                }
            }

            if visible.isEmpty {
                ContentUnavailableView {
                    Label("Nothing here yet", systemImage: "tray")
                } description: {
                    Text("No articles in this category from the latest run.")
                }
                .listRowSeparator(.hidden)
            }

            ForEach(visible) { article in
                NavigationLink {
                    ArticleDetailView(article: article)
                } label: {
                    ArticleCard(article: article)
                }
                .swipeActions(edge: .leading, allowsFullSwipe: true) {
                    Button {
                        Task { await store.vote(article, .like, context: context, settings: settings) }
                    } label: {
                        Label("More like this", systemImage: "hand.thumbsup.fill")
                    }
                    .tint(.green)
                }
                .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                    Button {
                        Task { await store.vote(article, .dislike, context: context, settings: settings) }
                    } label: {
                        Label("Less like this", systemImage: "hand.thumbsdown.fill")
                    }
                    .tint(.red)
                }
            }
        }
        .listStyle(.plain)
        .refreshable {
            await store.refresh(context: context, settings: settings)
        }
    }
}

private struct NotConfiguredView: View {
    let openSettings: () -> Void

    var body: some View {
        ContentUnavailableView {
            Label("Not connected", systemImage: "link.badge.plus")
        } description: {
            Text("Add your backend URL and function key to start pulling your feed.")
        } actions: {
            Button("Open Settings", action: openSettings)
                .buttonStyle(.borderedProminent)
        }
    }
}

private struct EmptyFeedView: View {
    let state: FeedStore.LoadState
    let refresh: () async -> Void

    var body: some View {
        switch state {
        case .loading:
            ProgressView("Fetching your feed…")
        case .failed(let message):
            ContentUnavailableView {
                Label("Couldn't load the feed", systemImage: "wifi.exclamationmark")
            } description: {
                Text(message)
            } actions: {
                Button("Try again") { Task { await refresh() } }
                    .buttonStyle(.borderedProminent)
            }
        case .idle:
            ContentUnavailableView {
                Label("Nothing scored yet", systemImage: "tray")
            } description: {
                Text("The crawler runs every 30 minutes and the digest three times a day. Pull to refresh.")
            } actions: {
                Button("Refresh") { Task { await refresh() } }
            }
        }
    }
}
