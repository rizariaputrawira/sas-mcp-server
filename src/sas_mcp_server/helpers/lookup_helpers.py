# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Lookup tables of SAS Intelligent Decisioning: key-value tables that rules consult.

A lookup table is a reference data *domain* (``/referenceData/domains``,
``domainType`` ``lookup``) — the same endpoint as value lists, told apart by
``type``. Its entries live in numbered *versions* (the API's "contents"). A rule
tests whether a variable's value is a key (condition type ``lookup``) or assigns
the value stored for a key (action type ``lookupValue``).

Behaviour this relies on, probed against SAS Viya 2026.09 in October 2026:

* **Activation publishes, and running decisions follow it.** Setting a
  version's status to ``production`` publishes its entries to every MAS
  destination (a DS2 package named by the domain's ``packageName``) and every
  CAS server (a format named by ``formatName``). A decision already published to
  MAS uses the newly activated entries on its next call — no republish. With
  ``asynchronous=true`` the version goes ``activating`` / ``running`` first and
  settles at ``production`` / ``active`` (seconds), or stays ``activating`` with
  ``activationStatus`` ``failed`` and an ``activationError`` when a destination
  fails — on Zeus the MAS publish has timed out after an hour that way.
* **A production version cannot change.** Entries of a version in production
  are refused; a new version is created from it with
  ``inheritContentEntriesUri``. Only one version waits to be activated
  (standing ``future``); creating another makes the previous one ``inactive``.
* **Entries are keys up to 256 characters with values up to 512**; keys are
  unique and not empty. An entry whose value is empty behaves at run time as if
  the key were missing.
* **Entry edits are JSON Patch** keyed by the entry key (``/key`` with ``~0`` for
  ``~`` and ``~1`` for ``/``): ``add`` inserts or overwrites, ``remove`` of a key
  that does not exist fails the whole patch. Replacing every entry is a POST of
  the list. Both need the entries' ETag in ``If-Match``.
* **JSON keeps every character; the CSV representation does not** (it is
  Latin-1: ``€`` comes back as ``?``), so entries are always read and written as
  JSON.
* **A rule references a lookup by ID.** A condition or action that names the
  lookup without its ID is stored with status ``error`` ("The lookup table ID is
  required") although the request succeeds.
* **Viya deletes a lookup table that rules still use**, and leaves those rules
  in error: their rule set no longer generates code. The relationships service
  lists the rule sets that use a lookup table, a few seconds after a rule that
  references it is saved.
* **MAS needs MAS code.** Decision code generated for CAS (the default)
  references the lookup as a CAS format and fails to publish to MAS ("Unrecognized
  format"); code generated with ``codeTarget=microAnalyticService`` calls the
  lookup's MAS package.
"""

import asyncio
import re
import time
from typing import Any

import httpx

from .. import config
from ..viya_client import filter_literal, get_paged_items, raise_for_viya_status
from .decisioning_assets import DecisioningError, JSONDict, get_response, send_json

LOOKUP = "application/vnd.sas.data.reference.domain+json"
LOOKUP_CONTENT_FULL = "application/vnd.sas.data.reference.domain.content.full+json"
BASE = "/referenceData/domains"
MAX_KEY_LENGTH = 256
MAX_VALUE_LENGTH = 512
# The newest version can be edited while it is in one of these.
EDITABLE_STATUSES = ("developing", "candidate")
_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_VERSION_FIELDS = (
    "id", "label", "majorNumber", "minorNumber", "status", "standing", "activationStatus", "keyLabel",
    "valueLabel", "creationTimeStamp", "modifiedTimeStamp",
)
# Entries returned with a lookup table unless a page is asked for.
DEFAULT_ENTRY_LIMIT = 1000


# --- entries -------------------------------------------------------------------------


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return "" if value is None else str(value)


def normalize_entries(entries: Any, *, what: str = "entries") -> list[JSONDict]:
    """``{"key": ..., "value": ...}`` entries from a mapping, objects or ``[key, value]`` pairs.

    Refuses what Viya refuses or cannot use: empty or duplicate keys, keys over
    256 and values over 512 characters, and empty values (at run time an entry
    with an empty value is the same as no entry).
    """
    if isinstance(entries, dict):
        pairs = list(entries.items())
    elif isinstance(entries, list):
        pairs = []
        for number, entry in enumerate(entries, start=1):
            if isinstance(entry, dict):
                if "key" not in entry:
                    raise DecisioningError(f"{what} entry {number} has no 'key': {entry!r}.")
                pairs.append((entry["key"], entry.get("value")))
            elif isinstance(entry, (list, tuple)) and len(entry) == 2:
                pairs.append((entry[0], entry[1]))
            else:
                raise DecisioningError(
                    f"{what} entry {number} must be {{\"key\", \"value\"}} or a [key, value] pair, not {entry!r}."
                )
    else:
        raise DecisioningError(f"{what} must be an object of key to value, or a list of entries.")
    out: list[JSONDict] = []
    seen: set[str] = set()
    problems: list[str] = []
    for raw_key, raw_value in pairs:
        key, value = _text(raw_key), _text(raw_value)
        if not key.strip():
            problems.append("a key is empty")
        elif len(key) > MAX_KEY_LENGTH:
            problems.append(f"key {key[:40]!r}... is longer than {MAX_KEY_LENGTH} characters")
        elif key in seen:
            problems.append(f"key {key!r} appears twice")
        if not value:
            problems.append(f"key {key!r} has no value (an empty value works as a missing key)")
        elif len(value) > MAX_VALUE_LENGTH:
            problems.append(f"the value of {key!r} is longer than {MAX_VALUE_LENGTH} characters")
        seen.add(key)
        out.append({"key": key, "value": value})
    if problems:
        shown = "; ".join(problems[:10]) + (f"; and {len(problems) - 10} more" if len(problems) > 10 else "")
        raise DecisioningError(f"{what} cannot be stored: {shown}.")
    return out


def pointer(key: str) -> str:
    """The JSON Patch path of an entry: ``/key`` with ``~`` and ``/`` escaped."""
    return "/" + key.replace("~", "~0").replace("/", "~1")


# --- finding a lookup table ----------------------------------------------------------------


def domain_path(lookup_id: str) -> str:
    return f"{BASE}/{lookup_id}"


async def find_lookup(client: httpx.AsyncClient, ref: str) -> JSONDict:
    """A lookup table by ID or by exact name; a value list or an ambiguous name is refused."""
    ref = ref.strip()
    if _UUID.fullmatch(ref):
        resp = await client.get(
            f"{config.VIYA_ENDPOINT}{domain_path(ref)}",
            headers={"Accept": "application/json"},
        )
        if resp.status_code == 404:
            raise DecisioningError(f"No lookup table with ID {ref!r}.")
        raise_for_viya_status(resp)
        domain = resp.json()
        if domain.get("domainType") != "lookup":
            raise DecisioningError(
                f"{domain.get('name')!r} ({ref}) is a {domain.get('domainType') or 'reference data'} domain, "
                "not a lookup table; value lists have their own tools."
            )
        return domain
    items, _ = await get_paged_items(
        BASE, client, limit=20, filters=f"and(eq(type,'lookup'),eq(name,'{filter_literal(ref)}'))"
    )
    if len(items) == 1:
        return (await get_response(client, domain_path(items[0]["id"]))).json()
    if not items:
        similar, _ = await get_paged_items(
            BASE, client, limit=10, filters=f"and(eq(type,'lookup'),contains(name,'{filter_literal(ref)}'))"
        )
        hint = f" Similar names: {', '.join(repr(i['name']) for i in similar)}." if similar else ""
        raise DecisioningError(f"No lookup table named {ref!r}.{hint}")
    raise DecisioningError(
        f"{len(items)} lookup tables are named {ref!r} (in different folders); use the ID: "
        + ", ".join(i["id"] for i in items)
        + "."
    )


# --- versions ---------------------------------------------------------------------------


def version_summary(content: JSONDict) -> JSONDict:
    summary = {k: content.get(k) for k in _VERSION_FIELDS if content.get(k) is not None}
    error = content.get("activationError")
    if error:
        messages = [e.get("message") for e in (error.get("errors") or []) if e.get("message")]
        summary["activationError"] = messages or [error.get("message")]
    return summary


def _number(content: JSONDict) -> tuple[int, int]:
    return content.get("majorNumber") or 0, content.get("minorNumber") or 0


async def list_versions(client: httpx.AsyncClient, lookup_id: str) -> list[JSONDict]:
    """Every version, oldest first."""
    items, _ = await get_paged_items(f"{domain_path(lookup_id)}/contents", client, limit=1000)
    return [version_summary(c) for c in sorted(items, key=_number)]


def pick_version(versions: list[JSONDict], which: str = "current") -> JSONDict | None:
    """``current`` (the active one, else the newest), ``newest``, or a version ID or label."""
    if not versions:
        return None
    if which == "newest":
        return versions[-1]
    if which == "current":
        return next((v for v in versions if v.get("standing") == "current"), versions[-1])
    found = next((v for v in versions if which in (v.get("id"), v.get("label"))), None)
    if found is None:
        labels = ", ".join(f"{v.get('label')} ({v.get('id')})" for v in versions)
        raise DecisioningError(f"No version {which!r}. The versions are: {labels}.")
    return found


async def entry_page(
    client: httpx.AsyncClient,
    lookup_id: str,
    version_id: str,
    *,
    start: int = 0,
    limit: int = DEFAULT_ENTRY_LIMIT,
    key_filter: str | None = None,
) -> tuple[list[JSONDict], int]:
    """A page of entries in key order, optionally only keys containing *key_filter*."""
    filters = f"contains(key,'{filter_literal(key_filter)}')" if key_filter else None
    items, total = await get_paged_items(
        f"{domain_path(lookup_id)}/contents/{version_id}/entries", client, limit=limit, start=start, filters=filters,
        extra_params={"sortBy": "key:ascending"},
    )
    return [{"key": e.get("key"), "value": e.get("value")} for e in items], total


async def all_entries(client: httpx.AsyncClient, lookup_id: str, version_id: str) -> list[JSONDict]:
    entries: list[JSONDict] = []
    while True:
        page, total = await entry_page(client, lookup_id, version_id, start=len(entries), limit=10_000)
        entries += page
        if not page or len(entries) >= total:
            return entries


async def entries_etag(client: httpx.AsyncClient, path: str) -> str:
    return (await get_response(client, path)).headers.get("etag", "")


async def editable_version(
    client: httpx.AsyncClient,
    lookup_id: str,
    versions: list[JSONDict],
    *,
    key_label: str | None = None,
    value_label: str | None = None,
) -> tuple[JSONDict, bool]:
    """The version edits go to, and whether it was created for them.

    The newest version is edited while it is still being developed. Once it is
    in production — or stuck in a failed activation — a new version is started
    from it, with its entries.
    """
    newest = versions[-1] if versions else None
    if newest is not None and newest.get("status") in EDITABLE_STATUSES:
        return newest, False
    created = await new_version(
        client, lookup_id, versions, source=newest, key_label=key_label, value_label=value_label
    )
    return created, True


async def new_version(
    client: httpx.AsyncClient,
    lookup_id: str,
    versions: list[JSONDict],
    *,
    source: JSONDict | None,
    key_label: str | None = None,
    value_label: str | None = None,
) -> JSONDict:
    """Start the next version, holding *source*'s entries (none without a source)."""
    major, minor = _number(versions[-1]) if versions else (1, -1)
    body: JSONDict = {
        "label": f"{major}.{minor + 1}",
        "status": "developing",
        "majorNumber": major,
        "minorNumber": minor + 1,
    }
    labels = {"keyLabel": key_label or (source or {}).get("keyLabel"),
              "valueLabel": value_label or (source or {}).get("valueLabel")}
    body.update({k: v for k, v in labels.items() if v})
    params = {}
    if source is not None:
        uri = f"{domain_path(lookup_id)}/contents/{source['id']}"
        params = {"inheritContentEntriesUri": uri, "fromRevisionUri": uri}
    created = (await send_json(client, "POST", f"{domain_path(lookup_id)}/contents", body, params=params)).json()
    return version_summary(created)


async def set_version_labels(
    client: httpx.AsyncClient, lookup_id: str, version: JSONDict, key_label: str | None, value_label: str | None
) -> JSONDict:
    """Name the key and value columns of a version (only the fields the update takes are sent)."""
    path = f"{domain_path(lookup_id)}/contents/{version['id']}"
    current = await get_response(client, path)
    data = current.json()
    body = {k: data.get(k) for k in ("label", "majorNumber", "minorNumber", "status", "keyLabel", "valueLabel")}
    if key_label is not None:
        body["keyLabel"] = key_label
    if value_label is not None:
        body["valueLabel"] = value_label
    updated = await send_json(client, "PUT", path, body, if_match=current.headers.get("etag", ""))
    return version_summary(updated.json())


# --- writing entries --------------------------------------------------------------------


async def replace_entries(client: httpx.AsyncClient, lookup_id: str, version_id: str, entries: list[JSONDict]) -> None:
    path = f"{domain_path(lookup_id)}/contents/{version_id}/entries"
    await send_json(client, "POST", path, entries, if_match=await entries_etag(client, path))


async def patch_entries(
    client: httpx.AsyncClient,
    lookup_id: str,
    version_id: str,
    *,
    upsert: list[JSONDict],
    remove: list[str],
) -> JSONDict:
    """Insert or overwrite *upsert* and remove *remove* in one atomic patch.

    Keys to remove that the version does not hold are reported, not sent: one
    such key would fail the whole patch.
    """
    path = f"{domain_path(lookup_id)}/contents/{version_id}/entries"
    existing = {e["key"]: e["value"] for e in await all_entries(client, lookup_id, version_id)}
    missing = [k for k in remove if k not in existing]
    removing = [k for k in dict.fromkeys(remove) if k in existing]
    operations = [{"op": "remove", "path": pointer(k)} for k in removing]
    operations += [{"op": "add", "path": pointer(e["key"]), "value": e["value"]} for e in upsert]
    if operations:
        await send_json(
            client, "PATCH", path, operations, content_type="application/json-patch+json",
            if_match=await entries_etag(client, path),
        )
    return {
        "added": sorted(e["key"] for e in upsert if e["key"] not in existing),
        "changed": sorted(e["key"] for e in upsert if e["key"] in existing and existing[e["key"]] != e["value"]),
        "removed": removing,
        "not_found": missing,
    }


# --- activation -------------------------------------------------------------------------


async def activate(
    client: httpx.AsyncClient, lookup_id: str, version: JSONDict, *, wait_seconds: float = 60.0,
    versions: list[JSONDict] | None = None,
) -> JSONDict:
    """Put a version into production and wait for its publish to MAS and CAS to settle.

    A version that cannot be put into production itself — one that was active
    before (a rollback) or whose activation failed — is activated as a new
    version with its entries; pass *versions* to allow that.
    """
    if version.get("standing") == "current" and version.get("status") == "production":
        return {"version": version, "outcome": "already_active",
                "note": "This version is already the active one; nothing was published."}
    if version.get("activationStatus") == "running":
        return {"version": version, "outcome": "running",
                "note": "This version is still being published; check again with get_lookup_table."}
    started_from = None
    if version.get("status") not in EDITABLE_STATUSES:
        if versions is None:
            raise DecisioningError(f"Version {version.get('label')} cannot be activated again.")
        started_from = version.get("label")
        version = await new_version(client, lookup_id, versions, source=version)
    path = f"{domain_path(lookup_id)}/contents/{version['id']}"
    page, total = await entry_page(client, lookup_id, version["id"], limit=1)
    if not total and not page:
        raise DecisioningError(f"Version {version.get('label')} has no entries; a lookup table needs one to activate.")
    current = await get_response(client, path)
    data = current.json()
    body = {k: data.get(k) for k in ("label", "majorNumber", "minorNumber", "keyLabel", "valueLabel")}
    body["status"] = "production"
    await send_json(
        client, "PUT", path, body, params={"asynchronous": "true"}, if_match=current.headers.get("etag", "")
    )
    started = time.monotonic()
    while True:
        state = version_summary((await get_response(client, path)).json())
        finished = state.get("activationStatus") in ("active", "failed") or state.get("status") == "production"
        if finished or time.monotonic() - started >= wait_seconds:
            break
        await asyncio.sleep(2)
    elapsed = round(time.monotonic() - started, 1)
    if state.get("status") == "production":
        outcome = "active"
        note = "Published to MAS and CAS. Decisions that use this lookup table see the new entries now."
    elif state.get("activationStatus") == "failed":
        outcome = "failed"
        note = ("Publishing failed for at least one destination (activationError says which); the previously "
                "active version stays in use. Fix the cause, then activate a new version.")
    else:
        outcome = "running"
        note = (f"Still publishing after {elapsed}s. Check again with get_lookup_table; the previously active "
                "version stays in use until this one is active.")
    result = {"version": state, "outcome": outcome, "seconds": elapsed, "note": note}
    if started_from:
        result["started_from"] = started_from
        result["note"] = (f"Version {started_from} cannot be put into production again, so its entries were "
                          f"activated as the new version {state.get('label')}. " + note)
    return result


async def republish_current(client: httpx.AsyncClient, lookup_id: str) -> JSONDict:
    """Copy the active version to MAS and CAS again, e.g. after a CAS restart lost its format."""
    path = f"{domain_path(lookup_id)}/currentContents"
    environments = {}
    for environment in ("cas", "mas"):
        etag = (await get_response(client, path)).headers.get("etag", "")
        operation = [{"op": "copy", "from": "/@current", "path": f"/@current/environments/{environment}"}]
        await send_json(client, "PATCH", path, operation, content_type="application/json-patch+json", if_match=etag)
        environments[environment] = "copied"
    return environments


# --- who uses it ------------------------------------------------------------------------


async def used_by(client: httpx.AsyncClient, lookup_id: str) -> list[JSONDict]:
    """The rule sets that reference the lookup table (from the relationships service)."""
    items, _ = await get_paged_items(
        "/relationships/relationships", client, limit=200,
        filters=f"eq(relatedResourceUri,'{domain_path(lookup_id)}')",
    )
    uris = sorted({
        i["resourceUri"].split("/revisions/")[0]
        for i in items
        if i.get("type") == "Dependent" and i.get("resourceUri", "").startswith("/businessRules/ruleSets/")
    })
    users = []
    for uri in uris:
        resp = await client.get(
            f"{config.VIYA_ENDPOINT}{uri}", headers={"Accept": "application/json"}
        )
        if resp.status_code == 404:  # a relationship the deleted rule set left behind
            continue
        raise_for_viya_status(resp)
        users.append({"type": "rule_set", "id": uri.rsplit("/", 1)[-1], "name": resp.json().get("name")})
    return users


# --- reading and creating a whole lookup table ------------------------------------------


def domain_summary(domain: JSONDict) -> JSONDict:
    keep = ("id", "name", "description", "packageName", "formatName", "createdBy", "creationTimeStamp",
            "modifiedBy", "modifiedTimeStamp")
    return {k: domain.get(k) for k in keep if domain.get(k) is not None}


async def describe(
    client: httpx.AsyncClient,
    domain: JSONDict,
    *,
    version: str = "current",
    start: int = 0,
    limit: int = DEFAULT_ENTRY_LIMIT,
    key_filter: str | None = None,
    with_users: bool = True,
) -> JSONDict:
    """A lookup table with its versions, one version's entries and the rule sets that use it."""
    versions = await list_versions(client, domain["id"])
    chosen = pick_version(versions, version)
    result: JSONDict = {**domain_summary(domain), "versions": versions}
    active = next((v for v in versions if v.get("standing") == "current"), None)
    result["active_version"] = active.get("label") if active else None
    if chosen is None:
        result.update(entries=[], entry_count=0, note="The lookup table has no versions yet.")
    else:
        entries, total = await entry_page(
            client, domain["id"], chosen["id"], start=start, limit=limit, key_filter=key_filter
        )
        result.update(version=chosen, entries=entries, entry_count=total)
        if start + len(entries) < total:
            result["next_start"] = start + len(entries)
    if with_users:
        result["used_by"] = await used_by(client, domain["id"])
    return result


async def create_lookup(
    client: httpx.AsyncClient,
    *,
    name: str,
    entries: list[JSONDict],
    folder_uri: str,
    description: str | None = None,
    key_label: str | None = None,
    value_label: str | None = None,
) -> JSONDict:
    """Create a lookup table with a first version holding *entries* (already normalized)."""
    domain = (
        await send_json(
            client, "POST", BASE, {"name": name, "description": description or "", "domainType": "lookup"},
            content_type=LOOKUP, accept=LOOKUP, params={"parentFolderUri": folder_uri},
        )
    ).json()
    body: JSONDict = {"label": "1.0", "status": "developing", "majorNumber": 1, "minorNumber": 0, "entries": entries}
    body.update({k: v for k, v in (("keyLabel", key_label), ("valueLabel", value_label)) if v})
    try:
        content = (
            await send_json(client, "POST", f"{domain_path(domain['id'])}/contents", body,
                            content_type=LOOKUP_CONTENT_FULL)
        ).json()
    except Exception:
        # The table was refused: do not leave an empty lookup table behind.
        await client.delete(f"{config.VIYA_ENDPOINT}{domain_path(domain['id'])}")
        raise
    return {**domain_summary(domain), "version": version_summary(content), "entry_count": len(entries)}


# --- rules ------------------------------------------------------------------------------


async def resolve_rule_lookups(
    client: httpx.AsyncClient, conditions: list[JSONDict], actions: list[JSONDict]
) -> tuple[list[JSONDict], list[JSONDict]]:
    """Give every lookup a rule condition or action names its ID.

    A rule may name the lookup table as ``"lookup": "<name or ID>"`` or
    ``{"name": ...}``; Viya stores a reference without an ID as an error.
    """
    found: dict[str, JSONDict] = {}

    async def resolve(item: JSONDict) -> JSONDict:
        ref = item.get("lookup")
        if isinstance(ref, dict) and ref.get("id"):
            return item
        if isinstance(ref, dict):
            ref = ref.get("name")
        if not ref:
            if item.get("type") in ("lookup", "lookupValue"):
                raise DecisioningError(
                    f"A {item.get('type')} {'condition' if item.get('type') == 'lookup' else 'action'} needs "
                    "'lookup': the lookup table's name or ID."
                )
            return item
        if ref not in found:
            domain = await find_lookup(client, str(ref))
            found[ref] = {"id": domain["id"], "name": domain["name"]}
        return {**item, "lookup": found[ref]}

    return [await resolve(c) for c in conditions], [await resolve(a) for a in actions]


def rule_problems(rule: JSONDict) -> list[str]:
    """The conditions and actions Viya stored with status ``error``, and why."""
    problems = []
    for kind in ("conditions", "actions"):
        for number, item in enumerate(rule.get(kind) or [], start=1):
            if item.get("status") == "error":
                term = (item.get("term") or {}).get("name")
                where = f"{kind[:-1]} {number}" + (f" ({term})" if term else "")
                problems.append(f"{where}: {(item.get('statusMessage') or 'invalid').strip()}")
    return problems
