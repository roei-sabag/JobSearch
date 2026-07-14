@echo off
echo Launching Autonomous Resume System...
echo Server will listen on 0.0.0.0:8000 (all network interfaces, including Tailscale).
echo Reachable from other Tailscale devices at http://[YOUR_TAILSCALE_IP]:8000/
start "" "http://127.0.0.1:8000/"
call .venv\Scripts\activate
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
pause


