---
name: orchestrator
description: Top-level routing skill for OracleOps. Takes any user complaint about Oracle Database performance, classifies the intent, dispatches to the right diagnostic skill(s) in parallel where useful, threads the results, and surfaces the next action. The agent's "front door."
when_to_use: |
  Default entry point for any DB-related user message. Specifically when
  the user has NOT named a specific skill, SQL_ID, or operation. Catch-all
  for phrases like: "the database is slow", "the orders app is hanging",
  "something is wrong", "morning health check", "any issues with the DB?"
requires_toolsets:
  - oracle_db
required_environment_variables:
  - ORACLE_USER
  - ORACLE_PASSWORD
  - ORACLE_DSN
metadata:
  hermes:
    config:
      auto_load: true
      auto_confirm: true
      cost_estimate: low
      priority: high
---

# orchestrator

## When to Use

This skill is the front door. Trigger it on any DB-related user message that does not already name a specific OracleOps skill. The orchestrator does three things:

1. **Classify** the user's complaint into one of a small number of intents.
2. **Dispatch** to the right diagnostic skill (or, where they're cheap, *several in parallel*).
3. **Synthesize** the results into one structured response with a clear next step.

The user should rarely need to know which OracleOps skill to invoke. They should be able to type "orders app is slow" and the agent should do the right thing.

Do not use this skill when:

- The user has already named a specific SQL_ID or operation ("explain plan for SQL_ID 4a2g8htg9k7bn"). Call the specific skill directly.
- A previous orchestrator turn has already classified the issue and a follow-up question is about that same issue. Route to whatever skill was previously identified.
- The user is asking about Oracle in general, not their specific DB ("what does enq: TX mean"). That's the `wait-events-explained` lookup.

## Procedure

### Step 1: Classify the intent

Match the user's message against these intent buckets. The first match wins:

| User says (or similar) | Intent | Primary skill |
|---|---|---|
| "kill session 1247" / "stop SID X" | `mitigation_kill` | `kill-session-suggestion` |
| "create the index" / "add index on customer_id" | `mitigation_index` | `recommend-index` (skip to step 8 — they've already agreed) |
| "tune SQL_ID X" / "this query is slow: <sql>" | `diagnose_specific_sql` | `diagnose-slow-query` |
| "app/report is hanging" / "queries blocked" / "user X locked out" | `lock_contention` | `find-lock-contention` |
| "morning briefing" / "scheduled health check" | `scheduled_health` | `awr-summary-now` |
| "what's slow" / "top SQL" / "what's eating the DB" | `generic_slowness` | `top-sql-this-hour` + `awr-summary-now` (in parallel) |
| "tablespace full" / "out of space" / "ORA-01654" | `space_issue` | `tablespace-health-check` |
| "explain wait event X" / "what does Y mean" | `lookup` | `wait-events-explained` |
| "the app is slow" / "the database is slow" / generic | `unknown_slowness` | run `awr-summary-now` first to classify |
| nothing else matches | `clarify` | ask the user a focused question |

### Step 2: Pull context from MEMORY

Before any tool call, inspect the agent's MEMORY for:

- **Recent SQL_IDs** the user has been discussing in this session (from prior `top-sql-this-hour` or `diagnose-slow-query` runs)
- **App-name → SQL_ID mappings** the user has set up (e.g., "the orders report" → SQL_ID 4a2g8htg9k7bn)
- **Site-specific thresholds** (e.g., "this DB always runs at 70% CPU and that's normal")
- **Maintenance windows** (e.g., "ETL runs nightly 2-4 AM and is expected to be slow")

This context is often what differentiates a useful diagnosis from a generic one.

### Step 3: For `unknown_slowness`, fan out

Generic "the database is slow" is the highest-volume intent. The right move is to fan out across two cheap diagnostics in parallel, then route based on what they find. Use Hermes' `delegate_task` to spawn parallel subagents:

- Subagent A: `awr-summary-now` for the last hour
- Subagent B: `top-sql-this-hour` for the last hour
- Subagent C: a quick `find-lock-contention` check (just the count of currently-blocked sessions)

Wait for all three. Then route:

```
IF (sessions_blocked > 0 AND blocker_held > 5 min)  → focus on lock_contention
ELSE IF (top_sql[0] DB_time_share > 40%)            → focus on diagnose_specific_sql for that SQL_ID
ELSE IF (awr.dominant_wait_class == 'User I/O')     → focus on I/O — likely recommend-index candidate
ELSE IF (awr.host_cpu > 90%)                        → focus on CPU — runaway SQL or workload spike
ELSE                                                 → report "no specific issue stands out"
```

### Step 4: Compose the response

Synthesize across all sub-skill outputs into one structured message. The user should never see "I ran four skills and here are four reports" — they should see one diagnosis.

```
WHAT I FOUND

<one-sentence headline diagnosis, plain English>

EVIDENCE
- <bullet from awr-summary-now: top wait event>
- <bullet from top-sql-this-hour: rank-1 SQL_ID and its share of DB time>
- <bullet from find-lock-contention: blocking count, if non-zero>
- <bullet from any other run subskill>

WHAT YOU CAN DO NOW
1. <highest-impact action, with the exact skill to invoke or DDL to run>
2. <second action>
3. <third action — usually "monitor and re-check in N min">

WANT ME TO ...
- "diagnose <top SQL_ID>"  → drills into the dominant SQL
- "show lock chain"        → if blocking was non-zero
- "summarize again in 10 min" → schedules a re-check via cron
- "explain <wait event>"   → if a wait event was unfamiliar
```

For each action button, the agent should be ready to handle the user's literal response in the next turn. The follow-up routes through this skill again or directly to the relevant subskill.

### Step 5: For `mitigation_kill` and `mitigation_index`

These are write-side. Route to the relevant skill but make sure:

- The user's intent is recent (they said "kill session 1247" *right now*, not five minutes ago after the agent suggested something else)
- The proposed DDL/DML is shown for confirmation before execution
- The confirmation gate in `kill-session-suggestion` / `recommend-index` is honored

The orchestrator's job here is mostly to *not* second-guess the user. If they typed "kill session 1247", route to `kill-session-suggestion` and let that skill's confirmation gate handle the proposal. Do not add extra confirmation layers — that's redundant and irritating.

### Step 6: For `clarify`

If you can't classify the intent confidently, ask one focused question. Not five. Not a checklist. One.

Examples of good clarifying questions:

- "Which app is slow — the order entry UI or the warehouse reports?"
- "Are users locked out, or is everything just slower than usual?"
- "Do you have a specific SQL_ID, or would you like me to find the top offenders?"

Bad clarifying questions (avoid):
- "Can you provide more details?" (too open)
- "What database is this?" (we already know — only one is configured)
- "When did it start, what changed, who is affected, what error..." (interview-style; user hates this)

### Step 7: Cache results for follow-up turns

The user often asks a follow-up like "OK, fix the top one" or "tell me more about that wait event". Cache the orchestrator's last response payload in MEMORY:

```
ORCHESTRATOR_LAST_RESULT: {
  timestamp: <iso>,
  intent: <intent_bucket>,
  headline: <one-sentence>,
  top_sql_ids: [<id>, <id>, ...],
  dominant_wait_event: <event>,
  blockers: [<sid>:<serial>, ...]
}
```

When the next user message is short ("yes", "fix it", "the top one", "tell me more"), resolve the referent from this cache. If the cache is empty or stale (> 10 minutes), re-run the orchestrator before deciding.

## Pitfalls

- **Don't fan out unnecessarily.** Parallel subagents have a real cost — three concurrent Oracle queries on Always Free 1 OCPU instances can degrade the very performance you're trying to measure. Only fan out for `unknown_slowness`. For all other intents, route to the single right skill.
- **Recall vs precision tradeoff.** A user who says "the orders app is slow" *probably* means one specific report SQL. But it could be lock contention on the orders table, an I/O issue, or anything else. The fan-out for `unknown_slowness` keeps recall high. Don't try to guess — measure.
- **Don't repeat work the user already did.** If MEMORY shows the user just ran `diagnose-slow-query` on SQL_ID X two minutes ago, don't re-run it. Show the prior result and ask if they want a fresh run.
- **Don't propose write actions in the orchestrator response.** Write actions belong to the dedicated mitigation skills with their confirmation gates. The orchestrator should *suggest* the path forward but not propose the DDL itself.
- **Beware of "the database is fine" misdiagnosis.** A clean `awr-summary-now` and a manageable `top-sql-this-hour` does NOT mean the user's complaint is invalid. Maybe the issue is intermittent, scheduled, or on an instance you're not querying (if RAC). When in doubt, say "I don't see anything in the last hour. Can you tell me when it last happened?" rather than "everything looks fine."
- **Scheduled invocations.** When called from cron (e.g., morning brief), this skill should respond with a structured digest, not a conversational "What do you want me to do?" message. Detect the trigger source (`HERMES_TRIGGER == 'cron'`) and adapt the output.
- **Long subagent chains.** Hermes' default delegation cap is 3 concurrent subagents. Don't try to spawn 5 — the framework will batch them and the response time degrades. Three is plenty for the fan-out pattern in step 3.

## Verification

There's no DB state change to verify (the orchestrator itself is read-only). Instead, verify the *response* quality:

1. **Did the agent route to the right primary skill?** Re-read the user's message. If they said "kill" and the agent did anything other than route to `kill-session-suggestion`, the routing logic missed.
2. **Was the synthesis actually synthesized, or just four reports stapled together?** Good output has one headline and one set of evidence bullets. Bad output has section headers like "AWR Result" / "Top SQL Result" / "Lock Result" copied from the subskills.
3. **Was the next-action recommendation specific?** "Diagnose SQL_ID 4a2g8htg9k7bn" is good. "Look at top SQL" is too vague.

After each orchestrator turn, capture into MEMORY:

```
ORCHESTRATOR_INVOCATION_<n>: {
  user_message: "<verbatim>",
  classified_intent: <bucket>,
  skills_called: [<list>],
  total_time_ms: <n>,
  user_followup_message: "<next turn, if any>",
  followup_was_useful: <bool>
}
```

This data is what the Curator promotes into auto-skill-generation. After ~10 successful orchestrator turns for the same `(user_pattern, classified_intent)` pair, the Curator can author a more specialized skill that skips the classification step.
