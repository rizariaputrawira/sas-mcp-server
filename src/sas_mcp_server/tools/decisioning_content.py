# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 7 — content tools for SAS Intelligent Decisioning beyond rule sets and flows.

Segmentation trees, code files (DS2, Python, SQL), value lists, custom functions
and DS2 validation, plus one set of life-cycle tools — list, get, code,
revisions, lock, copy, delete — that work for every kind of decisioning content,
lookup tables included, through :mod:`sas_mcp_server.helpers.decisioning_assets`. Registered by
:func:`sas_mcp_server.tools.decisioning.register` as part of the same tier.

Everything these tools create goes into a SAS Content folder: the ``folder``
argument, else ``MCP_DEFAULT_FOLDER``, else the call is refused and asks for one
(see :mod:`sas_mcp_server.helpers.folder_helpers`).
"""

import contextlib
import json
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from pydantic import BeforeValidator

from ..env import env_bool
from ..helpers import decisioning_assets as assets
from ..helpers import segmentation_tree_helpers as trees
from ..helpers.folder_helpers import current_folder, keep_in_folder, resolve_target_folder
from ._common import coerce_json_dict, coerce_json_list

DictListParam = Annotated[list[dict[str, Any]], BeforeValidator(coerce_json_list)]
AnyListParam = Annotated[list[Any], BeforeValidator(coerce_json_list)]
DictParam = Annotated[dict[str, Any], BeforeValidator(coerce_json_dict)]
AssetTypeParam = Literal[
    "rule_set", "decision", "segmentation_tree", "code_file", "value_list", "lookup_table", "custom_function",
    "function_category",
]
RevisedAssetParam = Literal["rule_set", "decision", "segmentation_tree", "code_file"]

# A result bigger than this is summarised instead of returned whole: a
# 3,000-leaf segmentation tree is ~600 KB of JSON, far more than a model context
# should carry to answer "what is in it".
MAX_CONTENT_CHARS = 150_000
_HEAVY = ("node", "flow", "rules", "code", "entries", "outcomes", "codeFiles", "booleanExpressions", "matrices")


def _summary(data: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in data.items() if k not in _HEAVY and k != "links"}
    for key in _HEAVY:
        value = data.get(key)
        if isinstance(value, list):
            out[f"{key}_count"] = len(value)
    if isinstance(data.get("node"), dict):
        nodes, leaves = 0, 0
        stack = [data["node"]]
        while stack:
            node = stack.pop()
            nodes += 1
            for value_node in node.get("valueNodes") or []:
                if value_node.get("next"):
                    stack.append(value_node["next"])
                else:
                    leaves += 1
        root_variable = data["node"].get("variableName")
        out["node_summary"] = {"split_nodes": nodes, "leaves": leaves, "root_variable": root_variable}
    if isinstance(data.get("flow"), dict):
        out["flow_step_types"] = [s.get("type", "").rsplit(".", 1)[-1] for s in data["flow"].get("steps", [])]
    return out


def register_content_tools(mcp: FastMCP, viya_session: Callable[..., Any]) -> None:
    """Register the decisioning content tools on *mcp* (part of Tier 7)."""

    # --- one life cycle for every kind ---------------------------------------------------

    @mcp.tool()
    async def list_decisioning_assets(
        asset_type: AssetTypeParam, ctx: Context, limit: int = 20, filter_name: str | None = None
    ) -> list[dict[str, Any]]:
        """List SAS Intelligent Decisioning content of one kind, optionally filtered by name.

        Args:
            asset_type: rule_set, decision, segmentation_tree, code_file,
                value_list, lookup_table, custom_function or function_category.
            limit: Maximum number of results (default 20).
            filter_name: Optional substring of the name.
        """
        async with viya_session("list_decisioning_assets", ctx) as client:
            return await assets.list_assets(client, asset_type, limit=limit, filter_name=filter_name)

    @mcp.tool()
    async def get_decisioning_asset(
        asset_type: AssetTypeParam,
        asset_id: str,
        ctx: Context,
        revision_id: str | None = None,
        summary_only: bool = False,
    ) -> dict[str, Any]:
        """Fetch one piece of decisioning content, or one of its revisions, with the folder it is in.

        A rule set comes with its rules, a code file with its code, a value
        list with the entries of its newest version, a lookup table with its
        versions, the entries of its active one (get_lookup_table pages and
        filters them) and the rule sets that use it. Content larger than
        150,000 characters — a big segmentation tree — is summarised (counts,
        root variable) instead of returned whole; ``summary_only`` asks for the
        summary of anything.

        Args:
            asset_type: rule_set, decision, segmentation_tree, code_file,
                value_list, lookup_table, custom_function or function_category.
            asset_id: The ID (not the name — list_decisioning_assets returns
                both); a lookup table may also be given by its exact name.
            revision_id: A revision to read instead of the current state (for a
                lookup table: a version ID or label).
            summary_only: Return the summary rather than the full content.
        """
        kind = assets.kind_for(asset_type)
        async with viya_session("get_decisioning_asset", ctx) as client:
            data = await assets.get_asset(client, asset_type, asset_id, revision_id)
            if kind.key not in ("custom_function", "function_category"):
                data["folder"] = await current_folder(client, assets.asset_path(kind, data.get("id") or asset_id))
        if summary_only or len(json.dumps(data, default=str)) > MAX_CONTENT_CHARS:
            summary = _summary(data)
            if not summary_only:
                summary["note"] = (
                    "The content is too large to return whole, so this is a summary. "
                    "get_decisioning_asset_code returns the generated code."
                )
            return summary
        return data

    @mcp.tool()
    async def get_decisioning_asset_code(
        asset_type: RevisedAssetParam, asset_id: str, ctx: Context, revision_id: str | None = None
    ) -> str:
        """The DS2 code Viya generates for a rule set, decision or segmentation tree, or a code file's source.

        Args:
            asset_type: rule_set, decision, segmentation_tree or code_file.
            asset_id: The ID.
            revision_id: A revision instead of the current state.
        """
        async with viya_session("get_decisioning_asset_code", ctx) as client:
            return await assets.get_code(client, asset_type, asset_id, revision_id)

    @mcp.tool()
    async def list_decisioning_asset_revisions(
        asset_type: RevisedAssetParam, asset_id: str, ctx: Context, limit: int = 20
    ) -> list[dict[str, Any]]:
        """List the revisions of a rule set, decision, segmentation tree or code file, newest first.

        The newest revision is the working one; every other one is locked.

        Args:
            asset_type: rule_set, decision, segmentation_tree or code_file.
            asset_id: The ID.
            limit: Maximum number of revisions (default 20).
        """
        async with viya_session("list_decisioning_asset_revisions", ctx) as client:
            return await assets.list_revisions(client, asset_type, asset_id, limit=limit)

    @mcp.tool()
    async def lock_decisioning_asset_revision(
        asset_type: RevisedAssetParam,
        asset_id: str,
        ctx: Context,
        revision_type: Literal["minor", "major"] = "minor",
    ) -> dict[str, Any]:
        """Freeze the current state of a rule set, decision, segmentation tree or code file as a locked revision.

        Viya locks the revision that was current and opens the next one for
        editing (1.0 is locked, 1.1 is open). ``locked_revision`` is the frozen
        state: reference it from a decision step or publish it. Later edits go
        to ``working_revision`` and do not change what was locked.

        Args:
            asset_type: rule_set, decision, segmentation_tree or code_file.
            asset_id: The ID.
            revision_type: "minor" for iterative changes, "major" for an approved milestone.
        """
        async with viya_session("lock_decisioning_asset_revision", ctx) as client:
            result = await assets.lock_revision(client, asset_type, asset_id, revision_type)
            result.pop("working", None)
            return result

    @mcp.tool()
    async def copy_decisioning_asset(
        asset_type: AssetTypeParam,
        asset_id: str,
        name: str,
        ctx: Context,
        folder: str | None = None,
        revision_id: str | None = None,
    ) -> dict[str, Any]:
        """Copy a rule set (with its rules), decision, segmentation tree, code file, value list or lookup table.

        Custom functions and function categories cannot be copied. A lookup
        table's copy holds the entries of its active version (or of
        ``revision_id``, a version ID or label) and is not active itself.

        Args:
            asset_type: rule_set, decision, segmentation_tree, code_file,
                value_list or lookup_table.
            asset_id: The ID of the source.
            name: The name of the copy.
            folder: Where to store the copy: a folder path such as
                ``/Public/Content/Decisions``, ``My Folder``, a folder ID, or a
                folder URI. Omit to use the default folder; if there is none,
                ask the user.
            revision_id: Copy this revision instead of the current state.
        """
        kind = assets.kind_for(asset_type)
        async with viya_session("copy_decisioning_asset", ctx) as client:
            target = await resolve_target_folder(client, folder, kind.what)
            created = await assets.copy_asset(
                client, asset_type, asset_id, name=name, folder=target, revision_id=revision_id
            )
        result = _summary(created)
        result["folder"] = target
        return result

    @mcp.tool()
    async def delete_decisioning_asset(asset_type: AssetTypeParam, asset_id: str, ctx: Context) -> dict[str, Any]:
        """Permanently delete a piece of decisioning content and all its revisions.

        Something still in use — a rule set or code file a decision references,
        a category that still holds functions — is refused by Viya. A lookup
        table that rule sets use is refused here, naming them: Viya would
        delete it and leave those rules invalid.

        Args:
            asset_type: rule_set, decision, segmentation_tree, code_file,
                value_list, lookup_table, custom_function or function_category.
            asset_id: The ID to delete.
        """
        async with viya_session("delete_decisioning_asset", ctx) as client:
            return await assets.delete_asset(client, asset_type, asset_id)

    # --- segmentation trees -----------------------------------------------------------

    @mcp.tool()
    async def create_segmentation_tree(
        name: str,
        signature: DictListParam,
        node: DictParam,
        ctx: Context,
        outcomes: DictListParam | None = None,
        description: str | None = None,
        boolean_expressions: DictListParam | None = None,
        matrices: DictListParam | None = None,
        code_files: DictListParam | None = None,
        folder: str | None = None,
    ) -> dict[str, Any]:
        """Create a SAS Intelligent Decisioning segmentation tree.

        A node tests one variable; its ``valueNodes`` are checked in order and
        the first match wins, then goes to ``next`` (another node) or to an
        ``outcomeName``. A value is a value, several separated by ``;``
        (``"A;B"``), a comparison (``">=630"``), a range with ``v``
        (``"600<=v<630"``), or ``"Other"`` for everything else, missing values
        included. Example::

            node = {"variable": "score", "valueNodes": [
                {"value": ">=630", "outcome": "low_risk"},
                {"value": "Other", "outcome": "high_risk"}]}
            outcomes = [{"name": "low_risk", "assign": {"band": "LOW"}},
                        {"name": "high_risk", "assign": {"band": "HIGH"}}]

        An outcome assigns with ``assign`` (variable to value) or runs DS2
        statements with ``code``. To build a large tree from a strategy table,
        use ``create_segmentation_tree_from_table``.

        Args:
            name: Tree name.
            signature: Every variable the tree tests or assigns, each
                ``{"name", "dataType", "direction"}`` — dataType string,
                decimal, integer, date, datetime or boolean; direction input,
                output or inOut. Add ``valueListId`` / ``valueListContentId``
                to tie a variable to a value list.
            node: The root node.
            outcomes: The outcomes the value nodes lead to.
            description: Optional description.
            boolean_expressions: Named DS2 conditions, ``{"name", "expression"}``,
                for nodes of type booleanExpression (value nodes "true" / "false").
            matrices: Matrix definitions, for nodes of type matrix.
            code_files: Internal code files ``{"name", "type": "internal", "code"}``.
            folder: Where to store the tree: a folder path, ``My Folder``, a
                folder ID or URI. Omit to use the default folder; if there is
                none, ask the user.
        """
        body = trees.tree_body(
            name=name, signature=signature, node=node, outcomes=outcomes, description=description,
            boolean_expressions=boolean_expressions, matrices=matrices, code_files=code_files,
        )
        async with viya_session("create_segmentation_tree", ctx) as client:
            target = await resolve_target_folder(client, folder, "segmentation tree")
            created = await assets.send_json(
                client, "POST", "/decisions/segmentationTrees", body, params={"parentFolderUri": target["uri"]}
            )
        result = created.json()
        result["folder"] = target
        return _summary(result) if len(json.dumps(result)) > MAX_CONTENT_CHARS else result

    @mcp.tool()
    async def update_segmentation_tree(
        tree_id: str,
        ctx: Context,
        name: str | None = None,
        description: str | None = None,
        signature: DictListParam | None = None,
        node: DictParam | None = None,
        outcomes: DictListParam | None = None,
        boolean_expressions: DictListParam | None = None,
        matrices: DictListParam | None = None,
        code_files: DictListParam | None = None,
        folder: str | None = None,
    ) -> dict[str, Any]:
        """Change a segmentation tree's working revision: only the parts given are replaced.

        ``node`` replaces the whole node structure and ``outcomes`` the whole
        outcome list. The tree stays in its folder; one in no folder is filed
        into ``folder`` or the default folder.

        Args:
            tree_id: The tree ID.
            name: New name.
            description: New description.
            signature: New signature (all variables).
            node: New root node (see create_segmentation_tree).
            outcomes: New outcomes.
            boolean_expressions: New boolean expressions.
            matrices: New matrices.
            code_files: New internal code files.
            folder: Only used for a tree that is in no folder yet.
        """
        path = f"/decisions/segmentationTrees/{tree_id}"
        async with viya_session("update_segmentation_tree", ctx) as client:
            current = await assets.get_response(client, path)
            merged = assets.content_body(current.json())
            given = {
                "name": name, "description": description, "signature": signature, "node": node,
                "outcomes": outcomes, "booleanExpressions": boolean_expressions, "matrices": matrices,
                "codeFiles": code_files,
            }
            merged.update({k: v for k, v in given.items() if v is not None})
            body = trees.tree_body(
                name=merged["name"], signature=merged.get("signature") or [], node=merged.get("node") or {},
                outcomes=merged.get("outcomes"), description=merged.get("description"),
                boolean_expressions=merged.get("booleanExpressions"), matrices=merged.get("matrices"),
                code_files=merged.get("codeFiles"),
            )
            etag = current.headers.get("etag", "")
            updated = (await assets.send_json(client, "PUT", path, body, if_match=etag)).json()
            updated["folder"] = await keep_in_folder(
                client, asset_uri=path, name=merged["name"], content_type="segmentationTree",
                folder=folder, what="segmentation tree",
            )
        return _summary(updated) if len(json.dumps(updated)) > MAX_CONTENT_CHARS else updated

    @mcp.tool()
    async def create_segmentation_tree_from_table(
        name: str,
        key_columns: AnyListParam,
        outcome_columns: AnyListParam,
        ctx: Context,
        rows: DictListParam | None = None,
        file_path: str | None = None,
        caslib_name: str | None = None,
        table_name: str | None = None,
        server_id: str = "cas-shared-default",
        split: Literal["lower_bound", "exact"] = "lower_bound",
        outcome_prefix: str | None = None,
        row_label_column: str | None = None,
        outcome_types: DictParam | None = None,
        create_value_lists: bool = False,
        description: str | None = None,
        folder: str | None = None,
    ) -> dict[str, Any]:
        """Build one segmentation tree from a strategy table: one row per branch, a column per decision key.

        This is how a FICO-style strategy export (TRIAD, Blaze) becomes a SAS
        Intelligent Decisioning tree. Decision keys are tested in the order of
        ``key_columns``; a key whose values do not differ within a subtree is
        not tested there. Each leaf leads to an outcome that assigns the
        row's ``outcome_columns``.

        With ``split="lower_bound"`` a key's value is the lower bound of a
        range, as such exports write it: sibling values ``0, 600, 630`` become
        ``>= 630``, ``600 to < 630`` and, for the lowest, Other (missing values
        included). With ``split="exact"`` each distinct value is its own branch.

        The table comes from exactly one of ``rows`` (inline, small tables),
        ``file_path`` (a CSV the server reads, gated by
        ``ALLOW_LOCAL_FILE_UPLOAD``) or ``caslib_name`` + ``table_name`` (a CAS
        table — load an Excel sheet with upload_data first). Repeated column
        names in a CSV get ``_2``, ``_3``.

        The result reports what was built — leaves, split nodes, outcomes, and
        rows whose path is identical but whose outcome differs (``conflicts``)
        — not the tree itself, which can be hundreds of kilobytes.

        Args:
            name: Tree name.
            key_columns: Decision key columns in strategy order. A column name,
                or ``{"column": ..., "variable": ...}`` to name the tree
                variable; two columns may test the same variable (a key that
                appears twice in the strategy).
            outcome_columns: Columns the leaves assign, e.g. ["Scenario ID", "Scenario Group"].
            rows: Inline rows, each an object of column to value.
            file_path: A CSV file the server reads.
            caslib_name: Caslib of a CAS table to read.
            table_name: The CAS table.
            server_id: CAS server (default cas-shared-default).
            split: "lower_bound" (range lower bounds) or "exact" (values).
            outcome_prefix: Prefix of outcome names (default: the first outcome column).
            row_label_column: Column that identifies a row in the report, e.g. the branch number.
            outcome_types: Data type per outcome column, overriding the one
                inferred from the values: ``{"Scenario Group": "string"}``.
                Types must match the decision variables the tree is mapped to.
            create_value_lists: Also create a value list per tested variable,
                holding the conditions its nodes use, and tie the variable to it.
            description: Optional description.
            folder: Where to store the tree (and value lists): a folder path,
                ``My Folder``, a folder ID or URI. Omit to use the default
                folder; if there is none, ask the user.
        """
        sources = [s for s, given in (("rows", rows), ("file_path", file_path), ("table", table_name or caslib_name))
                   if given]
        if len(sources) != 1:
            raise trees.TreeError("Give the table as exactly one of rows, file_path, or caslib_name + table_name.")
        if file_path and not env_bool("ALLOW_LOCAL_FILE_UPLOAD", True):
            raise trees.TreeError("Reading files from the server's disk is disabled (ALLOW_LOCAL_FILE_UPLOAD=false).")
        if (caslib_name is None) != (table_name is None):
            raise trees.TreeError("A CAS table needs both caslib_name and table_name.")
        async with viya_session("create_segmentation_tree_from_table", ctx) as client:
            target = await resolve_target_folder(client, folder, "segmentation tree")
            if rows is not None:
                table = rows
            elif file_path:
                table = trees.rows_from_csv_file(file_path)
            else:
                table = await trees.rows_from_cas_table(client, server_id, caslib_name, table_name)
            root, outcomes, signature, report = trees.build_tree_from_rows(
                table, key_columns=key_columns, outcome_columns=[str(c) for c in outcome_columns], split=split,
                outcome_prefix=outcome_prefix, row_label_column=row_label_column, outcome_types=outcome_types,
            )
            # Check the tree before anything is created, so a bad table leaves nothing behind.
            body = trees.tree_body(
                name=name, signature=signature, node=root, outcomes=outcomes,
                description=description or f"Built from a strategy table of {report['rows']} rows.",
            )
            created_lists: list[dict[str, Any]] = []
            if create_value_lists:
                for term in body["signature"]:
                    conditions = report["conditions"].get(term["name"])
                    if not conditions or term["direction"] != "input":
                        continue
                    ordered = (
                        trees.sorted_conditions(conditions) if split == "lower_bound"
                        else [{"key": c, "label": label} for c, label in conditions.items()]
                    )
                    value_list = await assets.create_value_list(
                        client, name=f"{name} {term['name']}"[:100],
                        data_type="numeric" if term["dataType"] != "string" else "character",
                        entries=ordered,
                        folder_uri=target["uri"], description=f"Conditions of {term['name']} in the tree {name}",
                    )
                    term["valueListId"] = value_list["id"]
                    term["valueListContentId"] = value_list["content"]["id"]
                    created_lists.append({"variable": term["name"], "id": value_list["id"], "entries": len(ordered)})
            try:
                created = (
                    await assets.send_json(
                        client, "POST", "/decisions/segmentationTrees", body, params={"parentFolderUri": target["uri"]}
                    )
                ).json()
            except Exception:
                for value_list in created_lists:  # the tree was refused: take its value lists with it
                    with contextlib.suppress(Exception):
                        await assets.delete_asset(client, "value_list", value_list["id"])
                raise
        report.pop("conditions", None)
        return {
            "id": created["id"],
            "name": created["name"],
            "folder": target,
            "report": report,
            "signature": [{k: t.get(k) for k in ("name", "dataType", "direction")} for t in body["signature"]],
            "value_lists": created_lists,
        }

    # --- code files -----------------------------------------------------------------

    @mcp.tool()
    async def create_code_file(
        name: str,
        code_type: Literal["ds2", "python", "sql"],
        code: str,
        ctx: Context,
        description: str | None = None,
        signature: DictListParam | None = None,
        folder: str | None = None,
    ) -> dict[str, Any]:
        """Create a SAS Intelligent Decisioning code file: DS2, Python or SQL.

        Viya reads the signature from the code. DS2: a package with
        ``method execute(...)`` — ``in_out`` parameters are outputs:
        ``package "${PACKAGE_NAME}" /inline; method execute(double score,
        in_out varchar band); ... end; endpackage;``. Python: ``def
        execute(score):`` with ``'Output: band'`` as the first statement and
        the outputs returned. SQL: ``{?:name:type}`` marks an input and
        ``{:name:type}`` an output column; the result rows also come back as a
        data grid.

        Python cannot state types in its code, so give ``signature`` with the
        ``dataType`` of every input and output, or they stay unknown; for DS2
        it adds descriptions and lengths. The result's ``status`` and
        ``errorMessage`` say whether the code parsed.

        Args:
            name: Code file name.
            code_type: ds2, python or sql.
            code: The code.
            description: Optional description.
            signature: Optional ``[{"name", "dataType", "direction", "description"}]``.
            folder: Where to store it: a folder path, ``My Folder``, a folder
                ID or URI. Omit to use the default folder; if there is none,
                ask the user.
        """
        async with viya_session("create_code_file", ctx) as client:
            target = await resolve_target_folder(client, folder, "code file")
            created = await assets.create_code_file(
                client, name=name, code_type=code_type, code=code, folder_uri=target["uri"],
                description=description, signature=signature,
            )
        created["folder"] = target
        return created

    @mcp.tool()
    async def update_code_file(
        code_file_id: str,
        ctx: Context,
        code: str | None = None,
        name: str | None = None,
        description: str | None = None,
        signature: DictListParam | None = None,
        folder: str | None = None,
    ) -> dict[str, Any]:
        """Change a code file's working revision: its code, name, description or signature types.

        New code makes Viya read the signature again. Lock a revision first
        (lock_decisioning_asset_revision) to keep the current state.

        Args:
            code_file_id: The code file ID.
            code: New code.
            name: New name.
            description: New description.
            signature: Types and descriptions (needed for Python, see create_code_file).
            folder: Only used for a code file that is in no folder yet.
        """
        async with viya_session("update_code_file", ctx) as client:
            updated = await assets.update_code_file(
                client, code_file_id, name=name, description=description, code=code, signature=signature
            )
            updated["folder"] = await keep_in_folder(
                client, asset_uri=f"/decisions/codeFiles/{code_file_id}", name=updated.get("name", ""),
                content_type=updated.get("type", "decisionDS2CodeFile"), folder=folder, what="code file",
            )
        return updated

    # --- value lists ----------------------------------------------------------------

    @mcp.tool()
    async def create_value_list(
        name: str,
        entries: AnyListParam,
        ctx: Context,
        data_type: Literal["character", "numeric"] = "character",
        description: str | None = None,
        folder: str | None = None,
    ) -> dict[str, Any]:
        """Create a value list: the values (or ranges) a variable is compared with.

        Segmentation tree variables and rule conditions offer a value list's
        entries. A numeric entry may be a range written with ``v``:
        ``"v>=630"``, ``"600<=v<630"``, ``"v<600"``.

        Args:
            name: Value list name.
            entries: Entries as strings, or ``{"key", "label"}`` objects.
            data_type: "character" or "numeric".
            description: Optional description.
            folder: Where to store it: a folder path, ``My Folder``, a folder
                ID or URI. Omit to use the default folder; if there is none,
                ask the user.
        """
        async with viya_session("create_value_list", ctx) as client:
            target = await resolve_target_folder(client, folder, "value list")
            created = await assets.create_value_list(
                client, name=name, data_type=data_type, entries=entries, folder_uri=target["uri"],
                description=description,
            )
        created["folder"] = target
        return {k: v for k, v in created.items() if k != "links"}

    @mcp.tool()
    async def update_value_list_entries(value_list_id: str, entries: AnyListParam, ctx: Context) -> dict[str, Any]:
        """Replace the entries of a value list's newest version.

        A version already in production is left alone: a new version holds the
        entries instead.

        Args:
            value_list_id: The value list ID.
            entries: The complete new entries, as strings or ``{"key", "label"}`` objects.
        """
        async with viya_session("update_value_list_entries", ctx) as client:
            return await assets.replace_value_list_entries(client, value_list_id, entries)

    # --- custom functions and validation ----------------------------------------------

    @mcp.tool()
    async def create_custom_function(
        code: str, category: str, ctx: Context, description: str | None = None
    ) -> dict[str, Any]:
        """Create a DS2 custom function that rule expressions can call, after checking that it compiles.

        The code is one DS2 method: ``method risk_band(double score) returns
        varchar(10); ... return 'LOW'; end;``. Its name is the function's name.
        A rule then uses it in an expression, e.g. an assignment
        ``risk_band(credit_score)``. A new function takes a few seconds to be
        visible to validate_decision_expression.

        Args:
            code: The DS2 method.
            category: The function category, by name or ID; a name that does not exist yet is created.
            description: Optional description.
        """
        async with viya_session("create_custom_function", ctx) as client:
            created = await assets.create_function(client, code=code, category=category, description=description)
        return {k: v for k, v in created.items() if k != "links"}

    @mcp.tool()
    async def update_custom_function(
        function_id: str,
        ctx: Context,
        code: str | None = None,
        description: str | None = None,
        category: str | None = None,
    ) -> dict[str, Any]:
        """Change a custom function's code, description or category; new code is checked to compile first.

        Every rule set that calls the function uses the new code from its next
        code generation.

        Args:
            function_id: The function ID.
            code: New DS2 method.
            description: New description.
            category: Move it to this category (name or ID).
        """
        async with viya_session("update_custom_function", ctx) as client:
            updated = await assets.update_function(
                client, function_id, code=code, description=description, category=category
            )
        return {k: v for k, v in updated.items() if k != "links"}

    @mcp.tool()
    async def validate_custom_function(code: str, ctx: Context, category: str | None = None) -> dict[str, Any]:
        """Compile a DS2 custom function without saving it, and return its errors.

        Args:
            code: The DS2 method.
            category: Optional category (name or ID) to validate against.
        """
        async with viya_session("validate_custom_function", ctx) as client:
            category_id = None
            if category:
                found = await assets.find_category(client, category)
                if found is None:
                    raise assets.DecisioningError(f"No function category {category!r}.")
                category_id = found["id"]
            return await assets.validate_function(client, code, category_id)

    @mcp.tool()
    async def validate_decision_expression(
        expression: str, ctx: Context, signature: DictListParam | None = None
    ) -> dict[str, Any]:
        """Check that a DS2 expression compiles: a rule condition or action, a branch or a condition step.

        The expression may use the variables in ``signature``, DS2 functions
        and custom functions. Returns ``valid`` and the compiler's error and
        warning lines. A variable the signature does not declare is a warning
        (DS2 creates it), not an error.

        Args:
            expression: The expression, e.g. ``risk_band(credit_score) = 'LOW'``.
            signature: The variables it uses, ``[{"name", "dataType", "direction"}]``.
        """
        async with viya_session("validate_decision_expression", ctx) as client:
            return await assets.validate_expression(client, expression, signature)
