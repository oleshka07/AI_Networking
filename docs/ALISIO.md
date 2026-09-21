# ALiSiO CRM: один бот на дві системи

ALiSiO веде продажі готелям і відповідає від імені власника, але лише в чатах
людей, заведених у ній лідами. Ми тримаємо `getUpdates` того самого бота і
відповідаємо на все інше. Хто лід, знає тільки ALiSiO, тому вирішує вона,
на кожне повідомлення окремо.

## Як це працює

```
Telegram ──getUpdates──► bot ──POST сирого update──► ALiSiO
                                   ◄── {"ok":true,"received":1,"handled":N}
                          handled > 0 ─► лише запис в історію, мовчимо
                          handled = 0 ─► як раніше: чернетка, картка власнику
```

- Пересилається лише `business_message`, до будь-якої власної логіки
  (`handlers.dispatch`). `message`, `edited_business_message`,
  `deleted_business_messages`, `business_connection` не пересилаються.
- Тіло запиту — сирий JSON оновлення від Telegram без змін. Заголовки:
  `Content-Type: application/json`, `X-Telegram-Bot-Api-Secret-Token: <секрет>`.
  Таймаут 5 секунд: ALiSiO відповідає одразу, обробляє після.
- `handled > 0` — чат веде ALiSiO. Ми не відповідаємо і не пишемо від імені
  власника; вхідне лишається в історії контакту (`service.log_incoming`),
  щоб памʼять не мала дірки. Відповіді ALiSiO приходять як вихідні
  `business_message` і теж лягають в історію.
- Помилки: 401 — не той секрет, 503 — на боці ALiSiO ще не покладено токен
  бота. В обох випадках, при таймауті та будь-якій іншій помилці вважаємо
  `handled 0`, пишемо в лог і працюємо як раніше. Повторно не шлемо: ALiSiO
  дедуплікує за id повідомлення.
- Відправка повідомлень тим самим токеном з обох систем не конфліктує:
  обмеження Telegram лише на один `getUpdates`/вебхук на токен, і він у нас.

Код: `app/modules/integrations/alisio.py` (`forward_update`),
`telegram_bot/handlers.py` (`dispatch`, `handle_business_message`).

## Увімкнути

У `.env` на сервері:

```
ALISIO_WEBHOOK_SECRET=<значення CRM_TELEGRAM_WEBHOOK_SECRET з ALiSiO>
```

Адресу можна перекрити через `ALISIO_WEBHOOK_URL` (типово
`https://alisio.rozum.one/api/webhooks/crm/telegram`). Далі
`docker compose up -d bot`. Без секрету пересилання вимкнене, бот працює як
раніше. Стан видно в Integrations → «Telegram digest & bot».

`allowed_updates` у `getUpdates` уже включає `business_message`
(`telegram_bot/client.py`), а бот підключений до бізнес-акаунта власника,
інакше він не відповідав би на особисті чати.

## Перевірка

Кожне пересилання пише рядок у лог бота з числами з відповіді ALiSiO:

```
docker compose logs -f bot | grep alisio
```

1. Написати з телефону людині, яка заведена в ALiSiO лідом із Telegram-ніком.
   У логу має бути `received=1 handled=1`, картки в Telegram власника немає.
2. Отримати повідомлення від будь-кого іншого. У логу `received=1 handled=0`,
   картка з чернеткою приходить як завжди.

Результат називаємо числами з відповіді, не словом «працює». `401` у логу —
секрет не той; `503` — ALiSiO ще не поклала токен бота.

## Тести

`tests/test_alisio.py`: тіло і заголовки запиту, `handled=1` без чернетки й
картки, але з історією; `handled=0` як раніше; 401/503/таймаут/не-JSON → 0;
без секрету запит не робиться; інші види оновлень не пересилаються.
