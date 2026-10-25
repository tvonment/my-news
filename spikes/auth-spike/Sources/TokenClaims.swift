import Foundation

/// The claims the spike shows, decoded locally from the access token's payload.
/// The token itself is never displayed, logged or stored: only these values are kept.
nonisolated struct TokenClaims: Sendable, Equatable {
    var iss: String?
    var aud: [String] = []
    var tid: String?
    var azp: String?
    var scp: String?
    var roles: [String] = []
    var oid: String?
    var idp: String?
    var ver: String?
    var exp: Date?
    var iat: Date?

    /// Reads the middle segment of a JWT. No signature check: the backend does that;
    /// the spike only needs to see what Entra put in the token.
    init?(jwt: String) {
        let segments = jwt.split(separator: ".", omittingEmptySubsequences: false)
        guard segments.count == 3 else { return nil }
        var base64 = String(segments[1])
            .replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        base64 += String(repeating: "=", count: (4 - base64.count % 4) % 4)
        guard let data = Data(base64Encoded: base64),
              let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return nil }

        iss = payload["iss"] as? String
        if let single = payload["aud"] as? String {
            aud = [single]
        } else if let many = payload["aud"] as? [String] {
            aud = many
        }
        tid = payload["tid"] as? String
        azp = payload["azp"] as? String
        scp = payload["scp"] as? String
        roles = payload["roles"] as? [String] ?? []
        oid = payload["oid"] as? String
        idp = payload["idp"] as? String
        ver = payload["ver"] as? String
        exp = (payload["exp"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) }
        iat = (payload["iat"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) }
    }

    /// The same checks core/auth.py makes, so a screenshot shows whether the
    /// backend would accept this token.
    var issuerOK: Bool { iss == SpikeConfig.expectedIssuer }
    var audienceOK: Bool { aud == [SpikeConfig.apiClientID] }
    var tenantOK: Bool { tid == SpikeConfig.tenantID }
    var clientOK: Bool { azp == SpikeConfig.clientID }
    var scopeOK: Bool { (scp ?? "").split(separator: " ").contains { $0 == SpikeConfig.expectedScope } }
}
