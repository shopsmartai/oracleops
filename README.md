# OracleOps

> A Telegram-based Oracle DBA that lives on your VPS. Reads AWR, diagnoses slow queries, finds lock contention, recommends fixes, and asks before it changes anything in production.

[![License: MIT](https://img.shields.io/badge/License-MIT-cyan.svg)](LICENSE)
[![Hermes Agent](https://img.shields.io/badge/Built%20for-Hermes%20Agent-purple.svg)](https://hermes-agent.nousresearch.com/)
[![agentskills.io](https://img.shields.io/badge/Format-agentskills.io-green.svg)](https://agentskills.io)
[![Oracle 23ai](https://img.shields.io/badge/Oracle-23ai-red.svg)](https://www.oracle.com/database/23ai/)

Submission for the [Hermes Agent Challenge](https://dev.to/devteam/join-the-hermes-agent-challenge-1000-in-prizes-13cd) by Nous Research.

## What this is

A skills pack and Hermes plugin that turns your Hermes Agent into a senior Oracle DBA. You DM your bot ("orders app is slow"), the agent runs the right diagnostics against your Autonomous Database, identifies the bottleneck, and either explains it or proposes a fix. Mitigations are gated behind explicit confirmation. Nothing destructive runs without your "yes."

Built around three real-world Oracle pain points:

1. **3 AM slow-query diagnosis.** The on-call DBA opens SQL Developer, runs `dbms_xplan.display_cursor`, eyeballs the wait event chain, and types the same five queries against `v$session` and `v$lock` they typed last week. Automate that, and on-call gets actual sleep.
2. **AWR is gold but nobody reads it.** Performance Hub is great if you log into OCI. Most DBAs don't, until production is on fire. A Telegram bot that proactively summarizes the latest AWR snapshot every morning surfaces the trend before it becomes an incident.
3. **Junior DBAs need a senior in the room.** A skill pack that *explains* (not just *executes*) gives junior team members context. "This wait event means X because Y" is more valuable than "I rebuilt the index."

## How it uses Hermes Agent's unique features

| Hermes feature | OracleOps usage |
|---|---|
| **Skill auto-generation** | After diagnosing 3 similar slow queries from the same app, agent auto-creates a `diagnose-{app}-pattern` skill for next time |
| **Persistent memory** | Remembers your DB's hot tables, your team's preferred remediations, last week's incidents |
| **Telegram gateway** | The DBA is in your Telegram. Type a complaint, get a diagnosis. |
| **NL cron** | `"Every weekday at 8am, send me the top 10 slow queries from the last 24 hours"` |
| **Subagent parallelization** | Three parallel subagents (Performance / Storage / Security) audit different aspects of the same DB concurrently |
| **Confirmation gates** | Every mitigation (kill session, drop index, rebuild) requires explicit `yes` in chat |

## Skills in this pack

🟢 **Diagnostic** (read-only, auto-run)

| Skill | Purpose |
|---|---|
| `diagnose-slow-query` | User pastes a slow query → explain plan + bottleneck analysis + remediation suggestions |
| `awr-summary-now` | Latest AWR snapshot summarized in plain English with thresholds |
| `find-lock-contention` | Who is blocking whom, with current SQL and blocker chain |
| `time-model-analysis` | Where DB time is spent: CPU, SQL execute, hard parse, PL/SQL |
| `wait-events-explained` | Translates `db file sequential read`, `enq: TX - row lock contention`, etc., into plain English with likely root causes |
| `tablespace-health-check` | Tablespaces nearing full, AUTOEXTEND off, fragmentation |
| `top-sql-this-hour` | Ranked expensive SQL with execution counts |
| `describe-and-row-count` | Table schema + row count + indexes + last analyzed |
| `index-usage-audit` | Indexes never used (candidates for drop) |
| `schema-bloat` | Per-schema storage breakdown |

🟡 **Recommendation** (requires confirmation)

| Skill | Purpose |
|---|---|
| `recommend-index` | From a slow query's plan, suggests composite index. Generates DDL. **Confirm gate.** |
| `recommend-statistics-refresh` | Stale stats on a hot table → proposes `dbms_stats.gather_table_stats` |
| `rewrite-bad-query` | Scalar subquery in select list, anti-pattern joins → proposes refactor |
| `propose-partition-strategy` | Big unpartitioned table → range/hash strategy based on access pattern |
| `kill-session-suggestion` | Runaway blocker → proposes `alter system kill session`. **Confirm gate.** |

🔵 **Communication**

| Skill | Purpose |
|---|---|
| `draft-incident-postmortem` | Real incident → 5-Why + AWR snapshots + remediation list |
| `explain-to-developer` | Translates wait-event jargon into developer language |
| `awr-executive-summary` | Senior-management version (3 bullets, no jargon) |

Plus an **orchestrator** skill that routes user complaints to the right diagnostic.

## Why Oracle 23ai

23ai ships with **AI Vector Search** in the database. OracleOps uses it to:

- Index every past incident's text into the vector store
- When a new complaint comes in ("the orders app is slow again"), the agent does a similarity search against past incidents before running fresh diagnostics
- "This looks like the contention you had on `orders_status_idx` two weeks ago. The fix then was..."

The pack also runs on 19c and 21c, but the AI similarity-search loop is 23ai-only.

## Running on OCI Always Free Tier

OracleOps is built to run at $0/month on OCI's permanent free tier:

| OCI resource | Free allocation | Used for |
|---|---|---|
| Ampere ARM VM | 4 OCPUs / 24 GB RAM | Hosts the Hermes Agent daemon |
| Autonomous Database (ATP) | 2 instances, 20 GB each | Your dev DB + a separate "user state" DB for OracleOps |
| Object Storage | 20 GB | AWR exports, postmortem artifacts |
| Outbound bandwidth | 10 TB/month | More than enough for Telegram polling |

Setup guide in [`docs/setup-oci.md`](docs/setup-oci.md).

## Installation

```bash
# 1. Install Hermes Agent
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
hermes setup

# 2. Clone OracleOps skills
git clone https://github.com/YOUR_HANDLE/oracleops ~/.hermes/skills-staging
cp -r ~/.hermes/skills-staging/skills/* ~/.hermes/skills/

# 3. Install the Oracle plugin
cp -r ~/.hermes/skills-staging/plugins/oracle ~/.hermes/plugins/
pip install -r ~/.hermes/plugins/oracle/requirements.txt

# 4. Configure connection
hermes config set ORACLE_CONNECTION_STRING "user/pass@//host:1521/service"

# 5. Wire Telegram (BotFather token)
hermes gateway add telegram --token <YOUR_BOT_TOKEN>

# 6. Start
hermes serve
```

## Try it

```
You (Telegram):   the orders app is slow

Bot:              [running diagnose-slow-query]
                  [running awr-summary-now in parallel]

                  Top wait event last 15 min:
                    enq: TX - row lock contention (37% DB time)

                  Likely cause: blocking session detected.
                  SID 1247 (oracle@app01) is blocking 23 sessions
                  on table orders.line_items.

                  Their current SQL is an UPDATE without WHERE
                  clause on indexed columns.

                  Want me to kill session 1247? [yes / no / show-sql]
```

## License

[MIT](LICENSE). Use it, fork it, ship a better one. agentskills.io-formatted skills are portable to Claude Code, Cursor, OpenHands, and Letta. So even if you don't use Hermes Agent, the skills in `skills/` work in your favorite agent runtime.

## Credits

- [Hermes Agent](https://hermes-agent.nousresearch.com/) by Nous Research
- [agentskills.io](https://agentskills.io) open standard
- The Oracle DBA community whose late-night Slack threads inspired half of these skills
