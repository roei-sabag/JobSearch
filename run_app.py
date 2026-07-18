"""
run_app.py
-----------
One-click launcher for the Autonomous Resume System.

Starts the FastAPI/Uvicorn server and automatically opens the default
web browser to the running application, so the user never has to
manually open a terminal, activate the virtual environment, run
uvicorn, or type the URL themselves.

Usage:
    python run_app.py
"""

import sys
import asyncio

# --- Windows-specific asyncio event loop policy fix ---
# See api/main.py for the full explanation: on Windows, the default
# SelectorEventLoop does not support asyncio subprocess creation, which
# breaks Playwright's Chromium launch during PDF rendering. Force the
# ProactorEventLoop here too, before uvicorn/anything else starts a loop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import threading
import time
import webbrowser

import uvicorn


# NOTE (Tailscale remote-access support): changed from "127.0.0.1" to
# "0.0.0.0" so uvicorn listens on ALL of the machine's network interfaces
# (not just the loopback interface), including the Tailscale virtual
# network adapter. This is what makes the API reachable from other devices
# on the same Tailscale tailnet (e.g. a phone) using the machine's
# Tailscale IP, not just from the machine itself via "localhost".
HOST = "0.0.0.0"
PORT = 8000

# The auto-opened local browser tab should still always use 127.0.0.1 (not
# 0.0.0.0, which is not a connectable address from a browser) for a fast,
# reliable local dev experience -- binding to 0.0.0.0 does not change what
# URL is used for that convenience browser launch.
URL = f"http://127.0.0.1:{PORT}/"


def open_browser():
    """
    Wait briefly for the server to bind to the port, then open the
    default web browser to the application's URL.
    """
    time.sleep(1.5)
    try:
        webbrowser.open(URL)
        print(f"Opened browser at {URL}")
    except Exception as exc:  # pragma: no cover - defensive
        print(f"Could not open browser automatically: {exc}")
        print(f"Please navigate to {URL} manually.")


def main():
    print("=" * 60)
    print("  Launching Autonomous Resume System...")
    print(f"  Server listening on: {HOST}:{PORT} (all network interfaces)")
    print(f"  Local browser will open at: {URL}")
    print("  Reachable from other Tailscale devices (e.g. your phone) at:")
    print("  http://[YOUR_TAILSCALE_IP]:8000/  (see `tailscale ip -4` for your IP)")
    print("=" * 60)

    # Open the browser in a background thread so it doesn't block
    # uvicorn's startup, and doesn't get blocked by uvicorn either.
    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()

    try:
        uvicorn.run(
            "api.main:app",
            host=HOST,
            port=PORT,
            reload=True,
        )
    except KeyboardInterrupt:
        print("\nServer stopped by user.")
    except Exception as exc:
        print(f"\nFailed to start server: {exc}")


if __name__ == "__main__":
    main()
