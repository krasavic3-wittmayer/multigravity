# multigravity

`multigravity` is a lightweight local orchestration layer between one interactive main agent (Account 1) and 1–3 background sub-agents (Accounts 2–4), each running on a separate paid Google AI Pro account.

The system offloads heavy, read-mostly tasks (deep research, reading tens of KB of documentation, test generation, and code review) to idle sub-agents through a local daemon and Model Context Protocol (MCP) shim, returning only compressed, schema-bound results to keep the main agent's context clean and responsive.

---

## Architecture

The system is deliberately split into two decoupled components so that killing, restarting, or disconnecting the MCP client/shim never terminates background worker jobs:

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

1. **Core Daemon (`multigravity-daemon`)**:
   - Long-lived background process running under the user's Linux session.
   - Manages the FIFO task queue, dispatcher, worker state machines (`IDLE`, `BUSY`, `COOLDOWN`, `AUTH_EXPIRED`, `DISABLED`), crash recovery journal, and structured event logs.
2. **MCP Shim (`multigravity-mcp-shim`)**:
   - Thin, stateless stdio adapter spawned by the main agent's MCP client.
   - Translates MCP tool calls into Unix Domain Socket requests (`mode 0600`).
   - If the shim process is killed mid-task, tasks in the daemon continue uninterrupted.
3. **Context Funnel**:
   - Enforces strict output contract schemas (`summary`, `key_findings`, `interface_spec_or_diff`, `sources`, `confidence`, `open_questions`).
   - Hard character cap (`max_output_chars`, default 4000).
   - One automatic repair/compression pass on contract violation; raw transcripts are saved to disk and never dumped into the main agent's context.

---

## Hard Boundaries & Rules

- **Account Limits**: Strictly 2–4 accounts total (Account 1 is dedicated to the main agent; up to 3 worker accounts).
- **No Token Tracking**: No token counting, burn-down, or budgets. The system designs strictly for per-account concurrency, RPM limits, and wall-clock parallelization.
- **No Heavy Infrastructure**: Zero external database servers, no message brokers, no web UI. Pure Python standard library (zero third-party dependencies).
- **Official Interfaces Only**: Workers run via the official `/usr/sbin/agy` CLI in isolated profiles. No spoofing or reverse-engineered endpoints.

---

## MCP Tool Surface

The main agent interacts with `multigravity` via 6 fast tools (all return < 1s):

| Tool | Parameters | Description |
|---|---|---|
| `submit_task` | `kind`, `instruction`, `inputs`, `output_spec`, `max_output_chars`, `mode` | Enqueues a background task and returns `task_id` immediately. |
| `get_status` | `task_ids` | Queries task state (`QUEUED`, `RUNNING`, `DONE`, `FAILED`, `CANCELLED`), attempt count, and worker label. |
| `get_result` | `task_id` | Fetches the compressed, schema-bound result for a completed task. |
| `wait` | `task_ids`, `timeout_s` | Waits until tasks complete or timeout expires. Hard-capped at 25s so MCP tool-call timeouts are never reached. |
| `cancel_task` | `task_id` | Cancels an in-flight or queued task, killing the worker process group. |
| `list_workers` | *(none)* | Diagnostic tool showing worker health, current state, and cooldown countdowns. |

---

## Quick Start with `mgy` & `install.sh`

The fastest way to install and operate Multigravity is via `install.sh` and the `mgy` unified launcher:

```bash
# 1. Run the one-step installer
./install.sh

# 2. Launch the interactive Main Agent with fleet orchestration & subagent awareness
mgy

# Or launch with an initial task prompt:
mgy "analyze the dependencies in this project"

# Monitor, diagnose, or stop:
mgy status
mgy doctor
mgy stop
```

`mgy` automatically performs self-healing preflight, verifies worker logins, ensures the core background daemon is running, and injects the Main Agent with the active fleet prompt and subagent delegation instructions.

---

## Setup & Running

### 1. Requirements
- Linux (x86_64 or aarch64)
- Python 3.12+ (Python 3.14 recommended, zero third-party dependencies required)
- Google Antigravity CLI (`agy`) installed at `/usr/sbin/agy`
- Desktop keyring isolation tools (`dbus-run-session`, `gnome-keyring-daemon`, `secret-tool`)

### 2. Configure Workers
Copy the example configuration:
```bash
cp config.example.toml config.toml
```
Edit `config.toml` to specify your worker profiles (1 to 3 background accounts; Account 1 is reserved for the interactive main agent).

### 3. Run Doctor to Audit Isolation
Verify path guards, environment filtering, and collision safety:
```bash
python3 -m multigravity.cli doctor
```

### 4. Interactive Sequential Login Wizard
Run the unified CLI without arguments to walk through logging in unverified accounts one at a time:
```bash
python3 -m multigravity.cli
```
Or log in / re-authenticate a single worker profile:
```bash
python3 -m multigravity.cli login worker-1
```
- **Login Isolation**: Each worker runs inside a dedicated private D-Bus session with an isolated keyring (`$XDG_DATA_HOME/keyrings/login.keyring`, mode `0700`).
- **OAuth URL Interception**: The browser helper displays the login URL prominently and warns you to sign in with the worker's dedicated account (not Account 1).
- **Duplicate Prevention**: If an account is already logged into another worker or the main account, the login is rejected and an isolated wipe/retry is offered.

### 5. Check Status Board
```bash
python3 -m multigravity.cli status
```

### 6. Stop Core Daemon
```bash
python3 -m multigravity.cli stop
```

### 7. Register with Main Agent MCP Client
Register the MCP shim with `agy`:
```bash
agy mcp add multigravity python3 -m multigravity.mcp_shim
```

---

## Running Acceptance Tests

Run the full unit and integration test suite with a single command:
```bash
python3 -m unittest discover tests
```

### Verified Acceptance Criteria
1. **Parallel Execution**: 10 submitted tasks across 3 mock workers complete in parallel time (~0.4s vs. 1.0s serial).
2. **429 Failover**: Injecting rate limits on a worker transparently re-queues the task on another worker; the main agent sees no provider errors, and the cooled-down worker re-enters rotation after cooldown.
3. **Queue Deadline**: When all workers are in cooldown, tasks wait cleanly up to `queue_deadline_s` and fail with structured `NO_WORKER_AVAILABLE` without hanging.
4. **Crash & Restart Resilience**:
   - Killing the MCP shim mid-task does not interrupt running daemon tasks; a new shim retrieves the result.
   - Killing the daemon mid-task recovers tasks from the journal on restart, marking `RUNNING` tasks as `QUEUED` (`attempt + 1`) and completing them with zero orphan processes.
5. **Contract Enforcement**: Oversized or non-schema outputs are intercepted by the validator, undergo a repair pass, and never forward raw transcripts to the main agent.
6. **Timeout Cap**: `wait` is hard-capped at 25 seconds to protect client timeouts.

---

## Troubleshooting

- **Worker State Shows `AUTH_EXPIRED`**:
  The session for that account expired. Re-authenticate the profile using `./scripts/multigravity-auth <profile_path>`. The daemon will automatically resume assigning tasks to it once re-armed.
- **Tasks Failing with `NO_WORKER_AVAILABLE`**:
  All configured worker accounts are either in cooldown or all workers are disabled/expired. Check `list_workers` for cooldown timers and inspect `logs/events.jsonl`.
- **Daemon Socket Connection Refused**:
  Ensure `multigravity-daemon` is running. Check that `$XDG_RUNTIME_DIR/multigravity.sock` exists and has permissions `0600`.
