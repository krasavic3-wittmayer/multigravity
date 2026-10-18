# Rules for Coding Agents

These rules apply to any coding agent working in this repository. Follow them strictly.

1. **Do not record or publish changes**: Do not run `git commit`, `git push`, `git tag`, or any other command that records or publishes changes. Leave all changes uncommitted in the working tree for the owner to review. The only exception was the initial bootstrap.
2. **Do not tamper with git history or config**: Do not rewrite history, force-push, or change remotes or git config.
3. **Never touch credentials**: Never create, print, or commit credentials or tokens. Worker profile directories hold account credentials: never open their contents, never copy them, and keep them out of the repository.
4. **Stay within scope and boundaries**: Stay inside the repository and the scope of the task. Ask before adding a dependency, deleting files, or touching the boundaries in section 2 of `docs/SPEC.md` (no token tracking, no scaling beyond 4 accounts, no extra infrastructure).
5. **Specification is truth**: Treat `docs/SPEC.md` as the source of truth. If the code has to deviate from it, state the deviation explicitly instead of diverging silently.
6. **Code quality and reporting**: Keep the codebase small, run the test suite before calling a task done, and finish with a short report: what changed, what was tested, what is still open.

Until the repository owner explicitly relaxes these rules, follow them as written.
