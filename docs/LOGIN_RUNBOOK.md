# Multigravity Worker Login Runbook

This runbook guides the owner through registering the dedicated background Google AI Pro accounts (**Accounts 2–4**) one by one, keeping them strictly isolated from each other and from the interactive primary account (**Account 1**).

---

## Architecture & Account Mapping

| Worker ID | Target Account | Purpose | Isolation Boundary |
| :--- | :--- | :--- | :--- |
| **Main Agent** | **Account 1** | Interactive primary account | Real user HOME & desktop keyring (`~/.local/share/keyrings/`) |
| `worker-1` | **Account 2** | Background worker 1 | `~/.multigravity/workers/worker-1` (private D-Bus & keyring) |
| `worker-2` | **Account 3** | Background worker 2 | `~/.multigravity/workers/worker-2` (private D-Bus & keyring) |
| `worker-3` | **Account 4** | Background worker 3 | `~/.multigravity/workers/worker-3` (private D-Bus & keyring) |

> [!IMPORTANT]
> **Account 1 is NEVER a worker.** It is strictly reserved for the owner's interactive agent.
> All credentials for workers are stored exclusively in isolated worker keyrings. The owner's desktop keyring is never modified.

---

## Step 0: Dependency Preflight & SSH Agent Check

### 1. Verify Arch Linux Packages
Ensure required system isolation utilities are installed:
```bash
sudo pacman -S dbus gnome-keyring libsecret xdg-utils
```

### 2. Check SSH Agent (for GitHub sync)
When working with git remotes over SSH, check if your SSH key is loaded into your agent:
```bash
ssh-add -l
```
If this prints `Could not open a connection to your authentication agent` or `The agent has no identities`, run in your interactive terminal:
```bash
eval $(ssh-agent)
ssh-add
```

---

## Step 1: Register Main Account (Account 1) Identity

To protect Account 1 against accidental sign-in during worker onboarding, register Account 1's email first.

```bash
python3 -m multigravity.cli set-main-identity
```

### What you will see:
```text
=== Multigravity: Register Main Account (Account 1) Identity ===
Enter the Google email address for your interactive Main Account (Account 1).
NOTE: The email itself is NEVER saved, logged, or sent anywhere.
Only a salted SHA-256 hash will be stored in ~/.multigravity/state/main_identity.json (mode 0600).

Account 1 Google email address: owner.main@example.com

[✓] Main Account (Account 1) identity recorded: ow***n@example.com
    Salted hash stored securely in state directory (mode 0600).
    Collision prevention against Account 1 is now active.
```

### Security Guarantee:
- The plain email address is **never** written to disk, git, or logs.
- Stored as a salted SHA-256 hash in `~/.multigravity/state/main_identity.json` with permissions `0600` (directory mode `0700`).

---

## Step 2: Run Diagnostics (Doctor)

Run `doctor` to audit system binaries, isolation tools, path guards, environment filters, and keyring isolation before logging in:

```bash
python3 -m multigravity.cli doctor
```

### Expected Output:
```text
==============================================================================
                        MULTIGRAVITY DOCTOR REPORT
==============================================================================
 STATUS  TYPE         CHECK                     : DETAILS
------------------------------------------------------------------------------
 [PASS]  [REAL]       agy binary                : Found at /usr/sbin/agy (Python 3.14 OK)
 [PASS]  [REAL]       keyring isolation tools   : dbus-run-session, gnome-keyring, secret-tool, xdg-open available
 [PASS]  [REAL]       path guard                : All worker profile paths are strictly isolated
 [PASS]  [REAL]       env allow-list            : Child environment strictly filtered (4 allowed variables, zero credentials leaked)
 [PASS]  [REAL]       profile permissions       : Profile directories configured mode 0700
 [PASS]  [REAL]       keyring isolation         : Worker credential stores and real HOME isolation verified
 [PASS]  [SIMULATED]  two-worker collision      : Simulated (duplicate rejection verified in test suite; workers not logged in yet)
==============================================================================
 Result: All isolation and security checks PASSED.
```

> [!TIP]
> If any check displays `[FAIL]`, review the message. For missing packages, run the suggested `sudo pacman -S` command.

---

## Step 3: Run the Login Wizard

Start the sequential login wizard:

```bash
python3 -m multigravity.cli
```

*(Alternatively, to log in a single worker individually, run `python3 -m multigravity.cli login worker-1`)*

### Interactive Flow:

1. **Preflight & Status Display**:
   The wizard prints the current status board showing all unauthenticated workers.

2. **Sequential Account Prompts**:
   For each unverified worker (`worker-1 (Account 2)`, `worker-2 (Account 3)`, `worker-3 (Account 4)`):
   ```text
   ##############################################################################
   Now logging in: worker-1 (Account 2)
   Target Google Account: Account 2 (NOT your main account)
   ##############################################################################
   NOTICE:
   • If your browser is already signed into your main account, Google will often auto-select it.
   • When the browser window opens, look closely at the active profile or select 'Use another account'.
   • The login URL will be displayed below so you can also copy-paste it into a Private/Incognito window.

   Press [Enter] to begin login for this worker...
   ```

3. **Browser Authentication**:
   - The helper script prints the OAuth URL in terminal.
   - If a graphical session is active, a private/incognito window opens automatically.
   - **Action**: In the Google sign-in window:
     - Check the active account avatar.
     - If it defaulted to Account 1 or another account, click **"Use another account"**.
     - Sign in with the **dedicated credentials** for that specific worker account.
     - Grant OAuth consent for Google Antigravity.

4. **Verification & Duplicate Protection**:
   - The CLI probes the worker's isolated Secret Service keyring.
   - If an identity token is found, it extracts the account email automatically; otherwise it prompts you to confirm the email you just logged into.
   - **Collision Prevention Check**:
     - If the account matches **Account 1 (Main Account)**:
       ```text
       !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
       COLLISION ERROR: The account 'ow***n@example.com' matches Main Account (Account 1)!
       Explanation: Worker worker-1 (Account 2) MUST NOT use your main interactive account.
       Your browser likely auto-selected an existing signed-in profile.
       !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
       Wipe profile for worker-1 (Account 2) and retry? [y/N]:
       ```
       Type `y` to wipe the profile and re-authenticate with the correct account.
     - If the account matches an already-authenticated worker, collision detection triggers similarly.

5. **Proceed to Next Worker**:
   Repeat for `worker-2 (Account 3)` and `worker-3 (Account 4)`.

---

## Step 4: Verify Status Board

After onboarding the workers, print the status board:

```bash
python3 -m multigravity.cli status
```

### Expected Output:
```text
==============================================================================
                        MULTIGRAVITY WORKER STATUS
==============================================================================
WORKER (ACCOUNT)           STATE          IDENTITY           VERIFIED
------------------------------------------------------------------------------
worker-1 (Account 2)       OK             ac***2@example.com   2026-10-18 17:30
worker-2 (Account 3)       OK             ac***3@example.com   2026-10-18 17:32
worker-3 (Account 4)       OK             ac***4@example.com   2026-10-18 17:35
==============================================================================
```

All 3 background accounts should show state `OK` with distinct masked identities.

---

## Step 5: Start or Stop the Background Daemon

### Starting the Core Daemon
When you run `python3 -m multigravity.cli` without arguments and all workers are verified, the core daemon starts in the background automatically:
```text
[✓] Core daemon started with PID 123456.
Next step: The interactive main agent (Account 1) can now submit tasks to sub-agents via MCP!
```

### Stopping the Daemon Cleanly
To shut down the background daemon:
```bash
python3 -m multigravity.cli stop
```
Output:
```text
Sent SIGTERM to daemon process (PID 123456). Waiting for clean shutdown...
[✓] multigravity daemon stopped cleanly.
```

---

## Troubleshooting & FAQ

### Q: What if I accidentally press Ctrl-C during login?
The login wizard reaps the child process group immediately (`SIGTERM` followed by `SIGKILL` if needed). The worker profile remains cleanly marked as `PENDING`, and your desktop keyring is completely untouched. You can simply re-run `python3 -m multigravity.cli login <worker-id>`.

### Q: How do I re-authenticate an expired worker?
Run:
```bash
python3 -m multigravity.cli login worker-1
```
The wizard will overwrite the expired token inside that worker's private keyring without affecting any other worker or Account 1.

### Q: Where are the worker credentials stored?
Under `~/.multigravity/workers/worker-N/data/keyrings/login.keyring` (mode `0700`). Each worker has its own isolated D-Bus session bus and its own private `gnome-keyring-daemon`.
