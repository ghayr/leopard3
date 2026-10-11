"""
Optional helper: open an ngrok tunnel to the bridge using pyngrok.

Start app.py first (in another terminal), then run:  python run_ngrok.py
Requires NGROK_AUTHTOKEN in .env (free at https://dashboard.ngrok.com).
"""

import os
import time
from pathlib import Path

from dotenv import load_dotenv
from pyngrok import conf, ngrok

load_dotenv(Path(__file__).resolve().with_name(".env"))

port = int(os.getenv("PORT", 5000))
token = os.getenv("NGROK_AUTHTOKEN")
if token:
    conf.get_default().auth_token = token

# NGROK_DOMAIN: your free static domain (e.g. my-name.ngrok-free.app), so the
# URL in the Android app never changes between restarts. Optional.
domain = os.getenv("NGROK_DOMAIN") or None
tunnel = ngrok.connect(port, "http", domain=domain) if domain else ngrok.connect(port, "http")

print(f"\nPublic URL : {tunnel.public_url}")
print(f"Webhook URL: {tunnel.public_url}/webhook   <- paste this into AutoResponder\n")
print("Press Ctrl+C to stop.")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    ngrok.kill()
