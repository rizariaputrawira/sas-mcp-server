# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 10 — Decisioning Advanced: SAS Intelligent Decisioning content beyond rule sets.

Segmentation trees (written, or built from a strategy table), code files (DS2,
Python, SQL), value lists and DS2 custom functions. Tier 7 (Decisioning Starter)
holds what uses them — decision flows, which take them as steps — and the
life-cycle tools that read, lock, copy and delete them, so this tier is meant to
be enabled together with Tier 7; :func:`sas_mcp_server.tools.register_tools`
warns when it is not.

Everything these tools create goes into a SAS Content folder: the ``folder``
argument, else ``MCP_DEFAULT_FOLDER``, else the call is refused and asks for one
(see :mod:`sas_mcp_server.helpers.folder_helpers`).
"""

import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastmcp import Context, FastMCP
from pydantic import BeforeValidator

from ..env import env_bool
from ..helpers import decisioning_assets as assets
from ..helpers import segmentation_tree_helpers as trees
from ..helpers.folder_helpers import keep_in_folder, resolve_target_folder
from ._common import coerce_json_dict, coerce_json_list, make_session_helpers

DictListParam = Annotated[list[dict[str, Any]], BeforeValidator(coerce_json_list)]
AnyListParam = Annotated[list[Any], BeforeValidator(coerce_json_list)]
DictParam = Annotated[dict[str, Any], BeforeValidator(coerce_json_dict)]


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 10 (Decisioning Advanced) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

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
        return assets.summarize(result) if len(json.dumps(result)) > assets.MAX_CONTENT_CHARS else result

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
        return assets.summarize(updated) if len(json.dumps(updated)) > assets.MAX_CONTENT_CHARS else updated

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
                assert caslib_name is not None and table_name is not None  # checked above
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
