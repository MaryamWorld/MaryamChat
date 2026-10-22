# ChatWave

A small WhatsApp-style web app: registration/login, real-time text chat with persistent history, and
browser-to-browser audio and video calls (WebRTC).

**Stack:** Flask · Flask-SocketIO · SQLite · vanilla JavaScript · WebRTC

## Features

- Register, log in, log out (hashed passwords, signed session cookies, CSRF protection, login throttling)
- Search registered users and start conversations; most recent conversations first; unread badges
- Real-time messaging; every message is stored in SQLite and survives restarts
- Older-message paging, timestamps, day separators, empty/loading/error states, reconnect re-sync
- Audio and video calls: incoming-call popup, accept/decline, ringing/connecting/connected states,
  mute, camera toggle, end call, connection-failure handling, full cleanup of camera/mic when a call ends
- Responsive layout (desktop two-pane, mobile single-pane) with automatic dark mode
- Online indicator (green dot) next to users who are currently connected

## Run it on Windows (Visual Studio Code)

Open the `ChatWave` folder in VS Code, open a terminal (**Terminal > New Terminal**, PowerShell), then:

```powershell
py -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python app.py
```

Open **http://localhost:5000**.

- If activation is blocked ("running scripts is disabled"), run
  `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` and activate again.
- The database (`instance/chatwave.db`) and a random secret key (`instance/.secret_key`) are created automatically.
- The chat page loads the Socket.IO browser client from cdnjs.cloudflare.com, so the browser needs internet access.

### Trying it with two accounts

Browser tabs in the same profile **share one login**. Use two different browsers (e.g. Chrome and Edge),
or one normal window plus one private/incognito window. Register `alice` in one and `bob` in the other,
search for the other user, and start chatting or calling.

### Calls: what you need to know

- Browsers only allow camera/microphone on **https://** pages or **http://localhost**. Calls therefore work on
  `http://localhost:5000` out of the box, but **not** at `http://192.168.x.x:5000` from another device.
  For another device on your network, serve HTTPS: create a certificate (for example with
  [mkcert](https://github.com/FiloSottile/mkcert)), then set `SSL_CERT`, `SSL_KEY` and `HOST=0.0.0.0` in `.env`.
- Allow the browser's microphone/camera prompt when asked.
- STUN (default: Google's public servers) is enough for many home networks. **For reliable calls across the
  internet you need a TURN relay**, because strict NATs, mobile networks and corporate firewalls block direct
  connections. ChatWave is already wired for it; you only have to run a TURN server and fill in `.env`:

  ```
  TURN_URLS=turn:turn.example.com:3478,turns:turn.example.com:5349
  TURN_SECRET=<same value as static-auth-secret in coturn>
  ```

  Minimal coturn config (`/etc/turnserver.conf`) matching `TURN_SECRET`:

  ```
  listening-port=3478
  tls-listening-port=5349
  use-auth-secret
  static-auth-secret=<same secret>
  realm=turn.example.com
  cert=/etc/letsencrypt/live/turn.example.com/fullchain.pem
  pkey=/etc/letsencrypt/live/turn.example.com/privkey.pem
  ```

  (Or use static `TURN_USERNAME` / `TURN_CREDENTIAL` instead of `TURN_SECRET`.)

## Run the tests

```powershell
python tests/test_chatwave.py
```

This runs 25 tests against the real Flask app and database (auth, validation, CSRF, authorization, message
history, persistence across process restarts, ICE config) and against the real Socket.IO call-signaling handlers
using a small recording stand-in for Flask-SocketIO (`tests/stubs`). They do not exercise the real Socket.IO
network transport or browser WebRTC.

## Production notes

- Serve over **HTTPS** behind a reverse proxy (nginx/Caddy) that forwards WebSocket upgrades; set
  `SESSION_COOKIE_SECURE=1`, a strong `SECRET_KEY`, and `ALLOWED_ORIGINS` if the public origin differs.
- Do not use the built-in Werkzeug server for real traffic. On Linux, for example:
  `pip install gunicorn` then `gunicorn -w 1 --threads 100 -b 127.0.0.1:5000 app:app`.
- **Run exactly one worker process.** Presence and active-call state are kept in memory. Scaling to several
  processes would require a Socket.IO message queue (Redis) and moving that state out of process memory.
- Messages are protected by TLS in transit and access control on the server, but they are stored in plain text
  in the database. ChatWave does **not** provide end-to-end encryption. WebRTC media is encrypted (DTLS-SRTP)
  between the browsers.
- Back up `instance/chatwave.db`. Never commit `.env` or the `instance/` folder.

## Troubleshooting

| Problem | Fix |
|---|---|
| `py` is not recognized | Install Python 3.10+ from python.org and tick "Add python.exe to PATH"; reopen the terminal. |
| `ModuleNotFoundError: flask_socketio` | The virtual environment isn't active or dependencies aren't installed: activate it and run `pip install -r requirements.txt`. |
| Port 5000 already in use | Set `PORT=5001` in `.env` and open `http://localhost:5001`. |
| Red "Connection lost" banner | The server stopped, or the page is on a different origin than the server (set `ALLOWED_ORIGINS`). |
| "Calls need a secure connection" | Open the site through `http://localhost:<port>` or HTTPS (see above). |
| "Permission was denied" | Click the camera/lock icon in the address bar and allow microphone/camera. |
| "No microphone or camera was found" | Connect a device or pick an audio-only call; check Windows privacy settings for camera/mic. |
| Call stays on "Connecting…" then fails | The two networks can't connect directly. Configure a TURN server (above). |
| Messages arrive only after refresh | The Socket.IO client script failed to load (offline or blocked CDN); check the browser console. |
| Forgot to log out / want a clean slate | Stop the app and delete `instance/chatwave.db`; it is recreated on the next start. |
