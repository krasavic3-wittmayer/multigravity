# Multigravity Worker Login Runbook

This runbook guides the owner through registering dedicated background Google AI Pro accounts (**Accounts 2–4**) one by one, keeping them strictly isolated from each other and from the interactive primary account (**Account 1**).

---

## Architecture & Account Mapping

| Worker ID | Target Account | Purpose | Default Status | Isolation Boundary |
| :--- | :--- | :--- | :--- | :--- |
| **Main Agent** | **Account 1** | Interactive primary agent | Always Active | Host user HOME & desktop keyring (`~/.local/share/keyrings/`) |
| `worker-1` | **Account 2** | Background worker 1 | `enabled = true` | Isolated worker root (private D-Bus & keyring) |
| `worker-2` | **Account 3** | Background worker 2 | `enabled = true` | Isolated worker root (private D-Bus & keyring) |
| `worker-3` | **Account 4** | Background worker 3 | `enabled = false` | Isolated worker root (private D-Bus & keyring) |

> [!IMPORTANT]
> **Account 1 is NEVER a worker.** It is strictly reserved for the owner's interactive agent.
> All credentials for workers are stored exclusively in isolated worker keyrings. The owner's desktop keyring is never modified.

> [!NOTE]
> **Running with 2 Workers (Accounts 2 & 3)**:
> Multigravity is fully operational with 1, 2, or 3 background workers:
> - If you have only two worker accounts, set `enabled = false` for `worker-3` in `config.local.toml` (or omit the `[[workers]]` block for `worker-3` entirely).
> - Disabled workers appear as `DISABLED` in status, are never probed, never offered for login, and never block the wizard or daemon.
> - **Adding a third worker later**: Simply set `enabled = true` in `config.local.toml` and run `python3 -m multigravity.cli login worker-3`.

> [!NOTE]
> **SSH agent / `ssh-add` is optional.**
> `ssh-add` is only needed if the owner chooses to push commits to GitHub from their interactive terminal. It is not required for running the login runbook or any `multigravity` CLI commands.

---

## Step-by-Step Login Sequence

### Step 1: Register Main Account (Account 1) Identity
To protect Account 1 against accidental sign-in during worker onboarding, register Account 1's email first:

```bash
python3 -m multigravity.cli set-main-identity
```

**Expected Flow**:
- Enter the Google email address for your interactive Main Account (Account 1).
- The raw email is **never** written to disk, git, or logs.
- Only a salted SHA-256 hash is saved to `state/main_identity.json` (mode `0600`).
- Collision protection against Account 1 is now active.

---

### Step 2: Run Preflight Diagnostics (Doctor)
Run `doctor` to verify system dependencies, isolation tools, path guards, environment filters, and keyring isolation before any logins:

```bash
python3 -m multigravity.cli doctor
```

**Expected Output**:
- Displays active config path (`Config: /path/to/config.local.toml`).
- All binaries (`agy`, `dbus-run-session`, `gnome-keyring`, `secret-tool`, `xdg-open`) report `[PASS] [REAL]`.
- Worker collision check reports `[PASS] [SIMULATED]` (workers not logged in yet).

---

### Step 3: Log In `worker-1` (Account 2)
Log in the first background worker:

```bash
python3 -m multigravity.cli login worker-1
```

> [!IMPORTANT]
> **Antigravity TUI Behavior & How to Exit**:
> 1. Launching `login` will start the Antigravity TUI and open the Google sign-in page in your browser.
> 2. Complete the Google login in your browser using **Account 2** (NOT Account 1).
> 3. **DO NOT enter any prompts or messages into the Antigravity TUI.**
> 4. Once browser sign-in is complete, **close the TUI immediately**: type `/quit` or press `Ctrl-D`.
> 5. As soon as you exit the TUI, the Multigravity wizard will automatically verify the stored credentials and complete worker verification.

---

### Step 4: Check Status
Verify that `worker-1` is recorded and in state `OK`:

```bash
python3 -m multigravity.cli status
```

**Expected Output (with 2-worker setup)**:
```text
Config: .../config.local.toml
==============================================================================
                        MULTIGRAVITY WORKER STATUS
==============================================================================
WORKER (ACCOUNT)           STATE          IDENTITY           VERIFIED
------------------------------------------------------------------------------
worker-1 (Account 2)       OK             ac***2@example.com 2026-10-18 17:45
worker-2 (Account 3)       PENDING        -                  -
worker-3 (Account 4)       DISABLED       -                  -
==============================================================================
```

---

### Step 5: Run Diagnostics Again
Run `doctor` again after the first login:

```bash
python3 -m multigravity.cli doctor
```

**Expected Output**:
- `worker-1` is verified.
- `two-worker collision` check still reports `[SIMULATED]` because fewer than two workers are logged in (`1 < 2`).

---

### Step 6: Log In `worker-2` (Account 3)
Log in the second background worker:

```bash
python3 -m multigravity.cli login worker-2
```

**Instructions**:
1. When prompted, press [Enter] to launch the TUI.
2. In the browser, sign in with **Account 3**.
3. **Do not enter prompts in the TUI.** Once browser login succeeds, exit the TUI with `/quit` or `Ctrl-D`.
4. The wizard verifies `worker-2` as `OK`.
5. *(If you also have Account 4 ready, you can set `enabled = true` on `worker-3` and run `python3 -m multigravity.cli login worker-3`; otherwise leave `worker-3` as `enabled = false`.)*

---

### Step 7: Run Diagnostics (Real Collision & Isolation Verification)
With both `worker-1` and `worker-2` authenticated, run `doctor`:

```bash
python3 -m multigravity.cli doctor
```

**What Happens**:
- Because $\ge 2$ enabled workers are logged in, the `two-worker collision` check automatically transitions to **`[REAL]`**.
- It snapshots the owner's desktop keyring and `~/.gemini`.
- It fingerprints the isolated worker credential stores.
- It alternates live status probes across the workers (`worker-1` then `worker-2`).
- It verifies distinct identity hashes and ensures worker credential hashes remained unchanged.
- It verifies that the owner's desktop keyring and `~/.gemini` were completely untouched.
- Output line shows:
  ```text
  [PASS]  [REAL]       two-worker collision      : Real verification passed: Workers worker-1 and worker-2 have distinct verified identities; zero credential alteration or host keyring leakage
  ```

---

### Step 8: Verify Host Desktop Keyring is Unchanged
Perform a final audit on your host system to confirm zero modification of your personal keyring or interactive agent state:

```bash
# Inspect host desktop keyring directory
ls -la ~/.local/share/keyrings/

# Inspect host gemini directory
ls -la ~/.gemini/
```

Confirm that no new files were created and modification timestamps match your interactive desktop session.

---

## Operating the Background Daemon

### Starting the Daemon
Once all enabled workers are verified:
```bash
python3 -m multigravity.cli
```
The daemon starts in the background and listens on the configured socket for task dispatch from the interactive agent (Account 1). Disabled workers are never dispatched to.

### Stopping the Daemon Cleanly
```bash
python3 -m multigravity.cli stop
```
Outputs:
```text
[✓] multigravity daemon stopped cleanly.
```

---

## Troubleshooting & FAQ

### Q: How do I add worker-3 later?
1. In `config.local.toml`, set `enabled = true` in the `[[workers]]` block for `worker-3`.
2. Run `python3 -m multigravity.cli login worker-3`.
3. Sign in to Account 4 and exit the TUI with `/quit`.
4. Run `python3 -m multigravity.cli status` to verify all 3 workers are `OK`.

### Q: Why does the login drop me into an Antigravity TUI?
Google Antigravity does not have a headless OAuth login command; initiating authentication requires running the interactive CLI. When it starts, it automatically triggers your browser for OAuth consent. Once Google says authentication is complete, you simply exit the TUI (`/quit` or `Ctrl-D`) so Multigravity can proceed.

### Q: What if I accidentally press Ctrl-C during login?
The login runner reaps the private D-Bus session and child processes cleanly. The worker profile remains `PENDING`, and your desktop keyring is untouched. You can re-run `python3 -m multigravity.cli login <worker-id>`.

### Q: How do I re-authenticate an expired worker?
Run `python3 -m multigravity.cli login <worker-id>`. The wizard updates the isolated credentials in that worker's private keyring without affecting any other worker or Account 1.
