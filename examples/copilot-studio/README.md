# Microsoft Copilot Studio integration

A Copilot Studio agent can use this server as a tool: the agent's model picks from the server's tools, and every call runs in SAS Viya as the person who signed in. This is the no-code route — nothing to package or sideload. For a declarative agent that lives in Microsoft 365 Copilot itself (and renders the interactive views), see [m365-copilot](../m365-copilot/README.md) instead; [microsoft-copilot.md](../microsoft-copilot.md) compares the two.

Verified against Copilot Studio on 1 October 2026 with server 1.18.0, all tiers (92 tools), an agent on Claude Opus 5: sign-in, tool discovery and multi-step tool calls work.

## What the server needs

1. **A URL Microsoft's cloud can reach over HTTPS.** Copilot Studio calls the server from Microsoft's datacenters, not from your browser, so `localhost` will not do. Any of the deployments in [deploy/](../../deploy/README.md) works; for a quick trial, a tunnel to a local `uv run app` is enough. Set `MCP_BASE_URL` to that public URL, and register `<MCP_BASE_URL>/auth/callback` on the SAS Viya OAuth client — `examples/register_mcp_client.py` reads `MCP_BASE_URL` and registers it.
2. **`MCP_TOOL_RENAME=catalog_=infocat_`.** Copilot Studio's agent runtime fails on tool names that start with `catalog_` — see [the catalog_ problem](#the-catalog_-problem) below. With this setting the nine catalog tools are presented as `infocat_search`, `infocat_run_agent` and so on; nothing else changes.

Nothing else is special. The default OAuth sign-in is what Copilot Studio uses, `MCP_STATELESS_HTTP` can stay at its default (both modes were tested), and `MCP_TIERS` / `MCP_READ_ONLY` scope the tool list as they do for any client.

**One deployment can serve Copilot Studio and Microsoft 365 Copilot together.** The settings the [declarative agent](../m365-copilot/README.md) needs — `MCP_OAUTH_CLIENT_SECRETS=true` and `MCP_STATELESS_HTTP=true` — do not disturb Copilot Studio: it still registers as a public client (`token_endpoint_auth_method: none`) and is given no secret, and the rename does not affect Microsoft 365 Copilot. All three settings together were tested against both clients.

```sh
MCP_BASE_URL=https://sas-mcp.example.com
MCP_TOOL_RENAME=catalog_=infocat_
```

## Add the server to an agent

1. In [Copilot Studio](https://copilotstudio.microsoft.com/), open an agent (or create one: **Agents** → **New agent**) and stay on the **Build** tab.
2. Next to **Tools**, select **+** → **+ Add** → **Model Context Protocol (MCP)**.
3. Fill in the form:
   - **Server name** — for example *SAS Viya*. It becomes a connector in your environment, so the name has to be unused there; a name that is taken fails with *"Can't create MCP server"*.
   - **Server description** — what the tools are for; the agent's model reads it.
   - **Server URL** — the MCP endpoint, `https://sas-mcp.example.com/mcp`.
   - **Authentication** — **OAuth 2.0**, configuration type **Dynamic (with discovery)**.
4. Select **Add**. Copilot Studio reads the server's OAuth metadata and registers itself as a client — there is no client ID or secret to copy.
5. Under **Connection**, choose **Create new connection** → **Create**. A window opens on the server's consent page naming the callback `https://global.consent.azure-apim.net/redirect/...`; select **Allow Access**, then sign in to SAS Viya if you are not already.
6. Back in the dialog the connection shows as connected. Select **Add**, then **Save** the agent.
7. Open the **Preview** tab and ask something that needs SAS Viya — *"List the CAS servers, then the caslibs on the first one."*

The tool's entry on the Build tab lists every tool the server offers with a switch each, if you want the agent to see fewer than the server exposes.

## Getting into the pre-populated list

The steps above add the server to one agent. The **Model Context Protocol** and **Connector** lists in the *Add tool* dialog are fed by Power Platform connectors, and a server gets into them for every customer through Microsoft's [MCP server certification](https://learn.microsoft.com/en-us/microsoft-copilot-studio/mcp-server-certification): the service owner packages it as a connector and submits it in Partner Center. A draft of that package is in [certified-connector](certified-connector/README.md). It asks the person creating a connection for the address of their own server and SAS Viya environment, since every customer hosts their own. It has not been imported or submitted; its README lists what is still open.

## What to expect

- **Each person signs in as themselves.** The connection uses *End user account* authentication, so results follow that person's SAS Viya permissions.
- **Interactive views are not rendered.** Copilot Studio's agent runtime does not implement the MCP Apps extension: it announces no UI capability and never reads a view. Tools return the same data either way, and `execute_sas_code` still gives the link to its HTML results.
- **The tool list is cached per connector.** The runtime reads `tools/list` the first time the connector is used and not again — removing and re-adding the tool does not refresh it. After changing what the server exposes (`MCP_TIERS`, `MCP_READ_ONLY`, `MCP_TOOL_RENAME`, an upgrade that adds tools), create a new MCP server entry.
- **The server log shows the original tool names.** A call to `infocat_search` is logged, counted and authorised as `catalog_search`.

## The `catalog_` problem

With the nine `catalog_*` tools in the list, every message to the agent ends in

> Sorry, an unexpected error occurred. Please try again, or contact your administrator with reference code MCS-2011. (Error code: SystemError)

before any tool is called, including for questions that need no tool. The server's side of the exchange is clean — `initialize` and `tools/list` both succeed — and the Build tab lists the tools without complaint; only the conversation fails.

It is the name prefix and nothing else. The complete, unmodified tool list works as soon as those nine names start with something other than `catalog_`: `infocat_search` works, and so does `sas_catalog_search`. Descriptions, parameter schemas, output schemas, annotations, the number of tools and the response encoding were each ruled out separately. The Microsoft 365 Copilot declarative-agent runtime is not affected.

`MCP_TOOL_RENAME` is the workaround. It changes only what the client is shown; see [configuration.md](../configuration.md#environment-file-options) for the details.

## Troubleshooting

- **`MCS-2011` on every message.** A tool name starting with `catalog_` is listed — set `MCP_TOOL_RENAME` as above, restart the server, and add the server to the agent again as a *new* MCP server (the old entry keeps its cached tool list).
- **"Can't create MCP server. Try again."** The server name is already used by a connector in the environment. Pick another name, or reuse the existing one from the **Add a tool** search.
- **The sign-in window shows *Invalid redirect*.** SAS Logon does not know `<MCP_BASE_URL>/auth/callback`; register it on the Viya OAuth client (see [TROUBLESHOOTING.md](../../TROUBLESHOOTING.md)).
- **Nothing reaches the server.** Copilot Studio calls from Microsoft's network: the URL must resolve and answer publicly. `curl -i https://sas-mcp.example.com/mcp` from outside your network should return `401` with a `WWW-Authenticate` header.
