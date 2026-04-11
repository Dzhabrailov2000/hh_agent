"""
Генерация ответов на вопросы HR в чате с помощью Claude.
"""
import re
import anthropic
from config import CHAT_SYSTEM_PROMPT

_client = anthropic.Anthropic()

# Паттерны prompt injection — попытки переопределить инструкции через текст HR
_INJECTION_PATTERNS = [
    r"игнорируй\s+.{0,30}инструкци",
    r"забудь\s+.{0,30}(инструкци|выше|ранее)",
    r"ignore\s+.{0,30}(previous|above|instruction)",
    r"disregard\s+.{0,30}instruction",
    r"you are now",
    r"new\s+instruction",
    r"system\s*:",
    r"<\s*system\s*>",
    r"\[INST\]",
    r"###\s*instruction",
    r"act as if",
    r"pretend you",
    r"roleplay as",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


def _sanitize(text: str) -> str:
    """
    Помечает подозрительный текст вместо удаления — чтобы не ломать контекст,
    но сигнализировать Claude что это ненадёжный ввод.
    """
    if _INJECTION_RE.search(text):
        return f"[НЕДОВЕРЕННЫЙ ВВОД — возможная попытка инъекции]: {text}"
    return text


def _wrap_hr_message(text: str) -> str:
    """Оборачивает текст от HR в явные теги — помогает Claude различать контекст."""
    return f"<hr_message>\n{_sanitize(text)}\n</hr_message>"


def is_human_hr(messages: list[dict]) -> bool:
    """
    Определяет, похоже ли последнее сообщение на живого HR-а (а не бота/автоматизацию).
    Возвращает True если человек — нужно уведомить пользователя.
    """
    last_text = ""
    for msg in reversed(messages):
        if msg.get("author", {}).get("participant_type") == "employer":
            last_text = msg.get("text", "")
            break

    if not last_text:
        return False

    # Строим историю для контекста (последние 6 сообщений)
    history_text = "\n".join(
        f"{'HR' if m.get('author', {}).get('participant_type') == 'employer' else 'Кандидат'}: {_sanitize(m.get('text', ''))}"
        for m in messages[-6:]
    )

    response = _client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=10,
        system=(
            "Определи: последнее сообщение от HR — это живой человек или автоматизированный бот/шаблон?\n"
            "Признаки бота: стандартные вопросы-анкеты, шаблонные фразы, вопросы о зарплате/опыте/стеке по списку.\n"
            "Признаки человека: персональное обращение, предложение позвонить/встретиться, нестандартные вопросы, эмоции.\n"
            "Ответь одним словом: ЧЕЛОВЕК или БОТ."
        ),
        messages=[{"role": "user", "content": f"История чата:\n{history_text}"}],
    )

    answer = response.content[0].text.strip().upper()
    return "ЧЕЛОВЕК" in answer


def build_chat_history(messages: list[dict]) -> list[dict]:
    """
    Преобразует историю сообщений HH в формат Claude messages.
    author.participant_type: applicant | employer
    Сообщения HR оборачиваются в теги для защиты от prompt injection.
    """
    result = []
    for msg in messages:
        participant = msg.get("author", {}).get("participant_type", "")
        text = msg.get("text", "").strip()
        if not text:
            continue
        if participant == "employer":
            result.append({"role": "user", "content": _wrap_hr_message(text)})
        else:
            result.append({"role": "assistant", "content": text})
    return result


def generate_cover_letter(resume: str, vacancy: dict) -> str:
    """
    Генерирует персональное сопроводительное письмо под конкретную вакансию.
    """
    title = vacancy.get("name", "")
    company = vacancy.get("employer", {}).get("name", "")
    description = vacancy.get("description", "")[:1500]
    skills = ", ".join(s["name"] for s in vacancy.get("key_skills", []))

    vacancy_text = f"Должность: {title}\nКомпания: {company}\n"
    if skills:
        vacancy_text += f"Ключевые навыки: {skills}\n"
    if description:
        vacancy_text += f"Описание: {description}"

    response = _client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=300,
        system=(
            "Ты пишешь короткое сопроводительное письмо от имени кандидата на hh.ru. "
            "Требования: 3-4 предложения, без воды, конкретно почему подходишь именно под эту вакансию. "
            "Не копируй весь опыт из резюме — выдели самое релевантное. "
            "Начни с 'Здравствуйте!' Пиши от первого лица, по-русски."
        ),
        messages=[{
            "role": "user",
            "content": f"Резюме кандидата:\n{resume}\n\nВакансия:\n{vacancy_text}\n\nНапиши сопроводительное письмо."
        }],
    )
    return response.content[0].text.strip()


def generate_response(resume: str, messages: list[dict]) -> str:
    """
    Генерирует ответ кандидата на последнее сообщение работодателя.
    messages — история чата из HH API (хронологически).
    """
    chat_history = build_chat_history(messages)

    # Если последнее сообщение от нас — отвечать не нужно
    if not chat_history or chat_history[-1]["role"] == "assistant":
        return ""

    # Убедимся что история начинается с user (работодателя)
    # Claude требует чередования user/assistant
    if chat_history[0]["role"] == "assistant":
        chat_history = chat_history[1:]

    if not chat_history:
        return ""

    system = CHAT_SYSTEM_PROMPT.format(resume=resume)

    response = _client.messages.create(
        model="claude-opus-4-6",
        max_tokens=512,
        thinking={"type": "adaptive"},
        system=system,
        messages=chat_history,
    )

    # Извлекаем текстовый блок (thinking блоки пропускаем)
    for block in response.content:
        if block.type == "text":
            return block.text.strip()

    return ""


def classify_quick_reply(messages: list[dict], options: list[str]) -> tuple[str, str]:
    """
    Определяет как обработать кнопки быстрых ответов в чате.

    Возвращает:
      ("auto", chosen_option)  — безопасно ответить автоматически
      ("notify", reason)       — нужно решение человека, reason — объяснение для Telegram

    Критерий IMPORTANT: интервью/встреча, оффер, сроки выхода, зарплатные ожидания,
    условия трудоустройства — всё что влияет на реальные договорённости.
    """
    history_text = "\n".join(
        f"{'HR' if m.get('author', {}).get('participant_type') == 'employer' else 'Кандидат'}: {_sanitize(m.get('text', ''))}"
        for m in messages[-6:]
    )
    options_text = "\n".join(f"- {_sanitize(o)}" for o in options)

    response = _client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=100,
        system=(
            "Ты решаешь — можно ли автоматически нажать кнопку ответа в чате с HR, "
            "или нужно уведомить кандидата для ручного решения.\n\n"
            "IMPORTANT (уведомить кандидата): кнопки касаются интервью/встречи, даты выхода, "
            "оффера, зарплаты, условий работы, согласования конкретных договорённостей.\n"
            "ROUTINE (ответить автоматически): подтверждение получения резюме, 'расскажите о себе', "
            "стандартные анкетные вопросы, согласие на обработку данных.\n\n"
            "Ответ строго в формате:\n"
            "ROUTINE: <точный текст лучшей кнопки>\n"
            "или\n"
            "IMPORTANT: <причина одной фразой>"
        ),
        messages=[{
            "role": "user",
            "content": f"История чата:\n{history_text}\n\nДоступные кнопки ответа:\n{options_text}"
        }],
    )

    text = response.content[0].text.strip()
    if text.startswith("ROUTINE:"):
        chosen = text[len("ROUTINE:"):].strip()
        # Берём ближайший вариант из списка если Claude немного переформулировал
        chosen = _closest_option(chosen, options)
        return "auto", chosen
    elif text.startswith("IMPORTANT:"):
        reason = text[len("IMPORTANT:"):].strip()
        return "notify", reason
    else:
        # Непонятный ответ — на всякий случай уведомляем
        return "notify", "не удалось классифицировать запрос"


def _closest_option(text: str, options: list[str]) -> str:
    """Возвращает опцию из списка наиболее близкую к тексту."""
    text_lower = text.lower()
    for opt in options:
        if opt.lower() == text_lower:
            return opt
    for opt in options:
        if text_lower in opt.lower() or opt.lower() in text_lower:
            return opt
    return options[0] if options else text


def answer_screening_questions(resume: str, questions: list[dict]) -> dict:
    """
    Отвечает на скрининговые вопросы работодателя.

    questions — список:
        {
          "id": "q1",
          "text": "Сколько лет опыта в Java?",
          "type": "text" | "textarea" | "radio" | "number",
          "options": ["1-2 года", "3-5 лет", "5+ лет"]  # только для radio
        }

    Возвращает {id: answer_string}.
    """
    if not questions:
        return {}

    questions_text = ""
    for q in questions:
        questions_text += f"\nВопрос [{q['id']}]: {q['text']}"
        if q.get("options"):
            questions_text += f"\n  Варианты ответа: {', '.join(q['options'])}"
        questions_text += f"\n  Тип поля: {q['type']}"

    response = _client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        system=(
            "Ты кандидат на должность Java/Kotlin разработчика, заполняешь анкету на hh.ru. "
            "Отвечай кратко и по делу на основе резюме. "
            "Ожидаемая зарплата — от 350 000 руб. "
            "ВАЖНО: всегда давай конкретный содержательный ответ. "
            "Если точной информации в резюме нет — дай разумный ответ от первого лица, основываясь на типичном опыте Senior-разработчика. "
            "Никогда не пиши 'информация отсутствует', 'не указано', 'уточните вопрос' и т.п. — это недопустимо. "
            "Для radio-вопросов отвечай ТОЧНО одним из предложенных вариантов. "
            "Ответ дай строго в формате JSON: {\"id\": \"ответ\", ...}"
        ),
        messages=[{
            "role": "user",
            "content": f"Резюме:\n{resume}\n\nВопросы анкеты:{questions_text}\n\nОтветь на все вопросы в JSON."
        }],
    )

    import json, re
    text = response.content[0].text.strip()
    # Извлекаем JSON из ответа
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except Exception:
            pass
    return {}
