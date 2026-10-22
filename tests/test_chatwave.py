"""ChatWave tests. Run from the project folder:  python tests/test_chatwave.py

HTTP/auth/database tests use the real Flask app. Socket handler tests run the real handlers from
app.py against a recording stand-in for Flask-SocketIO (tests/stubs). They verify server-side
logic only, not the Socket.IO transport or browser WebRTC.
"""
import contextlib, json, os, re, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUBS = Path(__file__).resolve().parent / "stubs"
sys.path.insert(0, str(STUBS))
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_PATH=os.path.join(TMP, "test.db"), SECRET_KEY="test-secret-key")

import flask_socketio  # noqa: E402  (the stub)
import app as cw       # noqa: E402
from flask import request, session  # noqa: E402

app, sio = cw.app, cw.socketio


class Client:
    def __init__(self):
        self.c = app.test_client()

    def token(self):
        with self.c.session_transaction() as s:
            tok = s.get("csrf")
        if not tok:  # same as a browser: loading any page creates the token
            html = self.c.get("/login", follow_redirects=True).get_data(as_text=True)
            tok = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
        return tok

    def post(self, url, **kw):
        return self.c.post(url, headers={"X-CSRF-Token": self.token()}, **kw)

    def form(self, url, data):
        return self.c.post(url, data={**data, "csrf_token": self.token()})

    def register(self, name, pw="password123", display=""):
        return self.form("/register", dict(username=name, display_name=display, password=pw, confirm=pw))

    def login(self, name, pw="password123"):
        return self.form("/login", dict(username=name, password=pw))

    def api_post(self, url, payload):
        return self.post(url, json=payload)


def user(name, **kw):
    cl = Client()
    r = cl.register(name, **kw)
    assert r.status_code == 302, r.data
    return cl


def uid(name):
    with app.app_context():
        return cw.db().execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()["id"]


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.alice, cls.bob, cls.carol = user("alice"), user("bob"), user("carol")
        cls.ids = {n: uid(n) for n in ("alice", "bob", "carol")}

    def test_01_pages_load_and_protected(self):
        c = Client().c
        self.assertEqual(c.get("/login").status_code, 200)
        self.assertEqual(c.get("/register").status_code, 200)
        self.assertIn("/login", c.get("/").headers["Location"])
        self.assertIn("/login", c.get("/chat").headers["Location"])
        for path in ("/api/conversations", "/api/users?q=a", "/api/ice", "/api/conversations/1/messages"):
            self.assertEqual(c.get(path).status_code, 401, path)
        self.assertEqual(c.get("/static/css/style.css").status_code, 200)
        for f in ("main.js", "chat.js", "calls.js"):
            self.assertEqual(c.get(f"/static/js/{f}").status_code, 200)

    def test_02_registration_validation(self):
        cl = Client()
        self.assertEqual(cl.register("ab").status_code, 400)
        self.assertEqual(cl.register("bad name!").status_code, 400)
        self.assertEqual(cl.register("newuser", pw="short").status_code, 400)
        r = cl.form("/register", dict(username="newuser", password="password123", confirm="different1"))
        self.assertEqual(r.status_code, 400)
        self.assertIn(b"Passwords do not match", r.data)
        self.assertEqual(cl.register("ALICE").status_code, 409)  # unique, case-insensitive

    def test_03_password_is_hashed(self):
        with app.app_context():
            h = cw.db().execute("SELECT password_hash FROM users WHERE username='alice'").fetchone()[0]
        self.assertNotIn("password123", h)
        self.assertTrue(h.startswith(("scrypt:", "pbkdf2:")))

    def test_04_login_logout(self):
        cl = Client()
        self.assertEqual(cl.login("alice", "wrongpass1").status_code, 401)
        self.assertEqual(cl.login("nobody").status_code, 401)
        self.assertEqual(cl.login("alice").status_code, 302)
        self.assertEqual(cl.c.get("/chat").status_code, 200)
        cl.form("/logout", {})
        self.assertEqual(cl.c.get("/chat").status_code, 302)

    def test_05_login_throttle(self):
        cl = Client()
        user("victim")
        codes = [cl.login("victim", "badbadbad1").status_code for _ in range(6)]
        self.assertEqual(codes[:5], [401] * 5)
        self.assertEqual(codes[5], 429)
        self.assertEqual(cl.login("victim").status_code, 429)  # even the right password is held off

    def test_06_csrf(self):
        cl = Client()
        cl.login("alice")
        r = cl.c.post("/api/conversations", json={"user_id": 2})  # no token
        self.assertEqual(r.status_code, 400)
        r = cl.c.post("/login", data={"username": "alice", "password": "password123"})  # no token
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_07_xss_escaped_in_page(self):
        cl = user("xssuser", display="<script>alert(1)</script>")
        html = cl.c.get("/chat").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_08_search(self):
        r = self.alice.c.get("/api/users?q=bo").get_json()["users"]
        self.assertEqual([u["username"] for u in r], ["bob"])
        self.assertNotIn("password_hash", json.dumps(r))
        self.assertEqual(self.alice.c.get("/api/users?q=alice").get_json()["users"], [])  # self excluded
        self.assertEqual(self.alice.c.get("/api/users?q=").get_json()["users"], [])
        self.assertEqual(self.alice.c.get("/api/users?q=%25").get_json()["users"], [])  # '%' is literal

    def test_09_conversations_and_messages(self):
        a, b, ids = self.alice, self.bob, self.ids
        r = a.api_post("/api/conversations", {"user_id": ids["bob"]})
        cid = r.get_json()["conversation"]["id"]
        self.assertEqual(b.api_post("/api/conversations", {"user_id": ids["alice"]}).get_json()["conversation"]["id"], cid)
        self.assertEqual(a.api_post("/api/conversations", {"user_id": ids["alice"]}).status_code, 400)
        self.assertEqual(a.api_post("/api/conversations", {"user_id": 99999}).status_code, 404)
        self.assertEqual(a.api_post("/api/conversations", {"user_id": "1"}).status_code, 400)
        # sending
        self.assertEqual(a.api_post(f"/api/conversations/{cid}/messages", {"body": "   "}).status_code, 400)
        self.assertEqual(a.api_post(f"/api/conversations/{cid}/messages", {"body": "x" * 2001}).status_code, 400)
        sio.emitted.clear()
        r = a.api_post(f"/api/conversations/{cid}/messages",
                       {"body": "hello <b>bob</b>", "sender_id": ids["carol"]})  # forged sender
        self.assertEqual(r.status_code, 201)
        msg = r.get_json()["message"]
        self.assertEqual(msg["sender_id"], ids["alice"])
        targets = {t for ev, _, t, _ in sio.emitted if ev == "message:new"}
        self.assertEqual(targets, {f"user:{ids['bob']}", f"user:{ids['alice']}"})
        # unread + history + read marking
        convs = b.c.get("/api/conversations").get_json()["conversations"]
        self.assertEqual(convs[0]["unread"], 1)
        self.assertEqual(convs[0]["last_body"], "hello <b>bob</b>")
        hist = b.c.get(f"/api/conversations/{cid}/messages").get_json()
        self.assertEqual([m["body"] for m in hist["messages"]], ["hello <b>bob</b>"])
        self.assertEqual(b.c.get("/api/conversations").get_json()["conversations"][0]["unread"], 0)
        b.api_post(f"/api/conversations/{cid}/messages", {"body": "hi alice"})
        self.__class__.cid = cid

    def test_10_authorization(self):
        cid = self.cid
        c = self.carol
        self.assertEqual(c.c.get(f"/api/conversations/{cid}/messages").status_code, 404)
        self.assertEqual(c.api_post(f"/api/conversations/{cid}/messages", {"body": "intrude"}).status_code, 404)
        self.assertEqual(c.api_post(f"/api/conversations/{cid}/read", {}).status_code, 404)
        self.assertEqual(c.c.get("/api/conversations").get_json()["conversations"], [])

    def test_11_ordering_and_pagination(self):
        a, ids = self.alice, self.ids
        cid_c = a.api_post("/api/conversations", {"user_id": ids["carol"]}).get_json()["conversation"]["id"]
        a.api_post(f"/api/conversations/{cid_c}/messages", {"body": "to carol"})
        convs = a.c.get("/api/conversations").get_json()["conversations"]
        self.assertEqual(convs[0]["peer"]["username"], "carol")  # most recent first
        a.api_post(f"/api/conversations/{self.cid}/messages", {"body": "to bob again"})
        self.assertEqual(a.c.get("/api/conversations").get_json()["conversations"][0]["peer"]["username"], "bob")
        for i in range(4):
            a.api_post(f"/api/conversations/{self.cid}/messages", {"body": f"m{i}"})
        page = a.c.get(f"/api/conversations/{self.cid}/messages?limit=3").get_json()
        self.assertTrue(page["has_more"])
        older = a.c.get(f"/api/conversations/{self.cid}/messages?limit=100&before={page['messages'][0]['id']}").get_json()
        self.assertFalse(older["has_more"])
        self.assertLess(older["messages"][-1]["id"], page["messages"][0]["id"])

    def test_12_persistence_across_restart(self):
        code = """
import app, json, re
c = app.app.test_client()
tok = re.search(r'name="csrf-token" content="([^"]+)"', c.get('/login').get_data(as_text=True)).group(1)
assert c.post('/login', data={'username': 'bob', 'password': 'password123', 'csrf_token': tok}).status_code == 302
convs = c.get('/api/conversations').get_json()['conversations']
bodies = []
for cv in convs:
    bodies += [m['body'] for m in c.get('/api/conversations/%d/messages' % cv['id']).get_json()['messages']]
print(json.dumps(bodies))
"""
        env = {**os.environ, "PYTHONPATH": f"{STUBS}{os.pathsep}{ROOT}"}
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=ROOT)
        self.assertEqual(out.returncode, 0, out.stderr)
        bodies = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertIn("hello <b>bob</b>", bodies)
        self.assertIn("hi alice", bodies)

    def test_13_ice_config(self):
        r = self.alice.c.get("/api/ice").get_json()
        self.assertTrue(r["iceServers"][0]["urls"][0].startswith("stun:"))
        os.environ.update(TURN_URLS="turn:t.example.com:3478", TURN_SECRET="s3cret")
        try:
            r = self.alice.c.get("/api/ice").get_json()["iceServers"]
            self.assertEqual(r[1]["urls"], ["turn:t.example.com:3478"])
            self.assertRegex(r[1]["username"], r"^\d+:\d+$")
            self.assertTrue(r[1]["credential"])
        finally:
            del os.environ["TURN_URLS"], os.environ["TURN_SECRET"]


@contextlib.contextmanager
def as_sid(sid, user_id=None):
    ctx = app.test_request_context("/")
    ctx.push()
    request.sid = sid
    if user_id is not None:
        session["user_id"] = user_id
    try:
        yield
    finally:
        ctx.pop()


def fire(sid, event, data=None, user_id=None):
    with as_sid(sid, user_id):
        return sio.handlers[event](data) if data is not None else sio.handlers[event]()


def sent(event):
    return [(d, to, skip) for ev, d, to, skip in sio.emitted if ev == event]


class SocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for n in ("soca", "socb", "socc", "socd"):
            user(n)
        cls.u = {n: uid(n) for n in ("soca", "socb", "socc", "socd")}
        with app.app_context():
            for x, y in (("soca", "socb"), ("soca", "socc"), ("soca", "socd")):
                lo, hi = sorted((cls.u[x], cls.u[y]))
                cw.db().execute("INSERT INTO conversations (user_a,user_b,updated_at) VALUES (?,?,?)", (lo, hi, cw.now()))
            cw.db().commit()

    def setUp(self):
        sio.emitted.clear()
        flask_socketio.rooms.clear()
        cw.calls.clear(); cw.user_call.clear(); cw.sid_user.clear(); cw.user_sids.clear()
        u = self.u
        self.assertIsNone(fire("A1", "connect", user_id=u["soca"]))
        self.assertIsNone(fire("B1", "connect", user_id=u["socb"]))
        self.assertIsNone(fire("B2", "connect", user_id=u["socb"]))  # second tab
        self.assertIsNone(fire("C1", "connect", user_id=u["socc"]))
        sio.emitted.clear()

    def start_call(self, kind="video"):
        fire("A1", "call:invite", {"callee_id": self.u["socb"], "kind": kind})
        return sent("call:ringing")[0][0]["call_id"]

    def answer(self, cid):
        fire("B1", "call:accept", {"call_id": cid})

    def test_01_connect_rules(self):
        self.assertFalse(fire("X1", "connect"))  # no session -> rejected
        self.assertFalse(fire("X2", "connect", user_id=99999))  # unknown user -> rejected
        self.assertIn(("A1", f"user:{self.u['soca']}"), flask_socketio.rooms)
        self.assertTrue(cw.is_online(self.u["socb"]))
        self.assertFalse(cw.is_online(self.u["socd"]))

    def test_02_invite_goes_to_callee_with_server_side_identity(self):
        fire("A1", "call:invite", {"callee_id": self.u["socb"], "kind": "audio", "from": {"id": 999}})
        (inc, to, _), = sent("call:incoming")
        self.assertEqual(to, f"user:{self.u['socb']}")
        self.assertEqual(inc["from"]["username"], "soca")
        self.assertEqual(inc["kind"], "audio")
        self.assertEqual(sent("call:ringing")[0][1], "A1")

    def test_03_invite_validation(self):
        fire("A1", "call:invite", {"callee_id": self.u["soca"], "kind": "audio"})   # self
        fire("A1", "call:invite", {"callee_id": self.u["socb"], "kind": "hologram"})  # bad kind
        fire("A1", "call:invite", {"callee_id": "2", "kind": "audio"})            # bad type
        fire("C1", "call:invite", {"callee_id": self.u["socb"], "kind": "audio"})   # no conversation
        self.assertEqual(len(sent("call:error")), 4)
        self.assertEqual(sent("call:incoming"), [])
        fire("A1", "call:invite", {"callee_id": self.u["socd"], "kind": "audio"})   # offline callee
        self.assertEqual(len(sent("call:unavailable")), 1)

    def test_04_busy(self):
        self.start_call()
        sio.emitted.clear()
        fire("C1", "call:invite", {"callee_id": self.u["soca"], "kind": "audio"})  # caller of other call
        self.assertEqual(len(sent("call:busy")), 1)
        fire("A1", "call:invite", {"callee_id": self.u["socc"], "kind": "audio"})  # already in a call
        self.assertEqual(sent("call:error")[0][0]["message"], "You are already in a call.")

    def test_05_accept_and_signal_relay(self):
        cid = self.start_call()
        fire("A1", "call:accept", {"call_id": cid})          # caller can't accept own call
        self.assertEqual(sent("call:accepted"), [])
        fire("C1", "call:accept", {"call_id": cid})          # outsider can't accept
        self.assertEqual(sent("call:accepted"), [])
        self.answer(cid)
        self.assertEqual(sent("call:accepted")[0][1], "A1")
        el = [x for x in sent("call:ended") if x[0]["reason"] == "answered_elsewhere"]
        self.assertEqual((el[0][1], el[0][2]), (f"user:{self.u['socb']}", "B1"))  # other tabs dismissed
        sio.emitted.clear()
        offer = {"type": "offer", "sdp": "v=0"}
        fire("A1", "call:signal", {"call_id": cid, "data": offer})
        self.assertEqual(sent("call:signal"), [({"call_id": cid, "data": offer}, "B1", None)])
        sio.emitted.clear()
        fire("B1", "call:signal", {"call_id": cid, "data": {"type": "answer", "sdp": "x"}})
        self.assertEqual(sent("call:signal")[0][1], "A1")
        sio.emitted.clear()
        fire("C1", "call:signal", {"call_id": cid, "data": offer})   # outsider
        fire("B2", "call:signal", {"call_id": cid, "data": offer})   # callee's other tab
        fire("A1", "call:signal", {"call_id": cid, "data": {"type": "evil"}})
        fire("A1", "call:signal", {"call_id": cid, "data": {"type": "offer", "sdp": "x" * 30000}})
        fire("A1", "call:signal", {"call_id": "nope", "data": offer})
        self.assertEqual(sent("call:signal"), [])

    def test_06_signal_before_accept_is_dropped(self):
        cid = self.start_call()
        fire("A1", "call:signal", {"call_id": cid, "data": {"type": "offer", "sdp": "x"}})
        self.assertEqual(sent("call:signal"), [])

    def test_07_end_cleans_up(self):
        cid = self.start_call(); self.answer(cid); sio.emitted.clear()
        fire("B1", "call:end", {"call_id": cid})
        (p, to, _), = sent("call:ended")
        self.assertEqual((to, p["reason"]), ("A1", "ended"))
        self.assertEqual((cw.calls, cw.user_call), ({}, {}))
        cid2 = self.start_call()  # users are free to call again
        self.assertNotEqual(cid, cid2)

    def test_08_reject(self):
        cid = self.start_call(); sio.emitted.clear()
        fire("A1", "call:reject", {"call_id": cid})            # caller can't "reject"
        self.assertEqual(sent("call:ended"), [])
        fire("B2", "call:reject", {"call_id": cid})
        ended = sent("call:ended")
        self.assertIn(("A1", "rejected"), [(to, d["reason"]) for d, to, _ in ended])
        self.assertEqual(cw.calls, {})

    def test_09_cancel_while_ringing(self):
        cid = self.start_call(); sio.emitted.clear()
        fire("B1", "call:end", {"call_id": cid})               # callee tab can't end a ringing call
        self.assertEqual(sent("call:ended"), [])
        fire("A1", "call:end", {"call_id": cid})
        self.assertEqual([(to, d["reason"]) for d, to, _ in sent("call:ended")],
                         [(f"user:{self.u['socb']}", "cancelled")])  # all callee tabs dismissed
        self.assertEqual(cw.calls, {})

    def test_10_disconnect_during_call(self):
        cid = self.start_call(); self.answer(cid); sio.emitted.clear()
        fire("A1", "disconnect")
        self.assertEqual([(to, d["reason"]) for d, to, _ in sent("call:ended")], [("B1", "disconnected")])
        self.assertEqual((cw.calls, cw.user_call), ({}, {}))
        self.assertFalse(cw.is_online(self.u["soca"]))

    def test_11_caller_disconnect_while_ringing(self):
        self.start_call(); sio.emitted.clear()
        fire("A1", "disconnect")
        self.assertEqual(sent("call:ended")[0][1], f"user:{self.u['socb']}")
        self.assertEqual(cw.calls, {})

    def test_12_logout_drops_sockets(self):
        cl = Client(); cl.login("soca")
        with cw.app.app_context():
            pass
        flask_socketio_server = sio.server
        flask_socketio_server.disconnected.clear()
        with cl.c.session_transaction() as s:
            self.assertEqual(s["user_id"], self.u["soca"])
        cl.form("/logout", {})
        self.assertEqual(flask_socketio_server.disconnected, ["A1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
