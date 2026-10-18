# Investigation: agy Authentication, Keyring Storage & Process Isolation

This document records the empirical findings regarding Google Antigravity CLI (`agy`) credential storage, authentication flow, browser invocation, empty-home behavior, account identity reporting, and keyring isolation.

---

## 1. Where `agy` Stores Credentials

### Empirical Discovery
1. **Desktop Keyring via Secret Service (D-Bus)**:
   - On Linux, `agy` integrates with the system desktop keyring using `github.com/zalando/go-keyring`.
   - Credentials are saved under:
     - **Service**: `gemini`
     - **Username**: `antigravity`
     - **Attributes**: `schema = org.freedesktop.Secret.Generic`, `attribute.service = gemini`, `attribute.username = antigravity`.
   - The stored secret is a JSON payload containing OAuth 2.0 access tokens, refresh tokens, auth method, and a signed OpenID Connect JWT (`id_token`).
   - The physical backing files are maintained by `gnome-keyring-daemon` under `$XDG_DATA_HOME/keyrings/` (`login.keyring` and `user.keystore`).

2. **The Keyring Collision Vulnerability**:
   - Because `agy` uses hardcoded keys (`service = gemini`, `username = antigravity`), running multiple workers (or a worker and Account 1) against the default user D-Bus session causes silent overwrite of credentials.
   - When Account 2 logs in on the shared session, it would overwrite Account 1's secret in the user's desktop keyring.

3. **Isolation Solution: Private D-Bus Session & Private Keyring**:
   - Each worker runs in its own private D-Bus session spawned via `dbus-run-session`.
   - Inside the private D-Bus session, a dedicated `gnome-keyring-daemon` is launched with `--control-directory=$XDG_RUNTIME_DIR/keyring` and `$XDG_DATA_HOME=$WORKER_DIR/data`.
   - By initializing the private keyring with an unlocked empty password (`echo '' | gnome-keyring-daemon --unlock`), the worker process reads and writes its credentials seamlessly with zero user prompting and without modifying or contacting the user's real keyring in `~/.local/share/keyrings/`.

---

## 2. Login and Status Commands and Flags

- **Interactive Launch**:
  - Running `agy` in an empty profile directory interactively launches the onboarding wizard (theme selection `[Next]`, followed by Google Account sign-in).
- **Probing / Non-Interactive Status Check**:
  - `agy models` cheaply contacts the API using the profile's credentials.
  - When authenticated: exits with code `0` and outputs model IDs (`gemini-3.8-flash-high`, etc.).
  - When unauthenticated: logs `error getting token source: You are not logged into Antigravity` and outputs no models.
  - When rate-limited: logs `429` / `RESOURCE_EXHAUSTED`.
  - When session expired: logs `oauth2: cannot fetch token` / `Keyring error`.

---

## 3. Browser Launch & URL Handling

- `agy` calls `xdg-open` to launch the browser during OAuth 2.0 authentication.
- On Linux, `xdg-open` honors the `BROWSER` environment variable.
- By configuring `BROWSER` to invoke a dedicated browser wrapper script (`scripts/multigravity-browser-helper`):
  1. The OAuth URL is intercepted and printed prominently to the terminal with clear instructions to open it in a private/incognito window or the correct browser profile.
  2. The user is prompted to use "Use another account" to avoid accidentally selecting Account 1.
  3. If a graphical browser is present, it can be launched cleanly in incognito mode (`google-chrome --incognito`, `firefox --private-window`).

---

## 4. Behavior with Empty Custom HOME

- When `agy` runs with a new, empty `HOME` directory:
  1. It creates standard config structures under `$HOME/.gemini/antigravity-cli/` (cache, conversations, logs, `installation_id`).
  2. It honors `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`, and `XDG_RUNTIME_DIR`.
  3. It does not crash or corrupt the parent environment as long as directory permissions are `0700` and paths are isolated.

---

## 5. Account Identity Extraction & Masking

- The stored OAuth secret contains an `id_token` (JWT).
- The payload portion of the JWT contains:
  ```json
  {
    "iss": "https://accounts.google.com",
    "email": "user@example.com",
    "email_verified": true,
    "name": "User Name"
  }
  ```
- **Automatic Identity Verification**:
  - The worker's private keyring is queried via `secret-tool lookup service gemini username antigravity`.
  - The email address is parsed directly from the JWT payload.
  - If no `id_token` is present, the CLI prompts the user to enter the email they signed into.
- **Privacy & Security**:
  - Credential contents, tokens, and raw keys are **never printed, logged, or saved to plain disk**.
  - Emails are displayed masked (e.g. `kr***@example.com`).
  - Only a salted SHA-256 hash of the email is stored in the profile metadata (`metadata.json`).
  - The duplicate check verifies that no two worker accounts share the same email hash, and that no worker shares an email hash with Account 1.
