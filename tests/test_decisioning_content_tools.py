# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tool-level tests for the decisioning content tools against a routed fake Viya (no network)."""

import json
import re
from unittest.mock import MagicMock

import httpx
import pytest
from fastmcp import Client

from conftest import _make_mock_response

FOLDER = {"id": "f-1", "name": "MCP Created"}
TREE_SIG = [{"name": "score", "dataType": "decimal", "direction": "input"},
            {"name": "band", "dataType": "string", "direction": "output"}]


class FakeViya:
    """Answers the mock client's requests by method and URL pattern; records what was sent."""

    def __init__(self, mock_client):
        self.routes: list[tuple[str, re.Pattern, object]] = []
        self.sent: list[tuple[str, str, dict]] = []
        for method in ("get", "post", "put", "delete"):
            getattr(mock_client, method).side_effect = self._handler(method.upper())
        self.add("GET", r"/folders/folders/f-1$", FOLDER)
        self.add("GET", r"/folders/ancestors$", lambda url, kw: (
            [{"id": "c-1", "name": "Content"}, {"id": "p-1", "name": "Public"}]
            if kw["params"]["childUri"].startswith("/folders/")
            else [
                {"id": "f-1", "name": "MCP Created"},
                {"id": "c-1", "name": "Content"},
                {"id": "p-1", "name": "Public"},
            ]
        ))

    def add(self, method, pattern, body, status=200, headers=None):
        def respond(url, kwargs):
            data = body(url, kwargs) if callable(body) else body
            resp = _make_mock_response(data, status_code=status)
            if status >= 400:
                request = httpx.Request(method, url)
                resp.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError(
                    f"HTTP {status}", request=request, response=httpx.Response(status, request=request)))
            if status == 204:
                resp.content = b""
            if isinstance(data, str):
                resp.text = data
            resp.headers = {"Content-Type": "application/json", "etag": '"e1"', **(headers or {})}
            return resp
        self.routes.insert(0, (method, re.compile(pattern), respond))

    def _handler(self, method):
        def handle(url, *args, **kwargs):
            self.sent.append((method, url, kwargs))
            for m, pattern, respond in self.routes:
                if m == method and pattern.search(url.split("?")[0]):
                    return respond(url, kwargs)
            raise AssertionError(f"unrouted {method} {url}")
        return handle

    def calls(self, method, pattern):
        return [(url, kw) for m, url, kw in self.sent if m == method and re.search(pattern, url)]


@pytest.fixture
def fake(mcp_server_with_mock_client, monkeypatch):
    monkeypatch.setattr("sas_mcp_server.config.MCP_DEFAULT_FOLDER", "/folders/folders/f-1")
    mcp, mock_client = mcp_server_with_mock_client
    return mcp, FakeViya(mock_client)


async def call(mcp, tool, args):
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args, raise_on_error=False)
    if result.is_error:
        return {"error": result.content[0].text}
    return result.data if result.data is not None else result.content[0].text


# --- reading ----------------------------------------------------------------------------


async def test_get_big_tree_is_summarised(fake):
    mcp, viya = fake
    leaves = [{"value": str(i), "outcomeName": "o"} for i in range(9000)]
    viya.add("GET", r"/segmentationTrees/t-1$", {"id": "t-1", "name": "Big", "signature": TREE_SIG,
                                                 "node": {"variableName": "score", "valueNodes": leaves},
                                                 "outcomes": [{"name": "o"}]})
    result = await call(mcp, "get_decisioning_asset", {"asset_type": "segmentation_tree", "asset_id": "t-1"})
    assert result["node_summary"] == {"split_nodes": 1, "leaves": 9000, "root_variable": "score"}
    assert result["outcomes_count"] == 1 and "node" not in result and "too large" in result["note"]
    assert result["folder"]["path"] == "/Public/Content/MCP Created"


async def test_get_code_file_value_list_and_function(fake):
    mcp, viya = fake
    viya.add(
        "GET", r"/codeFiles/cf-1$",
        {"id": "cf-1", "name": "C", "fileUri": "/files/files/x", "type": "decisionPythonFile"},
    )
    viya.add("GET", r"/files/files/x/content$", "def execute():\n  return 1\n")
    viya.add("GET", r"/domains/vl-1$", {"id": "vl-1", "name": "V", "properties": {"dataType": "character"}})
    viya.add("GET", r"/domains/vl-1/contents$", {"items": [{"id": "c1", "majorNumber": 1, "minorNumber": 0},
                                                         {"id": "c2", "majorNumber": 1, "minorNumber": 1}], "count": 2})
    viya.add(
        "GET", r"/contents/c2/entries$",
        {"items": [{"key": "B", "order": 2}, {"key": "A", "order": 1}], "count": 2},
    )
    viya.add("GET", r"/businessRules/functions/fn-1$", {"id": "fn-1", "name": "f", "code": "method f(); end;"})
    code_file = await call(mcp, "get_decisioning_asset", {"asset_type": "code_file", "asset_id": "cf-1"})
    assert code_file["code"].startswith("def execute")
    value_list = await call(mcp, "get_decisioning_asset", {"asset_type": "value_list", "asset_id": "vl-1"})
    assert value_list["content"]["id"] == "c2" and [e["key"] for e in value_list["entries"]] == ["A", "B"]
    function = await call(mcp, "get_decisioning_asset", {"asset_type": "custom_function", "asset_id": "fn-1"})
    assert "folder" not in function
    source = await call(mcp, "get_decisioning_asset_code", {"asset_type": "code_file", "asset_id": "cf-1"})
    assert source.startswith("def execute")


async def test_generated_code_and_revisions(fake):
    mcp, viya = fake
    viya.add("GET", r"/flows/d-1/revisions/r-1/code$", "package x; endpackage;")
    viya.add("GET", r"/flows/d-1/revisions$", {"items": [{"id": "r-2", "majorRevision": 1, "minorRevision": 1},
                                                         {"id": "r-1", "majorRevision": 1, "minorRevision": 0}],
                                                         "count": 2})
    code = await call(
        mcp, "get_decisioning_asset_code", {"asset_type": "decision", "asset_id": "d-1", "revision_id": "r-1"}
    )
    assert code == "package x; endpackage;"
    revisions = await call(mcp, "list_decisioning_asset_revisions", {"asset_type": "decision", "asset_id": "d-1"})
    assert [(r["id"], r["locked"]) for r in revisions] == [("r-2", False), ("r-1", True)]
    assert revisions[1]["uri"] == "/decisions/flows/d-1/revisions/r-1"


async def test_lock_code_file_sends_its_code(fake):
    mcp, viya = fake
    viya.add(
        "GET", r"/codeFiles/cf-1$",
        {"id": "cf-1", "name": "C", "type": "decisionDS2CodeFile", "fileUri": "/files/files/x"},
    )
    viya.add("GET", r"/files/files/x/content$", "package x; endpackage;")
    viya.add("POST", r"/codeFiles/cf-1/revisions$", {"id": "r-2", "name": "C", "majorRevision": 1, "minorRevision": 1})
    viya.add("GET", r"/codeFiles/cf-1/revisions$", {"items": [{"id": "r-2"}, {"id": "r-1"}], "count": 2})
    result = await call(mcp, "lock_decisioning_asset_revision", {"asset_type": "code_file", "asset_id": "cf-1"})
    assert result["locked_revision"]["id"] == "r-1" and "working" not in result
    _, kw = viya.calls("POST", r"/revisions$")[0]
    assert kw["headers"]["Content-Type"] == "application/vnd.sas.decision.code.file.with.code+json"
    assert json.loads(kw["content"])["code"] == "package x; endpackage;"


# --- copy and delete ------------------------------------------------------------------------


async def test_copy_tree_rule_set_and_value_list(fake):
    mcp, viya = fake
    viya.add("GET", r"/segmentationTrees/t-1$", {"id": "t-1", "name": "T", "signature": TREE_SIG, "node": {},
                                                 "revisionUri": "/decisions/segmentationTrees/t-1/revisions/r-1"})
    viya.add("POST", r"/decisions/segmentationTrees$", {"id": "t-2", "name": "T copy"})
    viya.add("GET", r"/ruleSets/rs-1$", {"id": "rs-1", "name": "R", "signature": [], "rules": [{"name": "x"}]})
    viya.add("POST", r"/businessRules/ruleSets$", {"id": "rs-2", "name": "R copy", "rules": [{"name": "x"}]})
    viya.add("GET", r"/domains/vl-1$", {"id": "vl-1", "name": "V", "properties": {"dataType": "numeric"}})
    viya.add(
        "GET", r"/domains/vl-1/contents$",
        {"items": [{"id": "c1", "majorNumber": 1, "minorNumber": 0}], "count": 1},
    )
    viya.add("GET", r"/domains/[^/]+/contents/c1/entries$", {"items": [{"key": "v>=1", "order": 1}], "count": 1})
    viya.add("POST", r"/referenceData/domains$", {"id": "vl-2", "name": "V copy"})
    viya.add("POST", r"/domains/vl-2/contents$", {"id": "c1", "label": "1.0", "status": "developing"})
    tree = await call(
        mcp, "copy_decisioning_asset", {"asset_type": "segmentation_tree", "asset_id": "t-1", "name": "T copy"}
    )
    assert tree["id"] == "t-2" and tree["folder"]["id"] == "f-1"
    _, kw = viya.calls("POST", r"/decisions/segmentationTrees$")[0]
    assert kw["params"]["fromRevisionUri"] == "/decisions/segmentationTrees/t-1/revisions/r-1"
    assert json.loads(kw["content"])["name"] == "T copy" and "id" not in json.loads(kw["content"])
    rule_set = await call(
        mcp, "copy_decisioning_asset", {"asset_type": "rule_set", "asset_id": "rs-1", "name": "R copy"}
    )
    assert rule_set["rules_count"] == 1
    _, kw = viya.calls("POST", r"/businessRules/ruleSets$")[0]
    assert kw["headers"]["Content-Type"] == "application/vnd.sas.business.rule.set.integral+json"
    value_list = await call(
        mcp, "copy_decisioning_asset", {"asset_type": "value_list", "asset_id": "vl-1", "name": "V copy"}
    )
    assert value_list["id"] == "vl-2"
    refused = await call(mcp, "copy_decisioning_asset", {"asset_type": "custom_function", "asset_id": "f", "name": "x"})
    assert "cannot be copied" in refused["error"]


async def test_delete_asset(fake):
    mcp, viya = fake
    viya.add("DELETE", r"/segmentationTrees/t-1$", None, status=204)
    result = await call(mcp, "delete_decisioning_asset", {"asset_type": "segmentation_tree", "asset_id": "t-1"})
    assert result == {"deleted": True, "asset_type": "segmentation_tree", "asset_id": "t-1"}


# --- segmentation trees --------------------------------------------------------------------


async def test_create_and_update_tree(fake):
    mcp, viya = fake
    viya.add("POST", r"/decisions/segmentationTrees$", lambda url, kw: {"id": "t-1", **json.loads(kw["content"])})
    created = await call(mcp, "create_segmentation_tree", {
        "name": "T", "signature": TREE_SIG,
        "node": {"variable": "score", "valueNodes": [
            {"value": ">=630", "outcome": "hi"}, {"value": "Other", "outcome": "lo"}]},
        "outcomes": [{"name": "hi", "assign": {"band": "HIGH"}}, {"name": "lo", "assign": {"band": "LOW"}}],
    })
    assert created["node"]["valueNodes"][1]["value"] == "__&OTHER&__" and created["folder"]["id"] == "f-1"
    viya.add("GET", r"/segmentationTrees/t-1$", {"id": "t-1", "name": "T", "signature": TREE_SIG,
                                                 "node": created["node"], "outcomes": created["outcomes"]})
    viya.add("PUT", r"/segmentationTrees/t-1$", lambda url, kw: {"id": "t-1", **json.loads(kw["content"])})
    updated = await call(mcp, "update_segmentation_tree", {"tree_id": "t-1", "description": "now described"})
    assert updated["description"] == "now described" and updated["name"] == "T"
    assert updated["folder"]["status"] == "unchanged"
    _, kw = viya.calls("PUT", r"/segmentationTrees/t-1$")[0]
    assert kw["headers"]["If-Match"] == '"e1"'


async def test_tree_from_rows_with_value_lists_and_rollback(fake):
    mcp, viya = fake
    counter = iter(range(100))
    viya.add("POST", r"/referenceData/domains$", lambda url, kw: {"id": f"vl-{next(counter)}", "name": "x"})
    viya.add("POST", r"/domains/[^/]+/contents$", {"id": "c1", "label": "1.0", "status": "developing"})
    viya.add("GET", r"/domains/[^/]+/contents/c1/entries$", {"items": [], "count": 0})
    viya.add(
        "POST", r"/decisions/segmentationTrees$",
        lambda url, kw: {"id": "t-1", "name": json.loads(kw["content"])["name"]},
    )
    rows = [{"A": 0, "Out": 1}, {"A": 600, "Out": 2}, {"A": 630, "Out": 3}]
    args = {"name": "T", "key_columns": ["A"], "outcome_columns": ["Out"], "rows": rows, "create_value_lists": True}
    result = await call(mcp, "create_segmentation_tree_from_table", args)
    assert result["report"]["leaves"] == 3 and len(result["value_lists"]) == 1
    sent_tree = json.loads(viya.calls("POST", r"/decisions/segmentationTrees$")[0][1]["content"])
    assert sent_tree["signature"][0]["valueListId"] == "vl-0"
    entries = json.loads(viya.calls("POST", r"/contents$")[0][1]["content"])["entries"]
    assert [e["key"] for e in entries] == ["600<=v<630", "v>=630"]
    # the tree is refused: its value lists go too
    viya.add("POST", r"/decisions/segmentationTrees$", {"message": "not valid"}, status=400)
    viya.add("DELETE", r"/referenceData/domains/vl-1$", None, status=204)
    failed = await call(mcp, "create_segmentation_tree_from_table", args)
    assert "error" in failed
    assert viya.calls("DELETE", r"/referenceData/domains/vl-1$")
    both = await call(mcp, "create_segmentation_tree_from_table", {**args, "file_path": "x.csv"})
    assert "exactly one" in both["error"]


# --- code files, value lists, functions -------------------------------------------------------


async def test_update_code_file_writes_new_code_and_renames(fake):
    mcp, viya = fake
    viya.add(
        "GET", r"/codeFiles/cf-1$",
        {"id": "cf-1", "name": "Old", "type": "decisionPythonFile", "fileUri": "/files/files/x"},
    )
    viya.add("GET", r"/files/files/x$", {"id": "x"})
    viya.add("PUT", r"/files/files/x/content$", None, status=204)
    viya.add("PUT", r"/codeFiles/cf-1$", lambda url, kw: json.loads(kw["content"]))
    result = await call(mcp, "update_code_file", {
        "code_file_id": "cf-1", "name": "New", "code": "def execute(a):\n   'Output: b'\n   return a\n",
        "signature": [{"name": "a", "dataType": "decimal", "direction": "input"}],
    })
    assert result["name"] == "New" and result["folder"]["status"] == "unchanged"
    _, kw = viya.calls("PUT", r"/content$")[0]
    assert kw["content"].decode().startswith('#/*{"signatureExtension":[{"name":"a"')


async def test_value_list_create_and_replace_entries(fake):
    mcp, viya = fake
    viya.add("POST", r"/referenceData/domains$", {"id": "vl-1", "name": "Bands"})
    viya.add("POST", r"/domains/vl-1/contents$", {"id": "c1", "label": "1.0", "status": "developing"})
    viya.add("GET", r"/domains/vl-1/contents/c1/entries$", {"items": [{"key": "LOW", "order": 1}], "count": 1})
    viya.add("GET", r"/domains/vl-1/contents$", {"items": [{"id": "c1", "majorNumber": 1, "minorNumber": 0,
                                                            "status": "developing"}], "count": 1})
    viya.add("POST", r"/domains/vl-1/contents/c1/entries$", None, status=204)
    created = await call(mcp, "create_value_list", {"name": "Bands", "entries": ["LOW"]})
    assert created["content"]["id"] == "c1" and created["folder"]["id"] == "f-1"
    replaced = await call(mcp, "update_value_list_entries", {"value_list_id": "vl-1", "entries": ["LOW", "HIGH"]})
    assert replaced["new_content_version"] is False
    viya.add("GET", r"/domains/vl-1/contents$", {"items": [{"id": "c1", "majorNumber": 1, "minorNumber": 0,
                                                            "status": "production"}], "count": 1})
    viya.add("POST", r"/domains/vl-1/contents$", {"id": "c1", "label": "1.1"})
    started = await call(mcp, "update_value_list_entries", {"value_list_id": "vl-1", "entries": ["LOW"]})
    assert started["new_content_version"] is True
    body = json.loads(viya.calls("POST", r"/domains/vl-1/contents$")[-1][1]["content"])
    assert (body["majorNumber"], body["minorNumber"]) == (1, 1)


async def test_custom_functions(fake):
    mcp, viya = fake
    viya.add("GET", r"/functionCategories$", {"items": [], "count": 0})
    viya.add("POST", r"/functionCategories$", {"id": "cat-1", "name": "New Category"})
    viya.add("POST", r"/validations/functions$", lambda url, kw: (
        {"items": [{"message": "ERROR: Line 2: Parse failed."}]} if "broken" in json.loads(kw["content"])["code"]
        else {"items": []}
    ))
    viya.add("POST", r"/functionCategories/cat-1/functions$", {"id": "fn-1", "name": "f"})
    broken = await call(mcp, "create_custom_function", {"code": "method broken(", "category": "New Category"})
    assert "does not compile" in broken["error"]
    created = await call(mcp, "create_custom_function", {"code": "method f() returns double; return 1; end;",
                                                         "category": "New Category", "description": "d"})
    assert created["category"] == {"id": "cat-1", "name": "New Category", "created": True}
    viya.add("GET", r"/businessRules/functions/fn-1$", {"id": "fn-1", "name": "f", "categoryId": "cat-1", "code": "x"})
    viya.add("PUT", r"/businessRules/functions/fn-1$", lambda url, kw: json.loads(kw["content"]))
    updated = await call(
        mcp, "update_custom_function", {"function_id": "fn-1", "code": "method f() returns double; return 2; end;"}
    )
    assert "return 2" in updated["code"]
    unknown = await call(mcp, "validate_custom_function", {"code": "method f(); end;", "category": "Missing"})
    assert "No function category" in unknown["error"]
    valid = await call(mcp, "validate_custom_function", {"code": "method f(); end;"})
    assert valid == {"valid": True, "errors": [], "warnings": []}


async def test_flow_with_both_step_kinds_is_refused(fake):
    mcp, _ = fake
    result = await call(mcp, "create_decision_flow", {
        "name": "D", "signature": TREE_SIG, "steps": [], "rule_set_steps": [],
    })
    assert "exactly one of steps" in result["error"]
