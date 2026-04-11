# HH.ru Job Agent

Автоматический агент для поиска работы на hh.ru. Откликается на вакансии из раздела «Рекомендованные», отвечает на сообщения рекрутеров через Claude AI и уведомляет в Telegram когда нужно ваше решение.

## Что умеет

- **Отклики по рекомендациям** — берёт вакансии из раздела hh.ru «Рекомендованные» под ваше резюме (фильтр: удалённо, зарплата от N, опыт 3–6 и 6+ лет)
- **Фильтр по стеку** — откликается только на Java/Kotlin-вакансии, пропускает QA, frontend, нерелевантные языки
- **Чёрный список** — компании и ключевые слова в названии вакансии
- **Автоотклик** — отправляет сопроводительное письмо, заполняет скрининговые вопросы через Claude
- **Мониторинг всех чатов** — проходит по всем переговорам с пагинацией (194+ чата)
- **Авто-ответ ботам** — если HR-бот пишет шаблон, Claude отвечает от вашего имени
- **Кнопки быстрых ответов** — рутинные («Здравствуйте!») нажимает сам, важные (зарплата, условия) — пингует в Telegram
- **Уведомление о живом HR** — если пишет человек, вы получаете уведомление в Telegram
- **Итоговый отчёт** — после каждого прохода по чатам отправляет сводку в Telegram
- **Excel-отчёт** — `vacancies.xlsx` с листами: Отклики, По дням, Ошибки, Быстрые ответы
- **Telegram-бот** — команды `/stats` и `/journal` для просмотра статистики

## Структура проекта

```
hh_agent/
├── main.py              # точка входа, главный цикл, apply + chat шаги
├── config.py            # все настройки: фильтры, промпты, блэклисты
├── resume.txt           # ваше резюме (в .gitignore)
├── resume.txt.example   # шаблон
├── requirements.txt
├── .env                 # секреты (в .gitignore)
├── .env.example         # шаблон
│
├── browser/
│   └── client.py        # Playwright-клиент: браузер, отклики, чаты, chatik iframe
│
├── ai/
│   ├── matcher.py       # score_vacancy() — оценка релевантности (не используется при MIN_SCORE=0)
│   └── responder.py     # generate_response(), is_human_hr(), classify_quick_reply()
│
├── core/
│   ├── state.py         # load_state() / save_state() → state.json
│   ├── excel.py         # экспорт в vacancies.xlsx
│   └── exceptions.py    # StopOnFail — остановка при нестандартной форме
│
└── telegram/
    ├── notifier.py      # send_hr_notification(), send_chat_summary()
    └── bot.py           # polling-бот: /stats /journal
```

## Режимы запуска

```bash
# Только отклики (интервал 30 мин)
python3 -u main.py --mode apply

# Только чаты (интервал 5 мин)
python3 -u main.py --mode chat

# Оба режима параллельно
python3 -u main.py --mode both

# Один проход и выйти
python3 -u main.py --mode chat --once

# Показать браузер
python3 -u main.py --mode apply --visible

# Только анализ без откликов
python3 -u main.py --dry-run --once

# Стоп при нестандартной форме (для отладки)
python3 -u main.py --mode apply --stop-on-fail --visible
```

> `-u` обязателен для вывода в реальном времени (без буферизации stdout)

## Установка

### 1. Зависимости

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Настроить окружение

```bash
cp .env.example .env
```

Заполнить `.env`:

```env
HH_LOGIN=your@email.com
HH_PASSWORD=yourpassword
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=123456:ABC...   # получить у @BotFather
TELEGRAM_CHAT_ID=123456789         # ваш chat_id (см. ниже)
```

### 3. Добавить резюме

```bash
cp resume.txt.example resume.txt
```

Заполнить `resume.txt` своими данными. Файл в `.gitignore`.

### 4. Настроить фильтры

Открыть `config.py`:

| Параметр | Описание |
|---|---|
| `SALARY_FROM` | Минимальная зарплата |
| `SCHEDULE` | `"remote"` — только удалённо |
| `EXPERIENCE` | Опыт работы (список) |
| `TITLE_BLACKLIST` | Слова в названии → пропустить |
| `COMPANY_BLACKLIST` | Компании → пропустить |
| `COVER_LETTER` | Сопроводительное письмо |
| `MIN_RELEVANCE_SCORE` | Порог оценки 1–10 (`0` = все вакансии) |
| `USE_RECOMMENDATIONS` | `True` — брать из «Рекомендованных» |

### 5. Первый запуск

```bash
python3 -u main.py --mode apply --visible --once
```

Браузер откроется, войдите в hh.ru вручную. Сессия сохранится в профиле браузера — повторный логин не нужен.

## Как работает мониторинг чатов

Агент проходит по **всем переговорам** с пагинацией, для каждого:

1. Открывает chatik-iframe
2. Читает последнее сообщение
3. Если от HR (и не обрабатывалось ранее):
   - Есть кнопки → рутинные нажимает сам, важные → Telegram
   - Нет кнопок + бот → Claude генерирует ответ и отправляет
   - Нет кнопок + живой HR → Telegram-уведомление
4. Сохраняет MD5-хеш сообщения в `state.json` (не повторяет обработку)

После прохода приходит итоговое сообщение в Telegram со списком авто-ответов и уведомлений.

## Профили браузера

Для режимов `apply` и `chat` используются отдельные профили Chromium:

```
hh_browser_profile/        # дефолтный
hh_browser_profile_apply/  # для откликов
hh_browser_profile_chat/   # для чатов
```

При первом запуске нового режима профиль копируется из основного (с сессией).

## Состояние (state.json)

```json
{
  "applied": ["vid1", "vid2"],
  "journal": {
    "vid": {
      "title": "...", "company": "...", "url": "...",
      "applied_at": "2026-04-11 14:30:45",
      "score": 0,
      "status": "applied | hr_contacted | human_hr",
      "hr_messages": [{"id": "...", "text": "...", "at": "..."}]
    }
  },
  "failed_apply": {"vid": {"attempts": 1, "screenshots": [...]}},
  "chat_last_seen": {"neg_id": "md5hash"},
  "quick_reply_log": [{"at": "...", "vacancy": "...", "action": "..."}]
}
```

## Как получить Telegram chat_id

1. Создать бота через [@BotFather](https://t.me/BotFather), получить токен
2. Написать боту любое сообщение
3. Открыть `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Найти `"chat": {"id": YOUR_CHAT_ID}`

## Требования

- Python 3.9+
- Аккаунт на hh.ru
- API-ключ Anthropic (Claude)
- Telegram-бот (опционально, но рекомендуется)
