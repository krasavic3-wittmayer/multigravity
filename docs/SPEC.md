Bootstrap: create and publish the repository (do this first, before section 0)
Create the project repository and publish it before writing any system code.
• Name and location: multigravity, at ~/Projects/multigravity. Create ~/Projects if it is missing. If ~/Projects/multigravity already exists and is not empty, stop and report instead of touching it.
• Initialize with git init -b main. Use the git identity already configured on the machine; do not change global git config.
• Initial commit contains only (plus AGENTS.md, specified below): README.md (one short paragraph on what the project is and that it is at the spec stage), .gitignore (Python artifacts, virtualenvs, .env, worker profile directories, journal and log directories, local config holding paths or secrets, .worktrees/), and docs/SPEC.md holding this entire document verbatim. No license file yet; the owner decides that later.
• Create the GitHub repository with the gh CLI under the currently authenticated user (check gh auth status), private by default, and push main with the initial commit, e.g. gh repo create multigravity --private --source . --remote origin --push.
• Never write credentials or tokens into any file, log or commit. If gh is missing or not authenticated, stop and tell the user exactly what to run (for example gh auth login); do not try to handle credentials yourself.
• Verify: git status is clean, origin/main matches local main, and gh repo view shows the remote repository. Report the repository URL and the commit hash.
AGENTS.md (create it before the initial commit)
Put general rules for any coding agent working in this repository into AGENTS.md: short, imperative, no filler. It must contain at least:
• Do not run git commit, git push, git tag, or any other command that records or publishes changes. Leave all changes uncommitted in the working tree for the owner to review. The only exception is the one-time bootstrap above.
• Do not rewrite history, force-push, or change remotes or git config.
• Never create, print, or commit credentials or tokens. Worker profile directories hold account credentials: never open their contents, never copy them, and keep them out of the repository.
• Stay inside the repository and the scope of the task. Ask before adding a dependency, deleting files, or touching the boundaries in section 2 (no token tracking, no scaling beyond 4 accounts, no extra infrastructure).
• Treat docs/SPEC.md as the source of truth. If the code has to deviate from it, say so explicitly instead of diverging silently.
• Keep the codebase small, run the test suite before calling a task done, and finish with a short report: what changed, what was tested, what is still open.
The owner may relax the commit and push rules later; until then, follow AGENTS.md exactly as written.
Only after this is done, continue with section 0 and the phases below. All later work happens in this repository and stays uncommitted for review, per AGENTS.md.
0. Your role and how to work
You are the lead engineer for a small local system, described below. Work in phases (section 11). Do not skip ahead: Phase 0 is an investigation that gates the real worker adapter, and everything else can be built and tested against a mock adapter first. State your assumptions explicitly, keep questions to a minimum, and end each phase with a short report (what exists, what was tested, what is open). Prefer boring, small, auditable code. Justify every third-party dependency in one line.
1. What we are building
A lightweight local orchestration layer between one interactive main agent (Account 1) and 1-3 background sub-agents (Accounts 2-4), each running on a separate paid Google AI Pro account (Antigravity-based, exact control method TBD in Phase 0).
The main agent delegates heavy, read-mostly work (research, reading tens of KB of docs, test generation, review) to idle sub-agents through a middleware. The sub-agent burns the raw material on its own account and returns only a compressed, schema-bound result. The main agent's context stays clean and it never blocks on long tasks.
2. Hard boundaries (do not violate)
• Strictly 2-4 accounts total. No scaling to dozens, no autoscaling, no multi-host.
• Do NOT track tokens: no counting, no budgets, no token-bucket, no burn-down. Daily token caps are not a practical constraint here.
• The real constraints to design for: (1) per-account concurrency / RPM limits, (2) wall-clock time (parallelize heavy tasks), (3) context bloat in the main agent.
• No message broker, no external DB server, no web UI, no metrics stack, no plugin framework beyond the worker adapter interface.
• Use only officially supported interfaces and the accounts' own legitimate credentials. Do not reverse-engineer private endpoints, spoof clients, or work around authentication or rate limiting. If Phase 0 shows the only available path is unsupported, stop and report it instead of building it.
• Account 1 is reserved for the interactive main agent and is never used as a worker.
3. Architecture
Two pieces, deliberately split so that the MCP client killing or restarting its server does not kill running work:
1. Core daemon (long-lived, per-user, Linux). Owns the task queue, dispatcher, worker registry and state machines, adapters, journal, and logs.
2. MCP shim (thin, stateless). Spawned by the main agent's MCP client (stdio by default), translates MCP tool calls to the daemon over a Unix domain socket (in $XDG_RUNTIME_DIR, mode 0600). If the main agent only supports remote MCP over HTTP, expose streamable HTTP bound to 127.0.0.1 with a random bearer token instead. Pick based on Phase 0 findings and document why.
Default stack unless Phase 0 gives a reason otherwise: Python 3.12+, asyncio, the official MCP Python SDK, SQLite or an append-only JSON-lines journal for restart recovery, a single TOML config file.
Core components: task store, dispatcher, worker registry (state machine), worker adapter interface, output contract validator, journal/recovery, structured logger.
4. MCP tool surface (what the main agent sees)
Every tool returns fast (target under 1 s). Nothing blocks on a long task.
• submit_task(kind, instruction, inputs, output_spec, max_output_chars, mode) returns task_id. kind is one of research, doc_digest, test_gen, review, custom. mode is read_only (default) or worktree.
• get_status(task_ids) returns per-task state, attempt number, worker label, timestamps.
• get_result(task_id) returns the compressed result only (see section 7).
• wait(task_ids, timeout_s) blocks until any/all finished or the timeout hits; hard-cap the timeout at about 25 s so client-side tool-call timeouts are never reached. The main agent loops.
• cancel_task(task_id).
• list_workers() is diagnostic: id, state (IDLE / BUSY / COOLDOWN / AUTH_EXPIRED / DISABLED), cooldown_until.
If the client supports progress or resource notifications you may add them as an optimization, but correctness must never depend on them.
Errors: the main agent must never see a raw 429 or any provider error. Terminal errors are structured (code, message, retryable), e.g. NO_WORKER_AVAILABLE (queue deadline exceeded), TASK_FAILED, OUTPUT_CONTRACT_VIOLATION, CANCELLED.
5. Task model and dispatcher
States: QUEUED, RUNNING, then DONE, FAILED or CANCELLED. Track the attempt count and the list of workers already tried.
Tasks must be idempotent and restartable from zero (workers share no state). Default mode is read_only: the worker returns text or a diff and never writes to the project. Optional worktree mode: the worker writes only inside a dedicated git worktree created per task; the middleware returns a clean diff and the main agent decides whether to apply it. Never allow workers to write to the shared working tree.
Dispatcher: FIFO queue; assign the head task to a worker in state IDLE; one task per worker at a time by default (configurable per worker); pick the least-recently-used idle worker. Keep it simple: no priorities beyond FIFO unless trivially cheap.
6. Failover and throttling
• On HTTP 429 or an equivalent rate/concurrency signal from a worker: mark it COOLDOWN, honor Retry-After if present, otherwise use exponential backoff with jitter (configurable base and cap), then return it to IDLE (optionally after a cheap probe).
• The in-flight task is transparently re-queued at the front with attempt+1, excluding the failed worker for that retry. The main agent sees at most QUEUED or RUNNING.
• Distinguish error classes: rate limit (cooldown + retry), auth/session failure (AUTH_EXPIRED, no retry on that worker), transient network/timeout (limited retries), deterministic task failure (do not retry endlessly; fail after max_attempts).
• If all workers are in COOLDOWN or unavailable, tasks wait in the queue up to a configurable queue_deadline, then fail with NO_WORKER_AVAILABLE. No silent infinite loops, no thundering herd when cooldowns end.
• Per-task wall-clock timeout; a hung worker is killed and handled like a transient failure.
7. Context funnel (output contract)
This is the point of the whole system. Every worker run is wrapped in a middleware-owned prompt template that forces a fixed result schema, for example: summary, key_findings[], interface_spec or diff (when relevant), sources[], confidence, open_questions[]. Hard character cap from max_output_chars, with a sane global default.
The middleware validates output against the schema and the cap. On violation: one automatic repair/compress pass, then fail with OUTPUT_CONTRACT_VIOLATION. Raw worker transcripts and full outputs are stored on disk for debugging and never returned to the main agent. Never forward raw documentation, logs, or reasoning traces.
8. Process isolation on Linux
• Every worker has its own isolated profile: separate HOME and XDG_CONFIG_HOME / XDG_DATA_HOME / XDG_CACHE_HOME / XDG_STATE_HOME directories, separate credential storage, own working directory. Directories are 0700. Credentials and tokens are never logged.
• Spawn workers in their own session/process group. On daemon shutdown or crash there must be no orphans (kill the process group, use PR_SET_PDEATHSIG or equivalent), children are reaped, no zombies.
• No shared writable state between workers except through the daemon.
• On daemon restart: recover from the journal, mark previously RUNNING tasks as QUEUED (attempt+1), and clean up stale worker processes and worktrees.
• Runs as a plain user process; an optional systemd user unit is a nice-to-have, not required.
9. Auth and session handling
Expect session expiry on headless workers to be the most fragile part. Detect it reliably, move the worker to AUTH_EXPIRED, surface it in list_workers and logs, and keep the system running on the remaining workers. Never attempt interactive login automatically. Provide a small documented command that re-authenticates one named worker profile interactively (when the user is at the machine). Use only refresh mechanisms that the official tooling supports.
10. Config, logging, observability
One TOML file: workers (id, label, profile_dir, enabled, concurrency), cooldown base/cap, queue_deadline, task timeout, max_attempts, default max_output_chars, log and journal directories. Structured JSON-lines logs with a per-task event timeline (submitted, assigned, retried, cooled-down, done). That is all the observability needed.
11. Phases and deliverables
Phase 0: Investigation (gates Phase 3). The user will run this part on a laptop that has the real accounts. Write small, safe probe scripts plus a checklist, and produce a short report answering: (a) how can Antigravity instances (or an officially supported alternative such as a headless CLI or API) be driven programmatically, and with which auth model; (b) does the main agent's client support MCP, over which transport, with what tool-call timeouts and notification support; (c) how do rate limits (429) actually present themselves; (d) how does session expiry present itself. Recommend one adapter approach plus a fallback.
Phase 1: Core with a mock adapter. Task store, dispatcher, worker state machines, journal/recovery, contract validator, config, logging. Include a FakeWorker adapter that can inject latency, 429s, auth failures, hangs and malformed output.
Phase 2: MCP shim and daemon IPC. The tool surface from section 4, end to end, testable with a scripted MCP client.
Phase 3: Real adapter. Implement the worker adapter chosen in Phase 0 behind the same interface.
Phase 4: Hardening and docs. README with install and run instructions, example config, troubleshooting, and a short architecture note.
12. Acceptance criteria
• With 3 mock workers, 10 submitted tasks complete in roughly the parallel time, not the serial time.
• Injecting 429 on a busy worker: the task finishes on another worker, the main-agent-visible API returns no error, and the failed worker re-enters rotation after cooldown.
• All workers in cooldown: tasks wait, then fail cleanly with NO_WORKER_AVAILABLE after the deadline; none are lost or looped.
• Kill the MCP shim mid-task: tasks continue and a new shim can fetch results. Kill the daemon mid-task: after restart the task is re-queued and completed, with no orphan processes.
• Oversized or malformed worker output never reaches the main agent.
• wait never exceeds its timeout cap.
• Unit and integration tests cover the above and run with a single command.
13. Working rules
Keep the codebase small and readable; no speculative features from the excluded list in section 2. Do not commit or push (see AGENTS.md); leave changes in the working tree for review. When something in this spec is ambiguous or conflicts with what Phase 0 finds, state the conflict, propose the smallest change, and continue with that unless it touches section 2.
