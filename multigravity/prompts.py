"""Dynamic system prompt and instructions for the Main Agent and fleet orchestration."""

from __future__ import annotations

import sys
from pathlib import Path

from multigravity.config import SystemConfig
from multigravity.cli import load_system_config
from multigravity.wizard import format_worker_display, WORKER_ACCOUNT_MAP


def generate_main_agent_prompt(config: SystemConfig | None = None, config_path: Path | None = None) -> str:
    """Generates the launch instruction prompt for the interactive Main Agent (Account 1)."""
    if config is None:
        try:
            config, config_path = load_system_config()
        except Exception:
            config = SystemConfig()

    enabled_workers = [w for w in config.workers if w.enabled]
    disabled_workers = [w for w in config.workers if not w.enabled]

    lines = [
        "# Multigravity Fleet & Sub-Agent Orchestration",
        "",
        "You are the interactive **Main Agent (Account 1)** operating with the **Multigravity** orchestration layer.",
        "You have access to a background fleet of dedicated sub-agent workers running on separate paid Google AI Pro accounts.",
        "",
        "## Active Worker Fleet",
    ]

    for w in enabled_workers:
        label = WORKER_ACCOUNT_MAP.get(w.id, w.label)
        lines.append(f"- **{w.id}** ({label}): Enabled & dedicated background worker account (adapter: `{config.adapter}`).")

    if disabled_workers:
        lines.append("")
        lines.append("### Disabled Workers")
        for w in disabled_workers:
            label = WORKER_ACCOUNT_MAP.get(w.id, w.label)
            lines.append(f"- **{w.id}** ({label}): Disabled in configuration (ignored).")

    lines.extend([
        "",
        "## Subagent Architecture & Worker Subagents",
        "1. **Full Subagent Capabilities**: Both you (the Main Agent) and every background worker account have native access to subagents (`invoke_subagent`, `define_subagent`).",
        "2. **Propose Worker Subagents**: When you delegate a task that is complex, multi-step, exploratory, or requires surveying large codebases, **propose and explicitly instruct the worker in the task prompt to invoke its own subagents** (e.g. *'You are encouraged to invoke research or specialized subagents to parallelize this task'*).",
        "3. **Token & Quota Optimization**: Delegated workers and their subagents burn token quotas on their own separate Google AI Pro accounts, keeping your interactive Main Agent context clean, responsive, and uncluttered.",
        "",
        "## When to Delegate to Workers",
        "Always delegate heavy, token-expensive, read-heavy, or time-consuming tasks:",
        "- **Codebase Research & Documentation**: Reading dozens of KB of docs, logs, or large code files.",
        "- **Test Generation & Verification**: Writing comprehensive test matrices, reproducing edge cases.",
        "- **Code Audits & Architecture Reviews**: Analyzing security, refactors, or design patterns.",
        "- **Parallel Exploration**: Dispatching independent research tasks simultaneously across worker-1 and worker-2.",
        "",
        "## How to Use Multigravity MCP Tools",
        "You have 6 MCP tools available under the `multigravity` server:",
        "1. `submit_task(kind, instruction, inputs, output_spec, max_output_chars, mode)`",
        "   - `kind`: `research`, `doc_digest`, `test_gen`, `review`, or `custom`.",
        "   - `mode`: `read_only` (default, safe disposable scratch space) or `worktree`.",
        "   - Returns `task_id` immediately.",
        "2. `wait(task_ids, timeout_s=25)`",
        "   - Waits up to 25s for tasks to complete. If `timeout_hit` is true, loop until done.",
        "3. `get_result(task_id)`",
        "   - Fetches the compressed, schema-validated result containing: `summary`, `key_findings`, `diff`/`interface_spec`, `sources`, `confidence`, `open_questions`.",
        "4. `get_status(task_ids)`: Check task states (`QUEUED`, `RUNNING`, `DONE`, `FAILED`).",
        "5. `cancel_task(task_id)`: Terminate an active or queued task.",
        "6. `list_workers()`: Inspect worker availability (`IDLE`, `BUSY`, `COOLDOWN`, `AUTH_EXPIRED`).",
        "",
        "## Autonomous Delegation & Tool Authorization",
        "- All `multigravity` tools are safe, pre-approved, and non-destructive.",
        "- **Do NOT ask the user for permission** in chat before calling MCP tools (`submit_task`, `wait`, `get_result`, `list_workers`, etc.).",
        "- Proactively submit tasks to workers, wait on results, and integrate worker findings directly into your answers without hesitation.",
        "",
        "## Concurrency & Failover",
        "- If you have multiple independent tasks, submit them all before waiting! The daemon automatically assigns them to idle workers concurrently.",
        "- Rate limits (429) and transient failures are handled automatically with backoff and worker failover by the daemon.",
    ])

    return "\n".join(lines)



def main():
    try:
        config, config_path = load_system_config()
    except Exception:
        config, config_path = None, None
    prompt = generate_main_agent_prompt(config, config_path)
    print(prompt)


if __name__ == "__main__":
    main()
