"""
Telegram-уведомления для hh_agent.

Требует в .env:
    TELEGRAM_BOT_TOKEN=...
    TELEGRAM_CHAT_ID=...

Как получить TELEGRAM_CHAT_ID:
    1. Напишите боту любое сообщение
    2. Откройте https://api.telegram.org/bot<TOKEN>/getUpdates
    3. Найдите "chat": {"id": ...}
"""

from __future__ import annotations

import os
import httpx

_BASE = "https://api.telegram.org/bot{token}/{method}"


def _token() -> str:
    t = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not t:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")
    return t


def _chat_id() -> str:
    c = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not c:
        raise RuntimeError("TELEGRAM_CHAT_ID не задан в .env")
    return c


async def send_hr_notification(
    vacancy_title: str,
    employer_name: str,
    message_text: str,
    negotiation_url: str,
) -> None:
    """Уведомление когда HR написал."""
    preview = message_text[:300] + ("..." if len(message_text) > 300 else "")
    text = (
        f"💬 *HR написал*\n\n"
        f"*{_esc(employer_name)}* — {_esc(vacancy_title)}\n\n"
        f"_{_esc(preview)}_"
    )
    await _send(text, url_button=("Открыть чат", negotiation_url))



async def send_chat_summary(
    auto_replied: list[dict],
    notified: list[dict],
) -> None:
    """Итоговый отчёт после прохода по чатам."""
    if not auto_replied and not notified:
        return
    lines = ["📊 *Итог прохода чатов*\n"]
    if auto_replied:
        lines.append(f"*Авто\\-ответы* \\({len(auto_replied)}\\):")
        for item in auto_replied:
            lines.append(f"  • {_esc(item['company'])} — {_esc(item['vacancy'])}")
            response = item.get("response", "")
            if response:
                lines.append(f"    _{_esc(response)}_")
    if notified:
        if auto_replied:
            lines.append("")
        lines.append(f"*Уведомления в TG* \\({len(notified)}\\):")
        for item in notified:
            lines.append(f"  • {_esc(item['company'])} — {_esc(item['vacancy'])}: _{_esc(item['reason'])}_")
    await _send("\n".join(lines))


async def send_applied_notification(
    vacancy_title: str,
    employer_name: str,
    vacancy_url: str,
    screening: list[dict] | None = None,
) -> None:
    """Уведомление об успешном отклике, опционально со скрининговыми ответами."""
    lines = [
        f"✅ *Отклик отправлен*\n",
        f"*{_esc(employer_name)}* — {_esc(vacancy_title)}",
    ]
    if screening:
        lines.append("")
        for item in screening:
            q = _esc(item["question"][:120])
            a = _esc(item["answer"][:300])
            lines.append(f"❓ _{q}_\n✏️ {a}")
    await _send("\n".join(lines), url_button=("Открыть вакансию", vacancy_url))


async def _send(text: str, url_button: tuple[str, str] | None = None) -> None:
    payload: dict = {
        "chat_id": _chat_id(),
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }
    if url_button:
        label, url = url_button
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": label, "url": url}]]
        }

    url = _BASE.format(token=_token(), method="sendMessage")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload)
        if not resp.is_success:
            print(f"  ⚠ Telegram ошибка: {resp.text}")


def _esc(text: str) -> str:
    """Экранирование спецсимволов MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text
