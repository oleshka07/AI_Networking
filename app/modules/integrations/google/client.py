"""Google integration: OAuth + Gmail/Calendar sync + outbound email.

Turns real emails and calendar meetings with your contacts into interactions,
so relationship warmth reflects reality with zero manual logging.

Single-user tool: one Google account, its token stored in the database
(`integration_tokens`, provider="google"). All Google libraries are imported
lazily so the rest of the app runs even if they're absent.
"""

from __future__ import annotations

import base64
import json
import logging
import os

# Google may return a slightly different scope set than requested (e.g. it
# echoes previously-granted scopes via include_granted_scopes). Tell oauthlib
# not to hard-fail on that mismatch — we validate what we need at call time.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import parseaddr

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import crud, models, warmth
from app.core.config import settings

logger = logging.getLogger("networking.google")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    # gmail.compose — створення чернеток у Gmail. gmail.send сам по собі
    # цього не дозволяє, тож після додавання скоупу Google треба перепідключити.
    "https://www.googleapis.com/auth/gmail.compose",
    # calendar.events = create/update events (needed to add meetings from
    # natural language). We also keep calendar.readonly: Google returns it
    # among previously-granted scopes (include_granted_scopes), and the
    # requested set must match the granted set or oauthlib rejects the token.
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
    # tasks — задачі, які видно всередині Google Календаря і в застосунку
    # Google Tasks. Старий токен цього скоупу не має: треба перепідключитися.
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

PROVIDER = "google"
PENDING_PROVIDER = "google_oauth_pending"  # holds the PKCE code_verifier


@dataclass
class SyncReport:
    contacts_processed: int = 0
    emails_added: int = 0
    meetings_added: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "contacts_processed": self.contacts_processed,
            "emails_added": self.emails_added,
            "meetings_added": self.meetings_added,
            "errors": self.errors,
        }


# ── Token storage ────────────────────────────────────────────────────────────


def _get_token_row(db: Session) -> models.IntegrationToken | None:
    return db.scalar(
        select(models.IntegrationToken).where(
            models.IntegrationToken.provider == PROVIDER
        )
    )


def _save_token(db: Session, token_json: str, account_email: str | None) -> None:
    _save_token_row(db, PROVIDER, token_json, account_email)


def _save_token_row(
    db: Session, provider: str, token_json: str, account_email: str | None
) -> None:
    row = db.scalar(
        select(models.IntegrationToken).where(
            models.IntegrationToken.provider == provider
        )
    )
    if row is None:
        row = models.IntegrationToken(provider=provider, token_json=token_json)
        db.add(row)
    row.token_json = token_json
    if account_email:
        row.account_email = account_email
    db.commit()


def disconnect(db: Session) -> None:
    row = _get_token_row(db)
    if row:
        db.delete(row)
        db.commit()


def status(db: Session) -> dict:
    row = _get_token_row(db)
    return {
        "configured": settings.google_configured,
        "connected": row is not None,
        "account_email": row.account_email if row else None,
        "last_sync_at": row.last_sync_at.isoformat()
        if row and row.last_sync_at
        else None,
    }


# ── OAuth flow ───────────────────────────────────────────────────────────────


def _client_config() -> dict:
    return {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_redirect_uri],
        }
    }


def build_authorization_url(db: Session) -> str:
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = settings.google_redirect_uri
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # force refresh_token on every connect
    )
    # Newer google-auth-oauthlib enables PKCE by default: it puts a code
    # challenge in the auth URL and expects the matching code_verifier at token
    # exchange. The two steps are separate requests/objects, so persist it.
    verifier = getattr(flow, "code_verifier", None)
    if verifier:
        _save_token_row(db, PENDING_PROVIDER, verifier, None)
    return url


def handle_oauth_callback(db: Session, code: str) -> str:
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(_client_config(), scopes=SCOPES)
    flow.redirect_uri = settings.google_redirect_uri

    pending = db.scalar(
        select(models.IntegrationToken).where(
            models.IntegrationToken.provider == PENDING_PROVIDER
        )
    )
    if pending:
        flow.code_verifier = pending.token_json

    flow.fetch_token(code=code)
    creds = flow.credentials

    account_email = _fetch_account_email(creds)
    _save_token(db, creds.to_json(), account_email)
    if pending:
        db.delete(pending)
        db.commit()
    return account_email or "(connected)"


# ── Credentials loading ──────────────────────────────────────────────────────


def _load_credentials(db: Session):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    row = _get_token_row(db)
    if row is None:
        return None
    info = json.loads(row.token_json)
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(db, creds.to_json(), row.account_email)
        except Exception as exc:  # pragma: no cover - network/auth
            logger.warning("Google token refresh failed: %s", exc)
            return None
    return creds


def _fetch_account_email(creds) -> str | None:
    try:
        from googleapiclient.discovery import build

        service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
        info = service.userinfo().get().execute()
        return info.get("email")
    except Exception:  # pragma: no cover
        return None


# ── Sync ─────────────────────────────────────────────────────────────────────


def sync(db: Session, contact_id: int | None = None) -> SyncReport:
    """Pull recent Gmail + Calendar activity into interactions.

    If contact_id is given, only that contact is synced; otherwise all
    contacts that have an email address.
    """
    report = SyncReport()
    creds = _load_credentials(db)
    if creds is None:
        report.errors.append("Google account is not connected.")
        return report

    try:
        from googleapiclient.discovery import build

        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
        my_email = (
            gmail.users().getProfile(userId="me").execute().get("emailAddress", "")
        ).lower()
    except Exception as exc:  # pragma: no cover
        report.errors.append(f"Could not initialise Google services: {exc}")
        return report

    if contact_id is not None:
        contact = db.get(models.Contact, contact_id)
        contacts = [contact] if contact else []
    else:
        contacts = list(db.scalars(select(models.Contact)))

    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.sync_window_days)

    for contact in contacts:
        if not contact or not contact.email:
            continue
        report.contacts_processed += 1
        created: list[models.Interaction] = []
        try:
            _sync_gmail(db, gmail, contact, my_email, report, created)
        except Exception as exc:  # pragma: no cover
            report.errors.append(f"Gmail sync failed for {contact.email}: {exc}")
        try:
            _sync_calendar(db, calendar, contact, cutoff, report, created)
        except Exception as exc:  # pragma: no cover
            report.errors.append(f"Calendar sync failed for {contact.email}: {exc}")
        if created:
            _apply_sentiment(created)
            db.refresh(contact)
            warmth.refresh(contact)
            db.commit()

    row = _get_token_row(db)
    if row:
        row.last_sync_at = datetime.now(timezone.utc)
        db.commit()
    return report


def _apply_sentiment(interactions: list[models.Interaction]) -> None:
    """Score the tone of freshly-synced interactions (if AI sentiment is on)."""
    if not settings.sync_ai_sentiment or not settings.ai_enabled:
        return
    from app import ai

    texts = [(i.summary or "") for i in interactions]
    scores = ai.score_sentiments(texts)
    for itx, score in zip(interactions, scores):
        itx.sentiment = score


def _sync_gmail(
    db: Session,
    gmail,
    contact: models.Contact,
    my_email: str,
    report: SyncReport,
    created: list,
) -> bool:
    email = contact.email.lower()
    query = f"(from:{email} OR to:{email}) newer_than:{settings.sync_window_days}d"
    resp = (
        gmail.users()
        .messages()
        .list(userId="me", q=query, maxResults=50)
        .execute()
    )
    messages = resp.get("messages", [])
    added = False
    for ref in messages:
        msg_id = ref["id"]
        external_id = f"gmail:{msg_id}"
        if crud.interaction_exists(db, contact.id, external_id):
            continue
        meta = (
            gmail.users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            )
            .execute()
        )
        headers = {
            h["name"].lower(): h["value"]
            for h in meta.get("payload", {}).get("headers", [])
        }
        from_addr = parseaddr(headers.get("from", ""))[1].lower()
        direction = (
            models.Direction.inbound
            if email in from_addr
            else models.Direction.outbound
        )
        occurred_at = _epoch_ms_to_dt(meta.get("internalDate"))
        subject = headers.get("subject", "(no subject)")
        snippet = (meta.get("snippet") or "").strip()
        summary = subject if not snippet else f"{subject} — {snippet[:200]}"
        created.append(
            crud.add_synced_interaction(
                db,
                contact,
                occurred_at=occurred_at,
                channel=models.Channel.email,
                direction=direction,
                summary=summary,
                source="gmail",
                external_id=external_id,
            )
        )
        report.emails_added += 1
        added = True
    return added


def _sync_calendar(
    db: Session,
    calendar,
    contact: models.Contact,
    cutoff: datetime,
    report: SyncReport,
    created: list,
) -> bool:
    email = contact.email.lower()
    now = datetime.now(timezone.utc)
    resp = (
        calendar.events()
        .list(
            calendarId="primary",
            timeMin=cutoff.isoformat(),
            timeMax=now.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
        )
        .execute()
    )
    added = False
    for event in resp.get("items", []):
        attendees = event.get("attendees", [])
        attendee_emails = {a.get("email", "").lower() for a in attendees}
        organizer = event.get("organizer", {}).get("email", "").lower()
        if email not in attendee_emails and email != organizer:
            continue
        external_id = f"gcal:{event['id']}"
        if crud.interaction_exists(db, contact.id, external_id):
            continue
        occurred_at = _event_start(event)
        if occurred_at is None or occurred_at > now:
            continue
        summary = event.get("summary", "Meeting")
        created.append(
            crud.add_synced_interaction(
                db,
                contact,
                occurred_at=occurred_at,
                channel=models.Channel.meeting,
                direction=models.Direction.outbound,
                summary=summary,
                source="gcal",
                external_id=external_id,
            )
        )
        report.meetings_added += 1
        added = True
    return added


def upcoming_events(
    db: Session, minutes_min: int = 45, minutes_max: int = 75, max_results: int = 10
) -> list[dict]:
    """Calendar events starting within [minutes_min, minutes_max] from now.

    Returns [{id, summary, start, attendee_emails, meet_link}] — the raw
    material for pre-meeting briefs. Empty list when Google isn't connected
    or on any error (briefs are best-effort, never noisy)."""
    creds = _load_credentials(db)
    if creds is None:
        return []
    now = datetime.now(timezone.utc)
    try:
        from googleapiclient.discovery import build

        calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
        resp = (
            calendar.events()
            .list(
                calendarId="primary",
                timeMin=(now + timedelta(minutes=minutes_min)).isoformat(),
                timeMax=(now + timedelta(minutes=minutes_max)).isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_results,
            )
            .execute()
        )
    except Exception as exc:  # pragma: no cover - network
        logger.warning("upcoming_events failed: %s", exc)
        return []
    out = []
    for event in resp.get("items", []):
        # Skip all-day events (they only carry a 'date', no 'dateTime') — a
        # timed "in ~1 hour" reminder makes no sense for them.
        if not event.get("start", {}).get("dateTime"):
            continue
        start = _event_start(event)
        if start is None:
            continue
        out.append(
            {
                "id": event.get("id", ""),
                "summary": event.get("summary", "Зустріч"),
                "start": start,
                "end": _event_end(event),
                "attendee_emails": [
                    a.get("email", "").lower()
                    for a in event.get("attendees", [])
                    if a.get("email") and not a.get("self")
                ],
                "meet_link": event.get("hangoutLink"),
                "html_link": event.get("htmlLink"),
            }
        )
    return out


def _primary_timezone(calendar) -> str:
    """The calendar's own timezone, so a '14:00' we create lands at the wall
    clock the user means. Falls back to UTC on any hiccup."""
    try:
        cal = calendar.calendars().get(calendarId="primary").execute()
        return cal.get("timeZone") or "UTC"
    except Exception:  # pragma: no cover - network
        return "UTC"


def create_event(
    db: Session,
    *,
    summary: str,
    start_local: str,
    end_local: str,
    attendee_email: str | None = None,
    add_meet: bool = False,
) -> dict | None:
    """Create a calendar event from wall-clock local times.

    ``start_local``/``end_local`` are naive ISO strings like
    "2026-07-09T14:00:00" — interpreted in the calendar's own timezone.
    Returns {summary, start, html_link, meet_link} or None on failure
    (not connected / missing write scope / API error)."""
    creds = _load_credentials(db)
    if creds is None:
        return None
    try:
        from googleapiclient.discovery import build

        calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
        tz = _primary_timezone(calendar)

        def _dt(value: str) -> dict:
            # If the value already carries a UTC offset (…Z / …+02:00), keep it
            # as an absolute instant; otherwise treat it as wall-clock in the
            # calendar's timezone.
            import re as _re

            has_offset = value.endswith("Z") or bool(
                _re.search(r"[+-]\d\d:\d\d$", value)
            )
            return {"dateTime": value} if has_offset else {
                "dateTime": value, "timeZone": tz,
            }

        body: dict = {
            "summary": summary,
            "start": _dt(start_local),
            "end": _dt(end_local),
        }
        if attendee_email:
            body["attendees"] = [{"email": attendee_email}]
        params: dict = {"calendarId": "primary", "body": body}
        if add_meet:
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": f"meet-{start_local}",
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
            params["conferenceDataVersion"] = 1
        if attendee_email:
            params["sendUpdates"] = "all"  # email the guest an invite
        created = calendar.events().insert(**params).execute()
        return {
            "summary": created.get("summary", summary),
            "start": created.get("start", {}).get("dateTime", start_local),
            "html_link": created.get("htmlLink"),
            "meet_link": created.get("hangoutLink"),
        }
    except Exception as exc:  # pragma: no cover - network
        logger.warning("create_event failed: %s", exc)
        return None


# ── Outbound email (for the daily digest) ────────────────────────────────────


def send_email(
    db: Session,
    to: str,
    subject: str,
    body_html: str,
    *,
    thread_id: str | None = None,
    plain: bool = False,
) -> bool:
    creds = _load_credentials(db)
    if creds is None:
        return False
    try:
        from googleapiclient.discovery import build

        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        message = MIMEText(body_html, "plain" if plain else "html", "utf-8")
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        body: dict = {"raw": raw}
        if thread_id:
            # Тримає відповідь у тому ж тредi, а не окремим листом.
            body["threadId"] = thread_id
        gmail.users().messages().send(userId="me", body=body).execute()
        return True
    except Exception as exc:  # pragma: no cover
        logger.warning("send_email failed: %s", exc)
        return False


# ── Helpers ──────────────────────────────────────────────────────────────────


def _epoch_ms_to_dt(value) -> datetime:
    try:
        return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _event_start(event: dict) -> datetime | None:
    start = event.get("start", {})
    raw = start.get("dateTime") or start.get("date")
    if not raw:
        return None
    try:
        if len(raw) == 10:  # all-day date "YYYY-MM-DD"
            return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _event_end(event: dict) -> datetime | None:
    end = event.get("end", {})
    raw = end.get("dateTime") or end.get("date")
    if not raw:
        return None
    try:
        if len(raw) == 10:
            return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── Інбокс: треди, тіла листів і чернетки ───────────────────────────────────

#: Що вважаємо «поштою, яка може вимагати відповіді»: тільки вхідна папка,
#: без промо, соцмереж і розсилок — інакше черга захлинеться шумом.
INBOX_QUERY = (
    "in:inbox -category:promotions -category:social -category:forums "
    "-category:updates"
)


def _gmail(db: Session):
    creds = _load_credentials(db)
    if creds is None:
        return None
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode(data: str | None) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data.encode()).decode("utf-8", "replace")
    except Exception:  # pragma: no cover - зіпсоване кодування не має валити синк
        return ""


def _body_text(payload: dict) -> str:
    """Текст листа: спершу text/plain, інакше html без тегів."""
    import re

    plain, html = "", ""
    stack = [payload or {}]
    while stack:
        part = stack.pop()
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if mime == "text/plain" and data and not plain:
            plain = _decode(data)
        elif mime == "text/html" and data and not html:
            html = _decode(data)
        stack.extend(part.get("parts") or [])
    if plain:
        return plain
    if html:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"[ \t]+", " ", text)
    return ""


def _headers(message: dict) -> dict:
    return {
        h["name"].lower(): h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }


def fetch_inbox_threads(db: Session, *, days: int = 14, limit: int = 60) -> list[dict]:
    """Треди вхідної пошти за період, кожен — зведений до того, що нам потрібно.

    Повертає список словників; порожній — якщо Google не підключений. Мережеві
    збої не піднімаються вище: пропущений синк не має валити планувальник.
    """
    gmail = _gmail(db)
    if gmail is None:
        return []
    me = (status(db).get("account_email") or "").lower()

    try:
        listed = (
            gmail.users()
            .threads()
            .list(
                userId="me",
                q=f"{INBOX_QUERY} newer_than:{days}d",
                maxResults=limit,
            )
            .execute()
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("inbox list failed: %s", exc)
        return []

    out: list[dict] = []
    for ref in listed.get("threads", []):
        try:
            thread = (
                gmail.users()
                .threads()
                .get(userId="me", id=ref["id"], format="full")
                .execute()
            )
        except Exception as exc:  # pragma: no cover
            logger.warning("inbox thread %s failed: %s", ref["id"], exc)
            continue

        messages = thread.get("messages") or []
        if not messages:
            continue
        last = messages[-1]
        head = _headers(last)
        first_head = _headers(messages[0])
        from_name, from_email = parseaddr(head.get("from", ""))
        from_email = from_email.lower()

        out.append(
            {
                "thread_id": thread.get("id"),
                "message_id": last.get("id"),
                "subject": first_head.get("subject") or head.get("subject") or "(без теми)",
                "from_email": from_email,
                "from_name": from_name or from_email,
                "snippet": (last.get("snippet") or "").strip(),
                "body": _body_text(last.get("payload") or {})[:8000],
                "last_at": _epoch_ms_to_dt(last.get("internalDate")),
                # Останнє слово за мною = тред уже відпрацьований.
                "last_from_me": bool(me and me in from_email),
                "messages": len(messages),
            }
        )
    return out


def create_draft(db: Session, *, thread_id: str, to: str, subject: str, body: str) -> str | None:
    """Кладе чернетку відповіді у Gmail, у той самий тред. Повертає id чернетки.

    ``None`` означає, що чернетку створити не вдалося — найчастіше тому, що
    токен виданий без скоупу gmail.compose (треба перепідключити Google).
    """
    gmail = _gmail(db)
    if gmail is None:
        return None
    message = MIMEText(body, "plain", "utf-8")
    message["to"] = to
    message["subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    try:
        created = (
            gmail.users()
            .drafts()
            .create(
                userId="me",
                body={"message": {"raw": raw, "threadId": thread_id}},
            )
            .execute()
        )
        return created.get("id")
    except Exception as exc:  # pragma: no cover
        logger.warning("create_draft failed: %s", exc)
        return None


# ── Google Tasks: задачі, видимі всередині Календаря ────────────────────────

# Id списку задач змінюється рідко; тримаємо в памʼяті ізолята, щоб не питати
# Google на кожен синк.
_tasklist_cache: dict[str, str] = {}


def _tasks_api(db: Session):
    creds = _load_credentials(db)
    if creds is None:
        return None
    from googleapiclient.discovery import build

    return build("tasks", "v1", credentials=creds, cache_discovery=False)


def ensure_tasklist(db: Session, title: str | None = None) -> str | None:
    """Знаходить (або створює) окремий список задач і повертає його id.

    Окремий список навмисно: не засмічуємо стандартний список власника, і в
    будь-який момент усе наше можна прибрати одним рухом у Google.
    """
    title = title or settings.google_tasklist_title
    cached = _tasklist_cache.get(title)
    if cached:
        return cached
    api = _tasks_api(db)
    if api is None:
        return None
    try:
        listed = api.tasklists().list(maxResults=100).execute()
        for item in listed.get("items", []):
            if item.get("title") == title:
                _tasklist_cache[title] = item["id"]
                return item["id"]
        created = api.tasklists().insert(body={"title": title}).execute()
        _tasklist_cache[title] = created["id"]
        return created["id"]
    except Exception as exc:  # pragma: no cover
        logger.warning("ensure_tasklist failed: %s", exc)
        return None


def list_google_tasks(db: Session, tasklist_id: str) -> list[dict]:
    """Усі задачі списку, включно з виконаними і прихованими."""
    api = _tasks_api(db)
    if api is None:
        return []
    out: list[dict] = []
    page = None
    try:
        while True:
            resp = (
                api.tasks()
                .list(
                    tasklist=tasklist_id,
                    showCompleted=True,
                    showHidden=True,
                    maxResults=100,
                    pageToken=page,
                )
                .execute()
            )
            out.extend(resp.get("items", []))
            page = resp.get("nextPageToken")
            if not page:
                break
    except Exception as exc:  # pragma: no cover
        logger.warning("list_google_tasks failed: %s", exc)
    return out


def upsert_google_task(
    db: Session,
    tasklist_id: str,
    *,
    task_id: str | None,
    title: str,
    notes: str | None,
    due: str | None,
    completed: bool,
) -> str | None:
    """Створює або оновлює задачу в Google. Повертає її id або ``None``."""
    api = _tasks_api(db)
    if api is None:
        return None
    body: dict = {
        "title": title[:1024],
        "status": "completed" if completed else "needsAction",
    }
    if notes:
        body["notes"] = notes[:8000]
    # Google приймає RFC3339, але з поля due бере ЛИШЕ дату — час губиться.
    body["due"] = due
    if not completed:
        # Знімаємо позначку виконання, якщо задачу відкрили назад у нас.
        body["completed"] = None
    try:
        if task_id:
            saved = (
                api.tasks()
                .patch(tasklist=tasklist_id, task=task_id, body=body)
                .execute()
            )
        else:
            saved = api.tasks().insert(tasklist=tasklist_id, body=body).execute()
        return saved.get("id")
    except Exception as exc:  # pragma: no cover
        logger.warning("upsert_google_task failed: %s", exc)
        return None


def delete_google_task(db: Session, tasklist_id: str, task_id: str) -> bool:
    api = _tasks_api(db)
    if api is None:
        return False
    try:
        api.tasks().delete(tasklist=tasklist_id, task=task_id).execute()
        return True
    except Exception as exc:  # pragma: no cover
        logger.warning("delete_google_task failed: %s", exc)
        return False
