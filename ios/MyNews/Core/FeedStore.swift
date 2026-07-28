import Foundation
import Observation
import SwiftData

/// Coordinates the network with the local cache.
///
/// The views read articles straight out of SwiftData with `@Query`, so this
/// type never holds the list itself — it only fetches, reconciles, and reports
/// what happened. That keeps the feed rendering instantly from cache on launch
/// with no loading state at all.
@Observable
@MainActor
final class FeedStore {
    enum LoadState: Equatable {
        case idle
        case loading
        case failed(String)
    }

    var state: LoadState = .idle
    var lastSynced: Date?

    /// Fetch the feed and reconcile it into the cache.
    ///
    /// Reconciliation is an upsert keyed by article id: server-owned fields are
    /// overwritten, local state (your vote, whether you opened it) is preserved.
    /// Nothing is deleted — an article that drops out of the top N stays
    /// readable offline until the TTL sweep removes it.
    func refresh(context: ModelContext, settings: AppSettings) async {
        state = .loading
        defer { if case .loading = state { state = .idle } }

        do {
            let client = try APIClient(settings: settings)
            // Send anything that failed to reach the backend earlier before
            // pulling, so the next scoring run already reflects those votes.
            await flushPendingVotes(context: context, client: client)

            let dtos = try await client.feed(minRelevance: settings.minRelevance)
            try upsert(dtos, into: context)
            lastSynced = .now
            state = .idle
        } catch {
            state = .failed((error as? APIError)?.errorDescription ?? error.localizedDescription)
        }
    }

    private func upsert(_ dtos: [ArticleDTO], into context: ModelContext) throws {
        let incoming = Set(dtos.map(\.id))
        let existing = try context.fetch(
            FetchDescriptor<Article>(predicate: #Predicate { incoming.contains($0.id) })
        )
        var byID = Dictionary(uniqueKeysWithValues: existing.map { ($0.id, $0) })

        for dto in dtos {
            if let article = byID[dto.id] {
                article.apply(dto)
            } else {
                let article = Article(dto: dto)
                context.insert(article)
                byID[dto.id] = article
            }
        }
        try context.save()
    }

    // MARK: - Voting

    /// Record a vote locally and try to send it.
    ///
    /// The local write happens first and unconditionally, so the UI responds at
    /// once. If the send fails the article stays marked pending and is retried
    /// on the next refresh — a vote is never silently dropped.
    func vote(_ article: Article, _ vote: Vote, context: ModelContext, settings: AppSettings) async {
        // Tapping the same vote again clears it.
        let newVote: Vote? = (article.vote == vote) ? nil : vote
        article.vote = newVote
        article.votePending = newVote != nil
        try? context.save()

        guard let newVote else { return }

        do {
            let client = try APIClient(settings: settings)
            try await client.sendFeedback(articleID: article.id, signal: newVote)
            article.votePending = false
            try? context.save()
        } catch {
            // Stays pending; flushPendingVotes retries it.
            state = .failed((error as? APIError)?.errorDescription ?? error.localizedDescription)
        }
    }

    private func flushPendingVotes(context: ModelContext, client: APIClient) async {
        let pending = (try? context.fetch(
            FetchDescriptor<Article>(predicate: #Predicate { $0.votePending })
        )) ?? []

        for article in pending {
            guard let vote = article.vote else {
                article.votePending = false
                continue
            }
            do {
                try await client.sendFeedback(articleID: article.id, signal: vote)
                article.votePending = false
            } catch {
                // Leave it pending and stop trying this round — if one send
                // fails the rest almost certainly will too.
                break
            }
        }
        try? context.save()
    }
}
