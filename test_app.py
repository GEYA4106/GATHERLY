"""End-to-end smoke tests: python test_app.py"""
import io
import os
import re
import tempfile
from datetime import datetime, timedelta

tmp = tempfile.mkdtemp()
os.environ["GATHERLY_DB"] = os.path.join(tmp, "test.db")
os.environ["GATHERLY_UPLOADS"] = os.path.join(tmp, "uploads")

import app as eh  # noqa: E402

eh.init_db()


def client():
    return eh.app.test_client()


def token(c, path="/login"):
    for p in (path, "/login", "/register", "/profile", "/"):
        html = c.get(p).get_data(as_text=True)
        m = re.search(r'name="_csrf" value="([^"]+)"', html)
        if m:
            return m.group(1)
    raise AssertionError("no CSRF token found on any page")


def post(c, path, data=None, tok=None, **kw):
    data = dict(data or {})
    data["_csrf"] = tok or token(c, "/")
    return c.post(path, data=data, follow_redirects=True, **kw)


def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    assert cond, msg


# --- auth ---
c = client()
ok(c.get("/").status_code == 200, "home page loads")
r = c.post("/register", data={"name": "A", "email": "bad", "password": "1"})
ok(r.status_code == 400, "register without CSRF token rejected")
tok = token(c, "/register")
r = c.post("/register", data={"_csrf": tok, "name": "Alice", "email": "alice@t.com", "password": "secret1", "confirm": "nope"})
ok(r.status_code == 400, "register validates password confirmation")
r = c.post("/register", data={"_csrf": tok, "name": "Alice", "email": "alice@t.com", "password": "secret1", "confirm": "secret1"}, follow_redirects=True)
ok(b"Account created" in r.data, "register works")
ok(b"Welcome to Gatherly" in c.get("/notifications").data, "welcome notification created")
r = post(c, "/profile", {"name": "Alice R", "email": "alice@t.com", "phone": "999", "bio": "hi"})
ok(b"Profile updated" in r.data, "profile update works")
post(c, "/logout")
r = post(c, "/login", {"email": "alice@t.com", "password": "wrong"})
ok(b"Incorrect email or password" in r.data, "bad login rejected")
r = post(c, "/login", {"email": "alice@t.com", "password": "secret1"})
ok(b"Welcome back" in r.data, "login works")

# --- event creation with poster ---
d = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
png = b"\x89PNG\r\n\x1a\n" + b"0" * 50
r = post(c, "/events/new", {"title": "Launch Party", "date": d, "time": "18:00", "venue": "Hall A",
                            "description": "Fun!", "capacity": "3",
                            "poster": (io.BytesIO(png), "poster.png")}, content_type="multipart/form-data")
ok(b"Event created" in r.data and b"Launch Party" in r.data, "event created with poster")
ev = eh.app.test_request_context()
with eh.app.app_context():
    row = eh.query("SELECT * FROM events", one=True)
    eid = row["id"]
    ok(bool(row["poster"]) and os.path.exists(os.path.join(eh.UPLOAD_DIR, row["poster"])), "poster saved to disk")
ok(c.get(f"/uploads/{row['poster']}").status_code == 200, "poster is served")
r = post(c, "/events/new", {"title": "x", "date": "2000-01-01", "time": "10:00", "venue": ""}, content_type="multipart/form-data")
ok(r.status_code == 200 and b"Event name must be" in r.data or b"future" in r.data, "invalid event rejected")
r = post(c, "/events/new", {"title": "Bad file", "date": d, "time": "10:00", "venue": "V",
                            "poster": (io.BytesIO(b"x"), "evil.exe")}, content_type="multipart/form-data")
ok(b"must be a PNG" in r.data, "non-image poster rejected")

# --- RSVP ---
ok(b"Launch Party" in c.get(f"/events/{eid}").data, "event detail loads")
r = post(c, f"/events/{eid}/rsvp", {"status": "going", "guests": "1"})
ok(b"Going" in r.data and b"+1" in r.data, "RSVP going with +1")
ok(b"RSVP confirmed" in c.get("/notifications").data, "RSVP confirmation notification")
r = post(c, f"/events/{eid}/rsvp", {"status": "going", "guests": "5"})
ok(b"spot" in r.data or b"full" in r.data, "capacity enforced")
r = post(c, f"/events/{eid}/rsvp", {"status": "maybe"})
ok(b"Maybe" in r.data, "RSVP changed to maybe")
ok(b"Launch Party" in c.get("/my-events").data, "my events lists RSVP")

# second user
b = client()
tok = token(b, "/register")
b.post("/register", data={"_csrf": tok, "name": "Bob", "email": "bob@t.com", "password": "secret1", "confirm": "secret1"})
post(b, f"/events/{eid}/rsvp", {"status": "going", "guests": "0"})
r = b.get(f"/events/{eid}/guests")
ok(r.status_code == 403, "non-owner cannot view guest list")
ok(b.get("/admin").status_code == 403, "non-admin blocked from admin")

# --- guest management ---
r = c.get(f"/events/{eid}/guests")
ok(b"Bob" in r.data and b"Guest list" in r.data, "owner sees guest list")
ok(b"Bob" in c.get(f"/events/{eid}/guests?status=going").data, "guest list filter")
csv = c.get(f"/events/{eid}/guests.csv")
ok(csv.mimetype == "text/csv" and b"bob@t.com" in csv.data, "CSV export")
with eh.app.app_context():
    bob_id = eh.query("SELECT id FROM users WHERE email='bob@t.com'", one=True)["id"]
r = post(c, f"/events/{eid}/checkin/{bob_id}")
ok(b"Checked in" in r.data, "check-in toggle")
r = post(c, f"/events/{eid}/announce", {"kind": "announcement", "message": "Doors at 5"})
ok(b"Sent to 2" in r.data, "announcement sent to going+maybe")
ok(b"Doors at 5" in b.get("/notifications").data, "guest receives announcement")
r = post(c, f"/events/{eid}/announce", {"kind": "reminder"})
ok(b"Sent to" in r.data, "manual reminder")

# --- reminder worker logic (event within 24h) ---
soon = datetime.now() + timedelta(hours=3)
with eh.app.app_context():
    uid = eh.query("SELECT id FROM users WHERE email='alice@t.com'", one=True)["id"]
    cur = eh.execute("INSERT INTO events (title,date,time,venue,capacity,created_by,created_at) VALUES (?,?,?,?,?,?,?)",
                     ("Soon Event", soon.strftime("%Y-%m-%d"), soon.strftime("%H:%M"), "V", 0, uid, eh.now_str()))
    eh.execute("INSERT INTO rsvps (event_id,user_id,status,guests,created_at,updated_at) VALUES (?,?,?,?,?,?)",
               (cur.lastrowid, bob_id, "going", 0, eh.now_str(), eh.now_str()))
    soon_id = cur.lastrowid
import threading, time  # noqa: E402
t = threading.Thread(target=eh.reminder_worker, daemon=True)
t.start()
time.sleep(1.5)
with eh.app.app_context():
    n = eh.query("SELECT COUNT(*) c FROM notifications WHERE event_id=? AND kind='reminder'", (soon_id,), one=True)["c"]
ok(n == 1, "automatic reminder sent exactly once")

# --- edit & notify ---
r = post(c, f"/events/{eid}/edit", {"title": "Launch Party", "date": d, "time": "19:00", "venue": "Hall B",
                                    "description": "Fun!", "capacity": "10"}, content_type="multipart/form-data")
ok(b"Event updated" in r.data, "event edit")
ok(b"details changed" in b.get("/notifications").data, "update notification sent")

# --- admin ---
a = client()
post(a, "/login", {"email": "admin@gatherly.local", "password": "admin123"}, tok=token(a))
r = a.get("/admin")
ok(r.status_code == 200 and b"Admin dashboard" in r.data and b"Launch Party" in r.data, "admin dashboard")
ok(a.get("/admin/users").status_code == 200, "admin users page")
ok(b"Bob" in a.get("/admin/users?q=bob").data, "admin user search")
r = post(a, f"/admin/users/{bob_id}/role")
ok(b"is now admin" in r.data, "admin can promote user")
ok(a.get(f"/events/{eid}/guests").status_code == 200, "admin can manage any event")

# --- delete ---
r = post(c, f"/events/{eid}/delete")
ok(b"Event deleted" in r.data, "event delete")
ok(b"cancelled" in b.get("/notifications").data, "cancellation notification sent")
ok(c.get(f"/events/{eid}").status_code == 404, "deleted event is gone")

# --- seed + all pages render ---
for path in ["/", "/?view=past", "/?q=party", "/my-events", "/profile", "/notifications"]:
    ok(c.get(path).status_code == 200, f"GET {path}")
anon = client()
for path in ["/login", "/register", "/"]:
    ok(anon.get(path).status_code == 200, f"anonymous GET {path}")
ok(anon.get("/my-events").status_code == 302, "anonymous redirected to login")
print("\nALL TESTS PASSED")
os._exit(0)
