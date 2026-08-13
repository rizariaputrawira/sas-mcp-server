# Microsoft 365 Copilot integration (declarative agent)

This example wraps the SAS Viya MCP Server as an **action inside a declarative agent for Microsoft 365 Copilot**, following Microsoft's [Build declarative agents for Microsoft 365 Copilot with MCP](https://devblogs.microsoft.com/microsoft365dev/build-declarative-agents-for-microsoft-365-copilot-with-mcp/) announcement and the [Build a plugin from an MCP server](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/build-mcp-plugins) guide.

The result is a **"SAS Viya Analyst"** agent in the Copilot sidebar: users ask data questions in natural language, Copilot calls the MCP server's tools (list CAS tables, profile columns, run FedSQL queries, execute SAS code), and every user is authenticated against Viya as themselves.

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
- The agent's tool list comes from the server at runtime (**dynamic tool discovery**), so what you expose is controlled server-side with `MCP_TIERS` / `MCP_READ_ONLY`.

## Prerequisites

1. **A remotely hosted SAS Viya MCP Server with TLS.** The [Kubernetes deployment](../../deploy/README.md) (sample manifest or Helm chart) is the intended shape: it serves `/mcp` **and the OAuth sibling paths at the host root** (`/authorize`, `/token`, `/register`, `/consent`, `/auth/callback`, and the two `/.well-known/` documents) — all eight must be reachable, or clients can connect but never sign in.
2. **The standard Viya-side setup** from [configuration.md](../configuration.md): the `sas-mcp` OAuth client registered with `redirect_uri` = `<MCP_BASE_URL>/auth/callback`, and the `form-action` CSP addressed (hosting the server on the Viya host, as the deploy docs do, sidesteps it).
3. **A Microsoft 365 tenant** where your account shows **Custom App Upload Enabled** and **Copilot Access Enabled** (Copilot license or metered usage). See [Microsoft's prerequisites](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/prerequisites).
4. For the guided path (Option A): [Visual Studio Code](https://code.visualstudio.com/) with the [Microsoft 365 Agents Toolkit](https://aka.ms/M365AgentsToolkit) **6.12.0 or later**.

### Recommended server configuration for this demo

Copilot sees every tool the server exposes, and 70+ tools dilute tool selection. Scope the deployment to the demo's needs:

```sh
MCP_TIERS=0-1        # 23 tools: Compute & Code Execution + Data Discovery
MCP_BASE_URL=https://sas-mcp.example.com   # the external HTTPS URL of the server
```

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

By default the plugin manifest omits tool definitions, so Copilot fetches them from the server at runtime. To ship a **fixed, curated tool list** inside the app package instead (useful for store validation, or to freeze the surface independently of the server), point the runtime spec at [`appPackage/mcp-tools.json`](appPackage/mcp-tools.json):

```json
"spec": {
    "url": "https://sas-mcp.example.com/mcp",
    "mcp_tool_description": { "file": "mcp-tools.json" }
}
```

The checked-in `mcp-tools.json` holds the 23 tier 0–1 tools, generated from the actual server code — never hand-edit it. Regenerate it to match your deployment's scope:

```sh
uv run python examples/m365-copilot/generate_tools_manifest.py --tiers 0-1
uv run python examples/m365-copilot/generate_tools_manifest.py --tiers 0-3 --read-only
```

When the pinned file changes, bump `version` in `manifest.json` and re-upload the zip.

## Files in this folder

| File | Purpose |
| --- | --- |
| `appPackage/manifest.json` | Microsoft 365 app manifest (v1.19); declares the declarative agent |
| `appPackage/declarativeAgent.json` | Agent definition (schema v1.6): name, instructions, conversation starters, action |
| `appPackage/ai-plugin.json` | Plugin manifest (schema v2.4) with the `RemoteMCPServer` runtime pointing at `/mcp` |
| `appPackage/mcp-tools.json` | Generated `tools/list` snapshot for the optional pinned-tools mode |
| `appPackage/color.png`, `appPackage/outline.png` | Placeholder icons — replace with real branding |
| `generate_tools_manifest.py` | Regenerates `mcp-tools.json` from the server code (`--tiers`, `--read-only`) |

## Troubleshooting

- **The agent responds but never calls a tool / "couldn't reach the service".** The server must be reachable from Microsoft's cloud over HTTPS at the exact `spec.url`. Verify `https://<host>/mcp` responds and that the deployment routes all eight OAuth/MCP paths (see [deploy/](../../deploy/README.md)).
- **Sign-in window opens SAS Logon but never returns.** The `form-action` CSP on SAS Logon is blocking the redirect back to the server's `/auth/callback` — see [configuration.md, Step 1](../configuration.md#step-1-disable-form-action-content-security-policy-on-sas-logon-manager); hosting the MCP server on the Viya host avoids this entirely.
- **Every call fails with 401 in static-OAuth mode.** The server isn't accepting raw Viya bearer tokens — set `ALLOW_RAW_BEARER=true`.
- **Tool calls time out.** Keep demo queries small (the agent instructions already push for `LIMIT`ed previews); long-running SAS jobs are better served by the batch tools in tier 4.
- **Copilot picks the wrong tool or rambles through many calls.** Narrow the server-side surface (`MCP_TIERS`), or switch to pinned tools with a curated subset.
- **A pinned tool list is stale after a server upgrade.** Regenerate `mcp-tools.json`, bump the app `version`, re-zip, re-upload.

## References

- [Build declarative agents for Microsoft 365 Copilot with MCP](https://devblogs.microsoft.com/microsoft365dev/build-declarative-agents-for-microsoft-365-copilot-with-mcp/) (announcement blog)
- [Build a plugin for a declarative agent from an MCP server](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/build-mcp-plugins)
- [Plugin manifest schema v2.4](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-manifest-2.4) (`RemoteMCPServer` runtime)
- [Declarative agent manifest schema v1.6](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/declarative-agent-manifest-1.6)
- [Authentication for MCP and API plugins](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/plugin-authentication)
