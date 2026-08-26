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

JSONL-вход: `{"user": "текст"}` или `{"stop": true}`.
Из стенда: `hello` → `reply` после каждого ответа Ивана → `done` с профилем.

В коде:

```python
from stokozavr_bot.qa_stand import IsolatedQASession

session = IsolatedQASession(
    persona="...", scenario="...", goal="...", max_turns=8, ai=fake_or_omit_for_live
)
await session.start()
reply = await session.send("какие фрукты есть?")
# прочитать reply.text и выбрать следующий ход
result = await session.finish()
```

Транскрипт: user/assistant + финальный `ClientProfile`. Секреты и сырые API-ответы не пишутся. Вложения прайса — только имя файла.
