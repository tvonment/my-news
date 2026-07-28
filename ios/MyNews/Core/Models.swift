import Foundation
import SwiftData

// MARK: - Wire format

/// One article as the API sends it. Every field except `id`, `title` and `url`
/// is optional: an article moves through harvested → gated → scored →
/// summarized, and the client can legitimately receive one at any stage.
struct ArticleDTO: Decodable {
    let id: String
    let title: String
    let url: String
    let source: String
    let publishedAt: Date?
    let lead: String
    let relevance: Int
    let why: String
    let categories: [String]
    let summary: [String]
    let entities: [String]
    let readingMinutes: Int
    let paywalled: Bool
    let satire: Bool
    let alsoReportedBy: [String]
    let clusterSize: Int

    private enum CodingKeys: String, CodingKey {
        case id, title, url, source, publishedAt, lead, relevance, why
        case categories, summary, entities, readingMinutes, paywalled
        case satire, alsoReportedBy, clusterSize
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? "(untitled)"
        url = try c.decodeIfPresent(String.self, forKey: .url) ?? ""
        source = try c.decodeIfPresent(String.self, forKey: .source) ?? "Unknown"
        lead = try c.decodeIfPresent(String.self, forKey: .lead) ?? ""
        relevance = try c.decodeIfPresent(Int.self, forKey: .relevance) ?? 0
        why = try c.decodeIfPresent(String.self, forKey: .why) ?? ""
        categories = try c.decodeIfPresent([String].self, forKey: .categories) ?? []
        summary = try c.decodeIfPresent([String].self, forKey: .summary) ?? []
        entities = try c.decodeIfPresent([String].self, forKey: .entities) ?? []
        readingMinutes = try c.decodeIfPresent(Int.self, forKey: .readingMinutes) ?? 0
        paywalled = try c.decodeIfPresent(Bool.self, forKey: .paywalled) ?? false
        satire = try c.decodeIfPresent(Bool.self, forKey: .satire) ?? false
        alsoReportedBy = try c.decodeIfPresent([String].self, forKey: .alsoReportedBy) ?? []
        clusterSize = try c.decodeIfPresent(Int.self, forKey: .clusterSize) ?? 1

        // The backend emits ISO-8601 with a Z suffix and no fractional seconds.
        if let raw = try c.decodeIfPresent(String.self, forKey: .publishedAt) {
            publishedAt = ArticleDTO.isoFormatter.date(from: raw)
                ?? ArticleDTO.isoFractionalFormatter.date(from: raw)
        } else {
            publishedAt = nil
        }
    }

    static let isoFormatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    static let isoFractionalFormatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
}

struct FeedResponse: Decodable {
    let count: Int
    let items: [ArticleDTO]
}

/// One bucket of the fixed taxonomy.
///
/// Served by the backend rather than hardcoded here, so the app and the scorer
/// can never disagree about what the categories are — the scorer enforces the
/// same list via a schema enum.
struct CategorySummary: Codable, Identifiable, Hashable {
    let id: String
    let label: String
    let count: Int
}

struct CategoriesResponse: Decodable {
    let categories: [CategorySummary]
}

// MARK: - Local cache

enum Vote: String, Codable {
    case like
    case dislike
}

@Model
final class Article {
    @Attribute(.unique) var id: String

    var title: String
    var url: String
    var source: String
    var publishedAt: Date
    var lead: String
    var relevance: Int
    var why: String
    var categories: [String]
    var summary: [String]
    var entities: [String]
    var readingMinutes: Int
    var paywalled: Bool
    /// Deliberately fake. Must stay visible in the UI — a summary strips the
    /// tonal cues that tell a reader it's a joke.
    var satire: Bool
    /// Other outlets that covered the same story, folded into this card.
    var alsoReportedBy: [String]
    var clusterSize: Int

    /// The vote shown in the UI. Set immediately on tap so the interface never
    /// waits on the network.
    var voteRaw: String?
    /// True while the vote has not yet been accepted by the backend. Pending
    /// votes are retried on the next refresh rather than being lost.
    var votePending: Bool
    var opened: Bool
    var cachedAt: Date

    var vote: Vote? {
        get { voteRaw.flatMap(Vote.init(rawValue:)) }
        set { voteRaw = newValue?.rawValue }
    }

    init(dto: ArticleDTO) {
        id = dto.id
        title = dto.title
        url = dto.url
        source = dto.source
        publishedAt = dto.publishedAt ?? .distantPast
        lead = dto.lead
        relevance = dto.relevance
        why = dto.why
        categories = dto.categories
        summary = dto.summary
        entities = dto.entities
        readingMinutes = dto.readingMinutes
        paywalled = dto.paywalled
        satire = dto.satire
        alsoReportedBy = dto.alsoReportedBy
        clusterSize = dto.clusterSize
        voteRaw = nil
        votePending = false
        opened = false
        cachedAt = .now
    }

    /// Refresh the server-owned fields, leaving local state (vote, opened) alone.
    func apply(_ dto: ArticleDTO) {
        title = dto.title
        url = dto.url
        source = dto.source
        publishedAt = dto.publishedAt ?? publishedAt
        lead = dto.lead
        relevance = dto.relevance
        why = dto.why
        categories = dto.categories
        summary = dto.summary
        entities = dto.entities
        readingMinutes = dto.readingMinutes
        paywalled = dto.paywalled
        satire = dto.satire
        alsoReportedBy = dto.alsoReportedBy
        clusterSize = dto.clusterSize
        cachedAt = .now
    }
}
