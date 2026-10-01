# Copyright © 2026, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Opt-in tool renaming on the wire (MCP_TOOL_RENAME).

The case behind it was found live: Microsoft Copilot Studio's agent runtime
fails every turn (MCS-2011) while a tool whose name starts with ``catalog_``
is listed, and works when exactly those names are changed.
"""

from __future__ import annotations

import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.server.middleware import Middleware

from sas_mcp_server import tools
from sas_mcp_server.exceptions import ConfigError
from sas_mcp_server.landing import collect_facts
from sas_mcp_server.tool_rename import (
    ToolRenameMiddleware,
    install_tool_rename,
    parse_rename_spec,
)

# --- the setting --------------------------------------------------------------


def test_parse_single_pair():
    assert parse_rename_spec("catalog_=infocat_") == [("catalog_", "infocat_")]


def test_parse_orders_longest_prefix_first_and_trims():
    assert parse_rename_spec(" list_ = ls_ , list_cas = cas_ ") == [
        ("list_cas", "cas_"),
        ("list_", "ls_"),
    ]


@pytest.mark.parametrize("empty", [None, "", "   ", ",", "# catalog_=infocat_"])
def test_parse_empty_means_no_rename(empty):
    assert parse_rename_spec(empty) == []


def test_parse_drops_a_pair_that_changes_nothing():
    assert parse_rename_spec("catalog_=catalog_") == []


@pytest.mark.parametrize(
    "bad, says",
    [
        ("catalog_", "old=new pair"),
        ("catalog_=", "old=new pair"),
        ("=infocat_", "old=new pair"),
        ("catalog_=info cat_", "letters, digits"),
        ("cata.log_=infocat_", "letters, digits"),
        ("a_=b_,a_=c_", "given twice"),
    ],
)
def test_parse_rejects_a_malformed_spec(bad, says):
    with pytest.raises(ConfigError, match=says):
        parse_rename_spec(bad)


# --- a small server -----------------------------------------------------------


class _Seen(Middleware):
    """Records the tool name the rest of the server is asked to run."""

    def __init__(self) -> None:
        self.names: list[str] = []

    async def on_call_tool(self, context, call_next):
        self.names.append(context.message.name)
        return await call_next(context)


def _server(spec: str | None = "catalog_=infocat_") -> tuple[FastMCP, _Seen, ToolRenameMiddleware | None]:
    mcp = FastMCP("rename-test")
    rename = install_tool_rename(mcp, spec)
    seen = _Seen()
    mcp.add_middleware(seen)

    @mcp.tool()
    async def catalog_search(query: str) -> str:
        """Search the catalog. For a filter, see catalog_search_helper."""
        return f"found {query}"

    @mcp.tool()
    async def catalog_search_helper() -> str:
        """Build a filter for catalog_search."""
        return "filter"

    @mcp.tool()
    async def list_terms(catalog_entity_id: str = "") -> str:
        """List terms. Find the asset with catalog_search (not `catalog_search_x`)
        and pass its id as catalog_entity_id."""
        return "terms"

    if rename is not None:
        rename.bind(["catalog_search", "catalog_search_helper", "list_terms"])
    return mcp, seen, rename


async def test_listing_shows_the_public_names_only():
    mcp, _, _ = _server()
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == {"infocat_search", "infocat_search_helper", "list_terms"}


async def test_descriptions_follow_the_rename_as_whole_words():
    mcp, _, _ = _server()
    async with Client(mcp) as client:
        listed = {t.name: t for t in await client.list_tools()}
    assert "see infocat_search_helper" in listed["infocat_search"].description
    assert "filter for infocat_search." in listed["infocat_search_helper"].description
    terms = listed["list_terms"]
    assert "with infocat_search (not" in terms.description
    # Not a tool name: a longer identifier and a parameter keep their spelling.
    assert "`catalog_search_x`" in terms.description
    assert "as catalog_entity_id" in terms.description
    assert "catalog_entity_id" in terms.inputSchema["properties"]


async def test_a_call_by_public_name_runs_the_tool_under_its_own_name():
    mcp, seen, _ = _server()
    async with Client(mcp) as client:
        result = await client.call_tool("infocat_search", {"query": "hmeq"})
    assert result.data == "found hmeq"
    assert seen.names == ["catalog_search"]


async def test_the_original_name_stays_callable():
    mcp, seen, _ = _server()
    async with Client(mcp) as client:
        result = await client.call_tool("catalog_search", {"query": "hmeq"})
    assert result.data == "found hmeq"
    assert seen.names == ["catalog_search"]


async def test_untouched_tools_are_untouched():
    mcp, seen, _ = _server()
    async with Client(mcp) as client:
        assert (await client.call_tool("list_terms", {})).data == "terms"
    assert seen.names == ["list_terms"]


async def test_no_setting_installs_nothing():
    mcp, _, rename = _server(spec="")
    assert rename is None
    assert not any(isinstance(m, ToolRenameMiddleware) for m in mcp.middleware)
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert "catalog_search" in names


def test_the_setting_is_read_from_config(monkeypatch):
    from sas_mcp_server import config

    monkeypatch.setattr(config, "MCP_TOOL_RENAME", "catalog_=infocat_")
    rename = install_tool_rename(FastMCP("rename-test"))
    assert rename is not None
    assert rename.public_name("catalog_run_agent") == "infocat_run_agent"


# --- a rename that cannot work stops the server -------------------------------


def test_bind_reports_what_was_renamed():
    rename = ToolRenameMiddleware(parse_rename_spec("catalog_=infocat_"))
    assert rename.bind(["catalog_search", "query_data"]) == {"catalog_search": "infocat_search"}


def test_bind_refuses_two_tools_under_one_name():
    rename = ToolRenameMiddleware(parse_rename_spec("catalog_=list_"))
    with pytest.raises(ConfigError, match="the same name, 'list_agents'"):
        rename.bind(["catalog_agents", "list_agents"])


def test_bind_refuses_a_name_no_model_api_accepts():
    rename = ToolRenameMiddleware(parse_rename_spec("catalog_=" + "x" * 60))
    with pytest.raises(ConfigError, match="not a usable tool name"):
        rename.bind(["catalog_search"])


def test_bind_tolerates_a_prefix_no_tool_has(caplog):
    rename = ToolRenameMiddleware(parse_rename_spec("catalog_=infocat_"))
    with caplog.at_level("WARNING"):
        assert rename.bind(["query_data"]) == {}
    assert "no registered tool starts with 'catalog_'" in caplog.text


def test_longest_prefix_wins():
    rename = ToolRenameMiddleware(parse_rename_spec("catalog_=ic_,catalog_search=find_assets"))
    assert rename.public_name("catalog_search") == "find_assets"
    assert rename.public_name("catalog_search_helper") == "find_assets_helper"
    assert rename.public_name("catalog_run_agent") == "ic_run_agent"


# --- against the real catalogue -----------------------------------------------


async def _token(ctx: Context) -> str:
    return "token"


async def _real_server(read_only: bool = False) -> tuple[FastMCP, ToolRenameMiddleware]:
    mcp = FastMCP("rename-real")
    rename = install_tool_rename(mcp, "catalog_=infocat_")
    assert rename is not None
    tools.register_tools(mcp, _token, tiers="0-1", read_only=read_only)
    rename.bind(tools.TOOL_TIERS)
    return mcp, rename


async def test_the_catalog_tools_are_renamed_and_nothing_else_changes():
    plain = FastMCP("plain")
    tools.register_tools(plain, _token, tiers="0-1")
    async with Client(plain) as client:
        before = {t.name: t for t in await client.list_tools()}

    mcp, _ = await _real_server()
    async with Client(mcp) as client:
        after = {t.name: t for t in await client.list_tools()}

    catalog = sorted(n for n in before if n.startswith("catalog_"))
    assert len(catalog) == 9
    assert not any(n.startswith("catalog_") for n in after)
    assert set(after) == {n.replace("catalog_", "infocat_", 1) if n in catalog else n for n in before}
    # No description still points at a name the client cannot see.
    for tool in after.values():
        for name in catalog:
            assert f"{name}`" not in (tool.description or "")
            assert f"{name} " not in (tool.description or "")
    # Annotations and schemas ride along: the classification keys on the name
    # in the code, which the rename never touches.
    assert after["infocat_search"].annotations.readOnlyHint is True
    assert after["infocat_run_agent"].annotations.readOnlyHint is False
    assert after["infocat_search"].inputSchema == before["catalog_search"].inputSchema
    assert after["execute_sas_code"].meta == before["execute_sas_code"].meta


async def test_read_only_mode_still_withholds_by_the_original_name():
    mcp, _ = await _real_server(read_only=True)
    async with Client(mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert "infocat_search" in names
    assert "infocat_run_agent" not in names
    assert "catalog_run_agent" not in names


async def test_the_landing_page_lists_what_clients_see():
    mcp, rename = await _real_server()
    facts = await collect_facts(
        mcp,
        server_name="s",
        mcp_url="http://localhost:8134/mcp",
        viya_endpoint="https://viya.example.com",
        auth_enabled=True,
        allow_raw_bearer=False,
        read_only=False,
        enabled_tiers={0, 1},
        public_name=rename.public_name,
    )
    listed = {entry.name for tier in facts.tiers for entry in tier.tools}
    assert "infocat_search" in listed
    assert "catalog_search" not in listed
    assert "list_cas_servers" in listed
