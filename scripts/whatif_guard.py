#!/usr/bin/env python3
"""Stop an `azd provision` whose what-if would cost money or lose data.

    whatif_guard.py <what-if.json>
    az deployment sub what-if ... --no-pretty-print -o json | whatif_guard.py

Reads the JSON that `az deployment sub what-if --no-pretty-print -o json`
prints, summarises what the provision would change, and exits 1 when one of the
rules below blocks. scripts/whatif.sh runs it as azd's preprovision hook, so a
block stops the provision before ARM touches anything.

To accept a blocked change on purpose, name its rule (comma-separate several):

    WHATIF_ALLOW=cosmos-backup azd provision

An allowed rule still prints, as "allowed", so the log keeps what was waved
through and what it would otherwise have stopped.

Exit status: 0 nothing blocks; 1 a rule blocks; 2 the input can't be judged
(not what-if JSON, or the what-if itself failed). 2 stops the hook too: a
guard that can't read its input must not pass it.

Why a guard at all: azd provisions whatever the Bicep says, and this project's
money and data each hang on a line or two of it (README, "Things that will cost
you money if changed"). The float16 outage added the other half: ARM accepts
what the data plane later refuses, so nothing at deploy time warns you, and the
README's answer was to gate every provision with what-if. Reading a what-if by
eye is the step that gets skipped. This reads it every time.

It runs under whatever `python3` the machine has, which on macOS is the system
3.9: standard library only, and nothing newer than 3.9 (no `match`, no `X | Y`
outside annotations). backend/tests/test_whatif_guard.py covers every rule.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from typing import Any, Callable, Iterator, NamedTuple

# Resource-level change types, in the order `az` prints them. Only Create,
# Modify and Deploy write. NoChange and Ignore leave the resource as it is, and
# Unsupported means what-if could not tell: reported, never judged. A type this
# list doesn't know counts as a write, because a new kind of change is more
# likely to be one than not.
CHANGE_TYPES = ("Delete", "Create", "Deploy", "Modify", "NoChange", "Unsupported", "Ignore")
WRITES = ("Create", "Modify", "Deploy")
_CANONICAL = {t.lower(): t for t in CHANGE_TYPES}
SYMBOLS = {"Delete": "-", "Create": "+", "Deploy": "!", "Modify": "~",
           "NoChange": "=", "Unsupported": "x", "Ignore": "*"}
PROPERTY_SYMBOLS = {"create": "+", "delete": "-", "modify": "~", "array": "~"}

# Lower-cased resource types, as read off the resource id.
COSMOS_ACCOUNT = "microsoft.documentdb/databaseaccounts"
COSMOS_DATABASE = COSMOS_ACCOUNT + "/sqldatabases"
COSMOS_CONTAINER = COSMOS_DATABASE + "/containers"
COSMOS_THROUGHPUT = (COSMOS_DATABASE + "/throughputsettings", COSMOS_CONTAINER + "/throughputsettings")
FOUNDRY_DEPLOYMENT = "microsoft.cognitiveservices/accounts/deployments"
FUNCTION_APP = "microsoft.web/sites"
FUNCTION_PLAN = "microsoft.web/serverfarms"

FREE_TIER = "properties.enableFreeTier"
LOCATIONS = "properties.locations"
CAPABILITIES = "properties.capabilities"
BACKUP = "properties.backupPolicy"
PARTITION_KEY = "properties.resource.partitionKey"
VECTOR_POLICY = "properties.resource.vectorEmbeddingPolicy"
ALWAYS_READY = "properties.functionAppConfig.scaleAndConcurrency.alwaysReady"

FREE_TIER_RU = 1000
FOUNDRY_SKUS = ("GlobalStandard", "DataZoneStandard", "Standard")
FLEX_PLAN_SKU = "FC1"

# How many changed paths a summary line shows before it says "+N more".
MAX_PATHS = 8


class InputError(Exception):
    """The input is not a what-if result this guard can judge (exit 2)."""


# --- Reading the what-if -----------------------------------------------------


class Leaf(NamedTuple):
    """One leaf of a resource's delta, with its full property path."""

    path: str
    kind: str  # what-if's propertyChangeType: Create, Delete, Modify, Array or NoEffect
    before: Any
    after: Any


def _join(parent: str, child: str, parent_is_array: bool) -> str:
    if not parent:
        return child
    if not child:
        return parent
    if parent_is_array or child.isdigit():
        return f"{parent}[{child}]"
    return f"{parent}.{child}"


def flatten(delta: Any, prefix: str = "", in_array: bool = False) -> list[Leaf]:
    """Every leaf of a what-if delta, with its full path.

    What-if nests its diff. An Array change carries one child per index it
    touches ("0", "1", ...), and an element that is itself an object carries
    children with paths relative to that element. So a zone-redundancy flip on
    the first Cosmos region arrives as properties.locations > 0 >
    isZoneRedundant, and reads here as properties.locations[0].isZoneRedundant.
    """
    leaves = []
    for entry in delta or ():
        if not isinstance(entry, dict):
            continue
        path = _join(prefix, str(entry.get("path") or ""), in_array)
        kind = str(entry.get("propertyChangeType") or "")
        children = entry.get("children")
        if children:
            leaves.extend(flatten(children, path, kind.lower() == "array"))
        else:
            leaves.append(Leaf(path, kind, entry.get("before"), entry.get("after")))
    return leaves


def _identify(raw: dict, before: dict | None, after: dict | None) -> tuple[str, str]:
    """(lower-cased resource type, display label) for one change.

    The label is the resource id from its last provider on, the way `az`
    prints it: Microsoft.DocumentDB/databaseAccounts/cosmos-…/sqlDatabases/mynews.
    """
    rid = raw.get("resourceId")
    if isinstance(rid, str) and rid:
        at = rid.lower().rfind("/providers/")
        if at >= 0:
            tail = rid[at + len("/providers/"):].strip("/")
            segments = tail.split("/")
            # Namespace, then alternating type/name: keep the types.
            return "/".join(segments[:1] + segments[1::2]).lower(), tail
        segments = rid.strip("/").split("/")
        if len(segments) >= 4 and segments[2].lower() == "resourcegroups":
            return "microsoft.resources/resourcegroups", "resourceGroups/" + segments[3]
        return rid.lower(), rid
    # Extensible (non-ARM) resources carry no resourceId; use what the payload says.
    state = after or before or {}
    rtype = str(state.get("type") or "unknown")
    return rtype.lower(), f"{rtype}/{state.get('name') or raw.get('symbolicName') or '?'}"


class Change:
    """One entry of `changes` or `potentialChanges`, normalised for the rules."""

    def __init__(self, raw: dict, potential: bool = False) -> None:
        change_type = str(raw.get("changeType") or "")
        self.change_type = _CANONICAL.get(change_type.lower(), change_type or "?")
        self.potential = potential
        self.resource_id = str(raw.get("resourceId") or "")
        self.before = raw.get("before") if isinstance(raw.get("before"), dict) else None
        self.after = raw.get("after") if isinstance(raw.get("after"), dict) else None
        self.unsupported_reason = raw.get("unsupportedReason")
        delta = raw.get("delta")
        # has_delta: what-if gave a property-level verdict at all. leaves drop
        # NoEffect, what-if's own label for read-only noise that is never
        # sent, so it never trips a rule or reaches the summary.
        self.has_delta = bool(delta)
        self.leaves = [leaf for leaf in flatten(delta) if leaf.kind.lower() != "noeffect"]
        self.type, label = _identify(raw, self.before, self.after)
        # potentialChanges are what-if's "MAY OR MAY NOT be deployed". They are
        # judged like the rest, and say so wherever they are named.
        self.label = label + (" (potential)" if potential else "")

    @property
    def writes(self) -> bool:
        return self.change_type in WRITES or self.change_type not in CHANGE_TYPES

    @property
    def state(self) -> dict | None:
        """The resource as it will be once the provision has run, if anyone knows."""
        if self.change_type == "Delete":
            return None
        return self.after if self.after is not None else self.before


def load(document: Any) -> tuple[list[Change], list[dict]]:
    """(changes, diagnostics) from a parsed what-if result, or InputError."""
    if not isinstance(document, dict):
        raise InputError("expected a JSON object, as printed by "
                         "`az deployment sub what-if --no-pretty-print -o json`")
    status, error = document.get("status"), document.get("error")
    if error or (status is not None and str(status).lower() != "succeeded"):
        # `az` raises on a failed what-if before printing anything, so this
        # only fires for JSON from somewhere else. A failed what-if predicts
        # nothing, and nothing is not the same as safe.
        detail = error.get("message") if isinstance(error, dict) else error
        raise InputError(f"the what-if itself did not succeed (status {status}): {detail or 'no detail'}")
    if "changes" not in document:
        raise InputError("no 'changes': is this `az deployment sub what-if --no-pretty-print -o json` output?")
    changes = []
    for key, potential in (("changes", False), ("potentialChanges", True)):
        entries = document.get(key)
        if entries is None and potential:
            continue
        if not isinstance(entries, list):
            raise InputError(f"'{key}' is {show(entries)}, not a list")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                # An entry that can't be read might be the one that matters.
                raise InputError(f"{key}[{index}] is not an object")
            changes.append(Change(entry, potential))
    diagnostics = [d for d in document.get("diagnostics") or () if isinstance(d, dict)]
    return changes, diagnostics


# --- Small helpers -----------------------------------------------------------


def get(obj: Any, dotted: str) -> Any:
    """obj[a][b][c] for "a.b.c", matching keys case-insensitively, else None.

    ARM property names are case-insensitive, and a resource provider's casing
    in `before` need not match the template's in `after`.
    """
    for key in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        if key in obj:
            obj = obj[key]
            continue
        lowered = key.lower()
        obj = next((v for k, v in obj.items() if isinstance(k, str) and k.lower() == lowered), None)
    return obj


def _overlaps(path: str, watched: str) -> bool:
    """True when `path` is the watched property, inside it, or encloses it."""
    a, b = path.lower(), watched.lower()
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    return longer.startswith(shorter) and longer[len(shorter)] in ".["


def may_change(change: Change, path: str) -> bool:
    """What-if's own verdict: does anything at or under `path` change?

    Its delta has already had read-only noise taken out, so when it says
    nothing there moves, nothing does. A change with no delta (a Create, a
    Deploy) carries no verdict, so it counts as a maybe and the rule's
    before/after comparison decides. Rules ask both questions, because the
    delta also reports differences that are only formatting: the Cosmos API
    returns "Switzerland North" where the template says "switzerlandnorth".
    """
    if not change.has_delta:
        return True
    return any(_overlaps(leaf.path, path) for leaf in change.leaves)


def touched_paths(change: Change, path: str) -> list[str]:
    return [leaf.path for leaf in change.leaves if _overlaps(leaf.path, path)] or [path]


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def show(value: Any, limit: int = 60) -> str:
    """A value for a one-line message: JSON-ish, and cut short."""
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _of(changes: list[Change], rtype: str, existing_only: bool = False) -> Iterator[Change]:
    """The writes to one resource type; existing_only drops Creates."""
    for change in changes:
        if change.type == rtype and change.writes and not (existing_only and change.change_type == "Create"):
            yield change


# --- Rules -------------------------------------------------------------------
#
# Each rule returns one line per offending resource, and nothing when it has
# nothing to say. `why` prints once above a rule's findings; the comment in
# each rule carries the longer argument. The ids are the operator's interface
# (WHATIF_ALLOW=<id>), so they don't change once published.


class Rule(NamedTuple):
    id: str
    why: str
    watches: tuple  # resource types it reads; empty means every type
    check: Callable[[list, Callable[[str], None]], list]


RULES: list[Rule] = []


def rule(rule_id: str, why: str, *watches: str) -> Callable:
    def register(check: Callable) -> Callable:
        RULES.append(Rule(rule_id, why, tuple(watches), check))
        return check
    return register


@rule("delete", "provisions here are incremental and never delete, so a Delete means the "
                "deployment mode changed; deleted data does not come back")
def _deletes(changes: list[Change], warn: Callable[[str], None]) -> list[str]:
    # azd provisions in incremental mode, which never deletes what the Bicep
    # stops mentioning: that is why Phase 0 deletes the old model deployments
    # and queues by hand. So a resource-level Delete here means the mode
    # changed under us (Complete mode, or azd's deployment stacks), and this
    # provision would remove whatever the template no longer names. The
    # Cosmos account is the copy of record for everything the app holds, and
    # continuous backup can restore a dropped one within 7 days, but a restore
    # is a recovery operation of its own, not an undo.
    return [f"{c.label} would be deleted" for c in changes if c.change_type == "Delete"]


@rule("cosmos-free-tier", "the free tier (1,000 RU/s + 25 GB) is fixed when an account is created, "
                          "one account per subscription", COSMOS_ACCOUNT)
def _free_tier(changes: list[Change], warn: Callable[[str], None]) -> list[str]:
    # enableFreeTier can only be set at account creation, and only one account
    # per subscription gets it. A Modify that touches it fails at deploy time,
    # after earlier resources in the same provision have already changed.
    #
    # A Create matters more. A NEW Cosmos account on an environment that has
    # one means the resource names moved: they hash the subscription, the env
    # name and the location (main.bicep's resourceToken), so a slip in any of
    # them provisions a second stack beside the first. Its account either asks
    # for the free tier the first one holds, and fails half-way through, or
    # goes without and bills its RU/s at list price. A genuinely new
    # environment trips this once, on purpose.
    found = []
    for c in _of(changes, COSMOS_ACCOUNT):
        if c.change_type == "Create":
            found.append(f"{c.label}: a NEW Cosmos account (enableFreeTier {show(get(c.after, FREE_TIER))}). "
                         "If this environment already exists, the subscription, AZURE_ENV_NAME or "
                         "AZURE_LOCATION is not the one it was provisioned with")
            continue
        before, after = get(c.before, FREE_TIER), get(c.after, FREE_TIER)
        if may_change(c, FREE_TIER) and before != after:
            found.append(f"{c.label}: enableFreeTier {show(before)} -> {show(after)}")
    return found


def _regions(state: dict | None) -> tuple:
    """The account's regions as (failover priority, name, zone-redundant), in order."""
    regions = []
    for entry in get(state, LOCATIONS) or ():
        if isinstance(entry, dict):
            name = "".join(str(get(entry, "locationName") or "?").split()).lower()
            regions.append((_number(get(entry, "failoverPriority")), name, bool(get(entry, "isZoneRedundant"))))
    regions.sort(key=lambda r: (r[0] is None, r[0] or 0, r[1]))
    return tuple(regions)


def _regions_text(regions: tuple) -> str:
    return ", ".join(name + (" (zone-redundant)" if zones else "") for _, name, zones in regions) or "(none)"


@rule("cosmos-locations", "every region bills the provisioned RU/s again, and the free grant does not "
                          "grow with it", COSMOS_ACCOUNT)
def _locations(changes: list[Change], warn: Callable[[str], None]) -> list[str]:
    # RU/s are provisioned in every region of the account, so a second region
    # doubles the throughput being billed while the free grant stays at
    # 1,000 RU/s. Removing one drops the replica in it, moving the write region
    # (failover priority 0) is a failover, and zone redundancy can't be flipped
    # on a region that exists. Compared by region, priority and zones, so the
    # API's "Switzerland North" against the template's "switzerlandnorth" is
    # not a change.
    found = []
    for c in _of(changes, COSMOS_ACCOUNT, existing_only=True):
        before, after = _regions(c.before), _regions(c.after)
        if may_change(c, LOCATIONS) and before != after:
            paths = ", ".join(touched_paths(c, LOCATIONS)[:MAX_PATHS])
            found.append(f"{c.label}: regions {_regions_text(before)} -> {_regions_text(after)} ({paths})")
    return found


def _capabilities(state: dict | None) -> dict[str, str]:
    names = {}
    for entry in get(state, CAPABILITIES) or ():
        name = get(entry, "name") if isinstance(entry, dict) else None
        if name:
            names[str(name).lower()] = str(name)
    return names


@rule("cosmos-capabilities-removed", "Cosmos capabilities are effectively one-way: removing one fails "
                                     "the deploy half-way or changes what the account is", COSMOS_ACCOUNT)
def _capabilities_removed(changes: list[Change], warn: Callable[[str], None]) -> list[str]:
    # data.bicep keeps EnableNoSQLVectorSearch although nothing uses it,
    # because capabilities are effectively additive: ARM may reject a removal
    # after other resources in the same provision have already changed, and
    # some (EnableServerless) decide what kind of account it is. The float16
    # outage is the reminder that ARM accepting a change says little about
    # what the data plane does with it later. Adding one is not judged here.
    found = []
    for c in _of(changes, COSMOS_ACCOUNT, existing_only=True):
        before, after = _capabilities(c.before), _capabilities(c.after)
        removed = sorted(name for key, name in before.items() if key not in after)
        if removed and may_change(c, CAPABILITIES):
            found.append(f"{c.label}: removes {', '.join(removed)}")
    return found


def _backup(state: dict | None) -> tuple:
    """The settings that decide what a backup policy keeps and costs."""
    policy = get(state, BACKUP)
    if not isinstance(policy, dict):
        return ("none",) if policy is None else ("?", show(policy))
    kind = str(get(policy, "type") or "?").lower()
    if kind == "continuous":
        return (kind, str(get(policy, "continuousModeProperties.tier") or "no tier").lower())
    return (kind,
            _number(get(policy, "periodicModeProperties.backupIntervalInMinutes")),
            _number(get(policy, "periodicModeProperties.backupRetentionIntervalInHours")),
            str(get(policy, "periodicModeProperties.backupStorageRedundancy") or "?").lower())


def _backup_text(state: dict | None) -> str:
    policy = get(state, BACKUP)
    if not isinstance(policy, dict):
        return show(policy)
    kind = str(get(policy, "type") or "?")
    if kind.lower() == "continuous":
        return f"Continuous ({get(policy, 'continuousModeProperties.tier') or 'no tier'})"
    periodic = get(policy, "periodicModeProperties")
    return (f"{kind} (every {show(get(periodic, 'backupIntervalInMinutes'))} min, "
            f"kept {show(get(periodic, 'backupRetentionIntervalInHours'))} h, "
            f"{show(get(periodic, 'backupStorageRedundancy'))})")


@rule("cosmos-backup", "Periodic -> Continuous is one-way, and only the 7-day continuous tier is free",
      COSMOS_ACCOUNT)
def _backup_changes(changes: list[Change], warn: Callable[[str], None]) -> list[str]:
    # The backup is the only way back from a bad migration or a buggy purge,
    # so it changes only when someone means it. Periodic -> Continuous is
    # one-way: Azure has no switch back. Continuous7Days costs nothing; the
    # 30-day tier bills per GB per region, and periodic copies beyond the two
    # free ones bill too. Phase 0's own switch to Continuous7Days trips this
    # once, which is the point: WHATIF_ALLOW=cosmos-backup azd provision.
    found = []
    for c in _of(changes, COSMOS_ACCOUNT, existing_only=True):
        if may_change(c, BACKUP) and _backup(c.before) != _backup(c.after):
            found.append(f"{c.label}: {_backup_text(c.before)} -> {_backup_text(c.after)}")
    return found


def _partition_key(state: dict | None) -> tuple:
    key = get(state, PARTITION_KEY)
    if not isinstance(key, dict):
        return (show(key),)
    paths = tuple(str(p) for p in get(key, "paths") or ())
    return (paths, str(get(key, "kind") or "?").lower(), _number(get(key, "version")))


def _partition_key_text(state: dict | None) -> str:
    key = _partition_key(state)
    if len(key) == 1:
        return key[0]
    return f"{','.join(key[0]) or '?'} ({key[1]}, v{show(key[2])})"


@rule("cosmos-partition-key", "a container's partition key is immutable; changing it means a new "
                              "container and a migration", COSMOS_CONTAINER)
def _partition_key_changes(changes: list[Change], warn: Callable[[str], None]) -> list[str]:
    # The partition key decides where every document lives, and Cosmos can't
    # change it in place: ARM rejects the update, after whatever came before
    # it in the same provision has already been applied. The real path is a
    # new container and a copy of every document, which Phase 1 does with
    # `news`. That one is a Create, which this rule leaves alone.
    found = []
    for c in _of(changes, COSMOS_CONTAINER, existing_only=True):
        if may_change(c, PARTITION_KEY) and _partition_key(c.before) != _partition_key(c.after):
            found.append(f"{c.label}: partition key {_partition_key_text(c.before)} -> "
                         f"{_partition_key_text(c.after)}")
    return found


def _has_content(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_content(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_content(v) for v in value)
    return value is not None and value != ""


def _vectors_text(policy: Any) -> str:
    parts = [f"{get(e, 'path')} {get(e, 'dataType')} x{get(e, 'dimensions')}"
             for e in get(policy, "vectorEmbeddings") or () if isinstance(e, dict)]
    return ", ".join(parts) or show(policy)


@rule("cosmos-vector-policy", "a float16 vector policy that ARM accepted took every query down on "
                              "2026-08-02; nothing here runs VectorDistance", COSMOS_CONTAINER)
def _vector_policy(changes: list[Change], warn: Callable[[str], None]) -> list[str]:
    # README, "The float16 outage": ARM accepted dataType float16, Microsoft
    # documents it, and five days later the data plane could no longer read
    # the policy it had stored. Every query, change-feed and ReadFeed call on
    # the container failed for two days while point reads kept the pipeline
    # half-alive. The policy was never used: all similarity is NumPy over
    # vectors read out of Cosmos. So a policy appearing, on a new container or
    # an old one, is the change that has cost an outage once already; it gets
    # through only as WHATIF_ALLOW=cosmos-vector-policy, with a reason.
    found = []
    for c in _of(changes, COSMOS_CONTAINER):
        # A Create has no delta and no before, so it always counts.
        after = get(c.after, VECTOR_POLICY)
        if _has_content(after) and may_change(c, VECTOR_POLICY) and after != get(c.before, VECTOR_POLICY):
            found.append(f"{c.label}: vectorEmbeddingPolicy {_vectors_text(after)}")
    return found


def _throughput(change: Change) -> tuple[Any, str] | None:
    """(RU/s, how) a database, container or throughputSettings provisions, or None.

    Autoscale counts at its maximum: it bills the highest RU/s it reaches each
    hour, and nothing here can know tomorrow's peak.
    """
    state = change.state
    base = "properties.resource" if change.type in COSMOS_THROUGHPUT else "properties.options"
    manual = get(state, base + ".throughput")
    if manual is not None:
        return manual, "manual"
    autoscale = get(state, base + ".autoscaleSettings.maxThroughput")
    if autoscale is not None:
        return autoscale, "autoscale max"
    return None


def _account_of(resource_id: str) -> str:
    marker = "/providers/microsoft.documentdb/databaseaccounts/"
    lowered = resource_id.lower()
    at = lowered.find(marker)
    if at < 0:
        return resource_id
    end = lowered.find("/", at + len(marker))
    return resource_id if end < 0 else resource_id[:end]


@rule("cosmos-throughput", "the free tier covers 1,000 RU/s per account; a second 400 RU/s container "
                           "costs $23.36/month", COSMOS_DATABASE, COSMOS_CONTAINER, *COSMOS_THROUGHPUT)
def _throughput_total(changes: list[Change], warn: Callable[[str], None]) -> list[str]:
    # The free grant is a hard budget of 1,000 RU/s for the account,
    # database-level (shared) throughput included, and every RU/s above it
    # bills around the clock. So this sums what every database and container
    # will provision once the provision has run, the unchanged ones too (which
    # is why whatif.sh never excludes NoChange), not just what this provision
    # touches. Phase 1 runs `news` at 600 beside `items` at 400; Phase 5 raises
    # `news` to 1,000 only after `items` is gone. A throughputSettings resource
    # overrides its owner's options, so it wins here as well.
    sized = (COSMOS_DATABASE, COSMOS_CONTAINER)
    options: dict[str, tuple] = {}
    settings: dict[str, tuple] = {}
    owners: dict[str, str] = {}
    for c in changes:
        if c.change_type == "Delete" or c.type not in sized + COSMOS_THROUGHPUT:
            continue
        owner = c.resource_id
        if c.type in COSMOS_THROUGHPUT:
            owner = owner[:max(owner.lower().rfind("/throughputsettings/"), 0)] or owner
        owners.setdefault(owner.lower(), owner)
        provisioned = _throughput(c)
        if provisioned is not None:
            (settings if c.type in COSMOS_THROUGHPUT else options)[owner.lower()] = provisioned
        elif c.state is None and c.type in sized:
            warn(f"cosmos-throughput: {c.label} is {c.change_type} and its throughput is not "
                 "visible, so it is not counted")

    found = []
    totals: dict[str, tuple[int, list]] = {}
    for key, owner in owners.items():
        provisioned = settings.get(key) or options.get(key)
        if provisioned is None:
            continue
        value, how = provisioned
        ru, name = _number(value), owner.rsplit("/", 1)[-1]
        if ru is None:
            found.append(f"{name}: throughput {show(value)} is not a number, so the total can't be "
                         f"shown to stay <= {FREE_TIER_RU}")
            continue
        total, parts = totals.get(_account_of(owner), (0, []))
        parts.append(f"{name} {ru:g}" + ("" if how == "manual" else f" ({how})"))
        totals[_account_of(owner)] = (total + ru, parts)
    for account, (total, parts) in sorted(totals.items()):
        if total > FREE_TIER_RU:
            found.append(f"{account.rsplit('/', 1)[-1]}: {total:g} RU/s in total ({', '.join(parts)}), "
                         f"over the {FREE_TIER_RU} the free tier covers")
    return found


@rule("foundry-sku", "provisioned Foundry SKUs bill per hour, used or not: ~$1,460/month per PTU, "
                     "~15 PTU minimum", FOUNDRY_DEPLOYMENT)
def _foundry_sku(changes: list[Change], warn: Callable[[str], None]) -> list[str]:
    # GlobalStandard, DataZoneStandard and Standard bill per token and nothing
    # at rest. ProvisionedManaged and its Global/DataZone variants bill per
    # PTU-hour whether or not a token is sent (ai.bicep). Phase 2 brings back
    # a Data Zone Standard classifier and summarizer and a regional Standard
    # embedding deployment; any other SKU misses the allowlist and blocks.
    # This reads the SKU only: a fine-tuned model's hosting fee ($1.70/hour,
    # idle or not) rides on an ordinary SKU and is not caught here.
    allowed = {sku.lower() for sku in FOUNDRY_SKUS}
    found = []
    for c in _of(changes, FOUNDRY_DEPLOYMENT):
        sku = get(c.after, "sku.name")
        if not isinstance(sku, str) or sku.lower() not in allowed:
            found.append(f"{c.label}: sku {show(sku)} (allowed: {', '.join(FOUNDRY_SKUS)})")
    return found


def _ready_instances(entries: Any) -> int:
    """How many always-ready instances an alwaysReady value asks for.

    An entry whose count can't be read counts as one; an entry of 0 asks for
    nothing and costs nothing.
    """
    if not isinstance(entries, list):
        return 1 if _has_content(entries) else 0
    total = 0
    for entry in entries:
        count = _number(get(entry, "instanceCount")) if isinstance(entry, dict) else None
        total += 1 if count is None else max(int(count), 0)
    return total


@rule("always-ready", "always-ready instances have no free grant: $5.26/month each at 512 MB, used or "
                      "not", FUNCTION_APP)
def _always_ready(changes: list[Change], warn: Callable[[str], None]) -> list[str]:
    # Flex Consumption bills per execution until an always-ready instance
    # exists, which bills around the clock with no free grant (README:
    # $5.26/month at 512 MB, $21.02 at 2,048 MB). The plan buys one only if a
    # cold brief's measured p95 stays above 3 s (Architecture, cold starts),
    # so the day that happens it is a deliberate WHATIF_ALLOW=always-ready.
    # More instances on an existing list block too: that is more money.
    found = []
    for c in _of(changes, FUNCTION_APP):
        after = get(c.after, ALWAYS_READY)
        instances = _ready_instances(after)
        if instances and may_change(c, ALWAYS_READY) and after != get(c.before, ALWAYS_READY):
            found.append(f"{c.label}: {instances} always-ready instance(s) {show(after)}")
    return found


@rule("plan-sku", "FC1 bills per execution; every other Functions plan bills by the hour, used or not",
      FUNCTION_PLAN)
def _plan_sku(changes: list[Change], warn: Callable[[str], None]) -> list[str]:
    # FC1 is Flex Consumption: no execution, no bill. Every other plan bills by
    # the hour whether or not anything runs: Elastic Premium EP1 is about
    # $150/month with one instance always on, and a dedicated plan is the same
    # story at its own price. Flex is also what functions.bicep is written for
    # (functionAppConfig, blob deployment), so a plan swap is never just a
    # price change.
    found = []
    for c in _of(changes, FUNCTION_PLAN):
        sku = get(c.after, "sku.name")
        if not isinstance(sku, str) or sku.upper() != FLEX_PLAN_SKU:
            found.append(f"{c.label}: sku {show(sku)}, not {FLEX_PLAN_SKU} (Flex Consumption)")
    return found


RULE_IDS = tuple(r.id for r in RULES)


# --- Verdict -----------------------------------------------------------------


def parse_allow(raw: str | None) -> frozenset:
    """Rule ids from a WHATIF_ALLOW value: comma-separated, any case or spacing."""
    return frozenset(part.strip().lower() for part in (raw or "").split(",") if part.strip())


class Report:
    def __init__(self, changes: list[Change], diagnostics: list[dict], allow: frozenset) -> None:
        self.changes = changes
        self.diagnostics = diagnostics
        self.allow = frozenset(allow)
        self.warnings: list[str] = []
        self.findings: dict[str, list[str]] = {}
        for r in RULES:
            found = r.check(changes, self.warnings.append)
            if found:
                self.findings[r.id] = found
        fired = [r.id for r in RULES if r.id in self.findings]
        self.allowed = [rid for rid in fired if rid in self.allow]
        self.blocked = [rid for rid in fired if rid not in self.allow]
        self.unknown_allow = sorted(self.allow - set(RULE_IDS))
        self.unused_allow = sorted((self.allow & set(RULE_IDS)) - set(fired))

    @property
    def exit_code(self) -> int:
        return 1 if self.blocked else 0

    def not_checked(self) -> list[str]:
        """Rules whose resources what-if could only report as Unsupported.

        Typically a module what-if short-circuited because one of its
        parameters is another module's output, which it can't know before the
        deployment runs. The rule then saw nothing, which is not the same as
        nothing being wrong.
        """
        judged = {c.type for c in self.changes if c.change_type != "Unsupported"}
        unsupported = {c.type for c in self.changes if c.change_type == "Unsupported"}
        return [r.id for r in RULES
                if r.watches and not judged.intersection(r.watches) and unsupported.intersection(r.watches)]

    def render(self) -> str:
        counts = Counter(c.change_type for c in self.changes)
        order = list(CHANGE_TYPES) + sorted(t for t in counts if t not in CHANGE_TYPES)
        tally = ", ".join(f"{counts[t]} {t}" for t in order if counts[t])
        lines = [f"what-if: {len(self.changes)} resource(s)" + (f": {tally}" if tally else "")]

        for c in self.changes:
            if c.change_type in ("NoChange", "Ignore", "Unsupported"):
                continue
            paths = [f"{PROPERTY_SYMBOLS.get(leaf.kind.lower(), '?')}{leaf.path}{_named(leaf)}"
                     for leaf in c.leaves]
            if len(paths) > MAX_PATHS:
                paths = paths[:MAX_PATHS] + [f"+{len(paths) - MAX_PATHS} more"]
            detail = f": {', '.join(paths)}" if paths else ""
            lines.append(f"  {SYMBOLS.get(c.change_type, '?')} {c.change_type:<6} {c.label}{detail}")

        for c in self.changes:
            if c.change_type == "Unsupported":
                lines.append(f"  x Unsupported {c.label}: {c.unsupported_reason or 'no reason given'}")
        for d in self.diagnostics:
            lines.append(f"  ! {d.get('level') or 'Info'} {d.get('code') or '?'} at {d.get('target') or '?'}: "
                         f"{d.get('message') or ''}")
        not_checked = self.not_checked()
        if not_checked:
            lines.append(f"  not checked, their resources were Unsupported: {', '.join(not_checked)}")
        lines.extend("  " + w for w in self.warnings)

        for label, ids in (("allowed", self.allowed), ("BLOCKED", self.blocked)):
            for rid in ids:
                lines.append(f"{label} {rid}: {next(r.why for r in RULES if r.id == rid)}")
                lines.extend("  - " + message for message in self.findings[rid])
        if self.unknown_allow:
            lines.append(f"WHATIF_ALLOW names no rule called {', '.join(self.unknown_allow)} "
                         f"(rules: {', '.join(RULE_IDS)})")
        if self.unused_allow:
            lines.append(f"WHATIF_ALLOW lists {', '.join(self.unused_allow)}, but nothing here needed it")

        # The verdict goes last: azd shows a non-interactive hook's output in a
        # few-line preview, and the last lines are the ones left on screen.
        if self.blocked:
            accept = ",".join(rid for rid in RULE_IDS if rid in self.allowed or rid in self.blocked)
            lines.append(f"what-if guard: BLOCKED by {', '.join(self.blocked)}. "
                         f"If that is what you want: WHATIF_ALLOW={accept} azd provision")
        elif self.allowed:
            lines.append(f"what-if guard: pass, with {', '.join(self.allowed)} allowed on purpose")
        else:
            lines.append("what-if guard: pass")
        return "\n".join(lines)


def _named(leaf: Leaf) -> str:
    # Array elements like {name, value} (app settings) or {locationName, ...}
    # read better by name. Only the name, never the value: app settings carry
    # connection strings.
    for value in (leaf.before, leaf.after):
        if isinstance(value, dict):
            name = get(value, "name") or get(value, "locationName")
            if isinstance(name, str) and name:
                return f" ({name})"
    return ""


def evaluate(document: Any, allow: frozenset = frozenset()) -> Report:
    """Judge a parsed what-if result. Raises InputError if it can't be judged."""
    changes, diagnostics = load(document)
    return Report(changes, diagnostics, allow)


def _read(source: str) -> str:
    if source == "-":
        if sys.stdin is None or sys.stdin.isatty():
            raise InputError("no input: pass a what-if JSON file, or pipe one in")
        data = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read()
    else:
        with open(source, "rb") as handle:
            data = handle.read()
    # utf-8-sig: az on Windows can lead with a byte-order mark.
    return data.decode("utf-8-sig") if isinstance(data, bytes) else data


def main(argv: list[str] | None = None, environ: Any = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    environ = os.environ if environ is None else environ
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if len(args) > 1:
        print("usage:\n" + __doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    try:
        text = _read(args[0] if args else "-")
        try:
            document = json.loads(text)
        except ValueError as exc:
            raise InputError(f"not JSON ({exc})") from None
        report = evaluate(document, parse_allow(environ.get("WHATIF_ALLOW", "")))
    except (InputError, OSError, UnicodeDecodeError) as exc:
        print(f"whatif_guard: {exc}", file=sys.stderr)
        return 2
    print(report.render())
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
