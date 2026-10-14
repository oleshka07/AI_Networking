# Чек-лист Claude ⇄ задачі AI Networking

Власник веде свої задачі в чек-листі «Задачі Олега» — сторінці-артефакті на
claude.ai, куди сесії Claude пишуть картки з кроками, текстами листів і
рішеннями. AI Networking до цієї сторінки не дістає: її база доступна лише
Claude. Тому міст — це Claude, а сервер робить те, що вміє: зберігає задачі,
нагадує, шле в Telegram і дзеркалить у Google Tasks.

```
 чек-лист (claude.ai)  ⇄  Claude: синк кожні ~3 год + після змін  ⇄  MCP ?profile=sync  →  tasks
                                                                                         ├─ Telegram: план, блокери, кнопки
                                                                                         ├─ Google Tasks / Календар (gsync)
                                                                                         └─ сайт: Tasks
```

Задачі з чек-листа — звичайні рядки `tasks` з `source='claude'`. Окремої
сутності немає: один список задач (ARCHITECTURE §14).

## Хто головний для чого

| Що | Хто вирішує |
|---|---|
| назва, нотатки, проєкт, строк задачі `source='claude'` | чек-лист: синк перезаписує їх щоразу |
| виконання | обидва боки; «done» липке — закрили в одному місці, закрито всюди |
| перевідкрити закриту | лише той бік, що закрив (`tasks.done_via`: `source` або `server`) |
| нагадування | сервер, за своїм розкладом; частота синку впливає лише на свіжість даних |
| кроки, тексти листів, рішення, примітки | лише чек-лист; на сайт і в Telegram іде назва й посилання «Картка» |

## Підключення

Синк — це запланована задача Claude (і сесії-контролери після своїх змін), яка
ходить у сервер через MCP. Потрібна та сама адреса конектора, що в
[MCP.md](MCP.md), **з профілем**: `<адреса>?profile=sync`. Профіль показує
шість інструментів замість двох десятків — менше схем у контексті задачі.
Без профілю синк-інструментів у списку немає (інтерактивний набір лишається
компактним), хоча викликати їх можна.

Адреса — пароль: її знає лише власник і налаштування конекторів Claude. У
репозиторій, звіти й коміти вона не потрапляє.

## Інструменти профілю `sync`

Відповіді — **JSON рядком**: їх читає програма, не людина.

| Інструмент | Що робить | Відповідь |
|---|---|---|
| `crm_upsert_task` | створює або оновлює задачу за (`source`, `external_id`); повтор не дублює | `{"uid", "created", "status"}` + `"conflict"` |
| `crm_list_task_changes` | усе створене, змінене, закрите чи видалене після `since` | `{"server_time", "changes": [...], "truncated"}` |
| `crm_link_task` | звʼязує рідну задачу сервера (з Telegram, календаря) з карткою | `{"uid", "linked"}` |
| `crm_close_missing` | закриває задачі джерела, яких немає в `keep_external_ids` | `{"closed": [...], "count"}` |
| `crm_complete_task` | закрити задачу (як кнопка в Telegram) | текст |
| `crm_send_owner_message` | написати власнику в Telegram | текст |

### Приклади (вигадані)

```json
// crm_upsert_task {"source":"claude","external_id":"demo-supplier-call","title":"Подзвонити постачальнику","project":"ALiSiO","url":"https://claude.ai/artifact/<id>#t-demo-supplier-call","due_date":"2026-10-18","duration_min":10,"tags":["продажі"]}
{"uid": "a1b2c3", "created": true, "status": "todo"}

// той самий виклик зі "status":"todo", коли задачу закрили кнопкою в Telegram
{"uid": "a1b2c3", "created": false, "conflict": "done_on_server", "status": "done"}

// crm_list_task_changes {"since":"2026-10-14T06:00:00+00:00","sources":["claude","telegram","calendar"]}
{"server_time": "2026-10-14T09:00:00.123456+00:00", "truncated": false, "changes": [
  {"uid": "a1b2c3", "source": "claude", "external_id": "demo-supplier-call", "linked": null,
   "url": "https://claude.ai/artifact/<id>#t-demo-supplier-call", "title": "Подзвонити постачальнику",
   "project": "ALiSiO", "status": "done", "kind": "task", "blocks_work": false,
   "done_at": "2026-10-14T08:12:00+00:00", "due_date": "2026-10-18", "counterpart": null,
   "created_at": "2026-10-13T07:00:00+00:00", "updated_at": "2026-10-14T08:12:00+00:00"},
  {"uid": "d4e5f6", "source": "telegram", "external_id": "tgb:100200:345", "linked": null,
   "title": "Обіцяв Марта: надішлю рахунок до пʼятниці", "status": "todo", "kind": "duty", "...": "..."}
]}

// crm_close_missing {"source":"claude","keep_external_ids":["demo-supplier-call","demo-invoice"]}
{"closed": ["0a9b8c"], "count": 1}
```

## Контракт: картка → задача

| Картка | Задача |
|---|---|
| `id` (напр. `os-posta-dosilka`) | `external_id`, `source='claude'` |
| `https://claude.ai/artifact/<id>#t-<id картки>` | `url` → `external_url` |
| `title` | `title` (до 300) |
| `project`: `alisio` / `brand` / `eet` / `holos` / `ucetni` / `vary` / `osobiste` | `project`: «ALiSiO», «Особистий бренд», «EvidujZdarma», «Holos», «Účetnictví», «Vary Servis», «Особисте» |
| `blocksClaude: true` | `blocks_work=true`, `status='urgent'`, строк — сьогодні |
| `section: today` | `status='todo'`, строк — сьогодні |
| `section: week` | `status='todo'`, строк — неділя поточного тижня |
| `section: later` / `learn` | `status='todo'`, без строку (`due_date: ""`), тег `@later` / `@learn` |
| `minutes` | `duration_min` |
| `tags` | `tags` (масив або рядок через кому) |
| `why` | `notes` (до 500 символів) + рядок «Кроки й тексти — у картці» |
| `done: true` | `crm_upsert_task` зі `status: "done"` |

Закривати картку з чек-листа краще через `crm_upsert_task` зі `status: "done"`:
тоді `done_via='source'`, і якщо галочку в чек-листі знімуть, наступний upsert
відкриє задачу знову. `crm_complete_task` теж закриває, але як сервер — тоді
перевідкрити з чек-листа не вийде (прийде `conflict`).

Синк береться лише за картки власника (`track: "oleg"`); задачі самих сесій
(`track: "claude"`) не переносяться.

## Контракт: задача → картка

1. `crm_list_task_changes` з `since` = збережений `server_time` попереднього
   проходу і `sources: ["claude", "telegram", "calendar"]`.
2. Задача `source='claude'` (або з `linked` на `claude:`) у статусі `done` —
   картці `done: true` і примітка «закрито в AI Networking».
3. `deleted: true` — картку теж закрити: задачу прибрали на сервері (наприклад,
   кнопкою «без підготовки»).
4. Нова задача `telegram` чи `calendar` без `linked` — створити картку у
   вкладці «Особисте» (або в проєкті, якщо він зрозумілий з назви) і одразу
   `crm_link_task` з її id. Після цього `crm_upsert_task(source='claude',
   external_id=<id картки>)` оновлює саме цю задачу, а не створює другу.
5. Решта рідних задач сервера (GTD, ворота, нагадування) лишається тільки в
   AI Networking: чек-лист не засмічується.
6. Наприкінці — `crm_close_missing(source='claude', keep_external_ids=[id усіх
   відкритих карток власника])`: картки, які видалили, поки синк не бачив,
   закриваються з позначкою «закрито в джерелі». Порожній список без
   `confirm_all: true` відхиляється — це частіше збій читання чек-листа.

## Конфлікти

| Відповідь | Що це | Що робити |
|---|---|---|
| `conflict: done_on_server` | задачу закрили тут (Telegram, сайт, Google) | закрити картку |
| `conflict: deleted_on_server` | задачу прибрали тут свідомо | закрити картку, не створювати знову |
| помилка «уже звʼязана» в `crm_link_task` | на цю картку вже є задача | звʼязати іншу картку або закрити дубль |

## Як діагностувати

1. **Сайт → Tasks → панель «Синк з чек-листом»**: час і назва останнього
   виклику синку, скільки задач чек-листа відкрито й закрито, остання помилка.
   Порожньо — синк жодного разу не звертався: перевірити конектор і профіль.
2. **Помилка в панелі** — це текст, який отримав Claude (`isError: true`), і
   він дієвий: невідоме джерело, задовгий `external_id`, нова задача без `title`.
3. **Дубль задачі** — подивитися `crm_list_task_changes` без `since`: у дубля
   буде інший `external_id` або не той `source`. Пара (`source`, `external_id`)
   унікальна серед непорожніх — це тримає сервіс, а не індекс.
4. **Картка не закривається** — у зміні має бути `status: "done"` або
   `deleted: true`; якщо їх немає, задачу на сервері не закривали.

Код: `app/modules/tasks/external.py` (логіка), `app/modules/mcp/tools.py`
(інструменти й профіль), тести — `tests/test_tasks_external.py`.
