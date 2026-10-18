# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Decision flows from steps a caller can write: what each step is, not how it is wired.

A decision flow step in the API names its object by a revision URI and lists a
mapping for every term it reads or writes. :func:`build_steps` takes steps like

    {"type": "segmentation_tree", "id": "<tree id>"}
    {"type": "condition", "expression": "score >= 630", "on_true": [...], "on_false": [...]}

and fills in the rest: the revision to reference, the object's name and type,
and mappings by name to the decision's own variables. Step types:

* ``rule_set``, ``segmentation_tree``, ``code_file``, ``decision`` (a
  sub-decision) — ``id``, optional ``revision`` and ``mappings``;
* ``model`` — ``id`` of a model in the model repository, optional ``mappings``;
* ``assignment`` — ``assignments`` as ``{"variable": value}``. The values are
  constants, written plainly (``"GOLD"``, ``720``): the service quotes a string
  itself and evaluates nothing, so ``"cats(a, b)"`` is stored as that text. A
  string wrapped in single quotes loses one level of them. Compute values in a
  rule set or code file;
* ``condition`` — ``expression`` with ``on_true`` / ``on_false`` steps;
* ``branch`` — ``branch_type`` ``range``, ``equals`` or ``like``, ``cases``
  each with ``steps``, and ``default`` steps. Cases are checked in order and
  the first that matches runs. A range compares inclusively, ``>= min`` and
  ``<= max`` (the service rejects ``>`` and ``<``), and ranges must not share
  a value — Intelligent Decisioning's validation rejects ``max 690`` next to
  ``min 690`` — so end the lower range just below: ``max 689.99``;
* any API step (``type`` starting with ``application/vnd.sas.decision.step.``)
  passes through unchanged.

``revision`` is ``"latest_locked"`` (the default: the newest locked revision,
locking one first if there is none), ``"working"`` (the editable revision —
later edits then change the decision too), or a revision ID.

Mappings default to same-named decision variables, case-insensitively. A
``mappings`` object renames: ``{"step term": "decision variable"}``. A step term
no decision variable matches is added to the decision as a variable of the same
type and direction (``variables_added`` in the report): Intelligent Decisioning
reports every unmapped term as an invalid mapping, and an unmapped output that
is not a number stops the decision from publishing. Without that, a step input
nothing maps to is reported, since the step would read an empty value.
The mapping direction is the term's own (input, output or inOut), as the
service expects. A step term is never mapped to a variable of another type
family (number, string, date, datetime, data grid): the service would accept
the decision and fail only at publish. With variables not added, every
output that is not a number still has to be mapped — a model's class label, a
SQL code file's result rows (a dataGrid variable) — since the service passes a
numeric placeholder for an unmapped output; ``warnings`` lists them.

Probed against SAS Viya in October 2026: every step type above, in one
decision, validates in Intelligent Decisioning, publishes to MAS and scores.
"""

from dataclasses import dataclass, field
from typing import Any

import httpx

from .. import config
from ..viya_client import raise_for_viya_status
from .decisioning_assets import ASSET_KINDS, DecisioningError, list_revisions, lock_revision

JSONDict = dict[str, Any]
STEP = "application/vnd.sas.decision.step."
_REFERENCED = ("rule_set", "segmentation_tree", "code_file", "decision")


@dataclass
class _Build:
    client: httpx.AsyncClient
    signature: list[JSONDict]
    add_missing: bool = True
    report: JSONDict = field(
        default_factory=lambda: {
            "steps": [], "variables_added": [], "unmapped_inputs": [], "warnings": [], "locked_for_this_flow": []
        }
    )

    @property
    def names(self) -> dict[str, str]:
        return {t["name"].lower(): t["name"] for t in self.signature}

    def data_type(self, variable: str) -> str | None:
        found = next((t for t in self.signature if t["name"].lower() == variable.lower()), None)
        return found.get("dataType") if found else None


async def _get(client: httpx.AsyncClient, path: str) -> JSONDict:
    resp = await client.get(f"{config.VIYA_ENDPOINT}{path}", headers={"Accept": "application/json"})
    raise_for_viya_status(resp)
    return resp.json()


async def _revision(build: _Build, asset_type: str, asset_id: str, revision: str | None) -> tuple[str, JSONDict]:
    kind = ASSET_KINDS[asset_type]
    choice = (revision or "latest_locked").strip()
    if choice in ("latest_locked", "working", "current"):
        revisions = await list_revisions(build.client, asset_type, asset_id, limit=50)
        if not revisions:
            raise DecisioningError(f"The {kind.what} {asset_id} has no revisions.")
        if choice == "latest_locked":
            locked = [r for r in revisions if r["locked"]]
            if locked:
                revision_id = locked[0]["id"]
            else:
                result = await lock_revision(build.client, asset_type, asset_id)
                revision_id = result["locked_revision"]["id"]
                build.report["locked_for_this_flow"].append(
                    {"asset_type": asset_type, "id": asset_id, "name": result.get("name"), "revision_id": revision_id}
                )
        else:
            revision_id = revisions[0]["id"]
    else:
        revision_id = choice
    content = await _get(build.client, f"{kind.base}/{asset_id}/revisions/{revision_id}")
    return revision_id, content


# DS2 passes every number as a double, so integer, decimal and boolean terms
# map to one another; a string or a date never maps to a number. A mismatch is
# accepted when the flow is saved and only fails at publish ("the type of the
# argument must be identical"), so it is refused here instead.
_TYPE_FAMILY = {"integer": "number", "decimal": "number", "boolean": "number", "string": "string",
                "date": "date", "datetime": "datetime", "dataGrid": "dataGrid"}


def _family(data_type: str | None) -> str | None:
    return _TYPE_FAMILY.get(data_type or "")


def _mappings(build: _Build, terms: list[JSONDict], spec: Any, where: str) -> list[JSONDict]:
    explicit: dict[str, str] = {}
    if isinstance(spec, dict):
        explicit = {str(k): str(v) for k, v in spec.items()}
    elif isinstance(spec, list):
        if spec and all(isinstance(m, dict) and "stepTermName" in m for m in spec):
            return spec  # already in the API's shape
        for m in spec:
            step_term = m.get("step_term") or m.get("stepTermName")
            target = m.get("decision_term") or m.get("variable") or m.get("targetDecisionTermName")
            if not step_term or not target:
                raise DecisioningError(f"{where}: a mapping needs step_term and decision_term: {m!r}.")
            explicit[step_term] = target
    by_name = {t["name"]: t for t in terms}
    unknown = [k for k in explicit if k not in by_name]
    if unknown:
        raise DecisioningError(f"{where}: {', '.join(unknown)} not in the step's signature ({', '.join(by_name)}).")
    names = build.names
    out = []
    for term in terms:
        target = explicit.get(term["name"]) or names.get(term["name"].lower())
        direction = term.get("direction") or "input"
        if target is None and build.add_missing:
            # Intelligent Decisioning validates a step only when every term is mapped, so a term the
            # decision has no variable for becomes one, of the same type and direction.
            keys = ("name", "dataType", "direction", "length", "dataGridExtension", "description")
            added = {k: term[k] for k in keys if term.get(k) not in (None, "")}
            added.setdefault("dataType", "decimal")
            added["direction"] = direction if direction in ("input", "output", "inOut") else "output"
            build.signature.append(added)
            build.report["variables_added"].append(
                {"step": where, "name": term["name"], "dataType": added["dataType"], "direction": added["direction"]}
            )
            names = build.names
            target = term["name"]
        if target is None:
            if direction in ("input", "inOut"):
                build.report["unmapped_inputs"].append({"step": where, "term": term["name"]})
            elif _family(term.get("dataType")) not in ("number", None):
                # The service passes a numeric placeholder for an unmapped output: saving accepts
                # it, publishing fails ("requires varchar(12), found double" / "requires package").
                hint = ""
                if term.get("dataType") == "dataGrid":
                    columns = [c.get("name") for c in term.get("dataGridExtension") or []]
                    hint = f" (a dataGrid variable with dataGridExtension columns {columns})"
                build.report["warnings"].append(
                    f"{where}: the {term.get('dataType')} output {term['name']!r} is not mapped, so the decision "
                    f"will not publish. Add a decision variable of that type{hint} and map it."
                )
            continue
        if target.lower() not in names:
            raise DecisioningError(f"{where}: maps {term['name']!r} to {target!r}, which is not a decision variable.")
        step_family, decision_family = _family(term.get("dataType")), _family(build.data_type(target))
        if step_family and decision_family and step_family != decision_family:
            raise DecisioningError(
                f"{where}: {term['name']!r} is {term.get('dataType')} in the step but the decision variable "
                f"{names[target.lower()]!r} is {build.data_type(target)}. "
                "Make the types match, or map another variable."
            )
        out.append(
            {"stepTermName": term["name"], "targetDecisionTermName": names[target.lower()], "direction": direction}
        )
    return out


def _steps_list(value: Any, where: str) -> list[JSONDict]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise DecisioningError(f"{where}: expected a list of steps.")
    return value


def _constant(build: _Build, variable: str | None, value: Any) -> str:
    text = str(value)
    if variable and build.data_type(variable) == "string" and not (text.startswith("'") and text.endswith("'")):
        return "'" + text.replace("'", "''") + "'"
    return text


def _check_disjoint(cases: list[JSONDict], where: str) -> None:
    """Intelligent Decisioning's validation rejects range cases that share a value, endpoints included."""
    spans = []
    for i, case in enumerate(cases):
        if "condition" in case:
            continue
        low = float(case["min"]) if case.get("min") is not None else float("-inf")
        high = float(case["max"]) if case.get("max") is not None else float("inf")
        spans.append((low, high, case.get("label") or f"Case {i + 1}"))
    spans.sort()
    for (_, high, label), (low, _, next_label) in zip(spans, spans[1:], strict=False):
        if low <= high:
            raise DecisioningError(
                f"{where}: the ranges {label!r} and {next_label!r} overlap at {low:g}. Range cases are inclusive "
                "and must not share a value, so end the lower one just below the next min (e.g. max 689.99 under 690)."
            )


def _case_condition(build: _Build, branch_type: str, variable: str | None, case: JSONDict, where: str) -> JSONDict:
    if "condition" in case:
        raw = case["condition"]
        return {"compoundCondition": raw} if "booleanOperator" in raw else {"simpleCondition": raw}
    variable = case.get("variable") or variable
    if not variable:
        raise DecisioningError(f"{where}: name the variable the branch tests (on the branch or the case).")
    term = {"name": build.names.get(variable.lower(), variable)}
    if branch_type == "range":
        low, high = case.get("min"), case.get("max")
        if low is None and high is None:
            raise DecisioningError(f"{where}: a range case needs min, max or both.")
        if case.get("min_exclusive") or case.get("max_exclusive"):
            raise DecisioningError(
                f"{where}: range branches compare inclusively (>= min, <= max); the service rejects > and <. "
                "End the lower range just below the next one's min, e.g. max 689.99 under min 690."
            )
        parts = []
        if low is not None:
            parts.append({"lhsTerm": term, "operator": ">=", "rhsConstant": str(low)})
        if high is not None:
            parts.append({"lhsTerm": term, "operator": "<=", "rhsConstant": str(high)})
        if len(parts) == 1:
            return {"simpleCondition": parts[0]}
        compound = {"booleanOperator": "AND", "lhsSimpleCondition": parts[0], "rhsSimpleCondition": parts[1]}
        return {"compoundCondition": compound}
    key = "value" if branch_type == "equals" else "pattern"
    if key not in case:
        raise DecisioningError(f"{where}: an {branch_type} case needs {key!r}.")
    operator = "=" if branch_type == "equals" else "like"
    constant = _constant(build, variable, case[key])
    return {"simpleCondition": {"lhsTerm": term, "operator": operator, "rhsConstant": constant}}


async def _step(build: _Build, spec: JSONDict, where: str) -> JSONDict:
    if not isinstance(spec, dict) or "type" not in spec:
        raise DecisioningError(f"{where}: every step needs a type.")
    kind = str(spec["type"])
    if kind.startswith(STEP):
        build.report["steps"].append({"step": where, "type": kind.removeprefix(STEP), "as_given": True})
        return spec
    if kind in _REFERENCED:
        if not spec.get("id"):
            raise DecisioningError(f"{where}: a {kind} step needs the id of the {ASSET_KINDS[kind].what}.")
        revision_id, content = await _revision(build, kind, spec["id"], spec.get("revision"))
        # Label the step with the object's current name: a revision keeps the name it had when locked.
        name = (await _get(build.client, f"{ASSET_KINDS[kind].base}/{spec['id']}")).get("name") or content.get("name")
        mappings = _mappings(build, content.get("signature") or [], spec.get("mappings"), where)
        build.report["steps"].append({"step": where, "type": kind, "name": name, "revision_id": revision_id,
                                      "mapped": len(mappings)})
        if kind == "rule_set":
            return {"type": STEP + "ruleset", "ruleset": {"id": spec["id"], "versionId": revision_id, "name": name},
                    "mappings": mappings}
        custom = {"uri": f"{ASSET_KINDS[kind].base}/{spec['id']}/revisions/{revision_id}", "name": name}
        if kind == "segmentation_tree":
            custom.update(type="segmentationTree", isRestDNT=True)
        elif kind == "decision":
            custom["type"] = "decision"
        else:
            custom["type"] = content.get("type") or "decisionDS2CodeFile"
        return {"type": STEP + "custom.object", "customObject": custom, "mappings": mappings}
    if kind == "model":
        if not spec.get("id"):
            raise DecisioningError(f"{where}: a model step needs the model id.")
        model = await _get(build.client, f"/modelRepository/models/{spec['id']}")
        resp = await build.client.get(
            f"{config.VIYA_ENDPOINT}/modelRepository/models/{spec['id']}/variables",
            params={"limit": 1000}, headers={"Accept": "application/vnd.sas.collection+json"},
        )
        raise_for_viya_status(resp)
        terms = [
            {"name": v["name"], "direction": "output" if v.get("role") == "output" else "input",
             "dataType": "string" if v.get("type") == "string" else "decimal"}
            for v in resp.json().get("items", []) if v.get("role") in ("input", "output")
        ]
        mappings = _mappings(build, terms, spec.get("mappings"), where)
        build.report["steps"].append(
            {"step": where, "type": "model", "name": model.get("name"), "mapped": len(mappings)}
        )
        return {"type": STEP + "model", "model": {"id": spec["id"], "name": model.get("name")}, "mappings": mappings}
    if kind == "assignment":
        given = spec.get("assignments") or {}
        pairs = list(given.items()) if isinstance(given, dict) else [
            (a.get("variable") or a.get("variableName"), a.get("value")) for a in given
        ]
        names = build.names
        assignments = []
        for variable, value in pairs:
            if not variable or str(variable).lower() not in names:
                raise DecisioningError(f"{where}: assigns {variable!r}, which is not a decision variable.")
            text = str(value)
            if build.data_type(str(variable)) == "string" and len(text) >= 2 and text[0] == text[-1] == "'":
                text = text[1:-1].replace("''", "'")  # the service adds the quotes itself
            assignments.append({"variableName": names[str(variable).lower()], "value": text})
        if not assignments:
            raise DecisioningError(f"{where}: an assignment step needs assignments.")
        build.report["steps"].append({"step": where, "type": "assignment", "assigned": len(assignments)})
        step = {"type": STEP + "variable.assignment", "name": spec.get("name") or "Assign variables",
                "assignments": assignments}
        if spec.get("description"):
            step["description"] = spec["description"]
        return step
    if kind == "condition":
        if not spec.get("expression"):
            raise DecisioningError(f"{where}: a condition step needs an expression.")
        build.report["steps"].append({"step": where, "type": "condition"})
        return {
            "type": STEP + "condition",
            "name": spec.get("name") or spec["expression"],
            "conditionExpression": spec["expression"],
            "onTrue": {"steps": [await _step(build, s, f"{where}.on_true[{i}]")
                                 for i, s in enumerate(_steps_list(spec.get("on_true"), where))]},
            "onFalse": {"steps": [await _step(build, s, f"{where}.on_false[{i}]")
                                  for i, s in enumerate(_steps_list(spec.get("on_false"), where))]},
        }
    if kind == "branch":
        branch_type = spec.get("branch_type", "range")
        if branch_type not in ("range", "equals", "like"):
            raise DecisioningError(f"{where}: branch_type must be range, equals or like.")
        if branch_type == "range":
            _check_disjoint(spec.get("cases") or [], where)
        cases = []
        for i, case in enumerate(spec.get("cases") or []):
            here = f"{where}.cases[{i}]"
            converted = {"label": case.get("label") or f"Case {i + 1}",
                         **_case_condition(build, branch_type, spec.get("variable"), case, here)}
            converted["onTrue"] = {"steps": [await _step(build, s, f"{here}.steps[{j}]")
                                             for j, s in enumerate(_steps_list(case.get("steps"), here))]}
            cases.append(converted)
        if not cases:
            raise DecisioningError(f"{where}: a branch needs at least one case.")
        build.report["steps"].append({"step": where, "type": "branch", "cases": len(cases)})
        return {
            "type": STEP + "branch",
            "name": spec.get("name") or "Branch",
            "branchType": branch_type,
            "branchCases": cases,
            "defaultCase": {"steps": [await _step(build, s, f"{where}.default[{i}]")
                                      for i, s in enumerate(_steps_list(spec.get("default"), where))]},
        }
    raise DecisioningError(
        f"{where}: unknown step type {kind!r}. Use rule_set, segmentation_tree, code_file, decision, model, "
        "assignment, condition or branch, or an API step type."
    )


async def build_steps(
    client: httpx.AsyncClient, steps: list[JSONDict], signature: list[JSONDict], *, add_missing_variables: bool = True
) -> tuple[list[JSONDict], JSONDict]:
    """API flow steps from shorthand steps, and a report of what was resolved.

    With *add_missing_variables*, a step term no decision variable matches is
    appended to *signature* (which is changed in place) and mapped to it.
    """
    build = _Build(client, signature, add_missing=add_missing_variables)
    built = [await _step(build, step, f"steps[{i}]") for i, step in enumerate(steps)]
    return built, build.report
