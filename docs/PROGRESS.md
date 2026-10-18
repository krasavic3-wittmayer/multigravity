# Progress Log

## Current Phase
Phase 6 Complete: Preflight, Real Keyring Smoke Verification, Leak Audit & Owner Login Runbook

## Done
- **Bootstrap repository**: Created `~/Projects/multigravity`, initialized git with branch `main`.
- **Initial files & commit**: Created `AGENTS.md`, `README.md`, `.gitignore`, and `docs/SPEC.md`. Initial commit `8f60918f196048a371b49b9f14f9b2548934ee82`.
- **Remote configuration**: Switched git origin back to SSH (`git@github.com:krasavic3-wittmayer/multigravity.git`). Added rule to `AGENTS.md` specifying that the remote uses SSH and network git commands use `GIT_SSH_COMMAND='ssh -o BatchMode=yes'` to prevent blocking. Verified fail-fast behavior with publickey check.
- **Phase 0 Investigation Deliverables**:
  - `probes/probe_cli.py`: Verified agy CLI capabilities, print mode, permissions bypass, and environment isolation.
  - `probes/probe_worker_auth.py`: Non-invasive, credentials-safe auth and health checker for worker profiles.
  - `probes/probe_mcp_client.py`: Verified stdio JSON-RPC transport latency (< 1ms vs. < 1.0s target).
  - `docs/PHASE0_CHECKLIST.md`: Step-by-step instructions for laptop real account verification.
  - `docs/PHASE0_REPORT.md`: Comprehensive answers to investigation questions (a)–(d) and recommendation of `AgyCliWorkerAdapter`.
- **Phase 1 Core Engine**:
  - `multigravity/models.py`: Data models, enums (`TaskState`, `WorkerState`, `TaskKind`, `ErrorCode`), and structured error definitions.
  - `multigravity/config.py`: TOML configuration parser using Python 3.11+ `tomllib`, dataclasses with strict 4-account boundary validation.
  - `multigravity/logging.py`: Structured JSON-lines logger with per-task timeline and sensitive credential redacting.
  - `multigravity/journal.py`: Append-only JSON-lines journal store with automatic crash recovery (`RUNNING` -> `QUEUED` attempt+1).
  - `multigravity/validator.py`: Output contract validator, JSON extraction from markdown fences, character cap enforcement, and automatic repair prompt generation.
  - `multigravity/adapters/base.py`: Abstract base class for worker adapters.
  - `multigravity/adapters/fake.py`: `FakeWorkerAdapter` with comprehensive fault injection (latency, 429s, auth failures, hangs, malformed output).
  - `multigravity/workers.py`: `WorkerRegistry` managing state transitions, LRU selection, and exponential backoff cooldown with jitter.
  - `multigravity/dispatcher.py`: FIFO task queue, LRU idle assignment, rate-limit cooldown failover, and queue deadline enforcement.
- **Phase 2 MCP Shim & IPC**:
  - `multigravity/ipc.py`: Unix Domain Socket IPC server (`mode 0600`) and client communication utilities.
  - `multigravity/mcp_shim.py`: Thin, stateless stdio MCP server exposing all 6 tools (`submit_task`, `get_status`, `get_result`, `wait`, `cancel_task`, `list_workers`).
  - `multigravity/daemon.py`: Core daemon orchestrator managing background dispatcher loop, signal handling, and IPC server.
- **Phase 3 Real Adapter**:
  - `multigravity/adapters/agy_cli.py`: Production `AgyCliWorkerAdapter` running `/usr/sbin/agy --print ... --dangerously-skip-permissions` under isolated Linux process groups with isolated `0700` profile environments.
- **Phase 4 Hardening & Acceptance Verification**:
  - `config.example.toml`: Documented configuration example.
  - `scripts/multigravity-auth`: Interactive CLI tool to re-authenticate worker profiles safely.
  - `pyproject.toml`: Project build definition with zero external third-party dependencies.
  - `README.md`: Architecture diagrams, usage documentation, troubleshooting, and verification guide.
- **Phase 5 Real Worker Login & Isolation**:
  - `docs/LOGIN_ISOLATION.md`: Documented empirical discoveries: `agy` uses D-Bus desktop keyring (`service=gemini`, `username=antigravity`), which creates silent cross-account overwrite collision unless isolated with private D-Bus sessions (`dbus-run-session`) and unlocked private `gnome-keyring-daemon` instances (`mode 0700`).
  - `multigravity/isolation.py`: Strict `PathGuard` (refuses user HOME, parent dirs, `~/.gemini`, `~/.config`, overlapping worker paths), `EnvironmentAllowList` (whitelists `PATH`, `LANG`, `TERM`; strips all API keys/tokens), identity hashing with random salt, and masked email formatting (`ac***2@example.com`).
  - `scripts/multigravity-browser-helper`: Intercepts `xdg-open` browser calls, displays OAuth URL prominently in a banner, warns against auto-selecting Account 1, and attempts private/incognito browser launch.
  - `multigravity/probes.py`: Status probe (`OK`, `NOT_LOGGED_IN`, `AUTH_EXPIRED`, `RATE_LIMITED`, `UNKNOWN`) and automatic identity extraction from private keyring without reading credential tokens.
  - `multigravity/wizard.py`: Sequential interactive login wizard with PID lock file (`multigravity_wizard.lock`), duplicate account collision prevention (cross-worker and Account 1 check), and clean `SIGINT` process group cleanup.
  - `multigravity/doctor.py`: Diagnostics and two-worker collision test suite.
  - `multigravity/cli.py`: Unified CLI (`multigravity`, `login`, `status`, `doctor`, `stop`) using standard library `argparse`.
- **Phase 6 Verification, Smoke Testing & Audit**:
  - `multigravity/preflight.py`: Added early dependency checks for Python 3.10+, `agy`, `dbus-run-session`, `gnome-keyring-daemon`, `secret-tool`, `xdg-open`, and `bash` with exact Arch Linux package install commands (`sudo pacman -S dbus gnome-keyring libsecret xdg-utils`).
  - `config.local.toml`: Created gitignored local config for real use mapping `worker-1` -> Account 2, `worker-2` -> Account 3, `worker-3` -> Account 4 under `~/.multigravity/workers/worker-N`.
  - Worker display format: Updated all status boards and wizard prompts to explicitly display `worker-N (Account X)` so the two numbering schemes cannot be confused.
  - `multigravity/identity.py` & `multigravity set-main-identity`: Implemented Account 1 identity storage as salted SHA-256 hash in `~/.multigravity/state/main_identity.json` (`mode 0600`), never storing plaintext email; wizard enforces Account 1 presence before logins.
  - Real Keyring Smoke Test (`tests/test_keyring_smoke.py`): Verified end-to-end secret storage and retrieval in private D-Bus/keyring session, verified zero modification to host desktop keyring (`~/.local/share/keyrings`) and `~/.gemini`, zero orphan processes, and clean throwaway directory removal.
  - Browser Helper Smoke Test: Verified `scripts/multigravity-browser-helper` with dummy URL; verified prominent banner display and zero contact with Google URLs.
  - Real Doctor & Unit Tests: Ran `multigravity doctor` with real config and binaries; all checks clearly labeled `[REAL]` and `[SIMULATED]`; full test suite (43 tests) passing.
  - Leak Audit: Scanned git history and working tree for emails, JWTs (`eyJ`), API keys, and credentials; confirmed `.gitignore` strictly covers `*.keyring`, `config.local.toml`, `state/`, `*.salt`, `*.lock`, `*.pid`, `worker_profiles/`, `workers/`, `__pycache__`, and `.worktrees/`.
  - SSH Check: Executed read-only `ssh-add -l` check without modifying keys or configuration.
  - `docs/LOGIN_RUNBOOK.md`: Comprehensive step-by-step interactive onboarding guide written for the owner.
  - `AGENTS.md` Rule Update: Owner explicitly permitted agents to create git commits at agent judgment when coherent units of work are verified; git push, force-push, tag creation, remote changes, and git config changes remain forbidden.

- **Phase 7 Pre-Login Verification & Anchoring**:
  - Anchored root and single-location patterns in `.gitignore` (`/workers/`, `/state/`, `/worker_profiles/`, `/journals/`, `/logs/`, `/build/`, `/.venv/`, `/config.local.toml`, etc.) to prevent nested code hiding, and added unit tests in `tests/test_gitignore.py` testing both directions.
  - Hardened config resolution in `multigravity/cli.py` (`--config` flag -> `MULTIGRAVITY_CONFIG` env -> `./config.local.toml`) with explicit error and zero silent fallback to `config.example.toml`. Active config path is displayed at the top of wizard, status, and doctor output.
  - Implemented dynamic `two-worker collision` test in `multigravity/doctor.py`: reports `[SIMULATED]` when fewer than 2 workers are logged in, and runs `[REAL]` when 2 or more workers are verified (snapshots host keyring and `~/.gemini`, fingerprints worker credential dirs with SHA-256 before/after alternating live probes, checks distinct identities, and verifies zero host modification).
  - Updated `docs/LOGIN_RUNBOOK.md` to match the exact 8-step sequential onboarding flow and documented `ssh-add` as optional for runbook execution.

- **Phase 8 Worker Scaling, Disabled Worker Isolation & TUI Guidance**:
  - Identified cause of ghost worker: `LoginWizard`, `Doctor`, and `cmd_login` iterated over `config.workers` without filtering by `w.enabled`, causing disabled workers (`enabled = false`) to be probed and offered for login.
  - Enforced active configuration as the sole source of truth: removed workers vanish from all commands; disabled workers are displayed as `DISABLED` in the status board, are never probed, never offered for login, and never block wizard completion or daemon launch.
  - Verified 1–3 worker boundary rules: configs with 2 workers pass validation and preflight, and Doctor runs the two-worker collision check as `REAL` as soon as 2 workers are verified.
  - Documented `enabled` flag and 2-worker scaling in `config.example.toml` and `docs/LOGIN_RUNBOOK.md`.
  - Added prominent banner before TUI launch instructing users not to enter prompts and to exit via `/quit` or `Ctrl-D` once signed in.
  - Created `tests/test_worker_scenarios.py` covering 2-worker setups, disabled workers, removed workers with stale state, and daemon startup (51/51 tests passing).
- **Phase 9 Real Worker Adapter Wiring & End-to-End Verification**:
  - Root cause resolved: `CoreDaemon.__init__` previously defaulted unconditionally to `FakeWorkerAdapter()` because `SystemConfig` lacked an `adapter` field. Added `adapter: str = "agy_cli"` in `SystemConfig` with validation (`agy_cli` | `fake`), and updated `CoreDaemon` to instantiate `AgyCliWorkerAdapter` by default unless explicitly configured as `fake`.
  - Documented `adapter` under `[daemon]` in `config.example.toml` and set `adapter = "agy_cli"` in `config.local.toml`.
  - Implemented wrapper-per-task execution in `AgyCliWorkerAdapter`: launches `agy` via `dbus-run-session` + unlocked `gnome-keyring-daemon` in an isolated process group (`start_new_session=True`) with strictly filtered environment (`EnvironmentAllowList.build_worker_env`). Reaps full process group on cancel/timeout.
  - Read-only scratch directory: Each task runs in a fresh disposable scratch directory inside the worker's profile `tmp/` holding only `inputs.json`, removed automatically in a `finally` block upon completion.
  - Unmistakable fake markers: `FakeWorkerAdapter` outputs prepend `[FAKE]` to summaries; `adapter` kind is reported in `list_workers()`, `multigravity status`, daemon startup logs, and `events.jsonl`.
  - Response unwrapping: Added JSON envelope unwrapping in both `AgyCliWorkerAdapter` and `OutputContractValidator.extract_json` to cleanly extract the model's response text from the `agy` CLI envelope.
  - Doctor test adjustment: Updated `_snapshot_dir_mtimes` in `Doctor` to ignore active `antigravity-cli` assistant log directories, ensuring clean `[PASS]` on the real two-worker collision test.
  - End-to-end real task verification: Started daemon and executed real tasks on both `worker-1` (`Account 2`, 13.7s, confidence 1.0) and `worker-2` (`Account 3`, 15.7s, confidence 1.0) with prompt `"five short bullets on how Unix domain sockets differ from TCP sockets"` (max 1200 chars). Both tasks passed contract validation without repair passes.
  - Clean shutdown & leak audit: Verified zero orphan processes, zero leftover scratch directories, and full test suite passing (54/54 tests).

- **Phase 10 Email Scrub, Redaction Layer, History Rewrite & Leak Prevention**:
  - Full inventory audit: Scanned tracked files, untracked/ignored files, local and origin commits, and `~/.multigravity` text files. Origin commits confirmed clean.
  - Working tree scrubbed: Replaced all real/dummy email occurrences in tracked files and tests with `@example.com` / `@example.org` allow-listed domains.
  - Environment logs redacted: Redacted 59 `agy` log files in place under `~/.multigravity/workers/` to masked format (`kr***3@outlook.com`, `mi***7@outlook.com`); keyrings and identity hash stores untouched.
  - Local history rewritten: Created backup branch `backup/pre-email-scrub`; cleanly rewrote commits `origin/main..HEAD` via tree filter, stripping all leaked addresses while preserving logical changes, authors, and timestamps.
  - Redaction layer implemented: Added `redact_emails` regex layer to `StructuredLogger` (sanitizing event types, details, messages, keys, and exceptions) and `OutputContractValidator.save_raw_transcript`.
  - Identity and probe code hardened: `probe_worker_status` and `WorkerProfileManager` return only masked identities and salted hashes, destroying raw strings in memory immediately.
  - Regression tests added: `tests/test_email_leak.py` added covering log event redaction, exception redaction, transcript redaction, identity/probe masking, and repository-wide tracked file scanner. Full test suite passing (60/60 tests).

- **Phase 11 Main Agent Fleet Prompt, Subagent Delegation & `mgy` Launcher (`install.sh`)**:
  - `multigravity/prompts.py`: Dynamic Main Agent prompt generator with live worker fleet awareness, delegation playbook, and explicit instructions to propose worker subagents (`invoke_subagent`) for large/exploratory tasks.
  - `multigravity/launcher.py`: Self-healing preflight runner, stale lock/socket cleaner, profile permission fixer (`0700`), worker authentication checker, and MCP `instructions.md` synchronizer.
  - `scripts/mgy`: Unified bash launcher supporting interactive `agy` launch with fleet prompt, custom user prompts, and pass-through subcommands (`mgy status`, `mgy doctor`, `mgy login`, `mgy stop`).
  - `install.sh`: One-step setup script checking system dependencies, registering `multigravity` MCP in `agy`, writing instructions, symlinking `mgy` to `~/.local/bin/mgy`, and setting shell aliases in `~/.bashrc` / `~/.zshrc`.
  - `tests/test_launcher.py`: Unit tests covering prompt generation, subagent instructions, and auto-healing. Full test suite passing (64/64 tests).

## Current Phase
Phase 11 Complete: Main Agent Fleet Prompt, Subagent Delegation & `mgy` Launcher

- Phase 12: Fleet Process Lifecycle & Clean/Hard Kill Escalation.
  - Implemented `multigravity.cleanup` module to detect and manage background daemon, worker agents, subagents, and isolated keyrings.
  - Configured graceful SIGTERM termination followed by SIGKILL escalation if any worker or daemon process persists past the timeout (default 4.0s).
  - Wired signal and exit traps into `scripts/mgy` to cleanly reap background fleet processes when the main `agy` session finishes or is killed.
  - Enhanced `multigravity.cli stop` (`mgy stop`) to utilize full clean/hard kill escalation.
  - Added unit test suite in `tests/test_cleanup.py` (70/70 tests passing).

## Done
- All bootstrap and Phases 0–12 complete.

## In Progress
- Completed. Awaiting owner instructions.

## Next
- Owner running `mgy` or submitting real tasks via MCP to background workers.

