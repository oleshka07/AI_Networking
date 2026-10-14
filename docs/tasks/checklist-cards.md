# Картки чек-листа «Задачі Олега» — що в них є

Чек-лист — сторінка-артефакт на claude.ai зі своєю базою документів (колекції `tasks`, `projects`, `contacts`, `meta`).
Писати в неї вміють лише сесії Claude (інструмент ArtifactData). Сервер AI Networking її не бачить і не повинен:
усе, що йому треба, приносить синк через MCP.

## Документ `tasks/<id>` (картка)

| Поле | Що це |
|---|---|
| `title` | дієслово + результат |
| `project` | ключ вкладки: `alisio`, `brand`, `eet`, `holos`, `ucetni`, `vary`, `osobiste` |
| `track` | `oleg` — задача власника; `claude` — задача самої сесії (не синхронізується) |
| `section` | `today`, `week`, `later`, `learn` |
| `order` | порядок у розділі |
| `blocksClaude` | картка тримає роботу сесій |
| `minutes` | оцінка часу |
| `tags` | масив міток |
| `why` | навіщо, 2–4 речення |
| `who` | хто робить |
| `steps` | кроки: рядок або `{label, text, url, copy}` |
| `actions` | кнопки: `{label, url}` або `{via: whatsapp/gmail/draft/file, …}` |
| `copies` | готові тексти `{label, text, send[]}` мовою адресата |
| `decisions` | вибір кнопками `{id, q, rec, options[{key, label, note}]}`; відповідь власника — `answers` |
| `note`, `noteAt` | примітка власника |
| `reply`, `replyAt` | відповідь сесії на примітку |
| `done`, `doneAt` | галочка «зроблено» |
| `createdAt`, `updatedAt`, `textAt` | час |

## Що з цього потрапляє в AI Networking

Лише те, що потрібно для нагадувань і огляду: `id`, посилання на картку, `title`, `project`, `section`, `blocksClaude`,
`minutes`, `tags`, коротко `why`, `done`. Кроки, тексти, рішення й примітки лишаються в чек-листі — кнопка «Картка» на
сайті й у Telegram веде туди.

## Приклад (вигаданий)

```json
{
  "id": "demo-supplier-call",
  "title": "Подзвонити постачальнику й погодити дату доставки",
  "project": "alisio",
  "track": "oleg",
  "section": "week",
  "blocksClaude": false,
  "minutes": 10,
  "tags": ["продажі"],
  "why": "Без дати доставки не можна поставити запуск у план.",
  "done": false,
  "createdAt": "2026-10-14T07:00:00Z"
}
```

Посилання на картку: `https://claude.ai/artifact/EyyTFVP37z5ioF94yJMefC#t-<id>` (відкривається лише у власника, з
входом у claude.ai).
