"""
Оценка релевантности вакансии резюме с помощью Claude.
"""

import re
import anthropic

_client = anthropic.Anthropic()


def _extract_text(vacancy: dict) -> str:
    """Формирует читаемое описание вакансии из объекта API."""
    parts = [
        f"Название: {vacancy.get('name', '')}",
        f"Компания: {vacancy.get('employer', {}).get('name', '')}",
    ]
    salary = vacancy.get("salary")
    if salary:
        s_from = salary.get("from") or ""
        s_to = salary.get("to") or ""
        currency = salary.get("currency", "")
        parts.append(f"Зарплата: {s_from}–{s_to} {currency}")

    experience = vacancy.get("experience", {}).get("name", "")
    if experience:
        parts.append(f"Опыт: {experience}")

    schedule = vacancy.get("schedule", {}).get("name", "")
    if schedule:
        parts.append(f"График: {schedule}")

    # Полное описание есть только в детальной карточке вакансии
    description = vacancy.get("description", "")
    if description:
        # Убираем HTML-теги
        clean = re.sub(r"<[^>]+>", " ", description)
        clean = re.sub(r"\s+", " ", clean).strip()
        parts.append(f"\nОписание:\n{clean[:2000]}")

    key_skills = vacancy.get("key_skills", [])
    if key_skills:
        skills_str = ", ".join(s["name"] for s in key_skills)
        parts.append(f"Ключевые навыки: {skills_str}")

    return "\n".join(parts)


def score_vacancy(resume: str, vacancy: dict) -> tuple[int, str]:
    """
    Оценивает релевантность вакансии от 1 до 10.
    Возвращает (score, reason).
    Синхронная функция — используется в asyncio через run_in_executor если нужно.
    """
    vacancy_text = _extract_text(vacancy)

    response = _client.messages.create(
        model="claude-haiku-4-5",  # Haiku дешевле для массовой фильтрации
        max_tokens=256,
        system=(
            "Ты — рекрутер. Оцени насколько вакансия подходит кандидату по шкале 1–10. "
            "Зарплатные ожидания кандидата — от 350 000 руб. Если зарплата указана и явно ниже — снижай оценку. "
            "Несоответствие по опыту (требуется больше лет) не является критичным — кандидат готов откликаться. "
            "Отвечай строго в формате:\nОЦЕНКА: <число>\nПРИЧИНА: <одно предложение>"
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"РЕЗЮМЕ КАНДИДАТА:\n{resume}\n\n"
                    f"ВАКАНСИЯ:\n{vacancy_text}\n\n"
                    "Оцени соответствие."
                ),
            }
        ],
    )

    text = response.content[0].text.strip()

    # Парсим ответ
    score_match = re.search(r"ОЦЕНКА:\s*(\d+)", text)
    reason_match = re.search(r"ПРИЧИНА:\s*(.+)", text)

    score = int(score_match.group(1)) if score_match else 5
    score = max(1, min(10, score))  # зажимаем в [1, 10]
    reason = reason_match.group(1).strip() if reason_match else text

    return score, reason
