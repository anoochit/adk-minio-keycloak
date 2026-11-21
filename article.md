# From 502s to First Token: Shipping a Zero-Trust File Assistant with Google ADK, Keycloak & MinIO STS

*How a chat agent, an OAuth proxy, and a self-hosted object store learned to cooperate — and the nine bugs that stood between them.*

---

## The Goal

I wanted a simple thing, said in the language of product managers: **a chat assistant that manages your personal files, and provably cannot see anyone else's.**

No "trust the frontend." No "the backend filters it." The storage layer itself must enforce isolation, using nothing but the identity embedded in the user's own login token. The model in the middle — Google's ADK agent running Gemini — should be an *untrusted orchestrator*: it can ask for files, but the credentials it receives are scoped to one folder, for one user, for fifteen minutes.

That constraint turns a toy chatbot into a small but real zero-trust system. This is the story of building it, and of the nine bugs that taught me how the pieces actually fit together.

---

## The Stack

```text
Browser ──▶ oauth2-proxy (:4180) ──▶ react-web (nginx + Vite build)
                 │                        │  /apps /run /run_sse
                 │ injects X-Forwarded-*   ▼
              Keycloak              adk-server (FastAPI + Google ADK)
              (:8080)                       │  JWT → STS exchange
                                            ▼
                                     MinIO (:9000)
                                     bucket: user-files/{username}/*
```

* **Keycloak** is the OIDC provider. A realm (`my-realm`) is auto-imported on first boot with a client (`adk-minio-client`) and a test user, `alice`.
* **oauth2-proxy** sits in front of everything, turns the Keycloak login into a session cookie, and forwards the access token plus username as headers: `X-Forwarded-Access-Token`, `X-Forwarded-Preferred-Username`.
* **react-web** is a Vite build served by nginx. Its nginx config doubles as the reverse proxy for ADK's session and streaming endpoints.
* **adk-server** wraps Google ADK's FastAPI app. A custom middleware validates the proxy's headers and injects the user's identity into every agent run.
* **MinIO** provides S3-compatible storage *and* the STS endpoint. `AssumeRoleWithWebIdentity` exchanges the user's Keycloak JWT for temporary credentials bound to an IAM policy.

### Three enforcement layers

1. **Proxy layer** — oauth2-proxy refuses anything without a valid Keycloak session.
2. **Application layer** — `AuthInjectMiddleware` re-pins the authenticated username onto every request, overwriting whatever the client sent, and passes `keycloak_token` + `username` to the agent via ADK's `state_delta`.
3. **Storage layer** — MinIO validates the JWT against Keycloak's OIDC discovery document, mints STS credentials, and applies `user-folder-policy.json`:

```json
{
  "Action": ["s3:ListBucket"],
  "Resource": "arn:aws:s3:::user-files",
  "Condition": { "StringLike": { "s3:prefix": ["${jwt:preferred_username}/*"] } }
},
{
  "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
  "Resource": "arn:aws:s3:::user-files/${jwt:preferred_username}/*"
}
```

The policy is templated from the token's own claims. Even if every line of application code were malicious, `alice`'s credentials cannot touch `bob/` — MinIO says no.

Layer 3 is the heart of the design. Everything else exists to deliver a *real* token to it.

---

## The Bug Log

Architecture diagrams are how software looks in my head. The list below is how it looked on screen.

### 1. `502 Bad Gateway` — every time I restarted the agent

**Symptom:** The stack worked until `docker compose up -d adk-server` recreated the container. From then on, every request through the proxy returned 502 — until I restarted nginx too.

**Root cause:** nginx resolves upstream hostnames **once, at startup**, and caches the IP forever. `adk-server` got a new IP; nginx kept dialing the old one.

**Fix** (`react-web/nginx.conf`):

```nginx
resolver 127.0.0.11 valid=10s ipv6=off;
set $adk_upstream http://adk-server:8000;
proxy_pass $adk_upstream;
```

Using a *variable* in `proxy_pass` forces per-request resolution, and `valid=10s` bounds how stale the answer can be. Docker's embedded DNS (`127.0.0.11`) does the rest.

### 2. `403 Forbidden` — ADK didn't trust my own frontend

**Symptom:** The proxy forwarded everything correctly, and ADK still rejected `/run_sse` with 403.

**Root cause:** ADK has an origin/host gate: the request's `Origin` header must match the `Host` it receives (or appear in `allow_origins`). Two things were wrong: `allow_origins` was never configured, and nginx forwarded `Host` via `$host`, which **strips the port** — so ADK compared `http://localhost:4180` against `http://localhost`.

**Fix:**

```python
app = get_fast_api_app(agents_dir=".", allow_origins=ALLOWED_ORIGINS, web=False)
```
```nginx
proxy_set_header Host $http_host;   # keeps :4180
```

`$http_host` (with port) versus `$host` (without) is a one-character difference that cost an afternoon.

### 3. Auth failures returned `500` instead of `401`

**Symptom:** A missing token produced `Internal Server Error`, not `401 Unauthorized`.

**Root cause:** My middleware did `raise HTTPException(status_code=401)`. Raising from inside an HTTP middleware bypasses Starlette's exception handlers — the exception propagates to the server and becomes a 500.

**Fix:** *Return* the response instead of raising it:

```python
def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": detail})
```

### 4. The identity rewrite was a silent no-op (and then a crash)

**Symptom:** First, `user_id` rewrites never reached the route. After "fixing" it by patching `request._receive`, `/run_sse` crashed with:

```text
RuntimeError: Unexpected message received: http.request
```

**Root cause:** `BaseHTTPMiddleware` reads `request.body()` to let you inspect it — and then **forwards the cached bytes** downstream. Patching `_receive` changes what *your* code sees, never what the route sees, while corrupting the disconnect handshake the streaming response depends on.

**Fix:** rewrite the whole thing as **raw ASGI middleware** (`AuthInjectMiddleware`), operating directly on `scope`, `receive`, and `send`. Body rewriting uses a replay channel:

```python
async def receive() -> Message:
    nonlocal sent
    if not sent:
        sent = True
        return {"type": "http.request", "body": new_body, "more_body": False}
    return await fallback()
```

One fixed body, then defer to the real channel. The lesson: `BaseHTTPMiddleware` is convenient right up until you need the request to still *be* a request.

### 5. `200 OK` with an empty stream

**Symptom:** `/run_sse` connected, returned 200, and then… nothing. No events, no error.

**Root cause:** my replay channel returned `http.disconnect` on the second read. Starlette's `StreamingResponse` waits on a disconnect concurrently — an immediate one **cancels the stream** before the model ever finishes.

**Fix:** the same replay shown above: first message = rewritten body; everything after = the real `receive`, which will only ever yield the genuine disconnect.

### 6. `409 Conflict` on "ensure session"

**Symptom:** Second message in a chat → 409 → client-side error.

**Root cause:** creating a session that already exists is a conflict — but *ensure* semantically means "make sure it exists." It does.

**Fix** (`adkStream.ts`):

```ts
if (!res.ok && res.status !== 409) { throw new Error(...); }
```

### 7. STS: `invalid ARN - bad partition field`

**Symptom:** every tool call failed with:

```text
An error occurred (InvalidParameterValue) when calling the
AssumeRoleWithWebIdentity operation: Error processing RoleArn parameter:
RoleARN parse err: invalid ARN - bad partition field
```

**Root cause:** I assumed AWS conventions. MinIO's STS parses ARN **partitions itself**, and its partition is `minio`, not `aws`. Probing the endpoint directly revealed the truth — omitting `RoleArn` produced:

```text
Role arn:minio:iam:::role/dummy-internal does not exist
```

MinIO has a role registry. When configured with `MINIO_IDENTITY_OPENID_ROLE_POLICY`, it generates a role at startup and prints it:

```text
INFO: IAM Roles: arn:minio:iam:::role/ivd6H_TwNAxH9YkB81tz8yJe_Jk
```

**Fix:** put *that exact ARN* in `MINIO_ROLE_ARN`. It's stable across restarts (I verified), derived from config rather than stored data, and there is no admin API to list or rename it — the log line is the source of truth.

### 8. `AccessDenied` on every S3 call — but raw `boto3` worked fine

**Symptom:** with STS finally succeeding, every agent tool still failed:

```text
S3 operation failed; code: AccessDenied, message: Access Denied., resource: /user-files
```

Isolation tests passed (`alice/t.txt` ✓, `other/b.txt` denied ✓) — so the policy was right. Something else was being denied.

**Root cause:** bisecting operations one by one:

| Operation | Result |
|---|---|
| `PutObject alice/t.txt` | ✅ OK |
| `ListObjectsV2 Prefix=alice/` | ✅ OK |
| `PutObject other/b.txt` | ❌ AccessDenied *(correct!)* |
| `GetBucketLocation` | ❌ **AccessDenied** ← culprit |

The `minio` Python SDK performs an automatic `GetBucketLocation` (region lookup) before **every** operation. The policy didn't grant it, so the SDK's own housekeeping call sank every request before the real one was sent. `boto3` doesn't do this — hence "works in isolation, fails in the app."

**Fix:** add a statement to `user-folder-policy.json`:

```json
{ "Effect": "Allow", "Action": ["s3:GetBucketLocation"],
  "Resource": "arn:aws:s3:::user-files" }
```

Re-applied with `mc admin policy create` (which overwrites), so future `minio-init` runs pick it up too.

*Takeaway: when a policy is "correct" but access is denied, log or bisect the actual operation list — SDKs make calls you never wrote.*

### 9. The assistant answered twice

**Symptom:** every reply rendered twice in the chat: `…เรียบร้อยแล้ว…เรียบร้อยแล้ว`.

**Root cause:** inspecting the raw SSE stream showed ADK's event contract:

```text
event 1  partial=true   text='ชีวิตในเมืองใหญ่มักเต็มไปด้วย…'   ← delta
event 2  partial=true   text='ทุกๆ วัน ผู้คนต่างออกเดินทาง…'    ← delta
…
event N  partial=false  text='(the complete text)'              ← full, again
```

Deltas for streaming, then the full text as the authoritative final event. My client appended *everything*.

**Fix** (`adkStream.ts` → `chatBox.tsx`):

* `partial === true` → append via `onToken` (streaming feel preserved)
* `partial === false` → `onFinal(fullText)` **replaces** the reply: `setReply(text)`

The final event is a replacement, not an append — so even if a delta was lost, the user sees exactly the right text, exactly once.

*(Bonus bug, same theme: this ADK build ignores `GOOGLE_GENAI_API_KEY` and only reads `GOOGLE_API_KEY`/`GEMINI_API_KEY`. A two-line bridge in `main.py` — copy without clobbering — saved the model calls.)*

---

## Verification: Trust, Then Verify

With everything green, I tested the way I'd attack it — with a **real** Keycloak token, through the **real** proxy chain:

1. **Happy path:** create `hello.txt` with Thai content → listed as `hello.txt (38 bytes)` → read back identical.
2. **Independent check:** fresh STS credentials, direct MinIO listing — object exists at `alice/hello.txt` with the right bytes. The agent wasn't lying.
3. **Isolation:** with `alice`'s credentials, writing `other/b.txt` → `AccessDenied`. The policy holds.
4. **Security matrix:**

   | Request | Result |
   |---|---|
   | Correct origin (`:4180`), valid token | ✅ 200 |
   | Missing token | ✅ 401 (not 500) |
   | Foreign origin | ✅ 403 |
   | CORS preflight | ✅ 200 |
   | Another user's session ID | ✅ 404 |
   | Forged `user_id` in body | ✅ overwritten server-side |

5. **Streaming:** a scripted run of the *actual* TypeScript client against a live `/run_sse` confirmed `onFinal` fires exactly once and the reply renders once.

---

## What I'd Take Away

1. **The storage layer is the only layer that can't lie.** Everything upstream — headers, middleware, the model — can be bypassed or confused. A policy evaluated by MinIO cannot.
2. **`BaseHTTPMiddleware` is a trap for body rewriting.** If you need to change what the route reads, go raw ASGI and think hard about `receive` semantics.
3. **nginx caches DNS;** Docker containers get new IPs. A `resolver` line + variable `proxy_pass` is the difference between "restart the stack" and "it just works."
4. **SDKs make invisible calls.** `GetBucketLocation` before every operation is invisible until you bisect. When policy and behavior disagree, test operations individually.
5. **Read the stream format before debugging the UI.** The doubled-reply bug looked like a React state bug; it was a protocol contract — `partial=true` deltas versus `partial=false` final — hiding one `if` statement away.
6. **Error messages are breadcrumbs.** `bad partition field` → probe the endpoint → `dummy-internal does not exist` → startup log → `IAM Roles:`. Each message narrowed the search; none of them were where I was looking.

---

## Run It

```bash
docker compose up --build -d
# open http://localhost:4180  →  sign in as alice / password123
# "สร้างไฟล์ชื่อ hello.txt เนื้อหา สวัสดีจาก ADK นะ"
# "รายชื่อไฟล์ของฉันทั้งหมด"
```

If STS ever complains about an ARN:

```bash
docker compose logs minio | grep "IAM Roles"
```

Full setup, configuration reference, and the complete troubleshooting index live in [`README.md`](README.md). The agent, its five MinIO tools, and the middleware are in [`adk-server/`](adk-server/).

---

*Built with Google ADK, FastAPI, Keycloak, oauth2-proxy, MinIO, nginx, React, and Gemini — held together by one JWT and a policy template.*
