"""
Cashflow Telegram Bot — Google Sheets версия
Улучшения: защита по ID, /edit, ежедневный баланс, кнопки меню
"""

import logging
import os
import json
import threading
from datetime import datetime, time
from http.server import HTTPServer, BaseHTTPRequestHandler

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
    JobQueue,
)

# ─────────────────────────────────────────────
#  НАСТРОЙКИ
# ─────────────────────────────────────────────
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
PORT           = int(os.environ.get("PORT", "8080"))

# Твой Telegram ID — узнай у @userinfobot
# Можно добавить несколько: [123456789, 987654321]
ALLOWED_IDS_STR = os.environ.get("ALLOWED_IDS", "")
ALLOWED_IDS = [int(x.strip()) for x in ALLOWED_IDS_STR.split(",") if x.strip()] if ALLOWED_IDS_STR else []

# Время ежедневного отчёта (UTC). UTC+5 = Ташкент. 04:00 UTC = 09:00 Ташкент
DAILY_REPORT_HOUR   = int(os.environ.get("DAILY_HOUR", "4"))
DAILY_REPORT_MINUTE = int(os.environ.get("DAILY_MINUTE", "0"))

KASSAS = ["Импорт Савдо", "Касса Ахрор", "Пластик карта"]

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  Шаги диалогов
# ─────────────────────────────────────────────
STEP_TYPE, STEP_KASSA, STEP_UZS, STEP_USD, STEP_NOTE, STEP_CONFIRM = range(6)
EDIT_FIELD, EDIT_VALUE, EDIT_CONFIRM = range(10, 13)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ─────────────────────────────────────────────
#  Health сервер для Render
# ─────────────────────────────────────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Cashflow Bot is running!")
    def log_message(self, format, *args):
        pass

def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health server on port {PORT}")

# ─────────────────────────────────────────────
#  Защита по ID
# ─────────────────────────────────────────────

def is_allowed(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True  # если список пустой — пускаем всех
    return update.effective_user.id in ALLOWED_IDS

async def check_access(update: Update) -> bool:
    if not is_allowed(update):
        uid = update.effective_user.id
        await update.message.reply_text(
            f"⛔ Нет доступа.\nТвой ID: {uid}\n\nДобавь его в переменную ALLOWED_IDS на Render."
        )
        logger.warning(f"Blocked user: {uid}")
        return False
    return True

# ─────────────────────────────────────────────
#  Google Sheets
# ─────────────────────────────────────────────

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

def find_next_row(ws) -> int:
    col_a = ws.col_values(1)
    col_b = ws.col_values(2)
    max_len = max(len(col_a), len(col_b))
    for i in range(2, max_len):
        a = col_a[i] if i < len(col_a) else ""
        b = col_b[i] if i < len(col_b) else ""
        if not str(a).strip() and not str(b).strip():
            return i + 1
    return max_len + 1

def find_last_bot_row(ws) -> int | None:
    col_t = ws.col_values(20)
    last = None
    for i, val in enumerate(col_t):
        if val == "Telegram Bot":
            last = i + 1
    return last

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
        logger.error(f"Write error: {e}")
        return False, f"Ошибка записи: {e}"

def update_last_bot_row(field: str, value) -> tuple[bool, str]:
    try:
        ws_cash, _ = get_sheets()
        row = find_last_bot_row(ws_cash)
        if row is None:
            return False, "Нет записей от бота для редактирования."
        col_map = {"kassa": 2, "in_uzs": 3, "in_usd": 4, "note": 5, "out_uzs": 6, "out_usd": 7}
        col = col_map.get(field)
        if not col:
            return False, "Неизвестное поле."
        ws_cash.update(f"{chr(64+col)}{row}", [[value]])
        return True, f"Строка {row} обновлена."
    except Exception as e:
        return False, f"Ошибка: {e}"

def get_last_bot_entry() -> dict | None:
    try:
        ws_cash, _ = get_sheets()
        row = find_last_bot_row(ws_cash)
        if row is None:
            return None
        r = ws_cash.row_values(row)
        return {
            "row":     row,
            "date":    r[0]  if len(r) > 0 else "",
            "kassa":   r[1]  if len(r) > 1 else "",
            "in_uzs":  r[2]  if len(r) > 2 else "",
            "in_usd":  r[3]  if len(r) > 3 else "",
            "note":    r[4]  if len(r) > 4 else "",
            "out_uzs": r[5]  if len(r) > 5 else "",
            "out_usd": r[6]  if len(r) > 6 else "",
        }
    except Exception as e:
        logger.error(f"get_last_bot_entry error: {e}")
        return None

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
        return None, f"Ошибка: {e}"

# ─────────────────────────────────────────────
#  Форматирование
# ─────────────────────────────────────────────

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
    sign = "📥 ПРИХОД" if ud.get("type") == "inflow" else "📤 РАСХОД"
    return (
        f"{sign}\n"
        f"📅 Дата: {ud['date'].strftime('%d.%m.%Y')}\n"
        f"🏦 Касса: {ud.get('kassa', '—')}\n"
        f"💵 UZS: {fmt(ud.get('uzs'))}\n"
        f"💲 USD: {fmt(ud.get('usd'))}\n"
        f"📝 Назначение: {ud.get('note', '—')}"
    )

def row_to_text(r: dict, idx=None) -> str:
    prefix = f"{idx}. " if idx else ""
    direction = "📥 ПРИХОД" if (r.get("in_uzs") or r.get("in_usd")) else "📤 РАСХОД"
    bot_mark  = " [бот]" if r.get("is_bot") else ""
    uzs = fmt(r.get("in_uzs") or r.get("out_uzs"))
    usd = fmt(r.get("in_usd") or r.get("out_usd"))
    return (
        f"{prefix}{direction}{bot_mark}\n"
        f"  📅 {r.get('date') or '—'}  🏦 {r.get('kassa') or '—'}\n"
        f"  💵 {uzs}   💲 {usd}\n"
        f"  📝 {r.get('note') or '—'}"
    )

def balance_text(items: list) -> str:
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    nonzero = [i for i in items if i["uzs"] != 0 or i["usd"] != 0]
    zero    = [i for i in items if i["uzs"] == 0 and i["usd"] == 0]
    lines = [f"💰 БАЛАНС на {now}\n"]
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
    lines.append(f"\n📊 ИТОГО:\n  UZS: {fmt(total_uzs)}\n  USD: {fmt(total_usd)}")
    return "\n".join(lines)

# ─────────────────────────────────────────────
#  Главное меню (кнопки)
# ─────────────────────────────────────────────

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["➕ Приход", "➖ Расход"],
        ["💰 Баланс", "📋 История"],
        ["✏️ Редактировать"],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

# ─────────────────────────────────────────────
#  Команды
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 Cashflow Bot\n\nВыбери действие:",
        reply_markup=MAIN_MENU,
    )
    return STEP_TYPE

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    await update.message.reply_text("Загружаю баланс...")
    items, err = read_balance()
    if err:
        await update.message.reply_text(err)
        return
    if not items:
        await update.message.reply_text("Лист Balance пустой.")
        return
    await update.message.reply_text(balance_text(items), reply_markup=MAIN_MENU)

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    await update.message.reply_text("Загружаю...")
    rows = read_last_rows(5)
    if isinstance(rows, str):
        await update.message.reply_text(rows)
        return
    if not rows:
        await update.message.reply_text("Записей пока нет.")
        return
    lines = ["📋 Последние 5 записей:\n"]
    for i, r in enumerate(reversed(rows), 1):
        lines.append(row_to_text(r, i))
        lines.append("")
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)

# ─────────────────────────────────────────────
#  /edit — редактирование последней записи
# ─────────────────────────────────────────────

EDIT_FIELD_LABELS = {
    "Касса":      "kassa",
    "UZS":        "uzs",
    "USD":        "usd",
    "Назначение": "note",
}

async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return ConversationHandler.END
    entry = get_last_bot_entry()
    if not entry:
        await update.message.reply_text(
            "Нет записей от бота для редактирования.",
            reply_markup=MAIN_MENU,
        )
        return ConversationHandler.END

    context.user_data["edit_entry"] = entry
    direction = "📥 ПРИХОД" if (entry.get("in_uzs") or entry.get("in_usd")) else "📤 РАСХОД"
    uzs = fmt(entry.get("in_uzs") or entry.get("out_uzs"))
    usd = fmt(entry.get("in_usd") or entry.get("out_usd"))
    text = (
        f"Последняя запись от бота (строка {entry['row']}):\n\n"
        f"{direction}\n"
        f"📅 {entry.get('date')}  🏦 {entry.get('kassa')}\n"
        f"💵 {uzs}   💲 {usd}\n"
        f"📝 {entry.get('note') or '—'}\n\n"
        f"Что изменить?"
    )
    kb = [[f] for f in EDIT_FIELD_LABELS.keys()] + [["❌ Отмена"]]
    await update.message.reply_text(
        text,
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
    )
    return EDIT_FIELD

async def edit_choose_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    if text not in EDIT_FIELD_LABELS:
        kb = [[f] for f in EDIT_FIELD_LABELS.keys()] + [["❌ Отмена"]]
        await update.message.reply_text(
            "Выбери поле:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
        )
        return EDIT_FIELD
    context.user_data["edit_field"] = text
    context.user_data["edit_field_key"] = EDIT_FIELD_LABELS[text]

    if text == "Касса":
        kb = [[k] for k in KASSAS] + [["❌ Отмена"]]
        await update.message.reply_text(
            "Выбери новую кассу:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
        )
    else:
        await update.message.reply_text(
            f"Введи новое значение для «{text}»:",
            reply_markup=ReplyKeyboardRemove(),
        )
    return EDIT_VALUE

async def edit_new_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END

    field_key = context.user_data.get("edit_field_key")
    field_label = context.user_data.get("edit_field")

    # Определяем реальный столбец
    entry = context.user_data.get("edit_entry", {})
    has_inflow = bool(entry.get("in_uzs") or entry.get("in_usd"))

    if field_key == "kassa":
        if text not in KASSAS:
            kb = [[k] for k in KASSAS] + [["❌ Отмена"]]
            await update.message.reply_text(
                "Выбери из списка:",
                reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
            )
            return EDIT_VALUE
        real_key = "kassa"
        new_val = text
    elif field_key == "uzs":
        real_key = "in_uzs" if has_inflow else "out_uzs"
        try:
            new_val = float(text.replace(" ", "").replace(",", "."))
        except ValueError:
            await update.message.reply_text("Введи число:")
            return EDIT_VALUE
    elif field_key == "usd":
        real_key = "in_usd" if has_inflow else "out_usd"
        try:
            new_val = float(text.replace(" ", "").replace(",", "."))
        except ValueError:
            await update.message.reply_text("Введи число:")
            return EDIT_VALUE
    else:
        real_key = "note"
        new_val = text

    context.user_data["edit_real_key"] = real_key
    context.user_data["edit_new_val"] = new_val

    kb = [["✅ Подтвердить", "❌ Отмена"]]
    await update.message.reply_text(
        f"Изменить «{field_label}» на «{new_val}»?",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
    )
    return EDIT_CONFIRM

async def edit_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Отмена" in text:
        await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    if "Подтвердить" not in text:
        kb = [["✅ Подтвердить", "❌ Отмена"]]
        await update.message.reply_text(
            "Нажми кнопку:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
        )
        return EDIT_CONFIRM

    real_key = context.user_data.get("edit_real_key")
    new_val  = context.user_data.get("edit_new_val")

    ok, msg = update_last_bot_row(real_key, new_val)
    if ok:
        await update.message.reply_text(f"✅ {msg}", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text(f"❌ {msg}", reply_markup=MAIN_MENU)

    context.user_data.clear()
    return ConversationHandler.END

# ─────────────────────────────────────────────
#  Диалог — новая запись
# ─────────────────────────────────────────────

async def step_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return ConversationHandler.END
    text = update.message.text
    if "Приход" in text:
        context.user_data["type"] = "inflow"
    elif "Расход" in text:
        context.user_data["type"] = "outflow"
    elif "Баланс" in text:
        await cmd_balance(update, context)
        return STEP_TYPE
    elif "История" in text:
        await cmd_history(update, context)
        return STEP_TYPE
    elif "Редактировать" in text:
        # запускаем edit flow
        result = await cmd_edit(update, context)
        return ConversationHandler.END
    else:
        await update.message.reply_text("Выбери действие:", reply_markup=MAIN_MENU)
        return STEP_TYPE

    kb = [[k] for k in KASSAS]
    await update.message.reply_text(
        "🏦 Выбери кассу:",
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
    await update.message.reply_text("💵 Сумма UZS (или 0):", reply_markup=ReplyKeyboardRemove())
    return STEP_UZS

async def step_uzs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(" ", "").replace(",", ".")
    try:
        val = float(text)
        context.user_data["uzs"] = val if val > 0 else None
    except ValueError:
        await update.message.reply_text("Введи число, например: 500000 или 0")
        return STEP_UZS
    await update.message.reply_text("💲 Сумма USD (или 0):")
    return STEP_USD

async def step_usd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(" ", "").replace(",", ".")
    try:
        val = float(text)
        context.user_data["usd"] = val if val > 0 else None
    except ValueError:
        await update.message.reply_text("Введи число, например: 100 или 0")
        return STEP_USD
    await update.message.reply_text("📝 Назначение / комментарий:")
    return STEP_NOTE

async def step_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["note"] = update.message.text.strip()
    context.user_data["date"] = datetime.today()
    kb = [["✅ Подтвердить", "❌ Отмена"]]
    await update.message.reply_text(
        f"Проверь данные:\n\n{summary(context.user_data)}",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
    )
    return STEP_CONFIRM

async def step_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Отмена" in text:
        context.user_data.clear()
        await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    if "Подтвердить" not in text:
        kb = [["✅ Подтвердить", "❌ Отмена"]]
        await update.message.reply_text(
            "Нажми кнопку:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
        )
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
        await update.message.reply_text(f"✅ Сохранено! {msg}", reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text(f"❌ {msg}", reply_markup=MAIN_MENU)
    context.user_data.clear()
    return STEP_TYPE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
    return ConversationHandler.END

# ─────────────────────────────────────────────
#  Ежедневный отчёт
# ─────────────────────────────────────────────

async def daily_report(context: ContextTypes.DEFAULT_TYPE):
    if not ALLOWED_IDS:
        return
    items, err = read_balance()
    if err or not items:
        return
    text = "🌅 Доброе утро!\n\n" + balance_text(items)
    for uid in ALLOWED_IDS:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception as e:
            logger.error(f"Daily report error for {uid}: {e}")

# ─────────────────────────────────────────────
#  Запуск
# ─────────────────────────────────────────────

def main():
    start_health_server()

    app = Application.builder().token(BOT_TOKEN).build()

    # Основной диалог (включает кнопки меню)
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_start),
        ],
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

    # Диалог редактирования
    edit_conv = ConversationHandler(
        entry_points=[CommandHandler("edit", cmd_edit)],
        states={
            EDIT_FIELD:   [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_choose_field)],
            EDIT_VALUE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_new_value)],
            EDIT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(edit_conv)
    app.add_handler(conv)
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("history", cmd_history))

    # Ежедневный отчёт
    app.job_queue.run_daily(
        daily_report,
        time=time(hour=DAILY_REPORT_HOUR, minute=DAILY_REPORT_MINUTE),
    )

    print("=" * 50)
    print("Cashflow Bot запущен!")
    print(f"Ежедневный отчёт: {DAILY_REPORT_HOUR:02d}:{DAILY_REPORT_MINUTE:02d} UTC")
    print(f"Разрешённые ID: {ALLOWED_IDS or 'все'}")
    print("=" * 50)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
