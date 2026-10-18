# Microsoft 365 Copilot integration (declarative agent)

This example wraps the SAS Viya MCP Server as an **action inside a declarative agent for Microsoft 365 Copilot**, following Microsoft's [Build declarative agents for Microsoft 365 Copilot with MCP](https://devblogs.microsoft.com/microsoft365dev/build-declarative-agents-for-microsoft-365-copilot-with-mcp/) announcement and the [Build a plugin from an MCP server](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/build-mcp-plugins) guide.

The result is a **"SAS Viya Analyst"** agent in the Copilot sidebar: users ask data questions in natural language, Copilot calls the MCP server's tools (list CAS tables, profile columns, run FedSQL queries, execute SAS code), and every user is authenticated against Viya as themselves.

To add the server to a Copilot Studio agent instead, with nothing to package, see [copilot-studio](../copilot-studio/README.md); [microsoft-copilot.md](../microsoft-copilot.md) compares the two.

## How it fits together

```
Microsoft 365 Copilot                    SAS Viya MCP Server                SAS Viya
┌─────────────────────┐   MCP over      ┌──────────────────────┐          ┌──────────────┐
│  Declarative agent   │  streamable    │  https://…/mcp        │  Viya    │  CAS, Compute │
│  (this app package)  │ ───HTTP+OAuth─▶│  OAuth proxy → SASLogon│──REST──▶│  SASLogon     │
└─────────────────────┘                 └──────────────────────┘          └──────────────┘
```

- Copilot connects to the server's **streamable HTTP** endpoint (`/mcp`). Local stdio servers cannot be used — Microsoft 365 only reaches **remotely hosted** MCP servers over HTTPS.
- On the first tool call, Copilot pops a sign-in window. The MCP server's built-in OAuth proxy sends the user through **SAS Logon**, so results always reflect that user's own Viya permissions — no shared service account.
- The agent's tool list comes from the server at runtime (**dynamic tool discovery**), so what you expose is controlled server-side with `MCP_TIERS` / `MCP_READ_ONLY` — change the deployment's scope and the agent follows, with no repackaging. Copilot re-validates each changed tool definition before activating it.
- Every tool advertises **MCP tool annotations** (`readOnlyHint`, `destructiveHint`, …), and Copilot acts on them: a read-only tool runs after a one-time **Allow**, anything else shows its arguments with **Confirm** / **Cancel** first.
- The server's **interactive views** ([MCP apps](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-mcp-apps)) render inline in Copilot: the data grid for table previews and query results (filter, paging, full screen) and the SAS log viewer for `execute_sas_code`. The log viewer's *Results* tab cannot embed the HTML results page inside Copilot's sandbox and offers **Open in browser** instead.

Verified against Microsoft 365 Copilot on 2 October 2026 with server 1.18.0 and all tiers (92 tools): registration, sign-in, tool calls with and without confirmation, and both views, in dynamic and in pinned mode.

## Prerequisites

1. **A remotely hosted SAS Viya MCP Server with TLS.** Microsoft's cloud has to reach it, so it needs a public HTTPS endpoint — a local stdio server or a cluster-internal service cannot be used. The [Kubernetes deployment](../../deploy/README.md) (sample manifest or Helm chart) is the intended shape: it serves `/mcp` **and the OAuth sibling paths** (`/authorize`, `/token`, `/register`, `/consent`, `/auth/callback`, and the two `/.well-known/` documents) — all eight must be reachable, or clients can connect but never sign in.

    **The easiest shape for Copilot is to mount the server on the Viya hostname itself**, at `https://<your-viya-host>/sas-mcp/mcp`. Since chart 0.2.0 that is `ingress.controller: contour` plus `ingress.pathPrefix: /sas-mcp` — see [CONTOUR-DEPLOYMENT.md](../../deploy/CONTOUR-DEPLOYMENT.md), and [AKS-CONTOUR.md](../../deploy/AKS-CONTOUR.md) if you are on Azure. It reuses Viya's certificate, DNS record and load balancer (no second hostname to get approved), and because the server is then same-origin with SAS Logon it sidesteps the `form-action` CSP problem in the next prerequisite entirely. The chart handles the routing the prefix needs — including the two discovery documents, which stay at the host root.

    **Check it from outside your network before you go near Copilot:** open `https://<host>/mcp` in a browser. Since 1.10.0 that returns a landing page describing the deployment (name, Viya host, exposed tiers, per-client configuration snippets) instead of a bare `401`, which makes it a one-glance reachability and configuration test. If you get a certificate warning, Copilot will fail too — Microsoft's cloud will not accept a self-signed chain.
2. **The standard Viya-side setup** from [configuration.md](../configuration.md): the `sas-mcp` OAuth client registered with `redirect_uri` = `<MCP_BASE_URL>/auth/callback`, and the `form-action` CSP addressed — mounting on the Viya hostname (prerequisite 1) makes the callback same-origin and sidesteps it.
3. **A Microsoft 365 tenant** where your account shows **Custom App Upload Enabled** and **Copilot Access Enabled** (Copilot license or metered usage). See [Microsoft's prerequisites](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/prerequisites).
4. For the guided path (Option A): [Visual Studio Code](https://code.visualstudio.com/) with the [Microsoft 365 Agents Toolkit](https://aka.ms/M365AgentsToolkit) **6.12.0 or later**.

### Recommended server configuration for this demo

Copilot sees every tool the server exposes, and this server has 92 of them — far too many for reliable tool selection. Scope the deployment to the demo's needs:

```sh
MCP_TIERS=0-1        # 24 tools: Compute & Code Execution + Data Discovery
MCP_BASE_URL=https://sas-mcp.example.com   # the external HTTPS URL of the server
MCP_OAUTH_CLIENT_SECRETS=true              # required for dynamic registration, see below
MCP_STATELESS_HTTP=true                    # required: Copilot speaks only the sessionless protocol
```

`MCP_STATELESS_HTTP=true` is not optional either. Copilot's runtime opens every turn with `server/discover` — the handshake of the sessionless 2026-07-28 protocol revision — and never sends `initialize`. The default stateful transport answers that with HTTP 400 (*Missing session ID*), which Copilot shows as a reply that never arrives.

`MCP_OAUTH_CLIENT_SECRETS=true` is what lets Copilot register itself. Microsoft's registration service insists on a confidential client: it refuses a server whose OAuth metadata lists no secret-based method — *"The authorization server does not support client_secret_basic or client_secret_post token endpoint authentication methods"* — and then a registration that comes back without a `client_secret`. By default this server registers every client as a public one, which is what other MCP clients expect; the setting makes it issue (and then require) a secret for a client that asks.

Tiers 0–1 cover the whole demo flow (discover → profile → query → run SAS code). Add tier `3` for Visual Analytics report tools. `MCP_READ_ONLY=true` is a good fit for a locked-down, exploration-only agent — but note it withholds `execute_sas_code`, which the demo's "run SAS code" scenario uses.

## Choosing the authentication type

When the Agents Toolkit asks for an authentication type, this server maps to the options like so:

| M365 auth option | Works with this server? | How |
| --- | --- | --- |
| **OAuth (dynamic registration)** | ✅ **Recommended** | The server's OAuth proxy implements MCP-spec dynamic client registration itself — Copilot registers a client against the *MCP server* at setup, and Viya needs nothing beyond the standard `sas-mcp` client. No client ID or secret to manage in M365. |
| **OAuth (static registration)** | ✅ Alternative | Requires `ALLOW_RAW_BEARER=true` on the server. Register a **separate confidential client in Viya** whose `redirect_uri` is `https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect`; give M365 that client ID + secret with authorization URL `https://<viya>/SASLogon/oauth/authorize` and token URL `https://<viya>/SASLogon/oauth/token` (scope `openid`). Copilot then sends the Viya access token directly as a bearer, which the server validates against Viya's JWKS. |
| **Entra SSO** | ❌ | SAS Logon is the identity provider here, not Microsoft Entra. |
| **None** | Only if `VIYA_AUTH=false` | For deployments that are deliberately unauthenticated (e.g. isolated demo stacks). |

<details>
<summary>Static-registration client example (curl)</summary>

```sh
curl -k -X POST "https://YOUR_VIYA_ENDPOINT/SASLogon/oauth/clients" \
   -H "Content-Type: application/json" \
   -H "Authorization: Bearer $BEARER_TOKEN" \
   -d '{"client_id": "sas-mcp-m365",
      "client_secret": "CHOOSE-A-STRONG-SECRET",
      "scope": ["openid"],
      "authorized_grant_types": ["authorization_code","refresh_token"],
      "redirect_uri": "https://teams.microsoft.com/api/platform/v1.0/oAuthRedirect",
      "autoapprove": true}'
```

See [configuration.md](../configuration.md#step-2-register-an-oauth-client-for-the-mcp-server) for how to obtain `$BEARER_TOKEN`.

</details>

## Option A — build it with the Microsoft 365 Agents Toolkit (guided)

This mirrors the blog post's walkthrough; the toolkit generates the same three files you'll find in [`appPackage/`](appPackage/).

1. In VS Code, open the **Microsoft 365 Agents Toolkit** panel and select **Create a New Agent/App** → **Declarative Agent**.
2. Select **Add an Action** → **Start with an MCP Server**.
3. Enter your server URL: `https://sas-mcp.example.com/mcp`.
4. Select the authentication type — **OAuth (with dynamic registration)** for a default deployment (see the table above). The toolkit creates the auth configuration and wires its ID into the plugin manifest for you.
5. Name the agent (e.g. *SAS Viya Analyst*) and let the toolkit scaffold the project.
6. Replace the generated `declarativeAgent.json` `instructions` and `conversation_starters` with the ones from [`appPackage/declarativeAgent.json`](appPackage/declarativeAgent.json) — they encode a discovery-first workflow tuned to this server's tools.
7. In the **Accounts** pane, sign in to Microsoft 365; in the **Lifecycle** pane, select **Provision** to sideload the agent into your tenant.

> Microsoft also ships a command-line path for the same loop, **Work IQ Dev Tools** (`wiqd agent package`, `wiqd agent provision --env local`). It is in preview and its connector commands are marked alpha, so the toolkit UI above is the safer route for now — but it is the one to watch if you want this in CI. See the [Work IQ Dev Tools documentation](https://aka.ms/wiqd/docs).

## Option B — use the pre-built app package in this folder

[`appPackage/`](appPackage/) is a complete, sideload-ready package; only two values need replacing.

1. **Set your server URL** in [`appPackage/ai-plugin.json`](appPackage/ai-plugin.json): replace `https://sas-mcp.example.com/mcp`.
2. **Create the authentication configuration** and set its ID. The auth config is a record in the Microsoft token store that Copilot uses at runtime; create it in the [Teams Developer Portal](https://dev.teams.microsoft.com/) (or let the Agents Toolkit do it) following [Microsoft's plugin authentication guide](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-authentication). Replace `REPLACE-WITH-OAUTH-CONFIG-ID` in `ai-plugin.json` with the resulting ID. (For a `VIYA_AUTH=false` server, use `"auth": { "type": "None" }` instead.)
3. Optionally replace the placeholder icons (`color.png` 192×192, `outline.png` 32×32) with real branding.
4. **Zip the package** — the files must sit at the zip root:

    ```powershell
    # PowerShell, from inside appPackage/
    Compress-Archive -Path * -DestinationPath ..\sas-viya-analyst.zip -Force
    ```

    ```sh
    # bash, from inside appPackage/
    zip -r ../sas-viya-analyst.zip .
    ```

5. **Sideload it**: in Teams (or [m365.cloud.microsoft](https://m365.cloud.microsoft/)), go to **Apps** → **Manage your apps** → **Upload an app** → **Upload a custom app** and pick the zip. Organization-wide rollout instead goes through the [Microsoft 365 admin center](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/publish) or Partner Center.

## Try it

Open [Copilot chat](https://m365.cloud.microsoft/chat), pick **SAS Viya Analyst** from the **Agents** list, and start with a conversation starter, or:

- *"What CAS tables are available to me?"*
- *"Profile the columns of the CARS table in the Samples caslib."*
- *"Using FedSQL, show the 10 heaviest cars with their make and model."*
- *"Run PROC MEANS on MSRP and summarize what it tells us."*

The first tool call prompts **Sign in** — that's the OAuth flow handing the user to SAS Logon. Subsequent calls reuse the session.

## Pinned tools instead of dynamic discovery

The shipped `ai-plugin.json` uses **dynamic tool discovery**: an empty `functions` array and `"run_for_functions": ["*"]`, so Copilot fetches the tool list from the server per session. Pin a **fixed, curated list** into the app package instead when you need the surface frozen — store validation, or a tool list that cannot move under a published agent.

Pinning is **three edits, not one**, and the first two are easy to miss: the tool *names* go in the manifest's top-level `functions` array **and** in the runtime's `run_for_functions`, while their *definitions* go in `mcp_tool_description`. A package that only sets `mcp_tool_description` declares no functions and the agent calls nothing.

```json
"functions": [
    { "name": "list_caslibs" },
    { "name": "query_data" }
],
"runtimes": [
    {
        "type": "RemoteMCPServer",
        "auth": { "type": "OAuthPluginVault", "reference_id": "…" },
        "spec": {
            "url": "https://sas-mcp.example.com/mcp",
            "mcp_tool_description": { "file": "mcp-tools.json" }
        },
        "run_for_functions": ["list_caslibs", "query_data"]
    }
]
```

The checked-in `mcp-tools.json` holds the 24 tier 0–1 tools, generated from the actual server code — never hand-edit it. `--plugin-snippet` prints the two name arrays ready to paste, so nobody types 24 names by hand:

```sh
uv run python examples/m365-copilot/generate_tools_manifest.py --tiers 0-1 --plugin-snippet
uv run python examples/m365-copilot/generate_tools_manifest.py --tiers 0-3 --read-only
```

**The pinned file carries each tool's annotations and view binding, and it has to.** Plugin manifest schema 2.4 describes a pinned tool as `name`, `description` and `inputSchema`, and an earlier version of the generator kept to that. Against Copilot it is the wrong reading: a pinned tool without `readOnlyHint` is treated as consequential, so **every** call — listing caslibs included — stops for a **Confirm**, and without `_meta.ui` no view is rendered. With both in the file (verified with the definitions inline in `ai-plugin.json`), a pinned list behaves exactly like dynamic discovery. The view URIs written are the stable, un-fingerprinted ones every deployment serves, so the views survive a server upgrade.

The Agents Toolkit can do the same job interactively: open `.vscode/mcp.json`, select **Start**, then **ATK: Fetch action from MCP** → `ai-plugin.json`, and pick the tools. It writes both arrays and the definitions for you.

When the pinned list changes, bump `version` in `manifest.json` and re-upload the zip. This is the cost of pinning: every server-side tool change needs a repackage, whereas dynamic discovery picks it up at runtime.

## Validate before you sideload

Sideloading reports a bad package as one unhelpful message, so check it first:

```sh
uv run python examples/m365-copilot/validate_package.py
```

It validates all three manifests against Microsoft's **published** schemas (fetched live, so a schema revision surfaces here rather than at upload), confirms every referenced file exists and the icons really are 192×192 and 32×32, and checks that the tool wiring is *complete* in whichever mode you chose.

**Two schema errors are expected, and the script reports them as tolerated.** They are defects in Microsoft's published v2.4 schema, not in this package — two `oneOf` branch sets that overlap, so a correct document matches two branches and is rejected:

| Where | Why it fails | Consequence |
| --- | --- | --- |
| `runtime.spec` | `{"url": …}` satisfies both `open-api-spec` and `mcp-execution-spec` | Every MCP plugin manifest hits it, including the dynamic-discovery example in Microsoft's own docs |
| `mcp_tool_description` | `{"file": …}` satisfies both `mcp-tool-file-reference` and `mcp-tool-inline` (that branch is an unconstrained object) | Only the **inline** form of pinned tools validates; the file form does not |

Neither blocks sideloading today — the runtime does not enforce the published schema. But if you hit a validator that does (store submission, a CI step), pinning tools *inline* rather than by `file` is the form that passes. Run `--strict` to check whether Microsoft has fixed either one.

## Files in this folder

| File | Purpose |
| --- | --- |
| `appPackage/manifest.json` | Microsoft 365 app manifest (v1.19); declares the declarative agent |
| `appPackage/declarativeAgent.json` | Agent definition (schema v1.6): name, instructions, conversation starters, action |
| `appPackage/ai-plugin.json` | Plugin manifest (schema v2.4) with the `RemoteMCPServer` runtime pointing at `/mcp` |
| `appPackage/mcp-tools.json` | Generated `tools/list` snapshot for the optional pinned-tools mode |
| `appPackage/color.png`, `appPackage/outline.png` | Placeholder icons — replace with real branding |
| `generate_tools_manifest.py` | Regenerates `mcp-tools.json` from the server code (`--tiers`, `--read-only`, `--plugin-snippet`) |
| `validate_package.py` | Checks the package against Microsoft's schemas and catches incomplete tool wiring before sideload |

## Troubleshooting

- **The agent responds but never calls a tool / "couldn't reach the service".** The server must be reachable from Microsoft's cloud over HTTPS at the exact `spec.url`. Open `https://<host>/mcp` in a browser from outside your network: the landing page means the endpoint and its TLS chain are fine, and it lists the tiers the deployment actually exposes. Then confirm the deployment routes all eight OAuth/MCP paths (see [deploy/](../../deploy/README.md)) — an agent that connects but never signs in is almost always a missing `/.well-known/` or `/authorize` route.
- **Sign-in window opens SAS Logon but never returns.** The `form-action` CSP on SAS Logon is blocking the redirect back to the server's `/auth/callback` — see [configuration.md, Step 1](../configuration.md#step-1-disable-form-action-content-security-policy-on-sas-logon-manager); hosting the MCP server on the Viya host avoids this entirely.
- **Every call fails with 401 in static-OAuth mode.** The server isn't accepting raw Viya bearer tokens — set `ALLOW_RAW_BEARER=true`.
- **Tool calls time out.** Keep demo queries small (the agent instructions already push for `LIMIT`ed previews); long-running SAS jobs are better served by the batch tools in tier 4.
- **Copilot picks the wrong tool or rambles through many calls.** Narrow the server-side surface (`MCP_TIERS`), or switch to pinned tools with a curated subset.
- **Provisioning fails at "Creating DCR configuration" with *"The authorization server does not support client_secret_basic or client_secret_post token endpoint authentication methods"*, or *"IDP registration response is missing required field: client_secret"*.** The server is registering every client as public — set `MCP_OAUTH_CLIENT_SECRETS=true` and restart it.
- **The agent asks to connect, and then the reply never arrives.** Look for `POST /mcp` → `400` in the server log: that is `server/discover` being refused by the stateful transport. Set `MCP_STATELESS_HTTP=true`.
- **The sign-in window says *Client Not Registered*.** Copilot is still serving the previous version of the agent, whose registration this server no longer knows (a re-provision, or a server whose OAuth store was reset). A republished agent takes several minutes to go live; start a new chat and try again.
- **"Oops! Something happened" on every message, including ones that worked a minute ago.** Copilot's chat drops its connection while its browser tab is hidden or covered. Bring the tab to the front and retry before suspecting the server — nothing reaches the server in this case.
- **Copilot asks to confirm every call, even read-only ones, or shows no views (pinned mode).** The pinned definitions were generated without annotations and `_meta` — regenerate `mcp-tools.json` and re-pin.
- **A pinned tool list is stale after a server upgrade.** Regenerate `mcp-tools.json`, bump the app `version`, re-zip, re-upload. Dynamic discovery avoids this class of problem entirely.
- **Pinned mode: the agent has an action but calls nothing.** The `functions` array or `run_for_functions` is still empty — `mcp_tool_description` alone does not declare the functions. Run the generator with `--plugin-snippet` and paste both arrays.

## References

- [Build declarative agents for Microsoft 365 Copilot with MCP](https://devblogs.microsoft.com/microsoft365dev/build-declarative-agents-for-microsoft-365-copilot-with-mcp/) (announcement blog)
- [Build a plugin for a declarative agent from an MCP server](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/build-mcp-plugins)
- [Plugin manifest schema v2.4](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-manifest-2.4) (`RemoteMCPServer` runtime, MCP server spec object)
- [Dynamic tool discovery for MCP plugins](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-dynamic-tool-discovery) (pinned vs dynamic, and the admin/audit story)
- [MCP apps in declarative agents](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-mcp-apps) (tools that return UI widgets)
- [Declarative agent manifest schema v1.6](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/declarative-agent-manifest-1.6)
- [Authentication for MCP and API plugins](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-authentication)
