# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the decisioning content helpers and tools (no network calls)."""

import json
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from conftest import _make_mock_response
from sas_mcp_server.helpers import decision_flow_helpers as flows
from sas_mcp_server.helpers import decisioning_assets as assets
from sas_mcp_server.helpers import segmentation_tree_helpers as trees

# --- segmentation trees: shorthand ----------------------------------------------


def test_normalize_node_accepts_shorthand():
    node = trees.normalize_node({
        "variable": "score",
        "valueNodes": [
            {"value": [1, 2.0, "3"], "outcome": "a"},
            {"value": "Other",
             "next": {"booleanExpressionName": "big", "valueNodes": [{"value": "true", "outcome": "b"}]}},
        ],
    })
    assert node["type"] == "variable" and node["variableName"] == "score"
    assert node["valueNodes"][0] == {"value": "1;2;3", "outcomeName": "a"}
    assert node["valueNodes"][1]["value"] == trees.OTHER
    assert node["valueNodes"][1]["next"]["type"] == "booleanExpression"


def test_outcome_shorthand_becomes_actions_and_code_files():
    outcomes, files = trees.normalize_outcomes(
        [{"name": "hi", "assign": {"band": "HIGH"}, "code": "flag = 1;"}], None
    )
    assert outcomes[0]["actions"] == [
        {"type": "variableAssignment", "variableName": "band", "valueType": "static", "value": "HIGH"},
        {"type": "codeAssignment", "codeFileName": "hi_code"},
    ]
    assert files == [{"name": "hi_code", "type": "internal", "code": "flag = 1;"}]


def test_tree_body_names_unknown_outcomes_and_variables():
    with pytest.raises(trees.TreeError) as exc:
        trees.tree_body(
            name="t",
            signature=[{"name": "score", "dataType": "decimal", "direction": "input"}],
            node={"variable": "scor", "valueNodes": [{"value": ">=1", "outcome": "nope"}]},
            outcomes=[{"name": "hi", "assign": {"band": "X"}}],
        )
    message = str(exc.value)
    assert "'scor'" in message and "'nope'" in message and "'band'" in message


# --- segmentation trees: from a strategy table --------------------------------------

ROWS = [  # branch, key a (lower bounds), key b, outcome
    {"Branch": 1, "A": 0, "B": 0, "Scenario": 10, "Group": "#N/A"},
    {"Branch": 2, "A": 0, "B": 5, "Scenario": 11, "Group": 3},
    {"Branch": 3, "A": 100, "B": 0, "Scenario": 12, "Group": 3},
    {"Branch": 4, "A": 600, "B": 0, "Scenario": 13, "Group": 4},
]


def _walk_values(node):
    return [vn["value"] for vn in node["valueNodes"]]


def test_lower_bound_tree_splits_on_sibling_bounds_and_skips_unsplit_keys():
    root, outcomes, signature, report = trees.build_tree_from_rows(
        ROWS, key_columns=["A", "B"], outcome_columns=["Scenario", "Group"], row_label_column="Branch",
    )
    assert root["variableName"] == "A"
    assert _walk_values(root) == ["v>=600", "100<=v<600", trees.OTHER]
    lowest = root["valueNodes"][2]
    assert lowest["next"]["variableName"] == "B"  # B splits only below 100
    assert _walk_values(lowest["next"]) == ["v>=5", trees.OTHER]
    assert root["valueNodes"][0]["outcomeName"] == "Scenario_13"  # B does not split above 600: no node
    assert report["leaves"] == 4 and report["split_nodes"] == 2 and report["conflicts"] == []
    assert [t["name"] for t in signature] == ["A", "B", "Scenario", "Group"]
    group = next(o for o in outcomes if o["name"] == "Scenario_10")
    assert [a["variableName"] for a in group["actions"]] == ["Scenario"]  # #N/A group is not assigned


def test_outcome_types_override_inference():
    _, outcomes, signature, _ = trees.build_tree_from_rows(
        ROWS, key_columns=["A", "B"], outcome_columns=["Scenario", "Group"], outcome_types={"Group": "string"},
    )
    assert next(t for t in signature if t["name"] == "Group")["dataType"] == "string"
    assert {"variableName": "Group", "valueType": "static", "value": "3", "type": "variableAssignment"} in next(
        o for o in outcomes if o["name"] == "Scenario_11"
    )["actions"]


def test_two_columns_can_test_one_variable_and_conflicts_are_reported():
    rows = [
        {"K1": 0, "K2": 0, "Out": 1},
        {"K1": 0, "K2": 0, "Out": 2},  # same path, other outcome
        {"K1": 5, "K2": 9, "Out": 3},
    ]
    root, _, signature, report = trees.build_tree_from_rows(
        rows, key_columns=[{"column": "K1", "variable": "score"}, {"column": "K2", "variable": "score"}],
        outcome_columns=["Out"],
    )
    assert [t["name"] for t in signature if t["direction"] == "input"] == ["score"]
    assert report["conflicts"] and report["conflicts"][0]["rows"] == [1, 2]


def test_exact_split_uses_each_value():
    rows = [{"Grade": "A", "Out": 1}, {"Grade": "B", "Out": 2}]
    root, _, signature, _ = trees.build_tree_from_rows(
        rows, key_columns=["Grade"], outcome_columns=["Out"], split="exact"
    )
    assert _walk_values(root) == ["A", "B"]
    assert signature[0]["dataType"] == "string"


def test_build_refuses_missing_columns_and_text_bounds():
    with pytest.raises(trees.TreeError, match="Columns not in the table"):
        trees.build_tree_from_rows(ROWS, key_columns=["A", "Nope"], outcome_columns=["Scenario"])
    with pytest.raises(trees.TreeError, match="not a number"):
        trees.build_tree_from_rows([{"A": "x", "O": 1}, {"A": 1, "O": 2}], key_columns=["A"], outcome_columns=["O"])


def test_csv_rows_dedupe_repeated_headers_and_value_lists_sort_by_bound():
    rows = trees.rows_from_csv_text("Score,Score,Out\n0,600,1\n5,630,2\n")
    assert list(rows[0]) == ["Score", "Score_2", "Out"]
    entries = trees.sorted_conditions({"v>=630": ">= 630", "600<=v<630": "600 to < 630", "100<=v<600": "100 to < 600"})
    assert [e["key"] for e in entries] == ["100<=v<600", "600<=v<630", "v>=630"]
    assert trees.ds2_name("% calc salary as CTO L12M") == "pctcalcsalaryasCTOL12M"


def test_excel_files_are_pointed_at_cas(tmp_path):
    book = tmp_path / "strategy.xlsx"
    book.write_bytes(b"PK")
    with pytest.raises(trees.TreeError, match="upload_data"):
        trees.rows_from_csv_file(str(book))


# --- assets helpers ---------------------------------------------------------------------


def test_signature_extension_is_compact_and_replaces_an_old_one():
    sig = [{"name": "score", "dataType": "decimal", "direction": "input", "id": "drop-me"}]
    code = '#/*{"signatureExtension":[{"name":"old"}]}*/\ndef execute(score):\n   return score\n'
    out = assets.with_signature_extension(code, "python", sig)
    first, rest = out.split("\n", 1)
    assert first == '#/*{"signatureExtension":[{"name":"score","dataType":"decimal","direction":"input"}]}*/'
    assert rest.startswith("def execute")  # the old extension is gone
    assert assets.with_signature_extension("select 1", "sql", sig) == "select 1"
    assert assets.with_signature_extension("x", "ds2", sig).startswith("/*{")


def test_value_list_entries_and_ds2_messages():
    entries = assets.normalize_entries(["A", {"key": "v>=630", "label": "high"}])
    assert entries == [
        {"key": "A", "value": "", "disabled": False, "order": 1},
        {"key": "v>=630", "value": "high", "disabled": False, "order": 2},
    ]
    log = "1 proc ds2;\nERROR: Compilation error.\nERROR: Line 3: Parse failed.\nWARNING: Line 4: undeclared x\n"
    assert assets.ds2_log_messages(log) == {
        "errors": ["ERROR: Line 3: Parse failed."], "warnings": ["WARNING: Line 4: undeclared x"]
    }
    with pytest.raises(assets.DecisioningError, match="Unknown asset_type"):
        assets.kind_for("tree")


async def test_lock_revision_reports_the_frozen_revision():
    client = AsyncMock()
    current = _make_mock_response({"name": "T", "signature": [], "node": {}, "id": "t-1", "links": []})
    listing = _make_mock_response({"items": [
        {"id": "rev-new", "majorRevision": 1, "minorRevision": 1},
        {"id": "rev-old", "majorRevision": 1, "minorRevision": 0},
    ], "count": 2})
    client.get.side_effect = [current, listing]
    client.post.return_value = _make_mock_response(
        {"id": "rev-new", "name": "T", "majorRevision": 1, "minorRevision": 1}
    )
    result = await assets.lock_revision(client, "segmentation_tree", "t-1")
    assert result["locked_revision"]["id"] == "rev-old" and result["locked_revision"]["locked"] is True
    assert result["working_revision"]["id"] == "rev-new"
    sent = json.loads(client.post.call_args[1]["content"])
    assert "id" not in sent and "links" not in sent  # server fields are not sent back
    assert client.post.call_args[1]["params"] == {"revisionType": "minor"}


async def test_delete_python_code_file_retries_without_locked_revisions():
    client = AsyncMock()
    client.delete.side_effect = [
        _make_mock_response({"message": "A data error occurred"}, status_code=500),
        _make_mock_response(status_code=204),  # the locked revision
        _make_mock_response(status_code=204),  # the code file
    ]
    client.get.return_value = _make_mock_response({"items": [{"id": "work"}, {"id": "locked"}], "count": 2})
    result = await assets.delete_asset(client, "code_file", "cf-1")
    assert result["locked_revisions_deleted_first"] == ["locked"]
    assert client.delete.call_args_list[1][0][0].endswith("/decisions/codeFiles/cf-1/revisions/locked")


# --- decision flows ---------------------------------------------------------------------

SIG = [
    {"name": "score", "dataType": "decimal", "direction": "input"},
    {"name": "band", "dataType": "string", "direction": "output"},
]


def _build(signature=None, add_missing=True):
    return flows._Build(AsyncMock(), [dict(t) for t in (signature or SIG)], add_missing=add_missing)


def test_mappings_by_name_rename_and_add_missing():
    build = _build()
    terms = [
        {"name": "SCORE", "dataType": "decimal", "direction": "input"},
        {"name": "label", "dataType": "string", "direction": "output"},
        {"name": "points", "dataType": "decimal", "direction": "output"},
    ]
    out = flows._mappings(build, terms, {"label": "band"}, "steps[0]")
    assert out == [
        {"stepTermName": "SCORE", "targetDecisionTermName": "score", "direction": "input"},
        {"stepTermName": "label", "targetDecisionTermName": "band", "direction": "output"},
        {"stepTermName": "points", "targetDecisionTermName": "points", "direction": "output"},
    ]
    assert build.report["variables_added"] == [
        {"step": "steps[0]", "name": "points", "dataType": "decimal", "direction": "output"}
    ]
    assert build.signature[-1]["name"] == "points"


def test_mappings_refuse_mismatched_types_and_warn_without_adding():
    build = _build()
    with pytest.raises(flows.DecisioningError, match="Make the types match"):
        flows._mappings(build, [{"name": "band", "dataType": "decimal", "direction": "output"}], None, "s")
    build = _build(add_missing=False)
    flows._mappings(build, [{"name": "rows", "dataType": "dataGrid", "direction": "output",
                             "dataGridExtension": [{"name": "n", "dataType": "integer"}]}], None, "s")
    assert "will not publish" in build.report["warnings"][0] and "['n']" in build.report["warnings"][0]


async def test_assignment_values_are_constants_and_branches_are_checked():
    build = _build()
    step = await flows._step(build, {"type": "assignment", "assignments": {"band": "'GOLD'", "score": 5}}, "s")
    assert step["assignments"] == [{"variableName": "band", "value": "GOLD"}, {"variableName": "score", "value": "5"}]
    eq = await flows._step(build, {"type": "branch", "branch_type": "equals", "variable": "band",
                                   "cases": [{"value": "HIGH", "steps": []}]}, "s")
    assert eq["branchCases"][0]["simpleCondition"]["rhsConstant"] == "'HIGH'"
    cases = [{"min": 690, "steps": []}, {"min": 630, "max": 689.99, "steps": []}]
    rng = await flows._step(build, {"type": "branch", "variable": "score", "cases": cases}, "s")
    assert rng["branchCases"][1]["compoundCondition"]["rhsSimpleCondition"]["operator"] == "<="
    with pytest.raises(flows.DecisioningError, match="overlap at 690"):
        await flows._step(build, {"type": "branch", "variable": "score",
                                  "cases": [{"min": 690, "steps": []}, {"min": 630, "max": 690, "steps": []}]}, "s")
    with pytest.raises(flows.DecisioningError, match="inclusively"):
        await flows._step(build, {"type": "branch", "variable": "score",
                                  "cases": [{"min": 1, "max_exclusive": True, "max": 2, "steps": []}]}, "s")


async def test_referenced_step_locks_a_revision_when_none_is_locked():
    client = AsyncMock()
    tree_sig = [{"name": "score", "dataType": "decimal", "direction": "input"},
                {"name": "band", "dataType": "string", "direction": "output"}]
    responses = {
        "revisions-1": _make_mock_response({"items": [{"id": "work"}], "count": 1}),  # only a working revision
        "current": _make_mock_response({"name": "Tree", "signature": tree_sig, "node": {}}),
        "revisions-2": _make_mock_response({"items": [{"id": "work2"}, {"id": "locked"}], "count": 2}),
        "revision": _make_mock_response({"name": "Tree", "signature": tree_sig}),
        "name": _make_mock_response({"name": "Tree (renamed)"}),
    }
    client.get.side_effect = [responses[k] for k in ("revisions-1", "current", "revisions-2", "revision", "name")]
    client.post.return_value = _make_mock_response({"id": "work2", "name": "Tree"})
    built, report = await flows.build_steps(
        client, [{"type": "segmentation_tree", "id": "t-1"}], [dict(t) for t in SIG]
    )
    step = built[0]
    assert step["customObject"] == {
        "uri": "/decisions/segmentationTrees/t-1/revisions/locked", "name": "Tree (renamed)",
        "type": "segmentationTree", "isRestDNT": True,
    }
    assert report["locked_for_this_flow"][0]["revision_id"] == "locked"
    assert len(step["mappings"]) == 2


# --- tools ------------------------------------------------------------------------------------


async def test_create_code_file_uploads_under_its_name_then_sets_the_description(
    mcp_server_with_mock_client, monkeypatch
):
    monkeypatch.setattr("sas_mcp_server.config.MCP_DEFAULT_FOLDER", "/folders/folders/f-1")
    mcp, mock_client = mcp_server_with_mock_client

    def get(url, *args, **kwargs):
        if url.endswith("/folders/folders/f-1"):
            return _make_mock_response({"id": "f-1", "name": "F"})
        if url.endswith("/folders/ancestors"):
            return _make_mock_response([{"name": "Public"}])
        resp = _make_mock_response(
            {"id": "cf-1", "name": "Score Band", "type": "decisionDS2CodeFile", "fileUri": "/files/files/x"}
        )
        resp.headers = {"etag": '"e"'}
        return resp

    mock_client.get.side_effect = get
    mock_client.post.side_effect = [
        _make_mock_response({"id": "file-1"}, status_code=201),
        _make_mock_response({"id": "cf-1", "name": "Score Band", "type": "decisionDS2CodeFile"}, status_code=201),
    ]
    mock_client.put.return_value = _make_mock_response({"id": "cf-1", "name": "Score Band", "description": "d"})
    async with Client(mcp) as client:
        result = (await client.call_tool("create_code_file", {
            "name": "Score Band", "code_type": "ds2", "code": "package x; endpackage;", "description": "d",
        })).data
    upload, create = mock_client.post.call_args_list
    assert upload[1]["headers"]["Content-Disposition"] == 'attachment; filename="Score Band.ds2"'
    assert json.loads(create[1]["content"])["fileUri"] == "/files/files/file-1"
    assert json.loads(mock_client.put.call_args[1]["content"])["description"] == "d"
    assert result["description"] == "d"


async def test_validate_decision_expression_returns_compiler_lines(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.post.return_value = _make_mock_response({"valid": False, "error": {
        "message": "1 proc ds2;\nERROR: Compilation error.\nERROR: Line 10: Unknown function risk_band.\n"}})
    async with Client(mcp) as client:
        result = (await client.call_tool("validate_decision_expression", {"expression": "risk_band(x)"})).data
    assert result["valid"] is False
    assert result["errors"] == ["ERROR: Line 10: Unknown function risk_band."]
    assert "few seconds" in result["note"]


async def test_create_tree_from_rows_checks_before_creating_value_lists(mcp_server_with_mock_client, monkeypatch):
    monkeypatch.setattr("sas_mcp_server.config.MCP_DEFAULT_FOLDER", "/folders/folders/f-1")
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.get.side_effect = lambda url, *a, **k: _make_mock_response(
        {"id": "f-1", "name": "F"} if url.endswith("/f-1") else [{"name": "Public"}]
    )
    async with Client(mcp) as client:
        result = await client.call_tool("create_segmentation_tree_from_table", {
            "name": "T", "key_columns": ["A"], "outcome_columns": ["Missing"], "rows": [{"A": 1}, {"A": 2}],
            "create_value_lists": True,
        }, raise_on_error=False)
    assert result.is_error and "Columns not in the table" in result.content[0].text
    mock_client.post.assert_not_called()


async def test_list_value_lists_filters_on_type(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    async with Client(mcp) as client:
        await client.call_tool("list_decisioning_assets", {"asset_type": "value_list", "filter_name": "SPID"})
    params = mock_client.get.call_args[1]["params"]
    assert params["filter"] == "and(eq(type,'valueList'),contains(name,'SPID'))"
