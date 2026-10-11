# AutoResponder ↔ Hermes Bridge

A small Flask server that runs on your PC. It takes chat messages forwarded by the
**AutoResponder** Android app, gets a reply from your local **Hermes** model
(through Hermes' OpenAI-compatible API), and sends the reply back to the phone.

```
Phone (AutoResponder) --HTTPS POST /webhook--> Tunnel --> app.py (your PC) --> Hermes :8642/v1
                      <--- {"replies":[{"message": "..."}]} ----------------------------
```

## Files

| File | Purpose |
|------|---------|
| `app.py` | The server (single file) |
| `requirements.txt` | Python dependencies |
| `.env.example` | Settings template. Copy it to `.env` |
| `run_ngrok.py` | Optional helper that opens an ngrok tunnel from Python |
| `.gitignore` | Keeps `.env`, logs and the virtualenv out of git |

## Behaviour

* **`POST /webhook`** reads `query.sender` and `query.message` and replies with
  `{"replies":[{"message":"..."}]}`.
* **Auth:** Basic Auth is required. If `AUTH_HEADER_TOKEN` is set, an `X-Auth-Token`
  header is required as well. Both are checked with constant-time comparison. A request
  that fails either check gets **401**. It didn't come from your app, so there's no reply
  flow to protect.
* **Errors never break the app:** an authenticated request **always** gets HTTP 200 with
  a valid body. That includes Hermes being down, timing out, returning an error or an
  empty reply, plus invalid JSON, an empty message, or an unexpected exception. In those
  cases the body carries `FALLBACK_MESSAGE`.
* **Logging:** every incoming message (platform, sender, group info, text) and every
  reply goes to stdout and to `bridge.log`. The log file rotates at 5 MB and 5 old files
  are kept.
* **Platform-agnostic:** it doesn't care which messenger sent the message.
  `messengerPackageName` is only used for logging and to keep each chat's memory separate.
* **Conversation memory (optional):** the last `HISTORY_TURNS` exchanges per chat go to
  Hermes so replies have context. Memory lives in RAM and clears on restart. Set
  `HISTORY_TURNS=0` to turn it off.
* **Group chats:** the message is sent to Hermes as `[Group chat 'Name'] Participant: text`
  so the model knows who is talking.
* **`GET /health`** returns `{"status":"ok"}` without auth. Use it to check that the
  tunnel works.
* Runs on **Waitress**, a production WSGI server for Windows, macOS and Linux, rather
  than Flask's dev server.

---

## 1. Local setup

You need Python 3.11+ and Hermes running at `http://localhost:8642/v1`.

### Windows (PowerShell)

```powershell
cd path\to\hermes-bridge
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env
```

### macOS / Linux

```bash
cd path/to/hermes-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
```

### Fill in `.env`

At minimum, set:

```ini
HERMES_API_KEY=<your Hermes API key>
BASIC_AUTH_USERNAME=autoresponder
BASIC_AUTH_PASSWORD=<long random string>
AUTH_HEADER_TOKEN=<another long random string>   # or leave empty to disable
```

Generate strong random values with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

To use a remote Hermes later, just change `HERMES_BASE_URL` (keep the `/v1` at the end).

### Run

```bash
python app.py
```

You should see:

```
[INFO] Starting bridge on http://127.0.0.1:5000  ->  Hermes at http://localhost:8642/v1 (model=hermes-agent)
```

The server won't start if the Basic Auth credentials are missing.

### Test locally

macOS / Linux / Git Bash:

```bash
curl -i -u 'autoresponder:YOUR_PASSWORD' \
  -H 'X-Auth-Token: YOUR_HEADER_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"appPackageName":"com.example.autoresponder","messengerPackageName":"com.example.messenger","query":{"sender":"John Doe","message":"Hello, is this a bot?","isGroup":false,"groupParticipant":""}}' \
  http://127.0.0.1:5000/webhook
```

Windows PowerShell (use `curl.exe`, not the `curl` alias):

```powershell
curl.exe -i -u "autoresponder:YOUR_PASSWORD" `
  -H "X-Auth-Token: YOUR_HEADER_TOKEN" `
  -H "Content-Type: application/json" `
  -d '{\"query\":{\"sender\":\"John Doe\",\"message\":\"Hello, is this a bot?\",\"isGroup\":false,\"groupParticipant\":\"\"}}' `
  http://127.0.0.1:5000/webhook
```

The expected result is `HTTP/1.1 200 OK` with `{"replies":[{"message":"..."}]}`.

---

## 2. Make it reachable from your phone

Keep `HOST=127.0.0.1` in `.env`. The tunnel connects from your PC to the outside, so you
don't need to open router ports and the server isn't exposed on your LAN. Keep `app.py`
running in one terminal and start the tunnel in a second one.

### Option A: Cloudflare Tunnel (preferred)

**Install `cloudflared`:**

| OS | Command |
|----|---------|
| Windows | `winget install --id Cloudflare.cloudflared` |
| macOS | `brew install cloudflared` |
| Debian/Ubuntu | `curl -L -o cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb && sudo dpkg -i cloudflared.deb` |

Check the install with `cloudflared --version`.

#### A1. Quick tunnel (no account, good for testing)

```bash
cloudflared tunnel --url http://localhost:5000
```

It prints a URL like `https://random-words-1234.trycloudflare.com`. Your webhook URL is:

```
https://random-words-1234.trycloudflare.com/webhook
```

⚠️ The URL changes **every time** you restart `cloudflared`, so you'd have to update the
app each time. For permanent use, set up A2.

#### A2. Named tunnel (permanent URL; needs a free Cloudflare account and a domain on Cloudflare)

```bash
# 1. Log in. This opens a browser; pick your domain.
cloudflared tunnel login

# 2. Create the tunnel. Note the Tunnel ID (UUID) it prints.
cloudflared tunnel create hermes-bridge

# 3. Point a subdomain at it.
cloudflared tunnel route dns hermes-bridge bridge.yourdomain.com
```

Create the config file:

* Windows: `C:\Users\<you>\.cloudflared\config.yml`
* macOS/Linux: `~/.cloudflared/config.yml`

```yaml
tunnel: hermes-bridge
credentials-file: C:\Users\<you>\.cloudflared\<TUNNEL-ID>.json   # ~/.cloudflared/<TUNNEL-ID>.json on macOS/Linux

ingress:
  - hostname: bridge.yourdomain.com
    service: http://localhost:5000
  - service: http_status:404
```

Then run it:

```bash
cloudflared tunnel run hermes-bridge
```

Your permanent webhook URL is `https://bridge.yourdomain.com/webhook`.

To start the tunnel automatically on boot, run `cloudflared service install` as
Administrator on Windows, or `sudo cloudflared service install` on Linux/macOS. As a
service it reads the config from the system location; see Cloudflare's docs if it can't
find your `config.yml`.

### Option B: ngrok (alternative)

1. Sign up at <https://dashboard.ngrok.com> and copy your authtoken.
2. Install it: Windows `winget install ngrok.ngrok`, macOS `brew install ngrok`, or
   download from <https://ngrok.com/download>.
3. Add your token once:

   ```bash
   ngrok config add-authtoken YOUR_NGROK_AUTHTOKEN
   ```

4. Start the tunnel:

   ```bash
   ngrok http 5000
   ```

   Copy the `Forwarding` URL (for example `https://ab12-34-56.ngrok-free.app`). Your
   webhook URL is that URL followed by `/webhook`.

5. **Recommended: a stable URL.** On the ngrok dashboard, go to *Domains* and claim your
   free static domain. Then run:

   ```bash
   ngrok http --url=your-name.ngrok-free.app 5000
   ```

   Older ngrok versions use `--domain=` instead of `--url=`.

**Or start ngrok from Python:** put `NGROK_AUTHTOKEN` (and optionally `NGROK_DOMAIN`)
in `.env`, then run:

```bash
python run_ngrok.py
```

It prints the exact webhook URL to paste into the app.

> On the free plan, ngrok shows a warning page to *browsers* only. AutoResponder's API
> calls aren't affected. If they ever are, add a custom header
> `ngrok-skip-browser-warning: true` in the app.

### Verify the tunnel

From the phone's browser, on mobile data with Wi-Fi off, open:

```
https://<your-public-url>/health
```

You should see `{"status":"ok"}`.

---

## 3. Configure AutoResponder

In AutoResponder, open the rule you want to automate and set its reply to come from a
**web server / webhook**. The exact menu names vary between versions. Then fill in:

| App field | Value |
|-----------|-------|
| Server URL | `https://<your-public-url>/webhook` (**`https`**, ending in `/webhook`) |
| Basic Auth username | your `BASIC_AUTH_USERNAME` |
| Basic Auth password | your `BASIC_AUTH_PASSWORD` |
| Custom header | Key `X-Auth-Token`, Value: your `AUTH_HEADER_TOKEN` (skip if you left it empty) |

Save the rule and send a test message from another account. The reply should arrive, and
you'll see an `IN` / `OUT` pair in the server's log.

**When the URL changes** (quick tunnel or ngrok without a static domain): copy the new
public URL from the tunnel terminal and replace only the host part of *Server URL* in the
rule. Keep `/webhook` at the end, then save. The credentials don't change.

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| App gets **401** | The username, password or `X-Auth-Token` in the app doesn't match `.env`. The log says which check failed. |
| App always receives the fallback message | Hermes is unreachable or erroring. Look for `Hermes API error` in `bridge.log` and check that Hermes is running and that `HERMES_BASE_URL` and `HERMES_API_KEY` are correct. |
| Replies are slow or the app times out | Lower `HERMES_TIMEOUT` so the fallback gets sent before the app gives up, use a smaller model, or set `HISTORY_TURNS=0`. |
| `/health` doesn't load on the phone | The tunnel isn't running, or the URL is wrong or outdated. |
| `Refusing to start an unprotected server` | Set `BASIC_AUTH_USERNAME` and `BASIC_AUTH_PASSWORD` in `.env`. |

## Security notes

* Never commit `.env`. It is already in `.gitignore`.
* Always use the `https://` tunnel URL. Basic Auth credentials are only safe over TLS.
* Use long random values for the password and the header token, and rotate them if they leak.
* `bridge.log` contains message contents. Protect or delete it as needed.
