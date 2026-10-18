# Rules for Coding Agents

These rules apply to any coding agent working in this repository. Follow them strictly.

1. **Git commit allowed**: `git commit` is allowed at your own judgment whenever a coherent unit of work is complete and verified. Stage explicit paths, write short imperative subject lines, and keep commits small and logical. Do not wait for approval.
2. **Pushing and remote changes forbidden**: `git push`, force-push, tag creation, remote changes, and git config changes remain forbidden. Never rewrite commits that already exist on origin (the initial bootstrap and pushed commits). You may amend or reorder your own local, unpushed commits.
3. **Never touch credentials**: Never create, print, or commit credentials or tokens. Worker profile directories hold account credentials: never open their contents, never copy them, and keep them out of the repository.
4. **Stay within scope and boundaries**: Stay inside the repository and the scope of the task. Ask before adding a dependency, deleting files, or touching the boundaries in section 2 of `docs/SPEC.md` (no token tracking, no scaling beyond 4 accounts, no extra infrastructure).
5. **Specification is truth**: Treat `docs/SPEC.md` as the source of truth. If the code has to deviate from it, state the deviation explicitly instead of diverging silently.
6. **Code quality and reporting**: Keep the codebase small, run the test suite before calling a task done, and finish with a short report: what changed, what was tested, what is still open.
7. **Git remote and SSH**: The remote uses SSH and stays that way. Network git commands always use `GIT_SSH_COMMAND='ssh -o BatchMode=yes'` so they fail fast instead of hanging. Pushing remains forbidden.

Until the repository owner explicitly relaxes these rules, follow them as written.


