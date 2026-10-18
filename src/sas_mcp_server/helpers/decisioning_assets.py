# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""One lifecycle for every kind of SAS Intelligent Decisioning content.

Rule sets, decisions, segmentation trees and code files share a life cycle:
they live in a folder, are read and listed the same way, freeze their state as
revisions, and generate code. Value lists, custom functions and function
categories share the reading, listing and deleting. :data:`ASSET_KINDS` states
each kind's endpoints and media types once, and the functions below implement
the life cycle against that table, so the tools need one implementation per
operation rather than one per kind and operation.

Behaviour this relies on, probed against SAS Viya in October 2026:

* **Locking a revision freezes the current state and opens the next one.**
  ``POST {asset}/revisions?revisionType=minor`` with the full content marks the
  revision that was current as locked and returns a *new*, unlocked working
  revision (1.0 locked, 1.1 open). The frozen state is therefore the revision
  before the one the response describes; :func:`lock_revision` reports both.
* **The newest revision is the working one.** Revision listings sorted newest
  first carry the numbers, and every revision but the first is locked.
* **A copy is a create with the source's content.** ``fromRevisionUri`` alone
  creates an empty object that only remembers its source.
* **Code files keep their code in the Files service.** A code file is created
  from a ``fileUri``; the with-code media type is accepted for revisions only.
  Writing new content to the file makes Viya re-parse the signature. The new
  code file is named after the uploaded *file* — the ``name`` in the request
  is ignored — and its ``description`` is dropped, so the file is uploaded
  under the wanted name and the description is set by an update afterwards.
* **Python code files need their types in a comment.** The signature extension
  ``#/*{"signatureExtension":[...]}*/`` is honoured only as compact JSON; with
  spaces after the separators every type stays ``unknown``.
* **Deleting a Python code file with a locked revision fails** with HTTP 500
  ("A data error occurred") until its locked revisions are deleted.
* **Value list ranges use a ``v`` placeholder**: ``v>=630``, ``600<=v<630``.
* **New custom functions take a few seconds** to become visible to expression
  validation; until then they are reported as unknown functions.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from .. import config
from ..viya_client import contains_filter, get_paged_items, raise_for_viya_status

JSONDict = dict[str, Any]
COLLECTION = "application/vnd.sas.collection+json"
DS2_SOURCE = "text/vnd.sas.source.ds2"
RULE_SET_INTEGRAL = "application/vnd.sas.business.rule.set.integral+json"
CODE_FILE_WITH_CODE = "application/vnd.sas.decision.code.file.with.code+json"
VALUE_LIST = "application/vnd.sas.data.reference.value.list+json"
VALUE_LIST_CONTENT_FULL = "application/vnd.sas.data.reference.value.list.content.full+json"

AssetType = Literal[
    "rule_set",
    "decision",
    "segmentation_tree",
    "code_file",
    "value_list",
    "custom_function",
    "function_category",
]
RevisionType = Literal["minor", "major"]


class DecisioningError(ValueError):
    """A request the decisioning tools refuse. The message says what to do instead."""


@dataclass(frozen=True)
class AssetKind:
    """How one kind of decisioning content is reached."""

    key: str
    what: str
    base: str
    folder_content_type: str | None = None  # None: not stored in folders, or depends on the item
    revisions: bool = True
    read_accept: str = "application/json"
    code: bool = True  # GET {asset}/code returns generated DS2
    list_fields: tuple[str, ...] = ("id", "name", "majorRevision", "minorRevision", "modifiedTimeStamp")
    list_filter: str | None = None


ASSET_KINDS: dict[str, AssetKind] = {
    kind.key: kind
    for kind in (
        AssetKind(
            "rule_set", "rule set", "/businessRules/ruleSets", "ruleSet",
            read_accept=RULE_SET_INTEGRAL,
            list_fields=("id", "name", "ruleSetType", "status", "majorRevision", "minorRevision", "modifiedTimeStamp"),
        ),
        AssetKind("decision", "decision", "/decisions/flows", "decision"),
        AssetKind("segmentation_tree", "segmentation tree", "/decisions/segmentationTrees", "segmentationTree"),
        AssetKind(
            "code_file", "code file", "/decisions/codeFiles", None, code=False,
            list_fields=("id", "name", "type", "status", "majorRevision", "minorRevision", "modifiedTimeStamp"),
        ),
        AssetKind(
            "value_list", "value list", "/referenceData/domains", "referenceValueList", revisions=False, code=False,
            list_fields=("id", "name", "description", "modifiedTimeStamp"), list_filter="eq(type,'valueList')",
        ),
        AssetKind(
            "custom_function", "custom function", "/businessRules/functions", revisions=False, code=False,
            list_fields=("id", "name", "description", "categoryId", "returnType"),
        ),
        AssetKind(
            "function_category", "function category", "/businessRules/functionCategories", revisions=False, code=False,
            list_fields=("id", "name", "description"),
        ),
    )
}

# Fields the server owns. A body built from a fetched object drops them before
# it is sent back as a new object or a new revision.
SERVER_FIELDS = frozenset(
    {
        "id", "links", "version", "createdBy", "creationTimeStamp", "modifiedBy", "modifiedTimeStamp",
        "revisionId", "revisionUri", "majorRevision", "minorRevision", "locked", "checkout", "folderType",
        "sourceRevisionUri", "copyTimeStamp", "status", "errorCode", "errorMessage", "validationStatus",
        "validatedBy", "validationTimeStamp", "hasErrors", "hasWarnings", "hasErrorsInSubDecisions",
        "hasWarningsInSubDecisions", "nodeCount", "decisionId", "fileUri",
    }
)


def kind_for(asset_type: str) -> AssetKind:
    try:
        return ASSET_KINDS[asset_type]
    except KeyError:
        raise DecisioningError(
            f"Unknown asset_type {asset_type!r}. Use one of: {', '.join(ASSET_KINDS)}."
        ) from None


def asset_path(kind: AssetKind, asset_id: str, revision_id: str | None = None) -> str:
    path = f"{kind.base}/{asset_id}"
    if revision_id:
        if not kind.revisions:
            raise DecisioningError(f"A {kind.what} has no revisions.")
        path += f"/revisions/{revision_id}"
    return path


def content_body(resource: JSONDict) -> JSONDict:
    """*resource* without the fields the server owns, ready to send as new content."""
    return {k: v for k, v in resource.items() if k not in SERVER_FIELDS}


def _url(path: str) -> str:
    return f"{config.VIYA_ENDPOINT}{path}"


async def get_response(
    client: httpx.AsyncClient, path: str, accept: str = "application/json", params: JSONDict | None = None
) -> httpx.Response:
    """GET *path*, raising Viya's own error message on failure."""
    resp = await client.get(_url(path), headers={"Accept": accept}, params=params or {})
    raise_for_viya_status(resp)
    return resp


async def send_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    body: Any,
    *,
    content_type: str = "application/json",
    accept: str = "application/json",
    params: JSONDict | None = None,
    if_match: str | None = None,
) -> httpx.Response:
    """Send *body* as JSON with *method*, raising Viya's own error message on failure."""
    headers = {"Content-Type": content_type, "Accept": accept}
    if if_match is not None:
        headers["If-Match"] = if_match
    send = getattr(client, method.lower())
    resp = await send(_url(path), content=json.dumps(body).encode(), headers=headers, params=params or {})
    raise_for_viya_status(resp)
    return resp


# --- reading -------------------------------------------------------------------


async def get_asset(
    client: httpx.AsyncClient, asset_type: str, asset_id: str, revision_id: str | None = None
) -> JSONDict:
    """The full content of an asset (or one revision), with what it is made of.

    A rule set comes with its rules, a code file with its code, a value list
    with the entries of its newest content version.
    """
    kind = kind_for(asset_type)
    data = (await get_response(client, asset_path(kind, asset_id, revision_id), kind.read_accept)).json()
    if kind.key == "code_file" and data.get("fileUri"):
        data["code"] = await read_file_text(client, data["fileUri"])
    elif kind.key == "value_list":
        content = await latest_value_list_content(client, asset_id)
        if content is not None:
            data["content"] = content
            data["entries"] = await value_list_entries(client, asset_id, content["id"])
    return data


async def list_assets(
    client: httpx.AsyncClient, asset_type: str, *, limit: int = 20, filter_name: str | None = None
) -> list[JSONDict]:
    kind = kind_for(asset_type)
    filters = [f for f in (kind.list_filter, contains_filter(filter_name)) if f]
    combined = filters[0] if len(filters) == 1 else (f"and({','.join(filters)})" if filters else None)
    items, _ = await get_paged_items(kind.base, client, limit=limit, filters=combined)
    return [{field: item.get(field) for field in kind.list_fields if field in item} for item in items]


async def read_file_text(client: httpx.AsyncClient, file_uri: str) -> str:
    return (await get_response(client, f"{file_uri}/content", "text/plain")).text


async def get_code(
    client: httpx.AsyncClient, asset_type: str, asset_id: str, revision_id: str | None = None
) -> str:
    """Generated DS2 for rule sets, decisions and trees; the source of a code file."""
    kind = kind_for(asset_type)
    if kind.key == "code_file":
        data = (await get_response(client, asset_path(kind, asset_id, revision_id))).json()
        return await read_file_text(client, data["fileUri"])
    if not kind.code:
        raise DecisioningError(f"A {kind.what} has no generated code.")
    return (await get_response(client, f"{asset_path(kind, asset_id, revision_id)}/code", DS2_SOURCE)).text


# --- revisions -------------------------------------------------------------------


def _revision_summary(kind: AssetKind, asset_id: str, item: JSONDict, locked: bool) -> JSONDict:
    return {
        "id": item.get("id"),
        "majorRevision": item.get("majorRevision"),
        "minorRevision": item.get("minorRevision"),
        "locked": item.get("locked", locked),
        "creationTimeStamp": item.get("creationTimeStamp"),
        "uri": f"{kind.base}/{asset_id}/revisions/{item.get('id')}",
    }


async def list_revisions(
    client: httpx.AsyncClient, asset_type: str, asset_id: str, *, limit: int = 20
) -> list[JSONDict]:
    """Revisions newest first. The first is the working revision; every other one is locked."""
    kind = kind_for(asset_type)
    if not kind.revisions:
        raise DecisioningError(f"A {kind.what} has no revisions.")
    items, _ = await get_paged_items(
        f"{kind.base}/{asset_id}/revisions", client, limit=limit,
        extra_params={"sortBy": "creationTimeStamp:descending"},
    )
    return [_revision_summary(kind, asset_id, item, locked=index > 0) for index, item in enumerate(items)]


async def _revision_body(client: httpx.AsyncClient, kind: AssetKind, asset_id: str) -> tuple[JSONDict, str]:
    """The current content as a revision request body, and its media type."""
    current = (await get_response(client, asset_path(kind, asset_id), kind.read_accept)).json()
    if kind.key == "rule_set":
        body = {k: current.get(k) for k in ("name", "description", "signature", "rules")}
        return body, RULE_SET_INTEGRAL
    if kind.key == "decision":
        return {k: current.get(k) for k in ("name", "description", "signature", "flow")}, "application/json"
    if kind.key == "code_file":
        body = {
            "name": current.get("name"),
            "description": current.get("description"),
            "type": current.get("type"),
            "code": await read_file_text(client, current["fileUri"]),
        }
        return body, CODE_FILE_WITH_CODE
    return content_body(current), "application/json"


async def lock_revision(
    client: httpx.AsyncClient, asset_type: str, asset_id: str, revision_type: str = "minor"
) -> JSONDict:
    """Freeze the current state as a locked revision and open the next working one.

    Returns ``locked_revision`` — the frozen state, which decision steps and
    publishing should reference — and ``working_revision``, the revision that
    edits now go to.
    """
    kind = kind_for(asset_type)
    if not kind.revisions:
        raise DecisioningError(f"A {kind.what} has no revisions.")
    if revision_type not in ("minor", "major"):
        raise DecisioningError(f"revision_type must be 'minor' or 'major', not {revision_type!r}.")
    body, content_type = await _revision_body(client, kind, asset_id)
    accept = RULE_SET_INTEGRAL if kind.key == "rule_set" else "application/json"
    working = (
        await send_json(
            client, "POST", f"{kind.base}/{asset_id}/revisions", body,
            content_type=content_type, accept=accept, params={"revisionType": revision_type},
        )
    ).json()
    revisions = await list_revisions(client, asset_type, asset_id, limit=5)
    locked = next((r for r in revisions if r["id"] != working.get("id")), None)
    if locked is not None:
        locked["locked"] = True
    return {
        "asset_type": kind.key,
        "asset_id": asset_id,
        "name": working.get("name"),
        "locked_revision": locked,
        "working_revision": _revision_summary(kind, asset_id, working, locked=False),
        "working": working,
    }


# --- copy and delete ---------------------------------------------------------------


async def copy_asset(
    client: httpx.AsyncClient,
    asset_type: str,
    asset_id: str,
    *,
    name: str,
    folder: JSONDict,
    revision_id: str | None = None,
) -> JSONDict:
    """Create a copy of an asset (or of one revision) in *folder*, under *name*."""
    kind = kind_for(asset_type)
    if kind.key in ("custom_function", "function_category"):
        raise DecisioningError(f"A {kind.what} cannot be copied; create a new one with the same code instead.")
    source = await get_asset(client, asset_type, asset_id, revision_id)
    params = {"parentFolderUri": folder["uri"]}
    if kind.key == "code_file":
        created = await create_code_file(
            client, name=name, code_type=CODE_TYPE_KEYS.get(source.get("type", ""), "ds2"),
            code=source.get("code", ""), description=source.get("description"), folder_uri=folder["uri"],
            raw_type=source.get("type"),
        )
    elif kind.key == "value_list":
        created = await create_value_list(
            client, name=name, data_type=(source.get("properties") or {}).get("dataType", "character"),
            entries=source.get("entries", []), description=source.get("description"), folder_uri=folder["uri"],
        )
    else:
        body = content_body(source)
        body["name"] = name
        if revision_id:
            params["fromRevisionUri"] = asset_path(kind, asset_id, revision_id)
        elif source.get("revisionUri"):
            params["fromRevisionUri"] = source["revisionUri"]
        content_type = RULE_SET_INTEGRAL if kind.key == "rule_set" else "application/json"
        created = (
            await send_json(
                client, "POST", kind.base, body, content_type=content_type, accept=content_type, params=params
            )
        ).json()
    created["copied_from"] = {"id": asset_id, "revision_id": revision_id, "name": source.get("name")}
    return created


async def delete_asset(client: httpx.AsyncClient, asset_type: str, asset_id: str) -> JSONDict:
    """Delete an asset. A code file the service refuses is retried without its locked revisions."""
    kind = kind_for(asset_type)
    path = asset_path(kind, asset_id)
    resp = await client.delete(_url(path))
    removed_revisions: list[str] = []
    if resp.status_code >= 500 and kind.key == "code_file":
        for revision in (await list_revisions(client, asset_type, asset_id, limit=100))[1:]:
            rev = await client.delete(_url(f"{path}/revisions/{revision['id']}"))
            raise_for_viya_status(rev)
            removed_revisions.append(revision["id"])
        resp = await client.delete(_url(path))
    raise_for_viya_status(resp)
    result: JSONDict = {"deleted": True, "asset_type": kind.key, "asset_id": asset_id}
    if removed_revisions:
        result["locked_revisions_deleted_first"] = removed_revisions
    return result


# --- code files --------------------------------------------------------------------

CODE_FILE_TYPES = {"ds2": "decisionDS2CodeFile", "python": "decisionPythonFile", "sql": "decisionSQLCodeFile"}
CODE_TYPE_KEYS = {value: key for key, value in CODE_FILE_TYPES.items()}
_CODE_EXTENSIONS = {"ds2": ".ds2", "python": ".py", "sql": ".sql"}
_SIGNATURE_EXTENSION = re.compile(r"\A\s*#?/\*\{\"signatureExtension\".*?\}\*/[ \t]*\r?\n?", re.S)


def code_type_for(code_type: str) -> str:
    try:
        return CODE_FILE_TYPES[code_type.lower()]
    except KeyError:
        raise DecisioningError(f"code_type must be one of {', '.join(CODE_FILE_TYPES)}, not {code_type!r}.") from None


def with_signature_extension(code: str, code_type: str, signature: list[JSONDict] | None) -> str:
    """*code* with its first-line signature extension set to *signature*.

    The extension carries data types, descriptions and lengths the parser
    cannot infer. Python needs it for any types at all, and only compact JSON
    is honoured. SQL code files declare their types inline, so they are left
    unchanged.
    """
    if not signature or code_type == "sql":
        return code
    terms = [{k: v for k, v in term.items() if k in ("name", "description", "dataType", "direction", "length",
                                                       "defaultValue", "generateDataGridColumns")}
             for term in signature]
    payload = json.dumps({"signatureExtension": terms}, separators=(",", ":"))
    prefix = "#" if code_type == "python" else ""
    return f"{prefix}/*{payload}*/\n" + _SIGNATURE_EXTENSION.sub("", code, count=1)


async def upload_code(client: httpx.AsyncClient, code: str, file_name: str) -> str:
    resp = await client.post(
        _url("/files/files"),
        content=code.encode("utf-8"),
        headers={
            "Content-Type": "text/plain",
            "Content-Disposition": f'attachment; filename="{file_name}"',
            "Accept": "application/json",
        },
    )
    raise_for_viya_status(resp)
    return f"/files/files/{resp.json()['id']}"


async def create_code_file(
    client: httpx.AsyncClient,
    *,
    name: str,
    code_type: str,
    code: str,
    folder_uri: str,
    description: str | None = None,
    signature: list[JSONDict] | None = None,
    raw_type: str | None = None,
) -> JSONDict:
    type_value = raw_type or code_type_for(code_type)
    key = CODE_TYPE_KEYS.get(type_value, code_type)
    source = with_signature_extension(code, key, signature)
    # The code file takes its name from the uploaded file, so keep the name as given
    # apart from what a Content-Disposition header or a path cannot carry.
    file_name = re.sub(r'["\\/\r\n]', "_", name).strip() + _CODE_EXTENSIONS.get(key, ".txt")
    file_uri = await upload_code(client, source, file_name)
    body = {"name": name, "type": type_value, "fileUri": file_uri}
    created = (
        await send_json(client, "POST", "/decisions/codeFiles", body, params={"parentFolderUri": folder_uri})
    ).json()
    if description or created.get("name") != name:
        created = await update_code_file(client, created["id"], name=name, description=description)
    return created


async def update_code_file(
    client: httpx.AsyncClient,
    code_file_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    code: str | None = None,
    signature: list[JSONDict] | None = None,
) -> JSONDict:
    """Change a code file's working revision. New code is written to its file, which re-parses the signature."""
    path = f"/decisions/codeFiles/{code_file_id}"
    current = (await get_response(client, path)).json()
    if code is not None or signature is not None:
        key = CODE_TYPE_KEYS.get(current.get("type", ""), "ds2")
        source = code if code is not None else await read_file_text(client, current["fileUri"])
        source = with_signature_extension(source, key, signature)
        file_path = f"{current['fileUri']}/content"
        etag = (await get_response(client, current["fileUri"])).headers.get("etag", "")
        resp = await client.put(
            _url(file_path), content=source.encode("utf-8"),
            headers={"Content-Type": "text/plain", "If-Match": etag},
        )
        raise_for_viya_status(resp)
    if name is None and description is None:
        return (await get_response(client, path)).json()
    fresh = await get_response(client, path)
    body = {k: v for k, v in fresh.json().items() if k != "links"}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    return (await send_json(client, "PUT", path, body, if_match=fresh.headers.get("etag", ""))).json()


# --- value lists -------------------------------------------------------------------


def normalize_entries(entries: list[Any]) -> list[JSONDict]:
    """Value list entries from strings or ``{"key", "label"/"value", "disabled"}`` objects."""
    out = []
    for order, entry in enumerate(entries, start=1):
        if isinstance(entry, dict):
            if "key" not in entry:
                raise DecisioningError(f"Value list entry {order} has no 'key': {entry!r}.")
            out.append({
                "key": str(entry["key"]),
                "value": str(entry.get("label", entry.get("value", "")) or ""),
                "disabled": bool(entry.get("disabled", False)),
                "order": int(entry.get("order", order)),
            })
        else:
            out.append({"key": str(entry), "value": "", "disabled": False, "order": order})
    return out


async def create_value_list(
    client: httpx.AsyncClient,
    *,
    name: str,
    data_type: str,
    entries: list[Any],
    folder_uri: str,
    description: str | None = None,
) -> JSONDict:
    if data_type not in ("numeric", "character"):
        raise DecisioningError(f"data_type must be 'numeric' or 'character', not {data_type!r}.")
    domain = (
        await send_json(
            client, "POST", "/referenceData/domains",
            {
                "name": name,
                "description": description or "",
                "domainType": "valueList",
                "properties": {"dataType": data_type},
            },
            content_type=VALUE_LIST, params={"parentFolderUri": folder_uri},
        )
    ).json()
    content = (
        await send_json(
            client, "POST", f"/referenceData/domains/{domain['id']}/contents",
            {
                "label": "1.0",
                "status": "developing",
                "majorNumber": 1,
                "minorNumber": 0,
                "entries": normalize_entries(entries),
            },
            content_type=VALUE_LIST_CONTENT_FULL,
        )
    ).json()
    domain["content"] = {k: content.get(k) for k in ("id", "label", "status", "standing", "majorNumber", "minorNumber")}
    domain["entries"] = await value_list_entries(client, domain["id"], content["id"])
    return domain


async def latest_value_list_content(client: httpx.AsyncClient, value_list_id: str) -> JSONDict | None:
    items, _ = await get_paged_items(f"/referenceData/domains/{value_list_id}/contents", client, limit=100)
    if not items:
        return None
    newest = max(items, key=lambda c: (c.get("majorNumber") or 0, c.get("minorNumber") or 0))
    return {k: newest.get(k) for k in ("id", "label", "status", "standing", "majorNumber", "minorNumber")}


async def value_list_entries(client: httpx.AsyncClient, value_list_id: str, content_id: str) -> list[JSONDict]:
    items, _ = await get_paged_items(
        f"/referenceData/domains/{value_list_id}/contents/{content_id}/entries", client, limit=1000
    )
    entries = [{k: e.get(k) for k in ("key", "value", "disabled", "order")} for e in items]
    return sorted(entries, key=lambda e: e.get("order") or 0)


async def replace_value_list_entries(client: httpx.AsyncClient, value_list_id: str, entries: list[Any]) -> JSONDict:
    """Replace the entries of the newest content version, or start a new one if that is in production."""
    content = await latest_value_list_content(client, value_list_id)
    normalized = normalize_entries(entries)
    if content is None or content.get("status") == "production":
        major = (content or {}).get("majorNumber") or 1
        minor = ((content or {}).get("minorNumber") or 0) + (1 if content else 0)
        created = (
            await send_json(
                client, "POST", f"/referenceData/domains/{value_list_id}/contents",
                {"label": f"{major}.{minor}", "status": "developing", "majorNumber": major, "minorNumber": minor,
                 "entries": normalized},
                content_type=VALUE_LIST_CONTENT_FULL,
            )
        ).json()
        content_id, started_new = created["id"], True
    else:
        content_id, started_new = content["id"], False
        path = f"/referenceData/domains/{value_list_id}/contents/{content_id}/entries"
        etag = (await get_response(client, path)).headers.get("etag", "")
        await send_json(client, "POST", path, normalized, if_match=etag)
    return {
        "value_list_id": value_list_id,
        "content": await latest_value_list_content(client, value_list_id),
        "new_content_version": started_new,
        "entries": await value_list_entries(client, value_list_id, content_id),
    }


# --- custom functions and DS2 validation ------------------------------------------------

_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


def ds2_log_messages(log: str) -> JSONDict:
    """The ERROR and WARNING lines of a DS2 compile log, without the echoed source."""
    errors = [line.strip() for line in log.splitlines() if line.strip().startswith("ERROR")]
    warnings = [line.strip() for line in log.splitlines() if line.strip().startswith("WARNING")]
    errors = [e for e in errors if e != "ERROR: Compilation error."] or errors
    return {"errors": errors, "warnings": warnings}


async def find_category(client: httpx.AsyncClient, category: str) -> JSONDict | None:
    """A function category by ID or by exact name."""
    if _UUID.fullmatch(category.strip()):
        resp = await client.get(
            _url(f"/businessRules/functionCategories/{category.strip()}"), headers={"Accept": "application/json"}
        )
        if resp.status_code == 404:
            return None
        raise_for_viya_status(resp)
        return resp.json()
    items, _ = await get_paged_items(
        "/businessRules/functionCategories", client, limit=10,
        filters=f"eq(name,'{category.replace(chr(39), chr(39) * 2)}')",
    )
    return items[0] if items else None


async def validate_function(client: httpx.AsyncClient, code: str, category_id: str | None = None) -> JSONDict:
    body: JSONDict = {"code": code}
    if category_id:
        body["categoryId"] = category_id
    data = (await send_json(client, "POST", "/businessRules/commons/validations/functions", body)).json()
    problems = data.get("items") or []
    errors: list[str] = []
    warnings: list[str] = []
    for problem in problems:
        parsed = ds2_log_messages(problem.get("message", ""))
        errors += parsed["errors"] or [problem.get("message", "")[:500]]
        warnings += parsed["warnings"]
    return {"valid": not problems, "errors": errors, "warnings": warnings}


async def create_function(
    client: httpx.AsyncClient, *, code: str, category: str, description: str | None = None
) -> JSONDict:
    """Validate, then create a custom function; a category named but missing is created."""
    found = await find_category(client, category)
    created_category = False
    if found is None:
        if _UUID.fullmatch(category.strip()):
            raise DecisioningError(f"No function category with ID {category!r}.")
        found = (await send_json(client, "POST", "/businessRules/functionCategories", {"name": category})).json()
        created_category = True
    check = await validate_function(client, code, found["id"])
    if not check["valid"]:
        raise DecisioningError("The function does not compile: " + " | ".join(check["errors"][:5]))
    body: JSONDict = {"code": code}
    if description:
        body["description"] = description
    path = f"/businessRules/functionCategories/{found['id']}/functions"
    function = (await send_json(client, "POST", path, body)).json()
    function["category"] = {"id": found["id"], "name": found.get("name"), "created": created_category}
    if check["warnings"]:
        function["compile_warnings"] = check["warnings"]
    return function


async def update_function(
    client: httpx.AsyncClient,
    function_id: str,
    *,
    code: str | None = None,
    description: str | None = None,
    category: str | None = None,
) -> JSONDict:
    path = f"/businessRules/functions/{function_id}"
    current = await get_response(client, path)
    body = {k: v for k, v in current.json().items() if k != "links"}
    if category is not None:
        found = await find_category(client, category)
        if found is None:
            raise DecisioningError(f"No function category {category!r}.")
        body["categoryId"] = found["id"]
    if code is not None:
        check = await validate_function(client, code, body.get("categoryId"))
        if not check["valid"]:
            raise DecisioningError("The function does not compile: " + " | ".join(check["errors"][:5]))
        body["code"] = code
    if description is not None:
        body["description"] = description
    return (await send_json(client, "PUT", path, body, if_match=current.headers.get("etag", ""))).json()


async def validate_expression(
    client: httpx.AsyncClient, expression: str, signature: list[JSONDict] | None = None
) -> JSONDict:
    body: JSONDict = {"expression": expression}
    if signature:
        body["signature"] = signature
    data = (await send_json(client, "POST", "/decisions/validations/expressionValidations", body)).json()
    if data.get("valid"):
        return {"valid": True, "errors": [], "warnings": []}
    error = data.get("error") or {}
    parsed = ds2_log_messages(error.get("message", ""))
    result: JSONDict = {"valid": False, **parsed}
    if not parsed["errors"] and error.get("message"):
        result["errors"] = [error["message"][:500]]
    if any("Unknown function" in e for e in result["errors"]):
        result["note"] = (
            "A custom function created moments ago can take a few seconds to become visible to "
            "validation; validate again shortly before treating this as a real error."
        )
    return result


def signature_terms(signature: list[JSONDict] | dict[str, str] | None) -> list[JSONDict] | None:
    """A signature given as a list of terms, or as ``{"name": "dataType"}``."""
    if signature is None:
        return None
    if isinstance(signature, dict):
        return [{"name": name, "dataType": data_type, "direction": "input"} for name, data_type in signature.items()]
    return signature
