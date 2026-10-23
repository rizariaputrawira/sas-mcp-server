# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 7 — one set of life-cycle tools for every kind of decisioning content.

List, get, code, revisions, lock, copy and delete take an ``asset_type`` and
work the same way for rule sets, decisions, segmentation trees, code files,
value lists, lookup tables, custom functions and function categories, through
:mod:`sas_mcp_server.helpers.decisioning_assets`. They are in the foundation tier,
so content the advanced tier (Tier 10) creates is read, locked, copied and
deleted with them. Registered by :func:`sas_mcp_server.tools.decisioning.register`.
"""

import json
from collections.abc import Callable
from typing import Any, Literal

from fastmcp import Context, FastMCP

from ..helpers import decisioning_assets as assets
from ..helpers.folder_helpers import current_folder, resolve_target_folder

AssetTypeParam = Literal[
    "rule_set", "decision", "segmentation_tree", "code_file", "value_list", "lookup_table", "custom_function",
    "function_category",
]
RevisedAssetParam = Literal["rule_set", "decision", "segmentation_tree", "code_file"]


def register_lifecycle_tools(mcp: FastMCP, viya_session: Callable[..., Any]) -> None:
    """Register the life-cycle tools on *mcp* (part of Tier 7)."""

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
        if summary_only or len(json.dumps(data, default=str)) > assets.MAX_CONTENT_CHARS:
            summary = assets.summarize(data)
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
        result = assets.summarize(created)
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
