"""
HH.ru Job Agent — главный цикл (Playwright-версия).

Запуск:
    pip install -r requirements.txt
    playwright install chromium
    cp .env.example .env   # заполните HH_LOGIN, HH_PASSWORD, ANTHROPIC_API_KEY
    python main.py         # первый запуск — браузер откроется видимым для логина

Флаги:
    --dry-run    Анализировать без откликов и ответов
    --once       Один проход без цикла
    --headless   Запуск без видимого браузера (только после сохранённой сессии)
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import shutil
import argparse
from datetime import datetime
from pathlib import Path

# load_dotenv() должен быть ДО импорта matcher/responder,
# иначе Anthropic-клиент инициализируется без API ключа
from dotenv import load_dotenv
load_dotenv()

from config import (
    RESUME,
    SEARCH_QUERIES,
    SEARCH_AREA,
    EXPERIENCE,
    SALARY_FROM,
    SCHEDULE,
    VACANCIES_PER_RUN,
    MIN_RELEVANCE_SCORE,
    CHECK_INTERVAL_MINUTES,
    COVER_LETTER,
    TITLE_BLACKLIST,
    COMPANY_BLACKLIST,
    USE_RECOMMENDATIONS,
)
from browser.client import HHBrowserClient
from ai.matcher import score_vacancy
from ai.responder import generate_response, is_human_hr, classify_quick_reply
from telegram.notifier import send_hr_notification, send_applied_notification, send_chat_summary
from telegram.bot import run_bot
from core.state import load_state, save_state
from core.excel import update as update_excel
from core.exceptions import StopOnFail


MAX_APPLY_ATTEMPTS = 3


async def _do_apply(
    client: HHBrowserClient,
    state: dict,
    url: str,
    vid: str,
    full: dict,
    score: int,
    applied_ids: set,
    stop_on_fail: bool = False,
) -> None:
    """Выполняет отклик и обновляет state. При ошибке — записывает в failed_apply."""
    title = full.get("name", "?")
    company = full.get("employer", {}).get("name", "?")
    try:
        result, reason, screenshot = await client.apply_to_vacancy(url, COVER_LETTER, vacancy_id=vid)

        if result == "not_authorized":
            print(f"    → ✗ Сессия протухла, перелогиниваемся...")
            hh_login = os.environ.get("HH_LOGIN")
            hh_password = os.environ.get("HH_PASSWORD")
            await client.login(hh_login, hh_password)
            result, reason, screenshot = await client.apply_to_vacancy(url, COVER_LETTER, vacancy_id=vid)
            if result == "applied":
                print(f"    → ✓ Отклик отправлен после повторного логина")

        if result == "applied":
            print(f"    → ✓ Отклик отправлен")
            screening = client._last_screening_answers
            client._last_screening_answers = []
        elif result == "already_applied":
            print(f"    → уже откликались")
        elif result == "error":
            print(f"    → ✗ Ошибка отклика: {reason}")
            _save_failed(state, vid, title, company, url, score, reason, screenshot)
            save_state(state)
            if stop_on_fail:
                raise StopOnFail(f"{title} @ {company}: {reason}")
            return  # НЕ добавляем в applied — ретраим на следующем цикле
        else:
            print(f"    → ✗ Неизвестный результат: {result}")

        applied_ids.add(vid)
        state["applied"].append(vid)
        # Убираем из failed если там было
        state.setdefault("failed_apply", {}).pop(vid, None)

        state["journal"][vid] = {
            "title": title,
            "company": company,
            "url": url,
            "applied_at": now(),
            "score": score,
            "status": "applied",
            "hr_messages": [],
            "screening": screening or [],
        }
        save_state(state)

        if result == "applied" and not dry_run:
            try:
                await send_applied_notification(
                    vacancy_title=title,
                    employer_name=company,
                    vacancy_url=url,
                    screening=screening,
                )
            except Exception as tg_err:
                print(f"    ⚠ Telegram: {tg_err}")

        await asyncio.sleep(2)

    except StopOnFail:
        raise
    except Exception as e:
        print(f"    → ✗ Исключение: {e}")
        _save_failed(state, vid, title, company, url, score, str(e), "")
        save_state(state)


def _save_failed(
    state: dict, vid: str, title: str, company: str, url: str,
    score: int, reason: str, screenshot: str = "",
) -> None:
    """Сохраняет или обновляет запись о неудачном отклике."""
    failed = state.setdefault("failed_apply", {})
    existing = failed.get(vid, {})
    attempts = existing.get("attempts", 0) + 1
    # Собираем историю скриншотов
    screenshots = existing.get("screenshots", [])
    if screenshot:
        screenshots.append(screenshot)
    failed[vid] = {
        "title": title,
        "company": company,
        "url": url,
        "score": score,
        "reason": reason,
        "failed_at": now(),
        "attempts": attempts,
        "screenshots": screenshots,
    }
    print(f"    → 📋 Сохранён в failed_apply (попытка {attempts}/{MAX_APPLY_ATTEMPTS})")


async def _retry_failed(
    client: HHBrowserClient, state: dict, dry_run: bool
) -> None:
    """Повторяет попытки откликнуться на вакансии из failed_apply."""
    failed = state.get("failed_apply", {})
    # Только те, что ещё не исчерпали попытки
    to_retry = {
        vid: entry for vid, entry in failed.items()
        if entry.get("attempts", 0) < MAX_APPLY_ATTEMPTS
        and vid not in state["applied"]
    }

    if not to_retry:
        return

    print(f"\n  [↩] Ретрай {len(to_retry)} неудачных откликов...")

    for vid, entry in to_retry.items():
        title = entry.get("title", "?")
        company = entry.get("company", "?")
        url = entry.get("url", "")
        score = entry.get("score", 0)
        prev_reason = entry.get("reason", "")
        attempts = entry.get("attempts", 0)

        print(f"    [↩{attempts+1}] {title} @ {company}")
        print(f"         Причина прошлой ошибки: {prev_reason}")

        if dry_run:
            print(f"    → [dry-run] ретрай пропущен")
            continue

        if not url:
            continue

        # Загружаем полную карточку заново — вакансия могла измениться
        try:
            full = await client.get_vacancy_details(url)
        except Exception as e:
            _save_failed(state, vid, title, company, url, score, f"ошибка загрузки: {e}")
            save_state(state)
            continue

        applied_ids = set(state["applied"])
        await _do_apply(client, state, url, vid, full, score, applied_ids)


# ---------------------------------------------------------------------------
# Шаг 1: поиск и отклики
# ---------------------------------------------------------------------------

async def run_apply_step(
    client: HHBrowserClient, state: dict, dry_run: bool, stop_on_fail: bool = False
) -> None:
    print(f"\n[{now()}] Поиск вакансий по {len(SEARCH_QUERIES)} запросам...")

    # Собираем вакансии
    vacancies = []
    if USE_RECOMMENDATIONS:
        print(f"  Режим: рекомендации (удалённо, от {SALARY_FROM:,} ₽)")
        vacancies = await client.get_recommended_vacancies(
            schedule=SCHEDULE,
            salary_from=SALARY_FROM,
            per_page=VACANCIES_PER_RUN,
            experience_list=EXPERIENCE,
        )
    else:
        seen_ids: set = set()
        for query in SEARCH_QUERIES:
            page_num = 0
            while True:
                results = await client.search_vacancies(
                    text=query,
                    area=SEARCH_AREA,
                    experience=EXPERIENCE,
                    salary_from=SALARY_FROM,
                    schedule=SCHEDULE,
                    per_page=VACANCIES_PER_RUN,
                    page_num=page_num,
                )
                for v in results:
                    if v["id"] not in seen_ids:
                        seen_ids.add(v["id"])
                        vacancies.append(v)
                print(f"    [{query}] стр.{page_num + 1}: {len(results)} вакансий")
                if len(results) < VACANCIES_PER_RUN:
                    break
                page_num += 1

    print(f"  Найдено уникальных: {len(vacancies)}")

    applied_ids = set(state["applied"])
    new_applied = 0

    for v in vacancies:
        vid = v["id"]
        if vid in applied_ids:
            continue

        # Загружаем полную карточку вакансии
        try:
            full = await client.get_vacancy_details(v["url"])
        except Exception as e:
            print(f"  ⚠ Вакансия {vid}: ошибка загрузки — {e}")
            continue

        if full.get("already_applied"):
            applied_ids.add(vid)
            state["applied"].append(vid)
            save_state(state)
            continue

        company = full.get("employer", {}).get("name", "?")
        title = full.get("name", "?")
        title_lower = title.lower()
        desc_lower = (full.get("description", "") + " ".join(
            s.get("name", "") for s in full.get("key_skills", [])
        )).lower()

        # Фильтр по компании (Сбер, Яндекс и пр.)
        if any(kw.lower() in company.lower() for kw in COMPANY_BLACKLIST):
            print(f"  [--] {title} @ {company} — company blacklist")
            continue

        # --- Проверка по описанию: Java/Kotlin backend ---

        # 1. Должен быть Java или Kotlin (в названии или описании)
        has_java = "java" in title_lower or "java" in desc_lower
        has_kotlin = "kotlin" in title_lower or "kotlin" in desc_lower
        if not has_java and not has_kotlin:
            print(f"  [--] {title} — нет Java/Kotlin")
            continue

        # 2. Не QA / автоматизация тестирования
        qa_keywords = (
            "qa ", "qа ", "quality assurance", "тестировщик", "тестировани",
            "автоматизатор", "automation test", "aqa", "aqа",
        )
        if any(kw in title_lower for kw in qa_keywords):
            print(f"  [--] {title} — QA/тестирование")
            continue

        # 3. Не фронтенд-heavy fullstack (когда фронт — основной стек)
        frontend_primary = ("react developer", "vue developer", "angular developer",
                            "frontend developer", "фронтенд разработчик",
                            "fullstack (react", "fullstack (vue", "full stack (react")
        if any(kw in title_lower for kw in frontend_primary):
            print(f"  [--] {title} — фронтенд/fullstack")
            continue

        score = 0
        print(f"  [→] {title} @ {company}")

        if dry_run:
            print(f"    → [dry-run] отклик пропущен")
            continue

        await _do_apply(client, state, v["url"], vid, full, score, applied_ids, stop_on_fail=stop_on_fail)
        new_applied += sum(1 for _ in [None] if vid in state["journal"] and state["journal"][vid]["status"] == "applied")

    # Ретрай ранее упавших откликов (до 3 попыток)
    await _retry_failed(client, state, dry_run)

    new_applied = sum(
        1 for e in state["journal"].values()
        if e.get("status") == "applied" and e.get("applied_at", "").startswith(now()[:10])
    )
    print(f"  Новых откликов: {new_applied}")


# ---------------------------------------------------------------------------
# Шаг 2: мониторинг чатов
# ---------------------------------------------------------------------------

async def run_chat_step(
    client: HHBrowserClient, state: dict, dry_run: bool
) -> None:
    print(f"\n[{now()}] Проверка чатов")

    try:
        negotiations = await client.get_negotiations()
    except Exception as e:
        print(f"  ⚠ Не удалось загрузить переговоры: {e}")
        return

    print(f"  Переговоров: {len(negotiations)}")
    chat_last_seen: dict = state.setdefault("chat_last_seen", {})
    new_notifications = 0
    summary_auto: list[dict] = []
    summary_notified: list[dict] = []

    for neg in negotiations:
        neg_url = neg["url"]
        neg_id = neg["id"]
        neg_tab = neg.get("tab_url")
        vacancy_name = neg.get("vacancy", {}).get("name", "?")
        employer_name = neg.get("employer", {}).get("name", "?")

        try:
            messages, chat_locked, quick_replies = await client.get_messages(neg_id, tab_url=neg_tab)
        except Exception as e:
            print(f"  ⚠ Чат {neg_id}: {e}")
            continue

        if not messages:
            print(f"  [пусто] {vacancy_name} @ {employer_name}")
            continue

        last = messages[-1]
        last_author = last.get("author", {}).get("participant_type", "")

        # Пропускаем если последнее сообщение от соискателя (т.е. от нас)
        if last_author not in ("employer", ""):
            print(f"  [наше] {vacancy_name} @ {employer_name} — последнее сообщение наше")
            continue

        # Проверяем: уже обрабатывали это конкретное сообщение?
        last_text_hash = hashlib.md5(last["text"].encode()).hexdigest()
        if chat_last_seen.get(neg_id) == last_text_hash:
            print(f"  [уже] {vacancy_name} @ {employer_name} — уже обработали")
            continue

        if chat_locked and not quick_replies:
            print(f"  [🔒] {vacancy_name} @ {employer_name} — чат заблокирован, пропускаем")
            continue

        tab_label = neg_tab.split("/")[-1] if neg_tab else "активные"
        print(f"  HR написал: {vacancy_name} @ {employer_name} [{tab_label}]")
        print(f"    Сообщение: {last['text'][:150]}")

        # Определяем: живой HR или бот? (до обновления журнала)
        try:
            human = is_human_hr(messages)
        except Exception as e:
            print(f"    ⚠ Ошибка классификации: {e}")
            human = False

        # Обновляем журнал
        vid = _find_vacancy_id_by_neg(state, neg_id, vacancy_name, employer_name, neg_url)
        if vid and vid in state["journal"]:
            entry = state["journal"][vid]
            entry["status"] = "human_hr" if human else "hr_contacted"
            entry["hr_messages"].append({
                "id": last.get("id", last_text_hash),
                "text": last["text"],
                "at": now(),
            })

        # --- Кнопки быстрых ответов ---
        if quick_replies:
            print(f"    Кнопки ответа: {quick_replies}")

            # Скриншот до нажатия — для лога
            shot_path = f"screenshots/qr_{neg_id}_{now()[:10]}.png"
            try:
                await client.screenshot_chat(neg_id, shot_path, tab_url=neg_tab)
            except Exception:
                shot_path = ""

            try:
                action, value = classify_quick_reply(messages, quick_replies)
            except Exception as e:
                print(f"    ⚠ Ошибка классификации кнопок: {e}")
                action, value = "notify", "ошибка классификации"

            # Логируем событие
            state["quick_reply_log"].append({
                "at": now(),
                "vacancy": vacancy_name,
                "company": employer_name,
                "message": last["text"][:300],
                "options": quick_replies,
                "action": action,
                "chosen": value if action == "auto" else "",
                "reason": value if action == "notify" else "",
                "screenshot": shot_path,
                "url": neg_url,
            })
            save_state(state)

            if action == "auto":
                print(f"    → Авто-ответ кнопкой: «{value}»")
                if dry_run:
                    print(f"    → [dry-run] клик пропущен")
                else:
                    try:
                        await client.click_quick_reply(neg_url, value, tab_url=neg_tab)
                        print(f"    → ✓ Кнопка нажата")
                        summary_auto.append({"company": employer_name, "vacancy": vacancy_name, "action": f"кнопка «{value}»", "response": f"кнопка: «{value}»"})
                        await asyncio.sleep(1.5)
                    except Exception as e:
                        print(f"    → ✗ {e}")
            else:
                # IMPORTANT — уведомляем пользователя
                print(f"    → Требует решения: {value}, уведомляю...")
                if dry_run:
                    print(f"    → [dry-run] уведомление пропущено")
                else:
                    try:
                        options_text = " | ".join(quick_replies)
                        await send_hr_notification(
                            vacancy_title=vacancy_name,
                            employer_name=employer_name,
                            message_text=f"{last['text'][:200]}\n\n🔘 Варианты: {options_text}\n\n❗ {value}",
                            negotiation_url=neg_url,
                        )
                        print(f"    → ✓ Уведомление отправлено в Telegram")
                        summary_notified.append({"company": employer_name, "vacancy": vacancy_name, "reason": value})
                        new_notifications += 1
                    except Exception as e:
                        print(f"    → ✗ Telegram: {e}")

        elif human:
            # Живой HR без кнопок — уведомляем
            print(f"    → Похоже на живого HR, уведомляю...")
            if dry_run:
                print(f"    → [dry-run] уведомление пропущено")
            else:
                try:
                    await send_hr_notification(
                        vacancy_title=vacancy_name,
                        employer_name=employer_name,
                        message_text=last["text"],
                        negotiation_url=neg_url,
                    )
                    print(f"    → ✓ Уведомление отправлено в Telegram")
                    summary_notified.append({"company": employer_name, "vacancy": vacancy_name, "reason": "живой HR"})
                    new_notifications += 1
                except Exception as e:
                    print(f"    → ✗ Telegram: {e}")
        else:
            # Бот/шаблон — генерируем текстовый ответ
            print(f"    → Автоматическое сообщение, отвечаю через AI...")
            try:
                answer = generate_response(RESUME, messages)
            except Exception as e:
                print(f"    ⚠ Ошибка генерации: {e}")
                answer = ""

            if answer:
                print(f"    Ответ: {answer[:150]}")
                if dry_run:
                    print(f"    → [dry-run] отправка пропущена")
                else:
                    try:
                        await client.send_message(neg_url, answer, tab_url=neg_tab)
                        print(f"    → ✓ Ответ отправлен")
                        summary_auto.append({"company": employer_name, "vacancy": vacancy_name, "action": "AI-ответ", "response": answer[:300]})
                        await asyncio.sleep(1.5)
                    except Exception as e:
                        print(f"    → ✗ {e}")

        chat_last_seen[neg_id] = last_text_hash
        state["chat_last_seen"] = chat_last_seen
        save_state(state)

    print(f"  Новых уведомлений: {new_notifications}")

    # Итоговый отчёт в Telegram
    if not dry_run and (summary_auto or summary_notified):
        try:
            await send_chat_summary(summary_auto, summary_notified)
        except Exception as e:
            print(f"  ⚠ Ошибка отправки итога в Telegram: {e}")


def _find_vacancy_id_by_neg(
    state: dict, neg_id: str, vacancy_name: str, employer_name: str, neg_url: str
) -> str | None:
    """Ищем vacancy_id в журнале по названию вакансии и компании."""
    for vid, entry in state["journal"].items():
        if entry["title"] == vacancy_name and entry["company"] == employer_name:
            return vid
    return None


# ---------------------------------------------------------------------------
# Главный цикл
# ---------------------------------------------------------------------------

def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--visible", action="store_true", help="Показывать браузер во время работы")
    parser.add_argument("--stop-on-fail", action="store_true", help="Остановить процесс при первой ошибке отклика")
    parser.add_argument(
        "--mode", choices=["apply", "chat", "both"], default="both",
        help="apply — только отклики; chat — только чаты; both — всё вместе (по умолчанию)"
    )
    args = parser.parse_args()

    hh_login = os.environ.get("HH_LOGIN")
    hh_password = os.environ.get("HH_PASSWORD")

    if not hh_login or not hh_password:
        print("Ошибка: задайте HH_LOGIN и HH_PASSWORD в .env")
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Ошибка: задайте ANTHROPIC_API_KEY в .env")
        sys.exit(1)

    bot_task = asyncio.create_task(run_bot())

    # --visible: сразу открываем браузер видимым
    # По умолчанию: тихий режим (headless), браузер открывается только если нужен логин
    force_visible = args.visible
    mode = args.mode

    # Если режим apply/chat — копируем базовый профиль если целевой не существует
    if mode in ("apply", "chat"):
        src = Path("hh_browser_profile")
        dst = Path(f"hh_browser_profile_{mode}")
        if src.exists() and not dst.exists():
            print(f"  Копирую профиль браузера в {dst}...")
            shutil.copytree(str(src), str(dst))

    # Интервалы: отклики каждые 30 мин, чаты каждые 5 мин
    APPLY_INTERVAL = CHECK_INTERVAL_MINUTES * 60
    CHAT_INTERVAL = 5 * 60

    client = HHBrowserClient(headless=not force_visible, mode=mode)

    try:
        await client.start()
        logged_in = await client.is_logged_in()

        if not logged_in:
            if force_visible:
                print("Войдите в браузере вручную...")
            else:
                await client.close()
                print("Сессия не найдена — открываю браузер для входа...")
                client = HHBrowserClient(headless=False, mode=mode)
                await client.start()
            if not await client.login(hh_login, hh_password):
                print("Не удалось войти. Проверьте логин/пароль.")
                return
        else:
            print("✓ Сессия активна" + ("" if force_visible else ", браузер работает в фоне"))

        if args.dry_run:
            print("[DRY RUN] Режим анализа без отправки\n")

        mode_label = {"apply": "откликов", "chat": "чатов", "both": "откликов + чатов"}[mode]
        interval = CHAT_INTERVAL if mode == "chat" else APPLY_INTERVAL
        print(f"  Режим: {mode_label}, интервал: {interval // 60} мин")

        state = load_state()

        while True:
            try:
                if mode in ("apply", "both"):
                    await run_apply_step(client, state, dry_run=args.dry_run, stop_on_fail=args.stop_on_fail)
                if mode in ("chat", "both"):
                    await run_chat_step(client, state, dry_run=args.dry_run)
                try:
                    update_excel(state)
                    print(f"  📊 Excel обновлён: vacancies.xlsx")
                except Exception as e:
                    print(f"  ⚠ Excel: {e}")
            except KeyboardInterrupt:
                break
            except StopOnFail as e:
                print(f"\n🛑 Остановка по --stop-on-fail: {e}")
                break
            except Exception as e:
                print(f"\n⚠ Ошибка цикла: {e}")

            if args.once:
                break

            print(f"\n  Следующий запуск через {interval // 60} мин. Ctrl+C для остановки.")
            try:
                await asyncio.sleep(interval)
            except (KeyboardInterrupt, asyncio.CancelledError):
                break

    finally:
        bot_task.cancel()
        print("\nЗавершение...")
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
