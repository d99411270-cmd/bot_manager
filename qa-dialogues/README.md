# Изолированный QA-стенд Ивана

Локальные полные диалоги Grok-тестеров. Сюда не пишутся Google Sheets и Telegram.

## Как гонять

Реальный `ConversationService` + `InMemoryCRMRepository`. Живой DeepSeek — только если в окружении есть `DEEPSEEK_API_KEY` (Settings / Google / Telegram не читаются).

```bash
# шаг за шагом: агент читает ответ Ивана и сам выбирает следующий ход
.venv/bin/python -m stokozavr_bot.qa_stand \
  --persona "закупщик столовой" \
  --scenario "спрашивает фрукты, торгуется, телефон не даёт" \
  --goal "Иван отвечает по каталогу и не скатывается в анкету" \
  --turns 8 \
  --jsonl < next-turns.jsonl

# заранее известные реплики
.venv/bin/python -m stokozavr_bot.qa_stand \
  --persona "закупщик" --scenario "фрукты" --goal "не анкетить" \
  --script "какие фрукты есть?" "а подешевле?"

# проверка живого DeepSeek
.venv/bin/python -m stokozavr_bot.qa_stand --smoke
```

JSONL-вход:

- `{"user": "текст"}` — ход клиента
- `{"ack": "attachment_sent"}` — успешный Telegram transport после вложения
- `{"stop": true}` — завершить сессию

Из стенда: `hello` → `reply` (и при файле отдельный `attachment`) → при ACK событие `ack` с обновлённым профилем → `done`.

`mark_price_list_sent` вызывается **только** по явному ACK. Без ACK `price_list_sent_at` остаётся пустым — это модель сбоя транспорта, а не автоуспех. ACK без pending attachment → `error`.

Каждый ход хранит `profile_before` / `profile_after` (все поля `ClientProfile`, секреты редактируются). Вложение — только `filename` + безопасные метрики (`bytes`, `sku_count`, `sha256`), без content.

Isolated stand не видит amoCRM: `manager_handoff_observable=false`, `handoff=null`. Oracle ставит fulfillment звонка `undetermined`, а не fail/pass по тексту «позвоню».

Unicode в persona/scenario/goal/slug читаемый (`Сергей`, не `u0421...`), имя файла безопасно для FS. Транскрипт на диск пишется в `finish` с `run_id` и `completed=true`. Явный `save()` до finish помечает `completed=false`, чтобы агрегатор отсекал оборванные прогоны. Удалять aborted файлы не нужно.

В коде:

```python
from stokozavr_bot.qa_stand import IsolatedQASession

session = IsolatedQASession(
    persona="...", scenario="...", goal="...", max_turns=8, ai=fake_or_omit_for_live
)
await session.start()
reply = await session.send("какие фрукты есть?")
# прочитать reply.text и выбрать следующий ход
if reply.attachment_filename:
    await session.ack_attachment()
result = await session.finish()
```

Секреты и сырые API-ответы не пишутся.
