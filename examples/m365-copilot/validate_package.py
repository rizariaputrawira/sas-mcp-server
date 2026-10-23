#!/usr/bin/env python3
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Validate the Microsoft 365 app package before you sideload it.

Sideloading reports failures as a single unhelpful message, so this script
front-runs the obvious causes:

* each manifest validates against Microsoft's **published** JSON schema
  (fetched live, so a schema revision shows up here rather than at upload),
* every file one manifest references actually exists in the package,
* the icons are really 192x192 and 32x32 PNGs,
* pinned-tools mode is *complete* — the tool names appear in ``functions``
  **and** ``run_for_functions``, not just in ``mcp_tool_description``.

Run it from the repository root::

    uv run python examples/m365-copilot/validate_package.py

Two schema errors are expected and reported as tolerated, because they are
defects in Microsoft's published v2.4 schema rather than in this package —
both are ``oneOf`` branches that overlap, so a valid document matches two of
them and is rejected:

1. ``runtime.spec`` — ``{"url": ...}`` matches both ``open-api-spec`` and
   ``mcp-execution-spec``. Every MCP plugin manifest hits this, including the
   dynamic-discovery example in Microsoft's own documentation.
2. ``mcp_tool_description`` — ``{"file": ...}`` matches both
   ``mcp-tool-file-reference`` and ``mcp-tool-inline`` (the inline branch is an
   unconstrained object). Only the *inline* form of pinned tools validates.

Anything else is a real finding. Use ``--strict`` to fail on the tolerated two
as well, e.g. to check whether Microsoft has fixed the schema.
"""

import argparse
import json
import struct
import sys
import urllib.request
from pathlib import Path

PKG = Path(__file__).parent / "appPackage"

SCHEMAS = {
    "manifest.json": "https://developer.microsoft.com/json-schemas/teams/v1.19/MicrosoftTeams.schema.json",
    "declarativeAgent.json": "https://developer.microsoft.com/json-schemas/copilot/declarative-agent/v1.6/schema.json",
    "ai-plugin.json": "https://developer.microsoft.com/json-schemas/copilot/plugin/v2.4/schema.json",
}

# (path prefix, substring of the message) of the two known schema defects.
TOLERATED = [
    ("runtimes", "mcp-execution-spec"),
    ("mcp_tool_description", "mcp-tool-inline"),
]

ICON_SIZES = {"color.png": (192, 192), "outline.png": (32, 32)}


def png_size(path: Path) -> tuple[int, int]:
    """Width and height from a PNG's IHDR chunk, without an image library."""
    header = path.read_bytes()[:24]
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", header[16:24])


def is_tolerated(error) -> bool:
    location = "/".join(str(part) for part in error.path)
    return any(
        prefix in location and needle in error.message for prefix, needle in TOLERATED
    )


def load(name: str) -> dict:
    return json.loads((PKG / name).read_text(encoding="utf-8"))


def check_schemas(strict: bool) -> list[str]:
    from jsonschema import Draft7Validator

    problems = []
    for name, url in SCHEMAS.items():
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                schema = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # network, proxy, or a moved schema
            problems.append(f"{name}: could not fetch {url} ({exc})")
            continue

        errors = sorted(
            Draft7Validator(schema).iter_errors(load(name)),
            key=lambda e: list(e.path),
        )
        real = [e for e in errors if strict or not is_tolerated(e)]
        tolerated = len(errors) - len(real)
        for error in real:
            location = "/".join(str(part) for part in error.path) or "<root>"
            problems.append(f"{name}: {location}: {error.message}")
        if not real:
            note = f" ({tolerated} known schema defect(s) tolerated)" if tolerated else ""
            print(f"ok   {name} validates{note}")
    return problems


def check_references() -> list[str]:
    problems = []
    manifest = load("manifest.json")

    for icon in manifest.get("icons", {}).values():
        if not (PKG / icon).is_file():
            problems.append(f"manifest.json references a missing icon: {icon}")
            continue
        expected = ICON_SIZES.get(icon)
        actual = png_size(PKG / icon)
        if expected and actual != expected:
            problems.append(f"{icon} is {actual[0]}x{actual[1]}, expected {expected[0]}x{expected[1]}")

    for agent in manifest.get("copilotAgents", {}).get("declarativeAgents", []):
        if not (PKG / agent["file"]).is_file():
            problems.append(f"manifest.json references a missing agent file: {agent['file']}")

    for action in load("declarativeAgent.json").get("actions", []):
        if not (PKG / action["file"]).is_file():
            problems.append(f"declarativeAgent.json references a missing action file: {action['file']}")

    if not problems:
        print("ok   every referenced file exists, icons are the right size")
    return problems


def check_tool_wiring() -> list[str]:
    """Dynamic discovery and pinned tools each have a shape; half of either fails silently."""
    problems = []
    plugin = load("ai-plugin.json")
    functions = [f["name"] for f in plugin.get("functions", [])]

    for index, runtime in enumerate(plugin.get("runtimes", [])):
        if runtime.get("type") != "RemoteMCPServer":
            continue
        spec = runtime.get("spec", {})
        run_for = runtime.get("run_for_functions", [])
        pinned = spec.get("mcp_tool_description")

        if pinned is None:
            if functions or run_for != ["*"]:
                problems.append(
                    f"runtimes[{index}]: dynamic tool discovery needs an empty "
                    'functions array and run_for_functions ["*"]'
                )
            else:
                print("ok   dynamic tool discovery is wired correctly")
            continue

        if "file" in pinned:
            referenced = PKG / pinned["file"]
            if not referenced.is_file():
                problems.append(f"runtimes[{index}]: mcp_tool_description.file not found: {pinned['file']}")
                return problems
            names = [t["name"] for t in json.loads(referenced.read_text(encoding="utf-8"))["tools"]]
        else:
            names = [t["name"] for t in pinned.get("tools", [])]

        # The failure this catches: definitions pinned, but nothing declared,
        # so the agent has an action and calls nothing.
        missing_functions = sorted(set(names) - set(functions))
        missing_run_for = sorted(set(names) - set(run_for))
        if missing_functions:
            problems.append(
                f"runtimes[{index}]: pinned but absent from the top-level functions array: "
                + ", ".join(missing_functions[:5])
                + (f" (+{len(missing_functions) - 5} more)" if len(missing_functions) > 5 else "")
            )
        if missing_run_for and run_for != ["*"]:
            problems.append(
                f"runtimes[{index}]: pinned but absent from run_for_functions: "
                + ", ".join(missing_run_for[:5])
                + (f" (+{len(missing_run_for) - 5} more)" if len(missing_run_for) > 5 else "")
            )
        if not (missing_functions or missing_run_for):
            print(f"ok   {len(names)} pinned tools are declared in both arrays")

    return problems


def check_pinned_file() -> list[str]:
    """The pinned file must match tools/list: name, description and inputSchema,
    plus the annotations (and view binding) Copilot acts on.

    Annotations are not decoration here. A pinned tool without ``readOnlyHint``
    is treated as consequential, so Copilot asks the user to confirm every
    call — verified against Copilot, as is that a pinned ``_meta.ui`` renders.
    """
    path = PKG / "mcp-tools.json"
    if not path.is_file():
        return []
    tools = json.loads(path.read_text(encoding="utf-8"))["tools"]
    required = {"name", "description", "inputSchema"}
    allowed = required | {"annotations", "_meta"}
    extra = sorted({key for tool in tools for key in tool} - allowed)
    incomplete = [t.get("name", "<unnamed>") for t in tools if not required <= set(t)]
    unannotated = [t.get("name", "<unnamed>") for t in tools if "annotations" not in t]

    problems = []
    if extra:
        problems.append(
            "mcp-tools.json carries properties tools/list does not have "
            f"({', '.join(extra)}); regenerate it with generate_tools_manifest.py."
        )
    if incomplete:
        problems.append(f"mcp-tools.json tools missing a required property: {', '.join(incomplete[:5])}")
    if unannotated:
        problems.append(
            f"mcp-tools.json: {len(unannotated)} tool(s) without annotations "
            f"({', '.join(unannotated[:5])}); Copilot will ask for confirmation "
            "before each of them. Regenerate the file."
        )
    if not problems:
        views = sum(1 for t in tools if "_meta" in t)
        print(
            f"ok   mcp-tools.json holds {len(tools)} tools in tools/list shape "
            f"(annotated, {views} with a view)"
        )
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Also fail on the two known defects in Microsoft's published schema.",
    )
    args = parser.parse_args()

    try:
        import jsonschema  # noqa: F401
    except ImportError:
        print(
            "jsonschema is not installed — run:\n"
            "    uv run --with jsonschema python examples/m365-copilot/validate_package.py",
            file=sys.stderr,
        )
        raise SystemExit(2) from None

    problems = (
        check_schemas(args.strict)
        + check_references()
        + check_tool_wiring()
        + check_pinned_file()
    )

    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        raise SystemExit(1)
    print("\nPackage looks sideload-ready.")


if __name__ == "__main__":
    main()
