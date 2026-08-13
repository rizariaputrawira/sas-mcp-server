#!/usr/bin/env python3
# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Generate ``mcp-tools.json`` for the Microsoft 365 Copilot app package.

Microsoft 365 Copilot's plugin manifest can pin a fixed set of MCP tools via a
``mcp_tool_description`` file whose content matches the server's ``tools/list``
response. Hand-writing that file for this server's tools would drift; this
script produces it from the actual server code instead, honoring the same
``MCP_TIERS`` / ``MCP_READ_ONLY`` scoping you run the deployment with.

Run from the repository root (the .env supplies VIYA_ENDPOINT; no Viya calls
are made — tools are only enumerated):

    uv run python examples/m365-copilot/generate_tools_manifest.py --tiers 0-1

The output defaults to ``appPackage/mcp-tools.json`` next to this script.

Note: opt-in collection-mode telemetry (COLLECTION_MODE=true) injects a
``goal`` parameter into every tool schema, and that shows up here too. Match
the setting to the deployment you are pinning tools for — the checked-in file
was generated with collection mode off (the default).
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dump the server's tools/list into mcp-tools.json"
    )
    parser.add_argument(
        "--tiers",
        default=None,
        metavar="SPEC",
        help="Tool tiers to include, e.g. '0-1' or '0,1,3'. "
        "Overrides MCP_TIERS for this run; omit to use the environment.",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Include only read-only tools (sets MCP_READ_ONLY=true for this run).",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path(__file__).parent / "appPackage" / "mcp-tools.json",
        help="Output path (default: appPackage/mcp-tools.json beside this script).",
    )
    return parser.parse_args()


async def dump_tools() -> dict:
    # Imports happen here, after the environment is finalized, because the
    # server's config module reads MCP_TIERS / MCP_READ_ONLY at import time.
    from fastmcp import Client

    from sas_mcp_server.stdio_server import mcp

    async with Client(mcp) as client:
        tools = await client.list_tools()

    return {
        "tools": [
            {
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": tool.inputSchema,
            }
            for tool in tools
        ]
    }


def main() -> None:
    args = parse_args()

    # Load .env first (mirrors the server's own startup) so a real
    # VIYA_ENDPOINT wins; the dummy below only covers checkouts without one.
    # Tool enumeration never contacts Viya, so the value is not used.
    from dotenv import load_dotenv

    load_dotenv()
    os.environ.setdefault("VIYA_ENDPOINT", "https://viya.example.com")

    if args.tiers is not None:
        os.environ["MCP_TIERS"] = args.tiers
    if args.read_only:
        os.environ["MCP_READ_ONLY"] = "true"

    manifest = asyncio.run(dump_tools())

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"Wrote {len(manifest['tools'])} tool descriptions to {args.output}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
