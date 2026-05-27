---
name: kill-session-suggestion
description: When find-lock-contention identifies a root blocker that should be terminated, propose the exact ALTER SYSTEM KILL SESSION statement with sid/serial/instance, surface the full context (user, machine, program, current SQL, time held), warn about rollback time, and only execute on an explicit "yes" from the user.
when_to_use: |
  find-lock-contention surfaced a root blocker that is INACTIVE
  (orphaned client) or that has held a lock past a reasonable threshold,
  AND the user has either asked "what should I do" or has been informed
  of the lock chain. Never auto-triggers — always explicitly user-driven
  via "kill that session" or affirmative response to a kill proposal.
requires_toolsets:
  - oracle_db
  - oracle_db_write
required_environment_variables:
  - ORACLE_USER
  - ORACLE_PASSWORD
  - ORACLE_DSN
metadata:
  hermes:
    config:
      auto_load: false
      auto_confirm: false
      requires_user_confirmation: true
      cost_estimate: high
      destructive: true
---

# kill-session-suggestion

## When to Use

This is the flagship safety-gated mitigation skill. Trigger only when:

1. `find-lock-contention` has identified a specific root blocker by SID/serial.
2. That blocker is in one of these states:
   - `INACTIVE` and has held the lock for more than 5 minutes (orphaned client)
   - `ACTIVE` but running a runaway query that has held the lock for hours
   - Explicitly named by the user ("kill session 1247")
3. The user is in the chat asking what to do, OR has typed an affirmative response to a previous kill proposal.

**Never auto-execute.** The skill writes the SQL and posts it for explicit user confirmation. The agent's response loop must collect a "yes" from the user *as the very next message* — not later in the session, not implied from earlier context. The `user_confirmation_token` passed to `oracle.write_with_confirmation` must be that fresh "yes".

## Procedure

### Step 1: Confirm the kill target

Pull the latest session state. Things can change in the seconds between `find-lock-contention` running and this skill running:

```sql
SELECT s.sid,
       s.serial#,
       s.inst_id,
       s.username,
       s.osuser,
       s.machine,
       s.program,
       s.module,
       s.status,
       s.event,
       s.wait_class,
       s.seconds_in_wait,
       s.last_call_et,
       s.sql_id,
       s.prev_sql_id,
       s.state,
       (SELECT COUNT(*) FROM gv$session bs
         WHERE bs.blocking_session = s.sid
           AND bs.blocking_instance = s.inst_id) AS sessions_blocked
  FROM gv$session s
 WHERE s.sid = :sid
   AND s.serial# = :serial
   AND (s.inst_id = :inst_id OR :inst_id IS NULL);
```

If no rows, the session is already gone (closed itself, killed by someone else, network died). Tell the user "session is no longer there" and stop.

If `s.state = 'PREPARED'`, this is a distributed transaction in two-phase commit. **Refuse to kill.** Explain that killing a prepared session leaves a transaction in `dba_2pc_pending` requiring DBA cleanup. Recommend asking the user who initiated the distributed transaction to commit or roll back instead.

### Step 2: Pull the blocker's current SQL

For the user's visibility — they should see what the session was doing before they kill it:

```sql
SELECT sql_id, sql_fulltext, parse_calls, executions, last_active_time, status
  FROM v$sql
 WHERE sql_id = NVL(:sql_id, :prev_sql_id);
```

For long-running anonymous PL/SQL blocks, the SQL text may say `BEGIN ... END;` only — the actual problem call is deeper. Mention this.

### Step 3: Calculate rollback exposure

When `KILL SESSION IMMEDIATE` runs, Oracle rolls back the session's uncommitted transaction. Rollback is single-threaded and reads the rollback segments. A session that has done 5 million inserts will take a long time to roll back.

Estimate rollback time using `v$transaction.used_ublk` and `used_urec`:

```sql
SELECT t.used_ublk, t.used_urec, t.start_time, t.status
  FROM v$transaction t
  JOIN v$session s ON s.taddr = t.addr
 WHERE s.sid = :sid AND s.serial# = :serial;
```

Rough rule of thumb: 100k undo records → expect ~30 seconds rollback. 1M → expect ~5 minutes. 10M → expect ~30+ minutes. These numbers vary wildly with redo write rate and undo block size; surface them to the user as a warning, not a promise.

### Step 4: Propose the kill SQL

Build the SQL with the correct syntax for the cluster topology:

**Single-instance (non-RAC):**
```sql
ALTER SYSTEM KILL SESSION '<sid>,<serial#>' IMMEDIATE;
```

**RAC (uses `@inst_id`):**
```sql
ALTER SYSTEM KILL SESSION '<sid>,<serial#>,@<inst_id>' IMMEDIATE;
```

The `IMMEDIATE` keyword:
- Marks the session as `KILLED` immediately, even if PMON cleanup is delayed
- Returns control to the caller right away
- Does NOT force rollback to be faster — that's still single-threaded

Without `IMMEDIATE`, the kill waits for the session's current call to finish, which may be hours.

### Step 5: Output the proposal

Use this structure verbatim. Plain-text rather than markdown so it works in Telegram, Slack, and email gateways identically.

```
PROPOSED KILL — REQUIRES YOUR CONFIRMATION

Target session
  SID            <sid>
  Serial#        <serial>
  Instance       <inst_id> (cluster: <db_unique_name>)
  Username       <oracle_user>
  OS user        <os_user> on <machine>
  Program        <program> (<module>)
  Status         <ACTIVE|INACTIVE>
  Held lock for  <seconds>s  (~<human-friendly>)
  Blocking       <n> other sessions

Current SQL
<sql_text first 300 chars>

Rollback exposure
  Undo blocks    <used_ublk>
  Undo records   <used_urec>
  Estimated rollback time: <~Ns to ~Nmin>

The SQL I want to run
  ALTER SYSTEM KILL SESSION '<sid>,<serial>[,@<inst>]' IMMEDIATE;

Reply 'yes' to execute. Reply 'no' or anything else and I'll stop.
Reply 'show-alternatives' to see other options before killing.
```

If `sessions_blocked` is high (> 10), prepend an "ALERT" line and a one-sentence justification: "Blocking 23 sessions for 47 minutes. This kill releases them."

If `sessions_blocked` is low (≤ 2), de-emphasize: "Only 1 session blocked. You may want to wait for the holder to finish naturally instead."

### Step 6: Wait for confirmation, with a stronger gate for destructive ops

The agent must consume the user's next message. Two tiers of confirmation now apply:

**Tier 1 (standard writes — INSERT / UPDATE / DELETE / MERGE / CREATE INDEX / GRANT / etc.):** A plain affirmative is enough. Any of these count:
- `yes`, `y`, `confirm`, `proceed`, `ok`, `do it`, `kill it`, `go ahead`, `yes, do it`, `yes do it`

**Tier 2 (DESTRUCTIVE — DROP / TRUNCATE / ALTER):** A plain "yes" is NOT enough. The user must type one of:
- The target object's name (case-insensitive). For `DROP TABLE orders`, the user must type something containing `orders`. For `ALTER TABLE customers DROP COLUMN x`, the user must type something containing `customers`.
- OR the literal phrase `I understand`.

This applies to the `kill-session-suggestion` skill when the user types `ALTER SYSTEM KILL SESSION '<sid>,<serial>'` — that's an ALTER, so the user must type `system` (or `kill`, depending on parse) or `I understand`. In practice for kill-session the simplest prompt is: "Type **I understand** to confirm killing this session."

If the user responds with anything else, do NOT call `oracle_write_with_confirmation`. Instead:

1. Reply: "I'll hold off. Reply with 'I understand' to actually kill the session, or describe what you'd prefer."
2. Call `oracle_record_denial` to log the rejected proposal with the user's response and a one-sentence reason. The denial goes into the same audit log alongside approvals, so the trail captures every decision point.

The `user_confirmation_token` passed to `oracle_write_with_confirmation` is the user's literal response string. The audit log records exactly what the human typed plus the reason field with the agent's justification.

### Step 7: Show alternatives if asked

If user says `show-alternatives`, present these in priority order. The point is to give the user *options* before forcing the destructive action:

1. **Contact the holder.** If `osuser` and `machine` identify a human, ask them to commit or roll back. Often resolves in seconds; zero risk.
2. **Wait it out.** If `last_call_et` is small (active recently) and the holder is doing legitimate work, the lock may release naturally. Mention current undo growth rate as the signal.
3. **Kill the *blocked* sessions instead.** If the blocked sessions are themselves runaway or wrong, killing them might be safer than killing the legitimate root blocker.
4. **Increase the time-out on the application.** If this is a recurring pattern, the right fix is application-side: `sqlnet.expire_time` for dead connection detection, or shorter transaction boundaries in the app.
5. **The kill as proposed (riskiest).**

### Step 8: Execute on confirmation

```python
# Inside the agent's tool-call code path
oracle.write_with_confirmation(
    sql="ALTER SYSTEM KILL SESSION '1247,9988,@1' IMMEDIATE",
    binds=None,
    user_confirmation_token="yes"  # literal user response
)
```

The plugin appends to `~/.hermes/oracleops/writes.jsonl`. Every kill is recorded with timestamp, SQL, binds, the user's confirmation string, and rows affected.

### Step 9: Verify after kill

Wait 5–10 seconds for PMON to start cleanup, then re-check:

```sql
-- Is the session actually gone?
SELECT status, blocking_session_status, final_blocking_session
  FROM v$session
 WHERE sid = :sid AND serial# = :serial;
-- Expected: no rows, or status = 'KILLED'

-- Did the blocked sessions move on?
SELECT sid, event, wait_class, seconds_in_wait
  FROM v$session
 WHERE sid IN (<list of previously blocked SIDs>);
-- Expected: events are no longer enq:* on the same object

-- Is there a new root blocker now visible?
-- (Re-run find-lock-contention to confirm)
```

If the session is still in `KILLED` state after 60 seconds, rollback is still running. Tell the user explicitly: "Killed but still rolling back. Estimated remaining: <n> minutes based on undo segment activity." Do not panic-kill again; concurrent kills make it worse.

## Pitfalls

- **`KILL SESSION IMMEDIATE` does not bypass rollback.** It only returns control to the caller faster. The transaction still rolls back single-threaded.
- **Rollback can outlast the kill.** A session with 10M unflushed inserts can stay in KILLED status for hours while PMON cleans up. The locks may not release immediately. Communicate this clearly to the user.
- **Distributed transactions in PREPARED state.** Killing one leaves an in-doubt transaction in `dba_2pc_pending`. DBA cleanup required. Refuse, don't proceed.
- **Background processes.** `ALTER SYSTEM KILL SESSION` on a background process (DBW, LGWR, CKPT, PMON, SMON) will crash the instance. Always check `v$session.type = 'USER'` before proposing.
- **Sessions owned by SYS.** Often legitimate maintenance work (DBMS_JOB, MMON, MMNL). Killing them can corrupt AWR, scheduler, or stats gathering. Always confirm with the user and prefer waiting.
- **RAC `@inst_id` syntax.** Required on RAC clusters. Omitting it on RAC kills only the local instance's slot. The skill should always include `@inst_id` if `gv$instance` shows more than one instance.
- **Server processes vs client processes.** `v$session` shows server-side sessions. If the client process is the actual culprit (e.g., infinite loop in app code), killing the server session leaves the client open, and it may immediately reconnect and create the same lock. Recommend the user also kill the client.
- **Audit and compliance.** Some sites require approval workflow for production kills. The `~/.hermes/oracleops/writes.jsonl` log is a starting point but may not satisfy a compliance auditor. Mention this if the user is in a regulated environment.
- **PMON cleanup contention.** Killing many sessions at once can saturate PMON. If the user wants to kill 10+ sessions, propose batching with a short delay between each.

## Verification

After the kill:

1. `v$session` query returns zero rows for the original (sid, serial#) OR `status = 'KILLED'` with progressing undo cleanup.
2. Previously blocked sessions are no longer waiting on `enq:` events. Their `event` column has moved on to user I/O or idle.
3. A re-run of `find-lock-contention` does NOT show a new chain rooted at a different session for the same object. If it does, this was a *symptom*, not a root cause — recommend the user investigate the application's transaction handling.
4. The audit log entry is in `~/.hermes/oracleops/writes.jsonl` with the user's confirmation token.

Capture into MEMORY:
```
On <date> at <time>, killed SID <sid>,<serial> (<user>@<machine>, program <program>).
Reason: <held|blocked|recurring>. Affected <n> blocked sessions. Rollback completed
in <duration>. Confirmation token: <user's literal "yes" message>.
```

If the same `(user, machine, program)` shows up as a kill target three times in a session, the Curator will be prompted to author a `diagnose-{program}-orphan-pattern` skill that knows to look at this application's behavior first.
