import Foundation

/// The External ID tenant "von Mentlen News" and the mynews-ios registration.
/// These are public identifiers, not secrets: a public client has none. The ids
/// themselves come from Generated/TenantIDs.swift, which ./configure.sh writes from
/// the azd env and git ignores, because the repository is public (docs/identity.md).
nonisolated enum SpikeConfig {
    static let tenantID = TenantIDs.tenantID

    /// MSALCIAMAuthority turns the bare subdomain form into
    /// https://vonmentlennews.ciamlogin.com/vonmentlennews.onmicrosoft.com, then takes
    /// the authorize and token endpoints from the OIDC metadata, which name the tenant
    /// id. That matters for Apple: Entra builds the return URL it sends to Apple from
    /// the authorize URL's path, and the Services ID only lists the tenant-id forms.
    /// An authorize URL built by hand on the onmicrosoft.com path gets Apple's
    /// "Invalid web redirect url" (checked 2026-10-21); MSAL's does not.
    static let authority = "https://vonmentlennews.ciamlogin.com/"

    static let clientID = TenantIDs.iosClientID
    static let redirectURI = "msauth.com.vonmentlen.mynews://auth"

    /// mynews-api. MSAL adds openid, profile and offline_access by itself.
    static let apiClientID = TenantIDs.apiClientID
    static let apiScope = "api://\(apiClientID)/access_as_user"
    static let scopes = [apiScope]

    /// "Issuer acceleration": External ID documents domain_hint=apple as the value
    /// that skips its own page and opens Apple's sign-in directly.
    static let appleDomainHint = "apple"

    /// What the backend's core/auth.py checks, shown as ✓/✗ next to each claim.
    static let expectedIssuer = "https://\(tenantID).ciamlogin.com/\(tenantID)/v2.0"
    static let expectedScope = "access_as_user"
}
