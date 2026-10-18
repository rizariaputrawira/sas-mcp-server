# Microsoft Copilot: which integration to use

The server can be used from Microsoft's Copilot products in two ways. Both talk to the same remote deployment, and both sign each person in to SAS Viya as themselves, so results always follow that person's SAS Viya permissions.

| | [Microsoft 365 Copilot declarative agent](m365-copilot/README.md) | [Copilot Studio agent](copilot-studio/README.md) |
| --- | --- | --- |
| **Where people use it** | In Microsoft 365 Copilot chat, as an agent next to their mail, files and meetings | Wherever the agent is published from Copilot Studio; tested in its own test chat |
| **How you build it** | An app package (three JSON manifests and two icons), from the Agents Toolkit or the pre-built package here, sideloaded or rolled out from the Microsoft 365 admin center | In the browser: **Tools** → **Add** → **Model Context Protocol**, then paste the server URL. Nothing to package |
| **Interactive views** | Rendered: the data grid and the SAS log viewer appear inline | Not rendered. Results are text, and `execute_sas_code` returns a link to its HTML results |
| **Model and orchestration** | Microsoft 365 Copilot's own orchestrator | The model you pick for the agent (tested with Claude Opus 5), alongside its other knowledge, topics and tools |
| **Choosing tools** | On the server (`MCP_TIERS`, `MCP_READ_ONLY`), or a pinned subset in the package | On the server, or one switch per tool on the agent's **Build** tab |
| **When the server's tools change** | Dynamic discovery picks the change up at run time; a pinned list needs a new package version | The tool list is cached per connector, so add the server again as a new MCP server entry |
| **Server settings it needs** | `MCP_OAUTH_CLIENT_SECRETS=true` and `MCP_STATELESS_HTTP=true` | `MCP_TOOL_RENAME=catalog_=infocat_` |
| **What your Microsoft tenant needs** | Custom app upload and Copilot access for each user | Access to a Copilot Studio environment for the person building the agent |
| **Status** | Verified live with all 92 tools, dynamic and pinned | Verified live with all 92 tools |

## When to choose which

**Choose the Microsoft 365 Copilot declarative agent when:**

- Your users already work in Microsoft 365 Copilot and should find SAS Viya there, without opening another tool.
- They should see results the way SAS Viya shows them. Only this route renders the data grid for tables and query results and the SAS log viewer for submitted code.
- You want the tools to follow the deployment. With dynamic discovery, a change to `MCP_TIERS` reaches every user without repackaging.
- Distribution should go through the Microsoft 365 admin center, like any other app in the tenant.

**Choose a Copilot Studio agent when:**

- You want to try the server in minutes. Adding it takes a URL and a sign-in, with no manifest to write and no app upload rights.
- You want control over the agent itself: its model, its instructions, and the exact tools it may call.
- The SAS Viya tools are one part of a larger agent that also uses other knowledge sources, connectors or flows.
- The agent should be offered somewhere other than Microsoft 365 Copilot chat. Copilot Studio publishes to several channels; only its test chat was tried here.
- Text answers are enough. Without views, wide tables and long logs come back as text and as a link to the HTML results.

**You do not have to choose.** One deployment serves both. With all three settings on, Copilot Studio still registers as a public client and gets no secret, and the rename does not affect Microsoft 365 Copilot. This combination was tested against both clients.

```sh
MCP_BASE_URL=https://sas-mcp.example.com
MCP_OAUTH_CLIENT_SECRETS=true       # Microsoft 365 Copilot registration
MCP_STATELESS_HTTP=true             # Microsoft 365 Copilot opens with server/discover
MCP_TOOL_RENAME=catalog_=infocat_   # Copilot Studio fails on names starting with catalog_
```

## What both need

- **A public HTTPS URL.** Both products call the server from Microsoft's cloud, so `localhost` and cluster-internal addresses do not work. The [Kubernetes deployment](../deploy/README.md), mounted on the SAS Viya hostname under a path prefix, is the simplest shape. A tunnel to a local server is enough for a trial.
- **The callback registered in SAS Logon.** `<MCP_BASE_URL>/auth/callback` must be a redirect URI of the server's OAuth client. [register_mcp_client.py](register_mcp_client.py) reads `MCP_BASE_URL` and registers it.
- **A tool list sized for the job.** All 92 tools work in both, but a smaller list helps the model pick the right one. Tiers 0–1 (24 tools) cover finding, profiling and querying data and running SAS code; add tier 3 for Visual Analytics reports.
