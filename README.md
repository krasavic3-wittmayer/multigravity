# multigravity

**One agent wasn't enough. So I got four.**

A local AI agent orchestrator. Python, Linux, zero third-party dependencies, questionable life choices.

- I use Arch, btw.
- This is all vibecoded, btw. I haven't touched the code at all.
- Yes, I made AI agents build a system for coordinating AI agents. No, I don't see a problem here.

Research, documentation, tests, code reviews — delegate the boring stuff, keep the main agent's context clean, and let the workers cook.

**100% vibes. 0% manually written code.**

## Architecture (I use Arch, btw)

```text
                   Main Agent
                       |
                  "do the thing"
                       |
                    MCP shim
                       |
                   Unix socket
                       |
               multigravity daemon
                 /      |      \
                /       |       \
           Worker 1  Worker 2  Worker 3
              |         |         |
           Account 2 Account 3 Account 4

        4 accounts. 1 questionable life choice.
```

The architecture is split into two processes, because apparently even my AI agents need process isolation.

- **`multigravity-daemon`** — queues tasks, manages workers, handles cooldowns, recovers after crashes, and generally makes sure the entire operation doesn't collapse because one subprocess had a bad day.
- **`multigravity-mcp-shim`** — a thin, stateless bridge between the main agent and the daemon. Kill it. Restart it. Blame it for your problems. The background jobs will keep running anyway.

The daemon handles the actual work. The shim just delivers messages.

Separation of concerns? More like separation of things that can break.

For the detailed subsystem architecture diagram, socket permission requirements, and failure isolation boundaries, see the [Technical Reference Manual](docs/TECHNICAL_REFERENCE.md).

## Why does this exist?

Because apparently having an AI agent write code wasn't complicated enough. Now we need an AI agent to tell other AI agents to read documentation so the first AI agent doesn't have to.

And honestly? It makes sense.

- **Parallel work.** Three workers can handle independent tasks while the main agent works on the actual project.
- **Less context pollution.** Workers return compact, structured results instead of their entire thought process and a small novel about the contents of `README.md`.
- **Crash recovery.** The daemon keeps track of unfinished tasks. The MCP client can disappear without taking the entire workforce with it.
- **Rate-limit handling.** Workers cool down when they hit HTTP 429s. Eligible tasks can be retried elsewhere.
- **Local-first.** Everything runs on your machine. No Redis, no PostgreSQL, no Kubernetes cluster that costs more to maintain than the project deserves.

The bottleneck isn't always intelligence. Sometimes it's just one agent trying to do twelve things at once.

## Design principles (yes, I have those)

- **One main account, up to three worker accounts.** Four accounts total. This is an orchestrator, not a cryptocurrency mining operation.
- **Zero third-party Python dependencies.** If the standard library can do it, it probably should. `pip install -r requirements.txt` would be embarrassing when there is no requirements file.
- **No token accounting.** No token burn-down charts, no budget dashboards, no pretending that a graph makes the model cheaper. Schedule around concurrency, rate limits, and time.
- **Official interfaces only.** Workers use the official `agy` CLI. No reverse-engineered endpoints, no API cosplay.
- **Linux-native.** Unix sockets, isolated D-Bus sessions, private keyrings. Windows users can enjoy their own adventures.
- **No web UI.** It's a local daemon, not a SaaS startup looking for its first round of funding.

Keep the implementation small. Keep the agents busy. Keep the yak shaving under control.

*(The yak is currently being shaved by another agent.)*

## Quick start

```bash
# Clone the repo, obviously
git clone <repo-url>
cd multigravity

# Install the thing
./install.sh

# Launch the agent fleet
mgy

# Or give the main agent a task
mgy "analyze the dependencies in this project"

# Observe the consequences of your actions
mgy status
mgy doctor

# Enough distributed systems for today
mgy stop
```

`mgy` is the unified launcher. It handles preflight checks, worker login verification, daemon startup, and injecting the main agent with the instructions needed to delegate work.

Basically, `mgy` does the boring setup so you can focus on making the AI do your homework. Professionally, of course.

## Setup

### Requirements

- Linux (x86_64 or aarch64)
- Python 3.12+ (3.14 recommended, because apparently even Python needs rolling releases)
- Google Antigravity CLI at `/usr/sbin/agy`
- `dbus-run-session`
- `gnome-keyring-daemon`
- `secret-tool`

If you run Arch, most of this should feel familiar. If something breaks, congratulations: you have discovered a new feature.

### Configure workers

```bash
cp config.example.toml config.toml
```

Configure one to three worker profiles in `config.toml`. The main agent gets its own account; the workers get theirs.

Yes, you actually need separate accounts. No, spawning three terminal windows does not magically give you three independent agents.

### Doctor, doctor

```bash
python3 -m multigravity.cli doctor
```

Checks isolation, path guards, environment filtering, and other things that would be significantly less fun to debug after something goes wrong.

Run it before blaming the kernel.

### Log in

```bash
# Interactive login wizard
python3 -m multigravity.cli

# Or authenticate a specific worker
python3 -m multigravity.cli login worker-1
```

Each worker gets an isolated session and keyring. Duplicate account logins are rejected.

Because sharing credentials between supposedly isolated workers would kind of defeat the entire point of isolating them.

### Connect MCP

```bash
agy mcp add multigravity python3 -m multigravity.mcp_shim
```

Or just use `mgy` and let it handle the usual startup routine.

## What does the main agent actually get?

Six tools. No unnecessarily elaborate dashboard.

| Tool | Purpose |
|---|---|
| `submit_task` | Submit a task and get an ID immediately. |
| `get_status` | Check whether the task is queued, running, or finished. |
| `get_result` | Get the useful bits without the transcript-shaped context bomb. |
| `wait` | Wait for completion, capped at 25 seconds. |
| `cancel_task` | Cancel a task and terminate its worker process group. |
| `list_workers` | Inspect worker health, cooldowns, and current state. |

Worker results follow a strict schema, with a character limit and one automatic repair/compression pass when needed.

The main agent gets the answer, not 40 KB of an agent talking to itself.

Raw transcripts stay on disk for debugging. Your context window has suffered enough.

For full parameter signatures, data types, and default values, see the [MCP Tool Surface Specification](docs/TECHNICAL_REFERENCE.md#5-mcp-tool-surface-detailed-specification).

## Under the hood

The daemon manages:

- A FIFO task queue and worker state machines.
- Worker cooldowns and rate-limit recovery.
- A persistent journal for crash recovery.
- Structured event logs.
- Unix socket communication with restricted permissions.
- Output validation and compression.

The MCP shim is deliberately stateless. Killing it doesn't kill the daemon's jobs.

If the daemon itself crashes, unfinished tasks are recovered from the journal on restart. Because `kill -9` is not a valid task scheduler.

## Testing

```bash
python3 -m unittest discover tests
```

The acceptance tests cover:

- Parallel execution across mock workers.
- HTTP 429 handling and worker failover.
- Queue deadlines when nobody is available.
- Daemon and shim crashes.
- Recovery of unfinished tasks.
- Output contract enforcement.
- MCP timeout protection.

Mock workers are used where appropriate. A successful mock test is not proof that an external provider will behave perfectly in production. We live in a society.

For acceptance benchmark metrics and criteria details, see [docs/TECHNICAL_REFERENCE.md](docs/TECHNICAL_REFERENCE.md#8-verified-acceptance-criteria--benchmark-metrics).

## Troubleshooting

**`AUTH_EXPIRED`**

Your worker needs to log in again.

```bash
./scripts/multigravity-auth <profile_path>
```

**`NO_WORKER_AVAILABLE`**

All workers are disabled, unauthenticated, or cooling down. Check worker states and inspect `logs/events.jsonl`.

Alternatively, all your agents have collectively decided to take a break. Check the logs before filing a bug against the laws of physics.

**Daemon socket connection refused**

```bash
python3 -m multigravity.cli status
```

Check that the daemon is running and that `$XDG_RUNTIME_DIR/multigravity.sock` exists with permissions `0600`.

If it doesn't, the daemon might be dead. Restarting the MCP client for the seventh time is unlikely to resurrect it.

See [Troubleshooting & Recovery](docs/TECHNICAL_REFERENCE.md#9-technical-troubleshooting--recovery) for detailed failure modes.

## Documentation

- [Technical Reference Manual](docs/TECHNICAL_REFERENCE.md) — complete parameter signatures, JSON contract schemas, state machine definitions, and security isolation mechanics.
- [Specification](docs/SPEC.md) — the foundational design specification and project boundaries.
- [Login Runbook](docs/LOGIN_RUNBOOK.md) — operator runbook for worker profile onboarding and keyrings.
- [Login Isolation Guide](docs/LOGIN_ISOLATION.md) — Linux desktop keyring & D-Bus isolation details.

## Philosophy

Why build one complicated system when you can build two simple processes and make them argue over a Unix socket?

Why read the docs when you can delegate reading the docs?

Why use one agent when you can introduce scheduling, concurrency, persistent state, crash recovery, and an entirely new class of bugs?

Because **overengineering is just engineering with better marketing.**

And because one agent wasn't enough.

---

*Built on Linux. Tested with mocks. Maintained with questionable amounts of caffeine.*

*I use Arch, btw.*

---

## Author's note

- OS: Arch Linux, btw.
- Code: vibecoded.
- Manual coding: none.
- Number of AI agents involved in making AI agents work together: yes.
- Number of layers of abstraction between me and the actual code: a concerning amount.

If something breaks, it's probably a skill issue.

Just not necessarily mine.

*I use Arch, btw.*
