# Gatherly — Event Management & RSVP System

Gatherly is a full-stack web app for creating events, collecting RSVPs, managing guests and sending notifications. It runs on **Python (Flask) + SQLite**, and a small **Node.js/npm launcher** lets you start everything with one command.

---

## Features

| Area | What it does |
|---|---|
| **User authentication** | Sign up, log in and out, hashed passwords, CSRF protection, and a profile page (name, email, phone, bio, change password) |
| **Event creation** | Event name, date, time, venue, description, capacity and **poster upload** (PNG, JPG, GIF or WEBP, up to 5 MB, with live preview). Edit and delete your own events |
| **RSVP management** | Guests respond **Going**, **Maybe** or **Not Going**, can change their answer any time, and can bring up to 5 extra guests. Capacity is enforced and RSVPs close once an event has passed |
| **Guest management** | Attendee list per event with status filters, live counts and seat totals, a **check-in** button and **CSV export** |
| **Notifications** | In-app alerts for welcome, RSVP confirmation, event updates, cancellations and organizer messages. **Automatic reminders** go out about 24 hours before an event. Real email is optional via SMTP |
| **Admin dashboard** | Totals, RSVP breakdown, attendance rate, recent activity, all events (guests, edit, delete) and user management (search, promote or demote, delete) |
| **Responsive design** | Works on mobile and desktop: hamburger menu, fluid card grid, and tables that become stacked cards on small screens |

## Tech stack

- **Frontend:** HTML (Jinja2 templates), CSS, vanilla JavaScript
- **Backend:** Python 3, Flask
- **Database:** SQLite (created automatically)
- **Tooling:** Node.js/npm launcher (`npm run dev`)

## Prerequisites

- [Node.js](https://nodejs.org) 16 or newer
- [Python](https://www.python.org/downloads/) 3.9 or newer (on Windows, tick **Add Python to PATH** during install)

Check both with `node --version` and `python --version` (use `python3` on Mac/Linux).

## Quick start (npm)

```bash
git clone https://github.com/YOUR-USERNAME/gatherly.git
cd gatherly
npm run seed      # optional: demo users and events
npm run dev       # starts the app
```

Open **http://localhost:5000**.

The first run creates a virtual environment and installs Flask automatically. After that, only `npm run dev` is needed.

| Command | Purpose |
|---|---|
| `npm run dev` | Start the app |
| `npm run seed` | Add demo users and events |
| `npm run setup` | Install dependencies only |
| `npm test` | Run the automated tests |

### Python only (without npm)

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py seed                # optional
python app.py
```

## Default logins

| Role | Email | Password |
|---|---|---|
| Admin | `admin@gatherly.local` | `admin123` |
| Demo user (after `seed`) | `alice@example.com` | `password` |
| Demo user (after `seed`) | `bala@example.com` | `password` |

> **Change the admin password** before using this anywhere public.

## How to use it

1. **Sign up** and log in.
2. Click **+ Create** to add an event with a poster.
3. Open any event and choose **Going**, **Maybe** or **Not going**.
4. Check **Alerts** for confirmations and reminders.
5. As an organizer, open **Guest list & check-in** on your event to filter guests, check people in, export a CSV, or send a message or reminder.
6. Log in as admin and open **Admin** for the dashboard and user management.

## Configuration

Set these as environment variables.

| Variable | Purpose | Default |
|---|---|---|
| `SECRET_KEY` | Session signing key. **Set this in production** | development value |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | First admin account (used on first run) | `admin@gatherly.local` / `admin123` |
| `PORT` | Server port | `5000` |
| `GATHERLY_DB` | Database file path | `gatherly.db` next to `app.py` |
| `GATHERLY_UPLOADS` | Poster upload folder | `uploads/` next to `app.py` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TLS` | Turn on real email. Without `SMTP_HOST`, emails are only logged to the console and alerts still appear in-app | off |

Examples:

```bash
# Mac/Linux
PORT=5001 npm run dev
ADMIN_PASSWORD=MyStrongPass SECRET_KEY=change-me npm run dev

# Windows (Command Prompt)
set PORT=5001
npm run dev
```

## Roles

- **User:** browse events, RSVP, create events, and manage their own events and guests.
- **Admin:** everything a user can do, for all events, plus the dashboard and user management.

## Project structure

```
gatherly/
├── app.py               # routes, database, notifications, reminder worker
├── test_app.py          # automated end-to-end tests
├── package.json         # npm scripts
├── scripts/run.js       # launcher: creates venv, installs Flask, runs the app
├── requirements.txt     # Python dependency (Flask)
├── templates/           # Jinja2 pages (events, RSVP, guests, admin, auth)
├── static/
│   ├── css/style.css    # responsive styling
│   └── js/app.js        # mobile menu, poster preview, countdown, confirm dialogs
└── uploads/             # poster images (created automatically)
```

## Troubleshooting

| Problem | Fix |
|---|---|
| `npm` not recognized | Reinstall Node.js, then reopen the terminal |
| "Python 3 was not found" | Install Python and tick **Add Python to PATH** |
| Port 5000 already in use | Run on another port, e.g. `PORT=5001 npm run dev` |
| Start with a clean database | Stop the app, delete `gatherly.db` and `uploads/`, then run `npm run seed` |

## Production notes

- Set a strong `SECRET_KEY` and a new admin password.
- Serve with a production server such as `gunicorn -w 1 app:app` and put it behind HTTPS.
- Keep a single worker, because the reminder job runs in a background thread. Otherwise, move reminders to a scheduled job.

## License

Add a license of your choice (for example MIT) before publishing.
