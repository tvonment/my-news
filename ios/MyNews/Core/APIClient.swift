import Foundation

enum APIError: LocalizedError {
    case notConfigured
    case badURL(String)
    case http(status: Int, body: String)
    case transport(Error)

    var errorDescription: String? {
        switch self {
        case .notConfigured:
            return "Set your backend URL and function key in Settings."
        case .badURL(let raw):
            return "That doesn't look like a valid URL: \(raw)"
        case .http(let status, let body) where status == 401:
            return "Rejected by the backend (401). The function key is probably wrong.\n\(body)"
        case .http(let status, let body):
            return "Backend returned HTTP \(status).\n\(body)"
        case .transport(let error):
            return error.localizedDescription
        }
    }
}

/// Thin wrapper over the Functions HTTP API.
///
/// Deliberately a value type rather than an actor: it holds no mutable state,
/// and `URLSession` is already safe to use concurrently. The only shared
/// resource is the session, which is created once below.
struct APIClient {
    let baseURL: URL
    let functionKey: String

    private static let session: URLSession = {
        let config = URLSessionConfiguration.default
        // Flex Consumption scales to zero, so the first request after an idle
        // period pays a cold start. Waiting beats surfacing a spurious failure.
        config.waitsForConnectivity = true
        config.timeoutIntervalForRequest = 60
        config.timeoutIntervalForResource = 120
        return URLSession(configuration: config)
    }()

    init(settings: AppSettings) throws {
        guard settings.isConfigured else { throw APIError.notConfigured }
        let trimmed = settings.baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed), url.scheme != nil, url.host() != nil else {
            throw APIError.badURL(trimmed)
        }
        baseURL = url
        functionKey = settings.functionKey.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // MARK: - Endpoints

    func feed(limit: Int = 60, minRelevance: Int = 0) async throws -> [ArticleDTO] {
        var components = URLComponents(
            url: baseURL.appending(path: "api/feed"),
            resolvingAgainstBaseURL: false
        )
        components?.queryItems = [
            URLQueryItem(name: "limit", value: String(limit)),
            URLQueryItem(name: "min_relevance", value: String(minRelevance)),
        ]
        guard let url = components?.url else { throw APIError.badURL(baseURL.absoluteString) }

        let data = try await send(request(url: url, method: "GET"))
        return try JSONDecoder().decode(FeedResponse.self, from: data).items
    }

    func categories() async throws -> [CategorySummary] {
        let data = try await send(
            request(url: baseURL.appending(path: "api/categories"), method: "GET")
        )
        return try JSONDecoder().decode(CategoriesResponse.self, from: data).categories
    }

    func sendFeedback(articleID: String, signal: Vote) async throws {
        var req = request(url: baseURL.appending(path: "api/feedback"), method: "POST")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(
            withJSONObject: ["articleId": articleID, "signal": signal.rawValue]
        )
        _ = try await send(req)
    }

    /// Anonymous — proves the backend is up and its Cosmos identity works,
    /// without needing a valid function key. That distinction makes it the right
    /// first probe when diagnosing a connection problem.
    func health() async throws -> Bool {
        let data = try await send(request(url: baseURL.appending(path: "api/health"), method: "GET"))
        let json = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        return json?["status"] as? String == "healthy"
    }

    // MARK: - Plumbing

    private func request(url: URL, method: String) -> URLRequest {
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue(functionKey, forHTTPHeaderField: "x-functions-key")
        return req
    }

    private func send(_ request: URLRequest) async throws -> Data {
        do {
            let (data, response) = try await Self.session.data(for: request)
            guard let http = response as? HTTPURLResponse else { return data }
            guard (200..<300).contains(http.statusCode) else {
                let body = String(data: data, encoding: .utf8) ?? ""
                throw APIError.http(status: http.statusCode, body: String(body.prefix(300)))
            }
            return data
        } catch let error as APIError {
            throw error
        } catch {
            throw APIError.transport(error)
        }
    }
}
