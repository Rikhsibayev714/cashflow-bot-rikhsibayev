"""
Cashflow Telegram Bot — полная версия
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
)

# ─────────────────────────────────────────────
#  НАСТРОЙКИ
# ─────────────────────────────────────────────
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "")
PORT           = int(os.environ.get("PORT", "8080"))
RENDER_URL     = os.environ.get("RENDER_URL", "")

ALLOWED_IDS_STR = os.environ.get("ALLOWED_IDS", "")
ALLOWED_IDS = [int(x.strip()) for x in ALLOWED_IDS_STR.split(",") if x.strip()] if ALLOWED_IDS_STR else []

MORNING_HOUR = int(os.environ.get("MORNING_HOUR", "4"))
EVENING_HOUR = int(os.environ.get("EVENING_HOUR", "13"))

KASSAS = ["Импорт Савдо", "Касса Ахрор", "Пластик карта", "Торговый 02", "Ислом ака карта", "Даврон ака карта"]

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

STEP_TYPE, STEP_KASSA, STEP_UZS, STEP_USD, STEP_NOTE, STEP_CONFIRM, STEP_INCOME_TYPE = range(7)
EDIT_FIELD, EDIT_VALUE, EDIT_CONFIRM = range(10, 13)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

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

async def ping_self(context):
    if not RENDER_URL:
        return
    try:
        import urllib.request
        urllib.request.urlopen(RENDER_URL, timeout=10)
        logger.info("Self-ping OK")
    except Exception as e:
        logger.warning(f"Self-ping failed: {e}")

def is_allowed(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True
    return update.effective_user.id in ALLOWED_IDS

async def check_access(update: Update) -> bool:
    if not is_allowed(update):
        uid = update.effective_user.id
        await update.message.reply_text(f"⛔ Нет доступа.\nТвой ID: {uid}")
        return False
    return True

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
            (f"K{row}", data.get("income_type") or ""),
            (f"T{row}", "Telegram Bot"),
        ]
        for cell, val in updates:
            ws_cash.update(cell, [[val]])
        return True, f"Записано в строку {row}"
    except Exception as e:
        logger.error(f"Write error: {e}")
        return False, f"Ошибка записи: {e}"

def update_last_bot_row(field: str, value):
    try:
        ws_cash, _ = get_sheets()
        row = find_last_bot_row(ws_cash)
        if row is None:
            return False, "Нет записей от бота."
        col_map = {"kassa": 2, "in_uzs": 3, "in_usd": 4, "note": 5, "out_uzs": 6, "out_usd": 7}
        col = col_map.get(field)
        if not col:
            return False, "Неизвестное поле."
        ws_cash.update(f"{chr(64+col)}{row}", [[value]])
        return True, f"Строка {row} обновлена."
    except Exception as e:
        return False, f"Ошибка: {e}"

def get_last_bot_entry():
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
        logger.error(f"get_last_bot_entry: {e}")
        return None

def read_all_rows():
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
        return data_rows
    except Exception as e:
        return f"Ошибка чтения: {e}"

def read_last_rows(n: int = 5):
    rows = read_all_rows()
    if isinstance(rows, str):
        return rows
    return rows[-n:] if len(rows) >= n else rows

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
                    s = str(v).strip().replace("\xa0", "").replace(" ", "")
                    if s.count(",") == 1 and s.count(".") >= 1:
                        s = s.replace(".", "").replace(",", ".")
                    elif s.count(",") == 1:
                        s = s.replace(",", ".")
                    return float(s)
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

def get_today_summary():
    rows = read_all_rows()
    if isinstance(rows, str):
        return None, rows
    today = datetime.today().strftime("%d.%m.%Y")
    today_rows = [r for r in rows if r.get("date", "") == today]

    def to_f(v):
        try:
            return float(str(v).replace(" ", "").replace(",", "."))
        except Exception:
            return 0.0

    in_uzs  = sum(to_f(r["in_uzs"])  for r in today_rows)
    in_usd  = sum(to_f(r["in_usd"])  for r in today_rows)
    out_uzs = sum(to_f(r["out_uzs"]) for r in today_rows)
    out_usd = sum(to_f(r["out_usd"]) for r in today_rows)
    count   = len(today_rows)

    return {
        "date":    today,
        "count":   count,
        "in_uzs":  in_uzs,
        "in_usd":  in_usd,
        "out_uzs": out_uzs,
        "out_usd": out_usd,
        "net_uzs": in_uzs - out_uzs,
        "net_usd": in_usd - out_usd,
    }, None

def parse_quick_input(text: str):
    text = text.strip()
    lower = text.lower()
    if lower.startswith("приход") or lower.startswith("прих"):
        op_type = "inflow"
        rest = text[6:].strip()
    elif lower.startswith("расход") or lower.startswith("расх"):
        op_type = "outflow"
        rest = text[6:].strip()
    else:
        return None
    parts = rest.split()
    if not parts:
        return None
    try:
        amount = float(parts[0].replace(",", "."))
    except ValueError:
        return None
    rest2 = " ".join(parts[1:])
    is_usd = False
    if rest2.lower().startswith("usd") or rest2.lower().startswith("$"):
        is_usd = True
        rest2 = rest2[3:].strip() if rest2.lower().startswith("usd") else rest2[1:].strip()
    kassa_found = None
    note = rest2
    for k in KASSAS:
        if k.lower() in rest2.lower():
            kassa_found = k
            note = rest2.lower().replace(k.lower(), "").strip()
            break
    return {
        "type":    op_type,
        "amount":  amount,
        "is_usd":  is_usd,
        "kassa":   kassa_found,
        "note":    note or "—",
    }

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
        f"📝 Назначение: {ud.get('note', '—')}\n"
        f"🏷 Тип: {ud.get('income_type') or '—'}"
    )

def row_to_text(r: dict, idx=None) -> str:
    prefix = f"{idx}. " if idx else ""
    direction = "📥" if (r.get("in_uzs") or r.get("in_usd")) else "📤"
    bot_mark  = " [бот]" if r.get("is_bot") else ""
    uzs = fmt(r.get("in_uzs") or r.get("out_uzs"))
    usd = fmt(r.get("in_usd") or r.get("out_usd"))
    return (
        f"{prefix}{direction}{bot_mark} {r.get('date') or '—'}\n"
        f"  🏦 {r.get('kassa') or '—'}\n"
        f"  💵 {uzs}   💲 {usd}\n"
        f"  📝 {r.get('note') or '—'}"
    )

def balance_text(items: list, title="💰 БАЛАНС") -> str:
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    nonzero = [i for i in items if i["uzs"] != 0 or i["usd"] != 0]
    zero    = [i for i in items if i["uzs"] == 0 and i["usd"] == 0]
    lines = [f"{title} на {now}\n"]
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

MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["➕ Приход", "➖ Расход"],
        ["💰 Баланс", "📋 История"],
        ["✏️ Редактировать", "📊 Отчёт за день"],
    ],
    resize_keyboard=True,
    one_time_keyboard=False,
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return ConversationHandler.END
    await update.message.reply_text(
        "👋 Cashflow Bot\n\nВыбери действие или напиши быстро:\n"
        "приход 500000 Импорт Савдо зарплата",
        reply_markup=MAIN_MENU,
    )
    return STEP_TYPE

async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    await update.message.reply_text("Загружаю...")
    items, err = read_balance()
    if err:
        await update.message.reply_text(err)
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

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    await update.message.reply_text("Загружаю...")
    data, err = get_today_summary()
    if err:
        await update.message.reply_text(err)
        return
    if data["count"] == 0:
        await update.message.reply_text(
            f"📊 Сегодня ({data['date']}) записей нет.",
            reply_markup=MAIN_MENU
        )
        return
    text = (
        f"📊 ОТЧЁТ ЗА {data['date']}\n"
        f"Записей: {data['count']}\n\n"
        f"📥 Приход:\n"
        f"  UZS: {fmt(data['in_uzs'])}\n"
        f"  USD: {fmt(data['in_usd'])}\n\n"
        f"📤 Расход:\n"
        f"  UZS: {fmt(data['out_uzs'])}\n"
        f"  USD: {fmt(data['out_usd'])}\n\n"
        f"📈 Итого за день:\n"
        f"  UZS: {fmt(data['net_uzs'])}\n"
        f"  USD: {fmt(data['net_usd'])}"
    )
    await update.message.reply_text(text, reply_markup=MAIN_MENU)

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text(
            "Использование: /search слово\nПример: /search зарплата",
            reply_markup=MAIN_MENU
        )
        return
    await update.message.reply_text(f"Ищу «{query}»...")
    rows = read_all_rows()
    if isinstance(rows, str):
        await update.message.reply_text(rows)
        return
    found = [r for r in rows if query.lower() in str(r.get("note", "")).lower()
             or query.lower() in str(r.get("kassa", "")).lower()]
    if not found:
        await update.message.reply_text(f"По запросу «{query}» ничего не найдено.", reply_markup=MAIN_MENU)
        return
    lines = [f"🔍 Найдено {len(found)} записей по «{query}»:\n"]
    for i, r in enumerate(found[-10:], 1):
        lines.append(row_to_text(r, i))
        lines.append("")
    await update.message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)

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
        await update.message.reply_text("Нет записей от бота.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    context.user_data["edit_entry"] = entry
    direction = "📥 ПРИХОД" if (entry.get("in_uzs") or entry.get("in_usd")) else "📤 РАСХОД"
    uzs = fmt(entry.get("in_uzs") or entry.get("out_uzs"))
    usd = fmt(entry.get("in_usd") or entry.get("out_usd"))
    text = (
        f"Последняя запись (строка {entry['row']}):\n\n"
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
        await update.message.reply_text("Выбери поле:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True))
        return EDIT_FIELD
    context.user_data["edit_field"] = text
    context.user_data["edit_field_key"] = EDIT_FIELD_LABELS[text]
    if text == "Касса":
        kb = [[k] for k in KASSAS] + [["❌ Отмена"]]
        await update.message.reply_text("Выбери кассу:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True))
    else:
        await update.message.reply_text(f"Введи новое значение для «{text}»:",
            reply_markup=ReplyKeyboardRemove())
    return EDIT_VALUE

async def edit_new_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "❌ Отмена":
        await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    field_key   = context.user_data.get("edit_field_key")
    field_label = context.user_data.get("edit_field")
    entry       = context.user_data.get("edit_entry", {})
    has_inflow  = bool(entry.get("in_uzs") or entry.get("in_usd"))
    if field_key == "kassa":
        if text not in KASSAS:
            kb = [[k] for k in KASSAS] + [["❌ Отмена"]]
            await update.message.reply_text("Выбери из списка:",
                reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True))
            return EDIT_VALUE
        real_key = "kassa"
        new_val  = text
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
        new_val  = text
    context.user_data["edit_real_key"] = real_key
    context.user_data["edit_new_val"]  = new_val
    kb = [["✅ Подтвердить", "❌ Отмена"]]
    await update.message.reply_text(f"Изменить «{field_label}» на «{new_val}»?",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True))
    return EDIT_CONFIRM

async def edit_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "Отмена" in text:
        await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    if "Подтвердить" not in text:
        kb = [["✅ Подтвердить", "❌ Отмена"]]
        await update.message.reply_text("Нажми кнопку:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True))
        return EDIT_CONFIRM
    ok, msg = update_last_bot_row(
        context.user_data.get("edit_real_key"),
        context.user_data.get("edit_new_val")
    )
    await update.message.reply_text(
        f"✅ {msg}" if ok else f"❌ {msg}",
        reply_markup=MAIN_MENU
    )
    context.user_data.clear()
    return ConversationHandler.END

async def delete_dialog_messages(context, chat_id):
    """Удаляет все сообщения диалога."""
    msg_ids = context.user_data.get("msg_ids", [])
    for msg_id in msg_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
    context.user_data["msg_ids"] = []


def track_msg(context, message):
    """Добавляет ID сообщения в список для удаления."""
    if "msg_ids" not in context.user_data:
        context.user_data["msg_ids"] = []
    if message and hasattr(message, "message_id"):
        context.user_data["msg_ids"].append(message.message_id)


async def reply_and_track(update, context, text, **kwargs):
    """Отправляет сообщение и трекает его ID."""
    msg = await update.message.reply_text(text, **kwargs)
    track_msg(context, msg)
    return msg


async def step_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update):
        return ConversationHandler.END
    track_msg(context, update.message)
    text = update.message.text.strip()
    if "Баланс" in text:
        await cmd_balance(update, context)
        return STEP_TYPE
    if "История" in text:
        await cmd_history(update, context)
        return STEP_TYPE
    if "Отчёт" in text:
        await cmd_today(update, context)
        return STEP_TYPE
    if "Редактировать" in text:
        await cmd_edit(update, context)
        return ConversationHandler.END
    parsed = parse_quick_input(text)
    if parsed:
        context.user_data["type"]  = parsed["type"]
        context.user_data["kassa"] = parsed["kassa"]
        context.user_data["note"]  = parsed["note"]
        context.user_data["date"]  = datetime.today()
        if parsed["is_usd"]:
            context.user_data["uzs"] = None
            context.user_data["usd"] = parsed["amount"]
        else:
            context.user_data["uzs"] = parsed["amount"]
            context.user_data["usd"] = None
        if parsed["kassa"]:
            kb = [["✅ Подтвердить", "❌ Отмена"]]
            await update.message.reply_text(
                f"Проверь данные:\n\n{summary(context.user_data)}",
                reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
            )
            return STEP_CONFIRM
        else:
            kb = [[k] for k in KASSAS]
            await update.message.reply_text(
                f"Касса не распознана. Выбери:\n\n{summary(context.user_data)}",
                reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
            )
            return STEP_KASSA
    if "Приход" in text:
        context.user_data["type"] = "inflow"
    elif "Расход" in text:
        context.user_data["type"] = "outflow"
    else:
        await update.message.reply_text(
            "Выбери действие или напиши быстро:\nприход 500000 Импорт Савдо зарплата",
            reply_markup=MAIN_MENU
        )
        return STEP_TYPE
    kb = [[k] for k in KASSAS]
    bot_msg = await update.message.reply_text("🏦 Выбери кассу:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True))
    track_msg(context, bot_msg)
    return STEP_KASSA

async def step_kassa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_msg(context, update.message)
    text = update.message.text.strip()
    if text not in KASSAS:
        kb = [[k] for k in KASSAS]
        await update.message.reply_text("Выбери из списка:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True))
        return STEP_KASSA
    context.user_data["kassa"] = text
    if "uzs" in context.user_data or "usd" in context.user_data:
        kb = [["✅ Подтвердить", "❌ Отмена"]]
        await update.message.reply_text(
            f"Проверь данные:\n\n{summary(context.user_data)}",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
        )
        return STEP_CONFIRM
    bot_msg = await update.message.reply_text("💵 Сумма UZS (или 0):", reply_markup=ReplyKeyboardRemove())
    track_msg(context, bot_msg)
    return STEP_UZS

async def step_uzs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_msg(context, update.message)
    text = update.message.text.strip().replace(" ", "").replace(",", ".")
    try:
        val = float(text)
        context.user_data["uzs"] = val if val > 0 else None
    except ValueError:
        await update.message.reply_text("Введи число, например: 500000 или 0")
        return STEP_UZS
    bot_msg = await update.message.reply_text("💲 Сумма USD (или 0):")
    track_msg(context, bot_msg)
    return STEP_USD

async def step_usd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_msg(context, update.message)
    text = update.message.text.strip().replace(" ", "").replace(",", ".")
    try:
        val = float(text)
        context.user_data["usd"] = val if val > 0 else None
    except ValueError:
        await update.message.reply_text("Введи число, например: 100 или 0")
        return STEP_USD
    bot_msg = await update.message.reply_text("📝 Назначение / комментарий:")
    track_msg(context, bot_msg)
    return STEP_NOTE

async def step_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_msg(context, update.message)
    context.user_data["note"] = update.message.text.strip()
    context.user_data["date"] = datetime.today()
    if context.user_data.get("type") == "inflow":
        kb = [["👤 Клиент", "🔄 Другое"]]
        bot_msg = await update.message.reply_text(
            "Тип прихода:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
        )
        track_msg(context, bot_msg)
        return STEP_INCOME_TYPE
    context.user_data["income_type"] = None
    kb = [["✅ Подтвердить", "❌ Отмена"]]
    bot_msg = await update.message.reply_text(
        f"Проверь данные:\n\n{summary(context.user_data)}",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
    )
    track_msg(context, bot_msg)
    return STEP_CONFIRM

async def step_income_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_msg(context, update.message)
    text = update.message.text.strip()
    if "Клиент" in text:
        context.user_data["income_type"] = "Клиент"
    elif "Другое" in text:
        context.user_data["income_type"] = "Другое"
    else:
        kb = [["👤 Клиент", "🔄 Другое"]]
        await update.message.reply_text("Выбери тип:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True))
        return STEP_INCOME_TYPE
    kb = [["✅ Подтвердить", "❌ Отмена"]]
    bot_msg = await update.message.reply_text(
        f"Проверь данные:\n\n{summary(context.user_data)}",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True),
    )
    track_msg(context, bot_msg)
    return STEP_CONFIRM

async def step_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_msg(context, update.message)
    text = update.message.text
    if "Отмена" in text:
        chat_id = update.message.chat_id
        await delete_dialog_messages(context, chat_id)
        context.user_data.clear()
        await context.bot.send_message(chat_id=chat_id, text="Отменено.", reply_markup=MAIN_MENU)
        return STEP_TYPE
    if "Подтвердить" not in text:
        kb = [["✅ Подтвердить", "❌ Отмена"]]
        bot_msg = await update.message.reply_text("Нажми кнопку:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True))
        track_msg(context, bot_msg)
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
        "income_type": ud.get("income_type"),
    }
    chat_id = update.message.chat_id
    saving_msg = await update.message.reply_text("Сохраняю...", reply_markup=ReplyKeyboardRemove())
    track_msg(context, saving_msg)
    ok, msg = write_transaction(data)

    # Удаляем весь диалог
    await delete_dialog_messages(context, chat_id)

    # Показываем краткий итог
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ Сохранено! {msg}" if ok else f"❌ {msg}",
        reply_markup=MAIN_MENU
    )
    context.user_data.clear()
    return STEP_TYPE

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменено.", reply_markup=MAIN_MENU)
    return STEP_TYPE

async def morning_report(context):
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
            logger.error(f"Morning report error {uid}: {e}")

async def evening_report(context):
    if not ALLOWED_IDS:
        return
    data, err = get_today_summary()
    if err or not data:
        return
    if data["count"] == 0:
        text = f"🌆 Итог дня {data['date']}: записей нет."
    else:
        text = (
            f"🌆 ИТОГ ДНЯ {data['date']}\n"
            f"Записей: {data['count']}\n\n"
            f"📥 Приход:\n"
            f"  UZS: {fmt(data['in_uzs'])}\n"
            f"  USD: {fmt(data['in_usd'])}\n\n"
            f"📤 Расход:\n"
            f"  UZS: {fmt(data['out_uzs'])}\n"
            f"  USD: {fmt(data['out_usd'])}\n\n"
            f"📈 Нетто:\n"
            f"  UZS: {fmt(data['net_uzs'])}\n"
            f"  USD: {fmt(data['net_usd'])}"
        )
    for uid in ALLOWED_IDS:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception as e:
            logger.error(f"Evening report error {uid}: {e}")

def main():
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).build()

    edit_conv = ConversationHandler(
        entry_points=[CommandHandler("edit", cmd_edit)],
        states={
            EDIT_FIELD:   [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_choose_field)],
            EDIT_VALUE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_new_value)],
            EDIT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_start),
        ],
        states={
            STEP_TYPE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, step_type)],
            STEP_KASSA:       [MessageHandler(filters.TEXT & ~filters.COMMAND, step_kassa)],
            STEP_UZS:         [MessageHandler(filters.TEXT & ~filters.COMMAND, step_uzs)],
            STEP_USD:         [MessageHandler(filters.TEXT & ~filters.COMMAND, step_usd)],
            STEP_NOTE:        [MessageHandler(filters.TEXT & ~filters.COMMAND, step_note)],
            STEP_CONFIRM:     [MessageHandler(filters.TEXT & ~filters.COMMAND, step_confirm)],
            STEP_INCOME_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_income_type)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(edit_conv)
    app.add_handler(conv)
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("today",   cmd_today))
    app.add_handler(CommandHandler("search",  cmd_search))

    jq = app.job_queue
    jq.run_daily(morning_report, time=time(hour=MORNING_HOUR, minute=0))
    jq.run_daily(evening_report, time=time(hour=EVENING_HOUR, minute=0))
    jq.run_repeating(ping_self, interval=600, first=60)

    print("=" * 50)
    print("Cashflow Bot запущен!")
    print(f"Утренний отчёт: {MORNING_HOUR:02d}:00 UTC = {MORNING_HOUR+5:02d}:00 Ташкент")
    print(f"Вечерний отчёт: {EVENING_HOUR:02d}:00 UTC = {EVENING_HOUR+5:02d}:00 Ташкент")
    print(f"Разрешённые ID: {ALLOWED_IDS or 'все'}")
    print("=" * 50)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
