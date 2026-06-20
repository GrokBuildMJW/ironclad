# Security & trust profiles (operator guide)

Ironclad is **single-tenant**: there is one operator/principal. Nothing here authenticates a *user* —
the optional token is a **deployment secret** ("is this my client process?"), not a login. Multi-user
identity/authorization is a separate, unbuilt phase (see [`roadmap.md`](roadmap.md)). This guide is how an
operator actually configures and runs the trust profiles; the mechanism lives in `engine/security.py`.

## The three profiles

Pick one with `security.profile` (or `GX10_PROFILE`). Weakest → strongest:

| `security.profile` | Auth | Bind | Session | Code locality | Use it when |
|--------------------|------|------|---------|---------------|-------------|
| **`open`** (default) | none | as requested (LAN) | — | `mount` allowed | trusted home LAN / local dev |
| **`token`** | `Bearer` deployment secret on gated routes | as requested (LAN) | — | honored as set | a shared LAN you want to gate |
| **`sealed`** | secret **required** | forced `127.0.0.1` (loopback) | **required** (open/heartbeat/close) | forced `local` | exposed / behind a client tunnel |

**Fail-closed boot:** a profile that needs a secret (`token`/`sealed`) **refuses to start** if none is set —
`security.profile=… requires a deployment secret but none is set`. Export the token first.

## Config keys & env overrides

All under `security.*`; each has an env override (env wins). The token *value* is never in config — it is
read from the environment variable **named** by `security.token_env`.

| Config key | Env override | Default | Meaning |
|------------|--------------|---------|---------|
| `security.profile` | `GX10_PROFILE` | `open` | the trust profile (boot-only / [frozen](config-runtime.md)) |
| `security.token_env` | — | `GX10_SERVER_TOKEN` | the **name** of the env var holding the secret |
| *(the secret value)* | `GX10_SERVER_TOKEN` (or your `token_env`) | — | the shared deployment secret (never in config/repo) |
| `security.session_heartbeat_s` | `GX10_SESSION_HEARTBEAT` | `30` | heartbeat interval; a session is live within **2×** this |
| `security.code_locality` | `GX10_CODE_LOCALITY` | `mount` | `mount` \| `local`; **`sealed` forces `local`** |

`security.profile` is a **frozen** config key (it wires the trust policy + bind host once at boot):
`/config get` reads it, `/config set` is refused — set it in the deploy and restart.

## The request gate

These routes require authorization (and, under `sealed`, a live session):

```
/chat  /chat/stream  /tool-result  /fanout  /cancel  /tasks  /pending  /feedback  /doctor
```

`/health` and `/session/open|heartbeat|close` are **not** gated — `/health` is the pre-auth handshake (it
advertises the profile shape, never the token) and `/session/open` checks the token itself. The gate checks
**token first** (cheap, no info leak), then the session:

- **`token` profile:** the same gated routes return **401** without a valid `Authorization: Bearer <secret>`
  and **200** with it — no session needed.
- **`sealed` profile:** as above, **plus** a live session is required; with none, gated routes return **401**
  ("channel sealed") and background autoplan **pauses** (`[AUTOPLAN] paused — channel sealed`).

### Client header contract

```
Authorization: Bearer <deployment-secret>     # token + sealed
X-Session-Id:  <session-id from /session/open> # sealed only
```

### Session lifecycle (sealed)

1. `POST /session/open` (with the secret) → `{session_id, heartbeat_s}`.
2. `POST /session/heartbeat` with `X-Session-Id` every ≤ `heartbeat_s` — the session stays live within
   `2 × heartbeat_s`.
3. `POST /session/close` on exit. The channel **seals the moment the last live session ends** (app-enforced
   by the heartbeat TTL; OS-enforced too when the loopback tunnel closes).

## Sealed deployment in practice

`sealed` binds `127.0.0.1:<port>` (default `8100`, `GX10_SERVER_PORT`) — it is **not** on the LAN. The
client reaches it over a **client-managed tunnel** (e.g. an SSH local-forward); the transport specifics live
in the operator's private config, never in `core/`. Code stays local (`code_locality=local`, no mount).

```bash
export GX10_PROFILE=sealed
export GX10_SERVER_TOKEN=$(openssl rand -hex 32)   # the shared deployment secret
python engine/server.py                            # binds 127.0.0.1:8100, refuses without a live session
```

`/health` then reports `profile: sealed`, `auth: true`, `session: true` so the client knows to open a
session before anything else.
