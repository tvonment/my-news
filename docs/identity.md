# Identity: Microsoft Entra External ID

The runbook for MyNews's sign-in. It covers the External ID tenant **von Mentlen News**, its three app
registrations, the user flow with Sign in with Apple, the `admin` role, the chores that keep sign-in
working, and the device spike that decides whether Apple and silent refresh are good enough to build
on. The backend's side of the contract (what a token must carry, and what every failure answers) is in
[api.md §2](api.md#2-authentication). This file is the tenant's side.

**No ids in this file.** The repository is public, so tenant, application and object ids live in the
azd environment (`azd env get-value <NAME>`), and the commands below read them from there. None of
them is a secret, but none of them belongs in a public file either. The secrets (the Apple `.p8` key
and `MYNEWS_HMAC_KEY`) live in the password manager. `MYNEWS_HMAC_KEY` is also in the azd env, and
nothing here prints it.

## Status

As built on 2026-10-21, and checked again on 2026-10-24 with read-only Graph reads: the tenant, the
three app registrations, the user flow with its two providers and two apps, and the one role
assignment.

| Step | State |
|---|---|
| Tenant "von Mentlen News", `vonmentlennews.onmicrosoft.com`, country Switzerland (data location Europe, `tenant_region_scope` EU) | Done 2026-10-21 (§2) |
| Billing linked to the Visual Studio Enterprise subscription | Done. The tenant's resource sits in **`rg-mynews-mynews`**, not the planned `rg-mynews-identity` (§2.3) |
| App registrations `mynews-api`, `mynews-ios`, `mynews-cli`, with admin consent | Done by `scripts/entra_setup.py` (§3) |
| User flow "MyNews sign-up and sign-in": email with password and Apple, for `mynews-ios` and `mynews-cli`, collecting email and display name | Done (§4) |
| Sign in with Apple: Services ID, key, identity provider, in the user flow | Done. Renew by `APPLE_SECRET_RENEW_BY` (§5) |
| `admin` role on the owner's email+password account | Done (§6) |
| German sign-in strings: Swiss spelling, informal "du" | Open, Phase 4 (Open decision 6, §4) |
| Device spike: Apple inside ASWebAuthenticationSession; silent refresh at 25 h and 7 days | **Open** (§9) |
| Graph-mode identity removal (app-only Graph, M2M add-on) | Later. `IDENTITY_DELETE_MODE=manual` (§8.1) |
| App Review demo account and review code | Phase 5 (§8.4) |

## 1. What the tenant has to produce

Every access token the API accepts comes from this tenant and carries the claims below.
`backend/core/auth.py` checks each one and answers `401 invalid_token` when one is wrong
([api.md §2.1](api.md#21-tokens)).

| Claim | Value | Set by |
|---|---|---|
| `iss` | `https://<tenant id>.ciamlogin.com/<tenant id>/v2.0` | v2 tokens, which `mynews-api` has to request (§3.1, step 4) |
| `aud` | `mynews-api`'s application id | v2 tokens always carry the API's client id |
| `ver` | `2.0` | as `iss` |
| `tid`, `oid` | the tenant, and the user's object id (lower-case GUID) | Entra |
| `azp` | `mynews-ios` or `mynews-cli` | the backend's allowlist, `ENTRA_ALLOWED_CLIENT_IDS` |
| `scp` | contains `access_as_user` | the scope `mynews-api` exposes (§3.1, step 3) |
| `roles` | `["admin"]` for the owner, absent for everyone else | the app role on `mynews-api` (§6) |

The tenant's discovery document is at
`https://vonmentlennews.ciamlogin.com/<tenant id>/v2.0/.well-known/openid-configuration`. It
advertises exactly the issuer above, `RS256` keys under `.../discovery/v2.0/keys`, and
`tenant_region_scope: EU`.

The values, by azd env name. The tenant id and subdomain are set by hand (§2.2), and
`scripts/entra_setup.py` prints the rest. Provisioning copies the `ENTRA_*` ones into the Function
App's settings (`infra/modules/appsettings.bicep`):

| Name | What | Read by |
|---|---|---|
| `ENTRA_TENANT_ID` | Directory (tenant) id | backend (issuer, JWKS, `tid`), `get_token.py` |
| `ENTRA_TENANT_SUBDOMAIN` | `vonmentlennews`, the sign-in host's label | clients' authority, `get_token.py` |
| `ENTRA_API_CLIENT_ID` | `mynews-api`'s application id | backend (`aud`), clients (scope `api://<id>/access_as_user`) |
| `ENTRA_ALLOWED_CLIENT_IDS` | `<mynews-ios id>,<mynews-cli id>` | backend (`azp`) |
| `ENTRA_REQUIRED_SCOPE` | `access_as_user` | backend, `get_token.py` |
| `ENTRA_ADMIN_ROLE` | `admin` (app setting; the code's default) | backend |
| `MYNEWS_IOS_CLIENT_ID`, `MYNEWS_CLI_CLIENT_ID` | the two clients' application ids | the iOS build settings; `get_token.py` |
| `ENTRA_ADMIN_APP_ROLE_ID`, `ENTRA_API_SP_ID`, `ENTRA_USER_FLOW_ID`, `ENTRA_APPLE_IDP_ID` | object ids of the role, `mynews-api`'s service principal, the user flow and the Apple provider | the Graph commands in §3.4 and §7 |
| `APPLE_SECRET_RENEW_BY` | `YYYY-MM-DD` | admin health, maintenance timer |
| `IDENTITY_DELETE_MODE` | `manual` | backend |

## 2. The tenant

### 2.1 Before you start

- The subscription has the `Microsoft.AzureActiveDirectory` resource provider registered. Check with
  `az provider show -n Microsoft.AzureActiveDirectory --query registrationState`. If it isn't
  `Registered`, the owner runs `az provider register --namespace Microsoft.AzureActiveDirectory`.
- You hold at least the **Tenant Creator** role on the subscription or on the resource group.

### 2.2 Create it (owner, Entra admin center), as done on 2026-10-21

1. Sign in to <https://entra.microsoft.com> in the directory that owns the Visual Studio subscription.
   Then go to **Entra ID › Overview › Manage tenants › Create › External › Continue**.
2. Choose **Use Azure Subscription**, not the 30-day free trial. The trial runs without a subscription
   for 30 days only, and this tenant has to outlive the beta.
3. **Basics**:
   - Tenant Name: `von Mentlen News`.
   - Domain Name: `vonmentlennews`. This gives `vonmentlennews.onmicrosoft.com` and the sign-in host
     `vonmentlennews.ciamlogin.com`. The name was still free on 2026-10-21.
   - Country/Region: **Switzerland**. It cannot be changed later, and it puts the directory in the
     Europe data location. The Go-Local option is offered only for Australia and Japan.
4. **Add a subscription**: the Visual Studio Enterprise Subscription. For the resource group, choose
   **Create new**, name it `rg-mynews-identity`, and set its location to Switzerland North.
5. **Review + Create**. Creation can take up to 30 minutes; the Notifications pane shows progress.
6. Write down the two values:

   ```sh
   azd env set ENTRA_TENANT_ID <Tenant ID, from the Overview page of the new tenant>
   azd env set ENTRA_TENANT_SUBDOMAIN vonmentlennews
   ```

**If the subscription refuses the link**, create a pay-as-you-go subscription that serves only this
tenant, and use it in step 4. MyNews uses no paid add-ons (SMS, M2M, Go-Local) and stays inside the
free monthly-active-user allowance. This fallback wasn't needed: the Visual Studio subscription
accepted the link. You can check the link at any time under the tenant's **Home › Billing**.

### 2.3 As built: the tenant's resource is in `rg-mynews-mynews`

The billing link is an ordinary Azure resource, of type `Microsoft.AzureActiveDirectory/ciamDirectories`.
This one went into the azd stack's resource group rather than a resource group of its own:

```sh
az resource list --resource-type Microsoft.AzureActiveDirectory/ciamDirectories \
  --query '[].{name:name, group:resourceGroup, location:location}' -o table
# vonmentlennews.onmicrosoft.com   rg-mynews-mynews   europe
```

- **Never run `azd down` on the `mynews` environment.** It deletes `rg-mynews-mynews` and everything
  in it, and that now includes the tenant's billing link, as well as the Cosmos account with all its
  data.
- Provisioning leaves it alone. Deployments are incremental, so a resource the template doesn't
  mention is never removed, and `scripts/whatif_guard.py`'s `delete` rule stops any Delete anyway.
- **Optional, and only the owner decides:** move it into its own group. Microsoft documents a
  Resource Manager move as the way to re-home an external tenant's billing within the same directory.
  A move between resource groups is the same operation, and `az resource move` validates the move
  before it changes anything:

  ```sh
  az group create -n rg-mynews-identity -l switzerlandnorth
  az resource move --destination-group rg-mynews-identity \
    --ids "$(az resource list --resource-type Microsoft.AzureActiveDirectory/ciamDirectories --query '[0].id' -o tsv)"
  ```

## 3. App registrations

`scripts/entra_setup.py` makes all three, and it is idempotent: it finds and reuses each app by its
display name (§7). The portal steps below do the same thing by hand. Use them for a rebuild, or to
check what the script did.

Three rules of external tenants shape everything here:

- Every app is single-tenant.
- Customers can't grant consent, so an admin grants it for everyone.
- The only Microsoft Graph permissions an app may hold are `openid`, `offline_access` and `User.Read`.

### 3.1 `mynews-api`: the resource

1. **Entra ID › App registrations › New registration**. Name `mynews-api`; Supported account types
   **Single tenant only**; no redirect URI; **Register**.
2. **Expose an API › Application ID URI › Add**: keep `api://<application id>` and **Save**.
3. **Expose an API › Add a scope**:
   - Scope name: `access_as_user`.
   - Who can consent: Admins and users.
   - Admin consent display name: "Use MyNews as the signed-in user".
   - Description: "Lets the MyNews apps call the MyNews API on behalf of the signed-in user."
   - State: Enabled.
4. **Manifest**: under `"api"`, set `"requestedAccessTokenVersion": 2` and **Save**. The default is
   `null`, which means version 1 tokens, with a different issuer and audience format. Every call would
   then fail with `401 invalid_token`.
5. **App roles › Create app role**:
   - Display name: `Admin`.
   - Allowed member types: Users/Groups.
   - Value: `admin`.
   - Description: "Operates MyNews: invites, source catalog, suggestions, deletions."
   - Enabled; **Apply**.

   Microsoft's support table for external tenants says a resource's app role is created "by
   modifying the application manifest". If the blade is missing, add the role to `"appRoles"` there.
   The role has to sit on `mynews-api`, the resource, to appear in the access token's `roles`.
6. **Enterprise apps › mynews-api › Properties › Assignment required? No › Save**. With Yes, every
   customer would need an explicit assignment before they could sign in at all.

### 3.2 `mynews-ios`

1. **New registration**: `mynews-ios`, Single tenant only, **Register**.
2. **Authentication › Add a platform › iOS / macOS**. Bundle ID `com.vonmentlen.mynews`. This gives
   the redirect `msauth.com.vonmentlen.mynews://auth`, MSAL's default `msauth.<bundle id>://auth`.
3. **Authentication › Advanced settings › Allow public client flows: No**. MSAL's interactive flow
   (authorization code with PKCE, in ASWebAuthenticationSession) doesn't need it. It only enables
   native authentication, device code, resource-owner password and Integrated Windows Authentication.
   The password-handling flows would skip the hosted page and its Apple button.
4. **API permissions**:
   - **Add a permission › APIs my organization uses › mynews-api › Delegated › `access_as_user`**.
   - Keep Microsoft Graph `openid` and `offline_access`.
   - Remove `User.Read` if it is there; nothing reads the Graph profile.
   - Then **Grant admin consent for von Mentlen News**.
5. Add it to the user flow (§4).

### 3.3 `mynews-cli`

The owner's tooling: `scripts/get_token.py` and `scripts/mynews_admin.py`.

1. **New registration**: `mynews-cli`, Single tenant only.
2. **Authentication › Add a platform › Mobile and desktop applications**, custom redirect URI
   `http://localhost`. MSAL Python listens on a free port, and Entra ignores the port when it matches a
   localhost redirect.
3. **Allow public client flows: No**, for the same reasons. `get_token.py` uses authorization code
   with PKCE in the system browser, never device code.
4. API permissions as for `mynews-ios`, and admin consent.
5. Add it to the user flow (§4).
6. Record it: `azd env set MYNEWS_CLI_CLIENT_ID <id>`. `ENTRA_ALLOWED_CLIENT_IDS` holds both client
   ids, iOS first: `azd env set ENTRA_ALLOWED_CLIENT_IDS "<ios id>,<cli id>"`.

### 3.4 Check what is there (read-only)

`az rest` signs in with the Azure CLI's *default* account, which is the subscription's directory.
Graph calls for this tenant therefore take a token for it explicitly. The helper below sends that
token through stdin, so it never appears in a process list or in shell history:

```sh
TID=$(azd env get-value ENTRA_TENANT_ID)
graph() {  # graph <path> [curl options...]: Microsoft Graph v1.0 in the External ID tenant
  p=$1; shift
  AZURE_EXTENSION_USE_DYNAMIC_INSTALL=no az account get-access-token --tenant "$TID" \
      --resource-type ms-graph --query accessToken -o tsv |
    sed 's/^/Authorization: Bearer /' |
    curl -sS -H @- "$@" "https://graph.microsoft.com/v1.0$p"
}

graph '/organization?$select=displayName,countryLetterCode,tenantType'
# von Mentlen News, CH, CIAM
graph '/applications?$select=displayName,signInAudience,isFallbackPublicClient,publicClient,identifierUris,api,appRoles'
# mynews-api: requestedAccessTokenVersion 2, identifierUris [api://<its appId>], scope access_as_user, role admin (User)
# mynews-ios: isFallbackPublicClient false, publicClient.redirectUris [msauth.com.vonmentlen.mynews://auth]
# mynews-cli: isFallbackPublicClient false, publicClient.redirectUris [http://localhost]
# b2c-extensions-app: created with the tenant to hold extension attributes. Never modify or delete it.
graph "/servicePrincipals/$(azd env get-value ENTRA_API_SP_ID)?\$select=appRoleAssignmentRequired"
# false
```

`isFallbackPublicClient` is the manifest's name for "Allow public client flows".

## 4. The user flow

1. **Entra ID › External Identities › User flows › New user flow**. Name `MyNews sign-up and sign-in`.
2. **Identity providers › Email Accounts › Email with password**. Don't choose "Email one-time
   passcode": refresh tokens for apps that sign in with email OTP last 24 hours instead of 90 days, so
   everyone would have to sign in again every day. Apple shows up here once §5 is done.
3. **User attributes**: Display Name only. The email address is the sign-in name, and the method
   collects it anyway. `entra_setup.py` makes the display name optional, with the pattern `^.{1,64}$`:
   the docs' example pattern rejects umlauts and hyphens, so "Müller" or "Anne-Marie" could not sign up.
4. **Create**. Then **Applications › Add application**: `mynews-ios` and `mynews-cli`. An app can
   belong to one user flow only.
5. **How people sign up**: from the hosted page's **"No account? Create one"** link. External ID
   tenants reject `prompt=create` with `AADSTS50021` ("This is a self-service sign-up request in CIAM
   tenant"), so no client sends it.
6. **Phase 4, German strings**: Swiss spelling (`ss`, never `ß`) and the informal "du" (Open
   decision 6).
   1. **User flows › MyNews sign-up and sign-in › Languages › German (Germany) › Sign up and sign in ›
      Download defaults**.
   2. Edit the JSON.
   3. Upload it as the override.

   German (Germany) is the only German on Microsoft's list, so the app passes `ui_locales=de-DE`.
   **Company branding › Browser language customizations** edits the same file, and the newer change
   always wins, so edit in one place only.

## 5. Sign in with Apple (renew every 6 months)

### 5.1 Apple Developer portal (Account Holder or Admin, team `99J753GKC3`)

Print the exact, lower-case values to paste:

```sh
TID=$(azd env get-value ENTRA_TENANT_ID); SUB=$(azd env get-value ENTRA_TENANT_SUBDOMAIN)
printf '%s\n' "$SUB.ciamlogin.com" "$TID.ciamlogin.com"     # Domains and Subdomains
printf 'https://%s.ciamlogin.com/%s/federation/oauth2\n' \
  "$TID" "$TID"  "$TID" "$SUB"  "$SUB" "$TID"                # Return URLs (three)
```

1. **Certificates, Identifiers & Profiles › Identifiers ›** App ID `com.vonmentlen.mynews`: enable
   **Sign in with Apple** as a primary App ID, then **Save**.
2. **Identifiers › + › Services IDs**:
   - Description: `MyNews`. Apple shows it on its consent screen.
   - Identifier: `com.vonmentlen.mynews.entra`. This is Entra's "Client (Apple service) ID".
   - **Register**.
3. Select the Services ID, then **Sign in with Apple › Configure**:
   - Primary App ID: `com.vonmentlen.mynews`.
   - Domains and Subdomains: the two hosts printed above.
   - Return URLs: the three URLs printed above, all lower-case.
   - Then **Next › Done › Continue › Save**.
4. **Keys › +**:
   - Key Name: `MyNews Entra`.
   - **Sign in with Apple › Configure** with Primary App ID `com.vonmentlen.mynews`, then **Save ›
     Continue › Register**.
   - Write down the **Key ID**.
   - **Download** the `.p8`. Apple doesn't keep the key: this is the only download.
   - Put the key in the password manager with its Key ID and the Team ID, then delete it from
     Downloads. (`*.p8` is git-ignored, but the key never belongs near the repository.)

   A primary App ID can have at most two Sign in with Apple keys.

### 5.2 Entra

1. **Entra ID › External Identities › All identity providers › Built-in › Apple**:
   - Client (Apple service) ID: `com.vonmentlen.mynews.entra`.
   - Apple developer team ID: `99J753GKC3`.
   - Key ID: from §5.1.
   - Client secret (.p8) key: paste the file's contents (`pbcopy < AuthKey_<KEYID>.p8`, then copy
     something else to clear the clipboard).
   - **Save**.

   `scripts/entra_setup.py --apple-key <file>` tries the same thing through Graph first. The Azure CLI's
   token lacks `IdentityProvider.ReadWrite.All` and gets a 403, so the form is the supported path. The
   script then prints these exact steps.
2. **User flows › MyNews sign-up and sign-in › Identity providers**: tick **Apple**, then **Save**.
3. `azd env set APPLE_SECRET_RENEW_BY <YYYY-MM-DD>`, at most six months out with a margin (as built:
   2027-04-20). Then provision. From 30 days before that date, admin health fails its `secrets` check
   and the maintenance timer logs `ALERT secret_expiry`.

### 5.3 Renewal, every 6 months

Entra signs a client-secret JWT for Apple with the `.p8` key. Apple refuses a client secret valid for
more than 15,777,000 seconds (six months), and Microsoft states: "Sign in with Apple requires the admin
to renew their client secret every 6 months."

1. Before `APPLE_SECRET_RENEW_BY`: **All identity providers › Apple**. Paste the same `.p8`, or a new
   key, and **Save**. Re-running `scripts/entra_setup.py --apple-key` prints the same steps.
2. Move the date on:

   ```sh
   azd env set APPLE_SECRET_RENEW_BY <new date>
   ./scripts/whatif.sh && azd provision
   ```

3. Prove it with a throwaway CLI profile:

   ```sh
   backend/.venv/bin/python scripts/get_token.py --profile apple-check --login --decode   # choose Apple
   backend/.venv/bin/python scripts/get_token.py --profile apple-check --sign-out
   ```

   Use the Apple ID the spike already signed in with. A first Apple sign-in is also a sign-up, and it
   would leave one more user in the tenant.

**If a key leaks**: create the second key, switch Entra to it, then revoke the old key in the Apple
portal.

## 6. The admin role

Assign it to the identity you will actually use: the owner's **email+password** account (Open
decision 2). An Apple sign-in by the same person can be a separate Entra user with its own `oid`.
That is also why invitees are told to pick one sign-in method and keep it.

- **Portal**: **Entra ID › Enterprise apps › All applications › mynews-api › Users and groups › Add
  user/group**. Select the account, choose the role **Admin**, then **Assign**.
- **CLI**: §7, step 3.

Then check with `backend/.venv/bin/python scripts/get_token.py --force-refresh --decode`: `claims.roles`
is `["admin"]`.

Entra writes roles into an access token when it issues one. A new or removed role therefore reaches
the backend only with the next token, up to 60–90 minutes later, unless the client forces a
refresh. A block, by contrast, is backend state and applies at once (§8.2).

## 7. The commands Claude runs

Once per machine, the **owner** signs in to the tenant from the Azure CLI as one of its Global
Administrators (a browser sign-in), then sets the subscription back as the CLI's default:

```sh
az login --tenant vonmentlennews.onmicrosoft.com --allow-no-subscriptions
az account set --subscription "$(azd env get-value AZURE_SUBSCRIPTION_ID)"
```

`--allow-no-subscriptions` is what lets the CLI sign in to a directory that has no subscription. The
tenant then appears in `az account list --all` as "N/A(tenant level account)". After that, Claude
runs the steps below, with the owner's approval for each write. Steps 1, 3 and 4 write to the tenant,
and step 1 also records the ids in the local azd env. Step 2 only signs in, and step 5 only reads.

```sh
TID=$(azd env get-value ENTRA_TENANT_ID)

# 1. App registrations, service principals, admin consent and the user flow.
#    Idempotent: it finds existing apps by display name. It prints ids, never a token or key.
python3 scripts/entra_setup.py --tenant "$TID" | tee /dev/stderr | python3 -c '
import json, subprocess, sys
text = sys.stdin.read(); ids = json.loads(text[text.rindex("{\n"):])
names = {"ADMIN_APP_ROLE_ID": "ENTRA_ADMIN_APP_ROLE_ID", "API_SERVICE_PRINCIPAL_ID": "ENTRA_API_SP_ID",
         "USER_FLOW_ID": "ENTRA_USER_FLOW_ID", "APPLE_IDENTITY_PROVIDER_ID": "ENTRA_APPLE_IDP_ID"}
for key, value in ids.items():
    if value:
        subprocess.run(["azd", "env", "set", names.get(key, key), value], check=True)'

# 2. The owner signs in once through mynews-cli (no role needed for that), which gives their oid.
OID=$(backend/.venv/bin/python scripts/get_token.py --decode |
      python3 -c 'import json, sys; print(json.load(sys.stdin)["claims"]["oid"])')

# 3. The admin role, on mynews-api's service principal (the graph helper from section 3.4).
SP=$(azd env get-value ENTRA_API_SP_ID); ROLE=$(azd env get-value ENTRA_ADMIN_APP_ROLE_ID)
graph "/servicePrincipals/$SP/appRoleAssignedTo" -X POST -H 'Content-Type: application/json' \
  --data "{\"principalId\": \"$OID\", \"resourceId\": \"$SP\", \"appRoleId\": \"$ROLE\"}"
backend/.venv/bin/python scripts/get_token.py --force-refresh --decode    # claims.roles == ["admin"]

# 4. Apple, after section 5.1. It falls back to printing the portal steps (section 5.2).
python3 scripts/entra_setup.py --tenant "$TID" --apple-key ~/Downloads/AuthKey_<KEYID>.p8

# 5. Check (read-only): section 3.4, and the user flow's providers and apps.
FLOW=$(azd env get-value ENTRA_USER_FLOW_ID)
graph "/identity/authenticationEventsFlows/$FLOW/microsoft.graph.externalUsersSelfServiceSignUpEventsFlow/onAuthenticationMethodLoadStart/microsoft.graph.onAuthenticationMethodLoadStartExternalUsersSelfServiceSignUp/identityProviders"
# EmailPassword-OAUTH and Apple
graph "/identity/authenticationEventsFlows/$FLOW/conditions/applications/includeApplications"
# the two client ids
```

## 8. Day-2 operations

### 8.1 Manual identity removal, within 7 days of a deletion

`IDENTITY_DELETE_MODE=manual`: the purge deletes everything MyNews stores, and the Entra user stays
until the owner removes it ([data-model.md §8](data-model.md#8-deletion-and-purge-order)).

1. `backend/.venv/bin/python scripts/mynews_admin.py deletions list` shows each deletion waiting for
   removal, with its `uid` and `oid`. Overdue ones (more than 7 days) are flagged, and admin health
   fails its `deletions` check.
2. Delete the Entra user, then delete it permanently. A deleted user otherwise stays restorable for 30
   days.
   - **Portal**: **Entra ID › Users**, search the `oid`, **Delete › Yes**. Then **Users › Deleted
     users**, the same user, **Delete permanently**.
   - **Graph** (a write; owner-approved), with the §3.4 helper:

     ```sh
     graph "/users/<oid>" -X DELETE
     graph "/directory/deletedItems/<oid>" -X DELETE
     ```

3. `backend/.venv/bin/python scripts/mynews_admin.py deletions complete <uid>` erases the stored
   `oid` and starts the tombstone's 30-day expiry.

### 8.2 Blocking

`mynews_admin.py users block <uid> --reason "..."` takes effect on the account's next request
(`403 account_blocked`). Export and delete stay available to the blocked person. Disabling sign-in
itself (**Entra ID › Users › the user › Edit properties › Account enabled** off, and **Revoke
sessions**) isn't needed for MyNews.

### 8.3 Calendar

| When | Chore |
|---|---|
| Every 6 months, before `APPLE_SECRET_RENEW_BY` | Renew the Apple client secret (§5.3) |
| Within 7 days of each account deletion | Identity removal (§8.1) |
| Within the week of a failed sign-in | Read **Monitoring › Sign-in logs**: an external tenant keeps them 7 days |
| Graph mode only (Later) | Rotate the `mynews-admin` client secret before it expires (at most 24 months; Key Vault) |

### 8.4 App Review (Phase 5)

- Mint a review code: `mynews_admin.py invites create --label "App Review" --max-uses 5
  --expires-days 60`.
- Create a demo email+password account through the hosted page's "Create one" link (or **Entra ID ›
  Users › New user › Create new external user**). Give it no admin role and no MFA, and redeem a code
  with it once, so the reviewer lands on a working app.
- Put both in the App Store Connect review notes.
- **If a reviewer deletes the demo account**, that identity's tombstone answers `410` from then on. It
  can never redeem again, by design.
  1. Complete its identity removal (§8.1).
  2. Create a new demo identity, which has a new `oid` and so a new `uid`.
  3. Redeem a fresh code with it.
  4. Update the review notes.

### 8.5 When sign-in fails

| Code | Usually |
|---|---|
| `AADSTS50011` | The redirect URI doesn't match the registration (§3.2 step 2, §3.3 step 2) |
| `AADSTS50021` | A client sent `prompt=create`: sign up through "Create one" instead (§4) |
| `AADSTS65001` | Admin consent is missing on the client (§3.2 step 4) |
| `AADSTS500011` | The scope names a resource the tenant doesn't know: wrong `ENTRA_API_CLIENT_ID`, or no App ID URI (§3.1 step 2) |
| `AADSTS700016` | The client id isn't in this tenant: wrong `MYNEWS_*_CLIENT_ID`, or wrong authority |
| `AADSTS700082` | The refresh token has expired: sign in again. In the spike, this is the result to record (§9) |
| Apple: `invalid_request`, "Invalid web redirect url" | The authority named the tenant by its domain (`…ciamlogin.com/vonmentlennews.onmicrosoft.com`), so Entra sent Apple a return URL that the Services ID doesn't list (§5.1). Use the tenant-id path, as `get_token.py` does, or the bare `https://vonmentlennews.ciamlogin.com/`, whose metadata names the tenant id |
| API `401 invalid_token` | Compare `get_token.py --decode` with §1. A v1 token (`ver` 1.0) means step 4 of §3.1 is missing |

## 9. The device spike: go or no-go before any Phase 2 code

### 9.1 What it decides

| Question | Go | No-go |
|---|---|---|
| Does Apple sign-in complete inside ASWebAuthenticationSession, MSAL iOS's default browser on iOS 12 and later? | Ship Apple and email+password | Ship email+password only; revisit Apple later |
| Does silent refresh work after 25 hours and after 7 days, for **both** methods? | Build on MSAL's refresh | Both fail within 24 h: stop and re-plan identity (native auth, or native Sign in with Apple with backend sessions) |
| Does app-only Graph deletion work with the M2M add-on on the linked subscription? | `IDENTITY_DELETE_MODE=graph` (Later) | Stay `manual` |

Microsoft documents 90-day refresh tokens for public clients, and 24 hours only for single-page apps
and email one-time passcodes. Both methods should therefore pass. The spike checks that this holds
for a federated Apple identity in this tenant, on a real device.

### 9.2 How to run it

- **iPhone**, with [`spikes/auth-spike`](../spikes/auth-spike/README.md). Its README has the build,
  the install and the full test protocol. The app uses the bundle id that
  `msauth.com.vonmentlen.mynews://auth` is registered for, so it replaces the retired v1 app on the
  device.
  - **Day 0**: **Continue with email** with your permanent email+password account, then **Continue
    with Apple**. The Apple account must not share that account's email: External ID refuses an
    Apple sign-up whose email already belongs to an email+password account, and Hide My Email avoids
    the clash. Then **Refresh silently (force)**: both lines must read `ok`. Then leave the app closed.
  - **Day 1** (at least 25 h after the Apple sign-in) and **day 7** (at least 168 h): open the app and
    tap nothing. It refreshes every account silently on launch, and by then the access tokens (60–90
    minutes) have long expired. Each `silent · launch` line is therefore a redemption of the refresh
    token, which is the thing being tested. Opening the app in between redeems it too and restarts
    its clock, so leave it closed between the checks.
  - Each line reads `ok`, or gives the MSAL and AADSTS codes of the failure, with the hours since that
    account's interactive sign-in. The spike README's "Reading a failed line" says what each code
    means: `AADSTS700082` is expiry from inactivity, and `AADSTS700084` is a fixed 24-hour lifetime.
- **Mac, as a cross-check of the refresh-token lifetime** (the system browser, not the in-app sheet):

  ```sh
  backend/.venv/bin/python scripts/get_token.py --profile soak-email --login > /dev/null   # T0, email+password
  backend/.venv/bin/python scripts/get_token.py --profile soak-apple --login > /dev/null   # T0, Apple
  # at T0 + 25 h and T0 + 7 d, for each profile; exit 0 = refreshed, exit 3 prints the error:
  backend/.venv/bin/python scripts/get_token.py --profile soak-email --force-refresh --silent-only --decode
  ```

### 9.3 The record

Copy each attempt from the spike's log into this table. For the Apple sign-in, also note which Apple
UI appeared (the native sheet with Face ID, or Apple's web form), whether **Private browser session**
was on, and whether anything left the app. Then write the decision below the table and in the plan's
Progress note.

| Check | Method | Device, iOS, spike build | Sign-in (T0, UTC) | Attempt (UTC) | Hours since T0 | Result | MSAL / AADSTS code |
|---|---|---|---|---|---|---|---|
| Interactive sign-in in ASWebAuthenticationSession | Apple | | | | 0 | | |
| Interactive sign-in | Email | | | | 0 | | |
| Forced silent refresh, day 0 | Apple | | | | 0 | | |
| Forced silent refresh, day 0 | Email | | | | 0 | | |
| Silent refresh on launch, day 1 | Apple | | | | ≥ 25 | | |
| Silent refresh on launch, day 1 | Email | | | | ≥ 25 | | |
| Silent refresh on launch, day 7 | Apple | | | | ≥ 168 | | |
| Silent refresh on launch, day 7 | Email | | | | ≥ 168 | | |
| CLI forced refresh (cross-check) | Email | Mac, `get_token.py` | | | ≥ 25 / ≥ 168 | | |
| CLI forced refresh (cross-check) | Apple | Mac, `get_token.py` | | | ≥ 25 / ≥ 168 | | |

**Decision**: (Apple: go / no-go) · (refresh: go / no-go) · date · who.

## Sources

Microsoft Learn and Apple Developer, read on 2026-10-21. The Apple, refresh-token, language and
error-code pages were read again on 2026-10-24.

- [Create an external tenant](https://learn.microsoft.com/en-us/entra/external-id/customers/how-to-create-external-tenant-portal):
  the Manage tenants path; trial vs Use Azure Subscription; Tenant Creator; country fixed at creation;
  up to 30 minutes.
- [External tenant features](https://learn.microsoft.com/en-us/entra/external-id/customers/concept-supported-features-customers):
  authority `<tenant-name>.ciamlogin.com`; single-tenant apps; allowed Graph permissions and
  admin-only consent; no ROPC; client credentials for v2.0 applications only; 30-day soft delete;
  7-day sign-in logs.
- [Register an app, and grant admin consent in external tenants](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app)
- [App manifest, `api.requestedAccessTokenVersion`](https://learn.microsoft.com/en-us/entra/identity-platform/reference-microsoft-graph-app-manifest):
  `null` defaults to version 1. The same page covers `isFallbackPublicClient` and the `api://<appId>`
  App ID URI.
- [Access token claims](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference):
  in v2.0 tokens `aud` is always the API's client id; `iss` ends in `/v2.0`; `azp`, `scp`, `roles`, `oid`.
- [App roles in external tenants](https://learn.microsoft.com/en-us/entra/external-id/customers/how-to-use-app-roles-customers)
  and [groups and app roles support](https://learn.microsoft.com/en-us/entra/external-id/customers/reference-group-app-roles-support):
  roles are created through the manifest; Enterprise apps › Users and groups.
- [Graph: grant an appRoleAssignment](https://learn.microsoft.com/en-us/graph/api/serviceprincipal-post-approleassignedto?view=graph-rest-1.0)
- [Public and confidential clients](https://learn.microsoft.com/en-us/entra/identity-platform/msal-client-applications):
  public client flows only for native auth, device code, ROPC and Windows Integrated Authentication.
- [Redirect URI rules](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url): the port is
  ignored for localhost (RFC 8252); `AADSTS50011`.
- [MSAL Python](https://learn.microsoft.com/en-us/entra/msal/python/) and
  [`PublicClientApplication.acquire_token_interactive`](https://learn.microsoft.com/en-us/python/api/msal/msal.application.publicclientapplication?view=msal-py-latest):
  the `<subdomain>.ciamlogin.com` authority; the `http://localhost` redirect.
- [MSAL iOS redirect URIs](https://learn.microsoft.com/en-us/entra/msal/objc/redirect-uris-ios) and
  [MSAL iOS web views](https://learn.microsoft.com/en-us/entra/msal/objc/customize-webviews):
  `msauth.<bundle id>://auth`; ASWebAuthenticationSession by default on iOS 12 and later.
- [Create a user flow](https://learn.microsoft.com/en-us/entra/external-id/customers/how-to-user-flow-sign-up-sign-in-customers),
  [add an application](https://learn.microsoft.com/en-us/entra/external-id/customers/how-to-user-flow-add-application)
  (one user flow per app) and
  [add an identity provider](https://learn.microsoft.com/en-us/entra/external-id/customers/how-to-add-identity-provider-to-user-flow-customers).
- [Refresh tokens](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens): 90 days by
  default; 24 hours for SPAs and for apps that use email one-time passcode.
- [Error codes](https://learn.microsoft.com/en-us/entra/identity-platform/reference-error-codes):
  `AADSTS50011`, `65001`, `500011`, `700016`, `700082` (inactivity) and `700084` (the fixed SPA
  lifetime). `AADSTS50021` for `prompt=create` isn't listed there; it was observed on this tenant.
- [Add Apple as an identity provider](https://learn.microsoft.com/en-us/entra/external-id/customers/how-to-apple-federation-customers):
  domains, lower-case return URLs, key, the portal form, the 6-month renewal.
- [Customize languages](https://learn.microsoft.com/en-us/entra/external-id/customers/how-to-customize-languages-customers):
  German (Germany); the user flow's Languages and Company branding edit one file.
- [Manage customer accounts](https://learn.microsoft.com/en-us/entra/external-id/customers/how-to-manage-customer-accounts)
  and [restore or permanently delete a user](https://learn.microsoft.com/en-us/entra/fundamentals/users-restore).
- [External ID pricing](https://learn.microsoft.com/en-us/entra/external-id/external-identities-pricing):
  MAU billing and add-ons; checking the link under Home › Billing; moving the tenant's resource with a
  Resource Manager move.
- [`ciamDirectories` resource type](https://learn.microsoft.com/en-us/azure/templates/microsoft.azureactivedirectory/ciamdirectories)
- [Azure CLI sign-in with `--tenant`](https://learn.microsoft.com/en-us/cli/azure/authenticate-azure-cli-interactively)
  and [`az login` reference (`--allow-no-subscriptions`)](https://learn.microsoft.com/en-us/cli/azure/reference-index#az-login)
- Apple:
  - [Configuring your environment for Sign in with Apple](https://developer.apple.com/documentation/signinwithapple/configuring-your-environment-for-sign-in-with-apple):
    primary App ID, Services ID, at most two keys per primary app.
  - [Configure Sign in with Apple for the web](https://developer.apple.com/help/account/capabilities/configure-sign-in-with-apple-for-the-web)
  - [Create a private key](https://developer.apple.com/help/account/keys/create-a-private-key): one
    download only.
  - [Creating a client secret](https://developer.apple.com/documentation/accountorganizationaldatasharing/creating-a-client-secret):
    `exp` at most 15,777,000 seconds.
