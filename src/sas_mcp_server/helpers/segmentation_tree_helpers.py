# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Segmentation trees: shorthand for writing one, and building one from a strategy table.

A tree node tests one variable. Its value nodes are evaluated in order, as an
``if`` / ``else if`` chain, and the first that matches decides where the case
goes: to the next node, or to an outcome that assigns variables. A value node's
``value`` is one of (probed against SAS Viya, October 2026):

* a value, or several separated by semicolons: ``"A"``, ``"1;2;3"``;
* a comparison: ``">=630"``, ``"<600"``; ``">=100;<0"`` is an OR of the two;
* a range with the ``v`` placeholder: ``"600<=v<630"``, ``"v>=630"``;
* ``"__&OTHER&__"`` — everything else, missing values included.

``"100-599"``, ``"[100,600)"`` and ``">=100 and <600"`` are rejected.

:func:`build_tree_from_rows` turns a strategy table — one row per branch, a
value per decision key, and the outcome columns — into one tree. It is how a
FICO-style strategy (a TRIAD or Blaze export) becomes a SAS Intelligent
Decisioning tree. Keys are tested in column order; a key whose values do not
differ inside a subtree is not tested there. With ``split="lower_bound"`` each
value is the lower bound of a range, as such exports write them: siblings
``0, 600, 630`` become ``v>=630``, ``600<=v<630`` and, for the lowest, Other —
which also takes missing values. With ``split="exact"`` each distinct value is
its own branch.
"""

import contextlib
import csv
import io
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .. import config
from ..viya_client import raise_for_viya_status

JSONDict = dict[str, Any]
OTHER = "__&OTHER&__"
_OTHER_ALIASES = frozenset({"other", "__&other&__", "*"})
_MISSING_STRINGS = frozenset({"", "#n/a", "n/a", "na", "null", "none", "."})
MAX_TABLE_ROWS = 50_000


class TreeError(ValueError):
    """A tree or a strategy table that cannot be turned into a tree. The message says why."""


# --- writing a tree --------------------------------------------------------------------


def _value_string(value: Any) -> str:
    if isinstance(value, list | tuple):
        return ";".join(_value_string(v) for v in value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value)
    return OTHER if text.strip().lower() in _OTHER_ALIASES else text


def normalize_node(node: JSONDict | None) -> JSONDict | None:
    """Accept the shorthand a caller is likely to write and return the API's node shape.

    ``variable`` for ``variableName``, ``outcome`` for ``outcomeName``, a list
    for a semicolon-separated value, ``"Other"`` for ``__&OTHER&__``, and a
    missing ``type`` inferred from the fields present.
    """
    if node is None:
        return None
    out = dict(node)
    if "variable" in out and "variableName" not in out:
        out["variableName"] = out.pop("variable")
    if "type" not in out:
        out["type"] = (
            "booleanExpression" if "booleanExpressionName" in out
            else "matrix" if "matrixName" in out
            else "variable"
        )
    value_nodes = []
    for value_node in out.get("valueNodes") or []:
        vn = dict(value_node)
        if "outcome" in vn and "outcomeName" not in vn:
            vn["outcomeName"] = vn.pop("outcome")
        if "value" in vn:
            vn["value"] = _value_string(vn["value"])
        if vn.get("next") is not None:
            vn["next"] = normalize_node(vn["next"])
        value_nodes.append(vn)
    out["valueNodes"] = value_nodes
    return out


def normalize_outcomes(outcomes: list[JSONDict] | None, code_files: list[JSONDict] | None) -> tuple[list, list]:
    """Outcomes from ``{"name", "assign": {var: value}, "code": "<DS2>"}`` shorthand.

    ``assign`` becomes static variable assignments; ``code`` becomes an internal
    code file of the tree, run by a code assignment. Outcomes that already carry
    ``actions`` pass through.
    """
    files = list(code_files or [])
    result = []
    for outcome in outcomes or []:
        if "name" not in outcome:
            raise TreeError(f"An outcome has no name: {outcome!r}.")
        o = {k: v for k, v in outcome.items() if k not in ("assign", "code")}
        actions = list(outcome.get("actions") or [])
        for variable, value in (outcome.get("assign") or {}).items():
            actions.append(
                {"type": "variableAssignment", "variableName": variable, "valueType": "static", "value": value}
            )
        if outcome.get("code"):
            file_name = f"{outcome['name']}_code"
            files.append({"name": file_name, "type": "internal", "code": outcome["code"]})
            actions.append({"type": "codeAssignment", "codeFileName": file_name})
        o["actions"] = actions
        result.append(o)
    return result, files


def _walk(node: JSONDict | None):
    if not node:
        return
    yield node
    for value_node in node.get("valueNodes") or []:
        yield from _walk(value_node.get("next"))


def check_tree(body: JSONDict) -> list[str]:
    """What the service would reject without saying where: unknown outcomes and variables."""
    problems = []
    outcomes = {o["name"] for o in body.get("outcomes") or []}
    variables = {t["name"] for t in body.get("signature") or []}
    expressions = {b["name"] for b in body.get("booleanExpressions") or []}
    for node in _walk(body.get("node")):
        if node.get("type") == "variable" and node.get("variableName") not in variables:
            problems.append(f"node tests {node.get('variableName')!r}, which is not in the signature")
        if node.get("type") == "booleanExpression" and node.get("booleanExpressionName") not in expressions:
            problems.append(f"node uses boolean expression {node.get('booleanExpressionName')!r}, which is not defined")
        for value_node in node.get("valueNodes") or []:
            name = value_node.get("outcomeName")
            if name and name not in outcomes:
                problems.append(f"value {value_node.get('value')!r} leads to outcome {name!r}, which is not defined")
    for outcome in body.get("outcomes") or []:
        for action in outcome.get("actions") or []:
            if action.get("type") == "variableAssignment" and action.get("variableName") not in variables:
                problems.append(
                    f"outcome {outcome['name']!r} assigns {action.get('variableName')!r}, which is not in the signature"
                )
    return problems


def tree_body(
    *,
    name: str,
    signature: list[JSONDict],
    node: JSONDict,
    outcomes: list[JSONDict] | None = None,
    description: str | None = None,
    boolean_expressions: list[JSONDict] | None = None,
    matrices: list[JSONDict] | None = None,
    code_files: list[JSONDict] | None = None,
) -> JSONDict:
    normalized_outcomes, files = normalize_outcomes(outcomes, code_files)
    body: JSONDict = {
        "name": name,
        "description": description or "",
        "signature": signature,
        "node": normalize_node(node),
        "outcomes": normalized_outcomes,
        "codeFiles": files,
        "booleanExpressions": boolean_expressions or [],
        "matrices": matrices or [],
    }
    problems = check_tree(body)
    if problems:
        raise TreeError("The tree is not consistent: " + "; ".join(problems[:10]))
    return body


# --- building a tree from a strategy table ------------------------------------------------


@dataclass(frozen=True)
class KeyColumn:
    column: str
    variable: str


def ds2_name(label: str) -> str:
    """A variable name from a column label: ``'SC Score 01 (Risk)'`` -> ``'SCScore01Risk'``."""
    name = re.sub(r"[^A-Za-z0-9_]", "", label.replace("%", "pct"))
    if not name:
        raise TreeError(f"Column {label!r} gives no usable variable name; name it explicitly.")
    if name[0].isdigit():
        name = "v_" + name
    return name[:60]


def parse_key_columns(spec: list[Any]) -> list[KeyColumn]:
    """``"column"`` or ``{"column": ..., "variable": ...}``. Two columns may test one variable."""
    keys = []
    for item in spec:
        if isinstance(item, dict):
            if "column" not in item:
                raise TreeError(f"A key column needs 'column': {item!r}.")
            keys.append(KeyColumn(item["column"], item.get("variable") or ds2_name(item["column"])))
        else:
            keys.append(KeyColumn(str(item), ds2_name(str(item))))
    if not keys:
        raise TreeError("Give at least one key column.")
    return keys


def to_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return None if isinstance(value, float) and math.isnan(value) else float(value)
    text = str(value).strip()
    if text.lower() in _MISSING_STRINGS:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value)) or (
        isinstance(value, str) and value.strip().lower() in _MISSING_STRINGS
    )


def _fmt(number: float) -> str:
    return str(int(number)) if float(number).is_integer() else repr(float(number))


def _column_type(values: list[Any]) -> str:
    present = [v for v in values if not _is_missing(v)]
    numbers = [to_number(v) for v in present]
    if present and all(n is not None for n in numbers):
        return "integer" if all(float(n).is_integer() for n in numbers) else "decimal"
    return "string"


def _typed(value: Any, data_type: str) -> Any:
    # Missing stays missing: the outcome does not assign it. Viya refuses an
    # empty string as a static value ("Data type string and value '' didn't match").
    if _is_missing(value):
        return None
    if data_type == "integer":
        return int(to_number(value))
    if data_type == "decimal":
        return to_number(value)
    number = to_number(value)
    return _fmt(number) if number is not None and not isinstance(value, str) else str(value).strip()


def build_tree_from_rows(
    rows: list[JSONDict],
    *,
    key_columns: list[Any],
    outcome_columns: list[str],
    split: str = "lower_bound",
    outcome_prefix: str | None = None,
    row_label_column: str | None = None,
    outcome_types: dict[str, str] | None = None,
) -> tuple[JSONDict, list[JSONDict], list[JSONDict], JSONDict]:
    """(root node, outcomes, signature, report) for a strategy table.

    An outcome column's type is inferred from its values unless *outcome_types*
    names it: a code that happens to be numeric (a scenario group) may need to
    be a string for the rules that read it.
    """
    if split not in ("lower_bound", "exact"):
        raise TreeError(f"split must be 'lower_bound' or 'exact', not {split!r}.")
    if not rows:
        raise TreeError("The table has no rows.")
    keys = parse_key_columns(key_columns)
    if not outcome_columns:
        raise TreeError("Give at least one outcome column: the leaves have to assign something.")
    present = set(rows[0])
    wanted = [k.column for k in keys] + list(outcome_columns) + ([row_label_column] if row_label_column else [])
    missing = [c for c in wanted if c not in present]
    if missing:
        raise TreeError(f"Columns not in the table: {', '.join(missing)}. Columns are: {', '.join(rows[0])}.")

    def label_of(index: int) -> Any:
        return rows[index].get(row_label_column) if row_label_column else index + 1

    # Key values: numbers for lower_bound, text for exact.
    def key_value(index: int, key: KeyColumn) -> Any:
        raw = rows[index][key.column]
        if split == "lower_bound":
            number = to_number(raw)
            if number is None:
                raise TreeError(
                    f"Row {label_of(index)}: {key.column!r} is {raw!r}, not a number; lower_bound splits need numbers."
                )
            return number
        number = to_number(raw)
        return _fmt(number) if number is not None and not isinstance(raw, str) else str(raw).strip()

    prepared = [([key_value(i, k) for k in keys], i) for i in range(len(rows))]
    forced = outcome_types or {}
    bad_types = {c: t for c, t in forced.items() if t not in ("string", "integer", "decimal")}
    if bad_types:
        raise TreeError(f"outcome_types must be string, integer or decimal: {bad_types}.")
    out_types = {c: forced.get(c) or _column_type([r[c] for r in rows]) for c in outcome_columns}

    outcomes: dict[tuple, JSONDict] = {}
    used_names: set[str] = set()

    def outcome_for(row: JSONDict) -> str:
        values = tuple(_typed(row[c], out_types[c]) for c in outcome_columns)
        if values in outcomes:
            return outcomes[values]["name"]
        first = _fmt(values[0]) if isinstance(values[0], float) else values[0]
        base = re.sub(r"[^A-Za-z0-9_]", "_", f"{outcome_prefix or ds2_name(outcome_columns[0])}_{first}")[:90]
        name, n = base, 2
        while name in used_names:
            name, n = f"{base}_{n}", n + 1
        used_names.add(name)
        outcomes[values] = {
            "name": name,
            "description": ", ".join(
                f"{c} = {v if v not in (None, '') else 'missing'}" for c, v in zip(outcome_columns, values, strict=True)
            ),
            "actions": [
                {"type": "variableAssignment", "variableName": ds2_name(c), "valueType": "static", "value": v}
                for c, v in zip(outcome_columns, values, strict=True) if v is not None
            ],
        }
        return name

    report: JSONDict = {"rows": len(rows), "leaves": 0, "split_nodes": 0, "conflicts": [], "max_depth": 0}
    conditions: dict[str, dict[str, str]] = {}

    def build(depth: int, group: list[tuple[list, int]], level: int) -> tuple[JSONDict | None, str | None]:
        while depth < len(keys) and len({values[depth] for values, _ in group}) == 1:
            depth += 1
        if depth == len(keys):
            report["leaves"] += 1
            report["max_depth"] = max(report["max_depth"], level)
            names = {outcome_for(rows[i]) for _, i in group}
            chosen = outcome_for(rows[group[0][1]])
            if len(names) > 1:
                report["conflicts"].append(
                    {"rows": [label_of(i) for _, i in group], "outcomes": sorted(names), "used": chosen}
                )
            return None, chosen
        key = keys[depth]
        report["split_nodes"] += 1
        present_values = {values[depth] for values, _ in group}
        if split == "lower_bound":
            distinct = sorted(present_values, reverse=True)
        else:
            distinct = sorted(present_values, key=lambda v: (to_number(v) is None, to_number(v) or 0, str(v)))
        value_nodes = []
        for index, bound in enumerate(distinct):
            members = [(values, i) for values, i in group if values[depth] == bound]
            if split == "lower_bound":
                if index == 0:
                    value, label = f"v>={_fmt(bound)}", f">= {_fmt(bound)}"
                elif index == len(distinct) - 1:
                    value, label = OTHER, f"< {_fmt(distinct[index - 1])} or missing"
                else:
                    upper = _fmt(distinct[index - 1])
                    value, label = f"{_fmt(bound)}<=v<{upper}", f"{_fmt(bound)} to < {upper}"
                if value != OTHER:
                    conditions.setdefault(key.variable, {})[value] = label
            else:
                value, label = str(bound), str(bound)
                conditions.setdefault(key.variable, {})[value] = label
            nxt, outcome = build(depth + 1, members, level + 1)
            vn: JSONDict = {"value": value, "label": label}
            if nxt is not None:
                vn["next"] = nxt
            else:
                vn["outcomeName"] = outcome
            value_nodes.append(vn)
        node = {"type": "variable", "variableName": key.variable, "label": key.variable, "valueNodes": value_nodes}
        return node, None

    root, single = build(0, prepared, 0)
    if root is None:
        raise TreeError(
            f"No key column differs across the table, so there is nothing to split on; every row ends in {single!r}."
        )
    tested = []
    for node in _walk(root):
        if node["variableName"] not in tested:
            tested.append(node["variableName"])
    key_types = {}
    for key in keys:
        if split == "lower_bound":
            key_types[key.variable] = "decimal"
        else:
            column_type = _column_type([r[key.column] for r in rows])
            key_types[key.variable] = "string" if column_type == "string" else "decimal"
    signature = [{"name": v, "direction": "input", "dataType": key_types[v]} for v in tested]
    signature += [{"name": ds2_name(c), "direction": "output", "dataType": out_types[c]} for c in outcome_columns]
    report["outcomes"] = len(outcomes)
    report["variables_tested"] = tested
    report["keys_never_split"] = [k.variable for k in keys if k.variable not in tested]
    report["conditions"] = {v: dict(c) for v, c in conditions.items()}
    return root, list(outcomes.values()), signature, report


def sorted_conditions(conditions: dict[str, str]) -> list[JSONDict]:
    """Value list entries for a variable's conditions, ordered by lower bound: ``{"key", "label"}``."""
    def lower(condition: str) -> float:
        head = condition.split("<=v<")[0] if "<=v<" in condition else condition.removeprefix("v>=")
        number = to_number(head)
        return number if number is not None else float("inf")
    return [{"key": c, "label": conditions[c]} for c in sorted(conditions, key=lower)]


# --- reading a strategy table ---------------------------------------------------------------


def _dedupe(header: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for raw in header:
        name = (raw or "").strip() or f"column_{len(out) + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


def rows_from_csv_text(text: str, max_rows: int = MAX_TABLE_ROWS) -> list[JSONDict]:
    """Rows of a CSV whose first line is the header. Repeated headers get ``_2``, ``_3``."""
    dialect: Any = csv.excel
    with contextlib.suppress(csv.Error):
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = _dedupe(next(reader))
    except StopIteration:
        raise TreeError("The CSV is empty.") from None
    rows = []
    for record in reader:
        if not any(cell.strip() for cell in record):
            continue
        rows.append(dict(zip(header, record, strict=False)))
        if len(rows) > max_rows:
            raise TreeError(f"The table has more than {max_rows} rows.")
    return rows


def rows_from_csv_file(path: str, max_rows: int = MAX_TABLE_ROWS) -> list[JSONDict]:
    file = Path(path).expanduser()
    if file.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        raise TreeError(
            "Excel files are not read directly. Load the sheet into CAS with upload_data (it reads xlsx), "
            "then pass caslib_name and table_name; or save the sheet as CSV."
        )
    if not file.is_file():
        raise TreeError(f"No file at {path!r}.")
    return rows_from_csv_text(file.read_text(encoding="utf-8-sig"), max_rows)


async def rows_from_cas_table(
    client: httpx.AsyncClient, server_id: str, caslib_name: str, table_name: str, max_rows: int = MAX_TABLE_ROWS
) -> list[JSONDict]:
    """All rows of a CAS table, by column name, paged through the rowSets service."""
    base = config.VIYA_ENDPOINT
    source = f"cas~fs~{server_id}~fs~{caslib_name}"
    columns: list[str] = []
    start = 0
    while True:
        resp = await client.get(
            f"{base}/dataTables/dataSources/{source}/tables/{table_name}/columns",
            params={"start": start, "limit": 100}, follow_redirects=True,
        )
        raise_for_viya_status(resp)
        data = resp.json()
        columns += [item["name"] for item in sorted(data.get("items", []), key=lambda i: i.get("index", 0))]
        start += 100
        if start >= data.get("count", 0):
            break
    rows: list[JSONDict] = []
    start = 0
    table_id = f"{source}~fs~{table_name}"
    while True:
        resp = await client.get(
            f"{base}/rowSets/tables/{table_id}/rows", params={"start": start, "limit": 1000}, follow_redirects=True
        )
        raise_for_viya_status(resp)
        data = resp.json()
        items = data.get("items", [])
        rows += [dict(zip(columns, item.get("cells", []), strict=False)) for item in items]
        start += len(items)
        if not items or start >= data.get("count", 0):
            break
        if len(rows) > max_rows:
            raise TreeError(f"The table has more than {max_rows} rows.")
    return rows
