"""Gatherly - Event Management & RSVP System (Flask + SQLite)."""
import csv
import io
import os
import secrets
import smtplib
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.environ.get("GATHERLY_DB", os.path.join(BASE_DIR, "gatherly.db"))
UPLOAD_DIR = os.environ.get("GATHERLY_UPLOADS", os.path.join(BASE_DIR, "uploads"))
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
STATUSES = {"going": "Going", "maybe": "Maybe", "not_going": "Not Going"}
MAX_GUESTS = 5
REMINDER_WINDOW_HOURS = 24

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-me-in-production")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB poster limit
os.makedirs(UPLOAD_DIR, exist_ok=True)
# Globals are visible inside imported macros (context-processor values are not)
app.jinja_env.globals["STATUSES"] = STATUSES


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    phone TEXT DEFAULT '',
    bio TEXT DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    venue TEXT NOT NULL,
    poster TEXT DEFAULT '',
    capacity INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rsvps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('going','maybe','not_going')),
    guests INTEGER NOT NULL DEFAULT 0,
    attended INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(event_id, user_id)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rsvps_event ON rsvps(event_id);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, is_read);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    with app.app_context():
        get_db().executescript(SCHEMA)
        admin_email = os.environ.get("ADMIN_EMAIL", "admin@gatherly.local").lower()
        admin_pw = os.environ.get("ADMIN_PASSWORD", "admin123")
        if not query("SELECT 1 FROM users WHERE role='admin'", one=True):
            execute(
                "INSERT INTO users (name,email,password_hash,role,created_at) VALUES (?,?,?,?,?)",
                ("Administrator", admin_email, generate_password_hash(admin_pw), "admin", now_str()),
            )
            print(f"[init] Admin account created: {admin_email} / {admin_pw}  (change it!)")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
EVENT_SELECT = """
SELECT e.*, u.name AS organizer,
 (SELECT COUNT(*) FROM rsvps r WHERE r.event_id=e.id AND r.status='going') AS going,
 (SELECT COUNT(*) FROM rsvps r WHERE r.event_id=e.id AND r.status='maybe') AS maybe,
 (SELECT COUNT(*) FROM rsvps r WHERE r.event_id=e.id AND r.status='not_going') AS not_going,
 (SELECT COALESCE(SUM(1+r.guests),0) FROM rsvps r WHERE r.event_id=e.id AND r.status='going') AS seats,
 (SELECT COUNT(*) FROM rsvps r WHERE r.event_id=e.id AND r.attended=1) AS checked_in
FROM events e JOIN users u ON u.id = e.created_by
"""


def event_dt(ev):
    return datetime.strptime(f"{ev['date']} {ev['time']}", "%Y-%m-%d %H:%M")


def is_past(ev):
    return event_dt(ev) < datetime.now()


def get_event(eid):
    return query(EVENT_SELECT + " WHERE e.id=?", (eid,), one=True)


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    if "user" not in g:
        g.user = query("SELECT * FROM users WHERE id=?", (uid,), one=True)
    return g.user


def can_manage(ev):
    u = current_user()
    return bool(u and (u["role"] == "admin" or u["id"] == ev["created_by"]))


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user():
            flash("Please log in to continue.", "info")
            return redirect(url_for("login", next=request.path))
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*a, **kw):
        if current_user()["role"] != "admin":
            abort(403)
        return fn(*a, **kw)
    return wrapper


def safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") else url_for("index")


def save_poster(file_storage):
    """Save an uploaded poster; return filename or None. Raises ValueError for bad files."""
    if not file_storage or not file_storage.filename:
        return None
    name = secure_filename(file_storage.filename)
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED_EXT:
        raise ValueError("Poster must be a PNG, JPG, GIF or WEBP image.")
    fname = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(UPLOAD_DIR, fname))
    return fname


def delete_poster(fname):
    if fname:
        try:
            os.remove(os.path.join(UPLOAD_DIR, fname))
        except OSError:
            pass


# --------------------------------------------------------------------------
# Notifications (in-app + optional email)
# --------------------------------------------------------------------------
def send_email(to_addr, subject, body):
    host = os.environ.get("SMTP_HOST")
    if not host:
        print(f"[email:not-configured] to={to_addr} subject={subject!r}")
        return False
    try:
        msg = EmailMessage()
        msg["From"] = os.environ.get("SMTP_FROM", "Gatherly <no-reply@gatherly.local>")
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=15) as s:
            if os.environ.get("SMTP_TLS", "1") == "1":
                s.starttls()
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)
        return True
    except Exception as exc:  # never break the request because of email
        print(f"[email:error] {exc}")
        return False


def notify(user_id, event_id, kind, message, subject=None):
    execute(
        "INSERT INTO notifications (user_id,event_id,kind,message,created_at) VALUES (?,?,?,?,?)",
        (user_id, event_id, kind, message, now_str()),
    )
    user = query("SELECT email,name FROM users WHERE id=?", (user_id,), one=True)
    if user:
        send_email(user["email"], subject or "Gatherly notification",
                   f"Hi {user['name']},\n\n{message}\n\n- Gatherly")


def pretty_when(ev):
    return event_dt(ev).strftime("%a, %d %b %Y at %I:%M %p")


def notify_attendees(ev, kind, message, subject, statuses=("going", "maybe")):
    marks = ",".join("?" * len(statuses))
    rows = query(f"SELECT user_id FROM rsvps WHERE event_id=? AND status IN ({marks})",
                 (ev["id"], *statuses))
    for r in rows:
        notify(r["user_id"], ev["id"], kind, message, subject)
    return len(rows)


def reminder_worker():
    """Background thread: send a reminder once per attendee ~24h before an event."""
    while True:
        try:
            with app.app_context():
                now = datetime.now()
                limit = now + timedelta(hours=REMINDER_WINDOW_HOURS)
                events = query(EVENT_SELECT + " WHERE (e.date||' '||e.time) BETWEEN ? AND ?",
                               (now.strftime("%Y-%m-%d %H:%M"), limit.strftime("%Y-%m-%d %H:%M")))
                for ev in events:
                    rows = query(
                        """SELECT user_id FROM rsvps r WHERE event_id=? AND status IN ('going','maybe')
                           AND NOT EXISTS (SELECT 1 FROM notifications n WHERE n.user_id=r.user_id
                                           AND n.event_id=r.event_id AND n.kind='reminder')""",
                        (ev["id"],))
                    for r in rows:
                        notify(r["user_id"], ev["id"], "reminder",
                               f"Reminder: \"{ev['title']}\" is coming up on {pretty_when(ev)} at {ev['venue']}.",
                               f"Reminder: {ev['title']}")
        except Exception as exc:
            print(f"[reminder-worker] {exc}")
        time.sleep(60)


# --------------------------------------------------------------------------
# CSRF + template helpers
# --------------------------------------------------------------------------
@app.before_request
def csrf_protect():
    if request.method == "POST":
        token = session.get("_csrf")
        if not token or token != request.form.get("_csrf"):
            abort(400, "Invalid or missing CSRF token. Go back, refresh and try again.")


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.context_processor
def inject_globals():
    user = current_user()
    unread = 0
    if user:
        unread = query("SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0",
                       (user["id"],), one=True)["c"]
    return dict(csrf_token=csrf_token, me=user, unread_count=unread,
                STATUSES=STATUSES, is_past=is_past, can_manage=can_manage,
                MAX_GUESTS=MAX_GUESTS)


@app.template_filter("fdate")
def fdate(s):
    return datetime.strptime(s, "%Y-%m-%d").strftime("%a, %d %b %Y")


@app.template_filter("fday")
def fday(s):
    return datetime.strptime(s, "%Y-%m-%d").strftime("%d")


@app.template_filter("fmon")
def fmon(s):
    return datetime.strptime(s, "%Y-%m-%d").strftime("%b")


@app.template_filter("ftime")
def ftime(s):
    return datetime.strptime(s, "%H:%M").strftime("%I:%M %p").lstrip("0")


@app.template_filter("fstamp")
def fstamp(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").strftime("%d %b %Y, %I:%M %p")


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", code=403, msg="You don't have permission to do that."), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, msg="We couldn't find that page."), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", code=400, msg=getattr(e, "description", "Bad request.")), 400


@app.errorhandler(413)
def too_large(_e):
    return render_template("error.html", code=413, msg="That file is too large (max 5 MB)."), 413


# --------------------------------------------------------------------------
# Authentication & profile
# --------------------------------------------------------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("index"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        pw = request.form.get("password", "")
        phone = request.form.get("phone", "").strip()
        errors = []
        if len(name) < 2:
            errors.append("Please enter your name.")
        if "@" not in email or "." not in email.split("@")[-1]:
            errors.append("Please enter a valid email address.")
        if len(pw) < 6:
            errors.append("Password must be at least 6 characters.")
        if pw != request.form.get("confirm", ""):
            errors.append("Passwords do not match.")
        if not errors and query("SELECT 1 FROM users WHERE email=?", (email,), one=True):
            errors.append("An account with that email already exists.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("register.html", form=request.form), 400
        cur = execute("INSERT INTO users (name,email,password_hash,phone,created_at) VALUES (?,?,?,?,?)",
                      (name, email, generate_password_hash(pw), phone, now_str()))
        session.clear()
        session["uid"] = cur.lastrowid
        notify(cur.lastrowid, None, "welcome", f"Welcome to Gatherly, {name}! Browse events and RSVP.",
               "Welcome to Gatherly")
        flash("Account created. Welcome!", "success")
        return redirect(url_for("index"))
    return render_template("register.html", form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("index"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = query("SELECT * FROM users WHERE email=?", (email,), one=True)
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            nxt = request.args.get("next")
            session.clear()
            session["uid"] = user["id"]
            flash(f"Welcome back, {user['name']}!", "success")
            return redirect(safe_next(nxt))
        flash("Incorrect email or password.", "error")
        return render_template("login.html", form=request.form), 401
    return render_template("login.html", form={})


@app.post("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = current_user()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        errors = []
        if len(name) < 2:
            errors.append("Name is too short.")
        if "@" not in email:
            errors.append("Enter a valid email.")
        other = query("SELECT 1 FROM users WHERE email=? AND id<>?", (email, user["id"]), one=True)
        if other:
            errors.append("That email is used by another account.")
        new_pw = request.form.get("new_password", "")
        if new_pw:
            if not check_password_hash(user["password_hash"], request.form.get("current_password", "")):
                errors.append("Current password is incorrect.")
            if len(new_pw) < 6:
                errors.append("New password must be at least 6 characters.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("profile.html", form=request.form), 400
        execute("UPDATE users SET name=?, email=?, phone=?, bio=? WHERE id=?",
                (name, email, request.form.get("phone", "").strip(),
                 request.form.get("bio", "").strip()[:500], user["id"]))
        if new_pw:
            execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new_pw), user["id"]))
        flash("Profile updated.", "success")
        return redirect(url_for("profile"))
    stats = query_one_stats(user["id"])
    return render_template("profile.html", form=user, stats=stats)


def query_one_stats(uid):
    return {
        "going": query("SELECT COUNT(*) c FROM rsvps WHERE user_id=? AND status='going'", (uid,), one=True)["c"],
        "attended": query("SELECT COUNT(*) c FROM rsvps WHERE user_id=? AND attended=1", (uid,), one=True)["c"],
        "created": query("SELECT COUNT(*) c FROM events WHERE created_by=?", (uid,), one=True)["c"],
    }


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------
@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    view = request.args.get("view", "upcoming")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    sql = EVENT_SELECT + " WHERE 1=1"
    args = []
    if view == "past":
        sql += " AND (e.date||' '||e.time) < ?"
        order = " ORDER BY e.date DESC, e.time DESC"
    else:
        view = "upcoming"
        sql += " AND (e.date||' '||e.time) >= ?"
        order = " ORDER BY e.date ASC, e.time ASC"
    args.append(now)
    if q:
        sql += " AND (e.title LIKE ? OR e.venue LIKE ? OR e.description LIKE ?)"
        args += [f"%{q}%"] * 3
    events = query(sql + order, args)
    my_rsvps = {}
    if current_user():
        for r in query("SELECT event_id,status FROM rsvps WHERE user_id=?", (current_user()["id"],)):
            my_rsvps[r["event_id"]] = r["status"]
    return render_template("index.html", events=events, q=q, view=view, my_rsvps=my_rsvps)


def read_event_form(existing=None):
    f = request.form
    data = {
        "title": f.get("title", "").strip(),
        "description": f.get("description", "").strip(),
        "date": f.get("date", "").strip(),
        "time": f.get("time", "").strip(),
        "venue": f.get("venue", "").strip(),
        "capacity": f.get("capacity", "0").strip() or "0",
    }
    errors = []
    if len(data["title"]) < 3:
        errors.append("Event name must be at least 3 characters.")
    if not data["venue"]:
        errors.append("Venue is required.")
    try:
        datetime.strptime(data["date"], "%Y-%m-%d")
        datetime.strptime(data["time"], "%H:%M")
        if existing is None and event_dt(data) < datetime.now():
            errors.append("Event date and time must be in the future.")
    except ValueError:
        errors.append("Please provide a valid date and time.")
    try:
        data["capacity"] = int(data["capacity"])
        if data["capacity"] < 0:
            raise ValueError
    except ValueError:
        errors.append("Capacity must be 0 (unlimited) or a positive number.")
    return data, errors


@app.route("/events/new", methods=["GET", "POST"])
@login_required
def event_new():
    if request.method == "POST":
        data, errors = read_event_form()
        poster = None
        try:
            poster = save_poster(request.files.get("poster"))
        except ValueError as exc:
            errors.append(str(exc))
        if errors:
            delete_poster(poster)
            for e in errors:
                flash(e, "error")
            return render_template("event_form.html", ev=data, mode="new"), 400
        cur = execute(
            """INSERT INTO events (title,description,date,time,venue,poster,capacity,created_by,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (data["title"], data["description"], data["date"], data["time"], data["venue"],
             poster or "", data["capacity"], current_user()["id"], now_str()))
        notify(current_user()["id"], cur.lastrowid, "event_created",
               f"Your event \"{data['title']}\" has been created.", "Event created")
        flash("Event created!", "success")
        return redirect(url_for("event_detail", eid=cur.lastrowid))
    return render_template("event_form.html", ev={}, mode="new")


@app.route("/events/<int:eid>")
def event_detail(eid):
    ev = get_event(eid) or abort(404)
    my = None
    if current_user():
        my = query("SELECT * FROM rsvps WHERE event_id=? AND user_id=?", (eid, current_user()["id"]), one=True)
    spots_left = None
    if ev["capacity"] > 0:
        spots_left = max(ev["capacity"] - ev["seats"], 0)
    return render_template("event_detail.html", ev=ev, my=my, spots_left=spots_left,
                           when_iso=event_dt(ev).isoformat())


@app.route("/events/<int:eid>/edit", methods=["GET", "POST"])
@login_required
def event_edit(eid):
    ev = get_event(eid) or abort(404)
    if not can_manage(ev):
        abort(403)
    if request.method == "POST":
        data, errors = read_event_form(existing=ev)
        if data["capacity"] and isinstance(data["capacity"], int) and 0 < data["capacity"] < ev["seats"]:
            errors.append(f"Capacity can't be lower than the {ev['seats']} seats already confirmed.")
        new_poster = None
        try:
            new_poster = save_poster(request.files.get("poster"))
        except ValueError as exc:
            errors.append(str(exc))
        if errors:
            delete_poster(new_poster)
            for e in errors:
                flash(e, "error")
            data["id"], data["poster"] = eid, ev["poster"]
            return render_template("event_form.html", ev=data, mode="edit"), 400
        poster = ev["poster"]
        if request.form.get("remove_poster") and not new_poster:
            delete_poster(poster)
            poster = ""
        if new_poster:
            delete_poster(poster)
            poster = new_poster
        execute("""UPDATE events SET title=?,description=?,date=?,time=?,venue=?,poster=?,capacity=? WHERE id=?""",
                (data["title"], data["description"], data["date"], data["time"], data["venue"],
                 poster, data["capacity"], eid))
        changed = any(str(ev[k]) != str(data[k]) for k in ("date", "time", "venue", "title"))
        if changed:
            fresh = get_event(eid)
            notify_attendees(fresh, "event_updated",
                             f"Update: \"{fresh['title']}\" details changed. It's now on {pretty_when(fresh)} at {fresh['venue']}.",
                             f"Event updated: {fresh['title']}")
        flash("Event updated.", "success")
        return redirect(url_for("event_detail", eid=eid))
    return render_template("event_form.html", ev=ev, mode="edit")


@app.post("/events/<int:eid>/delete")
@login_required
def event_delete(eid):
    ev = get_event(eid) or abort(404)
    if not can_manage(ev):
        abort(403)
    notify_attendees(ev, "event_cancelled",
                     f"\"{ev['title']}\" scheduled for {pretty_when(ev)} has been cancelled.",
                     f"Event cancelled: {ev['title']}", statuses=("going", "maybe", "not_going"))
    delete_poster(ev["poster"])
    execute("DELETE FROM events WHERE id=?", (eid,))
    flash("Event deleted and attendees notified.", "success")
    return redirect(url_for("admin_dashboard") if current_user()["role"] == "admin" else url_for("my_events"))


@app.route("/uploads/<path:filename>")
def uploads(filename):
    return send_from_directory(UPLOAD_DIR, filename)


# --------------------------------------------------------------------------
# RSVP
# --------------------------------------------------------------------------
@app.post("/events/<int:eid>/rsvp")
@login_required
def rsvp(eid):
    ev = get_event(eid) or abort(404)
    user = current_user()
    status = request.form.get("status")
    if status not in STATUSES:
        abort(400, "Invalid RSVP choice.")
    if is_past(ev):
        flash("This event has already taken place.", "error")
        return redirect(url_for("event_detail", eid=eid))
    try:
        guests = max(0, min(MAX_GUESTS, int(request.form.get("guests", 0) or 0)))
    except ValueError:
        guests = 0
    if status != "going":
        guests = 0
    existing = query("SELECT * FROM rsvps WHERE event_id=? AND user_id=?", (eid, user["id"]), one=True)
    if status == "going" and ev["capacity"] > 0:
        used = ev["seats"] - ((1 + existing["guests"]) if existing and existing["status"] == "going" else 0)
        left = ev["capacity"] - used
        if 1 + guests > left:
            msg = "Sorry, this event is full." if left <= 0 else f"Only {left} spot(s) left - reduce your guest count."
            flash(msg, "error")
            return redirect(url_for("event_detail", eid=eid))
    ts = now_str()
    execute("""INSERT INTO rsvps (event_id,user_id,status,guests,created_at,updated_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(event_id,user_id) DO UPDATE SET status=excluded.status,
               guests=excluded.guests, updated_at=excluded.updated_at""",
            (eid, user["id"], status, guests, ts, ts))
    label = STATUSES[status]
    extra = f" (+{guests} guest{'s' if guests != 1 else ''})" if guests else ""
    notify(user["id"], eid, "rsvp_confirmation",
           f"RSVP confirmed: you responded \"{label}\"{extra} for \"{ev['title']}\" on {pretty_when(ev)} at {ev['venue']}.",
           f"RSVP confirmation: {ev['title']}")
    flash(f"Your RSVP is saved: {label}{extra}.", "success")
    return redirect(url_for("event_detail", eid=eid))


@app.route("/my-events")
@login_required
def my_events():
    uid = current_user()["id"]
    attending = query(EVENT_SELECT + " WHERE e.id IN (SELECT event_id FROM rsvps WHERE user_id=?)"
                      " ORDER BY e.date ASC, e.time ASC", (uid,))
    mine = {r["event_id"]: r for r in query("SELECT * FROM rsvps WHERE user_id=?", (uid,))}
    hosting = query(EVENT_SELECT + " WHERE e.created_by=? ORDER BY e.date ASC, e.time ASC", (uid,))
    return render_template("my_events.html", attending=attending, hosting=hosting, mine=mine)


# --------------------------------------------------------------------------
# Guest management
# --------------------------------------------------------------------------
def guest_rows(eid, status=None):
    sql = """SELECT r.*, u.name, u.email, u.phone FROM rsvps r JOIN users u ON u.id=r.user_id
             WHERE r.event_id=?"""
    args = [eid]
    if status in STATUSES:
        sql += " AND r.status=?"
        args.append(status)
    return query(sql + " ORDER BY r.status, u.name", args)


@app.route("/events/<int:eid>/guests")
@login_required
def guests(eid):
    ev = get_event(eid) or abort(404)
    if not can_manage(ev):
        abort(403)
    status = request.args.get("status", "")
    return render_template("guests.html", ev=ev, rows=guest_rows(eid, status), status=status)


@app.route("/events/<int:eid>/guests.csv")
@login_required
def guests_csv(eid):
    ev = get_event(eid) or abort(404)
    if not can_manage(ev):
        abort(403)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Name", "Email", "Phone", "RSVP", "Extra guests", "Checked in", "Responded at"])
    for r in guest_rows(eid):
        w.writerow([r["name"], r["email"], r["phone"], STATUSES[r["status"]], r["guests"],
                    "Yes" if r["attended"] else "No", r["updated_at"]])
    fname = secure_filename(ev["title"]) or "event"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}_guests.csv"})


@app.post("/events/<int:eid>/checkin/<int:uid>")
@login_required
def checkin(eid, uid):
    ev = get_event(eid) or abort(404)
    if not can_manage(ev):
        abort(403)
    r = query("SELECT * FROM rsvps WHERE event_id=? AND user_id=?", (eid, uid), one=True) or abort(404)
    execute("UPDATE rsvps SET attended=? WHERE id=?", (0 if r["attended"] else 1, r["id"]))
    return redirect(request.form.get("back") or url_for("guests", eid=eid))


@app.post("/events/<int:eid>/announce")
@login_required
def announce(eid):
    ev = get_event(eid) or abort(404)
    if not can_manage(ev):
        abort(403)
    kind = request.form.get("kind", "announcement")
    if kind == "reminder":
        msg = f"Reminder: \"{ev['title']}\" is on {pretty_when(ev)} at {ev['venue']}. See you there!"
        subject = f"Reminder: {ev['title']}"
        notice = "reminder"
    else:
        text = request.form.get("message", "").strip()
        if not text:
            flash("Write a message first.", "error")
            return redirect(url_for("guests", eid=eid))
        msg = f"Message from the organizer of \"{ev['title']}\": {text[:500]}"
        subject = f"Update from {ev['title']}"
        notice = "announcement"
    n = notify_attendees(ev, notice, msg, subject)
    flash(f"Sent to {n} guest(s) (Going + Maybe).", "success")
    return redirect(url_for("guests", eid=eid))


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
@app.route("/notifications")
@login_required
def notifications():
    rows = query("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 100",
                 (current_user()["id"],))
    return render_template("notifications.html", rows=rows)


@app.post("/notifications/read")
@login_required
def notifications_read():
    execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (current_user()["id"],))
    return redirect(url_for("notifications"))


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------
@app.route("/admin")
@admin_required
def admin_dashboard():
    c = lambda sql: query(sql, one=True)["c"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    going = c("SELECT COUNT(*) c FROM rsvps WHERE status='going'")
    attended = c("SELECT COUNT(*) c FROM rsvps WHERE attended=1")
    stats = {
        "users": c("SELECT COUNT(*) c FROM users"),
        "events": c("SELECT COUNT(*) c FROM events"),
        "upcoming": query("SELECT COUNT(*) c FROM events WHERE (date||' '||time) >= ?", (now,), one=True)["c"],
        "rsvps": c("SELECT COUNT(*) c FROM rsvps"),
        "going": going,
        "maybe": c("SELECT COUNT(*) c FROM rsvps WHERE status='maybe'"),
        "not_going": c("SELECT COUNT(*) c FROM rsvps WHERE status='not_going'"),
        "attended": attended,
        "rate": round(100 * attended / going) if going else 0,
    }
    events = query(EVENT_SELECT + " ORDER BY e.date DESC, e.time DESC")
    recent = query("""SELECT r.*, u.name, e.title FROM rsvps r JOIN users u ON u.id=r.user_id
                      JOIN events e ON e.id=r.event_id ORDER BY r.updated_at DESC LIMIT 8""")
    return render_template("admin_dashboard.html", stats=stats, events=events, recent=recent)


@app.route("/admin/users")
@admin_required
def admin_users():
    q = request.args.get("q", "").strip()
    sql = """SELECT u.*, (SELECT COUNT(*) FROM rsvps r WHERE r.user_id=u.id) AS rsvp_count,
             (SELECT COUNT(*) FROM rsvps r WHERE r.user_id=u.id AND r.attended=1) AS attended_count,
             (SELECT COUNT(*) FROM events e WHERE e.created_by=u.id) AS event_count FROM users u"""
    args = ()
    if q:
        sql += " WHERE u.name LIKE ? OR u.email LIKE ?"
        args = (f"%{q}%", f"%{q}%")
    return render_template("admin_users.html", users=query(sql + " ORDER BY u.id", args), q=q)


@app.post("/admin/users/<int:uid>/role")
@admin_required
def admin_user_role(uid):
    if uid == current_user()["id"]:
        flash("You can't change your own role.", "error")
    else:
        u = query("SELECT * FROM users WHERE id=?", (uid,), one=True) or abort(404)
        new_role = "user" if u["role"] == "admin" else "admin"
        execute("UPDATE users SET role=? WHERE id=?", (new_role, uid))
        flash(f"{u['name']} is now {new_role}.", "success")
    return redirect(url_for("admin_users"))


@app.post("/admin/users/<int:uid>/delete")
@admin_required
def admin_user_delete(uid):
    if uid == current_user()["id"]:
        flash("You can't delete your own account.", "error")
    else:
        u = query("SELECT * FROM users WHERE id=?", (uid,), one=True) or abort(404)
        for ev in query("SELECT poster FROM events WHERE created_by=?", (uid,)):
            delete_poster(ev["poster"])
        execute("DELETE FROM users WHERE id=?", (uid,))
        flash(f"Deleted {u['name']}.", "success")
    return redirect(url_for("admin_users"))


# --------------------------------------------------------------------------
# Demo data + entry point
# --------------------------------------------------------------------------
def seed_demo():
    init_db()
    with app.app_context():
        if query("SELECT 1 FROM users WHERE email='alice@example.com'", one=True):
            print("Demo data already present.")
            return
        ts = now_str()
        ids = []
        for name, email in [("Alice Rao", "alice@example.com"), ("Bala Krishna", "bala@example.com"),
                            ("Chitra Devi", "chitra@example.com"), ("Dev Patel", "dev@example.com")]:
            cur = execute("INSERT INTO users (name,email,password_hash,created_at) VALUES (?,?,?,?)",
                          (name, email, generate_password_hash("password"), ts))
            ids.append(cur.lastrowid)
        base = datetime.now()
        demo = [
            ("Tech Meetup 2026", "Lightning talks, snacks and networking for local developers.", 3, "18:30", "Innovation Hub, Rajahmundry", 40),
            ("Diwali Cultural Night", "Music, dance and food. Bring your family!", 10, "19:00", "Town Hall Auditorium", 150),
            ("Weekend Photography Walk", "Golden-hour photo walk along the Godavari.", 1, "16:00", "Pushkar Ghat", 15),
            ("Startup Pitch Day", "Ten startups pitch to local investors.", 21, "10:00", "Convention Centre", 0),
        ]
        for i, (t, d, days, tm, v, cap) in enumerate(demo):
            date = (base + timedelta(days=days)).strftime("%Y-%m-%d")
            cur = execute("""INSERT INTO events (title,description,date,time,venue,capacity,created_by,created_at)
                             VALUES (?,?,?,?,?,?,?,?)""", (t, d, date, tm, v, cap, ids[i % 2], ts))
            for j, uid in enumerate(ids):
                st = ["going", "maybe", "not_going", "going"][(i + j) % 4]
                execute("INSERT OR IGNORE INTO rsvps (event_id,user_id,status,guests,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                        (cur.lastrowid, uid, st, 1 if st == "going" and j == 0 else 0, ts, ts))
        print("Demo data added. Users: alice@example.com, bala@example.com, ... (password: password)")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "seed":
        seed_demo()
        sys.exit(0)
    init_db()
    threading.Thread(target=reminder_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1", use_reloader=False)
