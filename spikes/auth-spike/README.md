# Identity spike (Phase 1, step 2)

A one-screen SwiftUI app that answers the go/no-go questions of the v2 plan's identity spike, on a physical
iPhone:

- **(a)** Sign in with Apple, brokered by Microsoft Entra External ID, completes inside MSAL's
  `ASWebAuthenticationSession`.
- **(b)** Silent token refresh still works **≥ 25 h** and **≥ 7 days** after the interactive sign-in, for an
  Apple account and for an email+password account.

The plan's no-go paths:

- If Apple fails in the browser sheet, ship email+password only and revisit Apple later.
- If refresh tokens die within 24 h for both methods, stop and re-plan identity (native auth, or native Sign in
  with Apple with backend sessions).

The app-only Graph delete check (`IDENTITY_DELETE_MODE`) is a separate part of the spike and is not in this app.

## What the app does

- **Continue with Apple** starts an interactive sign-in with `domain_hint=apple`. External ID documents that
  value as "issuer acceleration": it skips the hosted page and opens Apple's sign-in directly. It was checked
  against this tenant (see Findings).
- **Continue with email** opens the hosted page. Sign-up starts there, from "Create one".
  `prompt=create` is never sent: this tenant rejects it with AADSTS50021.
- Two toggles apply to both buttons:
  - **Ask for credentials (prompt=login)**, on by default. It stops the browser's session cookie from
    signing you straight back into the account you used before.
  - **Private browser session** (`prefersEphemeralWebBrowserSession`), off by default, which is MSAL's
    default. When it is off, iOS first asks "Auth Spike wants to use ciamlogin.com to sign in" (tap Continue),
    and the sheet shares cookies with Safari. When it is on, there is no prompt and no shared cookies. If Apple
    fails with it off, retry once with it on and record both.
- **The app keeps every account it signs in**, so the Apple and the email+password accounts soak side by side
  in one install. Each account gets a card with:
  - its username, and when it last signed in interactively;
  - when Entra last issued it tokens (that is the age of its refresh token);
  - the current access token's `iss`, `aud`, `tid`, `azp`, `scp`, `roles`, `oid`, `idp` and `exp`, decoded
    locally from the JWT payload. ✓/✗ marks show the checks that `core/auth.py` makes.
- **Refresh silently (no force)** returns the cached access token while it is valid, and redeems the refresh
  token once it has expired. **Refresh silently (force)** always redeems it (`forceRefresh = true`). Both
  refresh every account, with one log line each.
- **On launch and on every return to the foreground**, the app runs a non-forced silent refresh for every
  account by itself. Opening the app after 25 h or 7 d is the whole test step.
- **Sign out** removes one account's tokens from the MSAL cache. Its soak ends there.
- **The log** is newest first, persistent (UserDefaults) and capped at 200 entries. Each line has:
  - the timestamp with its UTC offset;
  - the kind (`interactive`, `silent`, `forced`, `signout`) and the trigger (`launch`, `foreground`, `button`);
  - the account;
  - the result: `ok`, the token's `exp`, and whether it was a new token from Entra (with the refresh token's
    age) or the cached one;
  - or, on failure, the MSAL error domain and code, the internal code, the OAuth error, the AADSTS codes, the
    server's description and the correlation id;
  - the hours since that account's last interactive sign-in.

  **Share** exports the same lines as text.
- **About** shows the app version and MSAL's version (`MSALPublicClientApplication.sdkVersion`).

**No token is ever shown, logged or stored by the app.** Only the decoded claims are kept, and only in memory.
MSAL's own log goes to a callback that keeps nothing but `AADSTS…` codes, because MSAL does not always put the
server's code into the error it returns ([#3082](https://github.com/AzureAD/microsoft-authentication-library-for-objc/issues/3082)).

## Configuration (public identifiers, not secrets)

The three ids are not in this folder's tracked files: the repository is public, and
[docs/identity.md](../../docs/identity.md) keeps tenant and application ids in the azd env. `./configure.sh`
reads them from there (or from the environment, which wins) and writes the git-ignored
`Generated/TenantIDs.swift`.

| | |
|---|---|
| Tenant | "von Mentlen News", `ENTRA_TENANT_ID` |
| Authority | `MSALCIAMAuthority` on `https://vonmentlennews.ciamlogin.com/` |
| Client | `mynews-ios`, `MYNEWS_IOS_CLIENT_ID`, redirect `msauth.com.vonmentlen.mynews://auth` |
| Scope | `api://<ENTRA_API_CLIENT_ID>/access_as_user` (MSAL adds `openid profile offline_access`) |
| MSAL | 2.16.0, Swift Package `https://github.com/AzureAD/microsoft-authentication-library-for-objc`, exact version |
| Keychain | `cacheConfig.keychainSharingGroup` = the bundle id, so the app needs no keychain-sharing entitlement |
| Broker | `MSALGlobalConfig.brokerAvailability = .none` and `webviewType = .authenticationSession` (CIAM authorities never use the broker anyway) |
| Bundle id | `com.vonmentlen.mynews`, team 99J753GKC3, automatic signing; iOS 26.0, Swift 6 language mode |

## Build

```sh
brew install xcodegen            # 2.46.0 was used
cd spikes/auth-spike
./configure.sh                   # writes Generated/TenantIDs.swift (git-ignored) from the azd env
xcodegen generate                # writes AuthSpike.xcodeproj (git-ignored); rerun after editing project.yml

# Simulator: proves it compiles
xcodebuild -project AuthSpike.xcodeproj -scheme AuthSpike -configuration Debug \
  -sdk iphonesimulator -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath build CODE_SIGNING_ALLOWED=NO build

# iPhone
xcodebuild -project AuthSpike.xcodeproj -scheme AuthSpike -configuration Debug \
  -destination 'generic/platform=iOS' -allowProvisioningUpdates \
  -derivedDataPath build build
```

- There is no asset catalog, so the simulator-runtime trap in the repo README (`actool` wanting a matching
  runtime) cannot fire.
- **The unsigned simulator build compiles but cannot sign in.** Without a signature there is no
  application-identifier, and MSAL fails at start with `-42008 Failed to retrieve teamId from keychain`. To run
  in the simulator, drop `CODE_SIGNING_ALLOWED=NO`: Xcode then signs it to run locally, and it runs on the
  installed iOS 27.0 runtime. The soak needs the phone anyway.
- To move MSAL, change `exactVersion` in `project.yml` and run `xcodegen generate`.

## Install on the iPhone

The phone must be connected, unlocked and in Developer Mode. It reads `unavailable` when locked, and the install
then fails with a `CoreDeviceError 1011`.

```sh
xcrun devicectl list devices      # your phone must read `available`
xcrun devicectl device install app --device <UDID> build/Build/Products/Debug-iphoneos/AuthSpike.app
```

**This replaces the retired v1 app on the phone.** It has the same bundle id, because the redirect URI is
registered for that id. Its home-screen name is "Auth Spike". iOS treats the install as an update, so v1's data
container stays, but nothing reads it.

Installing a newer build of the spike keeps the log and the MSAL cache. Deleting the app erases the log, so
don't delete it during the soak.

## Test protocol

**Day 0** (one sitting, about 10 minutes):

1. Install and open the app. The Accounts section reads "No signed-in account".
2. **Continue with email**, then tap Continue on the iOS prompt. On the hosted page, sign in with your permanent
   email+password account (the admin one). Back in the app, check:
   - an `interactive · button` line reads `ok`;
   - the "email · …" card shows ✓ on `iss`, `aud`, `tid`, `azp` and `scp`;
   - `roles` reads `admin`.
3. **Continue with Apple.** The sheet should open directly on appleid.apple.com, reading "Use your Apple Account
   to sign in to 'von Mentlen News'".
   - If you see the hosted page instead, tap "Sign in with Apple" there, and note it.
   - The first Apple sign-in is a sign-up, and External ID may ask for a display name.
   - Back in the app, a second card reads "Apple · …".

   **The two must be separate accounts and must not share an email.** External ID refuses an Apple sign-up
   whose email already belongs to an email+password account. Choose **Hide My Email** on Apple's sheet. Sharing
   an Apple ID email that differs from the email+password account's also works.

   Note which Apple UI you got: the native sheet with Face ID, or Apple's web form. Also note whether anything
   left the app (nothing should).
4. **Refresh silently (force).** Two `forced` lines should read `ok · … new token from Entra`. This shows that
   both refresh tokens work at minute 0.
5. Take a screenshot of the two cards and the log. Then leave the app alone: don't open it before the 25 h mark.
   Each open redeems the refresh token and resets its age.

**Day 1** (at least 25 h after step 3):

6. Open the app and tap nothing. It writes one `silent · launch` (or `· foreground`) line per account. Each
   should read `ok · exp … · new token from Entra (refresh token 25.x h old)`, followed by `last interactive
   sign-in 25.x h ago`.
7. Take a screenshot of the log.

**Day 7** (at least 168 h after step 3):

8. Repeat steps 6 and 7. Expect `ok`, with at least 168 h since the interactive sign-in.
   - The refresh token used here is about six days old, because day 1 renewed it.
   - Day 1 tests the 24 h trap. Day 7 tests that the session survives a week of normal use.
   - If the app was opened between the checks, the line shows a younger refresh token. Repeat that check with
     a clean gap.

**Reading a failed line:**

| Error | Meaning |
|---|---|
| `-50002 interactionRequired`, `oauth invalid_grant`, AADSTS700082 | The refresh token expired from inactivity |
| AADSTS700084 | A fixed 24 h lifetime (the SPA rule) |
| AADSTS70043 | A Conditional Access sign-in frequency |
| AADSTS50173 | The grant was revoked, for example after a password change or reset |

Two other failures are not results:

- `underlying NSURLErrorDomain -1009` means the phone was offline. Reopen the app online.
- "account is no longer in the MSAL cache" means the cache was lost, not that Entra refused the token.

After a refusal that comes with `oauth invalid_grant` and `suberror bad_token`, MSAL deletes that refresh
token. Every later line for the account then reads `-50002 interactionRequired` with no AADSTS code ("No
token matching arguments found in the cache"). The first failed line is the result.

**Reporting:** send the screenshots from day 0, day 1 and day 7. Share → Copy gives the same lines as text,
with no tokens in them, but with usernames and correlation ids, so keep the lines themselves out of the
public repository. Record the outcome per method in `docs/identity.md`:

- whether Apple worked in the sheet, and which Apple UI appeared;
- the result at 25 h, with any AADSTS code;
- the result at 7 days, with any AADSTS code.

## Findings so far (2026-10-21, before the device run)

- **`domain_hint=apple` works on this tenant.** An unauthenticated `GET …/oauth2/v2.0/authorize` with
  `domain_hint=apple` answers `302` straight to `https://appleid.apple.com/auth/authorize`, also together with
  `prompt=login`. Apple receives:
  - `client_id=com.vonmentlen.mynews.entra`;
  - `response_mode=form_post`;
  - `scope=openid email name`.

  In the signed simulator build, "Continue with Apple" (private session) opened appleid.apple.com inside
  `ASWebAuthenticationSession`, reading "Use your Apple Account to sign in to 'von Mentlen News'".
  "Continue with email" opened the hosted page, with "Create one" and "Sign in with Apple".
  `prompt=login` is accepted.
- **The authority's path decides the return URL that Apple sees.**
  - An authorize URL on `https://vonmentlennews.ciamlogin.com/vonmentlennews.onmicrosoft.com/…` makes Entra
    send Apple `…/vonmentlennews.onmicrosoft.com/federation/oauth2`. Apple rejects that with `invalid_request`,
    "Invalid web redirect url", because the Services ID lists only the tenant-id forms.
  - The tenant-id path (`…ciamlogin.com/<tenant id>/…`) is accepted.
  - MSAL is safe here: it takes the authorize endpoint from the OIDC metadata, which names the tenant id even
    when the authority is the bare `https://vonmentlennews.ciamlogin.com/`.
  - Any client that builds the authorize URL from the onmicrosoft.com form will break Sign in with Apple. Such a
    client should use `https://vonmentlennews.ciamlogin.com/<ENTRA_TENANT_ID>`, or that
    return URL should be added to the Services ID.

## Sources

- MSAL releases and Swift package (2.16.0, 2026-09-22; iOS 17+ binary target):
  https://github.com/AzureAD/microsoft-authentication-library-for-objc/releases
- External ID iOS tutorial (`MSALCIAMAuthority`, `https://<subdomain>.ciamlogin.com`):
  https://learn.microsoft.com/en-us/entra/identity-platform/tutorial-mobile-app-ios-swift-prepare-app?tabs=external-tenant
- Install and configure MSAL (URL scheme, `LSApplicationQueriesSchemes`, redirect handling):
  https://learn.microsoft.com/en-us/entra/msal/objc/install-and-configure-msal
- Keychain (disable sharing by passing the bundle id): https://learn.microsoft.com/en-us/entra/msal/objc/howto-v2-keychain-objc
- Issuer acceleration, `domain_hint=apple`:
  https://learn.microsoft.com/en-us/entra/external-id/customers/concept-authentication-methods-customers#issuer-acceleration
- Apple federation (return URLs, 6-month secret):
  https://learn.microsoft.com/en-us/entra/external-id/customers/how-to-apple-federation-customers
- Refresh token lifetimes (24 h for SPAs and email OTP, 90 days otherwise):
  https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens
- AADSTS codes: https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes
- `prefersEphemeralWebBrowserSession`:
  https://developer.apple.com/documentation/authenticationservices/aswebauthenticationsession/prefersephemeralwebbrowsersession
