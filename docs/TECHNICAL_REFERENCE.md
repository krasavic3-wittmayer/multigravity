# Technical Reference Manual

This document contains the detailed architectural specifications, interface definitions, security isolation mechanics, and operational reference for `multigravity` that were originally established in the technical README.

---

## 1. System Architecture & Decoupled Design

`multigravity` is designed with strict component decoupling so that killing, restarting, or disconnecting the MCP client/shim never terminates background worker jobs.

```
+-----------------------------------------------------------+
|              Interactive Main Agent (Account 1)           |
+-----------------------------------------------------------+
                             |
                   MCP JSON-RPC (stdio)
                             v
+-----------------------------------------------------------+
|                 MCP Shim (thin, stateless)                |
+-----------------------------------------------------------+
                             |
            Unix Domain Socket ($XDG_RUNTIME_DIR)
                             v
+-----------------------------------------------------------+
|               Core Daemon (long-lived, Linux)             |
|  - Task Queue (FIFO)          - Worker Registry & LRU     |
|  - Journal & Crash Recovery   - Context Funnel & Validator|
|  - 429 Cooldown / Backoff     - Structured Event Logger   |
+-----------------------------------------------------------+
          |                         |                     |
     agy CLI (0700)            agy CLI (0700)        agy CLI (0700)
          v                         v                     v
+-------------------+     +-------------------+ +-------------------+
|  SubAgent 1 (Acc2) |     |  SubAgent 2 (Acc3) | |  SubAgent 3 (Acc4) |
+-------------------+     +-------------------+ +-------------------+
```

### Core Subsystems

1. **Core Daemon (`multigravity-daemon`)**:
   - Long-lived background process running under the user's Linux session.
   - Manages the FIFO task queue, dispatcher, worker state machines (`IDLE`, `BUSY`, `COOLDOWN`, `AUTH_EXPIRED`, `DISABLED`), crash recovery journal, and structured event logs.
   - Listens on a Unix domain socket at `$XDG_RUNTIME_DIR/multigravity.sock` with file mode `0600` (restricted to the user).

2. **MCP Shim (`multigravity-mcp-shim`)**:
   - Thin, stateless stdio adapter spawned by the main agent's MCP client (`agy`).
   - Translates MCP tool calls into Unix Domain Socket requests (`mode 0600`).
   - If the shim process is killed mid-task, tasks running inside the daemon continue uninterrupted. A new shim can connect and fetch the results seamlessly.

3. **Context Funnel**:
   - Intercepts all raw outputs from worker subprocesses.
   - Enforces strict output contract schemas (`summary`, `key_findings`, `interface_spec_or_diff`, `sources`, `confidence`, `open_questions`).
   - Imposes a hard character cap (`max_output_chars`, default `4000`).
   - Executes one automatic repair/compression pass on contract violation; raw transcripts are saved to disk (`logs/transcripts/`) and never dumped into the main agent's context.

---

## 2. Hard Boundaries & Operational Constraints

- **Account Limits**: Strictly 2–4 accounts total. Account 1 is reserved exclusively for the interactive main agent; Accounts 2–4 (1 to 3 accounts) serve as background worker accounts.
- **Zero Token Tracking**: No token counting, burn-down charts, or token-bucket budgets. Concurrency, per-account RPM limits, and wall-clock parallelization are the primary operational constraints.
- **Zero Heavy Infrastructure**: No external database servers (PostgreSQL/MySQL), no message brokers (Redis/RabbitMQ), and no web UI. Built entirely on the Python 3.12+ standard library with zero third-party dependencies.
- **Official CLI Isolation**: Workers run via the official `/usr/sbin/agy` CLI in isolated profiles with mode `0700`. No reverse-engineered private endpoints or client spoofing.

---

## 3. Worker State Machine

Workers transition through the following formal states:

| State | Description | Transition Triggers |
|---|---|---|
| `IDLE` | Worker is healthy, authenticated, and ready to accept a task. | Daemon startup, task completion, or cooldown expiry. |
| `BUSY` | Worker is currently executing a task process group. | Task assignment from FIFO queue. |
| `COOLDOWN` | Worker encountered rate limits (HTTP 429) and is temporarily paused. | Provider 429 response; returns to `IDLE` after backoff delay. |
| `AUTH_EXPIRED` | Worker session or OAuth token has expired; cannot accept tasks. | Session expiration detected; requires manual re-auth via CLI. |
| `DISABLED` | Worker manually disabled via configuration or unrecoverable error. | `enabled = false` in `config.toml`. |

---

## 4. Context Funnel & Output Contract Schema

Worker tasks must return schema-bound JSON adhering to the following structure:

```json
{
  "summary": "High-level overview of the result (required, string)",
  "key_findings": [
    "Key finding 1 (required list of strings)"
  ],
  "interface_spec_or_diff": "Optional interface specification, unified diff, or empty string",
  "sources": [
    "List of files, docs, or URLs consulted (required list of strings)"
  ],
  "confidence": 0.95,
  "open_questions": [
    "Optional list of open questions or unresolved ambiguities"
  ]
}
```

### Funnel Guarantees:
- **Character Limit**: Default `max_output_chars = 4000`. Configurable per task or globally.
- **Repair Pass**: If the worker returns malformed JSON or exceeds `max_output_chars`, the middleware initiates a single automated repair/compression pass. If that fails, it returns a structured `OUTPUT_CONTRACT_VIOLATION` error.
- **Transcript Isolation**: Raw execution traces, agent thought processes, and full command outputs are saved directly to disk in `logs/` for offline inspection and never sent to the main agent's context.

---

## 5. MCP Tool Surface (Detailed Specification)

The main agent interacts with the `multigravity` daemon through 6 fast tools. All tools return within < 1 second.

| Tool | Parameters | Parameter Types & Defaults | Description |
|---|---|---|---|
| `submit_task` | `kind`<br>`instruction`<br>`inputs`<br>`output_spec`<br>`max_output_chars`<br>`mode` | `kind`: string (`research`, `doc_digest`, `test_gen`, `review`, `custom`)<br>`instruction`: string<br>`inputs`: dict / string<br>`output_spec`: dict (optional)<br>`max_output_chars`: int (default `4000`)<br>`mode`: string (`read_only` default, or `worktree`) | Enqueues a background task into the FIFO queue and returns `task_id` immediately. |
| `get_status` | `task_ids` | `task_ids`: list of strings | Queries task status: returns state (`QUEUED`, `RUNNING`, `DONE`, `FAILED`, `CANCELLED`), attempt count, worker label, and timestamps. |
| `get_result` | `task_id` | `task_id`: string | Fetches the compressed, schema-bound result for a completed task. |
| `wait` | `task_ids`<br>`timeout_s` | `task_ids`: list of strings<br>`timeout_s`: float (default `10.0`) | Waits until specified tasks reach a terminal state or timeout expires. **Hard-capped at 25 seconds** so MCP client tool-call timeouts are never triggered. The main agent can loop calls if necessary. |
| `cancel_task` | `task_id` | `task_id`: string | Cancels an in-flight or queued task. If `RUNNING`, sends `SIGTERM`/`SIGKILL` to the worker process group. |
| `list_workers` | *(none)* | *(none)* | Diagnostic tool returning worker IDs, labels, lifecycle states (`IDLE`, `BUSY`, `COOLDOWN`, `AUTH_EXPIRED`, `DISABLED`), and cooldown countdown timers. |

### Error Model
The main agent never sees raw HTTP 429s or raw provider crashes. Errors are structured with standard error codes:
- `NO_WORKER_AVAILABLE`: Queue deadline exceeded while all workers were cooling down or unavailable.
- `TASK_FAILED`: Task process exited with a fatal error after retries were exhausted.
- `OUTPUT_CONTRACT_VIOLATION`: Worker output failed schema validation even after an automated repair pass.
- `CANCELLED`: Task was explicitly aborted via `cancel_task`.

---

## 6. Security & Login Isolation Mechanics

Worker profiles are strictly isolated from each other and from the interactive main agent (Account 1):

1. **Private D-Bus Sessions & Keyrings**:
   - Each worker runs in an independent D-Bus session spawned via `dbus-run-session`.
   - Keyring storage is locked into `$XDG_DATA_HOME/keyrings/login.keyring` with permissions `0700`.
   - Separate directory trees are maintained for `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, and `XDG_STATE_HOME`.

2. **OAuth URL Interception**:
   - During worker authentication, the browser helper intercepts the OAuth challenge URL and presents it in the terminal.
   - Prompts explicitly remind the operator to log in using the assigned background worker account (Accounts 2–4), preventing accidental credential overlap with Account 1.

3. **Duplicate Identity Prevention**:
   - If an account is already authenticated in another worker profile or matches the main account identity, the login is rejected.
   - An isolated profile wipe and re-try prompt is offered to prevent credential contamination.

---

## 7. Command-Line Reference

While `mgy` provides a unified launcher, the low-level CLI provides granular management:

```bash
# Preflight isolation check (audits permissions, path guards, collision safety)
python3 -m multigravity.cli doctor

# Interactive sequential login wizard (logs in unauthenticated workers one-by-one)
python3 -m multigravity.cli

# Explicit login / re-authentication for a specific worker
python3 -m multigravity.cli login <worker-id>

# Display live status board (workers, active tasks, queue depth)
python3 -m multigravity.cli status

# Gracefully terminate the core daemon
python3 -m multigravity.cli stop

# Manually register the MCP shim with agy
agy mcp add multigravity python3 -m multigravity.mcp_shim
```

---

## 8. Verified Acceptance Criteria & Benchmark Metrics

The test suite validates the following six operational criteria (`python3 -m unittest discover tests`):

1. **Parallel Execution**:
   - 10 submitted tasks across 3 mock workers complete in parallel time (~0.4s vs. 1.0s serial execution).
2. **429 Failover**:
   - Injecting rate limits on an active worker transparently re-queues the task (`attempt + 1`) to another available worker. The main agent observes continuous progress with zero provider errors, and the cooled-down worker re-enters the rotation after backoff expires.
3. **Queue Deadline**:
   - When all workers are simultaneously in cooldown or disabled, queued tasks wait cleanly up to `queue_deadline_s` before terminating with `NO_WORKER_AVAILABLE` without process hangs or thundering herds.
4. **Crash & Restart Resilience**:
   - **Shim Disconnection**: Terminating `multigravity-mcp-shim` mid-task leaves daemon worker processes running; a new shim retrieves the result.
   - **Daemon Recovery**: Terminating the daemon recovers in-flight tasks from the persistent journal on restart, marking `RUNNING` tasks as `QUEUED` (`attempt + 1`) and completing them with zero orphaned processes.
5. **Contract Enforcement**:
   - Non-compliant or oversized outputs are intercepted by the output validator, subjected to an automated repair pass, and never forward raw transcripts or reasoning traces to the main agent.
6. **Timeout Cap**:
   - The `wait` tool enforces a strict 25-second ceiling regardless of requested `timeout_s`, guaranteeing that client-side MCP timeouts are never exceeded.

---

## 9. Technical Troubleshooting & Recovery

### `AUTH_EXPIRED`
- **Cause**: The worker's session token or OAuth credentials have expired.
- **Remedy**: Re-authenticate the profile using:
  ```bash
  python3 -m multigravity.cli login <worker-id>
  # or
  ./scripts/multigravity-auth <profile_path>
  ```
  The daemon will automatically detect valid credentials and re-arm the worker.

### `NO_WORKER_AVAILABLE`
- **Cause**: All configured worker accounts are cooling down from HTTP 429 rate limits, are unauthenticated (`AUTH_EXPIRED`), or have been disabled.
- **Remedy**:
  - Run `mgy status` or call `list_workers` to inspect cooldown timers.
  - Review `logs/events.jsonl` for rate-limit frequency.
  - If necessary, adjust `cooldown_base_s` or configure additional worker accounts in `config.toml`.

### Daemon Socket Connection Refused
- **Cause**: The `multigravity-daemon` process is not running or the socket file was removed.
- **Remedy**:
  - Verify if daemon is active: `mgy status` or `python3 -m multigravity.cli status`.
  - Confirm `$XDG_RUNTIME_DIR/multigravity.sock` exists and has permissions `0600`.
  - Restart the daemon with `mgy` or `python3 -m multigravity.daemon`.
