# Multigravity Worker Login Runbook

This runbook guides the owner through registering the dedicated background Google AI Pro accounts (**Accounts 2–4**) one by one, keeping them strictly isolated from each other and from the interactive primary account (**Account 1**).

---

## Architecture & Account Mapping

| Worker ID | Target Account | Purpose | Isolation Boundary |
| :--- | :--- | :--- | :--- |
| **Main Agent** | **Account 1** | Interactive primary account | Host user HOME & desktop keyring (`~/.local/share/keyrings/`) |
| `worker-1` | **Account 2** | Background worker 1 | Isolated worker root (private D-Bus & keyring) |
| `worker-2` | **Account 3** | Background worker 2 | Isolated worker root (private D-Bus & keyring) |
| `worker-3` | **Account 4** | Background worker 3 | Isolated worker root (private D-Bus & keyring) |

> [!IMPORTANT]
> **Account 1 is NEVER a worker.** It is strictly reserved for the owner's interactive agent.
> All credentials for workers are stored exclusively in isolated worker keyrings. The owner's desktop keyring is never modified.

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
Log in the first background worker individually:

```bash
python3 -m multigravity.cli login worker-1
```

**Instructions**:
1. Terminal will show: `Now logging in: worker-1 (Account 2)`
2. Press [Enter] to begin.
3. In the browser window:
   - Ensure you are logging into **Account 2** (NOT Account 1).
   - If auto-filled or auto-selected, click **"Use another account"**.
   - Complete Google login and approve OAuth consent.
4. If Account 1 was accidentally selected, the collision detector immediately rejects it and prompts you to wipe the profile and retry.

---

### Step 4: Check Status
Verify that `worker-1` is recorded and in state `OK`:

```bash
python3 -m multigravity.cli status
```

**Expected Output**:
```text
Config: .../config.local.toml
==============================================================================
                        MULTIGRAVITY WORKER STATUS
==============================================================================
WORKER (ACCOUNT)           STATE          IDENTITY           VERIFIED
------------------------------------------------------------------------------
worker-1 (Account 2)       OK             ac***2@example.com   ...
worker-2 (Account 3)       PENDING        -                  -
worker-3 (Account 4)       PENDING        -                  -
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

### Step 6: Log In `worker-2` (Account 3) and `worker-3` (Account 4)
Log in the remaining two background workers sequentially:

```bash
# Log in worker-2
python3 -m multigravity.cli login worker-2

# Log in worker-3
python3 -m multigravity.cli login worker-3
```

*(Or simply run `python3 -m multigravity.cli` without arguments to step through all pending workers in the interactive wizard.)*

For each worker:
- Verify that **Account 3** is used for `worker-2`.
- Verify that **Account 4** is used for `worker-3`.
- Collision detection guarantees no two workers share the same account, and none use Account 1.

---

### Step 7: Run Diagnostics (Real Collision & Isolation Verification)
With two or more workers authenticated, run `doctor`:

```bash
python3 -m multigravity.cli doctor
```

**What Happens**:
- Because $\ge 2$ workers are logged in, the `two-worker collision` check automatically transitions to **`[REAL]`**.
- It snapshots the owner's desktop keyring and `~/.gemini`.
- It fingerprints the isolated worker credential stores.
- It alternates live status probes across the workers (`worker-1` then `worker-2`).
- It verifies distinct identity hashes and ensures worker credential hashes remained unchanged.
- It verifies that the owner's desktop keyring and `~/.gemini` were completely untouched.
- Output line shows:
  ```text
  [PASS]  [REAL]       two-worker collision      : Real multi-worker credential isolation and distinct identity verification PASSED
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
Once all workers are verified:
```bash
python3 -m multigravity.cli
```
The daemon starts in the background and listens on the configured socket for task dispatch from the interactive agent (Account 1).

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

### Q: What if I accidentally press Ctrl-C during login?
The login runner reaps the private D-Bus session and child processes cleanly. The worker profile remains `PENDING`, and your desktop keyring is untouched. You can re-run `python3 -m multigravity.cli login <worker-id>`.

### Q: How do I re-authenticate an expired worker?
Run `python3 -m multigravity.cli login <worker-id>`. The wizard updates the isolated credentials in that worker's private keyring without affecting any other worker or Account 1.

### Q: Where are worker credentials stored?
Under each worker's isolated profile directory defined in `config.local.toml` (e.g. `~/.multigravity/workers/worker-N/data/keyrings/`). Each worker has its own dedicated D-Bus session bus and its own private `gnome-keyring-daemon`.
