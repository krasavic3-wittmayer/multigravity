# Phase 0 Investigation Report: Antigravity Orchestration & MCP Surface

## Executive Summary
This report documents the programmatic driving mechanisms, authentication models, MCP transport support, error surfaces (rate limits and auth expiry), and adapter architecture for `multigravity`.

---

## Findings

### (a) Programmatic Control of Antigravity Instances & Auth Model
- **Primary Driving Interface**: The official `agy` CLI binary (located at `/usr/sbin/agy`).
  - Supports headless single-prompt execution via `--print` / `-p` with structured JSON output (`--output-format json` or `--output-format stream-json`).
  - Supports auto-approval of permissions via `--dangerously-skip-permissions`, ensuring headless worker sub-agents never block waiting for terminal confirmation.
  - Supports structured output schema enforcement via `--json-schema <file_or_string>`, ensuring response compliance.
  - Supports model selection (`--model`, e.g., `gemini-3.8-flash-high`) and reasoning effort tuning (`--effort`).
- **Secondary Driving Interface**: The official `google-antigravity` Python SDK (`pip install google-antigravity`), providing `Agent(LocalAgentConfig(...))` asynchronous context management.
- **Authentication Model**:
  - Antigravity stores credentials, tokens, and session states per user profile (by default in `~/.gemini/antigravity-cli`).
  - Complete worker isolation is achieved on Linux by pointing environment variables (`HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`, `XDG_RUNTIME_DIR`) to a dedicated profile directory mode `0700`.
  - Credentials remain strictly separated between accounts and never cross into the main agent's profile (Account 1).

### (b) MCP Support in Main Agent Client
- **Transport Support**:
  - `agy mcp add` explicitly supports both `stdio` (default) and `http` transports.
  - For local execution on Linux, a thin stdio MCP shim communicating with the core daemon over a Unix domain socket (`$XDG_RUNTIME_DIR/multigravity.sock`, mode `0600`) provides maximum speed (<1 ms tool latency) and process boundary protection.
  - If remote or HTTP MCP is ever configured, streamable HTTP bound to `127.0.0.1` with a random bearer token is supported.
- **Tool-Call Timeouts**:
  - Standard MCP clients enforce client-side call timeouts (typically 30–60s).
  - To guarantee the client never experiences a client-side timeout, the `wait()` tool hard-caps its wait duration at 25 seconds, allowing the main agent to loop cleanly.
- **Notifications**:
  - MCP progress/resource notifications can be emitted, but all status and results are reliably queryable via polling tools (`get_status`, `get_result`). Correctness never relies on notifications.

### (c) Rate Limiting Behavior (429 / Concurrency Signals)
- Rate limits present as HTTP 429, `RESOURCE_EXHAUSTED` status codes, or `Quota exceeded` in stderr/JSON payloads.
- When detected, the adapter signals `RATE_LIMITED` to the dispatcher.
- The worker enters state `COOLDOWN` with exponential backoff and jitter (respecting `Retry-After` if supplied).
- The failed task is transparently re-queued at the front of the queue (`attempt + 1`), excluding the cooled-down worker. The main agent only sees `QUEUED` or `RUNNING`.

### (d) Session Expiry Behavior
- Session expiry manifests through error signatures:
  - `oauth2: cannot fetch token`
  - `Keyring error`
  - `could not find default credentials`
  - `unauthenticated` / `login required`
- The worker is moved to state `AUTH_EXPIRED` and removed from the active dispatcher rotation.
- Automated interactive login is strictly forbidden. The system continues running on any remaining healthy workers.
- The administrator can re-authenticate the expired profile interactively via a documented command.

---

## Recommended Adapter Architecture

### Primary Recommendation: `AgyCliWorkerAdapter`
- **Implementation**: Executes `/usr/sbin/agy --print ... --dangerously-skip-permissions --output-format json` under an isolated process group (`preexec_fn=os.setsid` or `setresuid`/`PR_SET_PDEATHSIG`).
- **Rationale**:
  - Zero external Python dependencies required beyond the Python standard library (`asyncio.subprocess`).
  - Directly leverages the installed and maintained `agy` runtime and official binary flags.
  - Clean lifecycle termination: killing the process group reaps all sub-processes with no orphaned processes.
  - Interactive re-auth is straightforward using the standard CLI in a terminal.

### Fallback Recommendation: `AntigravitySdkWorkerAdapter`
- **Implementation**: Uses `google.antigravity.Agent` from the `google-antigravity` package.
- **Rationale**: Fallback if in-process Python streaming of tokens/tool calls is desired in future phases.
