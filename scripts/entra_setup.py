#!/usr/bin/env python3
"""Create MyNews's app registrations in the External ID tenant. Idempotent.

docs/identity.md is the runbook; this script is the part of it that can be
automated with the Azure CLI's own Graph token (az login --tenant <tenant>
--allow-no-subscriptions). It creates, or finds and reuses by display name:

- ``mynews-api``: the resource. Exposes the ``access_as_user`` scope,
  declares the ``admin`` app role, and asks for v2 access tokens, which the
  backend's exact ``iss``/``aud`` checks depend on.
- ``mynews-ios``: a public client with the MSAL redirect
  ``msauth.com.vonmentlen.mynews://auth``.
- ``mynews-cli``: a public client on ``http://localhost`` for
  scripts/get_token.py and scripts/mynews_admin.py.

Both clients get tenant-wide admin consent for ``access_as_user`` and for
``openid offline_access``. Customers of an external tenant cannot grant
consent themselves, so without this every sign-in would stop at a consent
screen nobody can pass. Both have "Allow public client flows" off: MSAL's
auth-code + PKCE flow does not need it, and leaving it on would allow password
flows that skip the hosted sign-in page.

The sign-up/sign-in user flow needs ``EventListener.ReadWrite.All``, which the
Azure CLI's token does not carry. The script tries anyway and, on 403, prints
what to set up in the portal instead. (On this tenant, a Global Administrator's
Directory.AccessAsUser.All was enough.)

With ``--apple-key``, it also registers Sign in with Apple as an identity
provider (or updates it, which is the 6-month renewal Apple requires: run it
again with the same or a new key) and adds Apple to the user flow. The key file
is read here and sent straight to Graph, so its contents never appear on screen
or in a transcript.

Prints ids only, never tokens or keys. Usage:

    python3 scripts/entra_setup.py --tenant <tenant-id>
    python3 scripts/entra_setup.py --tenant <tenant-id> --apple-key ~/Downloads/AuthKey_<KEYID>.p8
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

GRAPH = "https://graph.microsoft.com/v1.0"
MS_GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
# Delegated Microsoft Graph permission ids (stable across tenants).
OPENID = "37f7f235-527c-4136-accd-4a02d197296e"
OFFLINE_ACCESS = "7427e0e9-2fba-42fe-b0c0-848c9e6a8182"

IOS_REDIRECT = "msauth.com.vonmentlen.mynews://auth"
CLI_REDIRECT = "http://localhost"
USER_FLOW_NAME = "MyNews sign-up and sign-in"


class Graph:
    def __init__(self, tenant: str) -> None:
        self.token = subprocess.run(
            ["az", "account", "get-access-token", "--tenant", tenant,
             "--resource-type", "ms-graph", "--query", "accessToken", "-o", "tsv"],
            check=True, capture_output=True, text=True,
            env={**os.environ, "AZURE_EXTENSION_USE_DYNAMIC_INSTALL": "no"},
        ).stdout.strip()

    def call(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        # OData filters contain spaces and quotes; encode the query string only.
        base, _, query = path.partition("?")
        url = GRAPH + base + ("?" + urllib.parse.quote(query, safe="=&$'(),:") if query else "")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} -> {exc.code}: {detail[:600]}") from None

    def one(self, path: str) -> dict[str, Any] | None:
        values = self.call("GET", path).get("value", [])
        return values[0] if values else None


def ensure_app(g: Graph, name: str, body: dict[str, Any]) -> dict[str, Any]:
    app = g.one(f"/applications?$filter=displayName eq '{name}'")
    if app:
        print(f"  {name}: exists (appId {app['appId']})")
        return app
    app = g.call("POST", "/applications", {"displayName": name, **body})
    print(f"  {name}: created (appId {app['appId']})")
    return app


def ensure_sp(g: Graph, app_id: str) -> dict[str, Any]:
    sp = g.one(f"/servicePrincipals?$filter=appId eq '{app_id}'")
    return sp or g.call("POST", "/servicePrincipals", {"appId": app_id})


def ensure_grant(g: Graph, client_sp: str, resource_sp: str, scope: str) -> None:
    existing = g.call(
        "GET", f"/oauth2PermissionGrants?$filter=clientId eq '{client_sp}' and resourceId eq '{resource_sp}'"
    ).get("value", [])
    if existing:
        grant = existing[0]
        if set(scope.split()) - set(grant.get("scope", "").split()):
            g.call("PATCH", f"/oauth2PermissionGrants/{grant['id']}", {"scope": scope})
        return
    g.call("POST", "/oauth2PermissionGrants",
           {"clientId": client_sp, "consentType": "AllPrincipals", "resourceId": resource_sp, "scope": scope})


APPLE_IDP_TYPE = "#microsoft.graph.appleManagedIdentityProvider"


def ensure_apple(g: Graph, key_path: str, key_id: str, team_id: str, service_id: str) -> tuple[str, str]:
    """Create the Apple identity provider, or update it in place (the renewal)."""
    with open(key_path, encoding="ascii") as handle:
        pem = handle.read().strip()
    if "BEGIN PRIVATE KEY" not in pem:
        raise SystemExit(f"{key_path} is not a PEM private key (.p8)")
    body = {"developerId": team_id, "serviceId": service_id, "keyId": key_id, "certificateData": pem}
    existing = [p for p in g.call("GET", "/identity/identityProviders").get("value", [])
                if p.get("@odata.type") == APPLE_IDP_TYPE]
    if existing:
        g.call("PATCH", f"/identity/identityProviders/{existing[0]['id']}", {"@odata.type": APPLE_IDP_TYPE, **body})
        return existing[0]["id"], "updated (renewed)"
    created = g.call("POST", "/identity/identityProviders",
                     {"@odata.type": APPLE_IDP_TYPE, "displayName": "Apple", **body})
    return created["id"], "created"


def ensure_flow_idp(g: Graph, flow_id: str, idp_id: str) -> str:
    base = (f"/identity/authenticationEventsFlows/{flow_id}"
            "/microsoft.graph.externalUsersSelfServiceSignUpEventsFlow/onAuthenticationMethodLoadStart"
            "/microsoft.graph.onAuthenticationMethodLoadStartExternalUsersSelfServiceSignUp/identityProviders")
    present = [p.get("id") for p in g.call("GET", base).get("value", [])]
    if idp_id in present:
        return "already in the user flow"
    g.call("POST", base + "/$ref", {"@odata.id": f"{GRAPH}/identity/identityProviders/{idp_id}"})
    return "added to the user flow"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--apple-key", help="Path to the Sign in with Apple key, AuthKey_<KEYID>.p8")
    parser.add_argument("--apple-key-id", help="Defaults to the <KEYID> in the file name")
    parser.add_argument("--apple-team-id", default="99J753GKC3")
    parser.add_argument("--apple-service-id", default="com.vonmentlen.mynews.entra")
    args = parser.parse_args()
    g = Graph(args.tenant)

    print("app registrations:")
    api = ensure_app(g, "mynews-api", {
        "signInAudience": "AzureADMyOrg",
        "api": {
            "requestedAccessTokenVersion": 2,
            "oauth2PermissionScopes": [{
                "id": str(uuid.uuid4()),
                "value": "access_as_user",
                "type": "User",
                "isEnabled": True,
                "adminConsentDisplayName": "Use MyNews as the signed-in user",
                "adminConsentDescription": "Lets the MyNews apps call the MyNews API on behalf of the signed-in user.",
                "userConsentDisplayName": "Use MyNews",
                "userConsentDescription": "Lets the MyNews app read your briefs and save your settings.",
            }],
        },
        "appRoles": [{
            "id": str(uuid.uuid4()),
            "value": "admin",
            "displayName": "Admin",
            "description": "Operates MyNews: invites, source catalog, suggestions, deletions.",
            "allowedMemberTypes": ["User"],
            "isEnabled": True,
        }],
    })
    # The App ID URI needs the appId, so it can only be set after creation.
    if f"api://{api['appId']}" not in (api.get("identifierUris") or []):
        g.call("PATCH", f"/applications/{api['id']}", {"identifierUris": [f"api://{api['appId']}"]})
    api = g.call("GET", f"/applications/{api['id']}")
    scope_id = api["api"]["oauth2PermissionScopes"][0]["id"]
    admin_role_id = next(r["id"] for r in api["appRoles"] if r["value"] == "admin")

    client_access = [
        {"resourceAppId": api["appId"], "resourceAccess": [{"id": scope_id, "type": "Scope"}]},
        {"resourceAppId": MS_GRAPH_APP_ID, "resourceAccess": [
            {"id": OPENID, "type": "Scope"}, {"id": OFFLINE_ACCESS, "type": "Scope"}]},
    ]
    ios = ensure_app(g, "mynews-ios", {
        "signInAudience": "AzureADMyOrg",
        "isFallbackPublicClient": False,
        "publicClient": {"redirectUris": [IOS_REDIRECT]},
        "requiredResourceAccess": client_access,
    })
    cli = ensure_app(g, "mynews-cli", {
        "signInAudience": "AzureADMyOrg",
        "isFallbackPublicClient": False,
        "publicClient": {"redirectUris": [CLI_REDIRECT]},
        "requiredResourceAccess": client_access,
    })

    print("service principals and admin consent:")
    api_sp = ensure_sp(g, api["appId"])
    graph_sp = ensure_sp(g, MS_GRAPH_APP_ID)
    for app in (ios, cli):
        sp = ensure_sp(g, app["appId"])
        ensure_grant(g, sp["id"], api_sp["id"], "access_as_user")
        ensure_grant(g, sp["id"], graph_sp["id"], "openid offline_access")
        print(f"  {app['displayName']}: consent granted for access_as_user, openid, offline_access")

    print("user flow:")
    try:
        flows = g.call("GET", "/identity/authenticationEventsFlows").get("value", [])
        listing_error = ""
    except RuntimeError as exc:
        # Listing needs EventListener.Read.All, which the CLI token may lack too.
        flows, listing_error = [], f" (could not list: {str(exc)[:120]})"
    flow = next((f for f in flows if f.get("displayName") == USER_FLOW_NAME), None)
    flow_status = "exists" if flow else "missing" + listing_error
    if flow is None:
        try:
            flow = g.call("POST", "/identity/authenticationEventsFlows", {
                "@odata.type": "#microsoft.graph.externalUsersSelfServiceSignUpEventsFlow",
                "displayName": USER_FLOW_NAME,
                "conditions": {"applications": {"includeApplications": [
                    {"appId": ios["appId"]}, {"appId": cli["appId"]}]}},
                "onAuthenticationMethodLoadStart": {
                    "@odata.type": "#microsoft.graph.onAuthenticationMethodLoadStartExternalUsersSelfServiceSignUp",
                    "identityProviders": [{"id": "EmailPassword-OAUTH"}],
                },
                "onInteractiveAuthFlowStart": {
                    "@odata.type": "#microsoft.graph.onInteractiveAuthFlowStartExternalUsersSelfServiceSignUp",
                    "isSignUpAllowed": True,
                },
                "onAttributeCollection": {
                    "@odata.type": "#microsoft.graph.onAttributeCollectionExternalUsersSelfServiceSignUp",
                    "attributes": [
                        {"id": "email", "displayName": "Email Address", "description": "Email address of the user",
                         "userFlowAttributeType": "builtIn", "dataType": "string"},
                        {"id": "displayName", "displayName": "Display Name", "description": "Display name of the user",
                         "userFlowAttributeType": "builtIn", "dataType": "string"},
                    ],
                    "attributeCollectionPage": {"views": [{"inputs": [
                        {"attribute": "email", "label": "Email Address", "inputType": "text", "hidden": True,
                         "editable": False, "writeToDirectory": True, "required": True,
                         "validationRegEx": "^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9-]+(?:\\.[a-zA-Z0-9-]+)*$"},
                        # Not the docs' example pattern, which rejects umlauts and hyphens:
                        # "Müller" or "Anne-Marie" could not sign up with it.
                        {"attribute": "displayName", "label": "Display Name", "inputType": "text", "hidden": False,
                         "editable": True, "writeToDirectory": True, "required": False,
                         "validationRegEx": "^.{1,64}$"},
                    ]}]},
                },
            })
            flow_status = "created"
        except RuntimeError as exc:
            flow_status = f"NOT created ({str(exc)[:160]})"
    print(f"  {USER_FLOW_NAME}: {flow_status}")

    apple_idp = ""
    if args.apple_key:
        key_id = args.apple_key_id or os.path.basename(args.apple_key).removeprefix("AuthKey_").removesuffix(".p8")
        print("sign in with apple:")
        try:
            apple_idp, status = ensure_apple(g, args.apple_key, key_id, args.apple_team_id, args.apple_service_id)
        except RuntimeError as exc:
            if "-> 403" not in str(exc):
                raise
            # identityProviders demands IdentityProvider.ReadWrite.All, which the
            # Azure CLI's token lacks (Directory.AccessAsUser.All is not enough
            # here, unlike for the user flow). Consenting another Microsoft app
            # just for this would leave a standing broad permission behind, so
            # the portal form is the supported path, at creation and renewal.
            print("  not possible with the Azure CLI token (needs IdentityProvider.ReadWrite.All). In the portal:")
            print("  Entra ID > External Identities > All identity providers > Built-in > Apple:")
            print(f"    Client (Apple service) ID {args.apple_service_id} | team ID {args.apple_team_id} | key ID {key_id}")
            print(f"    Client secret (.p8) key: paste the file (pbcopy < {args.apple_key}), then clear the clipboard")
            print(f"  then User flows > {USER_FLOW_NAME} > Identity providers > tick Apple > Save.")
        else:
            print(f"  identity provider {apple_idp}: {status} (service {args.apple_service_id}, key {key_id})")
            if flow:
                print(f"  {ensure_flow_idp(g, flow['id'], apple_idp)}")
        print("  Apple requires renewing this every 6 months (same or a new key).")

    print(json.dumps({
        "ENTRA_API_CLIENT_ID": api["appId"],
        "MYNEWS_IOS_CLIENT_ID": ios["appId"],
        "MYNEWS_CLI_CLIENT_ID": cli["appId"],
        "ENTRA_ALLOWED_CLIENT_IDS": f"{ios['appId']},{cli['appId']}",
        "ADMIN_APP_ROLE_ID": admin_role_id,
        "API_SERVICE_PRINCIPAL_ID": api_sp["id"],
        "USER_FLOW_ID": (flow or {}).get("id", ""),
        "APPLE_IDENTITY_PROVIDER_ID": apple_idp,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
