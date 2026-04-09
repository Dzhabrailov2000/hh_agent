"""
Telegram-бот для просмотра статистики hh_agent.

Команды:
    /stats   — общая статистика по откликам
    /journal — последние 10 откликов со статусами
"""

from __future__ import annotations

import asyncio
import os

import httpx

from core.state import load_state
_BASE = "https://api.telegram.org/bot{token}/{method}"

STATUS_EMOJI = {
    "applied":      "📤",
    "hr_contacted": "💬",
    "interview":    "📅",
    "rejected":     "❌",
    "offer":        "🎉",
}

STATUS_LABEL = {
    "applied":      "Отклик отправлен",
    "hr_contacted": "HR написал",
    "interview":    "Интервью",
    "rejected":     "Отказ",
    "offer":        "Оффер",
}


def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def _chat_id() -> str:
    return os.environ.get("TELEGRAM_CHAT_ID", "")



async def _send(text: str) -> None:
    url = _BASE.format(token=_token(), method="sendMessage")
    payload = {
        "chat_id": _chat_id(),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(url, json=payload)


async def _get_updates(offset: int) -> list[dict]:
    url = _BASE.format(token=_token(), method="getUpdates")
    params = {"offset": offset, "timeout": 20, "allowed_updates": ["message"]}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, params=params)
            data = resp.json()
            return data.get("result", [])
    except Exception:
        return []


def _build_stats() -> str:
    state = load_state()
    journal = state.get("journal", {})

    total = len(state.get("applied", []))
    by_status: dict[str, int] = {}
    for entry in journal.values():
        s = entry.get("status", "applied")
        by_status[s] = by_status.get(s, 0) + 1

    lines = ["<b>📊 Статистика</b>\n"]
    lines.append(f"Всего откликов: <b>{total}</b>")

    for status in ["applied", "hr_contacted", "interview", "offer", "rejected"]:
        count = by_status.get(status, 0)
        if count:
            emoji = STATUS_EMOJI[status]
            label = STATUS_LABEL[status]
            lines.append(f"{emoji} {label}: <b>{count}</b>")

    return "\n".join(lines)


def _build_journal() -> str:
    state = load_state()
    journal = state.get("journal", {})

    if not journal:
        return "Журнал пуст — откликов ещё не было."

    # Сортируем по дате (applied_at), берём последние 10
    entries = sorted(journal.items(), key=lambda x: x[1].get("applied_at", ""), reverse=True)[:10]

    lines = ["<b>📋 Последние отклики</b>\n"]
    for vid, entry in entries:
        emoji = STATUS_EMOJI.get(entry.get("status", "applied"), "📤")
        title = entry.get("title", "?")
        company = entry.get("company", "?")
        date = entry.get("applied_at", "?")
        url = entry.get("url", "")
        hr_count = len(entry.get("hr_messages", []))

        line = f"{emoji} <b>{company}</b> — {title}"
        if hr_count:
            line += f" ({hr_count} сообщ. HR)"
        line += f"\n   <i>{date}</i>"
        if url:
            line += f'  <a href="{url}">открыть</a>'
        lines.append(line)

    return "\n\n".join(lines)


async def handle_command(text: str) -> None:
    cmd = text.strip().lower().split()[0]
    if cmd == "/stats":
        await _send(_build_stats())
    elif cmd == "/journal":
        await _send(_build_journal())
    elif cmd == "/help":
        await _send(
            "<b>Команды:</b>\n"
            "/stats — статистика по откликам\n"
            "/journal — последние 10 откликов"
        )


async def run_bot() -> None:
    """Фоновая задача — поллинг команд от пользователя."""
    if not _token() or not _chat_id():
        return

    offset = 0
    # Сбрасываем накопленные обновления при старте
    updates = await _get_updates(offset)
    if updates:
        offset = updates[-1]["update_id"] + 1

    while True:
        try:
            updates = await _get_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                text = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                # Отвечаем только своему chat_id
                if text.startswith("/") and chat_id == _chat_id():
                    await handle_command(text)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"  ⚠ Telegram bot: {e}")
            await asyncio.sleep(5)
