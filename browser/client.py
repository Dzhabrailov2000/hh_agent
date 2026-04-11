"""
HH.ru клиент на Playwright — управляет браузером как человек.

Сессия сохраняется в hh_session.json — логиниться нужно только один раз.
При CAPTCHA браузер открывается в видимом режиме, агент ждёт решения вручную.
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Page, BrowserContext, Playwright

SESSION_FILE = Path("hh_session.json")
PROFILE_DIR = Path("hh_browser_profile")
BASE_URL = "https://hh.ru"


def get_profile_dir(mode: str = "default") -> Path:
    """Возвращает путь к профилю браузера для заданного режима."""
    if mode == "apply":
        return Path("hh_browser_profile_apply")
    if mode == "chat":
        return Path("hh_browser_profile_chat")
    return PROFILE_DIR


async def _delay(min_ms: int = 600, max_ms: int = 2000):
    """Случайная пауза для имитации человека."""
    await asyncio.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def _clean_vacancy_url(url: str) -> str:
    """Оставляет только путь /vacancy/<id> без tracking-параметров."""
    m = re.search(r"(https?://[^/]+/vacancy/\d+)", url)
    return m.group(1) if m else url


class HHBrowserClient:
    def __init__(self, headless: bool = False, mode: str = "default"):
        """
        headless=False — браузер виден (нужно для первого логина и CAPTCHA).
        headless=True — фоновый режим после сохранения сессии.
        mode — 'apply', 'chat', или 'default' — определяет папку профиля.
        """
        self.headless = headless
        self.mode = mode
        self._pw: Optional[Playwright] = None
        self._ctx: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._last_screening_answers: list[dict] = []

    async def start(self):
        self._pw = await async_playwright().start()
        profile_dir = get_profile_dir(self.mode)
        profile_dir.mkdir(exist_ok=True)

        # Удаляем файлы блокировки Chromium — могут остаться после некорректного завершения
        for lock_file in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
            (profile_dir / lock_file).unlink(missing_ok=True)

        # launch_persistent_context сохраняет cookies + sessionStorage + localStorage
        # между запусками — логиниться нужно только один раз
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 800},
            locale="ru-RU",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self.page.set_default_timeout(60000)  # 60 сек на все операции

        # Скрываем признаки Playwright
        await self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)

        # Закрываем диалог "Восстановить страницы?" если появился
        try:
            restore_btn = self.page.get_by_text("Восстановить", exact=True)
            close_btn = self.page.locator("[aria-label='Close'], [title='Close'], button:has-text('×'), button:has-text('✕')").first
            # Ищем крестик диалога восстановления
            dismiss = self.page.locator("button[class*='close'], [data-testid='close']").first
            if await dismiss.is_visible(timeout=2000):
                await dismiss.click()
            elif await close_btn.is_visible(timeout=1000):
                await close_btn.click()
        except Exception:
            pass

    async def save_session(self):
        # Профиль сохраняется автоматически через persistent context.
        # Метод оставлен для совместимости с вызовами по коду.
        pass

    # -------------------------------------------------------------------------
    # Авторизация
    # -------------------------------------------------------------------------

    async def is_logged_in(self) -> bool:
        try:
            await self.page.goto(
                f"{BASE_URL}/applicant/resumes",
                wait_until="domcontentloaded",
                timeout=60000,
            )
        except Exception as e:
            print(f"  ⚠ Не удалось открыть HH.ru: {e}")
            print("  Проверьте интернет-соединение.")
            return False
        await _delay(800, 1200)

        # Если редиректнуло на логин — не авторизованы
        if "/account/login" in self.page.url:
            return False

        # Позитивная проверка: карточка резюме /resume/<hash> есть только у авторизованного соискателя
        resume_card = await self.page.locator(
            "a[href*='/resume/']"
        ).count()
        if resume_card > 0:
            await self.save_session()
            return True

        return False

    async def login(self, login: str, password: str) -> bool:
        """Войти на HH.ru. Возвращает True при успехе."""
        await self.page.goto(f"{BASE_URL}/account/login", wait_until="domcontentloaded")
        await _delay(1000, 2000)

        # Если уже авторизованы — редирект прошёл сам
        if "/account/login" not in self.page.url:
            await self.save_session()
            print("✓ Авторизация успешна (редирект)")
            return True

        # Ждём ручного входа — даём 3 минуты
        print("\n" + "="*50)
        print("  Войдите вручную в открытом браузере.")
        print("  Введите телефон, пароль/SMS и нажмите «Войти».")
        print("  Ожидание до 3 минут...")
        print("="*50 + "\n")

        deadline = 180  # секунд
        interval = 2
        elapsed = 0
        while elapsed < deadline:
            await asyncio.sleep(interval)
            elapsed += interval
            if "/account/login" not in self.page.url:
                await self.save_session()
                print("✓ Авторизация успешна")
                return True

        print("✗ Время ожидания истекло — войти не удалось")
        return False

    # -------------------------------------------------------------------------
    # Поиск вакансий
    # -------------------------------------------------------------------------

    async def search_vacancies(
        self,
        text: str,
        area: int = 113,
        experience: str | list = "between3And6",
        salary_from: int = 0,
        schedule: str = None,
        page_num: int = 0,
        per_page: int = 20,
    ) -> list[dict]:
        """Поиск вакансий. Возвращает список базовой информации."""
        if isinstance(experience, list):
            exp_params = "&".join(f"experience={e}" for e in experience)
        else:
            exp_params = f"experience={experience}"
        params = (
            f"text={text}&area={area}&{exp_params}"
            f"&page={page_num}&per_page={per_page}"
        )
        if schedule:
            params += f"&schedule={schedule}"
        if salary_from:
            params += f"&salary={salary_from}&only_with_salary=true"

        await self.page.goto(f"{BASE_URL}/search/vacancy?{params}", wait_until="domcontentloaded")
        await _delay(1000, 2000)
        await self._handle_captcha()

        results = await self._scrape_vacancy_cards()
        return results

    async def _scrape_vacancy_cards(self) -> list[dict]:
        """Считывает карточки вакансий с текущей открытой страницы поиска."""
        results = []
        cards = await self.page.locator("[data-qa='vacancy-serp__vacancy']").all()

        for card in cards:
            try:
                title_el = card.locator("[data-qa='serp-item__title']")
                if not await title_el.count():
                    continue

                title = await title_el.inner_text()
                href = await title_el.get_attribute("href") or ""

                vid_match = re.search(r"/vacancy/(\d+)", href)
                if not vid_match:
                    continue

                company = ""
                comp_el = card.locator("[data-qa='vacancy-serp__vacancy-employer']")
                if await comp_el.count():
                    company = await comp_el.first.inner_text()

                salary_text = ""
                sal_el = card.locator("[data-qa='vacancy-serp__vacancy-compensation']")
                if await sal_el.count():
                    salary_text = await sal_el.first.inner_text()

                full_url = href if href.startswith("http") else f"{BASE_URL}{href}"
                results.append({
                    "id": vid_match.group(1),
                    "name": title.strip(),
                    "url": _clean_vacancy_url(full_url),
                    "employer": {"name": company.strip()},
                    "salary_text": salary_text.strip(),
                })
            except Exception:
                continue

        return results

    async def get_recommended_vacancies(
        self,
        schedule: str = "remote",
        salary_from: int = 0,
        per_page: int = 50,
        experience_list: list[str] | None = None,
    ) -> list[dict]:
        """
        Собирает все рекомендованные вакансии для резюме.
        Открывает страницу резюме, берёт ссылку 'N вакансий', добавляет фильтры.
        Делает два прохода: experience=between3And6 и experience=moreThan6.
        """
        await self.page.goto(f"{BASE_URL}/applicant/resumes", wait_until="domcontentloaded")
        await _delay(1500, 2500)
        await self._handle_captcha()

        # Ищем ссылку рядом с резюме: "567 вакансий"
        rec_url: str | None = None
        links = await self.page.locator("a[href*='resume=']").all()
        for link in links:
            href = await link.get_attribute("href") or ""
            if "vacancy" in href or "search" in href:
                rec_url = href if href.startswith("http") else f"{BASE_URL}{href}"
                break

        if not rec_url:
            # Пробуем через текст кнопки
            btn = self.page.get_by_text(re.compile(r"\d+\s*вакансий")).first
            if await btn.count():
                rec_url = await btn.get_attribute("href")
                if rec_url and not rec_url.startswith("http"):
                    rec_url = f"{BASE_URL}{rec_url}"

        if not rec_url:
            print("  ⚠ Не удалось найти ссылку на рекомендованные вакансии")
            return []

        # Только per_page — area не нужен для удалённых вакансий
        sep = "&" if "?" in rec_url else "?"
        rec_url += f"{sep}per_page={per_page}"

        # Два прохода по опыту: 3-6 лет и 6+ лет
        if experience_list is None:
            experience_list = ["between3And6", "moreThan6"]

        # Два порядка сортировки — чтобы получить больше уникальных вакансий
        # (hh.ru отдаёт ~160 результатов на запрос, разные сортировки дают разные наборы)
        sort_orders = ["relevance", "publication_time"]

        all_results: list[dict] = []
        seen_ids: set = set()

        for exp in experience_list:
            for order in sort_orders:
                # Строим URL с фильтрами удалёнки, опыта и сортировки
                exp_url = rec_url
                if schedule:
                    exp_url += f"&work_format={schedule.upper()}"
                exp_url += f"&experience={exp}&order_by={order}"

                print(f"\n  → Проход: experience={exp}, sort={order}")

                # Пагинация через URL (&page=0, &page=1, ...)
                page_idx = 0
                while True:
                    page_url = f"{exp_url}&page={page_idx}"
                    await self.page.goto(page_url, wait_until="domcontentloaded")
                    await _delay(1500, 2500)
                    await self._handle_captcha()

                    page_results = await self._scrape_vacancy_cards()
                    if not page_results:
                        break  # пустая страница — конец

                    new_count = 0
                    for v in page_results:
                        if v["id"] not in seen_ids:
                            seen_ids.add(v["id"])
                            all_results.append(v)
                            new_count += 1
                    print(f"    стр.{page_idx + 1}: {len(page_results)} вакансий ({new_count} новых)")

                    # Все вакансии страницы уже видели — дальше нет смысла
                    if new_count == 0:
                        break

                    page_idx += 1

        print(f"\n  Итого рекомендаций (уникальных): {len(all_results)}")
        return all_results

    async def get_vacancy_details(self, vacancy_url: str) -> dict:
        """Открывает страницу вакансии и достаёт полное описание."""
        await self.page.goto(vacancy_url, wait_until="domcontentloaded")
        await _delay(800, 1500)

        details: dict = {"url": vacancy_url}

        # Заголовок
        title_el = self.page.locator("h1[data-qa='vacancy-title']")
        if await title_el.count():
            details["name"] = await title_el.inner_text()

        # Компания
        comp_el = self.page.locator("[data-qa='vacancy-company-name']")
        if await comp_el.count():
            details["employer"] = {"name": (await comp_el.inner_text()).strip()}

        # Зарплата
        sal_el = self.page.locator("[data-qa='vacancy-salary']")
        if await sal_el.count():
            details["salary_text"] = await sal_el.inner_text()

        # Описание (уже без HTML — innerText)
        desc_el = self.page.locator("[data-qa='vacancy-description']").first
        if await desc_el.count():
            details["description"] = await desc_el.inner_text()

        # Ключевые навыки
        skills = []
        for el in await self.page.locator("[data-qa='bloko-tag__text']").all():
            skills.append({"name": await el.inner_text()})
        details["key_skills"] = skills

        # Уже откликались?
        details["already_applied"] = bool(
            await self.page.locator("text=Вы уже откликались").count()
        )

        return details

    # -------------------------------------------------------------------------
    # Откликнуться
    # -------------------------------------------------------------------------

    async def apply_to_vacancy(
        self, vacancy_url: str, cover_letter: str, vacancy_id: str = ""
    ) -> tuple[str, str, str]:
        """
        Отправить отклик на вакансию.
        Возвращает: (status, reason, screenshot_path)
          status: 'applied' | 'already_applied' | 'not_authorized' | 'error'
          reason: описание ошибки или пустая строка при успехе
          screenshot_path: путь к скриншоту ошибки или пустая строка
        """
        import os

        vacancy_url = _clean_vacancy_url(vacancy_url)
        await self.page.goto(vacancy_url, wait_until="domcontentloaded")
        await _delay(1200, 2000)

        tag = vacancy_id or "unknown"
        Path("screenshots").mkdir(exist_ok=True)

        if "/account/login" in self.page.url:
            return "not_authorized", "", ""

        if await self.page.locator("text=Вы уже откликались").count():
            return "already_applied", "", ""

        # Кнопка «Откликнуться» на странице вакансии
        btn = self.page.locator(
            "[data-qa='vacancy-response-link-top'], "
            "[data-qa='vacancy-response-btn-top'], "
            "[data-qa='vacancy-response-link-bottom'], "
            "[data-qa='vacancy-response-link']"
        ).first

        if not await btn.is_visible(timeout=3000):
            reason = await self._diagnose_apply_page()
            shot = f"screenshots/failed_{tag}_no_button.png"
            Path("screenshots").mkdir(exist_ok=True)
            await self.page.screenshot(path=shot)
            print(f"    ⚠ Кнопка 'Откликнуться' не найдена: {reason}")
            return "error", f"no_apply_button: {reason}", shot

        await btn.click()
        await _delay(2000, 3000)
        await self.page.screenshot(path=f"screenshots/apply_{tag}_step1.png")

        # --- Попап / форма «Сколько хотите получать» ---
        # Может появиться как шаг формы отклика (обязательный) или после отклика (опциональный).
        # В любом случае заполняем поле и жмём «Продолжить».
        try:
            from config import SALARY_FROM
            salary_field = self.page.locator(
                "input[data-qa*='salary'], "
                "input[placeholder*='зарплат' i], "
                "input[placeholder*='сумм' i], "
                "input[name*='salary'], "
                "input[type='number'][data-qa*='compensation']"
            ).first
            if await salary_field.is_visible(timeout=2000):
                await salary_field.click(click_count=3)
                await salary_field.fill(str(SALARY_FROM))
                await _delay(400, 700)
                print(f"    → Заполнена желаемая зарплата: {SALARY_FROM:,}")
                # Жмём «Сохранить и продолжить» или «Продолжить»
                for btn_name in ["Сохранить и продолжить", "Продолжить", "Далее"]:
                    btn = self.page.get_by_role("button", name=re.compile(btn_name, re.I)).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click()
                        await _delay(800, 1200)
                        break
            else:
                # Попап без поля ввода (просто информационный) — закрываем
                close_btn = self.page.locator(
                    "button[aria-label='Закрыть'], [data-qa='modal-close'], button.bloko-modal-close"
                ).first
                if await close_btn.is_visible(timeout=1000):
                    await close_btn.click()
                    await _delay(400, 700)
        except Exception:
            pass

        # Новый формат hh.ru: «Резюме доставлено» — резюме уже отправлено,
        # но на этой же странице могут быть скрининговые вопросы — заполняем их тоже.
        # Важно: проверяем что элемент ВИДЕН, а не просто есть в HTML (избегаем false-positive).
        try:
            delivered = self.page.locator(
                "text=Резюме доставлено, text=Resume delivered"
            ).first
            if await delivered.is_visible(timeout=1000):
                from config import RESUME
                await self._fill_screening_questions(RESUME)
                await self._fill_cover_letter_if_present(cover_letter)
                await self.save_session()
                return "applied", "", ""
        except Exception:
            pass

        # Если на странице уже есть «Вы откликнулись» — успех
        try:
            for phrase in ["Вы откликнулись", "Отклик отправлен", "уже откликались"]:
                if await self.page.locator(f"text={phrase}").first.is_visible(timeout=500):
                    await self.save_session()
                    return "applied", "", ""
        except Exception:
            pass

        # --- Закрываем cookie-баннер «Понятно» если мешает ---
        try:
            cookie_btn = self.page.get_by_role("button", name=re.compile("^Понятно$", re.I))
            if await cookie_btn.first.is_visible(timeout=1000):
                await cookie_btn.first.click()
                await _delay(300, 600)
        except Exception:
            pass

        # --- Попап «Напишите телефон» ---
        # ВАЖНО: сначала заполняем, потом кликаем «Продолжить»
        try:
            phone_input = self.page.locator(
                "input[placeholder*='Номер' i], "
                "input[placeholder*='телефон' i], "
                "input[type='tel'], "
                "input[data-qa*='phone'], "
                "input[name*='phone']"
            ).first
            if await phone_input.is_visible(timeout=3000):
                phone = os.environ.get("HH_LOGIN", "")
                print(f"    → Заполняю телефон: {phone}")
                await phone_input.click(click_count=3)
                await phone_input.fill(phone)
                await _delay(600, 1000)
                # Теперь кликаем «Продолжить» в этом попапе
                continue_phone_btn = self.page.get_by_role(
                    "button", name=re.compile("Продолжить", re.I)
                ).first
                if await continue_phone_btn.is_visible(timeout=2000):
                    await continue_phone_btn.click()
                    await _delay(1500, 2500)
                    print("    → Телефон подтверждён")
        except Exception:
            pass

        await self.page.screenshot(path=f"screenshots/apply_{tag}_step2.png")

        # --- Выбор резюме ---
        try:
            resume_radio = self.page.locator(
                "[data-qa='resume-list-item'] input[type='radio'], "
                "[data-qa='resume-list-item']"
            ).first
            if await resume_radio.count() and await resume_radio.is_visible(timeout=2000):
                await resume_radio.click()
                await _delay(500, 800)
        except Exception:
            pass

        # --- Кнопка «Продолжить» (двухшаговая форма) ---
        try:
            for btn_text in ["Продолжить", "Далее"]:
                cb = self.page.locator(
                    f"button:has-text('{btn_text}'), "
                    f"[data-qa*='submit']:has-text('{btn_text}')"
                ).first
                if await cb.is_visible(timeout=1500):
                    await cb.click()
                    await _delay(1000, 1500)
                    break
        except Exception:
            pass

        await self.page.screenshot(path=f"screenshots/apply_{tag}_step3.png")

        # --- Скрининговые вопросы работодателя ---
        from config import RESUME
        await self._fill_screening_questions(RESUME)

        # --- Сопроводительное письмо ---
        try:
            letter = self.page.locator(
                "textarea[data-qa='vacancy-response-popup-form-letter-input'], "
                "textarea[data-qa*='letter'], "
                "textarea[placeholder*='сопроводительн' i], "
                "textarea[placeholder*='письм' i], "
                "textarea[placeholder*='расскажите' i], "
                "textarea[placeholder*='напишите' i]"
            ).first
            if await letter.is_visible(timeout=2000):
                await letter.fill(cover_letter)
                await _delay(500, 900)
        except Exception:
            pass

        await self.page.screenshot(path=f"screenshots/apply_{tag}_step4.png")

        # --- Кнопка отправки ---
        submitted = False
        submit_selectors = [
            "[data-qa='vacancy-response-submit-popup']",
            "[data-qa='vacancy-response-submit']",
            "[data-qa='vacancy-response-letter-submit']",
            "button[type='submit']:has-text('Откликнуться')",
            "button[type='submit']:has-text('Отправить')",
            "button:has-text('Откликнуться')",
            "button:has-text('Отправить отклик')",
            "button:has-text('Отправить')",
        ]
        for sel in submit_selectors:
            try:
                submit_btn = self.page.locator(sel).last
                if await submit_btn.is_visible(timeout=1500):
                    print(f"    → Найдена кнопка: {sel}")
                    await submit_btn.click()
                    await _delay(2500, 3500)
                    submitted = True
                    break
            except Exception:
                continue

        if not submitted:
            reason = await self._diagnose_apply_page()
            Path("screenshots").mkdir(exist_ok=True)
            shot = f"screenshots/failed_{tag}_no_submit.png"
            await self.page.screenshot(path=shot)
            print(f"    ⚠ Кнопка отправки не найдена. {reason}")
            return "error", f"no_submit_button: {reason}", shot

        await self.page.screenshot(path=f"screenshots/apply_{tag}_step5.png")

        # Проверяем результат
        page_text = await self.page.content()
        if "уже откликались" in page_text.lower():
            return "already_applied", "", ""

        await self.save_session()
        return "applied", "", ""

    async def _diagnose_apply_page(self) -> str:
        """Собирает диагностику страницы — видимые кнопки и инпуты — для понимания причины ошибки."""
        parts = []
        try:
            all_buttons = await self.page.locator("button:visible").all()
            btns = []
            for b in all_buttons[:8]:
                try:
                    btns.append((await b.inner_text()).strip())
                except Exception:
                    pass
            if btns:
                parts.append(f"кнопки: {btns}")
        except Exception:
            pass
        try:
            inputs = await self.page.locator("input:visible, select:visible").all()
            inp_info = []
            for inp in inputs[:5]:
                try:
                    t = await inp.get_attribute("type") or "?"
                    name = await inp.get_attribute("name") or await inp.get_attribute("data-qa") or ""
                    placeholder = await inp.get_attribute("placeholder") or ""
                    inp_info.append(f"{t}/{name}/{placeholder}")
                except Exception:
                    pass
            if inp_info:
                parts.append(f"поля: {inp_info}")
        except Exception:
            pass
        try:
            url = self.page.url
            parts.append(f"url: {url}")
        except Exception:
            pass
        return " | ".join(parts) if parts else "нет деталей"

    # -------------------------------------------------------------------------
    # Чаты / переговоры — через chatik iframe
    # -------------------------------------------------------------------------

    async def _collect_negotiations_from_page(
        self, negotiations: list, seen_ids: set, tab_url: str
    ) -> None:
        """Собирает переговоры с кнопкой 'Перейти в чат' с текущей страницы."""
        chat_btns = await self.page.get_by_text("Перейти в чат").all()
        for btn in chat_btns:
            try:
                info = await btn.evaluate("""btn => {
                    const card = btn.closest('li,article,section,[class*="v-spacing"]');
                    if (!card) return null;
                    const vLink = card.querySelector('a[href*="/vacancy/"]');
                    const company = card.querySelector('[data-qa="negotiations-item-company"]');
                    const vacancy = card.querySelector('[data-qa="negotiations-item-vacancy"]');
                    if (!vLink) return null;
                    const m = vLink.href.match(/\\/vacancy\\/(\\d+)/);
                    return {
                        vid: m ? m[1] : null,
                        title: vacancy ? vacancy.textContent.trim() : vLink.textContent.trim(),
                        company: company ? company.textContent.trim() : ''
                    };
                }""")
                if not info or not info.get("vid"):
                    continue
                vid = info["vid"]
                if vid in seen_ids:
                    continue
                seen_ids.add(vid)
                negotiations.append({
                    "id": vid,
                    "url": f"{BASE_URL}/vacancy/{vid}",
                    "tab_url": tab_url,
                    "vacancy": {"name": info.get("title", "")},
                    "employer": {"name": info.get("company", "")},
                })
            except Exception:
                continue

    async def _collect_negotiations_paginated(
        self, negotiations: list, seen_ids: set, base_tab_url: str
    ) -> int:
        """Собирает переговоры со всех страниц вкладки (пагинация через &page=N)."""
        added_total = 0
        sep = "&" if "?" in base_tab_url else "?"
        for page_idx in range(0, 50):  # максимум 50 страниц
            page_url = f"{base_tab_url}{sep}page={page_idx}" if page_idx > 0 else base_tab_url
            if page_idx > 0:
                await self.page.goto(page_url, wait_until="domcontentloaded")
                await _delay(1200, 2000)
            before = len(negotiations)
            await self._collect_negotiations_from_page(negotiations, seen_ids, page_url)
            added = len(negotiations) - before
            added_total += added
            if added == 0:
                break  # страница пустая — конец пагинации
        return added_total

    async def get_negotiations(self) -> list[dict]:
        """
        Список переговоров где доступен чат ("Перейти в чат").
        Проверяет все вкладки (Активные, Ожидание, и др.) с пагинацией.
        Возвращает [{id, url, tab_url, vacancy.name, employer.name}]
        """
        main_url = f"{BASE_URL}/applicant/negotiations"
        await self.page.goto(main_url, wait_until="domcontentloaded")
        await _delay(2500, 3500)
        await self._handle_captcha()

        negotiations: list = []
        seen_ids: set = set()

        # Собираем с текущей (главной) вкладки + пагинация
        main_added = await self._collect_negotiations_paginated(negotiations, seen_ids, main_url)

        # Находим ссылки на другие вкладки переговоров
        tab_links = await self.page.locator("a[href*='/applicant/negotiations']").all()
        tab_urls: list = []
        for link in tab_links:
            href = await link.get_attribute("href") or ""
            if not href or href.rstrip("/") == "/applicant/negotiations":
                continue
            full = href if href.startswith("http") else f"{BASE_URL}{href}"
            if full != main_url and full not in tab_urls:
                tab_urls.append(full)

        print(f"  Вкладки переговоров: {[u.split('/')[-1] for u in tab_urls] or ['только главная']}")
        if main_added:
            print(f"    [главная] {main_added} чатов")

        for tab_url in tab_urls:
            try:
                await self.page.goto(tab_url, wait_until="domcontentloaded")
                await _delay(1500, 2500)
                added = await self._collect_negotiations_paginated(negotiations, seen_ids, tab_url)
                if added:
                    print(f"    [{tab_url.split('/')[-1]}] +{added} чатов")
            except Exception:
                continue

        return negotiations

    async def _open_chatik(self, vacancy_id: str, tab_url: str | None = None):
        """
        Открывает chatik-панель для нужной вакансии.
        tab_url — конкретная вкладка переговоров (Активные/Ожидание/...).
        Возвращает Frame chatik или None.
        """
        target = tab_url or f"{BASE_URL}/applicant/negotiations"
        if self.page.url != target:
            await self.page.goto(target, wait_until="domcontentloaded")
            await _delay(2500, 3500)

        # Получаем все vacancy_id с кнопкой чата (в порядке появления на странице)
        chat_btns = await self.page.get_by_text("Перейти в чат").all()
        vacancy_links = await self.page.locator("a[href*='/vacancy/']").all()

        # Строим список vacancy_id в порядке отображения
        ordered_ids = []
        seen = set()
        for link in vacancy_links:
            href = await link.get_attribute("href") or ""
            m = re.search(r"/vacancy/(\d+)", href)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                ordered_ids.append(m.group(1))

        # Находим индекс нашей вакансии
        if vacancy_id not in ordered_ids:
            return None
        idx = ordered_ids.index(vacancy_id)

        if idx >= len(chat_btns):
            return None

        await chat_btns[idx].click()
        await _delay(2000, 3500)

        for frame in self.page.frames:
            if "chatik.hh.ru/chat/" in frame.url:
                return frame
        return None

    async def _close_chatik(self):
        """Закрывает панель chatik."""
        try:
            close = self.page.locator("[class*='close'], [class*='Close']").first
            if await close.is_visible(timeout=1000):
                await close.click()
                await _delay(500, 800)
        except Exception:
            pass

    async def get_messages(
        self, vacancy_id: str, tab_url: str | None = None
    ) -> tuple[list[dict], bool, list[str]]:
        """
        Читает сообщения из chatik-панели для вакансии.
        Возвращает (messages, locked, quick_replies):
          messages      — список [{id, text, author.participant_type}]
          locked        — True если нет поля ввода (чат заблокирован/закрыт)
          quick_replies — список текстов кнопок-ответов (если есть)
        """
        frame = await self._open_chatik(vacancy_id, tab_url)
        if not frame:
            return [], True, []

        messages = []
        try:
            msg_els = await frame.locator("[class*='message--']").all()
            for i, el in enumerate(msg_els):
                try:
                    text = (await el.inner_text()).strip()
                    if not text:
                        continue
                    cls = await el.evaluate("e => e.className")
                    is_mine = "message_my" in cls
                    messages.append({
                        "id": f"{vacancy_id}_msg_{i}",
                        "text": text,
                        "author": {"participant_type": "applicant" if is_mine else "employer"},
                    })
                except Exception:
                    continue
        except Exception:
            pass

        # Проверяем, доступен ли ввод
        locked = True
        try:
            textarea = frame.locator("textarea, [contenteditable='true']").first
            locked = not await textarea.is_visible(timeout=1500)
        except Exception:
            pass

        # Ищем кнопки быстрых ответов (quick replies) в чате
        quick_replies: list[str] = []
        try:
            qr_selectors = (
                "[class*='suggest'], [class*='quick-reply'], [class*='quickReply'], "
                "[class*='QuickReply'], [data-qa*='suggest'], [data-qa*='quick'], "
                "[class*='chat-suggest'], [class*='chatSuggest']"
            )
            qr_els = await frame.locator(qr_selectors).all()
            for el in qr_els:
                try:
                    if await el.is_visible(timeout=500):
                        txt = (await el.inner_text()).strip()
                        if txt and txt not in quick_replies:
                            quick_replies.append(txt)
                except Exception:
                    continue
        except Exception:
            pass

        # Fallback: кнопки в зоне после последнего сообщения, не кнопка отправки
        if not quick_replies:
            try:
                btns = await frame.locator("button:visible").all()
                send_texts = {"отправить", "send", "→", ">"}
                for btn in btns:
                    try:
                        txt = (await btn.inner_text()).strip()
                        if txt and txt.lower() not in send_texts and len(txt) > 1:
                            quick_replies.append(txt)
                    except Exception:
                        continue
            except Exception:
                pass

        await self._close_chatik()
        return messages, locked, quick_replies

    async def screenshot_chat(
        self, vacancy_id: str, path: str, tab_url: str | None = None
    ) -> bool:
        """Открывает chatik и сохраняет скриншот. Возвращает True при успехе."""
        frame = await self._open_chatik(vacancy_id, tab_url)
        if not frame:
            return False
        try:
            Path("screenshots").mkdir(exist_ok=True)
            # Скриншот через page (frame.screenshot не всегда доступен)
            await self.page.screenshot(path=path)
            await self._close_chatik()
            return True
        except Exception:
            await self._close_chatik()
            return False

    async def click_quick_reply(
        self, vacancy_id: str, option_text: str, tab_url: str | None = None
    ) -> bool:
        """Кликает кнопку быстрого ответа в чате по тексту."""
        frame = await self._open_chatik(vacancy_id, tab_url)
        if not frame:
            return False
        try:
            # Ищем кнопку с точным текстом
            btn = frame.locator(f"button:has-text('{option_text}')").first
            if not await btn.is_visible(timeout=3000):
                # Fallback: любой кликабельный элемент с таким текстом
                btn = frame.locator(f":has-text('{option_text}')").last
            await btn.click()
            await _delay(800, 1200)
            await self._close_chatik()
            return True
        except Exception:
            await self._close_chatik()
            return False

    async def send_message(self, vacancy_id: str, text: str, tab_url: str | None = None) -> bool:
        """Отправляет сообщение в chatik-панели для вакансии."""
        frame = await self._open_chatik(vacancy_id, tab_url)
        if not frame:
            return False

        try:
            textarea = frame.locator("textarea, [contenteditable='true']").first
            await textarea.wait_for(state="visible", timeout=5000)
            await textarea.click()
            await _delay(300, 500)
            await textarea.type(text, delay=20)
            await _delay(400, 700)

            send_btn = frame.locator("button[type='submit'], button[class*='send']").last
            if await send_btn.is_visible(timeout=2000):
                await send_btn.click()
            else:
                await textarea.press("Control+Enter")

            await _delay(800, 1200)
            await self._close_chatik()
            return True
        except Exception:
            await self._close_chatik()
            return False

    # -------------------------------------------------------------------------
    # Утилиты
    # -------------------------------------------------------------------------

    async def _fill_screening_questions(self, resume: str) -> bool:
        """
        Обнаруживает скрининговые вопросы работодателя, отвечает на них через Claude.
        Возвращает True если вопросы найдены и заполнены.
        Бросает StopOnFail если вопрос не удалось заполнить — для ручного разбора.
        """
        from ai.responder import answer_screening_questions
        from core.exceptions import StopOnFail

        # Ищем блоки с вопросами — hh.ru оборачивает их в fieldset или [data-qa='task-body']
        question_blocks = await self.page.locator(
            "[data-qa='task-body'], "
            "fieldset[class*='question'], "
            "[class*='taskBody'], "
            "[class*='screening-question'], "
            "fieldset, "
            "[data-qa*='question'], "
            "[class*='Question']"
        ).all()

        if not question_blocks:
            return False

        print(f"    → Обнаружена анкета: {len(question_blocks)} вопрос(ов)")
        questions = []

        for i, block in enumerate(question_blocks):
            try:
                # Текст вопроса
                label_el = block.locator("legend, label, [class*='title'], p, span").first
                q_text = (await label_el.inner_text()).strip() if await label_el.count() else f"Вопрос {i+1}"

                # Определяем тип поля
                if await block.locator("input[type='radio']").count():
                    q_type = "radio"
                    options = []
                    for opt in await block.locator("input[type='radio']").all():
                        val = await opt.get_attribute("value") or ""
                        # Ищем label рядом
                        label = await block.locator(f"label[for='{await opt.get_attribute('id')}']").first.inner_text() if await opt.get_attribute("id") else val
                        options.append(label.strip() or val)
                    questions.append({"id": str(i), "text": q_text, "type": "radio", "options": options, "_block_idx": i})

                elif await block.locator("textarea").count():
                    questions.append({"id": str(i), "text": q_text, "type": "textarea", "_block_idx": i})

                elif await block.locator("input[type='number']").count():
                    questions.append({"id": str(i), "text": q_text, "type": "number", "_block_idx": i})

                elif await block.locator("input[type='text'], input:not([type])").count():
                    questions.append({"id": str(i), "text": q_text, "type": "text", "_block_idx": i})

                elif await block.locator("select").count():
                    options = []
                    for opt in await block.locator("option").all():
                        options.append((await opt.inner_text()).strip())
                    questions.append({"id": str(i), "text": q_text, "type": "select", "options": options, "_block_idx": i})

            except Exception:
                continue

        if not questions:
            return False

        # Получаем ответы от Claude
        answers = answer_screening_questions(resume, questions)
        if not answers:
            return False

        # Заполняем поля
        for q in questions:
            qid = q["id"]
            answer = answers.get(qid, "")
            if not answer:
                raise StopOnFail(
                    f"Вопрос без ответа: [{q['text'][:80]}] (тип: {q['type']}) — "
                    f"Claude не смог ответить. Разберите вручную."
                )
            # Детект мусорных ответов — Claude признал что не знает
            garbage_markers = [
                "информация отсутствует", "не указано", "уточните",
                "не содержит", "отсутствует в", "нет информации",
                "не могу ответить", "данные отсутствуют",
            ]
            if any(m in answer.lower() for m in garbage_markers):
                raise StopOnFail(
                    f"Вопрос [{q['text'][:80]}]: Claude дал мусорный ответ: «{answer[:100]}». "
                    f"Разберите вручную."
                )

            block = question_blocks[q["_block_idx"]]
            try:
                if q["type"] == "radio":
                    clicked = False
                    for opt in await block.locator("input[type='radio']").all():
                        val = await opt.get_attribute("value") or ""
                        opt_id = await opt.get_attribute("id") or ""
                        label_text = ""
                        if opt_id:
                            lbl = block.locator(f"label[for='{opt_id}']").first
                            if await lbl.count():
                                label_text = (await lbl.inner_text()).strip()
                        if answer.lower() in val.lower() or answer.lower() in label_text.lower():
                            await opt.click()
                            await _delay(200, 400)
                            clicked = True
                            break
                    if not clicked:
                        raise StopOnFail(
                            f"Radio-вопрос [{q['text'][:80]}]: ответ '{answer}' не совпал ни с одной опцией {q.get('options')}. "
                            f"Разберите вручную."
                        )

                elif q["type"] in ("textarea", "text"):
                    field = block.locator("textarea, input[type='text'], input:not([type])").first
                    await field.fill(answer)
                    await _delay(200, 400)

                elif q["type"] == "number":
                    field = block.locator("input[type='number']").first
                    num = re.sub(r"[^\d]", "", answer)
                    if not num:
                        raise StopOnFail(
                            f"Number-вопрос [{q['text'][:80]}]: ответ '{answer}' не содержит цифр. Разберите вручную."
                        )
                    await field.fill(num)
                    await _delay(200, 400)

                elif q["type"] == "select":
                    sel = block.locator("select").first
                    await sel.select_option(label=answer)
                    await _delay(200, 400)

                print(f"    → [{q['text'][:40]}] → {answer[:60]}")
            except StopOnFail:
                raise
            except Exception as e:
                raise StopOnFail(
                    f"Не удалось заполнить вопрос [{q['text'][:80]}] (тип: {q['type']}): {e}"
                ) from e

        filled = [
            {"question": q["text"], "answer": answers.get(q["id"], "")}
            for q in questions
        ]
        self._last_screening_answers = filled
        return filled

    async def _fill_cover_letter_if_present(self, cover_letter: str) -> None:
        """Заполняет и отправляет сопроводительное письмо если поле присутствует на странице."""
        try:
            textarea = self.page.locator(
                "textarea[placeholder*='сопроводительн' i], "
                "textarea[placeholder*='письм' i], "
                "textarea[placeholder*='расскажите' i], "
                "textarea[placeholder*='напишите' i], "
                "textarea[data-qa*='letter']"
            ).first
            if not await textarea.is_visible(timeout=3000):
                return
            await textarea.click()
            await _delay(300, 500)
            await textarea.fill(cover_letter)
            await _delay(500, 800)

            send_btn = self.page.locator(
                "button[data-qa*='submit'], "
                "button[type='submit']:has-text('Отправить'), "
                "button:has-text('Отправить')"
            ).first
            if await send_btn.is_visible(timeout=2000):
                await send_btn.click()
                await _delay(1000, 1500)
                print("    → ✓ Сопроводительное письмо отправлено")
        except Exception:
            pass

    async def _handle_captcha(self):
        """Если появилась CAPTCHA — ждём пока пользователь решит её вручную."""
        indicators = [
            "[data-qa='account-captcha']",
            ".g-recaptcha",
            "iframe[src*='recaptcha']",
            "iframe[src*='captcha']",
        ]
        for sel in indicators:
            if await self.page.locator(sel).count():
                print("\n⚠  CAPTCHA! Решите её вручную в браузере, затем нажмите Enter здесь...")
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, input)
                await _delay(1000, 2000)
                break

    async def close(self):
        try:
            await self.save_session()
        except Exception:
            pass
        try:
            if self._ctx:
                await self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
