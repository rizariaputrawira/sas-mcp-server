# Microsoft Copilot Cowork plugin

A plugin package that gives [Copilot Cowork](https://learn.microsoft.com/en-us/microsoft-365/copilot/cowork/get-started) a **connector** to a remotely hosted SAS Viya MCP Server and two **skills** that teach it how to use the tools. It follows Microsoft's [Build plugins for Copilot Cowork](https://learn.microsoft.com/en-us/microsoft-365/copilot/cowork/cowork-plugin-development).

Cowork is the third Microsoft route to this server. Pick by where people work:

| Route | What it is | Example |
| --- | --- | --- |
| Microsoft 365 Copilot | A declarative agent in the Copilot chat; renders the interactive views | [m365-copilot](../m365-copilot/README.md) |
| Copilot Studio | The server as a tool of a Copilot Studio agent; nothing to package | [copilot-studio](../copilot-studio/README.md) |
| Copilot Cowork | A plugin: connector plus skills, for multi-step tasks Cowork carries out | this folder |

> **Status: built and validated, not yet run in Cowork.** The package passes the v1.28 manifest schema, every documented package rule and the Microsoft 365 Agents Toolkit validator, and it sideloads (`atk install` returns a title ID). The tenant it was built in does not have Cowork enabled, so sign-in and tool calls from Cowork itself are untested. What Cowork needs from the server is therefore taken from Microsoft's documentation, not observed — see [What the server needs](#what-the-server-needs).

## What is in the package

```
appPackage/
├── manifest.json                      Microsoft 365 app manifest, schema 1.28
├── color.png, outline.png             192×192 and 32×32 icons
├── tools/sas-viya-tools.json          tool descriptions (required by the schema, not used by Cowork)
└── skills/
    ├── sas-viya-data-exploration/     find, profile and query CAS and library tables
    │   └── SKILL.md
    └── sas-viya-run-sas-code/         write and run SAS code, read the log, report results
        └── SKILL.md
```

- **The connector** (`agentConnectors`) is a `remoteMcpServer` pointing at the server's `/mcp` endpoint. It carries **no `authorization` block**: the server supports OAuth dynamic client registration, and Cowork then registers its own client. Nothing to register in Microsoft 365 and no client ID in the manifest.
- **The skills** (`agentSkills`) are [Agent Skills](https://agentskills.io/) — the same `SKILL.md` format Claude Code, VS Code and others read. Each names the server's tools explicitly (`list_castables`, `query_data`, `execute_sas_code`, …) and prescribes a workflow and an output format. Cowork picks a skill from its `description`, so that is where the trigger phrases are.
- **The tool-description file** is a copy of the server's `tools/list` for tiers 0–1. Cowork discovers tools from the server at run time and ignores the file, but schema 1.28 makes `mcpToolDescription` a required property, so it has to be there.

## What the server needs

1. **A URL Microsoft's cloud can reach over HTTPS**, as for the other two routes: any deployment in [deploy/](../../deploy/README.md), with `MCP_BASE_URL` set to the public URL and `<MCP_BASE_URL>/auth/callback` registered on the SAS Viya OAuth client.
2. **A tool list sized for the job.** The skills use tiers 0–1 (`MCP_TIERS=0-1`, 24 tools).
3. **Tool annotations** — nothing to do. Cowork asks for confirmation before any tool that is not marked read-only; this server marks every tool, so listing tables runs straight through and `execute_sas_code` asks first.

Not yet known, because it could not be tested:

| Question | Why it matters | Safe choice until tested |
| --- | --- | --- |
| Does Cowork register as a public client or ask for a client secret? | Microsoft 365 Copilot's registration service requires a secret; Copilot Studio does not | `MCP_OAUTH_CLIENT_SECRETS=true` — it serves both kinds |
| Does Cowork send `initialize` or only `server/discover`? | Microsoft 365 Copilot only works against the stateless transport. The documentation says Cowork sends `clientInfo` on `initialize`, which suggests the default transport is fine | `MCP_STATELESS_HTTP=true` — both handshakes work there |
| Do tool names starting with `catalog_` fail, as in Copilot Studio? | Tiers 0–1 include nine of them | If every request fails, set `MCP_TOOL_RENAME=catalog_=infocat_` |

All three settings together are a tested combination for the other two routes, so one deployment can serve all three.

## Build the package

```sh
uv run --with jsonschema python examples/m365-cowork/build_package.py \
    --url https://sas-mcp.example.com/mcp
```

This checks the package and writes `appPackage/build/sas-viya-cowork.zip` with your endpoint in the manifest. The checks are the v1.28 schema plus the rules Microsoft documents: the skill rules (`ASKILL-M001`…`P008`), the connector rules, the companion-file limits and the icon sizes. `--check-only` runs them without writing a zip.

To validate with Microsoft's own tool as well:

```sh
npx @microsoft/m365agentstoolkit-cli validate --package-file examples/m365-cowork/appPackage/build/sas-viya-cowork.zip
```

Before you distribute the package, change `id` in `manifest.json` to a GUID of your own and keep it stable across versions.

## Install it

**For yourself (sideload):**

```sh
npx @microsoft/m365agentstoolkit-cli auth login m365
npx @microsoft/m365agentstoolkit-cli install --file-path examples/m365-cowork/appPackage/build/sas-viya-cowork.zip --scope Personal
```

The command prints a `TitleId` and an `AppId`; keep them for updating or removing the plugin (`uninstall --mode title-id --title-id <TitleId>`).

**For your organization:** Microsoft 365 admin center → **Manage apps** → **Upload custom app** → **…** → **Add agent**, and upload the zip. It then appears in Cowork under **Sources & Skills** → **Plugins** → **Discover**.

Then open [Microsoft Copilot](https://copilot.cloud.microsoft), switch the toggle at the top from **Chat** to **Cowork**, and ask something that needs SAS Viya — *"Which CAS tables do I have, and how many rows are in the largest one?"* The first tool call opens the sign-in to SAS Viya.

If there is no **Cowork** toggle, Cowork is not enabled for your account. It needs a Microsoft 365 Copilot license, Cowork turned on for the tenant, and usage-based billing enabled — all administrator settings.

## Starting from a Claude Code plugin instead

Microsoft's toolkit can convert a Claude Code or Cursor plugin (`.claude-plugin/plugin.json`, `.mcp.json`, `skills/`) into this package shape with `atk import openplugin`, and back with `atk export openplugin`. The skills in this folder are plain Agent Skills, so they also work unchanged in Claude Code: copy the two folders into `.claude/skills/`. Slash commands, sub-agents and hooks are not converted.

## What we found while building it

Three places where the documentation and the tools disagree, as of October 2026:

- **`mcpToolDescription` is documented as optional but schema 1.28 requires it.** A connector without it fails schema validation with *"'mcpToolDescription' is a required property"*.
- **The documented path form `./tools/<file>.json` is rejected by the toolkit validator** — *"declared MCP tool description file ./tools/… not found in the app package"* — although the file is in the zip. Without the leading `./` it passes. `build_package.py` flags the `./` form.
- **Skill folders, by contrast, are written with `./`** in Microsoft's example, and that form passes.

## Troubleshooting

- **Upload fails with *"Property '…' has not been defined and the schema does not allow additional properties"*.** Schema 1.28 allows no extra root properties — a field that is fine in a Teams manifest, such as `packageName`, has to go.
- **A skill never activates.** Its folder name and the `name` in its front matter must be identical and kebab-case; the description has to contain the phrases people actually use.
- **Every tool call asks for confirmation.** Cowork treats a tool without annotations as destructive. This server annotates every tool; a proxy in front of it that strips `annotations` would cause this.
- **Testing against a server on your machine.** Use a tunnel, and create its port with `--protocol http` — the flag describes the local service, not the public URL. With `https` every request returns `502`.
