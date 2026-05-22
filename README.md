# Teacher Substitution API v2

A FastAPI + SQLite backend that automatically assigns free teachers when another teacher is absent.

---

## What's new in v2

- **`/health` endpoint** — ready for uptime monitors and load balancer health checks
- **`/api/auth/change-password`** — teachers can change their own password
- **`GET /api/teachers/{id}`** — fetch a single teacher
- **`PATCH /api/teachers/{id}`** — update teacher name / subjects
- **`DELETE /api/classes/{id}`** — remove a class
- **Filtered list endpoints** — filter absences by `?date=` or `?teacher_id=`, schedules by `?teacher_id=` or `?day=`, substitutions by `?status=`
- **Input validation** — day names, period ranges (1–10), date format (YYYY-MM-DD), password length, periods string
- **Conflict fix** — cancelled substitutions no longer block re-assignment
- **ON DELETE CASCADE** — deleting a teacher/class cleans up their schedules, absences, and substitutions
- **WAL journal mode** — SQLite handles concurrent reads better
- **`ALLOWED_ORIGINS` env var** — restrict CORS in production instead of allowing `*`
- **Docker + docker-compose** included

---

## Quick start (local)

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Server: http://localhost:8000  
Interactive docs: http://localhost:8000/docs

---

## Default admin credentials

| Field    | Value            |
|----------|------------------|
| Email    | admin@school.com |
| Password | admin123         |

**Change these immediately after first login.**

---

## Deploy to the internet

### Option A — Render.com (easiest, free tier)

1. Push this folder to a GitHub repository
2. Go to https://render.com → **New → Web Service** → connect your repo
3. Render reads `render.yaml` automatically
4. Your API will be live at `https://<your-app>.onrender.com`

> Free tier spins down after 15 min of inactivity (cold start ~30s). Upgrade to Starter ($7/mo) for always-on.

### Option B — Railway.app

1. Push to GitHub
2. Go to https://railway.app → **New Project → Deploy from GitHub**
3. Set env vars in the dashboard:
   - `SECRET_KEY` → a long random string
   - `DB_PATH` → `/data/substitution.db`
   - `ALLOWED_ORIGINS` → your frontend URL (or `*`)
4. Attach a **Volume** mounted at `/data` for persistent SQLite

### Option C — Docker (any VPS: DigitalOcean, Hetzner, AWS EC2, etc.)

```bash
# Clone / copy files to your server, then:
cp .env.example .env
nano .env          # set SECRET_KEY and ALLOWED_ORIGINS

docker compose up -d
```

Your API is now at `http://<server-ip>:8000`.  
Put Nginx or Caddy in front for HTTPS.

### Option D — Fly.io

```bash
fly launch          # follow prompts, picks up Dockerfile automatically
fly secrets set SECRET_KEY=$(openssl rand -hex 32)
fly volumes create db_data --size 1
fly deploy
```

---

## Environment variables

| Variable          | Default                        | Description                                 |
|-------------------|-------------------------------|---------------------------------------------|
| `SECRET_KEY`      | `change-this-in-production`   | JWT signing key — **must change**           |
| `DB_PATH`         | `substitution.db`             | Path to the SQLite database file            |
| `ALLOWED_ORIGINS` | `*`                           | Comma-separated CORS origins, e.g. `https://myapp.com` |
| `PORT`            | `8000`                        | Port uvicorn listens on                     |

---

## API reference

### Auth
| Method | Route                        | Auth     | Description              |
|--------|------------------------------|----------|--------------------------|
| POST   | /api/auth/login              | None     | Login, get JWT token     |
| GET    | /api/auth/me                 | Required | Current user info        |
| POST   | /api/auth/change-password    | Required | Change your password     |

### Teachers
| Method | Route                  | Auth  | Description              |
|--------|------------------------|-------|--------------------------|
| GET    | /api/teachers          | Any   | List all teachers        |
| GET    | /api/teachers/{id}     | Any   | Get single teacher       |
| POST   | /api/teachers          | Admin | Add teacher              |
| PATCH  | /api/teachers/{id}     | Admin | Update name/subjects     |
| DELETE | /api/teachers/{id}     | Admin | Remove teacher           |

### Classes
| Method | Route              | Auth  | Description       |
|--------|--------------------|-------|-------------------|
| GET    | /api/classes       | Any   | List all classes  |
| POST   | /api/classes       | Admin | Add class         |
| DELETE | /api/classes/{id}  | Admin | Remove class      |

### Schedules
| Method | Route                           | Auth  | Description                    |
|--------|---------------------------------|-------|--------------------------------|
| GET    | /api/schedules                  | Any   | Full timetable (filterable)    |
| GET    | /api/schedules/teacher/{id}     | Any   | One teacher's timetable        |
| POST   | /api/schedules                  | Admin | Add schedule entry             |
| DELETE | /api/schedules/{id}             | Admin | Remove schedule entry          |

Query params for GET /api/schedules: `?teacher_id=`, `?day=Monday`

### Absences
| Method | Route          | Auth | Description                               |
|--------|----------------|------|-------------------------------------------|
| GET    | /api/absences  | Any  | List absences (`?date=`, `?teacher_id=`)  |
| POST   | /api/absences  | Any  | Report absence + auto-assign substitutes  |

### Substitutions
| Method | Route                                    | Auth  | Description                      |
|--------|------------------------------------------|-------|----------------------------------|
| GET    | /api/substitutions                       | Any   | All substitutions (`?date=`, `?status=`) |
| GET    | /api/substitutions/free-teachers         | Any   | Free teachers for date+period    |
| POST   | /api/substitutions                       | Admin | Manually assign substitute       |
| PATCH  | /api/substitutions/{id}/status           | Any   | Update status                    |

Statuses: `assigned` → `confirmed` → `completed` / `cancelled`

### System
| Method | Route    | Auth | Description             |
|--------|----------|------|-------------------------|
| GET    | /        | None | API info                |
| GET    | /health  | None | Health check            |
| GET    | /docs    | None | Swagger UI              |
| GET    | /redoc   | None | ReDoc                   |

---

## Using the API from your frontend

```javascript
const API = "https://your-app.onrender.com";  // or localhost:8000

// 1. Login
const { access_token } = await fetch(`${API}/api/auth/login`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ email: "admin@school.com", password: "admin123" })
}).then(r => r.json());

// 2. Authenticated request
const teachers = await fetch(`${API}/api/teachers`, {
  headers: { Authorization: `Bearer ${access_token}` }
}).then(r => r.json());
```

