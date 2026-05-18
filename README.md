# OracleOps

> A Telegram-based Oracle DBA that lives on your laptop or VPS. Reads AWR, diagnoses slow queries, finds lock contention, recommends fixes, and asks before it changes anything in production. **Install with one command: `hermes plugins install shopsmartai/oracleops`**.

[![License: MIT](https://img.shields.io/badge/License-MIT-cyan.svg)](LICENSE)
[![Hermes Agent](https://img.shields.io/badge/Built%20for-Hermes%20Agent-purple.svg)](https://hermes-agent.nousresearch.com/)
[![agentskills.io](https://img.shields.io/badge/Format-agentskills.io-green.svg)](https://agentskills.io)
[![Oracle 23ai/26ai](https://img.shields.io/badge/Oracle-23ai%20%2F%2026ai-red.svg)](https://www.oracle.com/database/)

Submission for the [Hermes Agent Challenge](https://dev.to/devteam/join-the-hermes-agent-challenge-1000-in-prizes-13cd) by Nous Research.

## What this is

A Hermes Agent plugin and skill pack that turns your Hermes installation into a senior Oracle DBA you can DM. You message your bot ("orders app is slow"), the agent runs the right diagnostics against your Autonomous Database, identifies the bottleneck, and either explains it or proposes a fix. Mitigations are gated behind explicit confirmation. Nothing destructive runs without your "yes."

Built around three real-world Oracle pain points:

1. **3 AM slow-query diagnosis.** The on-call DBA opens SQL Developer, runs `dbms_xplan.display_cursor`, eyeballs the wait event chain, and types the same five queries against `v$session` and `v$lock` they typed last week. Automate that, and on-call gets actual sleep.
2. **AWR is gold but nobody reads it.** A Telegram bot that proactively summarizes the latest AWR snapshot every morning surfaces the trend before it becomes an incident.
3. **Junior DBAs need a senior in the room.** A skill pack that *explains* (not just *executes*) gives junior team members context. "This wait event means X because Y" is more valuable than "I rebuilt the index."

## Quick start

### 1. Install the plugin

```bash
# Requires Hermes Agent installed and configured (see https://hermes-agent.nousresearch.com/)
hermes plugins install shopsmartai/oracleops
hermes plugins enable oracleops
```

### 2. Install the database client into Hermes' venv

```bash
~/.hermes/hermes-agent/venv/bin/python -m pip install 'oracledb>=2.0'
```

### 3. Configure your Oracle credentials

```bash
hermes config set ORACLE_USER admin
hermes config set ORACLE_PASSWORD 'your-admin-password'
hermes config set ORACLE_DSN your_db_high                # from tnsnames.ora
hermes config set ORACLE_WALLET_DIR "$HOME/oracle-wallets/yourdb"
hermes config set ORACLE_WALLET_PASSWORD 'your-wallet-password'
```

### 4. Copy the skills into your Hermes skills directory

```bash
git clone https://github.com/shopsmartai/oracleops.git /tmp/oracleops-skills
cp -r /tmp/oracleops-skills/skills/* ~/.hermes/skills/
```

### 5. Start the agent

```bash
hermes gateway   # starts the messaging gateway (Telegram, Slack, etc.)
```

Then DM your bot:

```
You:  Show me the top SQL queries in the last hour
Bot:  [running top-sql-this-hour]

      TOP SQL BY ELAPSED TIME — window: 14:00 → 15:00

      # | SQL_ID         | ms/exec  | execs | DB s | hint
      1 | 4a2g8htg9k7bn  | 12450.2  | 12    | 149  | I/O bound — try recommend-index
      2 | 9z3xb1mqf2t7w  | 0.3      | 4.5M  | 1350 | app loop — look at caller
      ...
```

## How OracleOps uses Hermes Agent's unique features

| Hermes feature | OracleOps usage |
|---|---|
| **Skill auto-generation** | After diagnosing 3 similar slow queries from the same app, the Curator auto-creates a `diagnose-{app}-pattern` skill for next time |
| **Persistent memory** | Remembers your DB's hot tables, your team's preferred remediations, last week's incidents |
| **Messaging gateways** | The DBA lives in your Telegram (also Discord, Slack, WhatsApp — same skills, any gateway) |
| **NL cron** | `"Every weekday at 8am, send me the top 10 slow queries from the last 24 hours"` |
| **Subagent parallelization** | The orchestrator fans out to 3 parallel diagnostics for generic "DB is slow" complaints |
| **Confirmation gates** | Every mitigation (kill session, drop index, rebuild) requires explicit `yes` in chat |

## The five tools

Registered into the `oracle_db` toolset by `__init__.py` at plugin load:

| Tool | Purpose | Safety |
|---|---|---|
| `oracle_run_select` | Read-only SQL execution | Deny-list rejects INSERT/UPDATE/DELETE/DDL keywords |
| `oracle_describe_table` | Columns, indexes, stats for a table | Read-only |
| `oracle_explain_plan` | Optimizer's predicted plan for a SQL statement | Read-only |
| `oracle_display_cursor_plan` | Real runtime plan for a SQL_ID from cursor cache | Read-only |
| `oracle_write_with_confirmation` | DDL, DML, ALTER SYSTEM execution | Requires `user_confirmation_token` populated by the calling skill from the user's literal "yes". Every call audited to `~/.hermes/oracleops/writes.jsonl` |

## The 7 skills

Markdown files (agentskills.io format, portable to Claude Code / Cursor / Letta) describing HOW to combine the five tools above to diagnose Oracle issues:

🟢 **Diagnostic** (read-only, auto-run)

| Skill | What it does |
|---|---|
| `diagnose-slow-query` | Plan analysis + 7 bottleneck pattern matchers + remediation ladder cheapest-first |
| `awr-summary-now` | AWR diff between last two snapshots + 8 threshold callouts + escalation routing |
| `find-lock-contention` | Blocker chain walk + lock type taxonomy + root-cause classification |
| `top-sql-this-hour` | Top SQL via ASH (sub-hour) or AWR (longer); foundation for the orchestrator |

🟡 **Recommendation** (requires confirmation)

| Skill | What it proposes |
|---|---|
| `recommend-index` | CREATE INDEX DDL with right column order from plan analysis; dry-run impact via virtual index |
| `kill-session-suggestion` | ALTER SYSTEM KILL SESSION with rollback time estimate; literal "yes" required in next turn |

🔵 **Orchestrator**

| Skill | Purpose |
|---|---|
| `orchestrator` | Front door. Classifies user intent into 9 buckets, dispatches single skill or fan-out, synthesizes results into one structured response |

## Why Oracle 23ai/26ai

Latest 23ai (rebranded 26ai in 2026) ships with **AI Vector Search** in the database. OracleOps roadmap: index every past incident's text into the vector store; when a new complaint comes in ("orders app is slow again"), do a similarity search before running fresh diagnostics. *"This looks like the contention you had on `orders_status_idx` two weeks ago. The fix then was..."*

The plugin runs against 19c and 21c too, with the AI similarity-search loop being 23ai-only.

## Running on OCI Always Free Tier

| OCI resource | Free allocation | Used for |
|---|---|---|
| Ampere ARM VM | 4 OCPUs / 24 GB RAM | Hosts the Hermes Agent daemon |
| Autonomous Database (ATP) | 2 instances × 20 GB | Your test database + demo DB |
| Object Storage | 20 GB | AWR exports, postmortem artifacts |
| Outbound bandwidth | 10 TB/month | Telegram polling + LLM API calls |

Full setup guide in [`docs/setup-oci.md`](docs/setup-oci.md).

## Demo data

The repo ships with `examples/seed-demo.sql` — an e-commerce schema (100k customers, 500k orders, 1M order_items) with **seven intentional performance traps** so every skill has something realistic to find:

1. `orders.customer_id` has no index → `recommend-index` target
2. `order_items.product_id` has no index
3. `orders` has `INITRANS=1` → ITL contention under concurrent inserts
4. `orders.status` histogram missing → optimizer mis-estimates cardinality
5. `vw_customer_revenue` uses a scalar subquery → `rewrite-bad-query` target
6. 5% of orders moved to RETURNED status without re-gathering stats
7. One atypical PENDING order for bind-variable peeking demos

Load with:

```bash
# Option A: OCI Database Actions → SQL → paste examples/seed-demo.sql → Run Script
# Option B: SQL*Plus: sqlplus admin/<pw>@yourdb_high @examples/seed-demo.sql
# Option C: Python loader (handles PL/SQL blocks):
python scripts/load-seed.py
```

## Architecture

```
                User in Telegram
                       │
                       ▼
            ┌──────────────────────┐
            │ Hermes Agent runtime │
            │                      │
            │  orchestrator skill  │  ← routes "DB is slow" to the right tool
            │           │          │
            │           ▼          │
            │  diagnose-slow-query │  ← skill knows HOW
            │           │          │
            │           ▼          │
            │  oracle_run_select   │  ← tool DOES
            │  oracle_explain_plan │
            │  oracle_describe_table
            │           │          │
            └───────────┼──────────┘
                        │
                        ▼
                  Oracle ADB (23ai)
                  via wallet+TLS
```

## Files

| Path | What |
|---|---|
| `plugin.yaml` | Plugin metadata for Hermes' plugin loader |
| `__init__.py` | `register(ctx)` — wires tools into the `oracle_db` toolset |
| `tools.py` | 5 tool schemas + handlers, all returning JSON strings |
| `connection.py` | `oracledb` thin-mode pool, ADB-wallet-aware |
| `skills/` | 7 agentskills.io SKILL.md files |
| `examples/seed-demo.sql` | Reproducible demo schema |
| `scripts/load-seed.py` | Python loader that handles PL/SQL blocks |
| `docs/setup-oci.md` | End-to-end OCI Always Free deployment |
| `requirements.txt` | Just `oracledb>=2.0` |

## License

[MIT](LICENSE). Use it, fork it, ship a better one. agentskills.io-formatted skills are portable to Claude Code, Cursor, OpenHands, and Letta — so even if you don't use Hermes Agent, the skills in `skills/` work in your favorite agent runtime.

## Credits

- [Hermes Agent](https://hermes-agent.nousresearch.com/) by Nous Research
- [agentskills.io](https://agentskills.io) open standard
- The Oracle DBA community whose late-night Slack threads inspired half of these skills
