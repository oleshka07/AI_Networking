"""Integration endpoints: Google (Gmail+Calendar), Chater DB, Telegram."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.integrations import chater, google

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


# ── Google ───────────────────────────────────────────────────────────────────


@router.get("/google/status")
def google_status(db: Session = Depends(get_db)):
    return google.status(db)


@router.get("/google/authorize")
def google_authorize(db: Session = Depends(get_db)):
    if not settings.google_configured:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Google is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
        )
    return RedirectResponse(google.build_authorization_url(db))


@router.get("/google/callback")
def google_callback(
    code: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    if error:
        return RedirectResponse(f"/?google=error&detail={error}")
    if not code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing authorization code")
    try:
        email = google.handle_oauth_callback(db, code)
    except Exception as exc:
        return RedirectResponse(f"/?google=error&detail={exc}")
    return RedirectResponse(f"/?google=connected&email={email}")


@router.post("/google/sync")
def google_sync(
    contact_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
):
    return google.sync(db, contact_id=contact_id).as_dict()


@router.post("/google/disconnect")
def google_disconnect(db: Session = Depends(get_db)):
    google.disconnect(db)
    return {"disconnected": True}


# ── Chater (existing Telegram bot database) ──────────────────────────────────


@router.get("/chater/status")
def chater_status():
    return chater.status()


@router.get("/chater/inspect")
def chater_inspect():
    """Dry run — report Chater tables/columns we'd map. No writes."""
    return chater.inspect()


@router.post("/chater/import")
def chater_import(
    message_limit: int = Query(default=200, ge=0, le=5000),
    db: Session = Depends(get_db),
):
    return chater.import_data(db, message_limit=message_limit).as_dict()


@router.post("/chater/dedupe")
def chater_dedupe(db: Session = Depends(get_db)):
    """Remove duplicate chater-imported contacts (keeps the richest copy)."""
    return {"duplicates_removed": chater.dedupe(db)}


# ── Telegram ─────────────────────────────────────────────────────────────────


@router.get("/telegram/status")
def telegram_status():
    from app.modules.integrations import alisio, holos
    from app.modules.telegram_bot import transcribe

    return {
        "configured": settings.telegram_configured,
        "chat_id_set": bool(settings.telegram_chat_id),
        # ALiSiO CRM веде чати своїх лідів тим самим ботом.
        "alisio": alisio.configured(),
        # Holos: сторіс у профіль і ідеї з власних повідомлень (тим самим ботом).
        "holos": holos.configured(),
        # Розшифровка голосових: хто перший і хто в запасі.
        "voice_provider": transcribe.active_provider(),
        "voice_providers": transcribe.available_providers(),
    }


@router.post("/telegram/test")
def telegram_test():
    from app import telegram

    ok = telegram.send_message("✅ Networking AI is connected to Telegram.")
    if not ok:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Could not send. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.",
        )
    return {"sent": True}
