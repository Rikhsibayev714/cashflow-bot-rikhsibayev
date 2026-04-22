"""
Cashflow Telegram Bot — Google Sheets версия
Python 3.14 совместимая версия
"""

import logging
import os
import json
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
KASSAS = ["Импорт Савдо", "Касса Ахрор", "Пластик карта"]

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

STEP_TYPE, STEP_KASSA, STEP_UZS, STEP_USD, STEP_NOTE, STEP_CONFIRM = range(6)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_gc():
    creds_json = os.environ.get("GOOGLE_CREDS")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    return gspread.authorize(creds)

def get_sheets():
    gc = get_gc()
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.worksheet("Cashflow"), sh.worksheet("Balance")

def find_next_row(ws_cash) -> int:
    col_a = ws_cash.col_values(1)
    col_b = ws_cash.col_values(2)
    max_len = max(len(col_a), len(col_b))
    for i in range(2, max_len):
        a = col_a[i] if i < len(col_a) else ""
        b = col_b[i] if i < len(col_b) else ""
        if not str(a).strip() and not str(b).strip():
            return i + 1
    return max_len + 1

def write_transaction(data: dict):
    try:
        ws_cash, _ = get_sheets()
        row = find_next_row(ws_cash)
        date_str = data["date"].strftime("%d.%m.%Y")
        updates = [
            (f"A{row}", date_str),
            (f"B{row}", data["kassa"]),
            (f"C{row}", data["inflow_uzs"]  or ""),
            (f"D{row}", data["inflow_usd"]  or ""),
            (f"E{row}", data["note"]),
            (f"F{row}", data["outflow_uzs"] or ""),
            (f"G{row}", data["outflow_usd"] or ""),
            (f"T{row}", "Telegram Bot"),
        ]
        for cell, val in updates:
            ws_cash.update(cell, [[val]])
        return True, f"Записано в строку {row}"
    except Exception as e:
        logger.error(f"Sheets write error: {e}")
        return False, f"Ошибка записи: {e}"

def read_last_rows(n: int = 5):
    try:
        ws_cash, _ = get_sheets()
        all_rows = ws_cash.get_all_values()
        data_rows = []
        for i, row in enumerate(all_rows[2:], start=3):
            if not any(row[:7]):
                continue
            data_rows.append({
                "row":     i,
                "date":    row[0]  if len(row) > 0  else "",
                "kassa":   row[1]  if len(row) > 1  else "",
                "in_uzs":  row[2]  if len(row) > 2  else "",
                "in_usd":  row[3]  if len(row) > 3  else "",
                "note":    row[4]  if len(row) > 4  else "",
                "out_uzs": row[5]  if len(row) > 5  else "",
                "out_usd": row[6]  if len(row) > 6  else "",
                "is_bot":  (row[19] == "Telegram Bot") if len(row) > 19 else False,
            })
        return data_rows[-n:] if len(data_rows) >= n else data_rows
    except Exception as e:
        return f"Ошибка чтения: {e}"

def read_balance():
    try:
        _, ws_bal = get_sheets()
        all_rows = ws_bal.get_all_values()
        items = []
        for row in all_rows[2:]:
            if len(row) < 2 or not row[0]:
                continue
            try:
                num = int(row[0])
            except Exception:
                continue
            def to_num(v):
                try:
                    return float(str(v).replace(" ", "").replace(",", "."))
                except Exception:
                    return 0.0
            items.append({
                "num":  num,
                "name": row[1] if len(row) > 1 else "—",
                "uzs":  to_num(row[2] if len(row) > 2 else ""),
                "usd":  to_num(row[3] if len(row) > 3 else ""),
            })
        return items, None
    except Exception as e:
        return None, f"Ошибка чтения Balance: {e}"

def fmt(val) -> str:
    if val is None or val == "" or val == 0:
        return "—"
    try:
        n = float(str(val).replace(" ", "").replace(",", "."))
        if n == 0:
            return "—"
        if n < 0:
            return f"-{abs(n):,.0f}".replace(",", " ")
        return f"{n:,.0f}".replace(",", " ")
    except Exception:
        return str(val) if val else "—"

def summary(ud: dict) -> str:
    sign = "ПРИХОД" if ud.get("type") == "inflow" else "РАСХОД"
    return (
        f"{sign}\n"
        f"Дата: {ud['date'].strftime('%d.%m.%Y')}\n"
        f"Касса: {ud.get('kassa', '—')}\n"
        f"UZS: {fmt(ud.get('uzs'))}\n"
        f"USD: {fmt(ud.get('usd'))}\n"
        f"Назначение: {ud.get('note', '—')}"
    )

def row_to_text(r: dict, idx=None) -> str:
    prefix = f"{idx}. " if idx else ""
    direction = "ПРИХОД" if (r.get("in_uzs") or r.get("in_usd")) else "РАСХОД"
    bot_mark  = " [бот]" if r.get("is_bot") else ""
    uzs = fmt(r.get("in_uzs") or r.get("out_uzs"))
    usd = fmt(r.get("in_usd") or r.get("out_usd"))
    return (
        f"{prefix}{direction}{bot_mark}\n"
        f"  Дата: {r.get('date') or '—'}\n"
        f"  Касса: {r.get('kassa') or '—'}\n"
        f"  UZS: {uzs}   USD: {usd}\n"
        f"  Назначение: {r.get('note') or '—'}"
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["Приход", "Расход"]]
    await update.message.reply_text(
        "Cashflow Bot\n\nВыбери тип операции:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
    )
    return STEP_TYPE

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Загружаю...")
    rows = read_last_rows(5)
    if isinstance(rows, str):
        await update.message.reply_text(rows)
        return
    if not rows:
        await update.message.reply_text("Записей пока нет.")
        return
    lines = ["Последние 5 записей:\n"]
    for i, r in enumerate(reversed(rows), 1):
        lines.append(row_to_text(r, i))
        lines.append("")
    await update.message.reply_text("\n".join(lines))

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Загружаю баланс...")
    items, err = read_balance()
    if err:
        await update.message.reply_text(err)
        return
    if not items:
        await update.message.reply_text("Лист Balance пустой.")
        return
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    nonzero = [i for i in items if i["uzs"] != 0 or i["usd"] != 0]
    zero    = [i for i in items if i["uzs"] == 0 and i["usd"] == 0]
    lines = [f"БАЛАНС на {now}\n"]
    if nonzero:
        lines.append("── С остатком ──")
        for it in nonzero:
            uzs_str = f"{fmt(it['uzs'])} UZS" if it["uzs"] != 0 else ""
            usd_str = f"{fmt(it['usd'])} USD" if it["usd"] != 0 else ""
            amounts = "  |  ".join(filter(None, [uzs_str, usd_str]))
            lines.append(f"{it['num']}. {it['name']}\n   {amounts}")
    if zero:
        lines.append("\n── Нулевые ──")
        lines.append(", ".join(f"{it['num']}.{it['name']}" for it in zero))
    total_uzs = sum(it["uzs"] for it in items)
    total_usd = sum(it["usd"] for it in items)
    lines.append(f"\nИТОГО:\n  UZS: {fmt(total_uzs)}\n  USD: {fmt(total_usd)}")
    await update.message.reply_text("\n".join(lines))

async def step_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Приход" in text:
        context.user_data["type"] = "inflow"
    elif "Расход" in text:
        context.user_data["type"] = "outflow"
    else:
        await update.message.reply_text("Выбери: Приход или Расход")
        return STEP_TYPE
    kb = [[k] for k in KASSAS]
    await update.message.reply_text(
        "Выбери кассу:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
    )
    return STEP_KASSA

async def step_kassa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text not in KASSAS:
        kb = [[k] for k in KASSAS]
        await update.message.reply_text(
            "Выбери из списка:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
        )
        return STEP_KASSA
    context.user_data["kassa"] = text
    await update.message.reply_text("Сумма UZS (или 0):", reply_markup=ReplyKeyboardRemove())
    return STEP_UZS

async def step_uzs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(" ", "").replace(",", ".")
    try:
        val = float(text)
        context.user_data["uzs"] = val if val > 0 else None
    except ValueError:
        await update.message.reply_text("Введи число, например: 500000 или 0")
        return STEP_UZS
    await update.message.reply_text("Сумма USD (или 0):")
    return STEP_USD

async def step_usd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(" ", "").replace(",", ".")
    try:
        val = float(text)
        context.user_data["usd"] = val if val > 0 else None
    except ValueError:
        await update.message.reply_text("Введи число, например: 100 или 0")
        return STEP_USD
    await update.message.reply_text("Назначение / комментарий:")
    return STEP_NOTE

async def step_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["note"] = update.message.text.strip()
    context.user_data["date"] = datetime.today()
    kb = [["Подтвердить", "Отмена"]]
    await update.message.reply_text(
        f"Проверь данные:\n\n{summary(context.user_data)}",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
    )
    return STEP_CONFIRM

async def step_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Отмена" in text:
        context.user_data.clear()
        await update.message.reply_text("Отменено. Напиши /start.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END
    if "Подтвердить" not in text:
        kb = [["Подтвердить", "Отмена"]]
        await update.message.reply_text("Нажми кнопку:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True))
        return STEP_CONFIRM
    ud = context.user_data
    t  = ud.get("type")
    data = {
        "date":        ud.get("date", datetime.today()),
        "kassa":       ud["kassa"],
        "inflow_uzs":  ud.get("uzs") if t == "inflow"  else None,
        "inflow_usd":  ud.get("usd") if t == "inflow"  else None,
        "outflow_uzs": ud.get("uzs") if t == "outflow" else None,
        "outflow_usd": ud.get("usd") if t == "outflow" else None,
        "note":        ud.get("note", ""),
    }
    await update.message.reply_text("Сохраняю...", reply_markup=ReplyKeyboardRemove())
    ok, msg = write_transaction(data)
    if ok:
        await update.message.reply_text(f"Сохранено! {msg}\n\nНапиши /start для новой записи.")
    else:
        await update.message.reply_text(f"{msg}\n\nНапиши /start.")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменено. Напиши /start.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def main():
    # Используем JobQueue=False для совместимости с Python 3.14
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .updater(None)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            STEP_TYPE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_type)],
            STEP_KASSA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, step_kassa)],
            STEP_UZS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_uzs)],
            STEP_USD:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_usd)],
            STEP_NOTE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, step_note)],
            STEP_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("balance", cmd_balance))

    print("=" * 50)
    print("Cashflow Bot (Google Sheets) запущен!")
    print("Команды: /start  /history  /balance")
    print("=" * 50)

    import asyncio

    async def run():
        await app.initialize()
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        # держим процесс живым
        stop_event = asyncio.Event()
        await stop_event.wait()

    asyncio.run(run())

if __name__ == "__main__":
    main()
