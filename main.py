from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, validator
from typing import Optional, List
from datetime import datetime, timedelta
import jwt
import bcrypt
import sqlite3
import os
import re

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Teacher Substitution API",
    version="2.0.0",
    description="Automatically assigns free teachers when another teacher is absent.",
)

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

SECRET_KEY = os.getenv("SECRET_KEY", "change-this-in-production-please-use-a-strong-random-key")
ALGORITHM  = "HS256"
security   = HTTPBearer()
DB_PATH    = os.getenv("DB_PATH", "substitution.db")

VALID_DAYS     = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}
VALID_STATUSES = {"assigned", "confirmed", "completed", "cancelled"}

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")   # better concurrent reads
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS teachers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            email       TEXT UNIQUE NOT NULL COLLATE NOCASE,
            password    TEXT NOT NULL,
            subjects    TEXT NOT NULL DEFAULT '',
            is_admin    INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS classes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT UNIQUE NOT NULL,
            grade      TEXT NOT NULL,
            section    TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_id  INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
            class_id    INTEGER NOT NULL REFERENCES classes(id)  ON DELETE CASCADE,
            day         TEXT NOT NULL,
            period      INTEGER NOT NULL CHECK(period BETWEEN 1 AND 10),
            subject     TEXT NOT NULL,
            UNIQUE(teacher_id, day, period),
            UNIQUE(class_id,   day, period)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS absences (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            teacher_id      INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
            date            TEXT NOT NULL,
            periods         TEXT NOT NULL,
            reason          TEXT,
            reported_by     INTEGER REFERENCES teachers(id),
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS substitutions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            absence_id       INTEGER NOT NULL REFERENCES absences(id) ON DELETE CASCADE,
            substitute_id    INTEGER NOT NULL REFERENCES teachers(id),
            class_id         INTEGER NOT NULL REFERENCES classes(id),
            date             TEXT NOT NULL,
            period           INTEGER NOT NULL,
            subject          TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'assigned'
                             CHECK(status IN ('assigned','confirmed','completed','cancelled')),
            created_at       TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # Seed admin if not exists
    admin = c.execute("SELECT id FROM teachers WHERE email='admin@school.com'").fetchone()
    if not admin:
        pw = bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode()
        c.execute(
            "INSERT INTO teachers (name, email, password, subjects, is_admin) VALUES (?,?,?,?,1)",
            ("Admin", "admin@school.com", pw, "all")
        )

    conn.commit()
    conn.close()

# ── Auth helpers ──────────────────────────────────────────────────────────────

def make_token(teacher_id: int, is_admin: bool) -> str:
    payload = {
        "sub": str(teacher_id),
        "admin": is_admin,
        "exp": datetime.utcnow() + timedelta(days=7)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def current_user(
    creds: HTTPAuthorizationCredentials = Depends(security),
    db: sqlite3.Connection = Depends(get_db)
):
    try:
        payload = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        teacher = db.execute(
            "SELECT * FROM teachers WHERE id=?", (payload["sub"],)
        ).fetchone()
        if not teacher:
            raise HTTPException(status_code=401, detail="User not found")
        return dict(teacher)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

def require_admin(user=Depends(current_user)):
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

def validate_date(date_str: str) -> str:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        raise HTTPException(status_code=422, detail="Date must be YYYY-MM-DD format")

def parse_periods(periods_str: str) -> List[int]:
    try:
        periods = [int(p.strip()) for p in periods_str.split(",") if p.strip()]
        if not periods:
            raise ValueError
        if any(p < 1 or p > 10 for p in periods):
            raise HTTPException(status_code=422, detail="Periods must be between 1 and 10")
        return periods
    except ValueError:
        raise HTTPException(status_code=422, detail="Periods must be comma-separated integers e.g. '1,2,3'")

# ── Schemas ───────────────────────────────────────────────────────────────────

class LoginBody(BaseModel):
    email: str
    password: str

class RegisterTeacher(BaseModel):
    name: str
    email: str
    password: str
    subjects: str
    is_admin: bool = False

    @validator("name")
    def name_not_empty(cls, v):
        if not v.strip():
            raise ValueError("Name cannot be empty")
        return v.strip()

    @validator("password")
    def password_length(cls, v):
        if len(v) < 6:
            raise ValueError("Password must be at least 6 characters")
        return v

class ClassBody(BaseModel):
    name: str
    grade: str
    section: str

class ScheduleBody(BaseModel):
    teacher_id: int
    class_id: int
    day: str
    period: int
    subject: str

    @validator("day")
    def valid_day(cls, v):
        if v not in VALID_DAYS:
            raise ValueError(f"Day must be one of: {', '.join(sorted(VALID_DAYS))}")
        return v

    @validator("period")
    def valid_period(cls, v):
        if not 1 <= v <= 10:
            raise ValueError("Period must be between 1 and 10")
        return v

class AbsenceBody(BaseModel):
    teacher_id: int
    date: str
    periods: str
    reason: Optional[str] = None

class SubstituteBody(BaseModel):
    absence_id: int
    substitute_id: int
    class_id: int
    period: int
    subject: str

class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str

    @validator("new_password")
    def password_length(cls, v):
        if len(v) < 6:
            raise ValueError("New password must be at least 6 characters")
        return v

# ── Routes: health & root ─────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "message": "Teacher Substitution API v2",
        "docs": "/docs",
        "redoc": "/redoc",
        "health": "/health"
    }

@app.get("/health")
def health(db: sqlite3.Connection = Depends(get_db)):
    """Health check endpoint for uptime monitors and load balancers."""
    try:
        db.execute("SELECT 1").fetchone()
        return {"status": "ok", "database": "connected", "timestamp": datetime.utcnow().isoformat()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database error: {e}")

# ── Routes: auth ──────────────────────────────────────────────────────────────

@app.post("/api/auth/login")
def login(body: LoginBody, db: sqlite3.Connection = Depends(get_db)):
    teacher = db.execute(
        "SELECT * FROM teachers WHERE email=?", (body.email.strip().lower(),)
    ).fetchone()
    if not teacher or not bcrypt.checkpw(body.password.encode(), teacher["password"].encode()):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = make_token(teacher["id"], bool(teacher["is_admin"]))
    return {
        "access_token": token,
        "token_type": "bearer",
        "teacher": {
            "id": teacher["id"],
            "name": teacher["name"],
            "email": teacher["email"],
            "is_admin": bool(teacher["is_admin"]),
            "subjects": teacher["subjects"],
        }
    }

@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return {k: v for k, v in user.items() if k != "password"}

@app.post("/api/auth/change-password")
def change_password(
    body: ChangePasswordBody,
    db: sqlite3.Connection = Depends(get_db),
    user=Depends(current_user)
):
    if not bcrypt.checkpw(body.current_password.encode(), user["password"].encode()):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    new_pw = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()
    db.execute("UPDATE teachers SET password=? WHERE id=?", (new_pw, user["id"]))
    db.commit()
    return {"message": "Password changed successfully"}

# ── Routes: teachers ──────────────────────────────────────────────────────────

@app.get("/api/teachers")
def list_teachers(db: sqlite3.Connection = Depends(get_db), _=Depends(current_user)):
    rows = db.execute(
        "SELECT id, name, email, subjects, is_admin, created_at FROM teachers ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/teachers/{teacher_id}")
def get_teacher(
    teacher_id: int,
    db: sqlite3.Connection = Depends(get_db),
    _=Depends(current_user)
):
    row = db.execute(
        "SELECT id, name, email, subjects, is_admin, created_at FROM teachers WHERE id=?",
        (teacher_id,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Teacher not found")
    return dict(row)

@app.post("/api/teachers", status_code=201)
def add_teacher(
    body: RegisterTeacher,
    db: sqlite3.Connection = Depends(get_db),
    _=Depends(require_admin)
):
    existing = db.execute(
        "SELECT id FROM teachers WHERE email=?", (body.email.strip().lower(),)
    ).fetchone()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    pw = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    cur = db.execute(
        "INSERT INTO teachers (name, email, password, subjects, is_admin) VALUES (?,?,?,?,?)",
        (body.name, body.email.strip().lower(), pw, body.subjects, int(body.is_admin))
    )
    db.commit()
    return {"id": cur.lastrowid, "name": body.name, "email": body.email}

@app.patch("/api/teachers/{teacher_id}")
def update_teacher(
    teacher_id: int,
    name: Optional[str] = None,
    subjects: Optional[str] = None,
    db: sqlite3.Connection = Depends(get_db),
    _=Depends(require_admin)
):
    teacher = db.execute("SELECT id FROM teachers WHERE id=?", (teacher_id,)).fetchone()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
    if name:
        db.execute("UPDATE teachers SET name=? WHERE id=?", (name.strip(), teacher_id))
    if subjects is not None:
        db.execute("UPDATE teachers SET subjects=? WHERE id=?", (subjects, teacher_id))
    db.commit()
    return {"message": "Teacher updated"}

@app.delete("/api/teachers/{teacher_id}", status_code=204)
def remove_teacher(
    teacher_id: int,
    db: sqlite3.Connection = Depends(get_db),
    _=Depends(require_admin)
):
    teacher = db.execute("SELECT id FROM teachers WHERE id=?", (teacher_id,)).fetchone()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
    db.execute("DELETE FROM teachers WHERE id=?", (teacher_id,))
    db.commit()

# ── Routes: classes ───────────────────────────────────────────────────────────

@app.get("/api/classes")
def list_classes(db: sqlite3.Connection = Depends(get_db), _=Depends(current_user)):
    return [dict(r) for r in db.execute("SELECT * FROM classes ORDER BY grade, section").fetchall()]

@app.post("/api/classes", status_code=201)
def add_class(
    body: ClassBody,
    db: sqlite3.Connection = Depends(get_db),
    _=Depends(require_admin)
):
    try:
        cur = db.execute(
            "INSERT INTO classes (name, grade, section) VALUES (?,?,?)",
            (body.name.strip(), body.grade.strip(), body.section.strip())
        )
        db.commit()
        return {"id": cur.lastrowid, **body.dict()}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Class already exists")

@app.delete("/api/classes/{class_id}", status_code=204)
def remove_class(
    class_id: int,
    db: sqlite3.Connection = Depends(get_db),
    _=Depends(require_admin)
):
    db.execute("DELETE FROM classes WHERE id=?", (class_id,))
    db.commit()

# ── Routes: schedule ──────────────────────────────────────────────────────────

@app.get("/api/schedules")
def list_schedules(
    teacher_id: Optional[int] = None,
    day: Optional[str] = None,
    db: sqlite3.Connection = Depends(get_db),
    _=Depends(current_user)
):
    query = """
        SELECT s.*, t.name as teacher_name, c.name as class_name
        FROM schedules s
        JOIN teachers t ON t.id = s.teacher_id
        JOIN classes  c ON c.id = s.class_id
        WHERE 1=1
    """
    params = []
    if teacher_id:
        query += " AND s.teacher_id=?"
        params.append(teacher_id)
    if day:
        if day not in VALID_DAYS:
            raise HTTPException(status_code=422, detail=f"Day must be one of: {', '.join(sorted(VALID_DAYS))}")
        query += " AND s.day=?"
        params.append(day)
    query += " ORDER BY s.day, s.period"
    rows = db.execute(query, params).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/schedules/teacher/{teacher_id}")
def teacher_schedule(
    teacher_id: int,
    db: sqlite3.Connection = Depends(get_db),
    _=Depends(current_user)
):
    rows = db.execute("""
        SELECT s.*, c.name as class_name
        FROM schedules s JOIN classes c ON c.id = s.class_id
        WHERE s.teacher_id = ?
        ORDER BY s.day, s.period
    """, (teacher_id,)).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/schedules", status_code=201)
def add_schedule(
    body: ScheduleBody,
    db: sqlite3.Connection = Depends(get_db),
    _=Depends(require_admin)
):
    # Verify teacher and class exist
    if not db.execute("SELECT id FROM teachers WHERE id=?", (body.teacher_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Teacher not found")
    if not db.execute("SELECT id FROM classes WHERE id=?", (body.class_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Class not found")
    try:
        cur = db.execute(
            "INSERT INTO schedules (teacher_id, class_id, day, period, subject) VALUES (?,?,?,?,?)",
            (body.teacher_id, body.class_id, body.day, body.period, body.subject)
        )
        db.commit()
        return {"id": cur.lastrowid, **body.dict()}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=400, detail="Schedule conflict — teacher or class already booked for this slot")

@app.delete("/api/schedules/{schedule_id}", status_code=204)
def delete_schedule(
    schedule_id: int,
    db: sqlite3.Connection = Depends(get_db),
    _=Depends(require_admin)
):
    db.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
    db.commit()

# ── Routes: absences ──────────────────────────────────────────────────────────

@app.get("/api/absences")
def list_absences(
    date: Optional[str] = None,
    teacher_id: Optional[int] = None,
    db: sqlite3.Connection = Depends(get_db),
    _=Depends(current_user)
):
    query = """
        SELECT a.*, t.name as teacher_name
        FROM absences a JOIN teachers t ON t.id = a.teacher_id
        WHERE 1=1
    """
    params = []
    if date:
        validate_date(date)
        query += " AND a.date=?"
        params.append(date)
    if teacher_id:
        query += " AND a.teacher_id=?"
        params.append(teacher_id)
    query += " ORDER BY a.date DESC"
    rows = db.execute(query, params).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/absences", status_code=201)
def report_absence(
    body: AbsenceBody,
    db: sqlite3.Connection = Depends(get_db),
    user=Depends(current_user)
):
    validate_date(body.date)
    periods = parse_periods(body.periods)

    if not db.execute("SELECT id FROM teachers WHERE id=?", (body.teacher_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Teacher not found")

    cur = db.execute(
        "INSERT INTO absences (teacher_id, date, periods, reason, reported_by) VALUES (?,?,?,?,?)",
        (body.teacher_id, body.date, body.periods, body.reason, user["id"])
    )
    db.commit()
    absence_id = cur.lastrowid

    assigned = []
    try:
        day_name = datetime.strptime(body.date, "%Y-%m-%d").strftime("%A")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    for period in periods:
        slot = db.execute("""
            SELECT s.class_id, s.subject
            FROM schedules s
            WHERE s.teacher_id=? AND s.day=? AND s.period=?
        """, (body.teacher_id, day_name, period)).fetchone()
        if not slot:
            continue

        free = db.execute("""
            SELECT t.id, t.name FROM teachers t
            WHERE t.id != ?
              AND t.is_admin = 0
              AND t.id NOT IN (
                SELECT teacher_id FROM schedules
                WHERE day=? AND period=?
              )
              AND t.id NOT IN (
                SELECT substitute_id FROM substitutions
                WHERE date=? AND period=? AND status != 'cancelled'
              )
              AND t.id NOT IN (
                SELECT teacher_id FROM absences
                WHERE date=? AND periods LIKE '%' || ? || '%'
              )
            LIMIT 1
        """, (body.teacher_id, day_name, period, body.date, period, body.date, str(period))).fetchone()

        if free:
            sub_cur = db.execute("""
                INSERT INTO substitutions
                  (absence_id, substitute_id, class_id, date, period, subject)
                VALUES (?,?,?,?,?,?)
            """, (absence_id, free["id"], slot["class_id"], body.date, period, slot["subject"]))
            db.commit()
            assigned.append({
                "period": period,
                "substitute_id": free["id"],
                "substitute_name": free["name"],
                "class_id": slot["class_id"],
                "substitution_id": sub_cur.lastrowid,
            })

    return {
        "absence_id": absence_id,
        "auto_assigned": assigned,
        "unassigned_periods": [
            p for p in periods if p not in [a["period"] for a in assigned]
        ],
    }

# ── Routes: substitutions ─────────────────────────────────────────────────────

@app.get("/api/substitutions")
def list_substitutions(
    date: Optional[str] = None,
    status: Optional[str] = None,
    db: sqlite3.Connection = Depends(get_db),
    _=Depends(current_user)
):
    if status and status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"Status must be one of: {', '.join(VALID_STATUSES)}")
    query = """
        SELECT sub.*,
               t.name   as substitute_name,
               a_t.name as absent_teacher_name,
               c.name   as class_name,
               ab.date  as absence_date
        FROM substitutions sub
        JOIN teachers t   ON t.id  = sub.substitute_id
        JOIN absences ab  ON ab.id = sub.absence_id
        JOIN teachers a_t ON a_t.id = ab.teacher_id
        JOIN classes  c   ON c.id  = sub.class_id
        WHERE 1=1
    """
    params = []
    if date:
        validate_date(date)
        query += " AND sub.date=?"
        params.append(date)
    if status:
        query += " AND sub.status=?"
        params.append(status)
    query += " ORDER BY sub.date DESC, sub.period"
    rows = db.execute(query, params).fetchall()
    return [dict(r) for r in rows]

@app.post("/api/substitutions", status_code=201)
def manual_assign(
    body: SubstituteBody,
    db: sqlite3.Connection = Depends(get_db),
    user=Depends(require_admin)
):
    absence = db.execute("SELECT * FROM absences WHERE id=?", (body.absence_id,)).fetchone()
    if not absence:
        raise HTTPException(status_code=404, detail="Absence not found")

    sub = db.execute("SELECT id FROM teachers WHERE id=?", (body.substitute_id,)).fetchone()
    if not sub:
        raise HTTPException(status_code=404, detail="Substitute teacher not found")

    absence_dict = dict(absence)
    conflict = db.execute("""
        SELECT id FROM substitutions
        WHERE substitute_id=? AND date=? AND period=? AND status != 'cancelled'
    """, (body.substitute_id, absence_dict["date"], body.period)).fetchone()
    if conflict:
        raise HTTPException(status_code=400, detail="Teacher already assigned to another class in this period")

    cur = db.execute("""
        INSERT INTO substitutions (absence_id, substitute_id, class_id, date, period, subject)
        VALUES (?,?,?,?,?,?)
    """, (body.absence_id, body.substitute_id, body.class_id,
          absence_dict["date"], body.period, body.subject))
    db.commit()
    return {"id": cur.lastrowid, "status": "assigned"}

@app.patch("/api/substitutions/{sub_id}/status")
def update_status(
    sub_id: int,
    status: str,
    db: sqlite3.Connection = Depends(get_db),
    _=Depends(current_user)
):
    if status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Status must be one of: {', '.join(VALID_STATUSES)}")
    row = db.execute("SELECT id FROM substitutions WHERE id=?", (sub_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Substitution not found")
    db.execute("UPDATE substitutions SET status=? WHERE id=?", (status, sub_id))
    db.commit()
    return {"id": sub_id, "status": status}

@app.get("/api/substitutions/free-teachers")
def free_teachers(
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    period: int = Query(..., ge=1, le=10, description="Period number 1-10"),
    db: sqlite3.Connection = Depends(get_db),
    _=Depends(current_user)
):
    validate_date(date)
    try:
        day_name = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date")
    rows = db.execute("""
        SELECT t.id, t.name, t.subjects FROM teachers t
        WHERE t.is_admin = 0
          AND t.id NOT IN (
            SELECT teacher_id FROM schedules WHERE day=? AND period=?
          )
          AND t.id NOT IN (
            SELECT substitute_id FROM substitutions
            WHERE date=? AND period=? AND status != 'cancelled'
          )
          AND t.id NOT IN (
            SELECT teacher_id FROM absences
            WHERE date=? AND periods LIKE '%' || ? || '%'
          )
        ORDER BY t.name
    """, (day_name, period, date, period, date, str(period))).fetchall()
    return [dict(r) for r in rows]

# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()
