# Phase 0 Verification Checklist for Real Accounts

This checklist is for the machine owner running with actual Google AI Pro accounts (Accounts 2–4). Account 1 is reserved for the interactive main agent and is never used as a worker.

---

## 1. Directory & Worker Profile Isolation Setup

Each worker must have its own isolated directory structure with strict `0700` permissions.
Never place credentials in repository paths or shared locations.

```bash
# Example worker profile setup outside the repository
mkdir -p ~/.multigravity/workers/worker-1/{home,config,data,cache,state,runtime}
chmod -R 0700 ~/.multigravity/workers
```

Repeat for each worker (`worker-2`, `worker-3`, etc.).

---

## 2. Interactive One-Time Authentication

For each worker profile, perform an initial login interactively in a separate terminal:

```bash
HOME=~/.multigravity/workers/worker-1/home \
XDG_CONFIG_HOME=~/.multigravity/workers/worker-1/config \
XDG_DATA_HOME=~/.multigravity/workers/worker-1/data \
XDG_CACHE_HOME=~/.multigravity/workers/worker-1/cache \
XDG_STATE_HOME=~/.multigravity/workers/worker-1/state \
XDG_RUNTIME_DIR=~/.multigravity/workers/worker-1/runtime \
agy
```

Follow the standard on-screen authentication prompts to bind the worker's Google AI Pro account. Once authenticated, exit with `/quit` or `Ctrl+D Ctrl+D`.

---

## 3. Verify Isolation & Auth with Safe Probes

Run the non-invasive probe scripts:

1. **Verify CLI environment**:
   ```bash
   python3 probes/probe_cli.py
   ```
   Confirm that all features (print mode, JSON output, skip permissions, MCP) report `True`.

2. **Verify Worker Profile Auth**:
   ```bash
   python3 probes/probe_worker_auth.py ~/.multigravity/workers/worker-1
   ```
   Expected output:
   ```text
   Status: AUTHENTICATED
   Detail: Worker profile is authenticated and active.
   ```

3. **Verify MCP Transport & Latency**:
   ```bash
   python3 probes/probe_mcp_client.py
   ```
   Confirm all latency measurements are well under the 1.0s target.

---

## 4. Rate Limiting (429 / RESOURCE_EXHAUSTED) Verification

1. When a worker hits per-account RPM / concurrency limits, observe the error signal.
2. Confirm that `probe_worker_auth.py` or the runner detects `RATE_LIMITED` / `429` / `RESOURCE_EXHAUSTED`.
3. Confirm that the core daemon moves the worker to `COOLDOWN` and backs off exponentially without crashing or forwarding provider errors to the main agent.

---

## 5. Session Expiry & Re-Authentication Verification

1. When an auth token expires or session becomes invalid:
   - Worker transitions to `AUTH_EXPIRED`.
   - Main agent is notified via `list_workers()` and structured logs.
   - Core daemon does not attempt automated interactive login.
2. Re-authenticate by re-running the command from Step 2 when at the laptop.
