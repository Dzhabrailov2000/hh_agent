# HH.ru Job Agent

Автоматический агент для поиска работы на hh.ru. Откликается на вакансии, отвечает на сообщения рекрутеров через Claude AI и уведомляет вас в Telegram когда пишет живой HR.

## Что умеет

- **Поиск вакансий** — берёт рекомендации hh.ru под ваше резюме с фильтрами (удалённо, зарплата)
- **Оценка релевантности** — Claude оценивает каждую вакансию по шкале 1–10 и откликается только на подходящие
- **Автоотклик** — отправляет сопроводительное письмо, обходит форму письма если она есть
- **Мониторинг чатов** — проверяет все вкладки переговоров (Активные, Ожидание и др.)
- **Авто-ответ на ботов** — если рекрутер — бот/шаблон, Claude отвечает от вашего имени
- **Уведомление о живом HR** — если пишет человек, вы получаете уведомление в Telegram
- **Telegram-бот** — команды `/stats` и `/journal` для просмотра статистики

## Структура проекта

```
hh_agent/
├── main.py              # точка входа, главный цикл
├── config.py            # все настройки: резюме, фильтры, промпты
├── requirements.txt
├── .env                 # секреты (не попадает в git)
├── .env.example         # шаблон для заполнения
│
├── browser/
│   └── client.py        # Playwright-клиент для hh.ru
│
├── ai/
│   ├── matcher.py       # оценка релевантности вакансии (Claude Haiku)
│   └── responder.py     # генерация ответов в чате (Claude Opus)
│
├── telegram/
│   ├── notifier.py      # отправка уведомлений
│   └── bot.py           # polling-бот с командами /stats /journal
│
└── core/
    └── state.py         # сохранение состояния в state.json
```

## Установка

### 1. Клонировать репозиторий

```bash
git clone https://github.com/YOUR_USERNAME/hh_agent.git
cd hh_agent
```

### 2. Установить зависимости

```bash
pip install -r requirements.txt
playwright install chromium
```

### 3. Настроить окружение

```bash
cp .env.example .env
```

Заполнить `.env`:

```env
HH_LOGIN=your@email.com
HH_PASSWORD=yourpassword
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=123456:ABC...   # получить у @BotFather
TELEGRAM_CHAT_ID=123456789         # ваш chat_id
```

### 4. Настроить агента под себя

Открыть `config.py` и заполнить:

- `RESUME` — ваше резюме текстом (используется для оценки и ответов в чате)
- `COVER_LETTER` — сопроводительное письмо по умолчанию
- `SALARY_FROM` — минимальная зарплата
- `SCHEDULE` — `"remote"` для удалённой работы, `None` — без фильтра
- `MIN_RELEVANCE_SCORE` — порог оценки 1–10 (рекомендуется 3–5)
- `TITLE_BLACKLIST` — слова в названии вакансии для пропуска
- `COMPANY_BLACKLIST` — компании которые нужно игнорировать

### 5. Запустить

```bash
# Первый запуск — браузер откроется для входа в hh.ru
python main.py

# Один проход без цикла (для теста)
python main.py --once

# Без откликов и ответов (только анализ)
python main.py --dry-run --once
```

После первого входа сессия сохраняется в `hh_session.json` — повторный логин не нужен.

## Как получить Telegram chat_id

1. Создать бота через [@BotFather](https://t.me/BotFather), получить токен
2. Написать боту любое сообщение
3. Открыть `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Найти `"chat": {"id": YOUR_CHAT_ID}`

## Требования

- Python 3.9+
- Аккаунт на hh.ru
- API-ключ Anthropic (Claude)
- Telegram-бот (опционально)
