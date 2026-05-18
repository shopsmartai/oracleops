---
name: find-lock-contention
description: Find which Oracle sessions are blocking which. Builds the full blocker chain, identifies the root blocker, returns the current SQL for each blocker plus the contended object. Names the lock type (TX row lock, TM table lock, UL user lock, etc.) so the remediation is targeted.
when_to_use: |
  User says queries are hanging, sessions are stuck, a specific user's UPDATE
  has been running for an hour, or `awr-summary-now` flagged
  `enq: TX - row lock contention` as a top wait event. Also runs as part of
  the orchestrator response to "the app is frozen."
requires_toolsets:
  - oracle_db
required_environment_variables:
  - ORACLE_CONNECTION_STRING
metadata:
  hermes:
    config:
      auto_load: false
      auto_confirm: true
      cost_estimate: low
---

# find-lock-contention

## When to Use

User reports symptoms that point at blocking:

- "The application is hung"
- "My UPDATE has been running for 40 minutes"
- "All the order-entry sessions are stuck"
- "Users can't save changes"
- `awr-summary-now` returned `enq: TX - row lock contention` or `enq: TM - contention` in the top waits
- A specific session ID was named: "session 1247 is blocking everyone"

Do not use this skill for slowness in general. Slowness with no blocking is `diagnose-slow-query` or `awr-summary-now`. This skill is specifically for cases where one session is preventing another from making progress.

## Procedure

### Step 1: Live snapshot from v$session

The single most useful query for lock contention is a live view of `v$session` filtered to blocking and blocked sessions:

```sql
SELECT s.sid,
       s.serial#,
       s.username,
       s.osuser,
       s.machine,
       s.program,
       s.status,
       s.blocking_session,
       s.final_blocking_session,
       s.event,
       s.wait_class,
       s.seconds_in_wait,
       s.sql_id,
       s.row_wait_obj#,
       s.row_wait_file#,
       s.row_wait_block#,
       s.row_wait_row#
  FROM v$session s
 WHERE s.blocking_session IS NOT NULL
    OR s.sid IN (SELECT blocking_session
                   FROM v$session
                  WHERE blocking_session IS NOT NULL);
```

`blocking_session` is the immediate blocker. `final_blocking_session` is the root blocker (walk-up of the blocker chain). For deep chains the root is what matters.

### Step 2: Build the blocker chain

A chain looks like this in practice:
```
SID 1247 (idle, holds lock) ← SID 882 ← SID 901 ← SID 1003 ← SID 1144
                              (blocked) (blocked) (blocked) (blocked)
```

Use Oracle's hierarchical query syntax against `v$session`:

```sql
SELECT LEVEL AS chain_depth,
       sid,
       serial#,
       username,
       blocking_session,
       event,
       seconds_in_wait,
       sql_id
  FROM v$session
 START WITH blocking_session IS NULL
        AND sid IN (SELECT blocking_session FROM v$session WHERE blocking_session IS NOT NULL)
 CONNECT BY PRIOR sid = blocking_session
 ORDER BY chain_depth, seconds_in_wait DESC;
```

`chain_depth = 1` is a root blocker. `chain_depth = 2` is blocked by a root. And so on.

### Step 3: Identify the lock type

The blocker's `event` column tells you the lock type. The common ones with their plain-English meaning:

| Event | What it means | Typical cause |
|---|---|---|
| `enq: TX - row lock contention` | Two sessions want to update the same row | Active transaction has not committed |
| `enq: TX - allocate ITL entry` | No free Interested Transaction List slot on a block | Too many concurrent transactions on the same block; increase INITRANS |
| `enq: TM - contention` | DDL waiting on DML, or DML waiting on DDL | Someone running ALTER TABLE while OLTP runs, or FK without index causing parent-side lock |
| `enq: UL - contention` | User-defined lock from `dbms_lock` | Application-level lock — check the app code |
| `enq: HW - contention` | High-water mark contention on segment growth | Concurrent inserts into a small, growing table; consider ASSM or parallel inserts |
| `enq: SQ - contention` | Sequence cache contention | Sequence `CACHE` is too small; raise it |
| `library cache lock` or `library cache pin` | DDL is invalidating cursors while DML runs | Schema change during peak hours |
| `cursor: pin S wait on X` | Hot SQL cursor under hard parse | Look at parse_calls vs executions — bind variable issue |

### Step 4: Get the contended object

For row lock contention, the agent should name the table and (where possible) the actual row:

```sql
SELECT o.owner,
       o.object_name,
       o.subobject_name,
       o.object_type,
       s.row_wait_file#,
       s.row_wait_block#,
       s.row_wait_row#
  FROM v$session s
  JOIN dba_objects o ON o.object_id = s.row_wait_obj#
 WHERE s.sid = :blocked_sid
   AND s.row_wait_obj# > 0;
```

For TM contention, find the table from `v$locked_object`:

```sql
SELECT lo.session_id,
       o.owner,
       o.object_name,
       lo.locked_mode,
       lo.oracle_username
  FROM v$locked_object lo
  JOIN dba_objects o ON o.object_id = lo.object_id
 WHERE lo.session_id IN (SELECT blocking_session FROM v$session WHERE blocking_session IS NOT NULL);
```

`locked_mode` codes: 1 = null, 2 = row-share, 3 = row-X, 4 = share, 5 = share row-X, 6 = exclusive.

### Step 5: Get the blocker's current SQL and its state

A root blocker is interesting for one of three reasons:

1. **It is actively running a long transaction** (`s.status = 'ACTIVE'` and `s.event` is something productive). Just wait.
2. **It is idle but has not committed** (`s.status = 'INACTIVE'` and the blocker has held the lock for minutes). This is the bad case: a developer left a transaction open in their IDE, or an app forgot to commit.
3. **It is `INACTIVE` because the client disappeared.** TCP died, but the session lingers because `sqlnet.expire_time` is too long or unset.

Pull the blocker's most recent SQL:

```sql
SELECT sql_id, sql_text, last_active_time, executions, status
  FROM v$sql
 WHERE sql_id = (SELECT sql_id FROM v$session WHERE sid = :blocker_sid)
    OR sql_id = (SELECT prev_sql_id FROM v$session WHERE sid = :blocker_sid);
```

If `last_active_time` is more than five minutes ago and the session is INACTIVE, this is almost certainly a stuck transaction with no human attached.

### Step 6: Report

Output structure:

```
LOCK CONTENTION SNAPSHOT — <timestamp>

ROOT BLOCKER
SID <n>.<serial> | User: <oracle_user> (OS: <os_user> on <machine>)
Program: <program>
Status: <ACTIVE/INACTIVE>
Lock type: <event>
Time held: <seconds_in_wait>s ≈ <human>
Contended object: <schema.table>
Current SQL: <sql_text first 200 chars>

BLOCKED SESSIONS
Count: <n>
Apps affected: <distinct list of program/machine>

BLOCKER CHAIN
SID <root> (lock holder)
└── SID <n> (waiting <n>s on <event>) — running <sql_id>
    └── SID <n> (waiting <n>s)
        └── ...

DIAGNOSIS
<one sentence: e.g., "Root blocker is a long-running UPDATE that has not committed.
The session is INACTIVE — likely a stuck client or unfinished transaction.">

REMEDIATION OPTIONS (in order of preference)
1. Contact the user of SID <n> and ask them to commit or rollback.
2. If session is genuinely orphaned, kill it: ALTER SYSTEM KILL SESSION '<sid>,<serial>' IMMEDIATE;
   (THIS IS A 🟡 MITIGATION — REQUIRES CONFIRMATION)
3. If this is recurring, look at INITRANS / MAXTRANS for the segment, or the application's transaction handling.

NEXT SKILL TO RUN
<kill-session-suggestion if appropriate, or null>
```

## Pitfalls

- **`KILL SESSION IMMEDIATE` rolls back the transaction.** That can take *longer* than just waiting for the blocker to finish, because rollback is single-threaded. If the user wants the row freed *now*, sometimes the right answer is to wait, not to kill.
- **`final_blocking_session` may be null** even when blocking is happening. This is a known Oracle quirk on some versions. Use the recursive `CONNECT BY` query as the source of truth.
- **Distributed transactions.** In a two-phase commit window, a session can be PREPARED and look idle but actually hold locks legitimately. Check `v$session.state = 'PREPARED'` before recommending kill.
- **`KILL SESSION` on the root may release everything cleanly, or it may surface a *new* root blocker that was masked by the first.** Run this skill again after a kill to confirm.
- **PMON cleanup latency.** On a heavily-loaded DB, killed sessions can stay in `KILLED` status for minutes while PMON cleans up. The blocked sessions may not move immediately. If urgent, consider `IMMEDIATE` (which can be more aggressive but riskier).
- **RAC.** In a RAC cluster, the blocker may be on a different instance. Use `gv$session` instead of `v$session` and check `inst_id`. Kill syntax adds the instance: `ALTER SYSTEM KILL SESSION '<sid>,<serial>,@<inst_id>'`.
- **`SELECT FOR UPDATE` plus app crash.** A surprisingly common case: app process crashes after a `SELECT FOR UPDATE` but before commit. The TCP connection lingers; the lock lingers. `sqlnet.expire_time` needs to be set (typically 10 min) to detect this.
- **Library cache contention is NOT row-lock contention.** A `library cache lock` event in the wait class `Concurrency` means DDL is recompiling something. The "blocking session" is the one doing DDL. Different remediation entirely — never kill the DDL session unless you understand what it is doing.

## Verification

After remediation (kill or commit by holder), re-run this skill. Confirm:

1. The previously named root blocker no longer appears in `v$locked_object`.
2. The count of `blocking_session IS NOT NULL` sessions is now zero.
3. None of the previously blocked sessions are in `enq:` waits anymore — `event` should have moved on.
4. Affected applications' programs are no longer accumulating in `v$session`.

If a *new* root blocker is now showing, this means the first one was masking a chain. Re-run the skill and inform the user that contention is a pattern, not an isolated session — recommend looking at the application's transaction management.

Capture into MEMORY:
```
On <date> at <time>, blocking chain root was SID <n> (user <u>, program <p>),
holding <event> on <schema.table> for <seconds>s. Blocked <n> sessions. Resolved
by <action> at <time>. Total user-visible impact: <duration>.
```

After three similar entries for the same `program` or `schema.table`, the Curator will be prompted to author a `diagnose-<app>-locking-pattern` skill that pre-loads the historical context.
