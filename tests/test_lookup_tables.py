# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit and tool tests for SAS Intelligent Decisioning lookup tables (no network calls)."""

import json
from unittest.mock import AsyncMock

import pytest

from sas_mcp_server.helpers import lookup_helpers as lookups
from sas_mcp_server.helpers.decisioning_assets import DecisioningError
from sas_mcp_server.tools.decisioning import plain_input_names
from sas_mcp_server.tools.model_scoring import match_input_names
from test_decisioning_content_tools import FakeViya, call

LK = "11111111-2222-3333-4444-555555555555"
DOMAIN = {"id": LK, "name": "Listed Countries", "domainType": "lookup", "description": "d",
          "packageName": "lpkg", "formatName": "$LFMT_K", "properties": None}
V1 = {"id": "c-1", "label": "1.0", "majorNumber": 1, "minorNumber": 0, "status": "production", "standing": "legacy",
      "keyLabel": "Country", "valueLabel": "Risk"}
V2 = {"id": "c-2", "label": "1.1", "majorNumber": 1, "minorNumber": 1, "status": "production", "standing": "current",
      "activationStatus": "active", "keyLabel": "Country", "valueLabel": "Risk"}


# --- entries ------------------------------------------------------------------------------


def test_normalize_entries_accepts_a_mapping_objects_and_pairs():
    assert lookups.normalize_entries({"Iran": "High", 7: 1.0, "flag": True}) == [
        {"key": "Iran", "value": "High"}, {"key": "7", "value": "1"}, {"key": "flag", "value": "true"},
    ]
    assert lookups.normalize_entries([{"key": "A", "value": 2.5}, ["B", "x"], ("C", "y")]) == [
        {"key": "A", "value": "2.5"}, {"key": "B", "value": "x"}, {"key": "C", "value": "y"},
    ]


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        ([{"key": " ", "value": "x"}], "a key is empty"),
        ([["A", "x"], ["A", "y"]], "'A' appears twice"),
        ({"K" * 257: "x"}, "longer than 256"),
        ({"A": "V" * 513}, "longer than 512"),
        ({"A": ""}, "has no value"),
        ([{"value": "x"}], "has no 'key'"),
        (["just a key"], "[key, value] pair"),
        ("A=1", "object of key to value"),
    ],
)
def test_normalize_entries_refuses_what_viya_cannot_use(entries, expected):
    with pytest.raises(DecisioningError) as exc:
        lookups.normalize_entries(entries)
    assert expected in str(exc.value)


def test_pointer_escapes_tilde_and_slash():
    assert lookups.pointer("a/b~c") == "/a~1b~0c"


# --- versions -----------------------------------------------------------------------------


def test_pick_version():
    versions = [lookups.version_summary(V1), lookups.version_summary(V2), {"id": "c-3", "label": "1.2"}]
    assert lookups.pick_version(versions, "current")["id"] == "c-2"
    assert lookups.pick_version(versions, "newest")["id"] == "c-3"
    assert lookups.pick_version(versions, "1.0")["id"] == "c-1"
    assert lookups.pick_version([{"id": "c-9", "label": "2.0"}], "current")["id"] == "c-9"  # none active
    assert lookups.pick_version([], "current") is None
    with pytest.raises(DecisioningError, match="No version '3.0'"):
        lookups.pick_version(versions, "3.0")


def test_version_summary_flattens_the_activation_error():
    summary = lookups.version_summary({
        **V2, "status": "activating", "activationStatus": "failed",
        "activationError": {"message": "1 of 2 failed", "errors": [{"message": "MAS publish timed out"}]},
    })
    assert summary["activationError"] == ["MAS publish timed out"]


def test_rule_problems_names_the_invalid_condition():
    rule = {"conditions": [{"status": "valid"}, {"status": "error", "term": {"name": "country"},
                                                  "statusMessage": "The lookup table ID is required.\n"}]}
    assert lookups.rule_problems(rule) == ["condition 2 (country): The lookup table ID is required."]


def test_match_input_names_maps_to_trailing_underscore_inputs():
    given = {"Score": 1, "band_": "A", "other": 2}
    assert match_input_names(given, ["score_", "band_", "other"]) == {"Score": "score_"}
    assert match_input_names({"x": 1, "x_": 2}, ["x_"]) == {}  # both given: leave it to MAS


# --- tools against a routed fake Viya ----------------------------------------------------------


class LookupViya(FakeViya):
    """FakeViya that also answers PATCH and knows one lookup table."""

    def __init__(self, mock_client):
        super().__init__(mock_client)
        mock_client.patch.side_effect = self._handler("PATCH")
        self.add("GET", rf"/referenceData/domains/{LK}$", DOMAIN)
        self.add("GET", rf"/domains/{LK}/contents$", {"items": [V2, V1], "count": 2})
        self.add("GET", r"/relationships/relationships$", {"items": [], "count": 0})


@pytest.fixture
def fake(mcp_server_with_mock_client, monkeypatch):
    monkeypatch.setattr("sas_mcp_server.config.MCP_DEFAULT_FOLDER", "/folders/folders/f-1")
    monkeypatch.setattr("sas_mcp_server.helpers.lookup_helpers.asyncio.sleep", AsyncMock())
    mcp, mock_client = mcp_server_with_mock_client
    return mcp, LookupViya(mock_client)


def _body(kw):
    return json.loads(kw["content"])


async def test_create_lookup_table_stores_entries_in_a_first_version(fake):
    mcp, viya = fake
    viya.add("POST", r"/referenceData/domains$", {**DOMAIN, "id": LK}, status=201)
    viya.add("POST", rf"/domains/{LK}/contents$", {**V1, "status": "developing", "standing": "future"}, status=201)
    result = await call(mcp, "create_lookup_table", {
        "name": "Listed Countries", "entries": {"Iran": "High", "Cuba": "High"}, "key_label": "Country",
    })
    (_, domain_kw), = viya.calls("POST", r"/referenceData/domains$")
    assert _body(domain_kw) == {"name": "Listed Countries", "description": "", "domainType": "lookup"}
    assert domain_kw["params"] == {"parentFolderUri": "/folders/folders/f-1"}
    assert domain_kw["headers"]["Content-Type"] == lookups.LOOKUP
    (_, content_kw), = viya.calls("POST", r"/contents$")
    sent = _body(content_kw)
    assert sent["entries"] == [{"key": "Iran", "value": "High"}, {"key": "Cuba", "value": "High"}]
    assert sent["keyLabel"] == "Country" and "valueLabel" not in sent and sent["status"] == "developing"
    assert content_kw["headers"]["Content-Type"] == lookups.LOOKUP_CONTENT_FULL
    assert result["entry_count"] == 2 and "Not active yet" in result["note"]
    assert result["folder"]["path"] == "/Public/Content/MCP Created"


async def test_create_refuses_bad_entries_before_any_request(fake):
    mcp, viya = fake
    result = await call(mcp, "create_lookup_table", {"name": "X", "entries": {"A": ""}})
    assert "has no value" in result["error"] and not viya.sent


async def test_create_removes_the_table_when_its_entries_are_refused(fake):
    mcp, viya = fake
    viya.add("POST", r"/referenceData/domains$", DOMAIN, status=201)
    viya.add("POST", rf"/domains/{LK}/contents$", {"message": "bad"}, status=400)
    viya.add("DELETE", rf"/referenceData/domains/{LK}$", {}, status=204)
    result = await call(mcp, "create_lookup_table", {"name": "X", "entries": {"A": "1"}})
    assert "error" in result and viya.calls("DELETE", rf"/referenceData/domains/{LK}$")


async def test_create_and_activate_waits_for_the_publish(fake):
    mcp, viya = fake
    active = {"status": "production", "activationStatus": "active", "standing": "current"}
    states = iter([{"status": "developing"}, {"status": "activating", "activationStatus": "running"}])
    viya.add("POST", r"/referenceData/domains$", DOMAIN, status=201)
    viya.add("POST", rf"/domains/{LK}/contents$", {**V1, "status": "developing", "standing": "future"}, status=201)
    viya.add("GET", r"/contents/c-1/entries$", {"items": [{"key": "A", "value": "1"}], "count": 1})
    viya.add("GET", r"/contents/c-1$", lambda url, kw: {**V1, **next(states, active)})
    viya.add("PUT", r"/contents/c-1$", {**V1, "status": "activating"}, status=202)
    result = await call(mcp, "create_lookup_table", {"name": "X", "entries": {"A": "1"}, "activate": True})
    (_, put_kw), = viya.calls("PUT", r"/contents/c-1$")
    assert _body(put_kw)["status"] == "production" and put_kw["params"] == {"asynchronous": "true"}
    assert set(_body(put_kw)) == {"label", "majorNumber", "minorNumber", "keyLabel", "valueLabel", "status"}
    assert result["activation"]["outcome"] == "active"


async def test_find_by_name_refuses_ambiguous_missing_and_value_lists(fake):
    mcp, viya = fake

    def domains(url, kw):
        flt = kw["params"]["filter"]
        if "eq(name,'Twice')" in flt:
            return {"items": [{"id": "a"}, {"id": "b"}], "count": 2}
        if "contains(name,'Nope')" in flt:
            return {"items": [{"id": "s", "name": "Nope Similar"}], "count": 1}
        return {"items": [], "count": 0}

    viya.add("GET", r"/referenceData/domains$", domains)
    vl = "99999999-2222-3333-4444-555555555555"
    viya.add("GET", rf"/referenceData/domains/{vl}$", {"id": vl, "name": "VL", "domainType": "valueList"})
    assert "use the ID: a, b" in (await call(mcp, "get_lookup_table", {"lookup_table": "Twice"}))["error"]
    assert "'Nope Similar'" in (await call(mcp, "get_lookup_table", {"lookup_table": "Nope"}))["error"]
    assert "not a lookup table" in (await call(mcp, "get_lookup_table", {"lookup_table": vl}))["error"]


async def test_get_lookup_table_pages_entries_and_lists_live_users(fake):
    mcp, viya = fake
    viya.add("GET", r"/contents/c-2/entries$", {"items": [{"key": "Iran", "value": "High"}], "count": 3})
    viya.add("GET", r"/relationships/relationships$", {"items": [
        {"type": "Dependent", "resourceUri": "/businessRules/ruleSets/rs-1/revisions/r-1"},
        {"type": "Dependent", "resourceUri": "/businessRules/ruleSets/rs-1"},
        {"type": "Dependent", "resourceUri": "/businessRules/ruleSets/gone"},
        {"type": "Associated", "resourceUri": "/folders/folders/f-1"},
    ]})
    viya.add("GET", r"/ruleSets/rs-1$", {"id": "rs-1", "name": "Denylist Check"})
    viya.add("GET", r"/ruleSets/gone$", {}, status=404)
    result = await call(mcp, "get_lookup_table", {"lookup_table": LK, "limit": 1, "key_filter": "Ir"})
    assert result["active_version"] == "1.1" and result["version"]["id"] == "c-2"
    assert [v["label"] for v in result["versions"]] == ["1.0", "1.1"]
    assert result["entries"] == [{"key": "Iran", "value": "High"}] and result["next_start"] == 1
    assert result["used_by"] == [{"type": "rule_set", "id": "rs-1", "name": "Denylist Check"}]
    (_, entries_kw), = viya.calls("GET", r"/contents/c-2/entries$")
    assert entries_kw["params"]["filter"] == "contains(key,'Ir')"
    assert result["folder"]["path"] == "/Public/Content/MCP Created"


async def test_update_starts_a_version_from_the_active_one_and_patches_it(fake):
    mcp, viya = fake
    viya.add("POST", rf"/domains/{LK}/contents$", {"id": "c-3", "label": "1.2", "majorNumber": 1, "minorNumber": 2,
                                                   "status": "developing", "standing": "future"}, status=201)
    viya.add("GET", r"/contents/c-3/entries$", {"items": [{"key": "Iran", "value": "High"},
                                                         {"key": "a/b", "value": "x"}], "count": 2})
    viya.add("PATCH", r"/contents/c-3/entries$", {"items": []})
    result = await call(mcp, "update_lookup_table", {
        "lookup_table": LK, "set_entries": {"Iran": "Very High", "Cuba": "High"}, "remove_keys": ["a/b", "Nowhere"],
    })
    (_, new_kw), = viya.calls("POST", rf"/domains/{LK}/contents$")
    assert new_kw["params"]["inheritContentEntriesUri"] == f"/referenceData/domains/{LK}/contents/c-2"
    assert _body(new_kw)["minorNumber"] == 2 and _body(new_kw)["keyLabel"] == "Country"
    (_, patch_kw), = viya.calls("PATCH", r"/contents/c-3/entries$")
    assert _body(patch_kw) == [
        {"op": "remove", "path": "/a~1b"},
        {"op": "add", "path": "/Iran", "value": "Very High"},
        {"op": "add", "path": "/Cuba", "value": "High"},
    ]
    assert patch_kw["headers"]["If-Match"] == '"e1"'
    assert result["changes"] == {"added": ["Cuba"], "changed": ["Iran"], "removed": ["a/b"], "not_found": ["Nowhere"]}
    assert result["new_version"] is True and result["started_from"] == "1.1"
    assert "activate_lookup_table" in result["note"]


async def test_update_replaces_entries_of_a_version_in_development(fake):
    mcp, viya = fake
    developing = {"id": "c-3", "label": "1.2", "majorNumber": 1, "minorNumber": 2, "status": "developing"}
    viya.add("GET", rf"/domains/{LK}/contents$", {"items": [V1, V2, developing], "count": 3})
    viya.add("GET", r"/contents/c-3/entries$", {"items": [], "count": 1})
    viya.add("POST", r"/contents/c-3/entries$", {"items": []})
    viya.add("GET", r"/contents/c-3$", developing)
    viya.add("PUT", r"/contents/c-3$", {**developing, "valueLabel": "Level"})
    viya.add("PUT", rf"/referenceData/domains/{LK}$", {**DOMAIN, "name": "Renamed"})
    result = await call(mcp, "update_lookup_table", {
        "lookup_table": LK, "replace_entries": [["Iran", "High"]], "value_label": "Level", "name": "Renamed",
    })
    assert not viya.calls("POST", rf"/domains/{LK}/contents$")  # no new version
    (_, post_kw), = viya.calls("POST", r"/contents/c-3/entries$")
    assert _body(post_kw) == [{"key": "Iran", "value": "High"}]
    (_, label_kw), = viya.calls("PUT", r"/contents/c-3$")
    assert _body(label_kw)["valueLabel"] == "Level"
    (_, domain_kw), = viya.calls("PUT", rf"/referenceData/domains/{LK}$")
    assert _body(domain_kw)["name"] == "Renamed" and _body(domain_kw)["domainType"] == "lookup"
    assert result["name"] == "Renamed" and result["new_version"] is False
    assert result["changes"] == {"replaced_with": 1}


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"replace_entries": {"A": "1"}, "set_entries": {"B": "2"}}, "not both"),
        ({"replace_entries": []}, "at least one entry"),
    ],
)
async def test_update_refuses_conflicting_or_empty_entries(fake, args, expected):
    mcp, _ = fake
    assert expected in (await call(mcp, "update_lookup_table", {"lookup_table": LK, **args}))["error"]


async def test_activate_an_earlier_version_activates_a_copy_of_it(fake):
    mcp, viya = fake
    viya.add("POST", rf"/domains/{LK}/contents$", {"id": "c-3", "label": "1.2", "majorNumber": 1, "minorNumber": 2,
                                                   "status": "developing"}, status=201)
    viya.add("GET", r"/contents/c-3/entries$", {"items": [{"key": "A", "value": "1"}], "count": 1})
    viya.add("GET", r"/contents/c-3$", {"id": "c-3", "label": "1.2", "status": "production",
                                        "standing": "current", "activationStatus": "active"})
    viya.add("PUT", r"/contents/c-3$", {}, status=202)
    result = await call(mcp, "activate_lookup_table", {"lookup_table": LK, "version": "1.0"})
    (_, new_kw), = viya.calls("POST", rf"/domains/{LK}/contents$")
    assert new_kw["params"]["inheritContentEntriesUri"].endswith("/contents/c-1")
    assert result["outcome"] == "active" and result["started_from"] == "1.0" and "1.2" in result["note"]


async def test_activate_reports_failure_and_already_active(fake):
    mcp, viya = fake
    developing = {"id": "c-3", "label": "1.2", "majorNumber": 1, "minorNumber": 2, "status": "developing"}
    viya.add("GET", rf"/domains/{LK}/contents$", {"items": [V1, V2, developing], "count": 3})
    viya.add("GET", r"/contents/c-3/entries$", {"items": [{"key": "A", "value": "1"}], "count": 1})
    viya.add("GET", r"/contents/c-3$", {**developing, "status": "activating", "activationStatus": "failed",
                                        "activationError": {"errors": [{"message": "MAS timed out"}]}})
    viya.add("PUT", r"/contents/c-3$", {}, status=202)
    failed = await call(mcp, "activate_lookup_table", {"lookup_table": LK})
    assert failed["outcome"] == "failed" and failed["version"]["activationError"] == ["MAS timed out"]
    active = await call(mcp, "activate_lookup_table", {"lookup_table": LK, "version": "1.1"})
    assert active["outcome"] == "already_active"


async def test_activate_refuses_an_empty_version_and_republishes_the_active_one(fake):
    mcp, viya = fake
    developing = {"id": "c-3", "label": "1.2", "majorNumber": 1, "minorNumber": 2, "status": "developing"}
    viya.add("GET", rf"/domains/{LK}/contents$", {"items": [V1, V2, developing], "count": 3})
    viya.add("GET", r"/contents/c-3/entries$", {"items": [], "count": 0})
    assert "has no entries" in (await call(mcp, "activate_lookup_table", {"lookup_table": LK}))["error"]
    viya.add("GET", rf"/domains/{LK}/currentContents$", {"items": []})
    viya.add("PATCH", rf"/domains/{LK}/currentContents$", {})
    result = await call(mcp, "activate_lookup_table", {"lookup_table": LK, "republish": True})
    paths = [_body(kw)[0]["path"] for _, kw in viya.calls("PATCH", r"/currentContents$")]
    assert paths == ["/@current/environments/cas", "/@current/environments/mas"]
    assert result["outcome"] == "republished" and result["version"]["label"] == "1.1"


async def test_life_cycle_tools_list_copy_and_guard_deletes(fake):
    mcp, viya = fake
    viya.add("GET", r"/referenceData/domains$", {"items": [{"id": LK, "name": "Listed Countries"}], "count": 1})
    listed = await call(mcp, "list_decisioning_assets", {"asset_type": "lookup_table"})
    (_, list_kw), = viya.calls("GET", r"/referenceData/domains$")
    assert list_kw["params"]["filter"] == "eq(type,'lookup')" and listed[0]["id"] == LK

    viya.add("GET", r"/contents/c-2/entries$", {"items": [{"key": "Iran", "value": "High"}], "count": 1})
    viya.add("POST", r"/referenceData/domains$", {**DOMAIN, "id": "new"}, status=201)
    viya.add("POST", r"/domains/new/contents$", {**V1, "status": "developing"}, status=201)
    copied = await call(mcp, "copy_decisioning_asset", {"asset_type": "lookup_table", "asset_id": LK, "name": "Copy"})
    (_, copy_kw), = viya.calls("POST", r"/domains/new/contents$")
    assert _body(copy_kw)["entries"] == [{"key": "Iran", "value": "High"}] and _body(copy_kw)["keyLabel"] == "Country"
    assert "not active" in copied["note"]

    viya.add("GET", r"/relationships/relationships$", {"items": [
        {"type": "Dependent", "resourceUri": "/businessRules/ruleSets/rs-1"}]})
    viya.add("GET", r"/ruleSets/rs-1$", {"id": "rs-1", "name": "Denylist Check"})
    refused = await call(mcp, "delete_decisioning_asset", {"asset_type": "lookup_table", "asset_id": LK})
    assert "'Denylist Check' (rs-1)" in refused["error"] and not viya.calls("DELETE", r"/domains/")

    viya.add("GET", r"/relationships/relationships$", {"items": []})
    viya.add("DELETE", rf"/referenceData/domains/{LK}$", {}, status=204)
    deleted = await call(mcp, "delete_decisioning_asset", {"asset_type": "lookup_table", "asset_id": LK})
    (_, delete_kw), = viya.calls("DELETE", rf"/referenceData/domains/{LK}$")
    assert delete_kw["params"] == {"deleteProductionContent": "true"} and deleted["deleted"] is True


async def test_rules_get_the_lookup_id_and_report_invalid_parts(fake):
    mcp, viya = fake
    viya.add("GET", r"/referenceData/domains$", {"items": [{"id": LK}], "count": 1})
    viya.add("POST", r"/ruleSets/rs-1/rules$", lambda url, kw: {
        **kw["json"], "id": "rule-1",
        "conditions": [{**kw["json"]["conditions"][0], "status": "valid"}],
        "actions": [{"status": "error", "statusMessage": "Unknown variable", "term": {"name": "risk"}}],
    })
    term = {"name": "country", "dataType": "string", "direction": "input"}
    result = await call(mcp, "create_business_rule", {
        "ruleset_id": "rs-1", "name": "listed", "conditional": "if", "rule_fired_tracking_enabled": True,
        "conditions": [{"type": "lookup", "term": term, "lookup": "Listed Countries"}],
        "actions": [{"type": "lookupValue", "term": {**term, "name": "risk"}, "expression": "country",
                     "lookup": {"name": "Listed Countries"}}],
    })
    (_, rule_kw), = viya.calls("POST", r"/ruleSets/rs-1/rules$")
    assert rule_kw["json"]["conditions"][0]["lookup"] == {"id": LK, "name": "Listed Countries"}
    assert rule_kw["json"]["actions"][0]["lookup"] == {"id": LK, "name": "Listed Countries"}
    assert len(viya.calls("GET", r"/referenceData/domains$")) == 1  # the name is resolved once
    assert result["problems"] == ["action 1 (risk): Unknown variable"]
    missing = await call(mcp, "create_business_rule", {
        "ruleset_id": "rs-1", "name": "x", "conditional": "if", "rule_fired_tracking_enabled": True,
        "conditions": [{"type": "lookup", "term": term}], "actions": [],
    })
    assert "needs 'lookup'" in missing["error"]


async def test_score_data_maps_plain_names_to_underscored_inputs(fake):
    mcp, viya = fake
    viya.add("GET", r"/steps/execute$", {"inputs": [{"name": "country_"}]})
    viya.add("POST", r"/steps/execute$", {"outputs": [{"name": "risk", "value": "High"}]})
    scored = viya.routes[0][2]
    viya.add("POST", r"/steps/execute$", {"message": "The value of country_ must not be empty."}, status=400)
    refused = viya.routes[0][2]
    viya.routes[0] = ("POST", viya.routes[0][1], lambda url, kw: (
        scored if kw["json"]["inputs"][0]["name"] == "country_" else refused)(url, kw))
    result = await call(mcp, "score_data", {
        "module_id": "decision_1", "step_id": "execute", "input_data": {"country": "Iran"},
    })
    assert [kw["json"]["inputs"][0]["name"] for _, kw in viya.calls("POST", r"/steps/execute$")] == [
        "country", "country_"]
    assert result["inputs_renamed"] == {"country": "country_"}
    assert result["outputs"] == [{"name": "risk", "value": "High"}]


async def test_score_data_does_not_retry_when_no_name_maps(fake):
    mcp, viya = fake
    viya.add("GET", r"/steps/execute$", {"inputs": [{"name": "score"}]})
    viya.add("POST", r"/steps/execute$", {"message": "bad value"}, status=400)
    result = await call(mcp, "score_data", {"module_id": "m", "step_id": "execute", "input_data": {"score": "x"}})
    assert "error" in result and len(viya.calls("POST", r"/steps/execute$")) == 1


# --- publishing with the decision's input names --------------------------------------------

MAS_WRAPPER = '''package Decision_1 / inline;
    method execute_internal(varchar(100) "country", in_out varchar "risk", in_out integer "_filter_");
        "rules_hdl".execute("country", "risk");
    end;
    method execute(
       varchar(100) "country_"
      ,double "score_"
      ,in_out varchar "risk"
      ,in_out package datagrid "rows"
);
      dcl integer _filter_;
      execute_internal(
          "country_"
         ,"score_"
         ,"risk"
         ,"rows"
      ,"_filter_"
      );
    end;
      endpackage;
'''


def test_plain_input_names_renames_only_the_wrapper_inputs():
    code, renamed = plain_input_names(MAS_WRAPPER)
    assert renamed == ["country", "score"]
    wrapper = code[code.rindex("method execute("):]
    assert '"country_"' not in wrapper and '"score_"' not in wrapper
    assert 'varchar(100) "country"' in wrapper and '"risk"' in wrapper and '"rows"' in wrapper
    assert code[:code.rindex("method execute(")] == MAS_WRAPPER[:MAS_WRAPPER.rindex("method execute(")]


def test_plain_input_names_leaves_a_clash_and_unknown_shapes_alone():
    clash = MAS_WRAPPER.replace('in_out varchar "risk"\n', 'in_out varchar "risk"\n      ,in_out double "score"\n', 1)
    _, renamed = plain_input_names(clash)
    assert renamed == ["country"]  # "score" is taken by an output
    assert plain_input_names("package x; method execute(double a_); end; endpackage;") == (
        "package x; method execute(double a_); end; endpackage;", [])
    odd = MAS_WRAPPER.replace('double "score_"', 'double score_')
    assert plain_input_names(odd) == (odd, [])
