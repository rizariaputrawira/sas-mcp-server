---
name: sas-viya-data-exploration
description: |
  Finds, profiles and queries data in SAS Viya through the SAS Viya connector.
  Use when the user asks "what tables do I have in SAS Viya", "what is in this
  CAS table", "profile this table", "show me rows from", "how many rows", or
  wants numbers, counts or summaries from a caslib or SAS library table.
license: Apache-2.0
metadata:
  author: SAS Institute Inc.
  version: "1.0"
---

# SAS Viya data exploration

Answer questions about data in SAS Viya with the SAS Viya connector's tools.
Every result reflects what the signed-in person is allowed to see. Never guess
table names, column names or numbers: call a tool.

## Workflow

1. **Locate the table.** If the user did not name a caslib, call
   `list_cas_servers`, then `list_caslibs` on the server (normally
   `cas-shared-default`), then `list_castables` on the likely caslib. Ask
   which table they mean only when several are plausible.
2. **Learn its structure.** Call `get_castable_info` for the row count and
   `get_castable_columns` for names, types, labels and formats. Use the exact
   column names from this step in every later call.
3. **Answer with a query.** Use `query_data` with a FedSQL `SELECT` for
   anything that filters, aggregates, joins or sorts. Reference a CAS table as
   `caslib.table`. Use `get_castable_data` only for a plain look at the first
   rows.
4. **Report.** Give the answer first, then the table of numbers it rests on,
   then the caslib and table it came from.

## Rules for queries

- `query_data` returns 100 rows unless `limit` says otherwise. Aggregate in
  SQL instead of fetching rows to add them up yourself.
- A table that is listed but not loaded has to be loaded before it can be
  queried. If the connector offers `promote_table_to_memory`, call it and
  say that you did; otherwise tell the user the table is not loaded.
- A table in a SAS library (not a caslib) is queried with `target` set to
  `compute`. CAS and compute tables cannot be joined in one query.
- If a query fails, read the error, correct the SQL and retry once. If it
  fails again, show the user the error text.

## Output format

| Part | Content |
| --- | --- |
| Answer | One or two sentences with the figure the user asked for |
| Evidence | A table of the returned rows, at most 20, with column labels |
| Source | `caslib.table`, the row count of the table, and the SQL that ran |
