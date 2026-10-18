# multigravity

multigravity is a lightweight local orchestration layer between one interactive main agent and 1–3 background sub-agents running on separate paid Google AI Pro accounts (Antigravity-based). The system offloads heavy, read-mostly tasks (research, doc digestion, test generation, and review) to idle sub-agents through a local daemon and MCP shim, returning only compressed, schema-bound results to keep the main agent's context clean and responsive. The project is currently at the specification and investigation stage.
