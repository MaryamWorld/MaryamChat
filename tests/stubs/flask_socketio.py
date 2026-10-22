"""Minimal recording stand-in for Flask-SocketIO, used ONLY by tests/test_chatwave.py.

It lets the real handler functions in app.py run without a network transport, and records
every emit (event, payload, target, skip_sid) so tests can assert exactly who would receive what.
It does NOT test the real Socket.IO transport or the browser side.
"""
from flask import request

rooms = []  # (sid, room)


class _Server:
    def __init__(self):
        self.disconnected = []

    def disconnect(self, sid):
        self.disconnected.append(sid)


class SocketIO:
    def __init__(self, app=None, **kwargs):
        self.handlers, self.emitted, self.server = {}, [], _Server()

    def on(self, event, *a, **k):
        def deco(fn):
            self.handlers[event] = fn
            return fn
        return deco

    def emit(self, event, data=None, **kw):
        self.emitted.append((event, data, kw.get("to") or kw.get("room"), kw.get("skip_sid")))

    def run(self, *a, **k):
        raise RuntimeError("stub")


def join_room(room):
    rooms.append((request.sid, room))
