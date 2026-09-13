@echo off
rem Exposes the locally running app (run.cmd must already be running) on a public
rem https://....trycloudflare.com URL. The URL is printed below and changes on every start.
rem Remote visitors must sign in like everyone else. Close this window to take the app offline.
cd /d "%~dp0"
if not exist data\users.json (
  echo No users exist yet - create an admin first:  python manage.py create ^<name^> admin
  pause
  exit /b 1
)
where cloudflared >nul 2>nul || (
  echo cloudflared is not installed. Install it with:  winget install Cloudflare.cloudflared
  pause
  exit /b 1
)
echo Starting public tunnel to http://127.0.0.1:8765 ... keep this window open.
echo Look for the https://....trycloudflare.com line below.
cloudflared tunnel --url http://127.0.0.1:8765
pause
