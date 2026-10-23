# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for sas_mcp_server.helpers.folder_helpers (no network calls)."""

from unittest.mock import AsyncMock

import pytest

from conftest import _make_mock_response
from sas_mcp_server.helpers import folder_helpers as fh
from sas_mcp_server.helpers.folder_helpers import (
    MY_FOLDER_URI,
    FolderError,
    FolderReference,
    parse_folder_reference,
    resolve_folder,
    resolve_target_folder,
)

UUID = "7ecf7aab-8e22-4718-9668-56e2adda9080"


# --- parsing --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        (f"/folders/folders/{UUID}", FolderReference(uri=f"/folders/folders/{UUID}")),
        (f"  /folders/folders/{UUID}/  ", FolderReference(uri=f"/folders/folders/{UUID}")),
        (UUID, FolderReference(uri=f"/folders/folders/{UUID}")),
        (UUID.upper(), FolderReference(uri=f"/folders/folders/{UUID.upper()}")),
        ("/folders/folders/@myFolder", FolderReference(uri=MY_FOLDER_URI)),
        ("/folders/folders/@MYFOLDER", FolderReference(uri=MY_FOLDER_URI)),
        ("My Folder", FolderReference(uri=MY_FOLDER_URI)),
        ("my folder", FolderReference(uri=MY_FOLDER_URI)),
        ("@myFolder", FolderReference(uri=MY_FOLDER_URI)),
        ("~", FolderReference(uri=MY_FOLDER_URI)),
        ("/My Folder/", FolderReference(uri=MY_FOLDER_URI)),
        ("My Folder/Risk/2026", FolderReference(segments=("Risk", "2026"), under_my_folder=True)),
        ("~/Risk", FolderReference(segments=("Risk",), under_my_folder=True)),
        ("/Public/Content/MCP Created", FolderReference(segments=("Public", "Content", "MCP Created"))),
        ("Public/Content/MCP Created/", FolderReference(segments=("Public", "Content", "MCP Created"))),
        ("/Public//Content", FolderReference(segments=("Public", "Content"))),
    ],
)
def test_parse_folder_reference(reference, expected):
    assert parse_folder_reference(reference) == expected


@pytest.mark.parametrize("reference", ["", "   ", "/", "//", "/folders/folders/", "/folders/folders/a/b"])
def test_parse_rejects_references_that_name_no_folder(reference):
    with pytest.raises(FolderError):
        parse_folder_reference(reference)


# --- resolving --------------------------------------------------------------------


def _client(gets, posts=()):
    """A mock client answering GETs by URL suffix (+ childUri), POSTs in order."""
    client = AsyncMock()

    def get(url, *args, **kwargs):
        child = (kwargs.get("params") or {}).get("childUri")
        for (suffix, want_child), resp in gets.items():
            if url.endswith(suffix) and want_child == child:
                return resp
        raise AssertionError(f"unexpected GET {url} childUri={child}")

    client.get.side_effect = get
    client.post.side_effect = list(posts)
    return client


async def test_resolve_folder_uri_builds_the_full_path():
    client = _client({
        (f"/folders/folders/{UUID}", None): _make_mock_response({"id": UUID, "name": "MCP Created"}),
        ("/folders/ancestors", f"/folders/folders/{UUID}"): _make_mock_response(
            [{"name": "Content"}, {"name": "Public"}]
        ),
    })
    info = await resolve_folder(client, UUID)
    assert info == {
        "id": UUID,
        "uri": f"/folders/folders/{UUID}",
        "name": "MCP Created",
        "path": "/Public/Content/MCP Created",
    }


async def test_resolve_root_folder_has_a_one_level_path():
    client = _client({
        ("/folders/folders/root-1", None): _make_mock_response({"id": "root-1", "name": "Public"}),
        ("/folders/ancestors", "/folders/folders/root-1"): _make_mock_response({}, status_code=404),
    })
    info = await resolve_folder(client, "/folders/folders/root-1")
    assert info["path"] == "/Public"


async def test_resolve_my_folder_alias_returns_the_real_id():
    client = _client({
        ("/folders/folders/@myFolder", None): _make_mock_response({"id": "my-1", "name": "My Folder"}),
        ("/folders/ancestors", "/folders/folders/my-1"): _make_mock_response(
            [{"name": "gerdaw"}, {"name": "Users"}]
        ),
    })
    info = await resolve_folder(client, "My Folder")
    assert info["uri"] == "/folders/folders/my-1"
    assert info["path"] == "/Users/gerdaw/My Folder"


async def test_resolve_path_below_my_folder_uses_its_full_path():
    client = _client(
        {
            ("/folders/folders/@myFolder", None): _make_mock_response({"id": "my-1", "name": "My Folder"}),
            ("/folders/ancestors", "/folders/folders/my-1"): _make_mock_response(
                [{"name": "gerdaw"}, {"name": "Users"}]
            ),
        },
        posts=[_make_mock_response({"id": "risk-1", "name": "Risk"})],
    )
    info = await resolve_folder(client, "My Folder/Risk")
    body = client.post.call_args[1]["json"]
    assert body == {"items": ["Users", "gerdaw", "My Folder", "Risk"], "contentType": "folder"}
    assert info == {
        "id": "risk-1",
        "uri": "/folders/folders/risk-1",
        "name": "Risk",
        "path": "/Users/gerdaw/My Folder/Risk",
    }


async def test_resolve_unknown_folder_id_says_to_ask_for_a_path():
    client = _client({
        (f"/folders/folders/{UUID}", None): _make_mock_response({"message": "No folder"}, status_code=404),
    })
    with pytest.raises(FolderError, match="Ask the user for the folder's path"):
        await resolve_folder(client, UUID)


async def test_resolve_unknown_path_quotes_viya():
    missing = _make_mock_response(
        {"message": 'The item "Nope" could not be found in the path "Public".'}, status_code=404
    )
    client = _client({}, posts=[missing])
    with pytest.raises(FolderError) as exc:
        await resolve_folder(client, "/Public/Nope")
    assert "There is no folder '/Public/Nope'" in str(exc.value)
    assert 'The item "Nope" could not be found' in str(exc.value)


# --- the default folder ----------------------------------------------------------


async def test_target_without_argument_or_default_is_refused(monkeypatch):
    monkeypatch.setattr(fh.config, "MCP_DEFAULT_FOLDER", "")
    with pytest.raises(FolderError) as exc:
        await resolve_target_folder(AsyncMock(), None, "rule set")
    message = str(exc.value)
    assert "No folder was given for the rule set" in message
    assert "MCP_DEFAULT_FOLDER" in message
    assert "Ask the user where to store it" in message


async def test_target_blank_argument_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(fh.config, "MCP_DEFAULT_FOLDER", "/folders/folders/@myFolder")
    client = _client({
        ("/folders/folders/@myFolder", None): _make_mock_response({"id": "my-1", "name": "My Folder"}),
        ("/folders/ancestors", "/folders/folders/my-1"): _make_mock_response([{"name": "u"}, {"name": "Users"}]),
    })
    info = await resolve_target_folder(client, "   ", "decision")
    assert info["uri"] == "/folders/folders/my-1"


async def test_target_unusable_default_names_the_setting(monkeypatch):
    monkeypatch.setattr(fh.config, "MCP_DEFAULT_FOLDER", "/Public/Gone")
    missing = _make_mock_response({"message": "not found"}, status_code=404)
    client = _client({}, posts=[missing])
    with pytest.raises(FolderError) as exc:
        await resolve_target_folder(client, None, "decision")
    message = str(exc.value)
    assert "MCP_DEFAULT_FOLDER='/Public/Gone' cannot be used" in message
    assert "ask an administrator" in message
