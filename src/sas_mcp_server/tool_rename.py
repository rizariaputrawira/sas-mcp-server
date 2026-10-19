# Copyright © 2026, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Opt-in renaming of tools on the wire (``MCP_TOOL_RENAME``).

Some MCP hosts refuse tool names the protocol allows. Microsoft Copilot
Studio's agent runtime is the case this was built for: a ``tools/list`` that
contains a tool whose name starts with ``catalog_`` makes every turn of the
conversation fail with ``MCS-2011 SystemError`` — no detail, and nothing wrong
with the tool. The same list with only those names changed works.

Renaming the tools in the code would break every client that already calls
them, so the rename is a setting, and it happens at the edge::

    MCP_TOOL_RENAME=catalog_=infocat_

Each ``old=new`` pair replaces a name *prefix*; several pairs are separated by
commas and the longest matching prefix wins. A whole tool name is a prefix of
itself, so one tool can be renamed too.

The rename is the outermost middleware, which is what keeps it small:

* ``tools/list`` goes out with the public names, and with any mention of a
  renamed tool in the other tools' descriptions rewritten to match — a
  description that points the model at a name it cannot see is worse than no
  pointer.
* ``tools/call`` with a public name is translated back before anything else
  runs, so the read-only classification, tier map, telemetry, the user log and
  the MCP Apps view bindings all keep working on the names in the code.
* The original name stays callable. A view that calls a companion tool, or a
  manifest pinned before the setting was turned on, does not break.

Prompts are not rewritten: none of them names a tool by a prefix anyone has
had to rename.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

# The Tool model moved to fastmcp.tools.base in FastMCP 4 (same import the
# telemetry middleware uses).
from fastmcp.tools.base import Tool  # pyright: ignore[reportMissingImports]

from .exceptions import ConfigError
from .viya_client import logger

# What every model API accepts as a function name. The protocol itself is
# looser, but a public name outside this set would trade one host's problem
# for another's.
_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_PREFIX = re.compile(r"[A-Za-z0-9_-]+")


def parse_rename_spec(spec: str | None) -> list[tuple[str, str]]:
    """Parse ``"old=new,old2=new2"`` into prefix pairs, longest prefix first.

    Raises :class:`ConfigError` on anything that is not a clean pair: a typo
    here would otherwise surface as a host that still cannot use the server.
    """
    pairs: dict[str, str] = {}
    for raw in (spec or "").split(","):
        part = raw.strip()
        # An empty value is the .env.sample default; a leading '#' is a value
        # left as a commented-out placeholder (same rule as MCP_BASE_URL).
        if not part or part.startswith("#"):
            continue
        old, sep, new = (piece.strip() for piece in part.partition("="))
        if not sep or not old or not new:
            raise ConfigError(
                f"MCP_TOOL_RENAME: {part!r} is not an old=new pair (for example catalog_=infocat_)."
            )
        for piece in (old, new):
            if not _PREFIX.fullmatch(piece):
                raise ConfigError(
                    f"MCP_TOOL_RENAME: {piece!r} may only contain letters, digits, '_' and '-'."
                )
        if old in pairs:
            raise ConfigError(f"MCP_TOOL_RENAME: the prefix {old!r} is given twice.")
        if old != new:
            pairs[old] = new
    return sorted(pairs.items(), key=lambda pair: len(pair[0]), reverse=True)


class ToolRenameMiddleware(Middleware):
    """Present renamed tools to the client; run them under their own names."""

    def __init__(self, pairs: Sequence[tuple[str, str]]) -> None:
        self._pairs = list(pairs)
        # public name -> name in the code, filled by bind().
        self._canonical: dict[str, str] = {}

    def public_name(self, name: str) -> str:
        """The name a client sees for the tool registered as *name*."""
        for old, new in self._pairs:
            if name.startswith(old):
                return new + name[len(old) :]
        return name

    def bind(self, names: Iterable[str]) -> dict[str, str]:
        """Check the rename against the registered tool *names* and remember
        how to translate calls back. Returns ``{name: public name}`` for the
        tools that were renamed.

        Called once, after registration, so a rename that cannot work stops
        the server with the reason instead of confusing a client later.
        """
        registered = sorted(set(names))
        renamed = {n: self.public_name(n) for n in registered if self.public_name(n) != n}
        for name, public in renamed.items():
            if not _NAME.fullmatch(public):
                raise ConfigError(
                    f"MCP_TOOL_RENAME turns {name!r} into {public!r}, which is not a usable "
                    "tool name (1-64 letters, digits, '_' or '-')."
                )
        owners: dict[str, str] = {}
        for name in registered:
            public = renamed.get(name, name)
            if public in owners:
                raise ConfigError(
                    f"MCP_TOOL_RENAME gives {name!r} and {owners[public]!r} the same name, {public!r}."
                )
            owners[public] = name
        for old, new in self._pairs:
            if not any(name.startswith(old) for name in registered):
                # Not an error: the tier that holds those tools may be disabled.
                logger.warning("MCP_TOOL_RENAME: no registered tool starts with %r (-> %r)", old, new)
        self._canonical = {public: name for name, public in renamed.items()}
        if renamed:
            logger.info(
                "MCP_TOOL_RENAME: %d tool(s) presented under another name: %s",
                len(renamed),
                ", ".join(f"{name} -> {public}" for name, public in sorted(renamed.items())),
            )
        return renamed

    async def on_list_tools(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Sequence[Tool]:
        tools: Sequence[Tool] = await call_next(context)
        renamed = {t.name: self.public_name(t.name) for t in tools}
        renamed = {name: public for name, public in renamed.items() if public != name}
        if not renamed:
            return tools
        rewrite = _text_rewriter(renamed)
        out: list[Tool] = []
        for tool in tools:
            update: dict[str, Any] = {}
            if tool.name in renamed:
                update["name"] = renamed[tool.name]
            description = rewrite(tool.description)
            if description != tool.description:
                update["description"] = description
            parameters = rewrite(tool.parameters)
            if parameters != tool.parameters:
                update["parameters"] = parameters
            # A NEW Tool; the registry's own object is shared and never mutated.
            out.append(tool.model_copy(update=update) if update else tool)
        return out

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        canonical = self._canonical.get(getattr(context.message, "name", ""))
        if canonical is not None:
            context = context.copy(message=context.message.model_copy(update={"name": canonical}))
        return await call_next(context)


def _text_rewriter(renamed: dict[str, str]) -> Callable[[Any], Any]:
    """A function that replaces each renamed tool's name, as a whole word, in
    every string of a JSON-like value. Whole words only: ``catalog_search``
    must not touch a parameter called ``catalog_entity_id``."""
    names = sorted(renamed, key=len, reverse=True)
    pattern = re.compile(
        r"(?<![A-Za-z0-9_-])(" + "|".join(re.escape(n) for n in names) + r")(?![A-Za-z0-9_-])"
    )

    def rewrite(value: Any) -> Any:
        if isinstance(value, str):
            return pattern.sub(lambda match: renamed[match.group(1)], value)
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return value

    return rewrite


def install_tool_rename(mcp: FastMCP, spec: str | None = None) -> ToolRenameMiddleware | None:
    """Add the rename middleware to *mcp* when ``MCP_TOOL_RENAME`` (or *spec*)
    asks for one; returns it so the caller can :meth:`~ToolRenameMiddleware.bind`
    it once the tools are registered. Returns ``None``, adding nothing, when
    no rename is configured.

    Call it before any other middleware is added: the first one added is the
    outermost, and this one has to translate a call before the rest see it.
    """
    if spec is None:
        from .config import MCP_TOOL_RENAME

        spec = MCP_TOOL_RENAME
    pairs = parse_rename_spec(spec)
    if not pairs:
        return None
    middleware = ToolRenameMiddleware(pairs)
    mcp.add_middleware(middleware)
    return middleware
