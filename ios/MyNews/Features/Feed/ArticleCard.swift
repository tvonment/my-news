import SwiftUI

struct ArticleCard: View {
    let article: Article

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text(article.source)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)

                if article.publishedAt != .distantPast {
                    Text("· \(article.publishedAt, format: .relative(presentation: .numeric))")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }

                if article.satire {
                    // Deliberately loud. The summary below is written in the
                    // same flat voice as real news, so this badge is the only
                    // thing telling the reader it's a joke.
                    Text("SATIRE")
                        .font(.caption2.weight(.bold))
                        .padding(.horizontal, 5)
                        .padding(.vertical, 2)
                        .background(.orange.opacity(0.2), in: RoundedRectangle(cornerRadius: 4))
                        .foregroundStyle(.orange)
                }

                Spacer(minLength: 4)

                if article.paywalled {
                    Image(systemName: "lock.fill")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .accessibilityLabel("Paywalled")
                }
                if let vote = article.vote {
                    Image(systemName: vote == .like ? "hand.thumbsup.fill" : "hand.thumbsdown.fill")
                        .font(.caption2)
                        .foregroundStyle(vote == .like ? .green : .red)
                        .opacity(article.votePending ? 0.4 : 1)
                }
                RelevanceBadge(score: article.relevance)
            }

            Text(article.title)
                .font(.headline)
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)

            if !article.why.isEmpty {
                Text(article.why)
                    .font(.caption)
                    .italic()
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if !article.summary.isEmpty {
                // Two lines per bullet keeps a card scannable at a glance; the
                // detail view shows each one in full. Without the cap a single
                // article can fill the whole screen.
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(article.summary.prefix(3), id: \.self) { bullet in
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Text("•").foregroundStyle(.tertiary)
                            Text(bullet)
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                    }
                }
                if !article.alsoReportedBy.isEmpty {
                    Label(
                        "Also: \(article.alsoReportedBy.joined(separator: ", "))",
                        systemImage: "square.stack.3d.up"
                    )
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .padding(.top, 2)
                }
            } else if !article.lead.isEmpty {
                // Not summarized yet — it scored below the threshold, or the
                // digest hasn't reached it. Show the feed teaser rather than
                // an empty card.
                Text(article.lead)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
            }
        }
        .padding(.vertical, 6)
    }
}

struct RelevanceBadge: View {
    let score: Int

    private var tint: Color {
        switch score {
        case 85...: .green
        case 65..<85: .blue
        case 40..<65: .orange
        default: .gray
        }
    }

    var body: some View {
        Text("\(score)")
            .font(.caption2.weight(.semibold).monospacedDigit())
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(tint.opacity(0.15), in: Capsule())
            .foregroundStyle(tint)
            .accessibilityLabel("Relevance \(score) out of 100")
    }
}
