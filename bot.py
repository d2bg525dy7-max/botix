"""
Nexus — Telegram-бот поддержки (aiogram 3 + sqlite).

Переменные окружения (задайте в панели bothost):
  BOT_TOKEN       — токен от @BotFather (или в файле .env рядом с bot.py)
  DEV_IDS         — Telegram ID владельца/разработчиков через запятую (полный доступ)
  ADMIN_CHAT_ID   — ID беседы администрации (например -1001234567890)
  STAFF_CHAT_ID   — ID беседы тех. админов
  LOG_CHAT_ID     — ID беседы логов (сюда дублируются все действия)
  MAIN_CHAT_ID    — ID беседы главной администрации (в ней работают все команды)
  EDU_CHAT_ID     — ID беседы отдела обучения (сюда приходят итоги дня)
  BACKUP_CHAT_ID  — куда слать ежедневную копию базы (по умолчанию — в ЛС вписанным в код)
  DB_PATH         — путь к базе (по умолчанию nexus.db)
"""
import asyncio
import hashlib
import html
import logging
import os
import random
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ───────────────────────── НАСТРОЙКИ ─────────────────────────


def _load_env(path=".env"):
    """Мини-загрузчик .env (без зависимостей): строки вида KEY=value. Уже заданные переменные не трогает."""
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_env()
_load_env("1.env")  # то же самое, если файл назван 1.env
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
# Люди, вписанные в код: максимальный доступ, у них ничего нельзя забрать. ID берутся из DEV_IDS.
DEV_IDS = set()
DEV_IDS |= {int(x) for x in os.getenv("DEV_IDS", "").split(",") if x.strip().lstrip("-").isdigit()}
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or 0)  # беседа администрации
STAFF_CHAT_ID = int(os.getenv("STAFF_CHAT_ID", "0") or 0)  # беседа тех. админов
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "0") or 0)  # беседа логов
MAIN_CHAT_ID = int(os.getenv("MAIN_CHAT_ID", "0") or 0)  # беседа главной администрации
EDU_CHAT_ID = int(os.getenv("EDU_CHAT_ID", "0") or 0)  # беседа отдела обучения
BACKUP_CHAT_ID = int(os.getenv("BACKUP_CHAT_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "nexus.db")
TZ = timezone(timedelta(hours=3))  # МСК
PRED_COST = 10  # цена снятия 1 предупреждения за баллы (меняется командой /setcost)
VIG_COST = 30  # цена снятия 1 выговора за баллы (меняется командой /setcost)
FIVE_STAR_POINTS = 10  # баллов администратору за оценку 5⭐
REMIND_MIN = 10  # через сколько минут молчания напомнить администратору
REQUEUE_MIN = 20  # через сколько минут молчания вернуть диалог в очередь
LOW_ACTIVITY_ANSWERS = 3  # меньше стольки ответов за сутки — «низкая активность»
SUMMARY_HOUR = 22  # во сколько (МСК) слать итоги дня в беседу обучения
BACKUP_HOUR = 4  # во сколько (МСК) делать копию базы

DEPTS = {"none": "Без отдела", "tech": "Техническая администрация", "edu": "Отдел обучения",
         "main": "Главная администрация"}
DEPT_ALIASES = {
    "tech": "tech", "tex": "tech", "тех": "tech", "техадм": "tech", "техническая": "tech",
    "main": "main", "глав": "main", "главная": "main", "главадм": "main",
    "edu": "edu", "обучение": "edu", "обучения": "edu",
    "none": "none", "нет": "none", "-": "none",
}
PERMS = {
    "makeadmin": "Выдача админки",
    "uplvl": "Повышение уровня",
    "kick": "Снятие админа",
    "vig": "Выговоры",
    "pred": "Предупреждения",
    "setballs": "Баллы",
    "setdos": "Награды и достижения",
    "setdj": "Должности",
    "setot": "Отделы",
    "find": "Досье (/find)",
    "getday": "Ответы за день/неделю",
    "checkot": "Проверка ответа",
    "logs": "Логи файлом",
    "purs": "Выдача доступов (/purs)",
    "temes": "Шаблоны (/addtem, /deltem)",
}
WHERE_TXT = {
    "any": "ЛС бота и беседы: администрации, тех. админов, обучения, главной администрации",
    "staff_or_dev_dm": "беседа тех. админов или главной администрации (в ЛС — только вписанные в код)",
    "log_or_dev_dm": "беседа логов или главной администрации (в ЛС — только вписанные в код)",
    "dm": "только ЛС бота",
    "dev_dm": "ЛС бота или беседа главной администрации, только вписанные в код",
    "dev_priv": "только ЛС бота, только вписанные в код",
}
# (команда, аргументы, нужный доступ, где, описание)
CMDS = [
    ("/me", "", "admin", "any", "ваш профиль"),
    ("/dj", "", "admin", "any", "список должностей"),
    ("/week", "", "admin", "any", "статистика ответов за неделю"),
    ("/queue", "", "admin", "any", "очередь обращений (выбор категории)"),
    ("/onduty", "", "admin", "any", "кто на дежурстве и кто давно выключил /duty"),
    ("/top", "", "admin", "any", "топ администраторов за неделю/месяц"),
    ("/warns", "[@юз]", "admin", "any", "наказания: кто выдал и за что (чужие — с доступом vig/pred/find)"),
    ("/temes", "", "admin", "any", "актуальные шаблоны ответов"),
    ("/changepsevd", "псевдоним", "admin", "any", "сменить свой псевдоним (его видят пользователи вместо тега)"),
    ("/transfer", "номер @админ|tech", "admin", "any", "передать свой диалог админу или в тех. поддержку"),
    ("/duty", "", "admin", "any", "вкл/выкл приём обращений"),
    ("/start", "", "all", "dm", "начать и обратиться к администратору"),
    ("/end", "", "all", "dm", "завершить или отменить обращение"),
    ("/buypred", "", "admin", "any", "снять своё предупреждение за баллы (цену задаёт /setcost)"),
    ("/buyvig", "", "admin", "any", "снять свой выговор за баллы (цену задаёт /setcost)"),
    ("/vig", "+1/-1 @юз причина", "vig", "any", "выдать/снять выговор"),
    ("/pred", "+1/-1 @юз причина", "pred", "any", "выдать/снять предупреждение"),
    ("/setballs", "+N/-N @юз причина", "setballs", "any", "изменить баллы"),
    ("/setdos", "@юз текст", "setdos", "any", "выдать достижение"),
    ("/setaward", "@юз текст", "setdos", "any", "выдать награду"),
    ("/deldos", "@юз [номер]", "setdos", "any", "список достижений / удалить по номеру"),
    ("/delaward", "@юз [номер]", "setdos", "any", "список наград / удалить по номеру"),
    ("/addtem", "название | текст", "temes", "any", "добавить шаблон"),
    ("/deltem", "номер", "temes", "any", "удалить шаблон"),
    ("/makeadmin", "@юз уровень(0-7) [отдел]", "makeadmin", "staff_or_dev_dm", "выдать/снять админку"),
    ("/uplvl", "@юз", "uplvl", "staff_or_dev_dm", "повысить на 1 уровень"),
    ("/kick", "@юз", "kick", "staff_or_dev_dm", "полностью снять с админки"),
    ("/setdj", "@юз должность", "setdj", "staff_or_dev_dm", "изменить должность"),
    ("/setage", "@юз возраст", "makeadmin", "staff_or_dev_dm", "указать возраст"),
    ("/setot", "@юз отдел", "setot", "any", "изменить отдел (tech/edu/none)"),
    ("/find", "@юз", "find", "staff_or_dev_dm", "досье на администратора"),
    ("/getday", "@юз [week]", "getday", "any", "ID ответов за день/неделю"),
    ("/checkot", "id", "checkot", "any", "посмотреть ответ и контекст"),
    ("/purs", "@юз", "purs", "any", "меню доступов"),
    ("/logs", "@юз", "logs", "log_or_dev_dm", "все логи человека файлом .txt"),
    ("/fullban", "@юз причина", "dev", "dev_dm", "полный ЧС в боте"),
    ("/unfullban", "@юз", "dev", "dev_dm", "снять полный ЧС"),
    ("/setrate", "@юз 4.8", "dev", "dev_dm", "изменить рейтинг"),
    ("/botstats", "", "dev", "dev_dm", "статистика бота"),
    ("/setlogs", "", "dev", "dev_dm", "изменение данных бота и админов"),
    ("/people", "[@юз]", "dev", "dev_priv", "зарегистрированные в боте пользователи"),
    ("/broadcast", "[users|admins] текст", "dev", "dev_priv", "рассылка всем (предпросмотр и подтверждение)"),
    ("/backup", "", "dev", "dev_dm", "резервная копия базы прямо сейчас"),
    ("/setcost", "pred|vig число", "dev", "dev_dm", "цена снятия предупреждения/выговора за баллы"),
    ("/settext", "start|general|tech|maint [текст]", "dev", "dev_dm", "тексты бота: после /start, при выборе админа/тех. поддержки, при тех. работах"),
    ("/texon", "", "dev", "dev_dm", "включить режим тех. работ (люди не могут писать боту)"),
    ("/texoff", "", "dev", "dev_dm", "выключить режим тех. работ"),
]

DEFAULT_GREETING = (
    "Приветствую, {name}! 👋\n\n"
    "Добро пожаловать в бота поддержки <b>Nexus</b>.\n\n"
    "Нажмите кнопку ниже, чтобы перейти к администратору."
)

DEFAULT_WAIT = "⏳ Обращение создано. Напишите, что вас беспокоит, — администратор подключится и увидит ваши сообщения."
DEFAULT_MAINT = (
    "🛠 В боте проводятся технические работы. Приносим искренние извинения за неудобства — "
    "скоро всё заработает. Пожалуйста, загляните позже. 💙"
)
PSEUDOS = ["Котик", "Лисёнок", "Ёжик", "Пингвин", "Совёнок", "Енот", "Зайчик", "Медвежонок", "Тигрёнок",
           "Белка", "Панда", "Дельфин", "Хомяк", "Утёнок", "Бобёр", "Барсук", "Олень", "Выдра", "Кролик", "Ласточка"]
_maint_seen = {}

# ───────────────────────── БАЗА ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, name TEXT,
  first_seen INT, last_seen INT, banned INT DEFAULT 0, ban_reason TEXT, pending_review INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, level INT DEFAULT 1,
  position TEXT DEFAULT 'Администратор', dept TEXT DEFAULT 'none', points INT DEFAULT 0,
  warns INT DEFAULT 0, vigs INT DEFAULT 0, rating REAL DEFAULT 0, rating_cnt INT DEFAULT 0,
  age INT, duty INT DEFAULT 1, current_dialog INT DEFAULT 0, since INT);
CREATE TABLE IF NOT EXISTS perms(user_id INT, perm TEXT, PRIMARY KEY(user_id, perm));
CREATE TABLE IF NOT EXISTS awards(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT, kind TEXT,
  title TEXT, by_id INT, ts INT);
CREATE TABLE IF NOT EXISTS punishments(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT, kind TEXT,
  delta INT, reason TEXT, by_id INT, ts INT);
CREATE TABLE IF NOT EXISTS dialogs(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT, admin_id INT,
  dept TEXT, status TEXT, created INT, accepted INT, closed INT);
CREATE TABLE IF NOT EXISTS msgs(id INTEGER PRIMARY KEY AUTOINCREMENT, dialog_id INT, role TEXT,
  sender INT, ctype TEXT, text TEXT, tg_mid INT, ts INT);
CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY AUTOINCREMENT, dialog_id INT UNIQUE,
  admin_id INT, user_id INT, stars INT, text TEXT, ts INT);
CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT, actor INT, target INT,
  action TEXT, details TEXT, ts INT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS complaints(id INTEGER PRIMARY KEY AUTOINCREMENT, dialog_id INT, admin_id INT,
  user_id INT, text TEXT, ts INT);
CREATE TABLE IF NOT EXISTS templates(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, text TEXT,
  by_id INT, ts INT);
"""
)
db.commit()
for _sql in (  # миграции для уже созданной базы
    "ALTER TABLE admins ADD COLUMN duty_changed INT",
    "ALTER TABLE dialogs ADD COLUMN crisis INT DEFAULT 0",
    "ALTER TABLE dialogs ADD COLUMN remind_ref INT DEFAULT 0",
    "ALTER TABLE users ADD COLUMN nobc INT DEFAULT 0",
    "ALTER TABLE users ADD COLUMN started INT DEFAULT 0",
    "ALTER TABLE admins ADD COLUMN pseudo TEXT",
):
    try:
        db.execute(_sql)
        db.commit()
    except sqlite3.OperationalError:
        pass
START_TS = int(time.time())
DUTY_WARN_DAYS = 3  # через сколько дней выключенный /duty подсвечивается ⚠️
BOT: Bot = None  # type: ignore


def run(sql, args=()):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def get(sql, args=()):
    return db.execute(sql, args).fetchone()


def all_(sql, args=()):
    return db.execute(sql, args).fetchall()


# ───────────────────────── ХЕЛПЕРЫ ─────────────────────────
def now():
    return int(time.time())


def esc(s):
    return html.escape(str(s if s is not None else ""))


def fdt(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M") if ts else "—"


def is_dev(uid):
    return uid in DEV_IDS


def get_admin(uid):
    return get("SELECT * FROM admins WHERE user_id=? AND level>=1", (uid,))


def is_admin(uid):
    return is_dev(uid) or get_admin(uid) is not None


def perms_of(uid):
    return {r["perm"] for r in all_("SELECT perm FROM perms WHERE user_id=?", (uid,))}


def has_perm(uid, perm):
    if perm == "all":
        return True
    if perm == "dev":
        return is_dev(uid)
    if is_dev(uid):
        return True
    if not get_admin(uid):
        return False
    return perm == "admin" or perm in perms_of(uid)


def norm_dept(s):
    return DEPT_ALIASES.get((s or "").lower())


def tag(uid):
    u = get("SELECT username, name FROM users WHERE user_id=?", (uid,))
    if not u:
        return f"<code>{uid}</code>"
    if u["username"]:
        return f"@{esc(u['username'])}"
    return f'<a href="tg://user?id={uid}">{esc(u["name"] or uid)}</a>'


def anon_id(uid):
    salt = setting("salt")
    if not salt:
        salt = os.urandom(8).hex()
        run("INSERT OR REPLACE INTO settings(key,value) VALUES('salt',?)", (salt,))
    h = int(hashlib.sha256(f"{salt}{uid}".encode()).hexdigest()[:8], 16) % 100000
    return f"Пользователь #{h:05d}"


def is_plain_user(uid):
    return not is_admin(uid) and get("SELECT 1 FROM dialogs WHERE user_id=?", (uid,)) is not None


def who(uid):
    """Тег для служебных сообщений: обычные пользователи скрыты за псевдонимом."""
    return anon_id(uid) if is_plain_user(uid) else tag(uid)


def ptag(uid):
    if is_plain_user(uid):
        return anon_id(uid)
    u = get("SELECT username, name FROM users WHERE user_id=?", (uid,))
    if u and u["username"]:
        return f"@{u['username']} ({uid})"
    return f"{(u['name'] if u else '') or ''} ({uid})"


def find_user(tok):
    tok = (tok or "").strip()
    if tok.startswith("@"):
        return get("SELECT * FROM users WHERE username=? ORDER BY last_seen DESC", (tok[1:].lower(),))
    if tok.lstrip("-").isdigit():
        return get("SELECT * FROM users WHERE user_id=?", (int(tok),))
    if tok:  # псевдоним администратора (может состоять из нескольких слов)
        tok = " ".join(tok.split())
        for a in all_("SELECT user_id, pseudo FROM admins WHERE pseudo IS NOT NULL"):
            if a["pseudo"].casefold() == tok.casefold():
                return get("SELECT * FROM users WHERE user_id=?", (a["user_id"],))
    return None


def setting(key, default=""):
    r = get("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


def pseudo_of(uid):
    """Псевдоним администратора (его видят пользователи вместо тега). Выдаётся автоматически."""
    a = get("SELECT pseudo FROM admins WHERE user_id=?", (uid,))
    if a and a["pseudo"]:
        return a["pseudo"]
    used = {x["pseudo"].casefold() for x in all_("SELECT pseudo FROM admins WHERE pseudo IS NOT NULL")}
    free = [p for p in PSEUDOS if p.casefold() not in used]
    name = random.choice(free) if free else f"{random.choice(PSEUDOS)}{random.randint(10, 999)}"
    while name.casefold() in used:
        name = f"{random.choice(PSEUDOS)}{random.randint(10, 999)}"
    if a:
        run("UPDATE admins SET pseudo=? WHERE user_id=?", (name, uid))
    return name


async def dm(uid, text, **kw):
    try:
        return await BOT.send_message(uid, text, **kw)
    except Exception as e:  # пользователь заблокировал бота и т.п.
        logging.warning("send to %s failed: %s", uid, e)
        return None


async def alog(actor, target, action, details=""):
    run("INSERT INTO logs(actor,target,action,details,ts) VALUES(?,?,?,?,?)",
        (actor, target, action, details, now()))
    if LOG_CHAT_ID:
        await dm(LOG_CHAT_ID,
                 f"📝 <b>{esc(action)}</b>\n👤 {who(actor)}" + (f" → {who(target)}" if target else "")
                 + (f"\n{esc(details[:500])}" if details else ""))


def chunks(text, limit=3800):
    """Режет длинный текст по строкам, чтобы не превысить лимит Telegram (4096) и не сломать HTML-теги."""
    out, cur = [], ""
    for line in str(text).split("\n"):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur = (cur + "\n" + line) if cur else line
    if cur:
        out.append(cur)
    return out or [""]


async def reply_long(m: Message, text, **kw):
    parts = chunks(text)
    for i, part in enumerate(parts):
        await m.answer(part, **(kw if i == len(parts) - 1 else {}))


def chat_kind(m: Message):
    if m.chat.type == "private":
        return "priv"
    for name, cid in (("admin", ADMIN_CHAT_ID), ("staff", STAFF_CHAT_ID), ("edu", EDU_CHAT_ID),
                      ("main", MAIN_CHAT_ID), ("log", LOG_CHAT_ID)):
        if cid and m.chat.id == cid:
            return name
    return None


async def guard(m: Message, perm, where="any"):
    """Проверка места и доступа. perm: None | 'all' | 'admin' | 'dev' | ключ из PERMS.
    В беседе главной администрации разрешены любые команды (доступы всё равно проверяются)."""
    uid = m.from_user.id
    k = chat_kind(m)
    priv = k == "priv"
    dev = is_dev(uid)
    ok = {
        "any": k in ("priv", "admin", "staff", "edu", "main"),
        "staff_or_dev_dm": k in ("staff", "main") or (priv and dev),
        "log_or_dev_dm": k in ("log", "main") or (priv and dev),
        "dm": priv,
        "dev_dm": (priv and dev) or k == "main",
        "dev_priv": priv and dev,
    }[where]
    if not ok:
        if k:
            await m.answer("🚫 Эта команда здесь недоступна. Подробнее: /help (в ЛС бота).")
        return False
    if perm and not has_perm(uid, perm):
        await m.answer("🚫 У вас нет доступа к этой команде.")
        return False
    return True


async def target_or_fail(m, tok, need_admin=False, destructive=False):
    t = find_user(tok)
    if not t:
        await m.answer("❌ Пользователь не найден. Он должен хотя бы раз написать боту.")
        return None
    uid = t["user_id"]
    if need_admin and not get_admin(uid) and not is_dev(uid):
        await m.answer("❌ Этот пользователь не администратор.")
        return None
    if destructive and is_dev(uid):
        await m.answer("🛡 Вписанных в код нельзя снять с администрации.")
        return None
    return t


def level_error(actor, target_uid, new_level=None):
    """Иерархия: нельзя выдать уровень выше своего и нельзя менять админа своего или более высокого уровня.
    Вписанные в код ограничений не имеют."""
    if is_dev(actor):
        return None
    me = get_admin(actor)
    mylvl = me["level"] if me else 0
    if new_level is not None and new_level > mylvl:
        return f"🚫 Нельзя выдать уровень выше своего (ваш уровень: {mylvl})."
    tgt = get_admin(target_uid)
    if tgt and (target_uid == actor or tgt["level"] >= mylvl):
        return "🚫 Нельзя менять администратора своего или более высокого уровня."
    return None


def parse_delta(tok):
    try:
        d = int(tok)
    except Exception:
        return None
    return d if d != 0 and abs(d) <= 3 else None


# ───────────────────────── MIDDLEWARE ─────────────────────────
class Track(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = getattr(event, "from_user", None)
        if u and not u.is_bot:
            run(
                "INSERT INTO users(user_id,username,name,first_seen,last_seen) VALUES(?,?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, name=excluded.name, "
                "last_seen=excluded.last_seen",
                (u.id, (u.username or "").lower() or None, u.full_name, now(), now()),
            )
            if isinstance(event, Message) and event.chat.type == "private":
                run("UPDATE users SET started=1 WHERE user_id=?", (u.id,))
            if is_dev(u.id):
                run("INSERT OR IGNORE INTO admins(user_id, level, position, since) VALUES(?,7,'Разработчик',?)",
                    (u.id, now()))
                run("UPDATE admins SET level=1 WHERE user_id=? AND level<1", (u.id,))
                pseudo_of(u.id)
            else:
                row = get("SELECT banned FROM users WHERE user_id=?", (u.id,))
                if row and row["banned"]:
                    if isinstance(event, CallbackQuery):
                        await event.answer()
                    return
                if setting("maint") == "1" and not get_admin(u.id):
                    mtext = setting("maint_text", DEFAULT_MAINT)
                    if isinstance(event, CallbackQuery):
                        await event.answer(mtext[:190], show_alert=True)
                    elif event.chat.type == "private" and now() - _maint_seen.get(u.id, 0) > 30:
                        _maint_seen[u.id] = now()
                        await event.answer(mtext)
                    return
        return await handler(event, data)


r = Router()


# ───────────────────────── ПОЛЬЗОВАТЕЛЬ: /start, обращения, оценка ─────────────────────────
def ask_kb():
    b = InlineKeyboardBuilder()
    b.button(text="💬 Перейти к администратору", callback_data="ask:general")
    b.button(text="🛠 Техническая поддержка", callback_data="ask:tech")
    b.adjust(1)
    return b.as_markup()


@r.message(CommandStart(), F.chat.type == "private")
async def cmd_start(m: Message):
    text = setting("greeting", DEFAULT_GREETING).replace("{name}", esc(m.from_user.first_name))
    await m.answer(text, reply_markup=ask_kb())


async def notify_admins(d, exclude=()):
    n = 0
    cat = "🛠 Тех. поддержка" if d["dept"] == "tech" else "💬 Общее обращение"
    head = "🚨 <b>СРОЧНО · кризисный сигнал</b>\n" if d["crisis"] else ""
    for a in all_("SELECT user_id, dept FROM admins WHERE duty=1 AND current_dialog=0 AND user_id!=?",
                  (d["user_id"],)):
        if a["user_id"] in exclude:
            continue
        if d["dept"] == "tech" and a["dept"] != "tech" and not is_dev(a["user_id"]):
            continue
        b = InlineKeyboardBuilder()
        b.button(text="✅ Принять", callback_data=f"acc:{d['id']}")
        if await dm(a["user_id"], f"{head}🔔 <b>Новое обращение #{d['id']}</b>\n{cat}\n👤 {anon_id(d['user_id'])}",
                    reply_markup=b.as_markup()):
            n += 1
    return n


@r.callback_query(F.data.startswith("ask:"))
async def cb_ask(c: CallbackQuery):
    uid = c.from_user.id
    dept = "tech" if c.data == "ask:tech" else "general"
    if get("SELECT 1 FROM dialogs WHERE user_id=? AND status IN ('waiting','active')", (uid,)):
        await c.answer("У вас уже есть открытое обращение.", show_alert=True)
        return
    cur = run("INSERT INTO dialogs(user_id, dept, status, created) VALUES(?,?, 'waiting', ?)", (uid, dept, now()))
    d = get("SELECT * FROM dialogs WHERE id=?", (cur.lastrowid,))
    n = await notify_admins(d)
    await c.answer()
    txt = setting("wait_tech" if dept == "tech" else "wait_general", DEFAULT_WAIT).replace(
        "{name}", esc(c.from_user.first_name))
    await c.message.answer(
        txt + ("" if n else "\n\nСейчас свободных администраторов нет, но вы в очереди.")
        + "\n\nОтменить обращение: /end"
    )


@r.callback_query(F.data.startswith("acc:"))
async def cb_accept(c: CallbackQuery):
    aid = c.from_user.id
    did = int(c.data[4:])
    a = get_admin(aid)
    if not a:
        await c.answer("🚫 Вы не администратор.", show_alert=True)
        return
    if a["current_dialog"]:
        await c.answer("Сначала завершите текущий диалог (/end).", show_alert=True)
        return
    d = get("SELECT * FROM dialogs WHERE id=?", (did,))
    if not d or d["status"] != "waiting":
        await c.answer("Обращение уже принято или закрыто.", show_alert=True)
        return
    if d["user_id"] == aid:
        await c.answer("Нельзя принять своё обращение.", show_alert=True)
        return
    if d["dept"] == "tech" and a["dept"] != "tech" and not is_dev(aid):
        await c.answer("🚫 Тех. поддержка — только для техн. администрации.", show_alert=True)
        return
    cur = run("UPDATE dialogs SET admin_id=?, status='active', accepted=? WHERE id=? AND status='waiting'",
              (aid, now(), did))
    if cur.rowcount == 0:
        await c.answer("Обращение уже принято.", show_alert=True)
        return
    run("UPDATE admins SET current_dialog=? WHERE user_id=?", (did, aid))
    b = InlineKeyboardBuilder()
    b.button(text="🏁 Завершить диалог", callback_data=f"end:{did}")
    await c.answer("Принято")
    await c.message.edit_text(f"✅ Диалог #{did} принят · {anon_id(d['user_id'])}\n"
                              f"Всё, что вы пишете здесь, уходит пользователю.\n"
                              f"Завершить: /end · Передать: /transfer {did} @админ или tech",
                              reply_markup=b.as_markup())
    if d["crisis"]:
        await dm(aid, crisis_info())
    await dm(d["user_id"], f"✅ К вам подключился администратор <b>{esc(pseudo_of(aid))}</b> — <i>{esc(a['position'])}</i>.\n"
                           f"Можете писать.")
    for pm in all_("SELECT tg_mid FROM msgs WHERE dialog_id=? AND role='user' ORDER BY id", (did,)):
        try:
            await BOT.copy_message(aid, d["user_id"], pm["tg_mid"])
        except Exception:
            pass


async def close_dialog(d):
    run("UPDATE dialogs SET status='closed', closed=? WHERE id=?", (now(), d["id"]))
    if d["admin_id"]:
        run("UPDATE admins SET current_dialog=0 WHERE user_id=?", (d["admin_id"],))
        await dm(d["admin_id"], f"🏁 Диалог #{d['id']} завершён.")
        b = InlineKeyboardBuilder()
        for i in range(1, 6):
            b.button(text=f"{i}⭐", callback_data=f"rate:{d['id']}:{i}")
        b.button(text="🚩 Пожаловаться на администратора", callback_data=f"cmp:{d['id']}")
        b.adjust(5, 1)
        await dm(d["user_id"],
                 "Спасибо, что обратились в Nexus 💙\n\n<b>Оцените администратора</b> и оставьте отзыв:",
                 reply_markup=b.as_markup())
    else:
        await dm(d["user_id"], "Обращение отменено. Если понадобится помощь — /start")


async def end_for(uid):
    a = get_admin(uid)
    d = None
    if a and a["current_dialog"]:
        d = get("SELECT * FROM dialogs WHERE id=? AND status='active'", (a["current_dialog"],))
        if not d:
            run("UPDATE admins SET current_dialog=0 WHERE user_id=?", (uid,))
    if not d:
        d = get("SELECT * FROM dialogs WHERE user_id=? AND status IN ('waiting','active') ORDER BY id DESC", (uid,))
    if d:
        await close_dialog(d)
    return d


@r.message(Command("end"), F.chat.type == "private")
async def cmd_end(m: Message):
    if not await end_for(m.from_user.id):
        await m.answer("У вас нет открытых обращений.")


@r.callback_query(F.data.startswith("end:"))
async def cb_end(c: CallbackQuery):
    d = get("SELECT * FROM dialogs WHERE id=? AND status='active'", (int(c.data[4:]),))
    if not d or d["admin_id"] != c.from_user.id:
        await c.answer("Диалог уже завершён.", show_alert=True)
        return
    await c.answer()
    await close_dialog(d)


@r.callback_query(F.data.startswith("rate:"))
async def cb_rate(c: CallbackQuery):
    _, did, stars = c.data.split(":")
    did, stars = int(did), int(stars)
    d = get("SELECT * FROM dialogs WHERE id=?", (did,))
    if not d or d["user_id"] != c.from_user.id or not d["admin_id"]:
        await c.answer()
        return
    if get("SELECT 1 FROM feedback WHERE dialog_id=?", (did,)):
        await c.answer("Вы уже оценили этот диалог.", show_alert=True)
        return
    run("INSERT INTO feedback(dialog_id, admin_id, user_id, stars, ts) VALUES(?,?,?,?,?)",
        (did, d["admin_id"], d["user_id"], stars, now()))
    a = get("SELECT rating, rating_cnt FROM admins WHERE user_id=?", (d["admin_id"],))
    if a:
        cnt = a["rating_cnt"] + 1
        run("UPDATE admins SET rating=?, rating_cnt=? WHERE user_id=?",
            ((a["rating"] * a["rating_cnt"] + stars) / cnt, cnt, d["admin_id"]))
    if stars == 5:
        run("UPDATE admins SET points=points+? WHERE user_id=?", (FIVE_STAR_POINTS, d["admin_id"]))
        await dm(d["admin_id"], f"🌟 Оценка 5⭐ по диалогу #{did}! +{FIVE_STAR_POINTS} баллов.")
        await alog(d["admin_id"], d["admin_id"], "Баллы за оценку 5⭐", f"+{FIVE_STAR_POINTS}, диалог #{did}")
    run("UPDATE users SET pending_review=? WHERE user_id=?", (did, c.from_user.id))
    b = InlineKeyboardBuilder()
    b.button(text="Пропустить", callback_data=f"skip:{did}")
    b.button(text="🚩 Пожаловаться", callback_data=f"cmp:{did}")
    b.adjust(2)
    await c.answer("Спасибо за оценку!")
    await c.message.edit_text(f"Ваша оценка: {'⭐' * stars}\n\nНапишите отзыв одним сообщением или нажмите «Пропустить».",
                              reply_markup=b.as_markup())
    await alog(c.from_user.id, d["admin_id"], "Оценка администратора", f"диалог #{did}, {stars}/5")


@r.callback_query(F.data.startswith("skip:"))
async def cb_skip(c: CallbackQuery):
    run("UPDATE users SET pending_review=0 WHERE user_id=?", (c.from_user.id,))
    await c.answer()
    await c.message.edit_text("Спасибо! Берегите себя 💙 Если понадобится поддержка — /start")


# ───────────────────────── ПРОФИЛЬ И СТАТИСТИКА ─────────────────────────
def stars_txt(a):
    return f"{a['rating']:.2f} ⭐ ({a['rating_cnt']} отз.)" if a["rating_cnt"] or a["rating"] else "нет оценок"


def profile(uid, full=False):
    a = get("SELECT * FROM admins WHERE user_id=?", (uid,))
    w = min(a["warns"], 3)
    aw = all_("SELECT title FROM awards WHERE user_id=? AND kind='award' ORDER BY id DESC LIMIT 10", (uid,))
    ds = all_("SELECT title FROM awards WHERE user_id=? AND kind='dos' ORDER BY id DESC LIMIT 10", (uid,))
    t = (
        "━━━━━━━ 🪪 <b>ПРОФИЛЬ</b> ━━━━━━━\n"
        f"👤 <b>Тег:</b> {tag(uid)}\n"
        f"🎭 <b>Псевдоним:</b> {esc(pseudo_of(uid))}\n"
        f"🏷 <b>Должность:</b> {esc(a['position'])}\n"
        f"🎖 <b>Уровень:</b> {a['level']} / 7\n"
        f"🏢 <b>Отдел:</b> {DEPTS.get(a['dept'], '—')}\n"
        f"⭐ <b>Рейтинг:</b> {stars_txt(a)}\n"
        f"💰 <b>Баллы:</b> {a['points']}\n"
        f"⚠️ <b>Предупреждения:</b> {'🟥' * w}{'⬜' * (3 - w)} ({w}/3)\n"
        f"⛔ <b>Выговоры:</b> {a['vigs']}\n"
    )
    t += "\n🏆 <b>Награды</b>\n" + ("<blockquote>" + "\n".join(f"✦ {esc(x['title'])}" for x in aw) + "</blockquote>"
                                    if aw else "<i>— пока нет —</i>")
    t += "\n\n🎯 <b>Достижения</b>\n" + ("<blockquote>" + "\n".join(f"✧ {esc(x['title'])}" for x in ds) + "</blockquote>"
                                         if ds else "<i>— пока нет —</i>")
    return t


@r.message(Command("me"))
async def cmd_me(m: Message):
    if not await guard(m, "admin", "any"):
        return
    await m.answer(profile(m.from_user.id))


@r.message(Command("dj"))
async def cmd_dj(m: Message):
    if not await guard(m, "admin", "any"):
        return
    rows = all_("SELECT user_id, level, position FROM admins WHERE level>=1 ORDER BY level DESC, since")
    t = "🔒 <b>Это конфиденциальная информация. За её слив вы будете заблокированы навсегда.</b>\n\n"
    cur = None
    for a in rows:
        if a["level"] != cur:
            cur = a["level"]
            t += f"\n<b>━ Уровень {cur} ━</b>\n"
        t += f"• {tag(a['user_id'])} — {esc(a['position'])}\n"
    await reply_long(m, t)


@r.message(Command("week"))
async def cmd_week(m: Message):
    if not await guard(m, "admin", "any"):
        return
    uid = m.from_user.id
    today = datetime.now(TZ).date()
    t = "📊 <b>Ваши ответы за неделю</b>\n\n<code>"
    total = 0
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        s = int(datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp())
        n = get("SELECT COUNT(*) c FROM msgs WHERE role='admin' AND sender=? AND ts>=? AND ts<?",
                (uid, s, s + 86400))["c"]
        total += n
        t += f"{day.strftime('%d.%m')} {'▰' * min(n, 10)}{'▱' * (10 - min(n, 10))} {n}\n"
    since = now() - 7 * 86400
    dl = get("SELECT COUNT(*) c FROM dialogs WHERE admin_id=? AND status='closed' AND closed>=?", (uid, since))["c"]
    rt = get("SELECT AVG(stars) a, COUNT(*) c FROM feedback WHERE admin_id=? AND ts>=?", (uid, since))
    t += "</code>\n"
    t += f"💬 Ответов: <b>{total}</b>\n🏁 Диалогов закрыто: <b>{dl}</b>\n"
    t += f"⭐ Средняя оценка: <b>{rt['a']:.2f}</b> ({rt['c']} отз.)" if rt["c"] else "⭐ Оценок за неделю нет"
    await m.answer(t)


@r.message(Command("duty"))
async def cmd_duty(m: Message):
    if not await guard(m, "admin", "any"):
        return
    a = get_admin(m.from_user.id)
    v = 0 if a["duty"] else 1
    run("UPDATE admins SET duty=?, duty_changed=? WHERE user_id=?", (v, now(), m.from_user.id))
    await m.answer("🟢 Вы принимаете обращения." if v else "🔴 Вы не получаете уведомления об обращениях.")


def ago(ts):
    if not ts:
        return "неизвестно сколько"
    sec = max(0, now() - ts)
    d, h = sec // 86400, sec % 86400 // 3600
    return f"{d} дн. {h} ч." if d else (f"{h} ч." if h else f"{sec // 60} мин.")


@r.message(Command("onduty"))
async def cmd_onduty(m: Message):
    if not await guard(m, "admin", "any"):
        return
    rows = all_("SELECT user_id, position, duty, duty_changed, since, current_dialog FROM admins WHERE level>=1")
    on = [a for a in rows if a["duty"]]
    off = sorted((a for a in rows if not a["duty"]), key=lambda a: a["duty_changed"] or a["since"] or 0)
    t = f"🟢 <b>На дежурстве сейчас: {len(on)}</b>\n"
    for a in on:
        t += (f"• {tag(a['user_id'])} — {esc(a['position'])}" + (" · 💬 в диалоге" if a["current_dialog"] else "")
              + f" · с {fdt(a['duty_changed'] or a['since'])}\n")
    if not on:
        t += "— никого —\n"
    t += f"\n🔴 <b>Выключили /duty: {len(off)}</b> (сначала те, кто давно)\n"
    for a in off:
        ts = a["duty_changed"] or a["since"]
        long_ = ts and now() - ts > DUTY_WARN_DAYS * 86400
        t += f"• {tag(a['user_id'])} — {esc(a['position'])} · выключен {ago(ts)} назад{' ⚠️' if long_ else ''}\n"
    if not off:
        t += "— нет —\n"
    await reply_long(m, t)


@r.message(Command("queue"))
async def cmd_queue(m: Message):
    if not await guard(m, "admin", "any"):
        return
    a = get_admin(m.from_user.id)
    g = get("SELECT COUNT(*) c FROM dialogs WHERE status='waiting' AND dept='general'")["c"]
    tc = get("SELECT COUNT(*) c FROM dialogs WHERE status='waiting' AND dept='tech'")["c"]
    b = InlineKeyboardBuilder()
    b.button(text=f"💬 Общие ({g})", callback_data="q:general")
    if (a and a["dept"] == "tech") or is_dev(m.from_user.id):
        b.button(text=f"🛠 Тех. поддержка ({tc})", callback_data="q:tech")
    b.adjust(1)
    await m.answer("На какие обращения отвечать?", reply_markup=b.as_markup())


@r.callback_query(F.data.startswith("q:"))
async def cb_queue(c: CallbackQuery):
    dept = c.data[2:]
    if not has_perm(c.from_user.id, "admin"):
        await c.answer("🚫", show_alert=True)
        return
    rows = all_("SELECT id, created, crisis FROM dialogs WHERE status='waiting' AND dept=? "
                "ORDER BY crisis DESC, id LIMIT 10", (dept,))
    if not rows:
        await c.answer("Очередь пуста 🎉", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for d in rows:
        b.button(text=f"{'🚨' if d['crisis'] else '✅'} #{d['id']} · {fdt(d['created'])}", callback_data=f"acc:{d['id']}")
    b.adjust(1)
    await c.answer()
    await c.message.answer("Ожидают ответа:", reply_markup=b.as_markup())


def cost_of(kind):
    default = PRED_COST if kind == "pred" else VIG_COST
    try:
        return int(setting(f"cost_{kind}", default))
    except ValueError:
        return default


async def buy_off(m: Message, kind):
    if not await guard(m, "admin", "any"):
        return
    uid = m.from_user.id
    a = get_admin(uid)
    col = "warns" if kind == "pred" else "vigs"
    one, many = ("предупреждение", "предупреждений") if kind == "pred" else ("выговор", "выговоров")
    cost = cost_of(kind)
    if not a or a[col] < 1:
        await m.answer(f"У вас нет {many}.")
        return
    if a["points"] < cost:
        await m.answer(f"Недостаточно баллов: нужно {cost}, у вас {a['points']}.")
        return
    run(f"UPDATE admins SET {col}={col}-1, points=points-? WHERE user_id=?", (cost, uid))
    run("INSERT INTO punishments(user_id,kind,delta,reason,by_id,ts) VALUES(?,?,?,?,?,?)",
        (uid, kind, -1, f"снято за {cost} баллов", uid, now()))
    await alog(uid, uid, f"Снятие: {one} за баллы", f"-{cost} баллов")
    await m.answer(f"✅ {one.capitalize()} {'снято' if kind == 'pred' else 'снят'} за {cost} баллов.")


@r.message(Command("buypred"))
async def cmd_buypred(m: Message):
    await buy_off(m, "pred")


@r.message(Command("buyvig"))
async def cmd_buyvig(m: Message):
    await buy_off(m, "vig")


# ───────────────────────── УПРАВЛЕНИЕ АДМИНАМИ ─────────────────────────
@r.message(Command("makeadmin"))
async def cmd_makeadmin(m: Message, command: CommandObject):
    if not await guard(m, "makeadmin", "staff_or_dev_dm"):
        return
    p = (command.args or "").split()
    if len(p) < 2 or not p[1].isdigit() or int(p[1]) > 7:
        await m.answer("Использование: <code>/makeadmin @юз уровень(0-7) [отдел]</code>\n0 — снятие админки.")
        return
    t = await target_or_fail(m, p[0])
    if not t:
        return
    uid, lvl = t["user_id"], int(p[1])
    dept = norm_dept(p[2]) if len(p) > 2 else "none"
    if dept is None:
        await m.answer("Отдел: tech / edu / main / none")
        return
    err = level_error(m.from_user.id, uid, lvl)
    if err:
        await m.answer(err)
        return
    if lvl == 0:
        if is_dev(uid):
            await m.answer("🛡 Вписанных в код нельзя снять с администрации.")
            return
        run("DELETE FROM admins WHERE user_id=?", (uid,))
        run("DELETE FROM perms WHERE user_id=?", (uid,))
        await alog(m.from_user.id, uid, "Снятие админки (/makeadmin 0)")
        await m.answer(f"✅ {tag(uid)} снят с администрации.")
        await dm(uid, "Ваши права администратора были сняты.")
        return
    cur_row = get("SELECT dept FROM admins WHERE user_id=?", (uid,))
    if cur_row:
        if len(p) <= 2:  # отдел не указан — оставляем прежний
            dept = cur_row["dept"]
        run("UPDATE admins SET level=?, dept=? WHERE user_id=?", (lvl, dept, uid))
    else:
        run("INSERT INTO admins(user_id, level, dept, since) VALUES(?,?,?,?)", (uid, lvl, dept, now()))
    pseudo_of(uid)
    await alog(m.from_user.id, uid, "Выдача админки", f"уровень {lvl}, отдел {DEPTS[dept]}")
    await m.answer(f"✅ {tag(uid)} — администратор {lvl} ур., отдел: {DEPTS[dept]}.\n"
                   f"Доступы настраиваются через <code>/purs @юз</code>.")
    await dm(uid, f"🎉 Вам выдана админка Nexus, уровень {lvl}. Команды: /help")


@r.message(Command("uplvl"))
async def cmd_uplvl(m: Message, command: CommandObject):
    if not await guard(m, "uplvl", "staff_or_dev_dm"):
        return
    t = await target_or_fail(m, command.args or "", need_admin=True)
    if not t:
        return
    a = get_admin(t["user_id"])
    if a["level"] >= 7:
        await m.answer("Максимальный уровень уже достигнут.")
        return
    err = level_error(m.from_user.id, t["user_id"], a["level"] + 1)
    if err:
        await m.answer(err)
        return
    run("UPDATE admins SET level=level+1 WHERE user_id=?", (t["user_id"],))
    await alog(m.from_user.id, t["user_id"], "Повышение уровня", f"{a['level']} → {a['level'] + 1}")
    await m.answer(f"⬆️ {tag(t['user_id'])}: {a['level']} → {a['level'] + 1} ур.")
    await dm(t["user_id"], f"⬆️ Ваш уровень повышен до {a['level'] + 1}.")


@r.message(Command("kick"))
async def cmd_kick(m: Message, command: CommandObject):
    if not await guard(m, "kick", "staff_or_dev_dm"):
        return
    t = await target_or_fail(m, command.args or "", need_admin=True, destructive=True)
    if not t:
        return
    uid = t["user_id"]
    err = level_error(m.from_user.id, uid)
    if err:
        await m.answer(err)
        return
    d = get_admin(uid)
    if d and d["current_dialog"]:
        dd = get("SELECT * FROM dialogs WHERE id=?", (d["current_dialog"],))
        if dd:
            await close_dialog(dd)
    run("DELETE FROM admins WHERE user_id=?", (uid,))
    run("DELETE FROM perms WHERE user_id=?", (uid,))
    await alog(m.from_user.id, uid, "Полное снятие (/kick)")
    await m.answer(f"🗑 {tag(uid)} полностью снят: должность, уровень и доступы удалены.")
    await dm(uid, "Вы сняты с администрации Nexus.")


@r.message(Command("setdj"))
async def cmd_setdj(m: Message, command: CommandObject):
    if not await guard(m, "setdj", "staff_or_dev_dm"):
        return
    p = (command.args or "").split(maxsplit=1)
    if len(p) < 2:
        await m.answer("Использование: <code>/setdj @юз название должности</code>")
        return
    t = await target_or_fail(m, p[0], need_admin=True)
    if not t:
        return
    run("UPDATE admins SET position=? WHERE user_id=?", (p[1][:60], t["user_id"]))
    await alog(m.from_user.id, t["user_id"], "Смена должности", p[1][:60])
    await m.answer(f"✅ Должность {tag(t['user_id'])}: <b>{esc(p[1][:60])}</b>")


@r.message(Command("setage"))
async def cmd_setage(m: Message, command: CommandObject):
    if not await guard(m, "makeadmin", "staff_or_dev_dm"):
        return
    p = (command.args or "").split()
    if len(p) < 2 or not p[1].isdigit():
        await m.answer("Использование: <code>/setage @юз возраст</code>")
        return
    t = await target_or_fail(m, p[0], need_admin=True)
    if not t:
        return
    run("UPDATE admins SET age=? WHERE user_id=?", (int(p[1]), t["user_id"]))
    await alog(m.from_user.id, t["user_id"], "Указан возраст", p[1])
    await m.answer("✅ Возраст сохранён.")


@r.message(Command("setot"))
async def cmd_setot(m: Message, command: CommandObject):
    if not await guard(m, "setot", "any"):
        return
    p = (command.args or "").split()
    dept = norm_dept(p[1]) if len(p) > 1 else None
    if not dept:
        await m.answer("Использование: <code>/setot @юз отдел</code>\nОтделы: <b>tech</b> (техническая администрация), "
                       "<b>edu</b> (обучение), <b>main</b> (главная администрация), <b>none</b> (без отдела).")
        return
    t = await target_or_fail(m, p[0], need_admin=True)
    if not t:
        return
    run("UPDATE admins SET dept=? WHERE user_id=?", (dept, t["user_id"]))
    await alog(m.from_user.id, t["user_id"], "Смена отдела", DEPTS[dept])
    await m.answer(f"✅ {tag(t['user_id'])} → {DEPTS[dept]}")


async def punish(m, command, kind):
    if not await guard(m, kind, "any"):
        return
    p = (command.args or "").split(maxsplit=2)
    delta = parse_delta(p[0]) if p else None
    if len(p) < 3 or delta is None:
        await m.answer(f"Использование: <code>/{kind} +1 @юз причина</code> (от ±1 до ±3; минус — снять)")
        return
    t = await target_or_fail(m, p[1], need_admin=True)
    if not t:
        return
    uid = t["user_id"]
    a = get_admin(uid)
    warns, vigs, auto = a["warns"], a["vigs"], 0
    if kind == "vig":
        vigs = max(0, vigs + delta)
    else:
        warns = max(0, warns + delta)
        while warns >= 3:
            warns -= 3
            vigs += 1
            auto += 1
    run("UPDATE admins SET warns=?, vigs=? WHERE user_id=?", (warns, vigs, uid))
    run("INSERT INTO punishments(user_id,kind,delta,reason,by_id,ts) VALUES(?,?,?,?,?,?)",
        (uid, kind, delta, p[2][:300], m.from_user.id, now()))
    name = "Выговор" if kind == "vig" else "Предупреждение"
    await alog(m.from_user.id, uid, f"{name} {delta:+d}", p[2])
    extra = "\n⚠️ 3 предупреждения → выдан 1 выговор." * (1 if auto else 0)
    await m.answer(f"✅ {name} {delta:+d} для {tag(uid)}\nПричина: {esc(p[2])}\n"
                   f"Сейчас: предупреждения {warns}/3, выговоры {vigs}.{extra}")
    await dm(uid, f"📢 {name} {delta:+d}\nПричина: {esc(p[2])}\nВыдал: {tag(m.from_user.id)}")


@r.message(Command("vig"))
async def cmd_vig(m: Message, command: CommandObject):
    await punish(m, command, "vig")


@r.message(Command("pred"))
async def cmd_pred(m: Message, command: CommandObject):
    await punish(m, command, "pred")


@r.message(Command("setballs"))
async def cmd_setballs(m: Message, command: CommandObject):
    if not await guard(m, "setballs", "any"):
        return
    p = (command.args or "").split(maxsplit=2)
    try:
        delta = int(p[0])
    except Exception:
        delta = 0
    if len(p) < 3 or delta == 0:
        await m.answer("Использование: <code>/setballs +5 @юз причина</code>")
        return
    t = await target_or_fail(m, p[1], need_admin=True)
    if not t:
        return
    run("UPDATE admins SET points=points+? WHERE user_id=?", (delta, t["user_id"]))
    pts = get("SELECT points FROM admins WHERE user_id=?", (t["user_id"],))["points"]
    await alog(m.from_user.id, t["user_id"], f"Баллы {delta:+d}", p[2])
    await m.answer(f"✅ Баллы {tag(t['user_id'])}: {delta:+d} → <b>{pts}</b>\nПричина: {esc(p[2])}")
    await dm(t["user_id"], f"💰 Баллы {delta:+d}. Причина: {esc(p[2])}")


async def give_award(m, command, kind):
    if not await guard(m, "setdos", "any"):
        return
    p = (command.args or "").split(maxsplit=1)
    if len(p) < 2:
        await m.answer(f"Использование: <code>/{'setdos' if kind == 'dos' else 'setaward'} @юз текст</code>")
        return
    t = await target_or_fail(m, p[0], need_admin=True)
    if not t:
        return
    run("INSERT INTO awards(user_id,kind,title,by_id,ts) VALUES(?,?,?,?,?)",
        (t["user_id"], kind, p[1][:120], m.from_user.id, now()))
    label = "Достижение" if kind == "dos" else "Награда"
    await alog(m.from_user.id, t["user_id"], f"{label} выдано", p[1][:120])
    await m.answer(f"🏆 {label} для {tag(t['user_id'])}: <b>{esc(p[1][:120])}</b>")
    await dm(t["user_id"], f"🏆 Вы получили: <b>{esc(p[1][:120])}</b>")


@r.message(Command("setdos"))
async def cmd_setdos(m: Message, command: CommandObject):
    await give_award(m, command, "dos")


@r.message(Command("setaward"))
async def cmd_setaward(m: Message, command: CommandObject):
    await give_award(m, command, "award")


async def del_award(m, command, kind):
    if not await guard(m, "setdos", "any"):
        return
    name = "deldos" if kind == "dos" else "delaward"
    label = "достижений" if kind == "dos" else "наград"
    p = (command.args or "").split()
    if not p or (len(p) > 1 and not p[1].isdigit()):
        await m.answer(f"Использование:\n<code>/{name} @юз</code> — показать список {label} с номерами\n"
                       f"<code>/{name} @юз номер</code> — удалить")
        return
    t = await target_or_fail(m, p[0], need_admin=True, destructive=len(p) > 1)
    if not t:
        return
    uid = t["user_id"]
    if len(p) == 1:
        rows = all_("SELECT id, title FROM awards WHERE user_id=? AND kind=? ORDER BY id", (uid, kind))
        body = "\n".join(f"<code>{x['id']}</code> — {esc(x['title'])}" for x in rows) or "— пусто —"
        await m.answer(f"Список {label} {tag(uid)}:\n{body}\n\nУдалить: <code>/{name} @юз номер</code>")
        return
    row = get("SELECT * FROM awards WHERE id=? AND user_id=? AND kind=?", (int(p[1]), uid, kind))
    if not row:
        await m.answer("❌ Запись с таким номером не найдена. Номера — в списке <code>/%s @юз</code>." % name)
        return
    run("DELETE FROM awards WHERE id=?", (row["id"],))
    await alog(m.from_user.id, uid, "Удалено " + ("достижение" if kind == "dos" else "награда"), row["title"])
    await m.answer(f"🗑 Удалено у {tag(uid)}: <b>{esc(row['title'])}</b>")


@r.message(Command("deldos"))
async def cmd_deldos(m: Message, command: CommandObject):
    await del_award(m, command, "dos")


@r.message(Command("delaward"))
async def cmd_delaward(m: Message, command: CommandObject):
    await del_award(m, command, "award")


# ───────────────────────── ДОСТУПЫ /purs ─────────────────────────
def purs_kb(actor, target):
    tp = perms_of(target)
    ap = perms_of(actor)
    b = InlineKeyboardBuilder()
    for k, title in PERMS.items():
        if is_dev(actor) or k in ap:
            b.button(text=f"{'✅' if k in tp else '❌'} {title}", callback_data=f"pu:{target}:{k}")
    b.adjust(1)
    return b.as_markup()


@r.message(Command("purs"))
async def cmd_purs(m: Message, command: CommandObject):
    if not await guard(m, "purs", "any"):
        return
    t = await target_or_fail(m, command.args or "", need_admin=True, destructive=True)
    if not t:
        return
    await m.answer(f"🔧 Доступы {tag(t['user_id'])}\nВы можете выдавать только те доступы, которые есть у вас.",
                   reply_markup=purs_kb(m.from_user.id, t["user_id"]))


@r.callback_query(F.data.startswith("pu:"))
async def cb_purs(c: CallbackQuery):
    _, t, k = c.data.split(":")
    t, actor = int(t), c.from_user.id
    if (not has_perm(actor, "purs") or k not in PERMS or not (is_dev(actor) or k in perms_of(actor))
            or is_dev(t) or not get_admin(t)):
        await c.answer("🚫 Нет доступа", show_alert=True)
        return
    if k in perms_of(t):
        run("DELETE FROM perms WHERE user_id=? AND perm=?", (t, k))
        act = "Доступ снят"
    else:
        run("INSERT OR IGNORE INTO perms(user_id, perm) VALUES(?,?)", (t, k))
        act = "Доступ выдан"
    await alog(actor, t, act, PERMS[k])
    await c.answer(f"{act}: {PERMS[k]}")
    await c.message.edit_reply_markup(reply_markup=purs_kb(actor, t))


# ───────────────────────── ДОСЬЕ, ОТВЕТЫ, ЛОГИ ─────────────────────────
@r.message(Command("find"))
async def cmd_find(m: Message, command: CommandObject):
    if not await guard(m, "find", "staff_or_dev_dm"):
        return
    t = await target_or_fail(m, command.args or "", need_admin=True)
    if not t:
        return
    uid = t["user_id"]
    a = get("SELECT * FROM admins WHERE user_id=?", (uid,))
    pn = all_("SELECT * FROM punishments WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,))
    dl = get("SELECT COUNT(*) c FROM dialogs WHERE admin_id=? AND status='closed'", (uid,))["c"]
    cm = get("SELECT COUNT(*) c FROM complaints WHERE admin_id=?", (uid,))["c"]
    ps = ", ".join(PERMS[p] for p in perms_of(uid) if p in PERMS) or "—"
    text = (
        f"🔎 <b>Досье {tag(uid)}</b>\n"
        f"🆔 <code>{uid}</code> · 🎭 {esc(pseudo_of(uid))}\n"
        f"🎂 Возраст: {a['age'] or '—'}\n"
        f"🎖 Уровень: {a['level']} · {esc(a['position'])}\n"
        f"🏢 Отдел: {DEPTS.get(a['dept'], '—')}\n"
        f"⭐ Рейтинг: {stars_txt(a)}\n"
        f"💰 Баллы: {a['points']}\n"
        f"⚠️ Предупреждения: {a['warns']}/3 · ⛔ Выговоры: {a['vigs']}\n"
        f"💬 Закрыто диалогов: {dl} · 🚩 Жалоб: {cm}\n"
        f"📅 В администрации с: {fdt(a['since'])}\n"
        f"🔑 Доступы: {esc(ps)}\n\n<b>Наказания (последние 10):</b>\n"
    )
    text += "\n".join(f"• {fdt(x['ts'])} {x['kind']} {x['delta']:+d} — {esc(x['reason'])} ({tag(x['by_id'])})"
                      for x in pn) or "— нет —"
    await alog(m.from_user.id, uid, "Запрос досье (/find)")
    await reply_long(m, text)


@r.message(Command("getday"))
async def cmd_getday(m: Message, command: CommandObject):
    if not await guard(m, "getday", "any"):
        return
    p = (command.args or "").split()
    if not p:
        await m.answer("Использование: <code>/getday @юз [week]</code>")
        return
    t = await target_or_fail(m, p[0], need_admin=True)
    if not t:
        return
    week = len(p) > 1 and p[1].lower() in ("week", "неделя")
    since = now() - (7 if week else 1) * 86400
    rows = all_("SELECT id, ts, ctype FROM msgs WHERE role='admin' AND sender=? AND ts>=? ORDER BY id DESC LIMIT 100",
                (t["user_id"], since))
    head = f"🧾 Ответы {tag(t['user_id'])} за {'неделю' if week else 'день'}: <b>{len(rows)}</b> (до 100)\n\n"
    body = "\n".join(f"<code>#{x['id']}</code> · {fdt(x['ts'])} · {x['ctype']}" for x in rows) or "— нет ответов —"
    await reply_long(m, head + body + "\n\nПосмотреть: <code>/checkot id</code>")


@r.message(Command("checkot"))
async def cmd_checkot(m: Message, command: CommandObject):
    if not await guard(m, "checkot", "any"):
        return
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await m.answer("Использование: <code>/checkot id</code>")
        return
    x = get("SELECT * FROM msgs WHERE id=? AND role='admin'", (int(arg),))
    if not x:
        await m.answer("Ответ с таким ID не найден.")
        return
    d = get("SELECT * FROM dialogs WHERE id=?", (x["dialog_id"],))
    ctx = all_("SELECT * FROM msgs WHERE dialog_id=? AND id<=? ORDER BY id DESC LIMIT 5", (x["dialog_id"], x["id"]))[::-1]
    fb = get("SELECT stars, text FROM feedback WHERE dialog_id=?", (x["dialog_id"],))
    me = get_admin(m.from_user.id)
    reveal = is_dev(m.from_user.id) or (me and me["dept"] in ("tech", "edu", "main"))
    who_u = (tag(d["user_id"]) if reveal else anon_id(d["user_id"])) if d else "—"
    t = (f"🔍 <b>Ответ #{x['id']}</b> · диалог #{x['dialog_id']}\n"
         f"👮 Админ: {tag(x['sender'])}\n👤 Пользователь: {who_u}\n"
         f"🕒 {fdt(x['ts'])}\n\n<b>Контекст:</b>\n")
    for c_ in ctx:
        who = "🧑 Польз." if c_["role"] == "user" else "👮 Админ"
        mark = " ◀️" if c_["id"] == x["id"] else ""
        t += f"{who} [{c_['ctype']}]: {esc((c_['text'] or '—')[:500])}{mark}\n"
    if fb:
        t += f"\n⭐ Оценка диалога: {fb['stars']}/5\n💬 Отзыв: {esc(fb['text'] or '—')}"
    await alog(m.from_user.id, x["sender"], "Проверка ответа (/checkot)", f"#{x['id']}")
    await reply_long(m, t)


@r.message(Command("logs"))
async def cmd_logs(m: Message, command: CommandObject):
    if not await guard(m, "logs", "log_or_dev_dm"):
        return
    t = await target_or_fail(m, command.args or "")
    if not t:
        return
    uid = t["user_id"]
    out = [f"ЛОГИ {ptag(uid)} — сформировано {fdt(now())}", "", "== ДЕЙСТВИЯ =="]
    for x in all_("SELECT * FROM logs WHERE actor=? OR target=? ORDER BY id", (uid, uid)):
        out.append(f"[{fdt(x['ts'])}] {ptag(x['actor'])} -> {ptag(x['target']) if x['target'] else '-'} | "
                   f"{x['action']} | {x['details'] or ''}")
    out += ["", "== ОТВЕТЫ АДМИНИСТРАТОРА =="]
    for x in all_("SELECT * FROM msgs WHERE role='admin' AND sender=? ORDER BY id", (uid,)):
        out.append(f"[{fdt(x['ts'])}] #{x['id']} диалог {x['dialog_id']} [{x['ctype']}] {x['text'] or ''}")
    out += ["", "== НАКАЗАНИЯ =="]
    for x in all_("SELECT * FROM punishments WHERE user_id=? ORDER BY id", (uid,)):
        out.append(f"[{fdt(x['ts'])}] {x['kind']} {x['delta']:+d} | {x['reason']} | выдал {ptag(x['by_id'])}")
    await alog(m.from_user.id, uid, "Выгрузка логов (/logs)")
    await m.answer_document(BufferedInputFile("\n".join(out).encode("utf-8"), filename=f"logs_{uid}.txt"))


# ───────────────────────── ГЛАВНАЯ АДМИНИСТРАЦИЯ (только вписанные в код, ЛС) ─────────────────────────
@r.message(Command("fullban"))
async def cmd_fullban(m: Message, command: CommandObject):
    if not await guard(m, "dev", "dev_dm"):
        return
    p = (command.args or "").split(maxsplit=1)
    if len(p) < 2:
        await m.answer("Использование: <code>/fullban @юз причина</code>")
        return
    t = await target_or_fail(m, p[0], destructive=True)
    if not t:
        return
    run("UPDATE users SET banned=1, ban_reason=? WHERE user_id=?", (p[1], t["user_id"]))
    await end_for(t["user_id"])
    await alog(m.from_user.id, t["user_id"], "Полный ЧС (/fullban)", p[1])
    await m.answer(f"⛔ {tag(t['user_id'])} в полном ЧС. Бот ему больше не отвечает.")


@r.message(Command("unfullban"))
async def cmd_unfullban(m: Message, command: CommandObject):
    if not await guard(m, "dev", "dev_dm"):
        return
    t = await target_or_fail(m, command.args or "")
    if not t:
        return
    run("UPDATE users SET banned=0, ban_reason=NULL WHERE user_id=?", (t["user_id"],))
    await alog(m.from_user.id, t["user_id"], "Снят полный ЧС")
    await m.answer("✅ ЧС снят.")


@r.message(Command("setrate"))
async def cmd_setrate(m: Message, command: CommandObject):
    if not await guard(m, "dev", "dev_dm"):
        return
    p = (command.args or "").split()
    try:
        val = float(p[1].replace(",", "."))
        assert 0 <= val <= 5
    except Exception:
        await m.answer("Использование: <code>/setrate @юз 4.8</code> (0–5)")
        return
    t = await target_or_fail(m, p[0], need_admin=True)
    if not t:
        return
    run("UPDATE admins SET rating=? WHERE user_id=?", (val, t["user_id"]))
    await alog(m.from_user.id, t["user_id"], "Изменён рейтинг", str(val))
    await m.answer(f"⭐ Рейтинг {tag(t['user_id'])}: {val:.2f}")


PERIODS = {"day": ("за день", 86400), "week": ("за неделю", 7 * 86400),
           "month": ("за месяц", 30 * 86400), "year": ("за год", 365 * 86400)}


def botstats_text(period):
    label, sec = PERIODS[period]
    since = now() - sec
    one = lambda sql, a=(): get(sql, a)[0]  # noqa: E731
    best = all_("SELECT admin_id, AVG(stars) a, COUNT(*) c FROM feedback WHERE ts>=? "
                "GROUP BY admin_id ORDER BY a DESC, c DESC LIMIT 3", (since,))
    t = (f"📈 <b>Статистика бота {label}</b>\n\n"
         f"👥 Всего пользователей: <b>{one('SELECT COUNT(*) FROM users')}</b>\n"
         f"🆕 Новых: <b>{one('SELECT COUNT(*) FROM users WHERE first_seen>=?', (since,))}</b>\n"
         f"🔥 Активных: <b>{one('SELECT COUNT(DISTINCT sender) FROM msgs WHERE role=? AND ts>=?', ('user', since))}</b>\n"
         f"💬 Сообщений от людей: <b>{one('SELECT COUNT(*) FROM msgs WHERE role=? AND ts>=?', ('user', since))}</b>\n"
         f"💬 Ответов админов: <b>{one('SELECT COUNT(*) FROM msgs WHERE role=? AND ts>=?', ('admin', since))}</b>\n"
         f"📨 Обращений: <b>{one('SELECT COUNT(*) FROM dialogs WHERE created>=?', (since,))}</b> "
         f"(закрыто {one('SELECT COUNT(*) FROM dialogs WHERE status=? AND closed>=?', ('closed', since))})\n"
         f"⭐ Средняя оценка: <b>{(get('SELECT AVG(stars) FROM feedback WHERE ts>=?', (since,))[0] or 0):.2f}</b>\n"
         f"👮 Администраторов: <b>{one('SELECT COUNT(*) FROM admins WHERE level>=1')}</b>\n"
         f"⏱ Аптайм: <b>{(now() - START_TS) // 3600} ч</b>\n\n🏅 <b>Лучшие администраторы</b>\n")
    t += "\n".join(f"{i}. {tag(b['admin_id'])} — {b['a']:.2f}⭐ ({b['c']} отз.)" for i, b in enumerate(best, 1)) \
        or "— нет оценок —"
    return t


def stats_kb():
    b = InlineKeyboardBuilder()
    for k, (label, _) in PERIODS.items():
        b.button(text=label.capitalize(), callback_data=f"bs:{k}")
    b.adjust(4)
    return b.as_markup()


@r.message(Command("botstats"))
async def cmd_botstats(m: Message):
    if not await guard(m, "dev", "dev_dm"):
        return
    await m.answer(botstats_text("day"), reply_markup=stats_kb())


@r.callback_query(F.data.startswith("bs:"))
async def cb_botstats(c: CallbackQuery):
    if not is_dev(c.from_user.id):
        await c.answer("🚫", show_alert=True)
        return
    await c.answer()
    try:
        await c.message.edit_text(botstats_text(c.data[3:]), reply_markup=stats_kb())
    except Exception:
        pass


FIELDS = {"level": int, "points": int, "warns": int, "vigs": int, "rating": float, "age": int,
          "position": str, "dept": str}


@r.message(Command("setlogs"))
async def cmd_setlogs(m: Message, command: CommandObject):
    if not await guard(m, "dev", "dev_dm"):
        return
    p = (command.args or "").split(maxsplit=3)
    usage = ("Использование:\n<code>/setlogs admin @юз поле значение</code>\nПоля: " + ", ".join(FIELDS) +
             "\n<code>/setlogs bot greeting текст</code> — приветствие ({name} — имя)"
             "\n<code>/setlogs bot crisis текст</code> — памятка админу при кризисном сигнале")
    if len(p) >= 3 and p[0] == "bot" and p[1] in ("greeting", "crisis"):
        key = "greeting" if p[1] == "greeting" else "crisis_info"
        run("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, command.args.split(maxsplit=2)[2]))
        await alog(m.from_user.id, None, "Изменён текст бота: " + p[1])
        await m.answer("✅ Обновлено.")
        return
    if len(p) == 4 and p[0] == "admin" and p[2] in FIELDS:
        t = await target_or_fail(m, p[1], need_admin=True)
        if not t:
            return
        try:
            val = FIELDS[p[2]](p[3])
            if p[2] == "dept":
                val = norm_dept(p[3])
                assert val
            if p[2] == "level":
                assert 1 <= val <= 7
        except Exception:
            await m.answer("❌ Некорректное значение.\n" + usage)
            return
        run(f"UPDATE admins SET {p[2]}=? WHERE user_id=?", (val, t["user_id"]))  # поле из белого списка
        await alog(m.from_user.id, t["user_id"], "Изменение данных (/setlogs)", f"{p[2]}={val}")
        await m.answer(f"✅ {p[2]} = {esc(val)} для {tag(t['user_id'])}")
        return
    await m.answer(usage)


# ───────────────────────── /help ─────────────────────────
@r.message(Command("help"))
async def cmd_help(m: Message):
    if m.chat.type != "private":
        if chat_kind(m):
            await m.answer("ℹ️ /help работает только в личных сообщениях с ботом.")
        return
    uid = m.from_user.id
    lines = [f"<code>{c} {a}</code>\n   ↳ {d}\n   📍 {WHERE_TXT[w]}"
             for c, a, p, w, d in CMDS if has_perm(uid, p)]
    if not lines:
        await m.answer("Для обращения к администратору нажмите /start.")
        return
    await reply_long(m, "📖 <b>Доступные вам команды</b>\n\n" + "\n\n".join(lines))


# ───────────────────────── РАСШИРЕНИЯ ─────────────────────────
CRISIS_WORDS = (
    "суицид", "самоубий", "покончить с собой", "покончу с собой", "убить себя", "убью себя",
    "не хочу жить", "не хочется жить", "хочу умереть", "хочу сдохнуть", "повеситься", "повешусь",
    "вскрыть вены", "вскрою вены", "порезать себя", "режу себя", "нет смысла жить",
    "лучше бы меня не было", "свести счеты с жизнью", "прыгнуть с крыши", "спрыгну с крыши",
)
DEFAULT_CRISIS = (
    "🚨 <b>Кризисный сигнал</b>: в сообщении есть признаки мыслей о самоповреждении или суициде.\n\n"
    "<b>Памятка:</b>\n"
    "• Отвечайте спокойно, тепло и без осуждения. Не спорьте и не обесценивайте.\n"
    "• Спросите прямо, думает ли человек о самоубийстве, есть ли план и средства. Прямой вопрос не «подталкивает».\n"
    "• Выясните, один ли он сейчас и есть ли рядом близкие.\n"
    "• Если опасность прямо сейчас — помогите человеку обратиться в экстренные службы и сразу сообщите старшим.\n"
    "• Не обещайте хранить это в тайне, если жизнь под угрозой.\n"
    "• Нужна помощь коллег — передайте диалог: /transfer номер tech\n\n"
    "<i>Текст памятки меняется: /setlogs bot crisis текст</i>"
)


def is_crisis(text):
    t = (text or "").lower().replace("ё", "е")
    return any(w in t for w in CRISIS_WORDS)


def crisis_info():
    return setting("crisis_info", DEFAULT_CRISIS)


async def mark_crisis(d):
    run("UPDATE dialogs SET crisis=1 WHERE id=?", (d["id"],))
    await alog(d["user_id"], None, "🚨 Кризисный сигнал", f"диалог #{d['id']}")
    if d["status"] == "active" and d["admin_id"]:
        await dm(d["admin_id"], crisis_info())
    else:
        await notify_admins(get("SELECT * FROM dialogs WHERE id=?", (d["id"],)))
    await dm(d["user_id"], "💙 Мы вас слышим. Вы не одни, администратор скоро будет с вами.")


# ── жалоба на администратора
@r.callback_query(F.data.startswith("cmp:"))
async def cb_complaint(c: CallbackQuery):
    did = int(c.data[4:])
    d = get("SELECT * FROM dialogs WHERE id=?", (did,))
    if not d or d["user_id"] != c.from_user.id or not d["admin_id"]:
        await c.answer()
        return
    if get("SELECT 1 FROM complaints WHERE dialog_id=?", (did,)):
        await c.answer("Жалоба по этому диалогу уже отправлена.", show_alert=True)
        return
    run("UPDATE users SET pending_review=? WHERE user_id=?", (-did, c.from_user.id))
    await c.answer()
    await c.message.answer("🚩 Опишите жалобу одним сообщением. Администратор её не увидит: "
                           "её рассмотрит техническая администрация.")


# ── /top
TOP_PERIODS = {"week": ("неделю", 7 * 86400), "month": ("месяц", 30 * 86400)}
MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]


def top_text(period):
    label, sec = TOP_PERIODS[period]
    since = now() - sec
    rate = all_("SELECT admin_id, AVG(stars) a, COUNT(*) c, SUM(stars=5) f FROM feedback WHERE ts>=? "
                "GROUP BY admin_id ORDER BY a DESC, c DESC LIMIT 5", (since,))
    ans = all_("SELECT sender, COUNT(*) c FROM msgs WHERE role='admin' AND ts>=? "
               "GROUP BY sender ORDER BY c DESC LIMIT 5", (since,))
    t = f"🏆 <b>Топ за {label}</b>\n\n⭐ <b>По оценкам</b>\n"
    t += "\n".join(f"{MEDALS[i]} {tag(x['admin_id'])} — {x['a']:.2f}⭐ ({x['c']} отз., 5⭐: {x['f']})"
                   for i, x in enumerate(rate)) or "— нет оценок —"
    t += "\n\n💬 <b>По ответам</b>\n"
    t += "\n".join(f"{MEDALS[i]} {tag(x['sender'])} — {x['c']}" for i, x in enumerate(ans)) or "— нет ответов —"
    return t


def top_kb():
    b = InlineKeyboardBuilder()
    b.button(text="Неделя", callback_data="top:week")
    b.button(text="Месяц", callback_data="top:month")
    return b.as_markup()


@r.message(Command("top"))
async def cmd_top(m: Message):
    if not await guard(m, "admin", "any"):
        return
    await m.answer(top_text("week"), reply_markup=top_kb())


@r.callback_query(F.data.startswith("top:"))
async def cb_top(c: CallbackQuery):
    if not has_perm(c.from_user.id, "admin") or c.data[4:] not in TOP_PERIODS:
        await c.answer("🚫", show_alert=True)
        return
    await c.answer()
    try:
        await c.message.edit_text(top_text(c.data[4:]), reply_markup=top_kb())
    except Exception:
        pass


# ── /warns
KIND_NAMES = {"pred": "⚠️ Предупреждение", "vig": "⛔ Выговор"}


@r.message(Command("warns"))
async def cmd_warns(m: Message, command: CommandObject):
    if not await guard(m, "admin", "any"):
        return
    uid = m.from_user.id
    arg = (command.args or "").strip()
    if arg:
        if not any(has_perm(uid, p) for p in ("vig", "pred", "find")):
            await m.answer("🚫 Чужие наказания смотрят только те, у кого есть доступ к /vig, /pred или /find.")
            return
        t = await target_or_fail(m, arg, need_admin=True)
        if not t:
            return
        target = t["user_id"]
    else:
        target = uid
    a = get("SELECT warns, vigs FROM admins WHERE user_id=?", (target,))
    if not a:
        await m.answer("❌ Запись администратора не найдена.")
        return
    rows = all_("SELECT * FROM punishments WHERE user_id=? ORDER BY id DESC LIMIT 20", (target,))
    text = f"📜 <b>Наказания {tag(target)}</b>\n⚠️ Предупреждения: {a['warns']}/3 · ⛔ Выговоры: {a['vigs']}\n\n"
    for x in rows:
        act = "снято" if x["delta"] < 0 else "выдано"
        text += (f"{KIND_NAMES.get(x['kind'], x['kind'])} {x['delta']:+d} ({act}) · {fdt(x['ts'])}\n"
                 f"   Причина: {esc(x['reason'])}\n   Кто выдал: {tag(x['by_id'])}\n\n")
    if not rows:
        text += "— записей нет —"
    await reply_long(m, text)


# ── /transfer
@r.message(Command("transfer"))
async def cmd_transfer(m: Message, command: CommandObject):
    if not await guard(m, "admin", "any"):
        return
    p = (command.args or "").split()
    if len(p) < 2 or not p[0].lstrip("#").isdigit():
        await m.answer("Использование:\n<code>/transfer номер @админ</code> — передать конкретному администратору\n"
                       "<code>/transfer номер tech</code> — вернуть в очередь тех. поддержки")
        return
    did, uid = int(p[0].lstrip("#")), m.from_user.id
    d = get("SELECT * FROM dialogs WHERE id=? AND status='active'", (did,))
    if not d:
        await m.answer("❌ Активный диалог с таким номером не найден.")
        return
    if d["admin_id"] != uid and not is_dev(uid):
        await m.answer("🚫 Передавать можно только свой диалог.")
        return
    old = d["admin_id"]
    dest = " ".join(p[1:])
    if dest.lower() in ("tech", "tex", "тех"):
        run("UPDATE dialogs SET dept='tech', admin_id=NULL, status='waiting', remind_ref=0 WHERE id=?", (did,))
        run("UPDATE admins SET current_dialog=0 WHERE user_id=?", (old,))
        n = await notify_admins(get("SELECT * FROM dialogs WHERE id=?", (did,)), exclude={old})
        await dm(d["user_id"], "🔄 Переводим вас к специалисту тех. поддержки, пожалуйста, подождите немного.")
        await alog(uid, old, "Передача диалога в тех. поддержку", f"диалог #{did}")
        await m.answer(f"✅ Диалог #{did} передан в очередь тех. поддержки (уведомлено: {n}).")
        return
    t = await target_or_fail(m, dest, need_admin=True)
    if not t:
        return
    ta = get_admin(t["user_id"])
    if not ta or t["user_id"] == old:
        await m.answer("❌ Выберите другого администратора.")
        return
    if ta["current_dialog"]:
        await m.answer("❌ Этот администратор сейчас занят другим диалогом.")
        return
    if d["dept"] == "tech" and ta["dept"] != "tech" and not is_dev(t["user_id"]):
        await m.answer("❌ Это обращение тех. поддержки: передать можно только администратору тех. отдела.")
        return
    run("UPDATE dialogs SET admin_id=?, accepted=?, remind_ref=0 WHERE id=?", (t["user_id"], now(), did))
    run("UPDATE admins SET current_dialog=0 WHERE user_id=?", (old,))
    run("UPDATE admins SET current_dialog=? WHERE user_id=?", (did, t["user_id"]))
    b = InlineKeyboardBuilder()
    b.button(text="🏁 Завершить диалог", callback_data=f"end:{did}")
    await dm(t["user_id"], f"🔄 Вам передан диалог #{did} от {tag(old)} · {anon_id(d['user_id'])}\n"
                           f"Всё, что вы пишете здесь, уходит пользователю. Завершить: /end", reply_markup=b.as_markup())
    for pm in all_("SELECT tg_mid FROM msgs WHERE dialog_id=? AND role='user' ORDER BY id DESC LIMIT 15",
                   (did,))[::-1]:
        try:
            await BOT.copy_message(t["user_id"], d["user_id"], pm["tg_mid"])
        except Exception:
            pass
    if d["crisis"]:
        await dm(t["user_id"], crisis_info())
    await dm(d["user_id"], f"🔄 К вам подключился другой администратор <b>{esc(pseudo_of(t['user_id']))}</b> — "
                           f"<i>{esc(ta['position'])}</i>.")
    await alog(uid, t["user_id"], "Передача диалога", f"диалог #{did}")
    await m.answer(f"✅ Диалог #{did} передан {tag(t['user_id'])}.")


# ── /temes, /addtem, /deltem
@r.message(Command("temes"))
async def cmd_temes(m: Message):
    if not await guard(m, "admin", "any"):
        return
    rows = all_("SELECT * FROM templates ORDER BY id")
    if not rows:
        await m.answer("Шаблонов пока нет. Добавить: <code>/addtem название | текст</code>")
        return
    text = "📑 <b>Актуальные шаблоны</b>\n\n" + "\n\n".join(
        f"<b>{x['id']}. {esc(x['title'])}</b>\n{esc(x['text'])}" for x in rows)
    await reply_long(m, text)


@r.message(Command("addtem"))
async def cmd_addtem(m: Message, command: CommandObject):
    if not await guard(m, "temes", "any"):
        return
    if "|" not in (command.args or ""):
        await m.answer("Использование: <code>/addtem название | текст шаблона</code>")
        return
    title, body = (x.strip() for x in command.args.split("|", 1))
    if not title or not body:
        await m.answer("Использование: <code>/addtem название | текст шаблона</code>")
        return
    cur = run("INSERT INTO templates(title, text, by_id, ts) VALUES(?,?,?,?)",
              (title[:80], body[:2000], m.from_user.id, now()))
    await alog(m.from_user.id, None, "Добавлен шаблон", title[:80])
    await m.answer(f"✅ Шаблон №{cur.lastrowid} добавлен. Список: /temes")


@r.message(Command("deltem"))
async def cmd_deltem(m: Message, command: CommandObject):
    if not await guard(m, "temes", "any"):
        return
    arg = (command.args or "").strip().lstrip("#")
    row = get("SELECT * FROM templates WHERE id=?", (int(arg),)) if arg.isdigit() else None
    if not row:
        await m.answer("Использование: <code>/deltem номер</code> (номера — в /temes)")
        return
    run("DELETE FROM templates WHERE id=?", (row["id"],))
    await alog(m.from_user.id, None, "Удалён шаблон", row["title"])
    await m.answer(f"🗑 Шаблон «{esc(row['title'])}» удалён.")


# ── /people
@r.message(Command("people"))
async def cmd_people(m: Message, command: CommandObject):
    if not await guard(m, "dev", "dev_priv"):
        return
    arg = (command.args or "").strip()
    if arg:
        t = find_user(arg)
        if not t:
            await m.answer("❌ Пользователь не найден.")
            return
        uid = t["user_id"]
        n = get("SELECT COUNT(*) c FROM dialogs WHERE user_id=?", (uid,))["c"]
        last = get("SELECT MAX(created) t FROM dialogs WHERE user_id=?", (uid,))["t"]
        await m.answer(
            f"👤 {tag(uid)} · <code>{uid}</code>\n🆕 Первый визит: {fdt(t['first_seen'])}\n"
            f"🕒 Последняя активность: {fdt(t['last_seen'])}\n💬 Обращений: {n} (последнее: {fdt(last)})\n"
            f"⛔ ЧС: {'да — ' + esc(t['ban_reason'] or '') if t['banned'] else 'нет'}\n"
            f"🛡 Администратор: {'да' if is_admin(uid) else 'нет'}")
        return
    c = lambda sql, a=(): get(sql, a)[0]  # noqa: E731
    text = (
        "👥 <b>Зарегистрированные пользователи</b>\n\n"
        f"Всего: <b>{c('SELECT COUNT(*) FROM users')}</b> (писали боту в ЛС: {c('SELECT COUNT(*) FROM users WHERE started=1')})\n"
        f"Новых за 24 ч: <b>{c('SELECT COUNT(*) FROM users WHERE first_seen>=?', (now() - 86400,))}</b>\n"
        f"Новых за 7 дней: <b>{c('SELECT COUNT(*) FROM users WHERE first_seen>=?', (now() - 7 * 86400,))}</b>\n"
        f"Активных за 7 дней: <b>{c('SELECT COUNT(*) FROM users WHERE last_seen>=?', (now() - 7 * 86400,))}</b>\n"
        f"В полном ЧС: <b>{c('SELECT COUNT(*) FROM users WHERE banned=1')}</b>\n"
        f"Администраторов: <b>{c('SELECT COUNT(*) FROM admins WHERE level>=1')}</b>\n\n"
        "Подробности по человеку: <code>/people @юз</code>. Полный список — файлом ниже."
    )
    rows = all_("SELECT u.*, (SELECT COUNT(*) FROM dialogs d WHERE d.user_id=u.user_id) dc FROM users u "
                "ORDER BY first_seen DESC")
    lines = ["id | username | имя | первый визит | последняя активность | обращений | ЧС | админ"]
    for u in rows:
        lines.append(f"{u['user_id']} | @{u['username'] or '-'} | {u['name'] or '-'} | {fdt(u['first_seen'])} | "
                     f"{fdt(u['last_seen'])} | {u['dc']} | {'да' if u['banned'] else 'нет'} | "
                     f"{'да' if is_admin(u['user_id']) else 'нет'}")
    await m.answer(text)
    await m.answer_document(BufferedInputFile("\n".join(lines).encode("utf-8"), filename="people.txt"))


# ── /broadcast
PENDING_BC = {}
BC_AUD = {"users": "обычные пользователи", "admins": "администраторы", "all": "все"}


def bc_recipients(aud):
    if aud == "admins":
        rows = all_("SELECT user_id FROM admins WHERE level>=1")
    elif aud == "all":
        rows = all_("SELECT user_id FROM users WHERE banned=0 AND started=1")
    else:
        rows = all_("SELECT user_id FROM users WHERE banned=0 AND started=1 "
                    "AND user_id NOT IN (SELECT user_id FROM admins WHERE level>=1)")
    return [x["user_id"] for x in rows]


@r.message(Command("broadcast"))
async def cmd_broadcast(m: Message, command: CommandObject):
    if not await guard(m, "dev", "dev_priv"):
        return
    args, aud = (command.args or "").strip(), "all"
    parts = args.split(maxsplit=1)
    if parts and parts[0].lower() in BC_AUD:
        aud, args = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
    rep_msg = m.reply_to_message
    if not rep_msg and not args:
        await m.answer("Использование:\n<code>/broadcast текст</code> — всем (пользователям и администраторам)\n"
                       "<code>/broadcast users текст</code> — только пользователям, <code>/broadcast admins текст</code> — только администраторам\n"
                       "Ответом (reply) на любое сообщение с фото/видео: <code>/broadcast</code> — разошлёт его копию.\n"
                       "Текст поддерживает HTML: &lt;b&gt;, &lt;i&gt;, &lt;a href&gt;.")
        return
    ids = bc_recipients(aud)
    PENDING_BC[m.from_user.id] = {"aud": aud, "text": args,
                                  "src": (rep_msg.chat.id, rep_msg.message_id) if rep_msg else None}
    await m.answer(f"👀 <b>Предпросмотр</b>. Получателей: <b>{len(ids)}</b> ({BC_AUD[aud]})")
    try:
        if rep_msg:
            await BOT.copy_message(m.chat.id, rep_msg.chat.id, rep_msg.message_id)
        else:
            await m.answer(args)
    except Exception as e:
        PENDING_BC.pop(m.from_user.id, None)
        await m.answer(f"❌ Не удалось собрать сообщение: {esc(e)}")
        return
    b = InlineKeyboardBuilder()
    b.button(text="✅ Отправить", callback_data="bc:yes")
    b.button(text="❌ Отмена", callback_data="bc:no")
    await m.answer("Отправляем?", reply_markup=b.as_markup())


@r.callback_query(F.data.startswith("bc:"))
async def cb_broadcast(c: CallbackQuery):
    uid = c.from_user.id
    if not is_dev(uid):
        await c.answer("🚫", show_alert=True)
        return
    job = PENDING_BC.pop(uid, None)
    if c.data == "bc:no" or not job:
        await c.answer("Отменено" if job else "Черновик устарел, соберите рассылку заново.", show_alert=not job)
        try:
            await c.message.edit_text("❌ Рассылка отменена.")
        except Exception:
            pass
        return
    await c.answer("Запускаю")
    try:
        await c.message.edit_text("📤 Рассылка запущена…")
    except Exception:
        pass
    text = job["text"]
    sent = failed = 0
    for rid in bc_recipients(job["aud"]):
        for _ in (1, 2):
            try:
                if job["src"]:
                    await BOT.copy_message(rid, job["src"][0], job["src"][1])
                else:
                    await BOT.send_message(rid, text)
                sent += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception:
                failed += 1
                break
        else:
            failed += 1
        await asyncio.sleep(0.05)
    await alog(uid, None, "Рассылка", f"{BC_AUD[job['aud']]}: доставлено {sent}, не доставлено {failed}")
    await dm(uid, f"✅ Рассылка завершена: доставлено <b>{sent}</b>, не доставлено <b>{failed}</b>.")


# ── /backup, /setcost
@r.message(Command("backup"))
async def cmd_backup(m: Message):
    if not await guard(m, "dev", "dev_dm"):
        return
    await send_backup(to=m.chat.id)


@r.message(Command("setcost"))
async def cmd_setcost(m: Message, command: CommandObject):
    if not await guard(m, "dev", "dev_dm"):
        return
    p = (command.args or "").split()
    if len(p) == 2 and p[0] in ("pred", "vig") and p[1].isdigit():
        run("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (f"cost_{p[0]}", p[1]))
        await alog(m.from_user.id, None, "Изменена цена снятия", f"{p[0]} = {p[1]} баллов")
        await m.answer(f"✅ Цена снятия {'предупреждения' if p[0] == 'pred' else 'выговора'}: {p[1]} баллов.")
        return
    await m.answer(f"Сейчас: предупреждение — <b>{cost_of('pred')}</b> баллов, выговор — <b>{cost_of('vig')}</b>.\n"
                   "Изменить: <code>/setcost pred 15</code> или <code>/setcost vig 40</code>")


# ───────────────────────── ПСЕВДОНИМЫ, ТЕКСТЫ, ТЕХ. РАБОТЫ ─────────────────────────
@r.message(Command("changepsevd"))
async def cmd_changepsevd(m: Message, command: CommandObject):
    if not await guard(m, "admin", "any"):
        return
    uid = m.from_user.id
    new = " ".join((command.args or "").split())
    if (not 2 <= len(new) <= 32 or not re.fullmatch(r"[\w\-]+(?: [\w\-]+)*", new)
            or new.replace(" ", "").isdigit()):
        await m.answer("Использование: <code>/changepsevd псевдоним</code>\n"
                       "Можно из нескольких слов, 2–32 символа: буквы, цифры, «_» и «-». "
                       "Например: <code>Белый Котик</code>.\n"
                       f"Сейчас у вас: <b>{esc(pseudo_of(uid))}</b>")
        return
    for x in all_("SELECT user_id, pseudo FROM admins WHERE pseudo IS NOT NULL"):
        if x["user_id"] != uid and x["pseudo"].casefold() == new.casefold():
            await m.answer("❌ Такой псевдоним уже занят.")
            return
    old = pseudo_of(uid)
    run("UPDATE admins SET pseudo=? WHERE user_id=?", (new, uid))
    await alog(uid, uid, "Смена псевдонима", f"{old} → {new}")
    await m.answer(f"✅ Теперь пользователи видят вас как <b>{esc(new)}</b>.")


TEXT_KEYS = {
    "start": ("greeting", DEFAULT_GREETING, "приветствие после /start ({name} — имя)"),
    "general": ("wait_general", DEFAULT_WAIT, "текст после выбора «Перейти к администратору»"),
    "tech": ("wait_tech", DEFAULT_WAIT, "текст после выбора «Техническая поддержка»"),
    "maint": ("maint_text", DEFAULT_MAINT, "текст при технических работах (/texon)"),
}


@r.message(Command("settext"))
async def cmd_settext(m: Message, command: CommandObject):
    if not await guard(m, "dev", "dev_dm"):
        return
    p = (command.args or "").split(maxsplit=1)
    if not p or p[0] not in TEXT_KEYS:
        await m.answer("Использование:\n" + "\n".join(
            f"<code>/settext {k} текст</code> — {v[2]}" for k, v in TEXT_KEYS.items())
            + "\n\nБез текста (<code>/settext start</code>) покажет текущий. Поддерживается HTML: &lt;b&gt;, &lt;i&gt;.")
        return
    key, default, label = TEXT_KEYS[p[0]]
    if len(p) == 1:
        await m.answer(f"Сейчас ({label}):\n\n{esc(setting(key, default))}")
        return
    try:
        await BOT.send_message(m.chat.id, "👀 Так будет выглядеть:\n\n" + p[1].replace("{name}", "Имя"))
    except Exception as e:
        await m.answer(f"❌ Текст не принят (ошибка разметки): {esc(e)}")
        return
    run("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, p[1]))
    await alog(m.from_user.id, None, "Изменён текст бота", p[0])
    await m.answer("✅ Текст сохранён.")


@r.message(Command("texon"))
async def cmd_texon(m: Message):
    if not await guard(m, "dev", "dev_dm"):
        return
    run("INSERT OR REPLACE INTO settings(key,value) VALUES('maint','1')")
    await alog(m.from_user.id, None, "Включён режим технических работ")
    await m.answer("🛠 Режим технических работ <b>включён</b>. Пользователи не могут писать боту и видят текст "
                   "с извинением (меняется: <code>/settext maint текст</code>). Администраторы работают как обычно.\n"
                   "Выключить: /texoff")


@r.message(Command("texoff"))
async def cmd_texoff(m: Message):
    if not await guard(m, "dev", "dev_dm"):
        return
    run("INSERT OR REPLACE INTO settings(key,value) VALUES('maint','0')")
    await alog(m.from_user.id, None, "Выключен режим технических работ")
    await m.answer("✅ Режим технических работ выключен, бот снова отвечает всем.")


# ───────────────────────── ПЕРЕСЫЛКА СООБЩЕНИЙ (должен быть последним) ─────────────────────────
def save_msg(did, role, m: Message):
    ct = getattr(m.content_type, "value", str(m.content_type))
    cur = run("INSERT INTO msgs(dialog_id, role, sender, ctype, text, tg_mid, ts) VALUES(?,?,?,?,?,?,?)",
              (did, role, m.from_user.id, ct, m.text or m.caption or "", m.message_id, now()))
    return cur.lastrowid


@r.message(F.chat.type == "private")
async def relay(m: Message):
    if m.text and m.text.startswith("/"):
        return
    uid = m.from_user.id
    a = get("SELECT current_dialog FROM admins WHERE user_id=?", (uid,))
    if a and a["current_dialog"]:
        d = get("SELECT * FROM dialogs WHERE id=? AND status='active'", (a["current_dialog"],))
        if d:
            save_msg(d["id"], "admin", m)
            try:
                await m.copy_to(d["user_id"])
            except Exception:
                await m.answer("⚠️ Не удалось доставить сообщение (пользователь мог заблокировать бота).")
            return
    d = get("SELECT * FROM dialogs WHERE user_id=? AND status IN ('waiting','active') ORDER BY id DESC", (uid,))
    if d:
        save_msg(d["id"], "user", m)
        if m.text and not d["crisis"] and is_crisis(m.text):
            await mark_crisis(d)
            d = get("SELECT * FROM dialogs WHERE id=?", (d["id"],))
        if d["status"] == "active":
            try:
                await m.copy_to(d["admin_id"])
            except Exception:
                pass
        return
    u = get("SELECT pending_review FROM users WHERE user_id=?", (uid,))
    if u and u["pending_review"] and u["pending_review"] < 0 and m.text:
        did = -u["pending_review"]
        d2 = get("SELECT * FROM dialogs WHERE id=?", (did,))
        run("INSERT INTO complaints(dialog_id, admin_id, user_id, text, ts) VALUES(?,?,?,?,?)",
            (did, d2["admin_id"] if d2 else None, uid, m.text[:2000], now()))
        run("UPDATE users SET pending_review=0 WHERE user_id=?", (uid,))
        await m.answer("Спасибо, мы получили вашу жалобу и обязательно разберём её. 💙")
        target = STAFF_CHAT_ID or LOG_CHAT_ID
        if target and d2:
            await dm(target, f"🚩 <b>Жалоба</b> на {tag(d2['admin_id'])}\nДиалог #{did} · проверить: /checkot\n"
                             f"👤 {tag(uid)}\n\n{esc(m.text[:2000])}")
        await alog(uid, d2["admin_id"] if d2 else None, "Жалоба на администратора", f"диалог #{did}")
        return
    if u and u["pending_review"] and u["pending_review"] > 0 and m.text:
        did = u["pending_review"]
        run("UPDATE feedback SET text=? WHERE dialog_id=?", (m.text[:1000], did))
        run("UPDATE users SET pending_review=0 WHERE user_id=?", (uid,))
        fb = get("SELECT * FROM feedback WHERE dialog_id=?", (did,))
        await m.answer("Спасибо за отзыв! Берегите себя 💙 Если понадобится поддержка — /start")
        if fb:
            await dm(fb["admin_id"], f"💬 Новый отзыв по диалогу #{did}: {'⭐' * fb['stars']}\n{esc(m.text[:1000])}")
            await alog(uid, fb["admin_id"], "Отзыв об администраторе", m.text[:300])
        return
    await m.answer("Нажмите /start, чтобы обратиться к администратору.")


# ───────────────────────── ФОНОВЫЕ ЗАДАЧИ ─────────────────────────
async def requeue(d):
    old = d["admin_id"]
    run("UPDATE dialogs SET admin_id=NULL, status='waiting', remind_ref=0 WHERE id=?", (d["id"],))
    run("UPDATE admins SET current_dialog=0 WHERE user_id=?", (old,))
    await dm(old, f"⏰ Диалог #{d['id']} возвращён в очередь: вы не ответили вовремя.")
    await dm(d["user_id"], "⏳ Администратор не смог ответить вовремя. Мы передали ваше обращение другому, "
                           "пожалуйста, подождите немного.")
    await notify_admins(get("SELECT * FROM dialogs WHERE id=?", (d["id"],)), exclude={old})
    await alog(old, None, "Диалог возвращён в очередь (молчание)", f"диалог #{d['id']}")


async def silence_check():
    for d in all_("SELECT * FROM dialogs WHERE status='active' AND admin_id IS NOT NULL"):
        first = get("SELECT MIN(ts) t FROM msgs WHERE dialog_id=? AND role='user' AND id > "
                    "(SELECT COALESCE(MAX(id),0) FROM msgs WHERE dialog_id=? AND role='admin')",
                    (d["id"], d["id"]))["t"]
        if not first:
            continue
        ref = max(first, d["accepted"] or 0)
        wait = now() - ref
        if wait >= REQUEUE_MIN * 60:
            await requeue(d)
        elif wait >= REMIND_MIN * 60 and d["remind_ref"] != ref:
            run("UPDATE dialogs SET remind_ref=? WHERE id=?", (ref, d["id"]))
            await dm(d["admin_id"], f"⏰ Пользователь по диалогу #{d['id']} ждёт ответа уже {wait // 60} мин. "
                                    f"Ответьте или передайте: <code>/transfer {d['id']} tech</code>.\n"
                                    f"Через {max(1, REQUEUE_MIN - wait // 60)} мин. диалог вернётся в очередь.")


async def send_summary():
    chat = EDU_CHAT_ID or LOG_CHAT_ID
    if not chat:
        return
    since = now() - 86400
    one = lambda sql, a=(): get(sql, a)[0]  # noqa: E731
    rt = get("SELECT AVG(stars) a, COUNT(*) c FROM feedback WHERE ts>=?", (since,))
    best = get("SELECT admin_id, AVG(stars) a, COUNT(*) c FROM feedback WHERE ts>=? "
               "GROUP BY admin_id ORDER BY a DESC, c DESC LIMIT 1", (since,))
    low = []
    for a in all_("SELECT user_id, duty FROM admins WHERE level>=1"):
        if is_dev(a["user_id"]):
            continue
        n = one("SELECT COUNT(*) FROM msgs WHERE role='admin' AND sender=? AND ts>=?", (a["user_id"], since))
        if n < LOW_ACTIVITY_ANSWERS:
            low.append(f"• {tag(a['user_id'])} — ответов: {n}" + ("" if a["duty"] else " · /duty выключен"))
    t = (f"📋 <b>Итоги дня · {datetime.now(TZ).strftime('%d.%m.%Y')}</b>\n\n"
         f"📨 Обращений: <b>{one('SELECT COUNT(*) FROM dialogs WHERE created>=?', (since,))}</b> "
         f"(закрыто {one('SELECT COUNT(*) FROM dialogs WHERE status=? AND closed>=?', ('closed', since))})\n"
         f"💬 Ответов администраторов: <b>{one('SELECT COUNT(*) FROM msgs WHERE role=? AND ts>=?', ('admin', since))}</b>\n"
         f"⭐ Средняя оценка: <b>{rt['a']:.2f}</b> ({rt['c']} отз.)\n" if rt["c"] else
         f"📋 <b>Итоги дня · {datetime.now(TZ).strftime('%d.%m.%Y')}</b>\n\n"
         f"📨 Обращений: <b>{one('SELECT COUNT(*) FROM dialogs WHERE created>=?', (since,))}</b>\n"
         f"💬 Ответов администраторов: <b>{one('SELECT COUNT(*) FROM msgs WHERE role=? AND ts>=?', ('admin', since))}</b>\n"
         "⭐ Оценок за день нет\n")
    if best:
        t += f"🏅 Лучший за день: {tag(best['admin_id'])} — {best['a']:.2f}⭐ ({best['c']} отз.)\n"
    if low:
        t += (f"\n⚠️ <b>Низкая активность</b> (меньше {LOW_ACTIVITY_ANSWERS} ответов за сутки):\n"
              + "\n".join(low[:40]) + "\n\nВыдать предупреждение: <code>/pred +1 @юз причина</code>")
    else:
        t += "\n✅ Все администраторы были активны."
    for part in chunks(t):
        await dm(chat, part)


async def send_backup(to=None):
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    tmp = os.path.join(tempfile.gettempdir(), f"nexus_{day}.db")
    if os.path.exists(tmp):
        os.remove(tmp)
    dst = sqlite3.connect(tmp)
    db.backup(dst)
    dst.close()
    with open(tmp, "rb") as f:
        data = f.read()
    os.remove(tmp)
    targets = [to] if to else ([BACKUP_CHAT_ID] if BACKUP_CHAT_ID else sorted(DEV_IDS))
    for t in targets:
        try:
            await BOT.send_document(t, BufferedInputFile(data, filename=f"nexus_{day}.db"),
                                    caption="💾 Резервная копия базы Nexus. В ней есть переписки — храните в тайне.")
        except Exception as e:
            logging.warning("backup to %s failed: %s", t, e)


async def daily_jobs():
    t = datetime.now(TZ)
    today = t.strftime("%Y-%m-%d")
    if t.hour >= SUMMARY_HOUR and setting("last_summary") != today:
        run("INSERT OR REPLACE INTO settings(key,value) VALUES('last_summary',?)", (today,))
        await send_summary()
    if t.hour >= BACKUP_HOUR and setting("last_backup") != today:
        run("INSERT OR REPLACE INTO settings(key,value) VALUES('last_backup',?)", (today,))
        await send_backup()


async def scheduler():
    while True:
        try:
            await silence_check()
            await daily_jobs()
        except Exception:
            logging.exception("scheduler error")
        await asyncio.sleep(60)


# ───────────────────────── ЗАПУСК ─────────────────────────
async def main():
    global BOT
    logging.basicConfig(level=logging.INFO)
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN")
    if not DEV_IDS:
        logging.warning("DEV_IDS пуст: ни у кого нет полного доступа!")
    BOT = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.outer_middleware(Track())
    dp.callback_query.outer_middleware(Track())
    dp.include_router(r)
    await BOT.delete_webhook(drop_pending_updates=True)
    for _a in all_("SELECT user_id FROM admins WHERE level>=1"):
        pseudo_of(_a["user_id"])
    asyncio.create_task(scheduler())
    await dp.start_polling(BOT)


if __name__ == "__main__":
    asyncio.run(main())
