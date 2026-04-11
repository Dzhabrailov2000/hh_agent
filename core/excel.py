"""
Экспорт статистики откликов в Excel.

Файл vacancies.xlsx обновляется после каждого цикла агента.
Листы:
  - Отклики   — одна строка на вакансию (все отклики с деталями)
  - По дням   — агрегированная статистика по дням
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import os

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

EXCEL_FILE = Path("vacancies.xlsx")

# Цвета статусов
STATUS_COLORS = {
    "applied":      "FFFFFF",  # белый
    "hr_contacted": "D6E4F7",  # голубой
    "human_hr":     "D4EDDA",  # зелёный
    "rejected":     "F8D7DA",  # красный
}

STATUS_LABELS = {
    "applied":      "Отклик отправлен",
    "hr_contacted": "HR ответил (бот)",
    "human_hr":     "Живой HR",
    "rejected":     "Отказ",
}

HEADER_FILL   = PatternFill("solid", fgColor="2C3E50")
HEADER_FONT   = Font(color="FFFFFF", bold=True)
THIN_BORDER   = Border(
    left=Side(style="thin", color="CCCCCC"),
    right=Side(style="thin", color="CCCCCC"),
    top=Side(style="thin", color="CCCCCC"),
    bottom=Side(style="thin", color="CCCCCC"),
)


def _apply_header(ws, headers: list[str]) -> None:
    for col, title in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = THIN_BORDER
    ws.row_dimensions[1].height = 30


def _set_col_widths(ws, widths: list[int]) -> None:
    for col, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(col)].width = w


def _style_row(ws, row: int, status: str) -> None:
    color = STATUS_COLORS.get(status, "FFFFFF")
    fill = PatternFill("solid", fgColor=color)
    for col in range(1, ws.max_column + 1):
        cell = ws.cell(row=row, column=col)
        cell.fill = fill
        cell.border = THIN_BORDER
        cell.alignment = Alignment(vertical="center", wrap_text=False)


def update(state: dict) -> None:
    """Перестраивает Excel из текущего state['journal']."""
    wb = Workbook()

    _build_applications_sheet(wb, state)
    _build_daily_sheet(wb, state)
    _build_failed_sheet(wb, state)
    _build_quick_reply_sheet(wb, state)
    _build_screening_sheet(wb, state)

    # Удаляем дефолтный лист если не нужен
    if "Sheet" in wb.sheetnames:
        del wb["Sheet"]

    EXCEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    wb.save(EXCEL_FILE)


def _extract_date(ts: str) -> str:
    """Возвращает дату из строки '2026-04-09 HH:MM:SS' или 'HH:MM:SS' (→ '2026-04-09')."""
    if not ts:
        return ""
    if len(ts) >= 10 and ts[4] == "-":
        return ts[:10]
    # Старый формат — только время, дата первого запуска
    return "2026-04-09"


def _build_applications_sheet(wb: Workbook, state: dict) -> None:
    ws = wb.create_sheet("Отклики")

    headers = ["Дата", "Вакансия", "Компания", "Оценка", "Статус", "Ссылка", "Дата ответа HR"]
    _apply_header(ws, headers)
    _set_col_widths(ws, [14, 40, 28, 8, 22, 20, 16])

    journal = state.get("journal", {})
    # Сортируем по дате отклика (новые вверху)
    entries = sorted(journal.values(), key=lambda e: e.get("applied_at", ""), reverse=True)

    for row_num, entry in enumerate(entries, start=2):
        status = entry.get("status", "applied")
        hr_messages = entry.get("hr_messages", [])
        hr_date = hr_messages[0].get("at", "") if hr_messages else ""

        applied_date = _extract_date(entry.get("applied_at", ""))

        url = entry.get("url", "")
        score = entry.get("score", "")

        ws.cell(row=row_num, column=1, value=applied_date)
        ws.cell(row=row_num, column=2, value=entry.get("title", ""))
        ws.cell(row=row_num, column=3, value=entry.get("company", ""))
        ws.cell(row=row_num, column=4, value=score)
        ws.cell(row=row_num, column=5, value=STATUS_LABELS.get(status, status))

        # Ссылка кликабельная
        if url:
            link_cell = ws.cell(row=row_num, column=6, value="Открыть")
            link_cell.hyperlink = url
            link_cell.font = Font(color="0563C1", underline="single")
        else:
            ws.cell(row=row_num, column=6, value="")

        ws.cell(row=row_num, column=7, value=_extract_date(hr_date))

        _style_row(ws, row_num, status)

    # Закрепить заголовок
    ws.freeze_panes = "A2"


def _build_failed_sheet(wb: Workbook, state: dict) -> None:
    ws = wb.create_sheet("Ошибки откликов")

    headers = ["Дата", "Вакансия", "Компания", "Оценка", "Попыток", "Причина", "Ссылка", "Скриншоты"]
    _apply_header(ws, headers)
    _set_col_widths(ws, [16, 40, 28, 8, 8, 50, 20, 40])

    failed = state.get("failed_apply", {})
    entries = sorted(failed.values(), key=lambda e: e.get("failed_at", ""), reverse=True)

    ERROR_FILL = PatternFill("solid", fgColor="FFF3CD")  # жёлтый

    for row_num, entry in enumerate(entries, start=2):
        failed_date = _extract_date(entry.get("failed_at", ""))
        url = entry.get("url", "")

        ws.cell(row=row_num, column=1, value=failed_date)
        ws.cell(row=row_num, column=2, value=entry.get("title", ""))
        ws.cell(row=row_num, column=3, value=entry.get("company", ""))
        ws.cell(row=row_num, column=4, value=entry.get("score", ""))
        ws.cell(row=row_num, column=5, value=entry.get("attempts", 0))
        ws.cell(row=row_num, column=6, value=entry.get("reason", ""))

        if url:
            link_cell = ws.cell(row=row_num, column=7, value="Открыть")
            link_cell.hyperlink = url
            link_cell.font = Font(color="0563C1", underline="single")
        else:
            ws.cell(row=row_num, column=7, value="")

        # Скриншоты — список путей через запятую, каждый кликабелен
        screenshots = entry.get("screenshots", [])
        if screenshots:
            # Первый скриншот делаем гиперссылкой, остальные текстом
            shots_cell = ws.cell(row=row_num, column=8, value=", ".join(screenshots))
            if len(screenshots) == 1:
                shots_cell.hyperlink = Path(screenshots[0]).resolve().as_uri()
                shots_cell.font = Font(color="0563C1", underline="single")
        else:
            ws.cell(row=row_num, column=8, value="")

        for col in range(1, 9):
            cell = ws.cell(row=row_num, column=col)
            cell.fill = ERROR_FILL
            cell.border = THIN_BORDER
            cell.alignment = Alignment(vertical="center", wrap_text=col in (6, 8))

    ws.freeze_panes = "A2"


def _build_quick_reply_sheet(wb: Workbook, state: dict) -> None:
    ws = wb.create_sheet("Быстрые ответы")

    headers = ["Дата", "Вакансия", "Компания", "Сообщение HR", "Варианты", "Действие", "Выбор / Причина", "Скриншот", "Чат"]
    _apply_header(ws, headers)
    _set_col_widths(ws, [16, 35, 25, 40, 30, 12, 35, 40, 15])

    AUTO_FILL   = PatternFill("solid", fgColor="D4EDDA")  # зелёный — авто
    NOTIFY_FILL = PatternFill("solid", fgColor="FFF3CD")  # жёлтый — уведомление

    logs = state.get("quick_reply_log", [])
    for row_num, entry in enumerate(reversed(logs), start=2):
        action = entry.get("action", "")
        fill = AUTO_FILL if action == "auto" else NOTIFY_FILL

        ws.cell(row=row_num, column=1, value=_extract_date(entry.get("at", "")))
        ws.cell(row=row_num, column=2, value=entry.get("vacancy", ""))
        ws.cell(row=row_num, column=3, value=entry.get("company", ""))
        ws.cell(row=row_num, column=4, value=entry.get("message", "")[:200])
        ws.cell(row=row_num, column=5, value=" | ".join(entry.get("options", [])))
        ws.cell(row=row_num, column=6, value="Авто" if action == "auto" else "Уведомление")

        value_text = entry.get("chosen", "") or entry.get("reason", "")
        ws.cell(row=row_num, column=7, value=value_text)

        shot = entry.get("screenshot", "")
        if shot and Path(shot).exists():
            shot_cell = ws.cell(row=row_num, column=8, value=shot)
            shot_cell.hyperlink = Path(shot).resolve().as_uri()
            shot_cell.font = Font(color="0563C1", underline="single")
        else:
            ws.cell(row=row_num, column=8, value=shot or "")

        url = entry.get("url", "")
        if url:
            link_cell = ws.cell(row=row_num, column=9, value="Открыть")
            link_cell.hyperlink = url
            link_cell.font = Font(color="0563C1", underline="single")
        else:
            ws.cell(row=row_num, column=9, value="")

        for col in range(1, 10):
            cell = ws.cell(row=row_num, column=col)
            cell.fill = fill
            cell.border = THIN_BORDER
            cell.alignment = Alignment(vertical="center", wrap_text=col in (4, 5, 7))

    ws.freeze_panes = "A2"


def _build_screening_sheet(wb: Workbook, state: dict) -> None:
    ws = wb.create_sheet("Скрининг")

    headers = ["Дата", "Вакансия", "Компания", "Вопрос", "Ответ", "Ссылка"]
    _apply_header(ws, headers)
    _set_col_widths(ws, [12, 35, 25, 45, 55, 12])

    SCREENING_FILL = PatternFill("solid", fgColor="EAF4FB")

    journal = state.get("journal", {})
    entries = sorted(journal.values(), key=lambda e: e.get("applied_at", ""), reverse=True)

    row_num = 2
    for entry in entries:
        screening = entry.get("screening", [])
        if not screening:
            continue
        date = _extract_date(entry.get("applied_at", ""))
        title = entry.get("title", "")
        company = entry.get("company", "")
        url = entry.get("url", "")

        for item in screening:
            ws.cell(row=row_num, column=1, value=date)
            ws.cell(row=row_num, column=2, value=title)
            ws.cell(row=row_num, column=3, value=company)
            ws.cell(row=row_num, column=4, value=item.get("question", ""))
            ws.cell(row=row_num, column=5, value=item.get("answer", ""))
            if url:
                cell = ws.cell(row=row_num, column=6, value="Открыть")
                cell.hyperlink = url
                cell.font = Font(color="0563C1", underline="single")
            for col in range(1, 7):
                cell = ws.cell(row=row_num, column=col)
                cell.fill = SCREENING_FILL
                cell.border = THIN_BORDER
                cell.alignment = Alignment(vertical="center", wrap_text=col in (4, 5))
            row_num += 1

    ws.freeze_panes = "A2"


def _build_daily_sheet(wb: Workbook, state: dict) -> None:
    ws = wb.create_sheet("По дням")

    headers = ["Дата", "Откликов", "Ответов HR", "Живых HR"]
    _apply_header(ws, headers)
    _set_col_widths(ws, [14, 12, 14, 12])

    journal = state.get("journal", {})

    # Агрегация по дням
    by_day: dict[str, dict] = defaultdict(lambda: {"applied": 0, "hr_contacted": 0, "human_hr": 0})
    for entry in journal.values():
        applied_at = entry.get("applied_at", "")
        day = _extract_date(applied_at) or "неизвестно"
        status = entry.get("status", "applied")
        by_day[day]["applied"] += 1
        if status in ("hr_contacted", "human_hr"):
            by_day[day]["hr_contacted"] += 1
        if status == "human_hr":
            by_day[day]["human_hr"] += 1

    for row_num, (day, counts) in enumerate(sorted(by_day.items(), reverse=True), start=2):
        ws.cell(row=row_num, column=1, value=day)
        ws.cell(row=row_num, column=2, value=counts["applied"])
        ws.cell(row=row_num, column=3, value=counts["hr_contacted"])
        ws.cell(row=row_num, column=4, value=counts["human_hr"])
        for col in range(1, 5):
            cell = ws.cell(row=row_num, column=col)
            cell.border = THIN_BORDER
            cell.alignment = Alignment(horizontal="center", vertical="center")

    ws.freeze_panes = "A2"
