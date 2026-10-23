# SAS Viya MCP connector

> **Draft package, not submitted and not yet run.** These are the files Microsoft's connector certification asks for, prepared so a submission can start from something concrete. The connector has not been imported into a Power Platform environment, so the connection dialog, the sign-in and the routing to a customer's server are untested. See [Open points](#open-points).

## Overview

The SAS Viya MCP connector gives Microsoft Copilot Studio agents tools for a SAS Viya environment through the [SAS Viya MCP Server](https://github.com/sassoftware/sas-mcp-server). Agents discover the tools at run time and use them to find CAS and library tables, profile columns, query data with FedSQL and run SAS code. Every call runs as the person who signed in, with that person's SAS Viya permissions.

The server is deployed by each customer next to their own SAS Viya environment. The connector therefore has no fixed endpoint: the person creating a connection enters the address of their server and of their SAS Viya environment.

## Prerequisites

- A SAS Viya MCP Server with `MCP_TOOL_RENAME` support (1.19.0 or later) reachable from the internet over HTTPS, with:
  - `ALLOW_RAW_BEARER=true`, so the server accepts the SAS Viya access token the connector sends;
  - `MCP_TOOL_RENAME=catalog_=infocat_`, because Copilot Studio fails on tool names that start with `catalog_`.
- An OAuth client in SAS Logon for this connector, registered by a SAS Viya administrator:
  - grant types `authorization_code` and `refresh_token`, scope `openid`, with a client secret;
  - redirect URI: the one Power Platform shows for the connector, of the form `https://global.consent.azure-apim.net/redirect/<connector-id>`.

  ```sh
  curl -X POST "https://<viya-host>/SASLogon/oauth/clients" \
    -H "Authorization: Bearer <administrator access token>" \
    -H "Content-Type: application/json" \
    -d '{
      "client_id": "sas-mcp-copilot-studio",
      "client_secret": "<a secret you choose>",
      "scope": ["openid"],
      "authorized_grant_types": ["authorization_code", "refresh_token"],
      "redirect_uri": ["https://global.consent.azure-apim.net/redirect/<connector-id>"]
    }'
  ```

## Creating a connection

| Field | What to enter | Example |
| --- | --- | --- |
| SAS Viya MCP Server address | Host of the server, without `https://` and without `/mcp`; include the path prefix if it is mounted under one | `sas-mcp.example.com` or `viya.example.com/sas-mcp` |
| SAS Viya host | Host of the SAS Viya environment, where sign-in happens | `viya.example.com` |
| Client ID | The OAuth client registered above | `sas-mcp-copilot-studio` |
| Client secret | Its secret | |

After **Create**, a window opens on the SAS Viya sign-in page.

## Tools

The tools are whatever the connected server exposes; its administrator scopes them with `MCP_TIERS` and `MCP_READ_ONLY`. With the default tiers:

- **Data discovery** — `list_cas_servers`, `list_caslibs`, `list_castables`, `get_castable_info`, `get_castable_columns`, `get_castable_data`, `list_compute_libraries`, `list_compute_tables`, `list_compute_columns`, `get_compute_table_data`, `list_source_tables`
- **Query** — `query_data` (FedSQL `SELECT` over CAS or library tables)
- **Code execution** — `execute_sas_code`, `list_compute_contexts`, `reset_compute_session`
- **Catalog** — `infocat_search` and the other catalog tools, for finding and profiling governed data assets

Further tiers add reports, models, decisions, jobs, files and glossary tools; the [server documentation](https://github.com/sassoftware/sas-mcp-server#readme) lists them.

## Authentication

OAuth 2.0 authorization code flow against the customer's own SAS Logon. The connector sends the resulting SAS Viya access token to the MCP server, which validates it against that environment's signing keys. No credentials are stored by the MCP server, and no shared service account is involved.

## Known issues and limitations

- Copilot Studio reads the tool list once per connection. After the server's tool list changes, create a new connection.
- Copilot Studio does not render the server's interactive views; tools return the same data as text.

## Package contents

| File | Purpose |
| --- | --- |
| `apiDefinition.swagger.json` | One operation, `InvokeServer`, marked `x-ms-agentic-protocol: mcp-streamable-1.0` |
| `apiProperties.json` | The connection fields, generic OAuth with the SAS Viya host in the URLs, and the policy that routes calls to the address entered |
| `icon.png` | Connector icon |
| `README.md` | This file; its content is also what the submission's `intro.md` needs |

## Open points

1. **Untested.** Import the two JSON files as a custom connector (Power Apps → Custom connectors → Import an OpenAPI file, then the properties with the `paconn` CLI) and create a connection against a real deployment before anything is submitted.
2. **Does certification accept a customer-hosted MCP server?** Certified connectors for customer-hosted products use the same host-as-connection-field pattern, but all certified MCP connectors found so far point at one vendor-hosted endpoint.
3. **Does it accept customer-supplied OAuth clients?** The certification text asks for a multitenant OAuth application. Here each customer registers a client in their own SAS Logon. Certified connectors with customer-entered client ID and secret exist; whether that is accepted for an MCP server is a question for Microsoft.
4. **Test environment for Microsoft.** The review exercises every tool with credentials the publisher supplies, so SAS would have to provide a reachable SAS Viya environment with this server and a test user.
5. **Publisher.** Only the service owner can submit: a SAS Partner Center account, business-verified and enrolled in the Microsoft 365 and Copilot program.
6. **Icon and brand colour** are placeholders taken from the other examples; certification has its own icon rules.
