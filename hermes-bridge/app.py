"""
AutoResponder <-> Hermes bridge.

The AutoResponder Android app POSTs incoming chat messages to /webhook.
This server forwards each message to Hermes (an OpenAI-compatible API),
then returns the generated reply in the exact JSON shape the app expects:

    {"replies": [{"message": "..."}]}

Design rules:
  * Authenticated requests ALWAYS get HTTP 200 + a valid "replies" body,
    even if Hermes is down, slow, or the payload is malformed. A fallback
    message is returned instead of an error so the app's reply flow never
    breaks.
  * Unauthenticated requests get 401. They are not from your app, so there
    is no reply flow to protect.

Run:  python app.py        (see README.md for full setup)
"""

from __future__ import annotations

import hmac
import logging
import os
import sys
import threading
from collections import OrderedDict, deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_httpauth import HTTPBasicAuth
from openai import OpenAI, OpenAIError

# ---------------------------------------------------------------------------
# Configuration (all values come from .env / environment variables)
# ---------------------------------------------------------------------------

# Load .env from the same folder as app.py, regardless of the working directory.
load_dotenv(Path(__file__).resolve().with_name(".env"))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


# Hermes (OpenAI-compatible) settings
HERMES_BASE_URL = os.getenv("HERMES_BASE_URL", "http://localhost:8642/v1")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "")
HERMES_MODEL = os.getenv("HERMES_MODEL", "hermes-agent")
HERMES_TIMEOUT = _env_float("HERMES_TIMEOUT", 60.0)  # seconds per request
HERMES_MAX_RETRIES = _env_int("HERMES_MAX_RETRIES", 1)
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a helpful assistant replying to chat messages. "
    "Keep replies short, friendly, and conversational.",
)

# Security settings
BASIC_AUTH_USERNAME = os.getenv("BASIC_AUTH_USERNAME", "")
BASIC_AUTH_PASSWORD = os.getenv("BASIC_AUTH_PASSWORD", "")
# Optional second layer: if AUTH_HEADER_TOKEN is set, every request must
# carry the header named AUTH_HEADER_NAME with exactly this value.
AUTH_HEADER_NAME = os.getenv("AUTH_HEADER_NAME", "X-Auth-Token")
AUTH_HEADER_TOKEN = os.getenv("AUTH_HEADER_TOKEN", "")

# Behaviour settings
FALLBACK_MESSAGE = os.getenv(
    "FALLBACK_MESSAGE", "Sorry, I'm having trouble right now."
)
MAX_INPUT_CHARS = _env_int("MAX_INPUT_CHARS", 4000)  # trim huge incoming msgs
MAX_REPLY_CHARS = _env_int("MAX_REPLY_CHARS", 2000)  # trim huge replies
# Number of previous exchanges (user+assistant pairs) remembered per chat.
# 0 disables memory: every message is answered in isolation.
HISTORY_TURNS = _env_int("HISTORY_TURNS", 5)
HISTORY_MAX_CHATS = _env_int("HISTORY_MAX_CHATS", 500)

# Server settings
HOST = os.getenv("HOST", "127.0.0.1")
PORT = _env_int("PORT", 5000)
LOG_FILE = os.getenv("LOG_FILE", "bridge.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ---------------------------------------------------------------------------
# Logging: stdout + rotating file
# ---------------------------------------------------------------------------

logger = logging.getLogger("hermes_bridge")
logger.setLevel(LOG_LEVEL)
logger.propagate = False  # avoid duplicate lines via the root logger
_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

_stdout_handler = logging.StreamHandler(sys.stdout)
_stdout_handler.setFormatter(_formatter)
logger.addHandler(_stdout_handler)

if LOG_FILE:
    # 5 MB per file, keep 5 old files.
    _file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)

# ---------------------------------------------------------------------------
# Hermes client
# ---------------------------------------------------------------------------

# The openai client sends HERMES_API_KEY as "Authorization: Bearer <key>".
hermes = OpenAI(
    base_url=HERMES_BASE_URL,
    api_key=HERMES_API_KEY or "not-set",
    timeout=HERMES_TIMEOUT,
    max_retries=HERMES_MAX_RETRIES,
)

# ---------------------------------------------------------------------------
# Per-chat conversation memory (in-process, bounded, thread-safe)
# ---------------------------------------------------------------------------


class ConversationStore:
    """Keeps the last N exchanges for each chat, evicting the oldest chats."""

    def __init__(self, turns: int, max_chats: int) -> None:
        self.turns = turns
        self.max_chats = max_chats
        self._chats: OrderedDict[str, deque] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> list[dict]:
        if self.turns <= 0:
            return []
        with self._lock:
            history = self._chats.get(key)
            return list(history) if history else []

    def add(self, key: str, user_msg: str, assistant_msg: str) -> None:
        if self.turns <= 0:
            return
        with self._lock:
            history = self._chats.get(key)
            if history is None:
                history = deque(maxlen=self.turns * 2)
                self._chats[key] = history
            self._chats.move_to_end(key)
            history.append({"role": "user", "content": user_msg})
            history.append({"role": "assistant", "content": assistant_msg})
            while len(self._chats) > self.max_chats:
                self._chats.popitem(last=False)


conversations = ConversationStore(HISTORY_TURNS, HISTORY_MAX_CHATS)

# ---------------------------------------------------------------------------
# Flask app + authentication
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.json.ensure_ascii = False  # keep emoji / non-Latin text readable
auth = HTTPBasicAuth()


def _safe_equals(a: str, b: str) -> bool:
    """Constant-time string comparison (avoids timing attacks)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


@auth.verify_password
def verify_password(username: str, password: str) -> str | None:
    if not BASIC_AUTH_USERNAME or not BASIC_AUTH_PASSWORD:
        return None  # misconfigured: deny everything
    # Evaluate both comparisons so timing doesn't reveal which one failed.
    user_ok = _safe_equals(username or "", BASIC_AUTH_USERNAME)
    pass_ok = _safe_equals(password or "", BASIC_AUTH_PASSWORD)
    return username if (user_ok and pass_ok) else None


@auth.error_handler
def auth_error(status: int):
    logger.warning(
        "Rejected request from %s: bad or missing Basic Auth", _client_ip()
    )
    return jsonify({"error": "unauthorized"}), status


def _client_ip() -> str:
    # Cloudflare Tunnel / ngrok put the real client IP in these headers.
    return (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "unknown"
    )


def _header_token_ok() -> bool:
    """Second security layer. Passes automatically if no token is configured."""
    if not AUTH_HEADER_TOKEN:
        return True
    return _safe_equals(request.headers.get(AUTH_HEADER_NAME, ""), AUTH_HEADER_TOKEN)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def reply_payload(text: str):
    """Build the exact response format AutoResponder requires (always 200)."""
    return jsonify({"replies": [{"message": text}]}), 200


def ask_hermes(chat_key: str, user_content: str) -> str:
    """Send one message to Hermes and return its reply, or raise on failure."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(conversations.get(chat_key))
    messages.append({"role": "user", "content": user_content})

    response = hermes.chat.completions.create(model=HERMES_MODEL, messages=messages)

    if not response.choices:
        raise ValueError("Hermes returned no choices")
    reply = (response.choices[0].message.content or "").strip()
    if not reply:
        raise ValueError("Hermes returned an empty reply")

    conversations.add(chat_key, user_content, reply)
    return reply


@app.post("/webhook")
@auth.login_required
def webhook():
    if not _header_token_ok():
        logger.warning(
            "Rejected request from %s: bad or missing %s header",
            _client_ip(),
            AUTH_HEADER_NAME,
        )
        return jsonify({"error": "unauthorized"}), 401

    # --- Parse payload defensively; never fail with a non-200 from here on ---
    data = request.get_json(silent=True, force=True)
    if not isinstance(data, dict):
        logger.error("Invalid JSON body from %s", _client_ip())
        return reply_payload(FALLBACK_MESSAGE)

    query = data.get("query") if isinstance(data.get("query"), dict) else {}
    sender = str(query.get("sender") or "Unknown").strip()
    message = str(query.get("message") or "").strip()
    is_group = bool(query.get("isGroup", False))
    participant = str(query.get("groupParticipant") or "").strip()
    messenger = str(data.get("messengerPackageName") or "unknown")

    if not message:
        logger.warning("Empty message from sender=%r (%s)", sender, messenger)
        return reply_payload(FALLBACK_MESSAGE)

    message = message[:MAX_INPUT_CHARS]
    logger.info(
        "IN  | app=%s | sender=%r | group=%s | participant=%r | message=%r",
        messenger,
        sender,
        is_group,
        participant,
        message,
    )

    # One memory thread per chat (platform + chat name). In groups, tell the
    # model who actually spoke so it can address them correctly.
    chat_key = f"{messenger}|{sender}"
    if is_group and participant:
        user_content = f"[Group chat '{sender}'] {participant}: {message}"
    else:
        user_content = message

    try:
        reply = ask_hermes(chat_key, user_content)[:MAX_REPLY_CHARS]
    except OpenAIError as exc:  # timeouts, connection errors, 4xx/5xx
        logger.error("Hermes API error for sender=%r: %s", sender, exc)
        reply = FALLBACK_MESSAGE
    except Exception:  # anything unexpected: still answer the phone
        logger.exception("Unexpected error while generating reply for %r", sender)
        reply = FALLBACK_MESSAGE

    logger.info("OUT | sender=%r | reply=%r", sender, reply)
    return reply_payload(reply)


@app.get("/health")
def health():
    """Unauthenticated liveness check (handy for testing the tunnel URL)."""
    return jsonify({"status": "ok"}), 200


# Malformed requests that Flask itself rejects (e.g. wrong method) still get
# a JSON body; 404/405 only happen for requests not made by the app.
@app.errorhandler(404)
@app.errorhandler(405)
def not_found(err):
    return jsonify({"error": err.name}), err.code


@app.errorhandler(500)
def internal_error(err):
    # Last-resort safety net: if anything escapes the webhook, still return
    # a valid reply so the app's flow doesn't break.
    logger.exception("Unhandled server error: %s", err)
    if request.path == "/webhook":
        return reply_payload(FALLBACK_MESSAGE)
    return jsonify({"error": "internal server error"}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _check_config() -> None:
    if not BASIC_AUTH_USERNAME or not BASIC_AUTH_PASSWORD:
        logger.critical(
            "BASIC_AUTH_USERNAME and BASIC_AUTH_PASSWORD must be set in .env. "
            "Refusing to start an unprotected server."
        )
        sys.exit(1)
    if not HERMES_API_KEY:
        logger.warning("HERMES_API_KEY is empty; Hermes may reject requests.")
    if not AUTH_HEADER_TOKEN:
        logger.warning(
            "AUTH_HEADER_TOKEN is empty; %s header check is DISABLED.",
            AUTH_HEADER_NAME,
        )


if __name__ == "__main__":
    _check_config()
    logger.info(
        "Starting bridge on http://%s:%d  ->  Hermes at %s (model=%s)",
        HOST,
        PORT,
        HERMES_BASE_URL,
        HERMES_MODEL,
    )
    try:
        # Waitress: production-grade, multi-threaded, works on Windows/macOS/Linux.
        from waitress import serve

        serve(app, host=HOST, port=PORT, threads=8)
    except ImportError:
        logger.warning("waitress not installed; using Flask dev server.")
        app.run(host=HOST, port=PORT, threaded=True)
