---
name: kpi-selection
description: How to pick one KPI worth watching that nobody asked for and propose it with propose_kpi for owner approval. Use after exploration finds a recurring risk or driver.
license: Proprietary
---

# Choosing a KPI to propose

## When to use

Use this skill after exploration finds a recurring risk or a driver of money, and you want to suggest one number the owner should keep an eye on. Propose with propose_kpi.

## A good KPI

- Tied to money, or to a recurring risk you found today.
- One number, from one aggregate row.
- Actionable: the owner can do something when it moves.
- A stable definition that does not depend on judgment.
- A cadence that matches how fresh the data is.

## Writing the condition

The watchdog compiles your condition once into a one-row SELECT plus a trigger of type increase, threshold or change. So name the tables, columns, filter, window and threshold explicitly.

Example: "Count of South-region sales_orders in the trailing 7 days falls below 12".

Avoid vague wording such as "sales look bad". Check get_kpi_history for existing watches first.

## Measure-backed KPIs

When get_model lists a measure for the KPI, call propose_kpi with measure=<measure name> and a relative range such as the last 7 days (no by or grain), instead of writing a condition in words. The measure carries its own definition and freshness, so the watch never alerts on stale data. Add an alert only when the finding justifies a threshold.

## Cadence

Pick hourly, daily or weekly. Daily is the default for order-level data. Use weekly for slow measures such as monthly-cycle costs, and hourly only for data that really changes within the hour.

## Limits

- Propose at most 1 or 2 KPIs per run.
- Never propose anything listed under ALREADY WATCHED, PROPOSED OR DECLINED KPIs (do not propose again):.
- Always give a rationale that names the insight behind the KPI.

## Safety

A proposal is only a suggestion. Nothing runs until the owner approves it in Approvals. Do not describe a proposed KPI as active.
