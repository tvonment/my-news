import SwiftUI

/// Horizontal category filter.
///
/// The category *list* comes from the backend, because the same fixed taxonomy
/// is enforced in the scorer's JSON schema — hardcoding it here would create
/// two sources of truth that drift the moment one changes.
///
/// The *counts*, though, are computed from the articles actually loaded. The
/// backend counts the whole archive, and the app holds only the current top
/// slice, so a chip reading "Apple 118" would open a list of twenty. A count
/// that disagrees with what tapping it shows is worse than no count at all.
///
/// Empty buckets are hidden. A chip that always reads "0" looks like the filter
/// is broken rather than like there is no Science news today.
struct CategoryChips: View {
    let categories: [CategorySummary]
    let selected: String?
    let articles: [Article]
    let onSelect: (String?) -> Void

    private var counts: [String: Int] {
        articles.reduce(into: [:]) { totals, article in
            for category in article.categories { totals[category, default: 0] += 1 }
        }
    }

    private var populated: [(category: CategorySummary, count: Int)] {
        let counts = counts
        return categories.compactMap { category in
            let count = counts[category.id] ?? 0
            return count > 0 ? (category, count) : nil
        }
    }

    var body: some View {
        if !populated.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    Chip(
                        label: "All",
                        count: articles.count,
                        isSelected: selected == nil,
                        action: { onSelect(nil) }
                    )

                    ForEach(populated, id: \.category.id) { entry in
                        Chip(
                            label: entry.category.label,
                            count: entry.count,
                            isSelected: selected == entry.category.id,
                            // Tapping the active chip clears it, so you can get
                            // back to All without travelling to the far left.
                            action: {
                                onSelect(selected == entry.category.id ? nil : entry.category.id)
                            }
                        )
                    }
                }
                .padding(.horizontal)
                .padding(.vertical, 8)
            }
            .background(.bar)
        }
    }
}

private struct Chip: View {
    let label: String
    let count: Int
    let isSelected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Text(label)
                    .font(.subheadline.weight(isSelected ? .semibold : .regular))
                Text("\(count)")
                    .font(.caption2.monospacedDigit())
                    .opacity(0.7)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .background(
                isSelected ? AnyShapeStyle(.tint) : AnyShapeStyle(.quaternary),
                in: Capsule()
            )
            .foregroundStyle(isSelected ? AnyShapeStyle(.white) : AnyShapeStyle(.primary))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(label), \(count) articles")
        .accessibilityAddTraits(isSelected ? [.isSelected] : [])
    }
}
