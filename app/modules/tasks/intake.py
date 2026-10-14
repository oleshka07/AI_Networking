"""Задачі з переписки: розпізнати обіцянку чи прохання в одному повідомленні.

Без моделі — словники й регулярки: це спрацьовує на кожне повідомлення в
Telegram, і модель тут коштувала б і грошей, і затримки. Хибне спрацювання
дешеве (одне повідомлення з кнопкою «✖»), пропуск — теж, тож правило
навмисно стримане: дієслово обіцянки чи прохання ПЛЮС строк або предмет.
«Надішлю привіт мамі» — не задача: ні строку, ні предмета.

Мови: українська, російська, чеська, англійська.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

# Я зроблю (перша особа, майбутній час).
_PROMISE = (
    # uk
    r"надішлю|скину|подзвоню|зателефоную|зроблю|пришлю|перешлю|відправлю|напишу|"
    r"підготую|передам|гляну|перевірю|занесу|оплачу|"
    # ru
    r"отправлю|вышлю|позвоню|сделаю|подготовлю|оплачу|посмотрю|проверю|"
    # cs
    r"pošlu|zavolám|udělám|napíšu|připravím|přepošlu|zaplatím|"
    # en
    r"i'?ll send|i will send|i'?ll call|i will call|i'?ll do|i'?ll prepare|i'?ll share|"
    r"i'?ll forward|i'?ll get back|will send you|i'?ll pay|i'?ll check"
)
# Зроби ти (наказовий спосіб, прохання).
_REQUEST = (
    # uk
    r"надішли|надішліть|скинь|скиньте|подзвони|зателефонуй|зроби|зробіть|пришли|"
    r"перешли|відправ|підготуй|передай|глянь|перевір|оплати|"
    # ru
    r"отправь|отправьте|вышли|пришлите|позвони|сделай|сделайте|подготовь|скинь|"
    # cs
    r"pošli|pošlete|zavolej|udělej|připrav|napiš|přepošli|zaplať|"
    # en
    r"send me|please send|can you send|could you send|call me|please call|"
    r"can you prepare|please prepare|could you prepare|can you share|please share"
)
_MEETING = (
    r"зідзвон|созвон|зустрін|зустріч|встрет|встреч|zavoláme|schůzk|sejdeme|"
    r"\bmeet\b|\bmeeting\b|\bcall\b|catch up"
)
_PROMISE_RE = re.compile(r"(?<![\w'])(?:" + _PROMISE + r")(?![\w'])", re.IGNORECASE)
_REQUEST_RE = re.compile(r"(?<![\w'])(?:" + _REQUEST + r")(?![\w'])", re.IGNORECASE)
_MEETING_RE = re.compile(_MEETING, re.IGNORECASE)

#: Предмет домовленості: що саме надіслати чи зробити.
_OBJECT_RE = re.compile(
    r"рахун|фактур|invoice|faktur|документ|document|файл|file|soubor|договір|договор|"
    r"smlouv|contract|звіт|отчет|отчёт|report|zpráv|презентац|presentation|prezentac|"
    r"фото|photo|посилан|ссылк|\blink|odkaz|лист\b|листа|письм|e-?mail|прайс|price|"
    r"кошторис|смет|пропозиц|предложен|offer|nabídk|макет|mockup|дизайн|design|"
    r"доступ|access|přístup|таблиц|table|tabulk|резюме|\bcv\b|бюджет|budget|rozpoč|"
    r"оплат|платіж|платеж|payment|platb|кошти|деньг|peníze|адрес|address|номер|number",
    re.IGNORECASE,
)

_REL_DAYS = (
    (r"післязавтра|послезавтра|pozítří|day after tomorrow", 2),
    (r"завтра|tomorrow|zítra", 1),
    (r"сьогодні|сегодня|today|dnes|tonight|ввечері|вечером", 0),
)
#: Основи днів тижня (Пн=0). Межа слова — щоб «понеділка» не влучив у «неділ».
_WEEKDAYS = (
    (r"понеділ|понедельн|pondělí|pondělk|monday", 0),
    (r"вівтор|вторник|úterý|tuesday", 1),
    (r"серед[уиа]|сред[уаы]|středu|středa|wednesday", 2),
    (r"четвер|четверг|čtvrtek|čtvrtk|thursday", 3),
    (r"пʼятниц|п'ятниц|пятниц|пятниц|pátek|pátku|friday", 4),
    (r"субот|суббот|sobot|saturday", 5),
    (r"неділ|воскресен|neděl|sunday", 6),
)
_END_OF_WEEK_RE = re.compile(
    r"кінц\w* тижн|конц\w* недел|end of (?:the )?week|konce týdne", re.IGNORECASE
)
_NEXT_WEEK_RE = re.compile(
    r"наступн\w* тижн|следующ\w* недел|next week|příští týden", re.IGNORECASE
)
_DDMM_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")
_TIME_RE = re.compile(r"\b\d{1,2}[:.]\d{2}\b|\b(?:о|в|у|at|v)\s+\d{1,2}\b", re.IGNORECASE)
# «до», «do», «by» самі по собі надто часті («дійду до тебе», «I'll do it»),
# тож лише разом зі словом-строком.
_DAY_WORDS = (
    r"\d|завтр|кінц|конц|обід|вечор|ранк|понеділ|понедельн|вівтор|вторник|серед|сред|"
    r"четвер|пʼятниц|п'ятниц|пятниц|субот|суббот|неділ|воскрес|наступн|следующ"
)
_SOFT_DEADLINE_RE = re.compile(
    r"\bдо\s+(?:" + _DAY_WORDS + r")|"
    r"\bby\s+(?:\d|tomorrow|tonight|eod|end|next|monday|tuesday|wednesday|thursday|friday|weekend)|"
    r"\bdo\s+(?:\d|zítř|pátk|pondělí|úterý|středy|čtvrtk|soboty|neděle|konce)|"
    r"через\s+\d|за\s+\d+\s*(?:дн|день|годин)|"
    r"на\s+(?:цьому|этой)\s+(?:тижні|неделе)|this week|tento týden",
    re.IGNORECASE,
)


def due_from_text(text: str, today: date) -> date | None:
    """Строк із тексту: відносний день, день тижня, кінець тижня, дд.мм."""
    t = (text or "").lower()
    for pattern, offset in _REL_DAYS:
        if re.search(pattern, t):
            return today + timedelta(days=offset)
    for pattern, idx in _WEEKDAYS:
        if re.search(r"(?<!\w)(?:" + pattern + r")", t):
            return today + timedelta(days=((idx - today.weekday()) % 7 or 7))
    if _END_OF_WEEK_RE.search(t):
        return today + timedelta(days=(4 - today.weekday()) % 7)
    if _NEXT_WEEK_RE.search(t):
        return today + timedelta(days=7 - today.weekday())
    m = _DDMM_RE.search(t)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            found = date(year, month, day)
        except ValueError:
            return None
        if not m.group(3) and found < today:
            found = date(year + 1, month, day)
        return found
    return None


def _has_deadline(text: str, due: date | None) -> bool:
    return due is not None or bool(_TIME_RE.search(text) or _SOFT_DEADLINE_RE.search(text))


def detect_commitment(text: str, today: date) -> dict | None:
    """Обіцянка, прохання чи домовленість про дзвінок — або ``None``.

    ``{"type": "promise" | "request" | "meeting", "due": date | None}``.
    ``promise`` — автор обіцяє зробити сам; ``request`` — просить
    співрозмовника; ``meeting`` — домовленість зустрітися чи зідзвонитися.
    """
    text = " ".join(str(text or "").split())
    if len(text) < 6:
        return None
    due = due_from_text(text, today)
    deadline = _has_deadline(text, due)
    concrete = deadline or bool(_OBJECT_RE.search(text))

    if _MEETING_RE.search(text) and deadline:
        return {"type": "meeting", "due": due}
    if not concrete:
        return None
    if _PROMISE_RE.search(text):
        return {"type": "promise", "due": due}
    if _REQUEST_RE.search(text):
        return {"type": "request", "due": due}
    return None


def kind_for(commitment_type: str, *, outgoing: bool) -> str:
    """Чия це задача: обовʼязок (я винен) чи очікування (мені винні).

    Свою обіцянку і чуже прохання виконую я — ``duty``. Своє прохання і
    чужу обіцянку чекаю від іншого — ``expectation``. Зустріч — звичайна задача.
    """
    if commitment_type == "meeting":
        return "task"
    mine = (commitment_type == "promise") == outgoing
    return "duty" if mine else "expectation"


def title_for(kind: str, commitment_type: str, name: str, text: str, *, outgoing: bool) -> str:
    snippet = " ".join(str(text or "").split())
    snippet = snippet if len(snippet) <= 120 else snippet[:119] + "…"
    if commitment_type == "meeting":
        head = f"Зустріч/дзвінок з {name}"
    elif kind == "duty":
        head = f"Обіцяв {name}" if outgoing else f"{name} просить"
    else:
        head = f"Чекаю від {name}" if outgoing else f"{name} обіцяв"
    return f"{head}: {snippet}"[:300]
