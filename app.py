"""ChatWave backend: Flask + Flask-SocketIO + SQLite.

HTTP (REST) handles authentication, search, conversations and message storage.
Socket.IO handles real-time message delivery and WebRTC call signaling.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from flask_socketio import SocketIO, join_room
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)
DB_PATH = os.environ.get("DATABASE_PATH") or str(INSTANCE_DIR / "chatwave.db")

MAX_MESSAGE_LEN = 2000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
DEFAULT_STUN = "stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302"
DUMMY_HASH = generate_password_hash("not-a-real-password")


def load_secret_key():
    """Use SECRET_KEY from the environment, else create and persist a random one."""
    key = (os.environ.get("SECRET_KEY") or "").strip()
    if key and not key.startswith("change-me"):
        return key
    key_file = INSTANCE_DIR / ".secret_key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(32)
    key_file.write_text(key)
    return key


app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    MAX_CONTENT_LENGTH=64 * 1024,
)

_origins = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins=_origins or None)

# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_a INTEGER NOT NULL REFERENCES users(id),
    user_b INTEGER NOT NULL REFERENCES users(id),
    updated_at TEXT NOT NULL,
    UNIQUE (user_a, user_b),
    CHECK (user_a < user_b)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    sender_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages (conversation_id, id);
"""


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


init_db()

# --------------------------------------------------------------------------- #
# Security helpers: CSRF, auth, headers
# --------------------------------------------------------------------------- #


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def load_user_and_check_csrf():
    if request.endpoint == "static":
        return None
    g.user = None
    uid = session.get("user_id")
    if uid is not None:
        g.user = db().execute(
            "SELECT id, username, display_name FROM users WHERE id = ?", (uid,)).fetchone()
        if g.user is None:
            session.clear()
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
        expected = session.get("csrf", "")
        if not sent or not expected or not hmac.compare_digest(sent.encode(), expected.encode()):
            if request.path.startswith("/api/"):
                return jsonify(error="Invalid or missing security token. Reload the page."), 400
            flash("Your session expired. Please try again.", "error")
            return redirect(url_for("login"))
    return None


@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(self), microphone=(self)"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; "
        "style-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
        "connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    if request.path.startswith("/api/") or request.path in ("/chat", "/login", "/register"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                return jsonify(error="Authentication required."), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


_failed_logins = {}


def _login_throttled(key):
    cutoff = time.time() - 300
    recent = [t for t in _failed_logins.get(key, []) if t > cutoff]
    _failed_logins[key] = recent
    return len(recent) >= 5


# --------------------------------------------------------------------------- #
# Pages and authentication
# --------------------------------------------------------------------------- #


@app.get("/")
def index():
    return redirect(url_for("chat" if g.user else "login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("chat"))
    if request.method == "GET":
        return render_template("register.html", errors=[], form={})
    username = request.form.get("username", "").strip()
    display_name = request.form.get("display_name", "").strip() or username
    password = request.form.get("password", "")
    confirm = request.form.get("confirm", "")
    errors = []
    if not USERNAME_RE.match(username):
        errors.append("Username must be 3-20 characters: letters, numbers or underscores.")
    if len(display_name) > 40:
        errors.append("Display name must be 40 characters or fewer.")
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    elif len(password) > 128:
        errors.append("Password must be 128 characters or fewer.")
    if password != confirm:
        errors.append("Passwords do not match.")
    status = 400
    if not errors:
        try:
            cur = db().execute(
                "INSERT INTO users (username, display_name, password_hash, created_at) VALUES (?,?,?,?)",
                (username, display_name, generate_password_hash(password), now()))
            db().commit()
        except sqlite3.IntegrityError:
            errors.append("That username is already taken.")
            status = 409
        else:
            session.clear()
            session.permanent = True
            session["user_id"] = cur.lastrowid
            return redirect(url_for("chat"))
    return render_template("register.html", errors=errors,
                           form={"username": username, "display_name": display_name}), status


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("chat"))
    if request.method == "GET":
        return render_template("login.html", error=None, username="")
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    key = (request.remote_addr, username.lower())
    if _login_throttled(key):
        return render_template("login.html", username=username,
                               error="Too many failed attempts. Try again in a few minutes."), 429
    row = db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    # Always run a hash check so response time doesn't reveal whether the user exists.
    ok = check_password_hash(row["password_hash"] if row else DUMMY_HASH, password)
    if not (row and ok):
        _failed_logins.setdefault(key, []).append(time.time())
        return render_template("login.html", username=username,
                               error="Invalid username or password."), 401
    _failed_logins.pop(key, None)
    session.clear()
    session.permanent = True
    session["user_id"] = row["id"]
    return redirect(url_for("chat"))


@app.post("/logout")
def logout():
    uid = session.get("user_id")
    session.clear()
    if uid is not None:  # drop any open real-time connections for this user
        with state_lock:
            sids = list(user_sids.get(uid, ()))
        for sid in sids:
            try:
                socketio.server.disconnect(sid)
            except Exception:
                pass
    return redirect(url_for("login"))


@app.get("/chat")
@login_required
def chat():
    return render_template("chat.html", user=g.user)


# --------------------------------------------------------------------------- #
# JSON API
# --------------------------------------------------------------------------- #


def public_user(row):
    return {"id": row["id"], "username": row["username"],
            "display_name": row["display_name"], "online": is_online(row["id"])}


def _like_escape(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@app.get("/api/users")
@login_required
def search_users():
    q = request.args.get("q", "").strip()[:40]
    if not q:
        return jsonify(users=[])
    pattern = f"%{_like_escape(q)}%"
    rows = db().execute(
        "SELECT id, username, display_name FROM users WHERE id != ? "
        "AND (username LIKE ? ESCAPE '\\' OR display_name LIKE ? ESCAPE '\\') "
        "ORDER BY username LIMIT 20", (g.user["id"], pattern, pattern)).fetchall()
    return jsonify(users=[public_user(r) for r in rows])


def fetch_conversations(user_id, conversation_id=None):
    sql = """
    SELECT c.id, c.updated_at, u.id AS peer_id, u.username, u.display_name,
      (SELECT body FROM messages m WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_body,
      (SELECT created_at FROM messages m WHERE m.conversation_id = c.id ORDER BY m.id DESC LIMIT 1) AS last_at,
      (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id
         AND m.sender_id != :me AND m.read_at IS NULL) AS unread
    FROM conversations c
    JOIN users u ON u.id = CASE WHEN c.user_a = :me THEN c.user_b ELSE c.user_a END
    WHERE (c.user_a = :me OR c.user_b = :me) AND (:cid IS NULL OR c.id = :cid)
    ORDER BY c.updated_at DESC"""
    rows = db().execute(sql, {"me": user_id, "cid": conversation_id}).fetchall()
    return [{"id": r["id"], "updated_at": r["updated_at"], "last_body": r["last_body"],
             "last_at": r["last_at"], "unread": r["unread"],
             "peer": {"id": r["peer_id"], "username": r["username"],
                      "display_name": r["display_name"], "online": is_online(r["peer_id"])}}
            for r in rows]


def member_conversation(conversation_id):
    """Return the conversation row only if the current user belongs to it."""
    return db().execute(
        "SELECT * FROM conversations WHERE id = ? AND (user_a = ? OR user_b = ?)",
        (conversation_id, g.user["id"], g.user["id"])).fetchone()


@app.get("/api/conversations")
@login_required
def list_conversations():
    return jsonify(conversations=fetch_conversations(g.user["id"]))


@app.post("/api/conversations")
@login_required
def start_conversation():
    data = request.get_json(silent=True) or {}
    peer_id = data.get("user_id")
    if not isinstance(peer_id, int) or isinstance(peer_id, bool):
        return jsonify(error="A valid user is required."), 400
    if peer_id == g.user["id"]:
        return jsonify(error="You cannot start a conversation with yourself."), 400
    if not db().execute("SELECT 1 FROM users WHERE id = ?", (peer_id,)).fetchone():
        return jsonify(error="User not found."), 404
    a, b = sorted((g.user["id"], peer_id))
    db().execute("INSERT OR IGNORE INTO conversations (user_a, user_b, updated_at) VALUES (?,?,?)",
                 (a, b, now()))
    db().commit()
    cid = db().execute("SELECT id FROM conversations WHERE user_a = ? AND user_b = ?",
                       (a, b)).fetchone()["id"]
    return jsonify(conversation=fetch_conversations(g.user["id"], cid)[0])


def message_dict(row):
    return {"id": row["id"], "conversation_id": row["conversation_id"],
            "sender_id": row["sender_id"], "body": row["body"], "created_at": row["created_at"]}


@app.get("/api/conversations/<int:cid>/messages")
@login_required
def get_messages(cid):
    if not member_conversation(cid):
        return jsonify(error="Conversation not found."), 404
    before = request.args.get("before", type=int)
    limit = min(max(request.args.get("limit", 50, type=int), 1), 100)
    params = [cid]
    sql = "SELECT * FROM messages WHERE conversation_id = ?"
    if before:
        sql += " AND id < ?"
        params.append(before)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit + 1)
    rows = db().execute(sql, params).fetchall()
    has_more = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    db().execute("UPDATE messages SET read_at = ? WHERE conversation_id = ? AND sender_id != ? "
                 "AND read_at IS NULL", (now(), cid, g.user["id"]))
    db().commit()
    return jsonify(messages=[message_dict(r) for r in rows], has_more=has_more)


@app.post("/api/conversations/<int:cid>/read")
@login_required
def mark_read(cid):
    if not member_conversation(cid):
        return jsonify(error="Conversation not found."), 404
    db().execute("UPDATE messages SET read_at = ? WHERE conversation_id = ? AND sender_id != ? "
                 "AND read_at IS NULL", (now(), cid, g.user["id"]))
    db().commit()
    return jsonify(ok=True)


@app.post("/api/conversations/<int:cid>/messages")
@login_required
def send_message(cid):
    conv = member_conversation(cid)
    if not conv:
        return jsonify(error="Conversation not found."), 404
    data = request.get_json(silent=True) or {}
    body = data.get("body")
    if not isinstance(body, str) or not body.strip():
        return jsonify(error="Message cannot be empty."), 400
    body = body.strip()
    if len(body) > MAX_MESSAGE_LEN:
        return jsonify(error=f"Message is too long (max {MAX_MESSAGE_LEN} characters)."), 400
    ts = now()
    cur = db().execute(
        "INSERT INTO messages (conversation_id, sender_id, body, created_at) VALUES (?,?,?,?)",
        (cid, g.user["id"], body, ts))  # sender always comes from the session, never the client
    db().execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (ts, cid))
    db().commit()
    msg = {"id": cur.lastrowid, "conversation_id": cid, "sender_id": g.user["id"],
           "body": body, "created_at": ts}
    peer = conv["user_b"] if conv["user_a"] == g.user["id"] else conv["user_a"]
    for uid in (peer, g.user["id"]):  # sender's other tabs get it too; clients de-duplicate by id
        socketio.emit("message:new", msg, to=f"user:{uid}")
    return jsonify(message=msg), 201


@app.get("/api/ice")
@login_required
def ice_servers():
    stun = [u.strip() for u in os.environ.get("STUN_URLS", DEFAULT_STUN).split(",") if u.strip()]
    servers = [{"urls": stun}] if stun else []
    turn = [u.strip() for u in os.environ.get("TURN_URLS", "").split(",") if u.strip()]
    if turn:
        secret = os.environ.get("TURN_SECRET", "")
        if secret:  # coturn "use-auth-secret" time-limited credentials
            username = f"{int(time.time()) + 3600}:{g.user['id']}"
            digest = hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
            credential = base64.b64encode(digest).decode()
        else:
            username = os.environ.get("TURN_USERNAME", "")
            credential = os.environ.get("TURN_CREDENTIAL", "")
        servers.append({"urls": turn, "username": username, "credential": credential})
    return jsonify(iceServers=servers)


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return jsonify(error="Not found."), 404
    return render_template("login.html", error="Page not found.", username=""), 404


@app.errorhandler(413)
def too_large(_e):
    return jsonify(error="Request too large."), 413


# --------------------------------------------------------------------------- #
# Real-time: presence, message rooms and call signaling
# --------------------------------------------------------------------------- #
state_lock = threading.RLock()
sid_user = {}    # socket id -> user id
user_sids = {}   # user id -> set of socket ids (one per open tab)
calls = {}       # call id -> call state
user_call = {}   # user id -> call id (a user can be in only one call)


def is_online(user_id):
    return user_id in user_sids


def _user_row(user_id):
    return db().execute("SELECT id, username, display_name FROM users WHERE id = ?",
                        (user_id,)).fetchone()


def _conversation_exists(a, b):
    lo, hi = sorted((a, b))
    return db().execute("SELECT 1 FROM conversations WHERE user_a = ? AND user_b = ?",
                        (lo, hi)).fetchone() is not None


@socketio.on("connect")
def on_connect(auth=None):
    uid = session.get("user_id")
    if uid is None or _user_row(uid) is None:
        return False  # reject unauthenticated sockets
    sid = request.sid
    with state_lock:
        sid_user[sid] = uid
        user_sids.setdefault(uid, set()).add(sid)
    join_room(f"user:{uid}")
    return None


@socketio.on("disconnect")
def on_disconnect(*_args):
    sid = request.sid
    with state_lock:
        uid = sid_user.pop(sid, None)
        if uid is None:
            return
        sids = user_sids.get(uid, set())
        sids.discard(sid)
        if not sids:
            user_sids.pop(uid, None)
        call = calls.get(user_call.get(uid))
        if call:
            if sid in (call["caller_sid"], call["callee_sid"]):
                _finish_call(call["id"], "cancelled" if call["state"] == "ringing" else "disconnected",
                             skip_sid=sid)
            elif uid == call["callee"] and call["state"] == "ringing" and uid not in user_sids:
                _finish_call(call["id"], "cancelled", skip_sid=sid)


def _finish_call(call_id, reason, skip_sid=None):
    """Remove a call and tell everyone involved. Caller must hold state_lock."""
    call = calls.pop(call_id, None)
    if not call:
        return
    for uid in (call["caller"], call["callee"]):
        if user_call.get(uid) == call_id:
            del user_call[uid]
    payload = {"call_id": call_id, "reason": reason}
    for sid in (call["caller_sid"], call["callee_sid"]):
        if sid and sid != skip_sid:
            socketio.emit("call:ended", payload, to=sid)
    if call["state"] == "ringing":  # dismiss the incoming-call popup on all callee tabs
        socketio.emit("call:ended", payload, to=f"user:{call['callee']}", skip_sid=skip_sid)


def _call_error(message):
    socketio.emit("call:error", {"message": message}, to=request.sid)


def _my_call(data, expected_state=None):
    """Return (uid, call) if this socket is a legitimate participant of the call."""
    uid = sid_user.get(request.sid)
    if uid is None or not isinstance(data, dict):
        return None, None
    call = calls.get(data.get("call_id"))
    if not call or uid not in (call["caller"], call["callee"]):
        return uid, None
    if expected_state and call["state"] != expected_state:
        return uid, None
    return uid, call


@socketio.on("call:invite")
def on_call_invite(data):
    uid = sid_user.get(request.sid)
    if uid is None or not isinstance(data, dict):
        return
    kind, callee = data.get("kind"), data.get("callee_id")
    if kind not in ("audio", "video") or not isinstance(callee, int) \
            or isinstance(callee, bool) or callee == uid:
        return _call_error("Invalid call request.")
    if not _conversation_exists(uid, callee):
        return _call_error("You can only call people you have a conversation with.")
    caller_row = _user_row(uid)
    with state_lock:
        if uid in user_call:
            return _call_error("You are already in a call.")
        if not is_online(callee):
            return socketio.emit("call:unavailable", {"callee_id": callee}, to=request.sid)
        if callee in user_call:
            return socketio.emit("call:busy", {"callee_id": callee}, to=request.sid)
        call_id = uuid.uuid4().hex
        calls[call_id] = {"id": call_id, "caller": uid, "callee": callee, "kind": kind,
                          "state": "ringing", "caller_sid": request.sid, "callee_sid": None}
        user_call[uid] = user_call[callee] = call_id
        socketio.emit("call:incoming", {
            "call_id": call_id, "kind": kind,
            "from": {"id": uid, "username": caller_row["username"],
                     "display_name": caller_row["display_name"]}}, to=f"user:{callee}")
        socketio.emit("call:ringing", {"call_id": call_id}, to=request.sid)


@socketio.on("call:accept")
def on_call_accept(data):
    with state_lock:
        uid, call = _my_call(data, "ringing")
        if not call or uid != call["callee"]:
            return
        call["state"] = "active"
        call["callee_sid"] = request.sid
        socketio.emit("call:accepted", {"call_id": call["id"]}, to=call["caller_sid"])
        socketio.emit("call:ended", {"call_id": call["id"], "reason": "answered_elsewhere"},
                      to=f"user:{uid}", skip_sid=request.sid)


@socketio.on("call:reject")
def on_call_reject(data):
    with state_lock:
        uid, call = _my_call(data, "ringing")
        if call and uid == call["callee"]:
            _finish_call(call["id"], "rejected", skip_sid=request.sid)


@socketio.on("call:signal")
def on_call_signal(data):
    with state_lock:
        uid, call = _my_call(data, "active")
        if not call:
            return
        payload = data.get("data")
        if not isinstance(payload, dict) or payload.get("type") not in ("offer", "answer", "candidate") \
                or len(json.dumps(payload)) > 20000:
            return
        # Only relay between the two sockets that own this call (no impersonation / hijacking).
        if request.sid == call["caller_sid"]:
            target = call["callee_sid"]
        elif request.sid == call["callee_sid"]:
            target = call["caller_sid"]
        else:
            return
        socketio.emit("call:signal", {"call_id": call["id"], "data": payload}, to=target)


@socketio.on("call:end")
def on_call_end(data):
    with state_lock:
        uid, call = _my_call(data)
        if not call:
            return
        if call["state"] == "ringing":
            # Caller may cancel; a callee tab that has not answered may not "end" it (use reject).
            if uid != call["caller"]:
                return
            _finish_call(call["id"], "cancelled", skip_sid=request.sid)
        elif request.sid in (call["caller_sid"], call["callee_sid"]):
            _finish_call(call["id"], "ended", skip_sid=request.sid)


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    ssl_kwargs = {}
    if os.environ.get("SSL_CERT") and os.environ.get("SSL_KEY"):
        ssl_kwargs["ssl_context"] = (os.environ["SSL_CERT"], os.environ["SSL_KEY"])
    print(f"ChatWave running at {'https' if ssl_kwargs else 'http'}://{'localhost' if host == '127.0.0.1' else host}:{port}")
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True, **ssl_kwargs)
