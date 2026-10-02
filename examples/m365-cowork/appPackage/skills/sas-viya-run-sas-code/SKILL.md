---
name: sas-viya-run-sas-code
description: |
  Writes and runs SAS code in SAS Viya through the SAS Viya connector and
  reports what the log and the output say. Use when the user asks to "run this
  SAS program", "run a PROC", "fit a model in SAS", "create a table with a
  DATA step", "why did my SAS code fail", or wants a statistical procedure
  that a SQL query cannot do.
license: Apache-2.0
metadata:
  author: SAS Institute Inc.
  version: "1.0"
---

# Run SAS code in SAS Viya

Run SAS programs with the `execute_sas_code` tool of the SAS Viya connector.
It changes things in SAS Viya, so Cowork asks the user to confirm each run.
For a plain question about data that a `SELECT` can answer, use `query_data`
instead: it is read-only and needs no confirmation.

## Workflow

1. **Know the data first.** Unless the user supplied the code, look up the
   table with `list_castables` and `get_castable_columns` so the program uses
   real names.
2. **Write the program.** To reach a CAS table, assign the caslib as a
   library at the top, for example `libname casuser cas;` or
   `libname mylib cas caslib="Public";`. Do not use `caslib _all_ assign;`.
   End every step with `run;` or `quit;`.
3. **Show it, then run it.** Show the program to the user in a code block and
   call `execute_sas_code` with it.
4. **Read the result.** The tool returns `state`, `log`, `listing` and, when
   the program printed output, `html_results_url`.
   - `state` is `completed`: report the results from `listing`.
   - `state` is `warning`: report the results and quote the WARNING lines.
   - `state` is `error`: quote the first ERROR line and the statement it
     points at, fix the program, and run it again once.
5. **Report.** Give the finding, the key numbers in a table, and the link in
   `html_results_url` so the user can open the full output with graphs.

## Things to know

- The compute session is reused between runs: WORK tables, macro variables
  and librefs from an earlier run are still there. Pass `fresh_session` as
  true when the program must start clean.
- Never put passwords or other secrets in a program.
- Do not drop or overwrite a table the user did not ask you to change. Write
  new tables to WORK or to the user's `casuser` caslib.

## Output format

| Part | Content |
| --- | --- |
| Finding | One or two sentences answering the request |
| Results | The key numbers from the listing, as a table |
| Program | The SAS code that ran, in a code block |
| Full output | The `html_results_url` link, when present |
| Problems | Any WARNING or ERROR lines from the log, quoted |
