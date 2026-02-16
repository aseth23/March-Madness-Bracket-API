from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from typing import Dict
import json

from datetime import datetime
from zoneinfo import ZoneInfo

from db import Base, engine, SessionLocal
from models import Entry

app = FastAPI()

# ---- Bracket entry/edit deadline (America/New_York) ----
BRACKET_DEADLINE = datetime(2026, 3, 19, 0, 0, tzinfo=ZoneInfo("America/New_York"))

def _deadline_passed() -> bool:
    now_et = datetime.now(ZoneInfo("America/New_York"))
    return now_et >= BRACKET_DEADLINE

# Allow the Next.js frontend to call this API in the browser
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://march-madness-bracket-z4rb.vercel.app",
    ],
    allow_origin_regex=r"^https://.*\.vercel\.app$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class EntryCreate(BaseModel):
    name: str
    email: EmailStr
    username: str | None = None

@app.get("/")
def home():
    return {"status": "ok", "message": "Bracket app is running"}

@app.post("/entries")
def create_entry(entry: EntryCreate, db: Session = Depends(get_db)):
    # ✅ Stop new brackets starting March 19th
    if _deadline_passed():
        raise HTTPException(status_code=403, detail="Bracket entry is closed (deadline passed).")

    email_norm = entry.email.lower().strip()

    # Stevens-only emails (no verification flow yet)
    if not email_norm.endswith("@stevens.edu"):
        raise HTTPException(status_code=400, detail="Use your @stevens.edu email")

    existing = db.query(Entry).filter(Entry.email == email_norm).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already used")

    new_entry = Entry(
        name=entry.name.strip(),
        email=email_norm,
        username=(entry.username.strip() if entry.username else None),
    )

    db.add(new_entry)
    db.commit()
    db.refresh(new_entry)

    return {
        "id": new_entry.id,
        "name": new_entry.name,
        "email": new_entry.email,
        "username": new_entry.username,
    }

@app.get("/entries")
def list_entries(db: Session = Depends(get_db)):
    rows = (
        db.query(Entry)
        .order_by(Entry.created_at.asc())
        .all()
    )

    # Full list including bracket (used by frontend scoring/leaderboard)
    # (no email here)
    return [
        {
            "id": e.id,
            "name": e.name,
            "username": e.username,
            "bracket": json.loads(e.bracket) if e.bracket else {},
            "locked": e.locked,
        }
        for e in rows
    ]

@app.get("/entries/{entry_id}")
def get_entry(entry_id: int, db: Session = Depends(get_db)):
    entry = db.query(Entry).filter(Entry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    return {
        "id": entry.id,
        "name": entry.name,
        "email": entry.email,  # ✅ ADDED so view page can show it
        "username": entry.username,
        "bracket": json.loads(entry.bracket) if entry.bracket else {},
        "locked": entry.locked,
        "score": entry.score,
    }

# ✅ NEW: View-bracket endpoint for the "View" button
# Same data as /entries/{id} but a cleaner name for the frontend
@app.get("/view-bracket/{entry_id}")
def view_bracket(entry_id: int, db: Session = Depends(get_db)):
    entry = db.query(Entry).filter(Entry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    return {
        "id": entry.id,
        "name": entry.name,
        "email": entry.email,  # ✅ ADDED (in case frontend uses this route)
        "username": entry.username,
        "bracket": json.loads(entry.bracket) if entry.bracket else {},
        "locked": entry.locked,
    }

@app.post("/entries/{entry_id}/bracket")
def submit_bracket(entry_id: int, bracket: Dict, db: Session = Depends(get_db)):
    entry = db.query(Entry).filter(Entry.id == entry_id).first()

    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    # ✅ Stop bracket submissions/edits starting March 19th
    if _deadline_passed():
        raise HTTPException(status_code=403, detail="Bracket submissions are closed (deadline passed).")

    if entry.locked:
        raise HTTPException(status_code=403, detail="Bracket is locked")

    entry.bracket = json.dumps(bracket)
    db.commit()

    return {"status": "saved", "entry_id": entry_id}

@app.get("/leaderboard")
def leaderboard(db: Session = Depends(get_db)):
    rows = (
        db.query(Entry)
        .order_by(Entry.score.desc(), Entry.created_at.asc())
        .all()
    )

    return [
        {
            "id": e.id,
            "name": e.name,
            "username": e.username,
            "score": e.score,
            "locked": e.locked,
        }
        for e in rows
    ]
