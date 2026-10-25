"""The HTTP surface (docs/api.md): one blueprint per area, plus the response
helpers every blueprint and the access-level decorators share.

* ``public``  — GET /health, the one route without a credential.
* ``account`` — the caller's own account: redeem, /me, deletion, export.
* ``admin``   — the owner's admin routes (app role ``admin``).
* ``http``    — the envelope, JSON bodies, request ids, body limits and
  path/query parameters. Helpers only: no blueprint lives there.

Phase 3 adds ``content`` and ``me_state``. A route appears here only in the
phase that ships it; tests/test_route_inventory.py holds the registered set.
"""
