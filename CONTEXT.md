# CONTEXT.md

## Проект

Отдельный Telegram-бот для «Стокозавра». Путь: `/home/hermes/projects/stokozavr-telegram-bot`. GitHub: https://github.com/d99411270-cmd/bot_manager (public). Это не сайт `/home/hermes/projects/zavod`; файлы сайта в этой работе не менялись.

## Текущее состояние

- Python / aiogram 3, long polling.
- `ConversationService` — оркестратор, не анкета. DeepSeek пишет клиенту живьём; код — инструменты (каталог, quote, телефон, прайс-файл, handoff) и стоп-кран на выдуманные ₽/наличие. Код не клеит `PRICE_LIST_OFFER` и не подменяет ответ анкетой / FALLBACK / «не могу подтвердить», если SKU известен. SYSTEM_PROMPT больше не приказывает отвечать ровно FALLBACK и не приказывает предлагать прайс: нет факта в поиске/расчёте — живой отказ и `needs_human`, без «уточню и вернусь». Slice H live 679025492: «Вся/все категории/все продукты которые есть» — полный ассортимент, не SKU, не объём и не no-match: код предлагает прайс на почту («вам отправят»), файл в чат только при отказе; «какие овощи?» — обзор категории без автооффера; морковь/морква «за кг» / «за 1 кг» — 41 ₽/кг с мешка 10 кг, не dump и не volume=`1 кг`. Морс «за литр» — 106.67 ₽/л; «200 литров» / «Литров 200» — заказ, ближайшие фасовки 33 уп = 198 л = 21120 ₽ и 34 уп = 204 л = 21760 ₽ в `catalog_result` + `allowed_amounts`, не 200×106.67. `is_unsafe_claim` принимает суммы из structured quote (`allowed_amounts` / ₽/кг / ₽/л / ₽/шт), не только сырой catalog text. Slice C: structured line totals. Slice F: pickup≠call / ManagerHandoff. Slice G: два короба / alias консервы / канал звонка / CLOSE_ASK_TIME после телефона. Round-4: «магазин/кафе/ресторан/столовая/сеть» после «чем занимаетесь?» / «магазин или кафе?» — тип клиента, не SKU и не `catalog_no_match`; пустой «Устраивает такой расчёт?» при известном quote отбраковывается, клиенту уходят оба края 21120/21760 или 1720; «пару/пара/две банки» = 2 банки → nearest pack 860, не dump свежих овощей; подтверждённый `requested_slot` не переспрашивается на смешанном «итого 4 сетки это 3000?»; «3 мешка» / «бери 3 мешка» пишут volume. Канал только `pickup|call`, не «доставка».
- `/start` нового клиента — точный `START_TEXT`. Дальше любое сообщение, кроме явного валидного телефона/контакта, идёт в AI-менеджера.
- `analyze_intake` остаётся компактным JSON-экстрактором сущностей. Клиентский ответ на вопрос / отказ / оффтоп / «кто вы» / ассортимент идёт через `respond()` + prompt bundle + tool `search_catalog`.
- Вопрос про ассортимент, фрукты, консервацию или цену не подменяется шаблоном `PRODUCT_QUESTION` / `PRODUCT_ASSORTMENT`: `ConversationService` детерминированно вызывает локальный `search_catalog` до AI. Результат передаётся DeepSeek для естественной формулировки; при отбраковке/ошибке используется одноразовый repair-запрос, затем безопасный каталоговый fallback с товарами, производителями, фасовками, ценами и наличием. Для multi-intent сначала покрываются прямыми ответами все вопросы клиента о работе компании, затем допускается максимум один нужный вопрос.
- Телефон по-прежнему валидирует код при явном номере или Telegram contact.
- Identity-слоты (имя/фамилия/телефон/landline/email) пишутся только как `requested_slot × intent × value_kind`: greeting/`Прив`, вопрос «зачем имя», товарный токен «яблоки»/`риса`, «всё беру» не становятся именем. `parse_person_name` больше не гоняется по сырому тексту без capture-intent. Landline только из numeric phone-stage (`phone_digit_attempt`), не из «100 кг это 4 сетки…». Продукт на живом слоте телефона/почты AI не записывает (нельзя скипнуть анкету промптом). Каталог/товар/цена на слоте телефона сначала отвечают по каталогу (огурцы в банках → маринов/860), телефон — максимум один следующий вопрос, не вместо ответа; статус остаётся `ожидает телефон`. Volume-only «12 банок» при `current_interest` огурцы маринованные на слоте телефона (без phone, `product=None`) даёт 1720, не «Очень приятно, Лена»+phone; слот не снимается. На слоте телефона `current_interest` всё равно пишется (`_bind_current_interest`), `product` остаётся None. Анафора «в наличии есть?» при известной моркови отвечает 410/много, не FALLBACK «уточню и вернусь». Явный объём из текста клиента (`4 сетки`, `10 упаковок`, `36 банок`, `20 кг`, `999 коробок`) сохраняется и без контакта; выдуманный `semantic.volume` на слоте телефона отбрасывается. После валидного мобильного, если уже есть `product` / `current_interest` / `original_interests` / `volume`, следующий ход — `CLOSE_ASK_TIME` (или подтверждение `requested_slot`), не `PRODUCT_QUESTION`. Для каталогового диалога `current_interest` — активная тема на гранулярности позиции; `product` — подтверждённые позиции заказа; `original_interests` сохраняет предыдущие темы. Follow-up «А какие цены?» использует текущий интерес; новая тема («а овощи?», «консервы какие?», «теперь интересует сок») ищет текущую фразу через alias/stem (`консервы`→`консервация`, `морква`→`морковь`), не stale масло. Sticky no-match (`catalog_no_match_query`) только для той же неизвестной сущности, volume-only и анафоры (`а этот?`); «тогда макароны» / «а какие овощи есть?» очищают sticky. «Вся/все/всё/всё что есть/что есть» — browse категорий (`search("")`), не product и не no-match. При пустом каталоге явно названный неизвестный товар не записывается как `product`.
- После квалификации `_handle_ai` применяет `turn.product` / `turn.volume`. Подтверждённый объём не перезаписывается фрагментом фасовки (`12×340г`, «короб точно 10 кг?») и не затирается unit-price «за литр/кг»; фасовка не становится первым объёмом. Явная новая qty на **новой теме** («нет, давай картофель 100 кг») перезаписывает volume и кладёт предыдущую тему в `original_interests` даже если `product` ещё null и статус `ожидает телефон` (`_bind_current_interest` на unit/line quote, не только `_apply_intake_facts` при `_may_write_commercial_facts`); явная новая qty на **той же теме** с другим количеством («пару банок» → «12 банок») тоже перезаписывает volume; quantity-only без смены числа на той же теме только заполняет пустой слот; составной заказ (картофель 100 кг + рожки 10 упаковок) первую qty не роняет. Явные `36 банок` / `20 кг` / `10 упаковок` / `4 сетки` / `200 литров` / `Литров 200` / `3 мешка` / `пару банок` сохраняются. `parse_requested_quantity` читает словесные числительные (`два/две короба`, `пару/пара банок`), обратный порядок «литров 200» и не берёт размер фасовки из «короб 10 кг 820». Нецелое количество даёт `NearestPackQuote` (floor+ceil), не выдуманную смесь: 20 кг яблок → 2 короба = 1640 ₽; 100 кг картофеля → 4 сетки = 3000 ₽; 200 л морса → 33/34 упаковки = 21120/21760; 2 банки маринованных → от упаковки 6 шт / 860 ₽; 12 банок = 1720. Если SKU+volume известны и quote есть, пустой «устраивает?» / stub «не могу подтвердить» отбраковываются. Текущая фраза бьёт stale `current_interest`: «огурцы в банках» / банка → CAN-PICKLES, не dump VEG; parent-категория из intake (`овощи`) не пишется как product. «а за литр?» держит морс 106.67, не воду 33.33. Производитель на уникальном SKU отвечает из каталога (яблоки — Садовый Север); розничный stub без чужого бренда. Handoff только если производителя нет (категория «напитки»). После `is_irritated` флаг `pause_volume_prompt` — не переспрашивать объём на следующих ходах, пока клиент сам не даст volume. `fulfillment_channel=call` + `requested_slot` — подтвердить слот звонка, не «приехать» и не переспрашивать «во сколько?». Round-4 кафе≠SKU сохранён.
- `product_catalog.py` ищет markdown в `catalog/` (env `STOKOZAVR_CATALOG_DIR`, `catalog/` в корне репо, пакетные данные в wheel). `search("фрукты")` находит `frukty.md`. Пустой поиск — список категорий из заголовков. Поисковый слой и quote/topic-guard делят `catalog_tokens.py`: русские основы (`риса` → рис), алиасы и modifier-aware scoring (`свежие`/`короткоплодные` → VEG-CUCUMBER-001, `маринованные` / «в банках»/банка → CAN-PICKLES-001; голое `огурцы` остаётся неоднозначным и может показать оба).
- `product_catalog.py` принимает коммерческую запись только при наличии всех полей: категория → подкатегория → SKU → производитель → фасовка → цена → статус наличия → дата обновления ISO; неполные строки игнорируются.
- Локальный тестовый каталог: 30 разных основных товарных позиций и 30 конкурентных **розничных stub**-записей в `catalog/*.md` (`Производитель: розничные сети`, те же фасовки и прежние ₽, `Тип: конкурент; Для SKU`). Обычный поиск и прайс используют только primary. На конкретной primary-позиции (есть? / почём / наличие SKU) Иван **сам один раз** сравнивает с обычными сетевыми магазинами по цене stub (`доходит до 810 ₽`), без чужих брендов. Не в категорийном списке и не в прайс-файле. Opt-in «подешевле»/«есть варианты»/«сравнить» по-прежнему открывает сравнение. В диалоге счётчик считает **видимые** розничные сравнения (`сетев`/`розничн` или competitor-only цена с ₽), максимум два за разговор, не подряд. «Сравните» может быть вторым разом. Слова «сравн/альтернатив/конкурент» в ответе больше не сжигают весь текст; третье сравнение снимает розницу и оставляет primary, без «уточню и вернусь». «подешевле» может назвать 890 без 990 — счётчик не растёт, пока розница не показана.
- `product_catalog.py` принимает коммерческую запись только при наличии всех полей: категория → подкатегория → SKU → производитель → фасовка → цена → статус наличия → дата обновления ISO; неполные строки игнорируются.
- В записях каталога поддерживаются отдельные конкурентные варианты через `Тип: конкурент` и `Для SKU`; прайс их всегда исключает.
- `generated_price_list()` в `product_catalog.py` — источник истины для клиентского прайса: ровно 30 primary, товар/SKU/производитель/фасовка/цена, без дат, наличия и точных остатков. Pending-action прайса (`price_list_requested`) отделён от CRM-слота `EMAIL_QUESTION`. Одна pending-машина: голый `прайс`/`каталог`/`прайс есть?`/`прайс можно?`/`давайте прайс` и полный ассортимент (`wants_full_assortment`: «вся», «все категории», «все продукты», «весь ассортимент»; «еда»/«продукты питания») — сначала почта (`PRICE_LIST_EMAIL_OFFER` / `FULL_ASSORTMENT_EMAIL_OFFER`), без attachment, не «я отправил». Файл в чат только по явной просьбе «в чат / сюда / в Telegram» или отказ от почты после этого вопроса. Почта сохраняется с ack «Ок, почту записал. Вам отправят актуальный прайс.» (mailer нет). «пишите в этот чат» без предшествующего прайса — `contact_skipped`, не файл. Отказ (`прайс не надо`, `прайс повторно не надо`, `файл уже прислали`) отменяет pending. `mark_price_list_sent` только после реального `answer_document` / QA ACK. На **первом** товарном ходе (категория/SKU/ассортимент) код один раз дописывает развилку «прайс на почту или подсказать в чате»; флаг `Прайс-консультация предложена` в комментарии, без новой колонки Sheets и без поля профиля. Не префикс на каждый список. Если уже email/file path этого хода — развилку не клеить. «в чате / подскажите тут» после развилки — каталог дальше, не файл и не «без прайса». Старый автопрефикс `PRICE_LIST_OFFER` не возвращался.
- Hatch `force-include`: `prompts` → `stokozavr_bot/prompts`, `catalog` → `stokozavr_bot/catalog`.
- `context_builder.py` — единственная сборка контекста: `profile`, `missing_fields`, `deal_stage`, `returning`, `interests`, `recent_history`. Без `telegram_id`; отдельный `landline` не считается мобильным контактом.
- Профиль хранит `original_interests` отдельно от `current_interest`, чтобы переход с творожков на напитки/сок не затирал исходный интерес; профиль также хранит `pending_manager_question` для будущего уведомления менеджеру, `catalog_no_match_query` для sticky no-match и `price_list_requested`/`price_list_sent_at` для состояния прайса. Состояние прайса сохраняется в совместимом поле комментария Google Sheets (`Прайс запрошен` / `Прайс отправлен: ISO`); состояние no-match сохраняется там же как `Товар не найден: <исходный запрос>`. После старого email-вопроса pending также восстанавливается по последней паре истории. `needs_human` сохраняется в совместимом поле комментария CRM. `volume` устойчиво извлекает «36 банок», «20 упаковок», «полпаллеты», «50 литров», «200гр» и не задаёт вопрос повторно. Явно названный бюджет сохраняется только как сущность, извлечённая DeepSeek; смысловые фразы о зависимости от цены не превращаются кодом в бюджет и не запускают расчёт упаковок. Мобильный телефон принимается только как 11 цифр с исходным префиксом `8` или `+7`; 6-значный городской номер сохраняется отдельно в `landline` и CRM-комментарии, после него обязательно запрашивается мобильный. 10-значные и 11-значные номера с другим префиксом отклоняются и не затирают сохранённые контакты. Запросы цены за единицу распознаются также по «за каждую единицу», «за штуку», «за банку», «за бутылку», «отдельно» и опечатке «еденицу».
- Раздражение и вопрос о производителе обрабатываются до обычного AI-ответа: короткое извинение без повтора объёма (`pause_volume_prompt`) либо ответ производителем с каталога, если SKU один; иначе явное `needs_human`.
- `sales_state.py` — `deal_stage` отдельно от колонки Google Sheets.
- DeepSeek: модель по умолчанию `deepseek-v4-flash`, `max_tokens=800`, timeout 20 с. В JSON-запросах `thinking: {type: disabled}`. Пустой `content` — явная ошибка `ValueError`.
- `respond` отдаёт OpenAI-compatible `tools` с `search_catalog(query)`. Для сообщений с известным продуктом сервис передаёт локальный каталог в `respond_with_catalog`, поэтому DeepSeek сам понимает сомнение «зависит от цены» по истории/профилю и не повторяет известный объём. Для semantic-запросов цены за единицу intake передаёт `unit_price_request` и `target_product`; «за кг» / «за 1 кг» / «цена за кг» — unit-price, не volume=`1 кг` (`за 20 кг` остаётся заказом). Если intake сломан или target пуст, recovery выбирает последний однозначный primary товар из релевантной истории и строит `unit_price_catalog_result` локально. `unit_price_quote` считает ₽/кг из фасовки `мешок/короб/сетка N кг` без `piece_count` (морковь 410/10=41; рис 85, кукуруза 57.50, сок 148.33, морс 106.67 ₽/л, яблоки 20 кг = 1640 сохранены). При пустом детерминированном поиске DeepSeek получает явный `CATALOG_RESULT_EMPTY` вместе со списком категорий; честный no-match разрешён, цены/наличие и нерелевантные товары блокируются. `is_unsafe_claim` разрешает суммы из записи, derived unit prices и nearest-pack totals, не только regex по сырому тексту. Одноразовый `repair_response` / open-dialog; generic fallback не останавливает recovery. Если SKU известен, клиенту не уходит «не могу подтвердить» / FALLBACK — DeepSeek формулирует по quote, иначе grounded nearest/line recovery. Нерешённый вопрос без SKU — `pending_manager_question`/`needs_human` без фиктивного обещания возврата. Intake tools не получает.
- Перед применением semantic intake-фактов явно названный клиентом продукт проверяется одним детерминированным `search()`. При отсутствии позиций передаётся `CATALOG_RESULT_EMPTY`, `product`/`volume` не сохраняются, исходный запрос хранится в `catalog_no_match_query`, а follow-up повторяет no-match без объёма, альтернатив и `pending_manager_question`. Для intake exception новый товарный текст проверяется тем же каталоговым контекстом; generic обещания «уточню и вернусь» отбраковываются.
- При ошибке intake короткие подтверждения, служебные фразы, отказы и тип клиента (`магазин`/`кафе`/`ресторан`/`столовая`/`сеть` после вопроса про занятие) не считаются новым товарным запросом и не запускают no-match; явно названный товар по-прежнему проверяется детерминированным каталогом.
- Prompt bundle из `prompts/` подмешивается в `respond`, не в компактный intake.
- JSON-контракты и запрет выдумывать цены/наличие сохранены. `BUSINESS_CONTEXT` в обоих prompt.
- Контактная reply-клавиатура не создаётся; `request_contact` всегда `False`; каждый ответ шлёт `ReplyKeyboardRemove`.
- Имя не добавляется автоматически в каждый ответ; prompt разрешает обращение по имени только в естественных местах.
- `/start` не чистит карточку. Квалифицированный клиент получает returning greeting.
- CRM: `CRMRepository` + Google Sheets + in-memory fake. Объём по-прежнему в `комментарии` как `Объём: ...`.
- SOCKS только для Telegram (`TELEGRAM_PROXY_URL`). Sheets и DeepSeek напрямую.
- Setup-wizard в `tools/setup_wizard.py` без изменений этой фазы.
- Изолированный QA-стенд: `stokozavr_bot.qa_stand` / `python -m stokozavr_bot.qa_stand`. Реальный `ConversationService` и DeepSeek-клиент, CRM только `InMemoryCRMRepository`, telegram_id ≥ 2_000_000_000. Google Sheets и Telegram не импортируются. Транскрипты в `qa-dialogues/` (gitignore). Снапшоты `profile_before`/`profile_after` маскируют email/телефон/landline; репозиторий остаётся с живыми значениями. Живой smoke требует `DEEPSEEK_API_KEY` в env/.env; без ключа стенд готов, сеть не дергается.

## Решения и ограничения

- Обязательные столбцы листа `Клиенты` не расширялись. `deal_stage` в таблицу не пишется. `catalog_no_match_query` сохраняется в совместимом `комментарии` как `Товар не найден: ...` и не превращается в `интересующая продукция`. Интересы пишутся как `Интересы:` + urlsafe-base64 JSON (запятая и `|` в значениях не ломают roundtrip); legacy `Исходный интерес:` / `Текущий интерес:` по-прежнему читаются.
- При неизвестной информации, пустом каталоге, ошибке DeepSeek, опасном утверждении, вопросе про товар/цену без валидного AI-ответа: основной ответ → repair → максимум один open-dialog recovery (всего не более 4 AI-вызовов с повторным recovery на подтверждённом каталоге). Open-dialog получает полный релевантный history, profile/current_interest, память компании, причину отказа, каталог и структурированные расчёты; сам выбирает живой ответ/следующий шаг и максимум один вопрос, без возврата в анкету и без смысловых regex-ограничений. Если AI не дал безопасный ответ — сохраняются `pending_manager_question`/`needs_human` и используется grounded deterministic fallback без фиктивного обещания вернуться. Для кукурузы 12 x 340 г за 690 ₽ детерминированная цена за банку — 57.50 ₽/шт.
- В память компании добавлен подтверждённый адрес: г. Пенза, ул. Аустрина, 137, корп. 2. Составной заказ картофель 100 кг + макароны на бюджет обрабатывается одним каталоговым контекстом: расчёт картофеля 4 сетки по 25 кг = 3000 ₽, по макаронам DeepSeek задаёт один уточняющий вопрос.
- В память компании добавлены проверенные сведения: примерно 5 300 организаций, закупка крупных партий у заводов/фабрик/импортёров/крупных поставщиков, отдельные скидки до 90% только для отдельных партий, ориентир 200–300 актуальных позиций, а также спокойный стиль Ивана и правила ненавязчивой квалификации. Это company context, не product catalog; цифры нельзя выдумывать или превращать в цену/наличие.
- Причина исторических 780 ₽, 420 ₽ и «в наличии достаточно»: предыдущие markdown-заглушки содержали эти значения, а `listed_price_amounts()`/проверка наличия считали их разрешёнными. Заглушки очищены; теперь разрешаются только полные структурированные записи с датой. Добавленный каталог локальный и выдуманный для дружеского тестирования; реальные цены и наличие отсутствуют.
- Источник истины карточки — CRM; после рестарта этап по имени/телефону/продукту/объёму.
- Секреты не в репе. Прокси принадлежит только Telegram-session.
- Для прайса не добавлялся email-транспорт: почта — слот контакта и лид, не mailer. Голый «прайс есть?» / «прайс можно?» / «давайте прайс» / «каталог» сначала спрашивает почту для файла (`PRICE_LIST_EMAIL_OFFER`), не `EMAIL_QUESTION` и не вложение. Файл в чат только если клиент сам попросил чат/Telegram/сюда или отказался от почты после вопроса. Полный ассортимент — та же pending-машина, формулировка `FULL_ASSORTMENT_EMAIL_OFFER`. На email — «вам отправят», не «я отправил». Первый товарный ход — разовая развилка почта/чат (комментарий `Прайс-консультация предложена`). `parse_person_name` мапит однозначные уменьшительные на официальное имя (Ванёк→Иван, Диман→Дмитрий; Саша не трогаем). `answer_document` обёрнут в безопасную обработку ошибки; успех и `sent_at` только после реальной отправки. Postgres, пуш и деплой не делались.
- Самовывоз и звонок — разные каналы (`fulfillment_channel`: `pickup` | `call`). Слот визита/звонка — `requested_slot` на профиле (это не identity-слот). Явный запрос звонка (`звоните` / `звонок` / `перезвоните`) ставит `call` и не возвращает визит. «самовывоз не надо» / «не приеду» — отказ от самовывоза, не выбор pickup. Pickup остаётся только если клиент сам просил самовывоз; смена pickup→call без отказа — один вопрос канала. Обещание звонка только если in-memory `ManagerHandoff.create` вернул id; без адаптера слот подтверждается, `handoff_id` = None / undetermined, не catalog fallback и не «уточню и вернусь». Isolated QA-стенд по-прежнему `manager_handoff_observable=false`, адаптер к стенду не подключался. amoCRM HTTP и секреты не добавлялись. Адрес самовывоза: г. Пенза, ул. Аустрина, 137, корп. 2; окно «завтра можно» не подтверждено.

## Проверка

- На 2026-08-27 production deploy `427de1b` / `stable-production-427de1b`:
- GitHub `main` запушен. Site-packages: «прайс есть?» → `PRICE_LIST_EMAIL_OFFER`, Ванёк→Иван, Диман→Дмитрий.
- `Run polling for bot @Stokozavr_manager_bot` 2026-08-27T11:38:06Z, pid 312947. MAX и Xray остались `active`.
- CRM `679025492`: Клиенты DELETED 1 REMAINING 0, История DELETED 15 REMAINING 0.
- Живой ход: написать боту `/start` заново.

- На 2026-08-27 production deploy `038ccc8` / `stable-production-038ccc8`:
- GitHub `main` запушен. `wants_full_assortment("еда")` и `("продукты питания")` = True в site-packages. «молочные продукты» / «готовая еда» = False.
- `Run polling for bot @Stokozavr_manager_bot` 2026-08-27T10:53:31Z, pid 309970. MAX и Xray остались `active`.
- CRM `679025492`: Клиенты DELETED 1 REMAINING 0, История DELETED 5 REMAINING 0.
- Живой ход: написать боту `/start` заново.

- На 2026-08-27 production deploy `b685fa3` / `stable-production-b685fa3`:
- GitHub `main` запушен. Пакет `--reinstall` в site-packages: `line_total_quote("яблоки","20 кг")=1640 ₽`, конкуренты = «розничные сети» (30 stub), сравнение «доходит до 810 ₽» без Крупяной, прайс 30 SKU без розницы.
- `Run polling for bot @Stokozavr_manager_bot` 2026-08-27T10:34:35Z, pid 308701. MAX и Xray остались `active`.
- CRM `679025492`: Клиенты DELETED 1 REMAINING 0, История DELETED 15 REMAINING 0.
- Живой ход после рестарта: написать боту `/start` заново.

- На 2026-08-27 production deploy `805a96a` / `stable-production-805a96a`:
- GitHub `main` уже был на этом SHA. Пакет `--reinstall` в `/opt/stokozavr-telegram-bot/venv` site-packages, `line_total_quote("яблоки","20 кг")=1640 ₽`, `wants_full_assortment("все категории")=True`, прайс 30 SKU.
- `Run polling for bot @Stokozavr_manager_bot` 2026-08-27T09:45:00Z, pid 305855. MAX и Xray остались `active`.
- CRM `679025492`: Клиенты REMAINING 0, История REMAINING 0.
- Живой ход после рестарта не гоняли: написать боту `/start` заново.

- На 2026-08-27 (TDD: live QA R9 — volume-only «12 банок» на слоте телефона → 1720, не «Очень приятно»+phone; без push/deploy):
- `PYTHONPATH=src .venv/bin/pytest -q` — `526 passed`.
- `ruff check .` / `ruff format --check .` / `git diff --check` — чисто.
- Пуш и деплой **не** выполнялись. CRM не трогали.

- На 2026-08-27 (TDD: live QA R8 — snapshot original_interests на line/unit quote bind; current_interest на слоте телефона; «в наличии?» не stub; без push/deploy):
- `PYTHONPATH=src .venv/bin/pytest -q` — `525 passed`.
- `ruff check .` / `ruff format --check .` / `git diff --check` — чисто.
- Пуш и деплой **не** выполнялись. CRM не трогали.

- На 2026-08-27 (TDD: live QA R7 — original_interests без product, same-topic 12 банок, catalog не phone-only; без push/deploy):
- `PYTHONPATH=src .venv/bin/pytest -q` — `523 passed`.
- `ruff check .` / `ruff format --check .` / `git diff --check` — чисто.
- Пуш и деплой **не** выполнялись. CRM не трогали.

- На 2026-08-27 (TDD: корни live QA R5/R6 — volume/pack size, pickle vs VEG, manufacturer, irritation multi-turn, call slot, pause_volume в Sheets; без push/deploy):
- `PYTHONPATH=src .venv/bin/pytest -q` — `520 passed`.
- `ruff check .` / `ruff format --check .` / `git diff --check` — чисто.
- Пуш и деплой **не** выполнялись. CRM не трогали.

- На 2026-08-26 (TDD: корни live QA round-4 — магазин≠SKU, quote 200л/12 банок, слот не переспрашивать, volume «3 мешка»; без push/deploy):
- `PYTHONPATH=src .venv/bin/pytest -q` — `509 passed`.
- `ruff check .` / `ruff format --check .` / `git diff --check` — чисто.
- Коммит локальный: `Fix live QA roots after round 4`. Пуш и деплой **не** выполнялись. CRM не трогали.

Ранее на 2026-08-26 (TDD: сам один раз назвать linked-конкурента дороже на primary-позиции; прайс в Telegram файлом):
- Коммиты: `Send price list in Telegram by default` + `Mention linked competitor once on primary quote`.

Ранее на 2026-08-26 (TDD: в Telegram прайс по «можно/есть» сразу файлом, почта только по явной просьбе):
- `PYTHONPATH=src .venv/bin/pytest -q` — `475 passed`.

- На 2026-08-26 (TDD: убрать автооффер прайса / 200 л морса ближайшими упаковками, без push/deploy):
- `PYTHONPATH=src .venv/bin/pytest -q` — `461 passed`.
- `ruff check .` / `ruff format --check .` / `git diff --check` — чисто.
- Коммит локальный: `Stop price-list spam and quote nearest packs`. Пуш и деплой **не** выполнялись.

- На 2026-08-26 (TDD: снять намордник FALLBACK / легализовать 41 ₽/кг / «Вся» не товар, без push/deploy):
- `PYTHONPATH=src .venv/bin/pytest -q` — `446 passed`.
- `ruff check .` / `ruff format --check .` / `git diff --check` — чисто.
- Коммит локальный: `Let DeepSeek speak; ground unit prices`. Пуш и деплой **не** выполнялись.

- На 2026-08-26 (production deploy `1b5169e` / `stable-production-1b5169e`):
- GitHub `main` запушен, пакет переустановлен, `Run polling` для `@Stokozavr_manager_bot`.
- CRM `679025492`: Клиенты REMAINING 0, История REMAINING 0.
- MAX и Xray остались `active`. Пуш и деплой выполнены по явному слову.

Ранее на 2026-08-26 (main `0d1e773` + живой rerun P1/P3/P6 на isolated Beget, без push/deploy):
- `.venv/bin/pytest -q` — `430 passed`.
- `ruff check .` / `ruff format --check .` / `git diff --check` — чисто.
- Isolated smoke: `SMOKE=ok model=deepseek-v4-flash` на `/tmp/stokozavr-qa-round2`.
- Live rerun `qa-dialogues/round-2b/`: P1 два короба = 1640; P3 «консервы какие?» → горошек/кукуруза; P6 `channel=call`, после мобильного CLOSE_ASK_TIME, не PRODUCT. Production `@Stokozavr_manager_bot` не менялся.

Ранее на 2026-08-26 (TDD slice G: два короба / консервы alias / канал звонка / PRODUCT после телефона, без push/deploy):
- `PYTHONPATH=src /home/hermes/projects/stokozavr-telegram-bot/.venv/bin/pytest -q` — `430 passed`.
- `ruff check .` — `All checks passed!`
- `ruff format --check .` — formatted; `git diff --check` без ошибок.
- Коммит локальный: `Fix remaining live QA roots after round 2`. Пуш и деплой **не** выполнялись.

Ранее на 2026-08-26 (TDD slice F: pickup≠call / time slot / ManagerHandoff port, без push/deploy):
- `PYTHONPATH=src /home/hermes/projects/stokozavr-telegram-bot/.venv/bin/pytest -q` — `402 passed`.
- `ruff check .` — `All checks passed!`
- `ruff format --check .` — `64 files already formatted`.
- `git diff --check` — без ошибок.
- Коммит локальный: `Separate pickup from callback and add handoff port`. Пуш и деплой **не** выполнялись.

Ранее на 2026-08-26 (TDD: visible competitor mentions, без push/deploy):
- `PYTHONPATH=src /home/hermes/projects/stokozavr-telegram-bot/.venv/bin/pytest -q` — `404 passed`.
- `ruff check .` — `All checks passed!`
- `ruff format --check .` — `63 files already formatted`.
- `git diff --check` — без ошибок.
- Коммит локальный: `Count visible competitor mentions without burning replies`. Пуш и деплой **не** выполнялись.

Ранее на 2026-08-26 (TDD: pending-машина прайса — email не сбрасывает запрос, «в чат» отдаёт файл, отказ отменяет, канал ≠ прайс; без push/deploy):
- `PYTHONPATH=src /home/hermes/projects/stokozavr-telegram-bot/.venv/bin/pytest -q` — `400 passed`.
- `ruff check .` — `All checks passed!`
- `ruff format --check .` — `63 files already formatted`.
- `git diff --check` — без ошибок.
- Коммит локальный: `Keep pending price list until send or cancel`. Пуш и деплой **не** выполнялись.

Ранее на 2026-08-26 (TDD post-review: catalog tokens / cucumber modifiers / sticky anaphora / Sheets interests / QA PII / packaging volume, без push/deploy):
- `PYTHONPATH=src /home/hermes/projects/stokozavr-telegram-bot/.venv/bin/pytest -q` — `395 passed`.
- `ruff check .` — `All checks passed!`
- `ruff format --check .` — `63 files already formatted`.
- `git diff --check` — без ошибок.
- Коммит локальный: `Fix reviewed catalog and QA edge cases`. Пуш и деплой **не** выполнялись.

Ранее на 2026-08-26 (TDD slice C: structured quotes в живом диалоге, без push/deploy):

- `PYTHONPATH=src /home/hermes/projects/stokozavr-telegram-bot/.venv/bin/pytest -q` — `372 passed`.
- `ruff check .` — `All checks passed!`
- `ruff format --check .` — `62 files already formatted`.
- `git diff --check` — без ошибок.
- Коммит локальный: `Ground catalog answers in structured quotes`. Пуш и деплой **не** выполнялись.

Ранее на 2026-08-26 (TDD slice A: state/topic/context QA round-1, без push/deploy):

- `.venv/bin/pytest -q` — `328 passed`.
- `.venv/bin/ruff check .` — `All checks passed!`.
- `.venv/bin/ruff format --check .` — `59 files already formatted`.
- `git diff --check` — без ошибок.

Ранее на 2026-08-26 (изолированный QA-стенд для Grok-тестеров, без product behaviour):

- `.venv/bin/pytest -q` — `300 passed`.
- `.venv/bin/ruff check .` — `All checks passed!`.
- `.venv/bin/ruff format --check .` — `59 files already formatted`.
- `git diff --check` — без ошибок.
- Живой DeepSeek smoke: локально **нет** `DEEPSEEK_API_KEY` / `.env`. CLI вернул `SMOKE=blocked`. Сеть не вызывалась.
- Пуш и деплой **не** выполнялись.

Ранее на 2026-08-26 (локальный тестовый каталог, opt-in конкуренты с лимитом 2, generated primary price list, Telegram attachment delivery/state, strict invalid-phone, landline/mobile validation, company-memory rules and generic fallback recovery):

- `.venv/bin/pytest -q` — `285 passed`.
- `.venv/bin/ruff check .` — `All checks passed!`.
- `.venv/bin/ruff format --check .` — `56 files already formatted`.
- `git diff --check` — без ошибок.
- Пуш и деплой **не** выполнялись.

Ранее на 2026-08-25:

- `uv run --with pytest --with pytest-asyncio --with httpx pytest -q` — `218 passed`.
- `105 passed` после prompts / память клиента / ассортимент / отказ телефона (локально, на Beget ещё не выкладывалось).
- Regression пустого `комментарии`, SOCKS5, реальный preflight Telegram/Sheets/DeepSeek, systemd на Beget `45.153.188.226` — см. историю ниже.

Развёрнуто ранее на Beget `45.153.188.226` в `/opt/stokozavr-telegram-bot`. Эта фаза **не** выкладывалась.

## Дальше

Slice I (обновлено): голый «прайс есть/можно» — почта сначала, не файл; файл только по просьбе «в чат». Полный ассортимент — та же pending-машина. Разовая товарная развилка почта/чат. Уменьшительные → официальное имя. Локально, **не** на Beget, **не** закоммичено. После явного пуш/деплоя проверить: «прайс можно?» = вопрос почты, не файл и не EMAIL_QUESTION; «Прайс в чат» = файл; «какие овощи?» = список + одна развилка, второй товарный ход без повтора; «Ванёк» = Иван. Осталось: живой amoCRM, name-frequency, handoff adapter в стенде.

Локально есть выдуманный тестовый каталог и tool-calling. Реальные коммерческие данные не добавлялись. Production на Beget: `1b5169e`.

- Google Sheets API включён.
- Таблица `CRM Стокозавр`: `1H-Iwm_CjjpSdDPk-UQJE6uu7PzFZKY-XsVWA0X_buxc`.
- Таблица расшарена редактору `stokozavr-bot@stokozavr-bot.iam.gserviceaccount.com`.
- Рабочий Google JSON установлен в `/opt/stokozavr-telegram-bot/secrets/google-service-account.json` с закрытыми правами; `.env` также закрыт.

1. После явного «пушь / деплой» выложить и проверить в `@Stokozavr_manager_bot`: `/start` → «какие фрукты есть?» без телефона должен ответить как менеджер по каталогу, не анкетой.
2. Проверить строки в обоих листах Google Sheets и журнал systemd.
