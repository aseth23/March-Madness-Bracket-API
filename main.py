from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.orm import Session
from typing import Dict, Any
import json
import os
import ssl
import certifi
import urllib.request
import urllib.error

from datetime import datetime
from zoneinfo import ZoneInfo
from passlib.context import CryptContext

from db import Base, engine, SessionLocal
from models import Entry

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI()

# ---- Bracket entry/edit deadline (America/New_York) ----
# ✅ DEADLINE = March 19, 2026 at 12:00 PM (noon) ET
BRACKET_DEADLINE = datetime(2026, 3, 19, 12, 0, tzinfo=ZoneInfo("America/New_York"))

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

# Safe migration: add password_hash column to existing databases
with engine.connect() as _conn:
    try:
        _conn.execute(text("ALTER TABLE entries ADD COLUMN password_hash TEXT"))
        _conn.commit()
    except Exception:
        pass  # Column already exists

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
    password: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

# ----------------------------
# SCORING (LIVE)
# ----------------------------
RESULTS: Dict[str, str] = {}

ROUND_POINTS = {
    "_64_": 1,
    "_32_": 2,
    "_S16_": 4,
    "_E8_": 8,
    "FF_SEMI": 16,
    "FF_CHAMP": 32,
}

def _points_for_game_id(game_id: str) -> int:
    if "FF_CHAMP" in game_id:
        return ROUND_POINTS["FF_CHAMP"]
    if "FF_SEMI" in game_id:
        return ROUND_POINTS["FF_SEMI"]
    for k in ["_E8_", "_S16_", "_32_", "_64_"]:
        if k in game_id:
            return ROUND_POINTS[k]
    return 0

def compute_score(picks: Dict[str, str], results: Dict[str, str]) -> int:
    if not picks or not results:
        return 0
    score = 0
    for game_id, correct_teamkey in results.items():
        if not correct_teamkey:
            continue
        stored = picks.get(game_id)
        if not stored:
            continue
        if stored == correct_teamkey:
            score += _points_for_game_id(game_id)
            continue
        # Seed fallback: handles play-in name changes (e.g. "11|Miami/Ohio State" vs "11|Ohio State")
        stored_seed = stored.split("|")[0] if "|" in stored else None
        correct_seed = correct_teamkey.split("|")[0] if "|" in correct_teamkey else None
        if stored_seed and correct_seed and stored_seed == correct_seed:
            score += _points_for_game_id(game_id)
    return score

# Endpoints to set/get RESULTS (no auth)
@app.get("/results")
def get_results():
    return {"results": RESULTS, "points": ROUND_POINTS}

@app.post("/results")
def set_results(payload: Dict[str, str]):
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload must be a dict of {game_id: teamKey}")
    cleaned: Dict[str, str] = {}
    for k, v in payload.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise HTTPException(status_code=400, detail="All keys and values must be strings")
        cleaned[k] = v
    RESULTS.clear()
    RESULTS.update(cleaned)
    return {"status": "ok", "count": len(RESULTS)}

@app.post("/recompute-scores")
def recompute_scores(db: Session = Depends(get_db)):
    rows = db.query(Entry).all()
    for e in rows:
        picks = json.loads(e.bracket) if e.bracket else {}
        e.score = compute_score(picks, RESULTS)
    db.commit()
    return {"status": "ok", "updated": len(rows)}

# ----------------------------
# ROUTES
# ----------------------------
@app.get("/")
def home():
    return {"status": "ok", "message": "Bracket app is running"}

# ✅ NEW: expose deadline to frontend for countdown banner
@app.get("/meta")
def meta():
    return {
        "deadline_et": BRACKET_DEADLINE.isoformat(),
        "deadline_passed": _deadline_passed(),
    }

@app.post("/entries")
def create_entry(entry: EntryCreate, db: Session = Depends(get_db)):
    if _deadline_passed():
        raise HTTPException(status_code=403, detail="Bracket entry is closed (deadline passed).")

    email_norm = entry.email.lower().strip()

    if not email_norm.endswith("@stevens.edu"):
        raise HTTPException(status_code=400, detail="Use your @stevens.edu email")

    existing = db.query(Entry).filter(Entry.email == email_norm).first()
    if existing:
        raise HTTPException(status_code=409, detail="Email already used")

    if not entry.password or len(entry.password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")

    new_entry = Entry(
        name=entry.name.strip(),
        email=email_norm,
        username=(entry.username.strip() if entry.username else None),
        password_hash=pwd_context.hash(entry.password),
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

@app.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    email_norm = req.email.lower().strip()
    entry = db.query(Entry).filter(Entry.email == email_norm).first()
    if not entry:
        raise HTTPException(status_code=401, detail="No account found for that email")

    if entry.password_hash:
        if not pwd_context.verify(req.password, entry.password_hash):
            raise HTTPException(status_code=401, detail="Incorrect password")
    # Legacy entries without a password: allow login, and set password now
    else:
        if req.password:
            entry.password_hash = pwd_context.hash(req.password)
            db.commit()

    return {"id": entry.id, "name": entry.name, "username": entry.username}

@app.get("/entries")
def list_entries(db: Session = Depends(get_db)):
    rows = db.query(Entry).order_by(Entry.created_at.asc()).all()
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

    picks = json.loads(entry.bracket) if entry.bracket else {}
    live_score = compute_score(picks, RESULTS)

    return {
        "id": entry.id,
        "name": entry.name,
        "email": entry.email,
        "username": entry.username,
        "bracket": picks,
        "locked": entry.locked,
        "score": live_score,
    }

@app.get("/view-bracket/{entry_id}")
def view_bracket(entry_id: int, db: Session = Depends(get_db)):
    entry = db.query(Entry).filter(Entry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    picks = json.loads(entry.bracket) if entry.bracket else {}
    live_score = compute_score(picks, RESULTS)

    return {
        "id": entry.id,
        "name": entry.name,
        "email": entry.email,
        "username": entry.username,
        "bracket": picks,
        "locked": entry.locked,
        "score": live_score,
    }

@app.post("/entries/{entry_id}/bracket")
def submit_bracket(entry_id: int, bracket: Dict[str, Any], db: Session = Depends(get_db)):
    entry = db.query(Entry).filter(Entry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    if _deadline_passed():
        raise HTTPException(status_code=403, detail="Bracket submissions are closed (deadline passed).")

    if entry.locked:
        raise HTTPException(status_code=403, detail="Bracket is locked")

    entry.bracket = json.dumps(bracket)
    entry.score = compute_score(bracket if isinstance(bracket, dict) else {}, RESULTS)

    db.commit()
    return {"status": "saved", "entry_id": entry_id}

class TTSRequest(BaseModel):
    text: str

ELEVENLABS_VOICE_ID = "UKvDHTUpXOC66VwQ3n2w"

@app.post("/tts")
def tts(body: TTSRequest):
    api_key = os.environ.get("ELEVENLABS_API_KEY", "sk_7b2a55847afcfccaefe0dede3114fd924d29ece6f7d1f0bf")
    if not api_key:
        raise HTTPException(status_code=503, detail="TTS not configured")

    payload = json.dumps({
        "text": body.text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }).encode()

    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        tts_req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
            data=payload,
            headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            method="POST",
        )
        with urllib.request.urlopen(tts_req, context=ctx) as r:
            audio = r.read()
    except urllib.error.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"ElevenLabs error: {e.read().decode()}")

    return Response(content=audio, media_type="audio/mpeg")


@app.get("/leaderboard")
def leaderboard(db: Session = Depends(get_db)):
    rows = db.query(Entry).order_by(Entry.created_at.asc()).all()

    out = []
    for e in rows:
        picks = json.loads(e.bracket) if e.bracket else {}
        live_score = compute_score(picks, RESULTS)
        out.append(
            {
                "id": e.id,
                "name": e.name,
                "username": e.username,
                "score": live_score,
                "locked": e.locked,
            }
        )

    out.sort(key=lambda x: (x["score"] or 0), reverse=True)
    return out
