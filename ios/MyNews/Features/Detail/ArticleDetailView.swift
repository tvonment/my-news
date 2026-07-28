import SwiftData
import SwiftUI

struct ArticleDetailView: View {
    let article: Article

    @Environment(\.modelContext) private var context
    @Environment(AppSettings.self) private var settings
    @State private var store = FeedStore()
    @State private var showingOriginal = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                header
                Divider()

                if !article.summary.isEmpty {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(article.summary, id: \.self) { bullet in
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Image(systemName: "circle.fill")
                                    .font(.system(size: 5))
                                    .foregroundStyle(.tertiary)
                                    .padding(.top, 6)
                                Text(bullet)
                                    .font(.body)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                } else if !article.lead.isEmpty {
                    Text(article.lead).font(.body)
                }

                if article.paywalled {
                    Label(
                        "This article is behind a paywall — the summary is based on the feed teaser only.",
                        systemImage: "lock"
                    )
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                }

                if !article.entities.isEmpty {
                    ChipRow(title: "Mentioned", items: article.entities)
                }
                if !article.categories.isEmpty {
                    ChipRow(title: "Categories", items: article.categories)
                }

                openOriginal
                voteButtons
            }
            .padding()
        }
        .navigationTitle(article.source)
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $showingOriginal) {
            if let url = URL(string: article.url) {
                // SFSafariViewController, not an embedded WKWebView: this is
                // someone else's page, and Safari brings the reader's own
                // cookies, content blockers and Reader mode with it.
                SafariView(url: url)
                    .ignoresSafeArea()
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(article.title)
                .font(.title2.bold())
                .fixedSize(horizontal: false, vertical: true)

            HStack(spacing: 8) {
                RelevanceBadge(score: article.relevance)
                if article.publishedAt != .distantPast {
                    Text(article.publishedAt, format: .relative(presentation: .named))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if article.readingMinutes > 0 {
                    Text("· \(article.readingMinutes) min read")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            if !article.why.isEmpty {
                Text(article.why)
                    .font(.subheadline)
                    .italic()
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var openOriginal: some View {
        Button {
            article.opened = true
            try? context.save()
            showingOriginal = true
        } label: {
            Label("Open original", systemImage: "safari")
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.borderedProminent)
        .controlSize(.large)
        .disabled(URL(string: article.url) == nil)
    }

    private var voteButtons: some View {
        HStack(spacing: 12) {
            voteButton(.dislike, title: "Less like this", icon: "hand.thumbsdown", tint: .red)
            voteButton(.like, title: "More like this", icon: "hand.thumbsup", tint: .green)
        }
    }

    private func voteButton(_ vote: Vote, title: String, icon: String, tint: Color) -> some View {
        let selected = article.vote == vote
        return Button {
            Task { await store.vote(article, vote, context: context, settings: settings) }
        } label: {
            Label(title, systemImage: selected ? "\(icon).fill" : icon)
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.bordered)
        .controlSize(.large)
        .tint(selected ? tint : .secondary)
    }
}

private struct ChipRow: View {
    let title: String
    let items: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(items, id: \.self) { item in
                        Text(item)
                            .font(.caption)
                            .padding(.horizontal, 9)
                            .padding(.vertical, 4)
                            .background(.quaternary, in: Capsule())
                    }
                }
            }
        }
    }
}
