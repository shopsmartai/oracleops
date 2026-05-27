---
title: I Built an Oracle DBA That Lives in Telegram. It Cut a 500K-Row Scan to 5 — After Asking Permission.
published: false
description: An AI agent that diagnoses slow Oracle queries, proposes index fixes, and asks before touching production. Hermes Agent plugin with 7 portable agentskills.io skills. Built against Oracle Autonomous Database 23ai/26ai on OCI Always Free.
tags: hermeschallenge, hermesagent, agents, oracle
canonical_url: https://github.com/shopsmartai/oracleops
---

*This is a submission for the [Hermes Agent Challenge](https://dev.to/challenges/hermes-agent-2026-05-15)*

## What I Built

**OracleOps** is a Hermes Agent plugin and skill pack that turns *any messaging app your team already uses* into a senior Oracle DBA you can DM at 3 AM.

> 📡 **One install. Any chat platform.**
> The demo runs in **Telegram**, but the same OracleOps skills work identically in **Slack**, **Discord**, **Microsoft Teams**, **WhatsApp**, **Signal**, and **Email** — anywhere Hermes Agent's messaging gateway reaches. Zero code changes between platforms. Bank ops team on Teams, startup ops team on Discord, on-call engineer on WhatsApp? Same bot, same skills, same audit log.

You message your bot, "*Why is this query slow: `SELECT * FROM orders WHERE customer_id = 42`?*"

The agent:

1. Pulls the real runtime plan from Oracle's cursor cache
2. Reads existing indexes on the table
3. Identifies the dominant bottleneck (in this case, no index on `customer_id` forcing a 500,000-row full scan to return 5 rows)
4. Generates the exact CREATE INDEX DDL needed to fix it
5. **Asks for your explicit "yes" before executing it**
6. Verifies the new plan and reports the cost reduction
7. Logs everything to an audit trail

It solves three pain points that Oracle DBAs deal with every week:

- **3 AM slow-query diagnosis** that today requires opening SQL Developer, running `dbms_xplan.display_cursor`, eyeballing wait events, and typing the same five `v$session` queries you typed last week
- **AWR is gold but nobody reads it** until production is on fire
- **Production safety**: the "AI agent deleted our database" anxiety is real. OracleOps proposes, then waits for explicit consent

Build cost: $0. Runs on Oracle Cloud Infrastructure's permanent free tier. Repo includes a reproducible 7-trap demo schema so anyone can clone, install, and see all skills working in 60 seconds.

## Demo

60-second screencap of the full diagnose → recommend → confirm → verify cycle in Telegram:

{% embed https://youtu.be/bePw9NCdNPs %}

What the demo captures:

| Step | What happens |
|---|---|
| 1 | I ask, "*What's slow in my database right now?*" |
| 2 | Agent calls `top-sql-this-hour` skill → returns ranked SQL by elapsed time |
| 3 | I ask, "*Why is this query slow: `SELECT * FROM orders WHERE customer_id = 42`?*" |
| 4 | Agent runs `diagnose-slow-query` → calls `oracle_explain_plan` + `oracle_describe_table` → identifies missing index |
| 5 | Agent proposes `CREATE INDEX IX_ORDERS_CUSTOMER_ID ON orders(customer_id)` |
| 6 | I reply, "*Yes*" |
| 7 | Agent fires `oracle_write_with_confirmation`, gets a fresh execution plan, verifies the fix worked |

The headline numbers from the live ADB run:

| | Before | After |
|---|---|---|
| Plan operation | `TABLE ACCESS STORAGE FULL` | `INDEX RANGE SCAN` |
| Cost | 709 | 8 |
| Predicate | `filter()` (post-scan) | `access()` (index lookup) |
| Rows touched | 500,000 | ~5 |

**98.9% cost reduction. From a Telegram message.**

Every write the agent makes is appended to an audit log. Here is the entry the demo produced:

```json
{
  "ts": "2026-05-18T23:23:15Z",
  "user": "admin",
  "dsn": "oracleopsdemo_high",
  "sql": "CREATE INDEX IX_ORDERS_CUSTOMER_ID ON orders(customer_id)",
  "user_confirmation_token": "Yes",
  "reason": "Create missing index on orders.customer_id to fix full table scan on 500K-row table — estimated 99% reduction in query cost.",
  "rows_affected": 0
}
```

The `user_confirmation_token` field is the user's literal reply that was required before the SQL ran. The `reason` captures the agent's rationale. Together they form a compliance-friendly trail of every destructive thing the agent has ever done on your database.

## Code

**Repo:** https://github.com/shopsmartai/oracleops

Install one-line:

```bash
hermes plugins install shopsmartai/oracleops
hermes plugins enable oracleops
```

Then drop the skills into your Hermes skills directory:

```bash
git clone https://github.com/shopsmartai/oracleops /tmp/oracleops-skills
cp -r /tmp/oracleops-skills/skills/* ~/.hermes/skills/
```

Configure Oracle credentials and restart the gateway:

```bash
hermes config set ORACLE_USER admin
hermes config set ORACLE_PASSWORD 'your-password'
hermes config set ORACLE_DSN your_db_high
hermes config set ORACLE_WALLET_DIR "$HOME/oracle-wallets/yourdb"
hermes config set ORACLE_WALLET_PASSWORD 'your-wallet-password'
hermes gateway restart
```

MIT licensed. Repo includes:

- 7 agentskills.io-formatted skills (portable to Claude Code, Cursor, OpenHands, and Letta — not just Hermes)
- 5 Oracle tools registered into the `oracle_db` toolset
- A 100k-customer / 500k-orders / 1M-order_items demo schema with 7 intentional performance traps
- End-to-end OCI Always Free Tier deployment guide

### My Tech Stack

| Layer | What I used |
|---|---|
| Agent runtime | Hermes Agent v0.14 from Nous Research |
| Model | Anthropic Claude Sonnet 4.6 (switched from Gemini after hitting free-tier 429s) |
| Messaging | Telegram via Hermes' bundled gateway (one of 7 supported — Slack, Discord, Teams, WhatsApp, Signal, Email all work identically) |
| Plugin language | Python 3.11 with `oracledb 4.0` in thin mode (no Instant Client install) |
| Database | Oracle Autonomous Database 23ai / 26ai on Oracle Cloud Always Free Tier |
| Auth to ADB | mTLS via wallet (`cwallet.sso`, `tnsnames.ora`) |
| Skill format | [agentskills.io](https://agentskills.io) Markdown with YAML frontmatter |
| Audit log | JSON Lines at `~/.hermes/oracleops/writes.jsonl` |
| Hosting | OCI ARM Ampere A1 free tier (4 OCPU / 24 GB RAM) - $0/month total |

## How I Used Hermes Agent

Five Hermes capabilities did the heavy lifting:

### 1. The plugin contract (`register(ctx)` + JSON-schema tools)

OracleOps registers 5 tools into the `oracle_db` toolset using Hermes' `ctx.register_tool` API. Each tool is a Python function that accepts `(args: dict, **kwargs)`, returns a JSON string, and exposes a JSON Schema for parameters. The result is that the language model can decide *when* to call my tools the same way it decides when to call Hermes' built-in tools - no special prompting needed. I followed Spotify's bundled plugin as the template; the contract is genuinely clean.

### 2. agentskills.io format for skills

Skills live in 7 separate Markdown files with YAML frontmatter. Hermes loads them at runtime; the agent picks the right skill based on the `description` and `when_to_use` fields. **The skills are portable** - they work in Claude Code, Cursor, OpenHands, and Letta without modification. So even if a user doesn't run Hermes, they can drop these skills into any agent that speaks the same format.

This is something I didn't fully appreciate until I'd written the third skill: I'm not building Hermes-only software. I'm building a portable Oracle DBA playbook that happens to run on Hermes today.

### 3. Confirmation-gated writes

This was the killer feature for the safety story. The `oracle_write_with_confirmation` tool requires `user_confirmation_token` as a mandatory parameter. The calling skill's job is to collect the user's literal "yes" in the immediately prior chat turn and pass it through. The tool refuses to execute unless the token is in a small allowlist of affirmatives (`yes`, `y`, `confirm`, `proceed`, etc.).

Every executed write hits a JSON Lines audit log outside the plugin directory. Even if the plugin gets uninstalled or rewritten, the historical trail of "what did the agent do with my consent" survives. This is the answer to that Hacker News thread titled "An AI agent deleted our production database."

### 4. Messaging gateways (the multi-platform story)

I wired **Telegram** for the demo with `hermes setup gateway` — about 2 minutes of clicks in BotFather plus one `hermes config set` for the allowlist. **The same OracleOps install also serves**:

| Platform | Status | Setup |
|---|---|---|
| Telegram | ✅ demo | BotFather token |
| Slack | ✅ supported | Socket Mode bot token |
| Discord | ✅ supported | Application + Message Content Intent |
| Microsoft Teams | ✅ supported | Bot Framework + Adaptive Cards |
| WhatsApp | ✅ supported | Meta Cloud API or Twilio |
| Signal | ✅ supported | `signal-cli` linked to your number |
| Email | ✅ supported | IMAP/SMTP |

**Crucially, the skills don't change.** All 7 OracleOps skills are platform-agnostic Markdown — the Hermes gateway plugin handles the protocol differences (token-by-token streaming on Telegram/Slack, message-batched on Email, etc.). So when the bank ops team prefers Microsoft Teams and the startup ops team prefers Discord, the *same* OracleOps deployment serves both audiences with zero code rework.

The allowlist (`TELEGRAM_ALLOWED_USERS` / `SLACK_ALLOWED_USERS` / etc.) restricts who can DM the bot. This matters: an Oracle DBA agent with no allowlist is a remote-code-execution surface to anyone who finds the bot username.

### 5. The orchestrator pattern with subagents on call

The `orchestrator` skill is the front door for natural-language complaints. It classifies user intent into 9 buckets and either calls one specific skill or fans out (via Hermes' `delegate_task`) to three diagnostics in parallel for generic "the database is slow" questions. The subagents run concurrent SQL against the ADB so the response time stays under 10 seconds even when three diagnostics are needed.

I also wired a `kill-session-suggestion` skill for the worst-case 3 AM scenario: a blocking session that won't release. The skill walks the blocker chain, names the lock type, estimates rollback time, and proposes `ALTER SYSTEM KILL SESSION` with explicit `@<inst_id>` syntax for RAC clusters. Same confirmation gate, same audit log.

---

## Safety architecture (the part that matters most)

Before the engineering deep-dive, the design choice the whole project hinges on: **the agent never mutates state without permission, and "permission" gets progressively harder to give as the blast radius grows.**

Three tiers, smallest blast radius first:

### Tier 0 — Read-only path (auto-runs, no consent needed)

`oracle_run_select` is the only tool that the agent can dispatch without any confirmation flow. It runs a single regex deny-list against the SQL before the database ever sees it:

```python
_FORBIDDEN_IN_READ = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|CREATE|ALTER|"
    r"GRANT|REVOKE|COMMIT|ROLLBACK|SAVEPOINT)\b",
    re.IGNORECASE,
)
```

Anything matching is hard-rejected with an explanatory error. Even if the agent hallucinates an `UPDATE` snuck inside what looks like a `SELECT`, it cannot mutate state through this path. The skills layer is the primary contract that this is read-only; the regex is the defense-in-depth.

### Tier 1 — Standard writes (require a plain "yes")

Most writes (CREATE INDEX, INSERT, UPDATE, DELETE, GRANT) go through `oracle_write_with_confirmation`. The tool refuses to execute unless its `user_confirmation_token` parameter matches one of a small allowlist of affirmatives:

```python
{"yes", "y", "confirm", "proceed", "ok", "do it",
 "kill it", "go ahead", "yes, do it", "yes do it"}
```

The calling skill is responsible for collecting that token from the user's immediately prior chat turn. No prior approval can be reused across turns.

### Tier 2 — Destructive ops (require typed-name confirmation)

`DROP`, `TRUNCATE`, and `ALTER` are different. A plain "yes" to one of these is too easy — the user might have skimmed the proposal and reflexively approved. So for these statement types, the confirmation token must contain either:

- **The target object's name** parsed out of the SQL (e.g., for `DROP TABLE orders`, the user must type something containing `orders`), OR
- **The literal phrase `I understand`**

This is the typed-confirmation pattern that ops-tooling folks have used for years on `rm -rf` and `kubectl delete namespace`, brought into the agent layer. Plain "yes" gets rejected with a clear error pointing the user at the expected token shape.

### The audit log (every decision point recorded)

Every approved write AND every denied proposal lands in a single append-only JSON Lines file at `~/.hermes/oracleops/writes.jsonl`. The file survives plugin upgrades and uninstalls because it lives outside the plugin directory.

An approved entry:

```json
{
  "event": "approved",
  "ts": "2026-05-18T23:23:15Z",
  "user": "admin",
  "dsn": "oracleopsdemo_high",
  "sql": "CREATE INDEX IX_ORDERS_CUSTOMER_ID ON orders(customer_id)",
  "user_confirmation_token": "Yes",
  "reason": "Create missing index on orders.customer_id to fix full table scan",
  "rows_affected": 0
}
```

A denied entry:

```json
{
  "event": "denied",
  "ts": "2026-05-19T08:12:04Z",
  "user": "admin",
  "dsn": "oracleopsdemo_high",
  "proposed_sql": "DROP INDEX IX_ORDERS_OBSOLETE",
  "user_response": "no, hold off until I check who's still using it",
  "reason": "User wants to confirm no apps depend on the index before dropping"
}
```

Filter approvals with `jq 'select(.event == "approved")' writes.jsonl`. Filter denials with `select(.event == "denied")`. Together they form the complete decision history. The denials are particularly valuable for tuning — they show exactly where the agent's judgment diverged from what a human DBA chose, which is the signal you'd want to teach the agent with next time.

### How the tiers map to the user experience

| User says | Path |
|---|---|
| "What's slow right now?" | Tier 0, auto-runs `oracle_run_select` |
| "Show me the plan for that SQL" | Tier 0, auto-runs `oracle_explain_plan` |
| "Recommend an index" | Tier 0 to propose, Tier 1 to create on "yes" |
| "Yes" → CREATE INDEX runs | Tier 1, executes, logs `event: approved` |
| "Drop the customers table" | Tier 2, requires `customers` or `I understand` in token; rejected on plain "yes" |
| "No, hold off" → DROP not run | Tier 2 path aborts, agent calls `oracle_record_denial`, logs `event: denied` |

The whole point: you can have an agent that's genuinely useful for ops without it being able to nuke production by mistake. The friction grows with the stakes.

---

## The 8 engineering problems I hit

Most of these weren't in the docs. Sharing them so anyone building on Hermes can shortcut the painful parts.

### 1. The Hermes plugin contract isn't `pip install`

`hermes plugins install` takes a Git URL or `owner/repo`, not a local path. My first attempt at packaging the plugin as `plugins/oracle/` nested inside a project repo was wrong. The plugin manifest, registration code, and Python tool modules must live at the *root* of a Git repo - the repo IS the plugin. I learned this by reading `~/.hermes/hermes-agent/plugins/spotify/` source.

### 2. Oracle passwords break naive connection-string parsers

Oracle allows passwords containing `@`, `#`, `!`, `/`, and most punctuation. My initial parser did `split("@", 1)` on `user/password@dsn`, which broke on passwords containing `@`. Fix: prefer three separate env vars (`ORACLE_USER`, `ORACLE_PASSWORD`, `ORACLE_DSN`) and only fall back to combined parsing with `rsplit("@", 1)` for the simple case.

### 3. PL/SQL anonymous blocks shred under `split(";")`

My demo seed had a `BEGIN ... END; /` block for stats gathering. Naive Python `split(";")` produced 7 invalid fragments that all failed with ORA-00900. Fix: wrote a proper splitter that tracks PL/SQL block boundaries (`BEGIN`/`DECLARE` ... `/`) separately from plain SQL terminators. Bonus: it also respects semicolons inside string literals.

### 4. Hermes' bundled venv doesn't ship pip

The Hermes Python venv at `~/.hermes/hermes-agent/venv/` came pip-less - Hermes is managed with `uv`. To install `oracledb`, the path was `python -m ensurepip --upgrade && python -m pip install oracledb>=2.0`. Tucked this into the README for users.

### 5. Skill `required_environment_variables` must match the plugin's env vars exactly

Three of my SKILL.md files listed `ORACLE_CONNECTION_STRING` in `required_environment_variables`. The plugin uses the three-separate-vars form. Result: the agent refused to run those skills with "Please provide ORACLE_CONNECTION_STRING." Fix: align the skill manifests with the plugin's actual env var contract. (Caught it during the first real Telegram test, which is exactly when an integration test should catch it.)

### 6. Session memory caches old skill content across gateway restarts

After updating SKILL.md and restarting the gateway, the same Telegram session kept asking for the old env var. The fix is to start a fresh session (Hermes has `/new`). I didn't expect this; the skill file on disk is the source of truth, but in-session conversation state preserves the model's earlier reasoning.

### 7. Gemini 2.5 Pro's free tier has `limit: 0`

The setup wizard reported "paid ✓" for my Gemini key, but the actual quota was zero for `gemini-2.5-pro`. Symptoms: HTTP 429 `RESOURCE_EXHAUSTED` for every request. Fix paths: (a) switch to `gemini-2.5-flash` which IS free-tier accessible, or (b) attach billing to the Google Cloud project. I ended up switching to Anthropic Claude Sonnet 4.6 entirely - better instruction-following for structured Oracle SQL analysis and the free credits cover the demo budget.

### 8. Telegram bot tokens are RCE if not allowlisted

By default, a Hermes bot accepts messages from any Telegram user that finds it. With OracleOps active, that means anyone can ask the bot to run SQL against my database. Mitigation: `hermes config set TELEGRAM_ALLOWED_USERS <my_user_id>`. Anyone outside the allowlist gets ignored at the gateway layer before the agent ever sees their message.

## What I learned about Hermes Agent that wasn't in the docs

- **The plugin contract is `register(ctx)`, not class-based.** It's `ctx.register_tool(name, toolset, schema, handler, check_fn, emoji)` per tool. Simple, clean, fewer ceremony than I expected.
- **`check_fn` is for graceful degradation, not gating.** A tool with `check_fn` returning False stays listed in `hermes tools` but won't dispatch. Users see the tool exists; runtime errors clearly explain "set ORACLE_USER..." instead of the tool silently disappearing.
- **Hermes runs everything in its own venv** at `~/.hermes/hermes-agent/venv/`. Plugin dependencies install into this venv with the path-explicit `python -m pip install` pattern.
- **`hermes plugins list` is global; `hermes tools list` is per-platform.** A toolset can be enabled globally but disabled for Telegram or CLI specifically. Worth knowing during testing.
- **The agentskills.io standard is bigger than just Hermes.** Skills written for Hermes work in Claude Code, Cursor, OpenHands, and Letta. The skill pack in this repo is a usable Oracle DBA playbook independent of which agent runtime you use.

## What I would build next

- **Cron-driven morning briefings.** Wire `awr-summary-now` + `top-sql-this-hour` + `find-lock-contention` into a daily 8 AM Telegram digest using Hermes' NL cron. Hermes has this built in; I ran out of time before adding the cron syntax to the README.
- **Oracle 23ai AI Vector Search for incident similarity.** When a new complaint comes in ("orders app is slow again"), do a vector similarity search against past incident postmortems before running fresh diagnostics. *"This looks like the contention you had on `orders_status_idx` two weeks ago - the fix then was..."* This is what 23ai's in-database vector index makes possible.
- **Auto-generated skills via the Curator.** After three similar slow-query diagnoses on the same application, have Hermes promote the pattern into a `diagnose-<app>-pattern` skill so future diagnoses skip the discovery phase. I designed the MEMORY captures with this in mind; the Curator hooks are next.
- **Multi-instance RAC.** All my `v$` queries should use `gv$` and walk `inst_id` for cluster databases. Single-instance ADB doesn't need it, but on-prem RAC does. Pure plumbing, no new ideas.

## Thanks

- **Nous Research** for shipping Hermes Agent v0.14.0 the same week as the challenge — and for making the plugin contract clean enough that I could ship a working tool integration in a week. The Spotify bundled plugin was the perfect template.
- **Oracle Cloud's Always Free Tier** for the 2 free Autonomous Database instances. Building on real Oracle 23ai/26ai at $0/month is the difference between a hackathon toy and an actually useful piece of software.
- **The agentskills.io open standard.** Knowing my skills also work in Claude Code, Cursor, OpenHands, and Letta means the value of this project survives whichever agent framework wins long-term.
- **The Oracle DBA community** whose late-night Slack threads about ITL contention, bind variable peeking, and adaptive cursor sharing inspired half the skill content. Most of the technique in `diagnose-slow-query`'s 7-pattern bottleneck table came from those conversations.

Try the live install with `hermes plugins install shopsmartai/oracleops` and tell me what breaks. PRs welcome. The code is MIT.
