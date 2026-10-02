# Copyright © 2026, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Check the Copilot Cowork plugin package and zip it for upload.

    uv run --with jsonschema python examples/m365-cowork/build_package.py \\
        --url https://sas-mcp.example.com/mcp

The checks are the ones Microsoft documents for a Cowork plugin (the
ASKILL-M/P rules, the connector rules, and the companion-file limits) plus
the v1.28 manifest schema, so a package that passes here is not turned away
at upload for a reason that could have been caught on the desk.

``--url`` writes the MCP endpoint of *your* deployment into the zipped
manifest; the manifest in the repository keeps its placeholder. Without it the
package is checked and zipped as it stands.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
PKG = HERE / "appPackage"
PLACEHOLDER_HOST = "sas-mcp.example.com"

KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9\-_. !]+$")
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
ICONS = {"color": (192, 192), "outline": (32, 32)}
MAX_SKILLS, MAX_CONNECTORS = 20, 10
MAX_COMPANIONS, MAX_COMPANION_BYTES, MAX_COMPANION_TOTAL = 20, 5 * 2**20, 10 * 2**20


def png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG file")
    return struct.unpack(">II", header[16:24])


def frontmatter(text: str) -> dict[str, str] | None:
    """The top-level keys of a SKILL.md front matter block, or None without one.

    Only what the package rules look at is needed — which keys exist and the
    scalar value of ``name`` — so this does not pull in a YAML parser.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = lines.index("---", 1)
    except ValueError:
        return None
    keys: dict[str, str] = {}
    for line in lines[1:end]:
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if match:
            keys[match.group(1)] = match.group(2).strip().strip("\"'")
    return keys


def check_schema(manifest: dict) -> list[str]:
    from jsonschema import Draft7Validator

    url = manifest.get("$schema", "")
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            schema = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # network, proxy, or a moved schema
        return [f"manifest.json: could not fetch {url} ({exc})"]
    problems = []
    for error in sorted(Draft7Validator(schema).iter_errors(manifest), key=lambda e: list(e.path)):
        location = "/".join(str(part) for part in error.path) or "<root>"
        problems.append(f"manifest.json: {location}: {error.message}")
    if not problems:
        print(f"ok   manifest.json validates against schema {manifest.get('manifestVersion')}")
    return problems


def check_icons(manifest: dict) -> list[str]:
    problems = []
    for kind, expected in ICONS.items():
        path = PKG / manifest.get("icons", {}).get(kind, "")
        if not path.is_file():
            problems.append(f"icons.{kind}: {path.name or '<unset>'} is not in the package")
            continue
        try:
            size = png_size(path)
        except ValueError as exc:
            problems.append(f"icons.{kind}: {path.name}: {exc}")
            continue
        if size != expected:
            problems.append(f"icons.{kind}: {path.name} is {size[0]}x{size[1]}, expected {expected[0]}x{expected[1]}")
    if not problems:
        print("ok   icons are present and correctly sized")
    return problems


def check_companions(folder: Path, label: str) -> list[str]:
    problems = []
    companions = [p for p in folder.rglob("*") if p.is_file() and p != folder / "SKILL.md"]
    if len(companions) > MAX_COMPANIONS:
        problems.append(f"{label}: {len(companions)} companion files, the limit is {MAX_COMPANIONS}")
    total = 0
    for path in companions:
        size = path.stat().st_size
        total += size
        relative = path.relative_to(folder)
        if size > MAX_COMPANION_BYTES:
            problems.append(f"{label}: {relative} is larger than 5 MB")
        for part in relative.parts:
            if part.startswith("."):
                problems.append(f"{label}: {relative} is a hidden file or sits in a hidden folder")
            elif not SAFE_NAME.match(part):
                problems.append(f"{label}: {relative} has characters a companion file name may not use")
            elif part.split(".")[0].upper() in RESERVED:
                problems.append(f"{label}: {relative} uses a Windows reserved name")
    if total > MAX_COMPANION_TOTAL:
        problems.append(f"{label}: companion files total more than 10 MB")
    return problems


def check_skills(manifest: dict) -> list[str]:
    skills = manifest.get("agentSkills", [])
    problems = []
    if len(skills) > MAX_SKILLS:
        problems.append(f"agentSkills: {len(skills)} entries, the limit is {MAX_SKILLS} (ASKILL-M002)")
    seen: set[str] = set()
    for index, entry in enumerate(skills):
        folder_ref = entry.get("folder")
        label = f"agentSkills[{index}]"
        if not folder_ref:
            problems.append(f"{label}: 'folder' is required (ASKILL-M001)")
            continue
        if len(folder_ref) > 256:
            problems.append(f"{label}: folder path is longer than 256 characters (ASKILL-M003)")
        if folder_ref in seen:
            problems.append(f"{label}: {folder_ref} is listed twice (ASKILL-P008)")
        seen.add(folder_ref)
        folder = PKG / folder_ref
        if not folder.is_dir():
            problems.append(f"{label}: {folder_ref} is not in the package (ASKILL-P001)")
            continue
        skill_file = folder / "SKILL.md"
        if not skill_file.is_file():
            problems.append(f"{label}: {folder_ref} has no SKILL.md (ASKILL-P002)")
            continue
        keys = frontmatter(skill_file.read_text(encoding="utf-8"))
        if keys is None:
            problems.append(f"{label}: SKILL.md has no front matter between '---' lines (ASKILL-P003)")
            continue
        name = keys.get("name", "")
        if not name:
            problems.append(f"{label}: front matter has no 'name' (ASKILL-P004)")
        if "description" not in keys:
            problems.append(f"{label}: front matter has no 'description' (ASKILL-P005)")
        if name and name != folder.name:
            problems.append(f"{label}: name '{name}' does not match the folder '{folder.name}' (ASKILL-P006)")
        if name and not (KEBAB.match(name) and len(name) <= 64):
            problems.append(f"{label}: name '{name}' is not kebab-case of at most 64 characters (ASKILL-P007)")
        problems += check_companions(folder, label)
    if not problems:
        print(f"ok   {len(skills)} skill(s) follow the package rules")
    return problems


def check_connectors(manifest: dict) -> list[str]:
    connectors = manifest.get("agentConnectors", [])
    problems = []
    if len(connectors) > MAX_CONNECTORS:
        problems.append(f"agentConnectors: {len(connectors)} entries, the limit is {MAX_CONNECTORS}")
    ids = [c.get("id") for c in connectors]
    if len(set(ids)) != len(ids):
        problems.append("agentConnectors: connector ids must be unique")
    for index, connector in enumerate(connectors):
        label = f"agentConnectors[{index}]"
        if not connector.get("id") or not connector.get("displayName"):
            problems.append(f"{label}: 'id' and 'displayName' are required")
        server = connector.get("toolSource", {}).get("remoteMcpServer")
        if not server:
            continue
        url = server.get("mcpServerUrl", "")
        if not url.startswith("https://"):
            problems.append(f"{label}: mcpServerUrl must be an HTTPS URL, got '{url}'")
        described = server.get("mcpToolDescription", {}).get("file")
        if described and described.startswith("./"):
            # Microsoft's own example writes "./tools/…", and the Agents Toolkit
            # validator then reports the file as missing from the package.
            problems.append(f"{label}: write mcpToolDescription.file without the leading './' ({described})")
        if described and not (PKG / described).is_file():
            problems.append(f"{label}: mcpToolDescription file {described} is not in the package")
        auth = server.get("authorization")
        if auth:
            if auth.get("type") == "None" and "referenceId" in auth:
                problems.append(f"{label}: authorization type None must not carry a referenceId")
            if auth.get("type") != "None" and not auth.get("referenceId"):
                problems.append(f"{label}: authorization type {auth.get('type')} needs a referenceId")
    if not problems:
        print(f"ok   {len(connectors)} connector(s) follow the package rules")
    return problems


def build(manifest: dict, url: str | None, output: Path) -> None:
    if url:
        for connector in manifest.get("agentConnectors", []):
            server = connector.get("toolSource", {}).get("remoteMcpServer")
            if server:
                server["mcpServerUrl"] = url
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=4, ensure_ascii=False) + "\n")
        for path in sorted(PKG.rglob("*")):
            if path.is_file() and path.name != "manifest.json" and "build" not in path.relative_to(PKG).parts:
                # Skills are read as text on the other side: ship LF whatever the checkout has.
                data = path.read_bytes()
                if path.suffix in (".md", ".json"):
                    data = data.replace(b"\r\n", b"\n")
                archive.writestr(path.relative_to(PKG).as_posix(), data)
    print(f"\nWrote {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", help="MCP endpoint of your deployment, e.g. https://sas-mcp.example.com/mcp")
    parser.add_argument("-o", "--output", type=Path, default=PKG / "build" / "sas-viya-cowork.zip")
    parser.add_argument("--check-only", action="store_true", help="Run the checks and write no zip.")
    args = parser.parse_args()

    try:
        import jsonschema  # noqa: F401
    except ImportError:
        print(
            "jsonschema is not installed — run:\n"
            "    uv run --with jsonschema python examples/m365-cowork/build_package.py",
            file=sys.stderr,
        )
        raise SystemExit(2) from None

    manifest = json.loads((PKG / "manifest.json").read_text(encoding="utf-8"))
    problems = check_schema(manifest) + check_icons(manifest) + check_skills(manifest) + check_connectors(manifest)
    if args.url and not args.url.startswith("https://"):
        problems.append(f"--url must be an HTTPS URL, got '{args.url}'")
    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        raise SystemExit(1)
    if args.check_only:
        print("\nPackage looks upload-ready.")
        return
    if not args.url:
        print(f"note no --url given: the zip points at the placeholder {PLACEHOLDER_HOST}")
    build(manifest, args.url, args.output)


if __name__ == "__main__":
    main()
