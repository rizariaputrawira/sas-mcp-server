# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 6 — Model Management & Scoring tools."""

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastmcp import Context, FastMCP

from ..viya_client import contains_filter, get_json, get_paged_items, post_json, return_items
from ._common import make_session_helpers


def match_input_names(input_data: dict[str, Any], step_inputs: list[str]) -> dict[str, str]:
    """The names in *input_data* the step does not know but knows with a trailing underscore.

    Modules published from SAS Intelligent Decisioning name their inputs
    ``credit_score_``; MAS compares names without case. Returns
    ``{given name: step input name}``.
    """
    known = {name.lower(): name for name in step_inputs}
    return {
        name: known[f"{name}_".lower()]
        for name in input_data
        if name.lower() not in known and f"{name}_".lower() in known and f"{name}_" not in input_data
    }


def _inputs(input_data: dict[str, Any]) -> dict[str, Any]:
    return {"inputs": [{"name": k, "value": v} for k, v in input_data.items()]}


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 6 (Model Management & Scoring) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

    @mcp.tool()
    async def list_registered_models(ctx: Context, limit: int = 50) -> list[dict[str, Any]]:
        """List models in the Model Repository.

        Args:
            limit: Maximum models to return (default 50).
        """
        async with viya_session("list_registered_models", ctx) as client:
            items, _ = await get_paged_items("/modelRepository/models", client, limit=limit)
            return return_items(items, ["id", "name", "description", "modelVersionName"])

    @mcp.tool()
    async def list_publishing_destinations(
        ctx: Context, limit: int = 50, start: int = 0, filter_name: str | None = None
    ) -> list[dict[str, Any]]:
        """List available publishing destinations.

        Args:
            ctx: FastMCP context.
            limit: Maximum destinations to return (default 50).
            start: Row offset (default 0).
            filter_name: Optional filter for destination names.
        """
        async with viya_session("list_publishing_destinations", ctx) as client:
            items, _ = await get_paged_items(
                "/modelPublish/destinations",
                client,
                limit=limit,
                start=start,
                filters=contains_filter(filter_name),
            )
            return return_items(items, ["id", "name", "description", "destinationType"])

    @mcp.tool()
    async def list_mas_modules(ctx: Context, limit: int = 50) -> list[dict[str, Any]]:
        """List published scoring models and decisions (MAS modules).

        Args:
            limit: Maximum modules to return (default 50).
        """
        async with viya_session("list_mas_modules", ctx) as client:
            items, _ = await get_paged_items("/microanalyticScore/modules", client, limit=limit)
            return return_items(items, ["id", "name", "description"])

    @mcp.tool()
    async def get_mas_module_step_signature(module_id: str, ctx: Context, step_id: str = "execute") -> dict[str, Any]:
        """Fetch a MAS module step's input/output variable signature.

        Call before ``score_data`` to know the exact variable names, types,
        and order to pass as inputs, and what outputs to expect.

        Args:
            module_id: The MAS module ID (see ``list_mas_modules``).
            step_id: The step within the module to inspect (default "execute").
        """
        async with viya_session("get_mas_module_step_signature", ctx) as client:
            return await get_json(f"/microanalyticScore/modules/{module_id}/steps/{step_id}", client)

    @mcp.tool()
    async def score_data(module_id: str, step_id: str, input_data: dict, ctx: Context) -> dict[str, Any]:
        """Score data against a published model or decision (MAS module).

        A decision published from the SAS Intelligent Decisioning UI names its
        inputs with a trailing underscore (``credit_score_``); the plain names
        are accepted and mapped, and the result's ``inputs_renamed`` says so.
        (``publish_decision_flow`` keeps the plain names.)

        Args:
            module_id: MAS module ID.
            step_id: Step ID within the module (usually 'score' or 'execute').
            input_data: Dictionary of input variable name-value pairs.
        """
        path = f"/microanalyticScore/modules/{module_id}/steps/{step_id}"
        async with viya_session("score_data", ctx) as client:
            try:
                return await post_json(path, client, body=_inputs(input_data))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 400:
                    raise
                step = await get_json(path, client)
                renames = match_input_names(input_data, [i.get("name", "") for i in step.get("inputs") or []])
                if not renames:
                    raise
            renamed = {renames.get(name, name): value for name, value in input_data.items()}
            result = await post_json(path, client, body=_inputs(renamed))
            result["inputs_renamed"] = renames
            return result
