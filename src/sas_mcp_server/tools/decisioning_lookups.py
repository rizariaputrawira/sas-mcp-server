# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 7 — lookup tables of SAS Intelligent Decisioning.

A lookup table maps keys to values (a country to its risk level, a code to its
label). Rules use it in two ways: a ``lookup`` condition tests whether a
variable's value is one of its keys, and a ``lookupValue`` action assigns the
value stored for a key. Rules see a lookup table's *active* version, which
activation publishes to MAS and CAS; a decision already published picks up a
newly activated version without being published again.

Reading, copying and deleting also go through the shared life-cycle tools
(``asset_type="lookup_table"``). The Viya behaviour these rely on is described
in :mod:`sas_mcp_server.helpers.lookup_helpers`. Registered by
:func:`sas_mcp_server.tools.decisioning.register` as part of the same tier.
"""

import json
from collections.abc import Callable
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import BeforeValidator

from ..helpers import lookup_helpers as lookups
from ..helpers.decisioning_assets import DecisioningError, get_response, send_json
from ..helpers.folder_helpers import current_folder, keep_in_folder, resolve_target_folder
from ._common import coerce_json_list

LOOKUP_CONTENT_TYPE = "referenceDataDomain"
# Longest a call waits for activation to finish publishing; it usually takes seconds.
MAX_WAIT_SECONDS = 300.0


def _coerce_entries(value: Any) -> Any:
    """Entries sent as a JSON-encoded string are decoded (see _common.coerce_json_list)."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                "expected entries as an object of key to value or a list of {key, value}; got an unparseable string."
            ) from exc
    return value


EntriesParam = Annotated[dict[str, Any] | list[Any], BeforeValidator(_coerce_entries)]
KeyListParam = Annotated[list[str], BeforeValidator(coerce_json_list)]


def _wait(seconds: float) -> float:
    return max(0.0, min(float(seconds), MAX_WAIT_SECONDS))


def register_lookup_tools(mcp: FastMCP, viya_session: Callable[..., Any]) -> None:
    """Register the lookup table tools on *mcp* (part of Tier 7)."""

    @mcp.tool()
    async def get_lookup_table(
        lookup_table: str,
        ctx: Context,
        version: str = "current",
        key_filter: str | None = None,
        start: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Read a lookup table: its versions, one version's entries and the rule sets that use it.

        ``active_version`` is the version rules use now. A version's ``status``
        is developing, production (active, or formerly active) or activating;
        ``activationStatus`` failed with ``activationError`` means its publish
        failed. ``used_by`` lists the rule sets that reference the table.

        Args:
            lookup_table: The lookup table's ID or exact name.
            version: "current" (the active version, else the newest), "newest",
                or a version's ID or label.
            key_filter: Only entries whose key contains this text.
            start: Index of the first entry to return (for paging).
            limit: Maximum entries to return (default 200); ``next_start``
                is set when there are more.
        """
        async with viya_session("get_lookup_table", ctx) as client:
            domain = await lookups.find_lookup(client, lookup_table)
            result = await lookups.describe(
                client, domain, version=version, start=max(start, 0), limit=max(min(limit, 10_000), 1),
                key_filter=key_filter,
            )
            result["folder"] = await current_folder(client, lookups.domain_path(domain["id"]))
        return result

    @mcp.tool()
    async def create_lookup_table(
        name: str,
        entries: EntriesParam,
        ctx: Context,
        description: str | None = None,
        key_label: str | None = None,
        value_label: str | None = None,
        folder: str | None = None,
        activate: bool = False,
        wait_seconds: float = 60.0,
    ) -> dict[str, Any]:
        """Create a SAS Intelligent Decisioning lookup table: keys and the value stored for each.

        A rule uses it with a condition ``{"type": "lookup", "term": {...},
        "lookup": "<name or ID>"}`` (is the variable's value a key?) or an action
        ``{"type": "lookupValue", "term": {...}, "expression": "<key
        expression>", "lookup": "<name or ID>"}`` (assign the key's value).

        Rules only see an active version. With ``activate`` the table is
        published to MAS and CAS right away (usually seconds); otherwise call
        activate_lookup_table once the entries are final.

        Keys are text up to 256 characters, values up to 512; numbers are
        stored as text. Keys must be unique and values not empty — an empty
        value behaves like a missing key.

        Args:
            name: Lookup table name.
            entries: ``{"Iran": "High", "Cuba": "High"}``, or a list of
                ``{"key", "value"}`` objects or ``[key, value]`` pairs.
            description: Optional description.
            key_label: Optional name of the key column, e.g. "Country".
            value_label: Optional name of the value column, e.g. "Risk level".
            folder: Where to store it: a folder path, ``My Folder``, a folder
                ID or URI. Omit to use the default folder; if there is none,
                ask the user.
            activate: Publish the first version right away (default false).
            wait_seconds: How long to wait for activation to finish (max 300).
        """
        normalized = lookups.normalize_entries(entries)  # refuse bad entries before anything exists
        async with viya_session("create_lookup_table", ctx) as client:
            target = await resolve_target_folder(client, folder, "lookup table")
            created = await lookups.create_lookup(
                client, name=name, entries=normalized, folder_uri=target["uri"], description=description,
                key_label=key_label, value_label=value_label,
            )
            created["folder"] = target
            if activate:
                created["activation"] = await lookups.activate(
                    client, created["id"], created["version"], wait_seconds=_wait(wait_seconds)
                )
            else:
                created["note"] = (
                    "Not active yet: rules use a lookup table's active version. "
                    "activate_lookup_table publishes it."
                )
        return created

    @mcp.tool()
    async def update_lookup_table(
        lookup_table: str,
        ctx: Context,
        set_entries: EntriesParam | None = None,
        remove_keys: KeyListParam | None = None,
        replace_entries: EntriesParam | None = None,
        name: str | None = None,
        description: str | None = None,
        key_label: str | None = None,
        value_label: str | None = None,
        activate: bool = False,
        wait_seconds: float = 60.0,
        folder: str | None = None,
    ) -> dict[str, Any]:
        """Change a lookup table's entries, column labels, name or description.

        Entry changes go to the newest version while it is still being
        developed. An active version is never changed: a new version is
        started from it, with its entries, and the changes go there. Rules
        keep using the active version until the new one is activated — pass
        ``activate`` to do that in the same call.

        ``set_entries`` adds keys and overwrites the values of existing ones;
        ``remove_keys`` deletes keys (keys that are not there are reported in
        ``not_found``); ``replace_entries`` replaces every entry instead.

        Args:
            lookup_table: The lookup table's ID or exact name.
            set_entries: Entries to add or overwrite: ``{"key": "value"}`` or a
                list of ``{"key", "value"}`` objects.
            remove_keys: Keys to delete.
            replace_entries: The complete new entries (not with set_entries or remove_keys).
            name: New name.
            description: New description.
            key_label: New name of the key column.
            value_label: New name of the value column.
            activate: Publish the changed version right away (default false).
            wait_seconds: How long to wait for activation to finish (max 300).
            folder: Only used for a lookup table that is in no folder yet.
        """
        if replace_entries is not None and (set_entries is not None or remove_keys):
            raise DecisioningError("Give replace_entries alone, or set_entries / remove_keys — not both.")
        replacing = lookups.normalize_entries(replace_entries, what="replace_entries") if replace_entries is not None \
            else None
        if replacing == []:
            raise DecisioningError("A lookup table needs at least one entry; to drop it, delete it instead.")
        upsert = lookups.normalize_entries(set_entries, what="set_entries") if set_entries is not None else []
        removing = [str(k) for k in remove_keys or []]
        edits_entries = replacing is not None or bool(upsert) or bool(removing)
        async with viya_session("update_lookup_table", ctx) as client:
            domain = await lookups.find_lookup(client, lookup_table)
            lookup_id = domain["id"]
            result: dict[str, Any] = {"id": lookup_id}
            if name is not None or description is not None:
                current = await get_response(client, lookups.domain_path(lookup_id))
                body = {k: current.json().get(k) for k in ("name", "description", "domainType", "properties")}
                body.update({k: v for k, v in (("name", name), ("description", description)) if v is not None})
                domain = (
                    await send_json(
                        client, "PUT", lookups.domain_path(lookup_id), body, content_type=lookups.LOOKUP,
                        accept=lookups.LOOKUP, if_match=current.headers.get("etag", ""),
                    )
                ).json()
            result["name"] = domain.get("name")
            version = None
            if edits_entries or key_label is not None or value_label is not None:
                versions = await lookups.list_versions(client, lookup_id)
                version, started = await lookups.editable_version(
                    client, lookup_id, versions, key_label=key_label, value_label=value_label
                )
                if not started and (key_label is not None or value_label is not None):
                    version = await lookups.set_version_labels(client, lookup_id, version, key_label, value_label)
                result["new_version"] = started
                if started:
                    result["started_from"] = versions[-1].get("label") if versions else None
                if replacing is not None:
                    await lookups.replace_entries(client, lookup_id, version["id"], replacing)
                    result["changes"] = {"replaced_with": len(replacing)}
                elif upsert or removing:
                    result["changes"] = await lookups.patch_entries(
                        client, lookup_id, version["id"], upsert=upsert, remove=removing
                    )
                _, result["entry_count"] = await lookups.entry_page(client, lookup_id, version["id"], limit=1)
            if activate:
                versions = await lookups.list_versions(client, lookup_id)
                if version is None:
                    version = lookups.pick_version(versions, "newest")
                if version is None:
                    raise DecisioningError("The lookup table has no version to activate.")
                result["activation"] = await lookups.activate(
                    client, lookup_id, version, wait_seconds=_wait(wait_seconds), versions=versions
                )
                version = result["activation"]["version"]
            elif edits_entries:
                result["note"] = (
                    f"The changes are in version {(version or {}).get('label')}, which rules do not use until it is "
                    "activated (activate_lookup_table)."
                )
            if version is not None:
                result["version"] = version
            result["folder"] = await keep_in_folder(
                client, asset_uri=lookups.domain_path(lookup_id), name=result["name"] or "",
                content_type=LOOKUP_CONTENT_TYPE, folder=folder, what="lookup table",
            )
        return result

    @mcp.tool()
    async def activate_lookup_table(
        lookup_table: str,
        ctx: Context,
        version: str = "newest",
        wait_seconds: float = 60.0,
        republish: bool = False,
    ) -> dict[str, Any]:
        """Make a lookup table version the one rules use: publish its entries to MAS and CAS.

        Decisions already published to MAS use the newly active entries on
        their next call; nothing needs to be published again. The previously
        active version stays in use until this one is active. ``outcome`` is
        active, running (still publishing after ``wait_seconds``) or failed
        (``activationError`` says why).

        ``republish`` copies the already active version to MAS and CAS again —
        for a destination that lost it, such as CAS after a restart.

        Args:
            lookup_table: The lookup table's ID or exact name.
            version: "newest" (default), or a version's ID or label. An earlier
                version (a rollback) or one whose activation failed is
                activated as a new version with its entries.
            wait_seconds: How long to wait for publishing to finish (max 300).
            republish: Copy the active version to MAS and CAS again instead of activating one.
        """
        async with viya_session("activate_lookup_table", ctx) as client:
            domain = await lookups.find_lookup(client, lookup_table)
            versions = await lookups.list_versions(client, domain["id"])
            base = {"id": domain["id"], "name": domain.get("name")}
            if republish:
                active = next((v for v in versions if v.get("standing") == "current"), None)
                if active is None:
                    raise DecisioningError("The lookup table has no active version to publish again.")
                environments = await lookups.republish_current(client, domain["id"])
                return {**base, "version": active, "outcome": "republished", "environments": environments}
            chosen = lookups.pick_version(versions, version)
            if chosen is None:
                raise DecisioningError("The lookup table has no version to activate.")
            activated = await lookups.activate(
                client, domain["id"], chosen, wait_seconds=_wait(wait_seconds), versions=versions
            )
            return {**base, **activated}
