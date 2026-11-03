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
  INFO_CHAT_ID    — ID беседы INFO администрации (объявления о повышениях и назначениях)
  DEV_CHAT_ID     — ID беседы разработчиков (тест: работают все команды, доступы проверяются)
  DB_PATH         — путь к базе (по умолчанию nexus.db)
"""
import asyncio
import hashlib
import html
import json
import logging
import os
import random
import re
import sqlite3
import tempfile
import time
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, ErrorEvent, Message
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
INFO_CHAT_ID = int(os.getenv("INFO_CHAT_ID", "0") or 0)  # беседа INFO администрации
DEV_CHAT_ID = int(os.getenv("DEV_CHAT_ID", "0") or 0)  # беседа разработчиков (тест команд)
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
BAG_DAILY_LIMIT = 5  # сколько багов в сутки может прислать один пользователь
AUDIT_PUNISH_LIMIT = 5  # больше стольких наказаний от одного админа за период — подозрительно
QUEUE_ALERT = 5  # столько людей и больше в очереди — тревога в беседу INFO
QUEUE_WAIT_ALERT = 15  # или самое долгое ожидание больше стольких минут
QUEUE_ALERT_COOLDOWN = 15  # не чаще раза в столько минут
QC_HOUR = 12  # во сколько (МСК) выбирать диалоги для проверки качества
QC_COUNT = 3  # сколько диалогов в день проверяет отдел обучения
WEEKLY_HOUR = 10  # во сколько (МСК) в понедельник слать недельный отчёт
ACCESS_DAYS = 30  # доступ без использования дольше стольких дней считается лишним
MENTOR_DIALOGS = 10  # сколько первых диалогов новичка смотрит наставник

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
    "getdialog": "Просмотр диалога (/getdialog)",
    "botstats": "Статистика бота (/botstats)",
    "broadcast": "Рассылка (/broadcast)",
    "fullban": "Полный ЧС (/fullban, /unfullban)",
    "bugs": "Баги (/bugs, проверка /bag)",
    "rules": "Правила бота (/setrules)",
    "complaints": "Разбор жалоб и плохих отзывов",
    "status": "Состояние бота (/status)",
    "export": "Экспорт статистики (/export)",
    "mentor": "Наставники (/mentor)",
    "apps": "Заявки на админку и оценка тренажёра",
    "access": "Ревизия доступов (/access)",
    "appeals": "Апелляции ЧС (/appeals)",
    "kb": "База знаний: редактирование",
    "psevd": "Псевдонимы (/setpsevd)",
    "qc": "Проверка качества (/qc)",
}
WHERE_TXT = {
    "any": "ЛС бота и беседы: администрации, тех. админов, обучения, главной администрации, разработчиков",
    "staff_or_dev_dm": "беседа тех. админов или главной администрации (в ЛС — только вписанные в код)",
    "log_or_dev_dm": "беседа логов или главной администрации (в ЛС — только вписанные в код)",
    "work": "ЛС бота, беседы тех. админов, обучения, главной администрации",
    "logs": "беседа логов или тех. админов, главная администрация (в ЛС — только вписанные в код)",
    "bugs": "ЛС бота, беседы тех. админов, главной администрации, разработчиков",
    "main_only": "только беседа главной администрации (и разработчиков)",
    "dm": "ЛС бота или беседа главной администрации",
    "dm_only": "только ЛС бота",
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
    ("/transfer", "номер @админ|tech", "admin", "any", "передать свой диалог админу или в тех. поддержку"),
    ("/duty", "", "admin", "any", "вкл/выкл приём обращений"),
    ("/start", "", "all", "dm_only", "начать и обратиться к администратору"),
    ("/bag", "", "all", "dm_only", "сообщить о баге (форма из 4 вопросов)"),
    ("/rules", "", "all", "any", "правила бота"),
    ("/cancel", "", "all", "dm_only", "отменить заполнение формы или вопроса"),
    ("/ping", "", "all", "any", "проверить, работает ли бот, и за сколько он отвечает"),
    ("/apply", "", "all", "dm_only", "подать заявку на должность администратора"),
    ("/train", "", "all", "dm_only", "тренажёр: учебные обращения (кандидатам и админам)"),
    ("/end", "", "all", "dm_only", "завершить или отменить обращение"),
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
    ("/getdialog", "номер", "getdialog", "work", "показать весь диалог по номеру"),
    ("/audit", "[день|неделя|месяц]", "logs", "logs", "аудит действий: подозрительное, критичное, жалобы"),
    ("/bugs", "[all|номер|add текст|fix номер|…]", "bugs", "bugs", "список багов и работа с ними"),
    ("/works", "[add текст|done номер|del номер]", "admin", "main_only", "список задач главной администрации"),
    ("/setrules", "текст", "rules", "dm", "изменить правила бота"),
    ("/setpsevd", "@юз псевдоним", "psevd", "any", "установить псевдоним администратору"),
    ("/complaints", "[номер|all]", "complaints", "bugs", "жалобы на админов: список и разбор"),
    ("/status", "", "status", "work", "состояние бота: аптайм, база, очередь, ошибки"),
    ("/export", "[день|неделя|месяц|год|все]", "export", "work", "статистика за период файлом .txt"),
    ("/apps", "[номер]", "apps", "bugs", "заявки на админку"),
    ("/appeals", "", "appeals", "bugs", "апелляции из ЧС"),
    ("/access", "[@юз]", "access", "logs", "ревизия доступов: кто чем и когда пользовался"),
    ("/mentor", "[@новичок @наставник | off @новичок]", "admin", "any", "наставники: ваши подопечные или ваш наставник"),
    ("/qc", "[new]", "admin", "work", "проверка качества диалогов (отдел обучения)"),
    ("/kb", "[слово|номер]", "admin", "any", "база знаний: поиск по слову прямо в диалоге"),
    ("/kbadd", "название | текст", "admin", "any", "добавить статью (отдел обучения или доступ kb)"),
    ("/kbedit", "номер | текст", "admin", "any", "изменить статью (отдел обучения или доступ kb)"),
    ("/kbdel", "номер", "admin", "any", "удалить статью (отдел обучения или доступ kb)"),
    ("/makeadmin", "@юз уровень(0-7) [отдел]", "makeadmin", "staff_or_dev_dm", "выдать/снять админку"),
    ("/uplvl", "@юз", "uplvl", "staff_or_dev_dm", "повысить на 1 уровень"),
    ("/kick", "@юз", "kick", "staff_or_dev_dm", "полностью снять с админки"),
    ("/setdj", "@юз должность", "setdj", "staff_or_dev_dm", "изменить должность"),
    ("/setage", "@юз возраст", "makeadmin", "staff_or_dev_dm", "указать возраст"),
    ("/setot", "@юз отдел", "setot", "any", "изменить отдел (tech/edu/none)"),
    ("/find", "@юз", "find", "staff_or_dev_dm", "досье на администратора"),
    ("/getday", "@юз [week]", "getday", "any", "ID ответов за день/неделю"),
    ("/checkot", "id", "checkot", "work", "ответ по ID с контекстом"),
    ("/purs", "@юз", "purs", "any", "меню доступов"),
    ("/logs", "@юз|all [период] [категория] [слово]", "logs", "logs", "логи файлом .txt с фильтрами"),
    ("/fullban", "@юз причина", "fullban", "dm", "ЧС: заявка (подтверждает главная администрация), у главной — сразу"),
    ("/unfullban", "@юз", "fullban", "dm", "снять полный ЧС"),
    ("/setrate", "@юз 4.8", "dev", "dev_dm", "изменить рейтинг"),
    ("/botstats", "", "botstats", "work", "статистика бота и лучшие администраторы"),
    ("/setlogs", "", "dev", "dev_dm", "изменение данных бота и админов"),
    ("/people", "[@юз]", "dev", "dev_priv", "зарегистрированные в боте пользователи"),
    ("/broadcast", "[users|admins] текст", "broadcast", "dm_only", "рассылка всем (предпросмотр и подтверждение)"),
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
CREATE TABLE IF NOT EXISTS bugs(id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, reporter INT, what TEXT,
  how TEXT, cur_beh TEXT, exp_beh TEXT, status TEXT, by_id INT, note TEXT, ts INT, closed INT);
CREATE TABLE IF NOT EXISTS bag_drafts(user_id INTEGER PRIMARY KEY, step INT, a1 TEXT, a2 TEXT, a3 TEXT,
  a4 TEXT, ts INT);
CREATE TABLE IF NOT EXISTS info_drafts(id INTEGER PRIMARY KEY AUTOINCREMENT, actor INT, kind TEXT, target INT,
  old_level INT, new_level INT, old_pos TEXT, new_pos TEXT, step TEXT, reason TEXT, text TEXT, ts INT);
CREATE TABLE IF NOT EXISTS works(id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, status TEXT DEFAULT 'open',
  by_id INT, ts INT, done_by INT, done_ts INT);
CREATE TABLE IF NOT EXISTS bad_reviews(id INTEGER PRIMARY KEY AUTOINCREMENT, feedback_id INT, dialog_id INT,
  admin_id INT, user_id INT, stars INT, reason TEXT, decision TEXT, handler INT, status TEXT DEFAULT 'new', ts INT);
CREATE TABLE IF NOT EXISTS qc(id INTEGER PRIMARY KEY AUTOINCREMENT, dialog_id INT, admin_id INT,
  status TEXT DEFAULT 'new', polite INT, speed INT, literacy INT, reviewer INT, ts INT, done_ts INT);
CREATE TABLE IF NOT EXISTS mentors(newbie INTEGER PRIMARY KEY, mentor INT, assigned_by INT, ts INT,
  reviewed INT DEFAULT 0, done INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS mentor_notes(id INTEGER PRIMARY KEY AUTOINCREMENT, newbie INT, mentor INT,
  dialog_id INT, text TEXT, ts INT);
CREATE TABLE IF NOT EXISTS inputs(user_id INTEGER PRIMARY KEY, kind TEXT, ref INT, step INT DEFAULT 0,
  data TEXT, ts INT);
CREATE TABLE IF NOT EXISTS apps(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT, a1 TEXT, a2 TEXT, a3 TEXT,
  a4 TEXT, a5 TEXT, status TEXT DEFAULT 'new', handler INT, note TEXT, ts INT, done_ts INT);
CREATE TABLE IF NOT EXISTS trainer(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT, app_id INT, scen TEXT,
  answers TEXT, status TEXT DEFAULT 'active', reviewer INT, score INT, note TEXT, ts INT, done_ts INT);
CREATE TABLE IF NOT EXISTS ban_requests(id INTEGER PRIMARY KEY AUTOINCREMENT, target INT, requester INT,
  reason TEXT, status TEXT DEFAULT 'new', by_id INT, ts INT, done_ts INT);
CREATE TABLE IF NOT EXISTS appeals(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT, text TEXT,
  status TEXT DEFAULT 'new', by_id INT, ts INT, done_ts INT);
CREATE TABLE IF NOT EXISTS kb(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, text TEXT, by_id INT, ts INT);
CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INT, place TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS perm_use(user_id INT, perm TEXT, last_ts INT, cnt INT DEFAULT 0,
  PRIMARY KEY(user_id, perm));
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
    "ALTER TABLE perms ADD COLUMN granted_ts INT",
    "ALTER TABLE users ADD COLUMN ban_notified INT DEFAULT 0",
    "ALTER TABLE users ADD COLUMN appealed INT DEFAULT 0",
    "ALTER TABLE complaints ADD COLUMN status TEXT DEFAULT 'new'",
    "ALTER TABLE complaints ADD COLUMN handler INT",
    "ALTER TABLE complaints ADD COLUMN done_ts INT",
    "ALTER TABLE complaints ADD COLUMN warned INT DEFAULT 0",
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


def depts_of(v):
    """Отделы администратора (их может быть несколько): строка 'tech,edu' или строка таблицы admins."""
    if not isinstance(v, str):
        v = v["dept"] if v is not None else ""
    return {x for x in (v or "").split(",") if x and x != "none"}


def has_dept(v, name):
    return name in depts_of(v)


def dept_label(v):
    names = [DEPTS[x] for x in sorted(depts_of(v)) if x in DEPTS]
    return ", ".join(names) or DEPTS["none"]


def parse_depts(tokens):
    """['tech', 'edu'] или ['tech,edu'] → 'edu,tech'; 'none' — без отдела; None — ошибка."""
    out = set()
    for tok in tokens:
        for part in tok.replace(",", " ").split():
            d = norm_dept(part)
            if d is None:
                return None
            if d != "none":
                out.add(d)
    return ",".join(sorted(out)) or "none"


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
                 + (f"\n{esc(details[:500])}" if details else "") + f"\n#{log_cat(action)}")


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
                      ("main", MAIN_CHAT_ID), ("log", LOG_CHAT_ID), ("dev", DEV_CHAT_ID), ("info", INFO_CHAT_ID)):
        if cid and m.chat.id == cid:
            return name
    return None


async def guard(m: Message, perm, where="any"):
    """Проверка места и доступа. perm: None | 'all' | 'admin' | 'dev' | ключ из PERMS.
    В беседе главной администрации разрешены почти все команды, в беседе разработчиков — все.
    Доступы проверяются везде."""
    uid = m.from_user.id
    k = chat_kind(m)
    priv = k == "priv"
    dev = is_dev(uid)
    free = k in ("main", "dev")
    ok = {
        "any": k in ("priv", "admin", "staff", "edu", "main", "dev"),
        "staff_or_dev_dm": k in ("staff", "main", "dev") or (priv and dev),
        "log_or_dev_dm": k in ("log", "main", "dev") or (priv and dev),
        "work": k in ("priv", "staff", "edu", "main", "dev"),
        "logs": k in ("log", "staff", "main", "dev") or (priv and dev),
        "bugs": k in ("priv", "staff", "main", "dev"),
        "main_only": k in ("main", "dev"),
        "dm": priv or free,
        "dm_only": priv,
        "dev_dm": (priv and dev) or free,
        "dev_priv": priv and dev,
    }[where]
    if k == "dev":
        ok = True
    if not ok:
        if k and k != "info":
            await m.answer("🚫 Эта команда здесь недоступна. Подробнее: /help (в ЛС бота).")
        return False
    if perm and not has_perm(uid, perm):
        await m.answer("🚫 У вас нет доступа к этой команде.")
        return False
    if perm in PERMS and not dev:  # для ревизии доступов (/access)
        run("INSERT INTO perm_use(user_id, perm, last_ts, cnt) VALUES(?,?,?,1) "
            "ON CONFLICT(user_id, perm) DO UPDATE SET last_ts=excluded.last_ts, cnt=cnt+1", (uid, perm, now()))
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
                    await banned_flow(event, u)
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
        if d["dept"] == "tech" and not has_dept(a, "tech") and not is_dev(a["user_id"]):
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
    if d["dept"] == "tech" and not has_dept(a, "tech") and not is_dev(aid):
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
        await notify_mentor(d)
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
    if stars <= 3:
        await bad_review_card(d, stars)


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
        f"🏢 <b>Отдел:</b> {dept_label(a['dept'])}\n"
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
    if (a and has_dept(a, "tech")) or is_dev(m.from_user.id):
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
    dept = parse_depts(p[2:]) if len(p) > 2 else "none"
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
    cur_row = get("SELECT dept, level FROM admins WHERE user_id=?", (uid,))
    if cur_row:
        if len(p) <= 2:  # отдел не указан — оставляем прежний
            dept = cur_row["dept"]
        run("UPDATE admins SET level=?, dept=? WHERE user_id=?", (lvl, dept, uid))
    else:
        run("INSERT INTO admins(user_id, level, dept, since) VALUES(?,?,?,?)", (uid, lvl, dept, now()))
    pseudo_of(uid)
    await alog(m.from_user.id, uid, "Выдача админки", f"уровень {lvl}, отдел {dept_label(dept)}")
    await m.answer(f"✅ {tag(uid)} — администратор {lvl} ур., отдел: {dept_label(dept)}.\n"
                   f"Доступы настраиваются через <code>/purs @юз</code>.")
    await dm(uid, f"🎉 Вам выдана админка Nexus, уровень {lvl}. Команды: /help")
    if cur_row is None:
        await after_appoint(m.from_user.id, uid, lvl)
    elif lvl > cur_row["level"]:
        await offer_info(m.from_user.id, "promote", uid, cur_row["level"], lvl)
    elif lvl < cur_row["level"]:
        await offer_info(m.from_user.id, "demote", uid, cur_row["level"], lvl)


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
    await offer_info(m.from_user.id, "promote", t["user_id"], a["level"], a["level"] + 1)


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
    old_pos = get("SELECT position FROM admins WHERE user_id=?", (t["user_id"],))["position"]
    run("UPDATE admins SET position=? WHERE user_id=?", (p[1][:60], t["user_id"]))
    await alog(m.from_user.id, t["user_id"], "Смена должности", p[1][:60])
    await m.answer(f"✅ Должность {tag(t['user_id'])}: <b>{esc(p[1][:60])}</b>")
    await offer_info(m.from_user.id, "position", t["user_id"], old_pos=old_pos, new_pos=p[1][:60])


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
    usage = ("Использование:\n<code>/setot @юз tech edu</code> — задать список отделов\n"
             "<code>/setot @юз +edu</code> — добавить отдел, <code>/setot @юз -tech</code> — убрать\n"
             "<code>/setot @юз none</code> — без отдела\n"
             "Отделы: <b>tech</b>, <b>edu</b>, <b>main</b>. У администратора их может быть несколько.")
    if len(p) < 2:
        await m.answer(usage)
        return
    t = await target_or_fail(m, p[0], need_admin=True)
    if not t:
        return
    uid = t["user_id"]
    tokens = p[1:]
    signed = [x[0] in "+-" for x in tokens]
    if all(signed):
        cur = depts_of(get("SELECT dept FROM admins WHERE user_id=?", (uid,))["dept"])
        for tok in tokens:
            for part in tok[1:].replace(",", " ").split():
                d = norm_dept(part)
                if d is None or d == "none":
                    await m.answer(usage)
                    return
                cur.add(d) if tok[0] == "+" else cur.discard(d)
        new = ",".join(sorted(cur)) or "none"
    elif not any(signed):
        new = parse_depts(tokens)
        if new is None:
            await m.answer(usage)
            return
    else:
        await m.answer(usage)
        return
    run("UPDATE admins SET dept=? WHERE user_id=?", (new, uid))
    await alog(m.from_user.id, uid, "Смена отдела", dept_label(new))
    await m.answer(f"✅ {tag(uid)} → {dept_label(new)}")


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
    t = await target_or_fail(m, p[0], need_admin=True)
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
        run("INSERT OR IGNORE INTO perms(user_id, perm, granted_ts) VALUES(?,?,?)", (t, k, now()))
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
        f"🏢 Отдел: {dept_label(a['dept'])}\n"
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
    if not await guard(m, "checkot", "work"):
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
    reveal = can_see_users(m.from_user.id)
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


LOG_CATS = (
    ("наказания", ("выговор", "предупрежд", "баллы", "снятие:")),
    ("доступы", ("доступ",)),
    ("баны", ("чс", "бан")),
    ("уровни", ("админк", "уровн", "повышени", "полное снятие", "должност", "отдел", "псевдоним", "возраст",
                "данных", "публикац")),
    ("диалоги", ("диалог", "оценк", "жалоб", "отзыв", "кризис", "очеред")),
    ("баги", ("баг",)),
)
LOG_CAT_NAMES = {n for n, _ in LOG_CATS} | {"система"}
LOG_PERIODS = {"день": 86400, "сутки": 86400, "day": 86400, "неделя": 7 * 86400, "week": 7 * 86400,
               "месяц": 30 * 86400, "month": 30 * 86400, "все": 0, "всё": 0, "all": 0}
PERIOD_NAMES = {86400: "за день", 7 * 86400: "за неделю", 30 * 86400: "за месяц", 0: "за всё время"}


def log_cat(action):
    a = (action or "").lower()
    for name, keys in LOG_CATS:
        if any(k in a for k in keys):
            return name
    return "система"


@r.message(Command("logs"))
async def cmd_logs(m: Message, command: CommandObject):
    if not await guard(m, "logs", "logs"):
        return
    p = (command.args or "").split()
    if not p:
        await m.answer(
            "📂 <b>Логи</b>\n<code>/logs @юз</code> — всё про человека\n"
            "<code>/logs @юз неделя</code> — за период: <i>день, неделя, месяц, все</i>\n"
            "<code>/logs @юз месяц наказания</code> — по категории: <i>" + ", ".join(sorted(LOG_CAT_NAMES)) + "</i>\n"
            "<code>/logs all день доступы</code> — все действия бота за период\n"
            "<code>/logs all неделя уровни</code> + слово для поиска, например <code>/logs all неделя выговор</code>\n\n"
            "Файл .txt: сначала сводка, потом действия, ответы и наказания. Для разбора действий тех. админов: /audit.")
        return
    uid = None
    if p[0].lower() not in ("all", "все", "всё"):
        t = await target_or_fail(m, p[0])
        if not t:
            return
        uid = t["user_id"]
    period, cat, words = 0, None, []
    for tok in p[1:]:
        lo = tok.lower()
        if lo in LOG_PERIODS:
            period = LOG_PERIODS[lo]
        elif lo in LOG_CAT_NAMES:
            cat = lo
        else:
            words.append(lo)
    since = now() - period if period else 0
    kw = " ".join(words)
    if uid is None:
        rows = all_("SELECT * FROM logs WHERE ts>=? ORDER BY id", (since,))
    else:
        rows = all_("SELECT * FROM logs WHERE ts>=? AND (actor=? OR target=?) ORDER BY id", (since, uid, uid))
    rows = [x for x in rows if (not cat or log_cat(x["action"]) == cat)
            and (not kw or kw in f"{x['action']} {x['details'] or ''} {ptag(x['actor'])}".lower())][-5000:]
    cnt = Counter(log_cat(x["action"]) for x in rows)
    who_ = "ВСЕ" if uid is None else ptag(uid)
    out = [f"ЛОГИ: {who_} — {PERIOD_NAMES[period]}" + (f" · категория: {cat}" if cat else "") + (f" · поиск: {kw}" if kw else ""),
           f"Сформировано: {fdt(now())}", "", "== СВОДКА ==", f"Действий: {len(rows)}"]
    out += [f"  {n}: {c}" for n, c in cnt.most_common()]
    out += ["", "== ДЕЙСТВИЯ =="]
    for x in rows:
        out.append(f"[{fdt(x['ts'])}] #{log_cat(x['action'])} {ptag(x['actor'])} -> "
                   f"{ptag(x['target']) if x['target'] else '-'} | {x['action']} | {x['details'] or ''}")
    if uid is not None and cat in (None, "диалоги"):
        out += ["", "== ОТВЕТЫ АДМИНИСТРАТОРА =="]
        for x in all_("SELECT * FROM msgs WHERE role='admin' AND sender=? AND ts>=? ORDER BY id", (uid, since)):
            if not kw or kw in (x["text"] or "").lower():
                out.append(f"[{fdt(x['ts'])}] #{x['id']} диалог {x['dialog_id']} [{x['ctype']}] {x['text'] or ''}")
    if uid is not None and cat in (None, "наказания"):
        out += ["", "== НАКАЗАНИЯ =="]
        for x in all_("SELECT * FROM punishments WHERE user_id=? AND ts>=? ORDER BY id", (uid, since)):
            if not kw or kw in (x["reason"] or "").lower():
                out.append(f"[{fdt(x['ts'])}] {x['kind']} {x['delta']:+d} | {x['reason']} | выдал {ptag(x['by_id'])}")
    await alog(m.from_user.id, uid, "Выгрузка логов (/logs)", f"{PERIOD_NAMES[period]} {cat or ''} {kw}".strip())
    summary = ", ".join(f"{n} {c}" for n, c in cnt.most_common()) or "ничего не найдено"
    await m.answer_document(BufferedInputFile("\n".join(out).encode("utf-8"),
                                              filename=f"logs_{uid or 'all'}_{period // 86400 or 'all'}.txt"),
                            caption=esc(f"📎 {PERIOD_NAMES[period]}: {len(rows)} записей ({summary})"))


def audit_text(sec, label):
    since = now() - sec
    rows = all_("SELECT * FROM logs WHERE ts>=?", (since,))
    cats = Counter(log_cat(x["action"]) for x in rows)
    actors = Counter(x["actor"] for x in rows if is_admin(x["actor"])).most_common(5)
    punish = all_("SELECT by_id, COUNT(*) c FROM punishments WHERE ts>=? AND delta>0 GROUP BY by_id ORDER BY c DESC",
                  (since,))
    flagged = [x for x in punish if x["c"] > AUDIT_PUNISH_LIMIT]
    crit = [x for x in rows if any(k in x["action"].lower() for k in
                                   ("чс", "снятие", "админк", "доступ", "данных", "уровн", "повышени"))][-12:]
    comp = all_("SELECT * FROM complaints WHERE ts>=?", (since,))
    new_bugs = get("SELECT COUNT(*) c FROM bugs WHERE status='new'")["c"]
    t = f"📊 <b>Аудит {label}</b>\nДействий в логах: <b>{len(rows)}</b>\n"
    t += (" · ".join(f"{n} {c}" for n, c in cats.most_common()) or "—") + "\n"
    t += "\n👮 <b>Самые активные:</b>\n" + ("\n".join(f"• {tag(a)} — {c}" for a, c in actors) or "—")
    t += f"\n\n⚠️ <b>Подозрительно</b> (больше {AUDIT_PUNISH_LIMIT} наказаний от одного админа):\n"
    t += "\n".join(f"• {tag(x['by_id'])} — {x['c']} → <code>/logs @юз {'день' if sec <= 86400 else 'неделя'} наказания</code>"
                   for x in flagged) or "— нет —"
    t += "\n\n🔐 <b>Критичные действия</b> (последние):\n"
    t += "\n".join(f"• {fdt(x['ts'])} {who(x['actor'])}" + (f" → {who(x['target'])}" if x['target'] else "")
                   + f": {esc(x['action'])}" + (f" ({esc((x['details'] or '')[:50])})" if x['details'] else "")
                   for x in crit) or "— нет —"
    t += f"\n\n🚩 <b>Жалобы:</b> {len(comp)}"
    t += "".join(f"\n• на {tag(c['admin_id'])}, диалог #{c['dialog_id']} → <code>/getdialog {c['dialog_id']}</code>" for c in comp[:10])
    t += f"\n🐞 Багов ждут проверки: {new_bugs}" + (" → /bugs" if new_bugs else "")
    t += ("\n\n<b>Что проверить:</b> 1) критичные действия — обоснованы ли; 2) подозрительных — запросите /logs; "
          "3) жалобы — откройте диалог через /getdialog; 4) баги — /bugs.")
    return t


@r.message(Command("audit"))
async def cmd_audit(m: Message, command: CommandObject):
    if not await guard(m, "logs", "logs"):
        return
    arg = (command.args or "день").strip().lower()
    sec = LOG_PERIODS.get(arg, 86400) or 30 * 86400
    await alog(m.from_user.id, None, "Аудит (/audit)", arg)
    await reply_long(m, audit_text(sec, PERIOD_NAMES.get(sec, "за период")))


async def send_audit():
    if STAFF_CHAT_ID:
        for part in chunks(audit_text(86400, "за день")):
            await dm(STAFF_CHAT_ID, part)


# ───────────────────────── ГЛАВНАЯ АДМИНИСТРАЦИЯ (только вписанные в код, ЛС) ─────────────────────────
@r.message(Command("fullban"))
async def cmd_fullban(m: Message, command: CommandObject):
    if not await guard(m, "fullban", "dm"):
        return
    p = (command.args or "").split(maxsplit=1)
    if len(p) < 2:
        await m.answer("Использование: <code>/fullban @юз причина</code>")
        return
    t = await target_or_fail(m, p[0], destructive=True)
    if not t:
        return
    if can_confirm_ban(m.from_user.id):
        await do_fullban(m.from_user.id, t["user_id"], p[1])
        await m.answer(f"⛔ {tag(t['user_id'])} в полном ЧС. Бот ему больше не отвечает.")
        return
    cur = run("INSERT INTO ban_requests(target, requester, reason, ts) VALUES(?,?,?,?)",
              (t["user_id"], m.from_user.id, p[1][:300], now()))
    req = get("SELECT * FROM ban_requests WHERE id=?", (cur.lastrowid,))
    chat = MAIN_CHAT_ID or DEV_CHAT_ID or STAFF_CHAT_ID
    if chat:
        await dm(chat, ban_text(req), reply_markup=ban_kb(req))
    await alog(m.from_user.id, t["user_id"], "Заявка на ЧС", f"#{cur.lastrowid}: {p[1][:200]}")
    await m.answer(f"📨 Заявка #{cur.lastrowid} на ЧС отправлена главной администрации. Решение придёт сюда.")


@r.message(Command("unfullban"))
async def cmd_unfullban(m: Message, command: CommandObject):
    if not await guard(m, "fullban", "dm"):
        return
    t = await target_or_fail(m, command.args or "")
    if not t:
        return
    run("UPDATE users SET banned=0, ban_reason=NULL, ban_notified=0, appealed=0 WHERE user_id=?", (t["user_id"],))
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
    if not await guard(m, "botstats", "work"):
        return
    await m.answer(botstats_text("day"), reply_markup=stats_kb())


@r.callback_query(F.data.startswith("bs:"))
async def cb_botstats(c: CallbackQuery):
    if not has_perm(c.from_user.id, "botstats"):
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
                val = parse_depts(p[3].split())
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
    if d["dept"] == "tech" and not has_dept(ta, "tech") and not is_dev(t["user_id"]):
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
    if not await guard(m, "broadcast", "dm_only"):
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
    if not has_perm(uid, "broadcast"):
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


# ───────────────────────── /bag, /bugs, /getdialog, INFO, /works, /rules ─────────────────────────
def can_see_users(uid):
    """Настоящий тег пользователя видят вписанные в код, тех. админы, отдел обучения и главная администрация."""
    if is_dev(uid):
        return True
    me = get_admin(uid)
    return bool(me and depts_of(me) & {"tech", "edu", "main"})


@r.message(Command("cancel"), F.chat.type == "private")
async def cmd_cancel(m: Message):
    uid = m.from_user.id
    run("DELETE FROM bag_drafts WHERE user_id=?", (uid,))
    run("DELETE FROM info_drafts WHERE actor=? AND step IN ('reason','congrats')", (uid,))
    run("DELETE FROM inputs WHERE user_id=?", (uid,))
    run("UPDATE trainer SET status='cancelled' WHERE user_id=? AND status='active'", (uid,))
    await m.answer("Хорошо, отменено.")


# ── /bag: пользователь сообщает о баге
BAG_QUESTIONS = [
    "🐞 <b>1/4.</b> Что за баг? Опишите коротко.",
    "🔁 <b>2/4.</b> Как вы его получили? Что нажимали или писали?",
    "📍 <b>3/4.</b> Как это работает сейчас?",
    "✅ <b>4/4.</b> Как это должно работать?",
]
BUG_STATUS = {"new": "🆕 ожидает проверки", "confirmed": "🛠 в работе", "fixed": "✅ исправлен", "rejected": "❌ отклонён"}


def bug_text(b):
    src = "пользователь" if b["source"] == "user" else "администратор"
    t = (f"🐞 <b>Баг #{b['id']}</b> · {BUG_STATUS.get(b['status'], b['status'])}\n"
         f"👤 Сообщил ({src}): {tag(b['reporter'])} · {fdt(b['ts'])}\n")
    for label, key in (("Что за баг", "what"), ("Как получили", "how"), ("Как работает сейчас", "cur_beh"),
                       ("Как должно работать", "exp_beh"), ("Заметка", "note")):
        if b[key]:
            t += f"\n<b>{label}:</b> {esc(b[key][:700])}"
    return t


def bug_kb(b):
    kb = InlineKeyboardBuilder()
    if b["status"] == "new":
        kb.button(text="✅ Достоверный", callback_data=f"bug:ok:{b['id']}")
        kb.button(text="❌ Не подтверждён", callback_data=f"bug:no:{b['id']}")
    elif b["status"] == "confirmed":
        kb.button(text="🛠 Исправлено", callback_data=f"bug:fix:{b['id']}")
    kb.adjust(2)
    return kb.as_markup()


@r.message(Command("bag"), F.chat.type == "private")
async def cmd_bag(m: Message):
    uid = m.from_user.id
    n = get("SELECT COUNT(*) c FROM bugs WHERE reporter=? AND source='user' AND ts>=?", (uid, now() - 86400))["c"]
    if n >= BAG_DAILY_LIMIT:
        await m.answer("Вы уже отправили много сообщений о багах за сутки. Спасибо! Попробуйте завтра.")
        return
    run("INSERT OR REPLACE INTO bag_drafts(user_id, step, ts) VALUES(?,0,?)", (uid, now()))
    await m.answer("🐞 <b>Сообщить о баге</b>\nЯ задам 4 коротких вопроса. Отмена: /cancel\n\n" + BAG_QUESTIONS[0])


async def bag_input(m: Message):
    d = get("SELECT * FROM bag_drafts WHERE user_id=?", (m.from_user.id,))
    if not d or not m.text:
        return False
    cols = ["a1", "a2", "a3", "a4"]
    step = d["step"]
    run(f"UPDATE bag_drafts SET {cols[step]}=?, step=? WHERE user_id=?", (m.text[:700], step + 1, m.from_user.id))
    if step < 3:
        await m.answer(BAG_QUESTIONS[step + 1])
        return True
    d = get("SELECT * FROM bag_drafts WHERE user_id=?", (m.from_user.id,))
    cur = run("INSERT INTO bugs(source, reporter, what, how, cur_beh, exp_beh, status, ts) VALUES('user',?,?,?,?,?,'new',?)",
              (m.from_user.id, d["a1"], d["a2"], d["a3"], d["a4"], now()))
    run("DELETE FROM bag_drafts WHERE user_id=?", (m.from_user.id,))
    await m.answer(f"🙏 Спасибо! Баг #{cur.lastrowid} отправлен технической администрации. "
                   "Если он подтвердится, мы вам сообщим.")
    b = get("SELECT * FROM bugs WHERE id=?", (cur.lastrowid,))
    target = STAFF_CHAT_ID or DEV_CHAT_ID or LOG_CHAT_ID
    if target:
        devs = " ".join(tag(x) for x in sorted(DEV_IDS))
        await dm(target, (f"🔔 {devs}\n" if devs else "") + bug_text(b) + "\n\n<i>Проверьте и нажмите кнопку.</i>",
                 reply_markup=bug_kb(b))
    await alog(m.from_user.id, None, "Новый баг от пользователя", f"#{b['id']}")
    return True


@r.callback_query(F.data.startswith("bug:"))
async def cb_bug(c: CallbackQuery):
    _, act, bid = c.data.split(":")
    b = get("SELECT * FROM bugs WHERE id=?", (int(bid),))
    uid = c.from_user.id
    if not has_perm(uid, "bugs"):
        await c.answer("🚫 Нет доступа", show_alert=True)
        return
    if not b or (act, b["status"]) not in (("ok", "new"), ("no", "new"), ("fix", "confirmed")):
        await c.answer("Уже обработано.", show_alert=True)
        return
    new = {"ok": "confirmed", "no": "rejected", "fix": "fixed"}[act]
    run("UPDATE bugs SET status=?, by_id=?, closed=? WHERE id=?",
        (new, uid, now() if new != "confirmed" else None, b["id"]))
    b = get("SELECT * FROM bugs WHERE id=?", (b["id"],))
    if b["source"] == "user":
        msg = {"ok": f"🙏 Спасибо за баг-репорт #{b['id']}! Ошибка подтверждена и передана в работу технической администрации.",
               "no": f"Спасибо за обращение по багу #{b['id']}. Воспроизвести ошибку не удалось. "
                     "Если есть новые подробности, отправьте /bag ещё раз.",
               "fix": f"✅ Ошибка из вашего репорта #{b['id']} исправлена. Спасибо, что помогаете делать бота лучше! 💙"}[act]
        await dm(b["reporter"], msg)
    await alog(uid, b["reporter"], "Проверка бага", f"#{b['id']} → {new}")
    await c.answer("Готово")
    try:
        await c.message.edit_text(bug_text(b) + f"\n\n✍️ {tag(uid)}", reply_markup=bug_kb(b))
    except Exception:
        pass


@r.message(Command("bugs"))
async def cmd_bugs(m: Message, command: CommandObject):
    if not await guard(m, "bugs", "bugs"):
        return
    a = (command.args or "").split(maxsplit=1)
    sub = a[0].lower() if a else ""
    rest = a[1].strip() if len(a) > 1 else ""
    uid = m.from_user.id
    if sub in ("", "all", "new", "confirmed", "fixed", "rejected"):
        if sub == "":
            rows = all_("SELECT * FROM bugs WHERE status IN ('new','confirmed') ORDER BY id DESC LIMIT 40")
            head = "🐞 <b>Открытые баги</b>"
        elif sub == "all":
            rows = all_("SELECT * FROM bugs ORDER BY id DESC LIMIT 40")
            head = "🐞 <b>Все баги</b> (последние 40)"
        else:
            rows = all_("SELECT * FROM bugs WHERE status=? ORDER BY id DESC LIMIT 40", (sub,))
            head = f"🐞 <b>Баги: {BUG_STATUS[sub]}</b>"
        body = "\n".join(f"<code>#{x['id']}</code> {BUG_STATUS[x['status']].split()[0]} {esc((x['what'] or '—')[:70])}"
                         f" · {'👤' if x['source'] == 'user' else '👮'}" for x in rows) or "— пусто —"
        await reply_long(m, f"{head}\n\n{body}\n\n<code>/bugs номер</code> — подробно · <code>/bugs add текст</code> — добавить · "
                            "<code>/bugs fix|ok|no|reopen|del номер</code>")
        return
    if sub == "add":
        if not rest:
            await m.answer("Использование: <code>/bugs add описание бага</code>")
            return
        cur = run("INSERT INTO bugs(source, reporter, what, status, by_id, ts) VALUES('admin',?,?,'confirmed',?,?)",
                  (uid, rest[:700], uid, now()))
        await alog(uid, None, "Добавлен баг", f"#{cur.lastrowid}")
        await m.answer(f"✅ Баг #{cur.lastrowid} добавлен в список (в работе).")
        return
    num = sub if sub.isdigit() else (rest.lstrip("#") if rest.lstrip("#").isdigit() else "")
    if sub.isdigit():
        act = "show"
    elif sub in ("show", "fix", "ok", "no", "reopen", "del") and num:
        act = sub
    else:
        await m.answer("Использование: <code>/bugs</code>, <code>/bugs all</code>, <code>/bugs номер</code>, "
                       "<code>/bugs add текст</code>, <code>/bugs fix|ok|no|reopen|del номер</code>")
        return
    b = get("SELECT * FROM bugs WHERE id=?", (int(num),))
    if not b:
        await m.answer("❌ Баг с таким номером не найден.")
        return
    if act == "show":
        await reply_long(m, bug_text(b), reply_markup=bug_kb(b))
        return
    if act == "del":
        run("DELETE FROM bugs WHERE id=?", (b["id"],))
        await alog(uid, None, "Удалён баг", f"#{b['id']}")
        await m.answer(f"🗑 Баг #{b['id']} удалён.")
        return
    new = {"fix": "fixed", "ok": "confirmed", "no": "rejected", "reopen": "confirmed"}[act]
    run("UPDATE bugs SET status=?, by_id=?, closed=? WHERE id=?",
        (new, uid, now() if new in ("fixed", "rejected") else None, b["id"]))
    if b["source"] == "user" and act in ("fix", "ok"):
        await dm(b["reporter"], f"🙏 Спасибо за баг-репорт #{b['id']}! " +
                 ("Ошибка исправлена." if act == "fix" else "Ошибка подтверждена и передана в работу."))
    await alog(uid, None, "Статус бага", f"#{b['id']} → {new}")
    await m.answer(f"✅ Баг #{b['id']}: {BUG_STATUS[new]}")


# ── /getdialog
@r.message(Command("getdialog"))
async def cmd_getdialog(m: Message, command: CommandObject):
    if not await guard(m, "getdialog", "work"):
        return
    arg = (command.args or "").strip().lstrip("#")
    d = get("SELECT * FROM dialogs WHERE id=?", (int(arg),)) if arg.isdigit() else None
    if not d:
        await m.answer("Использование: <code>/getdialog номер</code> (номер диалога, например из жалобы или /checkot)")
        return
    reveal = can_see_users(m.from_user.id)
    user_ = tag(d["user_id"]) if reveal else anon_id(d["user_id"])
    fb = get("SELECT stars, text FROM feedback WHERE dialog_id=?", (d["id"],))
    head = (f"💬 <b>Диалог #{d['id']}</b> · {d['status']} · {'🛠 тех.' if d['dept'] == 'tech' else 'общий'}"
            f"{' · 🚨 кризис' if d['crisis'] else ''}\n"
            f"👤 Пользователь: {user_}\n👮 Администратор: {tag(d['admin_id']) if d['admin_id'] else '—'}\n"
            f"🕒 {fdt(d['created'])} → {fdt(d['closed'])}\n")
    if fb:
        head += f"⭐ Оценка: {fb['stars']}/5 · отзыв: {esc(fb['text'] or '—')}\n"
    rows = all_("SELECT * FROM msgs WHERE dialog_id=? ORDER BY id", (d["id"],))
    lines = []
    for x in rows:
        who_ = "🧑" if x["role"] == "user" else "👮"
        body = x["text"] or f"[{x['ctype']}]"
        lines.append(f"{who_} {datetime.fromtimestamp(x['ts'], TZ).strftime('%d.%m %H:%M')} {esc(body)}")
    await alog(m.from_user.id, d["admin_id"], "Просмотр диалога (/getdialog)", f"диалог #{d['id']}")
    full = head + "\n" + ("\n".join(lines) or "— сообщений нет —")
    if len(full) <= 3800:
        await m.answer(full)
        return
    plain = [re.sub(r"<[^>]+>", "", head), ""] + [html.unescape(re.sub(r"<[^>]+>", "", x)) for x in lines]
    await m.answer(re.sub(r"<[^>]+>", "", head) + f"\nДиалог длинный ({len(lines)} сообщений) — файлом ниже.")
    await m.answer_document(BufferedInputFile("\n".join(plain).encode("utf-8"), filename=f"dialog_{d['id']}.txt"))


# ── INFO: объявления о повышении, понижении, назначении, смене должности
INFO_LABELS = {"promote": "📈 Повышение", "demote": "📉 Понижение", "appoint": "🆕 Назначение",
               "position": "🏷 Смена должности"}


async def offer_info(actor, kind, target, old_level=None, new_level=None, old_pos=None, new_pos=None):
    if not INFO_CHAT_ID:
        return
    cur = run("INSERT INTO info_drafts(actor, kind, target, old_level, new_level, old_pos, new_pos, step, ts) "
              "VALUES(?,?,?,?,?,?,?, 'offer', ?)", (actor, kind, target, old_level, new_level, old_pos, new_pos, now()))
    kb = InlineKeyboardBuilder()
    kb.button(text="📢 Да, опубликовать", callback_data=f"inf:y:{cur.lastrowid}")
    kb.button(text="Нет", callback_data=f"inf:n:{cur.lastrowid}")
    kb.adjust(2)
    detail = (f"\n🎖 Уровень: {old_level} → {new_level}" if kind in ("promote", "demote")
              else f"\n🎖 Уровень: {new_level}" if kind == "appoint"
              else f"\n🏷 {esc(old_pos)} → {esc(new_pos)}")
    await dm(actor, f"📢 <b>Опубликовать в INFO?</b>\n{INFO_LABELS[kind]}: {tag(target)}{detail}", reply_markup=kb.as_markup())


@r.callback_query(F.data.startswith("inf:"))
async def cb_info(c: CallbackQuery):
    _, ans, did = c.data.split(":")
    d = get("SELECT * FROM info_drafts WHERE id=?", (int(did),))
    if not d or d["actor"] != c.from_user.id or d["step"] != "offer":
        await c.answer("Уже обработано.", show_alert=True)
        return
    await c.answer()
    if ans == "n":
        run("DELETE FROM info_drafts WHERE id=?", (d["id"],))
        await c.message.edit_text("Хорошо, в INFO не публикуем.")
        return
    run("DELETE FROM info_drafts WHERE actor=? AND step IN ('reason','congrats') AND id!=?", (d["actor"], d["id"]))
    step = "congrats" if d["kind"] == "appoint" else "reason"
    run("UPDATE info_drafts SET step=? WHERE id=?", (step, d["id"]))
    await c.message.edit_text(info_prompt(d["kind"], step))


def info_prompt(kind, step):
    if step == "reason":
        what = {"promote": "повышения", "demote": "понижения", "position": "смены должности"}[kind]
        return f"✍️ Напишите причину {what} одним сообщением.\nОтмена: /cancel"
    return "🎉 Напишите текст поздравления одним сообщением («-» — без текста).\nОтмена: /cancel"


async def info_input(m: Message):
    d = get("SELECT * FROM info_drafts WHERE actor=? AND step IN ('reason','congrats') ORDER BY id DESC LIMIT 1",
            (m.from_user.id,))
    if not d or not m.text:
        return False
    text = m.text.strip()
    if d["step"] == "reason":
        run("UPDATE info_drafts SET reason=? WHERE id=?", (text[:300], d["id"]))
        if d["kind"] == "promote":
            run("UPDATE info_drafts SET step='congrats' WHERE id=?", (d["id"],))
            await m.answer(info_prompt("promote", "congrats"))
            return True
    else:
        run("UPDATE info_drafts SET text=? WHERE id=?", ("" if text == "-" else text[:500], d["id"]))
    await publish_info(get("SELECT * FROM info_drafts WHERE id=?", (d["id"],)))
    await m.answer("✅ Опубликовано в INFO.")
    return True


async def publish_info(d):
    a = get("SELECT position, dept FROM admins WHERE user_id=?", (d["target"],))
    pos = esc(d["new_pos"] or (a["position"] if a else ""))
    t = f"<b>{INFO_LABELS[d['kind']]}</b>\n👤 {tag(d['target'])} — {pos}\n"
    if d["kind"] in ("promote", "demote"):
        t += f"🎖 Уровень: {d['old_level']} → {d['new_level']}\n"
    elif d["kind"] == "appoint":
        t += f"🎖 Уровень: {d['new_level']}" + (f" · {dept_label(a['dept'])}" if a and depts_of(a) else "") + "\n"
    else:
        t += f"🏷 Было: {esc(d['old_pos'])}\n"
    if d["reason"]:
        t += f"📝 Причина: {esc(d['reason'])}\n"
    if d["text"]:
        t += f"\n🎉 {esc(d['text'])}\n"
    t += f"\n✍️ Опубликовал: {tag(d['actor'])}"
    await dm(INFO_CHAT_ID, t)
    run("UPDATE info_drafts SET step='done' WHERE id=?", (d["id"],))
    await alog(d["actor"], d["target"], "Публикация в INFO", INFO_LABELS[d["kind"]])


# ── /works: задачи главной администрации
@r.message(Command("works"))
async def cmd_works(m: Message, command: CommandObject):
    if not await guard(m, "admin", "main_only"):
        return
    a = (command.args or "").split(maxsplit=1)
    sub = a[0].lower() if a else ""
    rest = a[1].strip() if len(a) > 1 else ""
    uid = m.from_user.id
    if sub in ("", "all"):
        rows = all_("SELECT * FROM works ORDER BY id DESC LIMIT 60") if sub == "all" else \
            all_("SELECT * FROM works WHERE status='open' ORDER BY id")
        body = "\n".join(f"{'✅' if x['status'] == 'done' else '▫️'} <code>#{x['id']}</code> {esc(x['text'])} "
                         f"<i>({fdt(x['ts'])}, {tag(x['by_id'])})</i>" for x in rows) or "— задач нет —"
        await reply_long(m, f"📋 <b>Задачи{' (все)' if sub == 'all' else ''}</b>\n\n{body}\n\n"
                            "<code>/works add текст</code> · <code>/works done номер</code> · "
                            "<code>/works del номер</code> · <code>/works all</code>")
        return
    if sub == "add" and rest:
        cur = run("INSERT INTO works(text, status, by_id, ts) VALUES(?,'open',?,?)", (rest[:500], uid, now()))
        await alog(uid, None, "Добавлена задача (/works)", rest[:100])
        await m.answer(f"✅ Задача #{cur.lastrowid} добавлена.")
        return
    num = rest.lstrip("#")
    if sub in ("done", "del", "remove") and num.isdigit():
        w = get("SELECT * FROM works WHERE id=?", (int(num),))
        if not w:
            await m.answer("❌ Задача с таким номером не найдена.")
            return
        if sub == "done":
            run("UPDATE works SET status='done', done_by=?, done_ts=? WHERE id=?", (uid, now(), w["id"]))
            await m.answer(f"✅ Задача #{w['id']} выполнена.")
        else:
            run("DELETE FROM works WHERE id=?", (w["id"],))
            await m.answer(f"🗑 Задача #{w['id']} убрана.")
        await alog(uid, None, "Задача (/works)", f"{sub} #{w['id']}")
        return
    await m.answer("Использование: <code>/works</code>, <code>/works add текст</code>, "
                   "<code>/works done номер</code>, <code>/works del номер</code>, <code>/works all</code>")


# ── /rules, /setrules
DEFAULT_RULES = (
    "📜 <b>Правила бота Nexus</b>\n\n"
    "1. Общайтесь вежливо: оскорбления, угрозы и травля запрещены.\n"
    "2. Не присылайте спам, рекламу и чужие личные данные.\n"
    "3. Администраторы помогают словом и не заменяют врачей и экстренные службы.\n"
    "4. Если вам или кому-то рядом угрожает опасность, сразу обратитесь в экстренные службы.\n"
    "5. За нарушение правил доступ к боту может быть ограничен."
)


@r.message(Command("rules"))
async def cmd_rules(m: Message):
    k = chat_kind(m)
    if k is None or k in ("log", "info"):
        return
    await reply_long(m, setting("rules", DEFAULT_RULES))


@r.message(Command("setrules"))
async def cmd_setrules(m: Message, command: CommandObject):
    if not await guard(m, "rules", "dm"):
        return
    text = (command.args or "").strip()
    if not text:
        await m.answer("Использование: <code>/setrules текст правил</code> (HTML: &lt;b&gt;, &lt;i&gt;).\n"
                       "Сейчас:\n\n" + setting("rules", DEFAULT_RULES))
        return
    try:
        await BOT.send_message(m.chat.id, "👀 Так будут выглядеть правила:\n\n" + text)
    except Exception as e:
        await m.answer(f"❌ Текст не принят (ошибка разметки): {esc(e)}")
        return
    run("INSERT OR REPLACE INTO settings(key,value) VALUES('rules',?)", (text,))
    await alog(m.from_user.id, None, "Изменены правила бота")
    await m.answer("✅ Правила сохранены. Показать: /rules")


# ───────────────────────── v3: жалобы, плохие оценки, качество, статус ─────────────────────────
def set_input(uid, kind, ref=0, step=0, data=""):
    run("INSERT OR REPLACE INTO inputs(user_id, kind, ref, step, data, ts) VALUES(?,?,?,?,?,?)",
        (uid, kind, ref, step, data, now()))


def clear_input(uid):
    run("DELETE FROM inputs WHERE user_id=?", (uid,))


async def dm_long(chat, text, **kw):
    parts = chunks(text)
    for i, part in enumerate(parts):
        await dm(chat, part, **(kw if i == len(parts) - 1 else {}))


def apply_punish(actor, uid, kind, delta, reason):
    a = get("SELECT warns, vigs FROM admins WHERE user_id=?", (uid,))
    if not a:
        return None
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
        (uid, kind, delta, reason[:300], actor, now()))
    return warns, vigs, auto


# ── разбор жалоб
CPL_STATUS = {"new": "🆕 новая", "work": "🔎 в работе", "justified": "✅ обоснована", "unjustified": "❌ необоснована"}


def complaint_text(c):
    adm = tag(c["admin_id"]) if c["admin_id"] else "—"
    t = (f"🚩 <b>Жалоба #{c['id']}</b> · {CPL_STATUS.get(c['status'], c['status'])}\n"
         f"На: {adm} · диалог #{c['dialog_id']} → <code>/getdialog {c['dialog_id']}</code>\n"
         f"👤 {tag(c['user_id'])} · {fdt(c['ts'])}\n\n{esc(c['text'])}")
    if c["handler"]:
        t += f"\n\n✍️ Рассмотрел: {tag(c['handler'])}"
    if c["warned"]:
        t += " · ⚠️ предупреждение выдано"
    return t


def cpl_kb(c):
    kb = InlineKeyboardBuilder()
    if c["status"] == "new":
        kb.button(text="🔎 Взять в работу", callback_data=f"cpl:take:{c['id']}")
    if c["status"] in ("new", "work"):
        kb.button(text="✅ Обоснована", callback_data=f"cpl:yes:{c['id']}")
        kb.button(text="❌ Необоснована", callback_data=f"cpl:no:{c['id']}")
    if c["status"] == "justified" and not c["warned"] and c["admin_id"]:
        kb.button(text="⚠️ Выдать предупреждение админу", callback_data=f"cpl:warn:{c['id']}")
    kb.adjust(1)
    return kb.as_markup()


@r.callback_query(F.data.startswith("cpl:"))
async def cb_complaint_review(c: CallbackQuery):
    _, act, cid = c.data.split(":")
    uid = c.from_user.id
    cp = get("SELECT * FROM complaints WHERE id=?", (int(cid),))
    if not has_perm(uid, "complaints"):
        await c.answer("🚫 Нет доступа", show_alert=True)
        return
    if not cp:
        await c.answer("Жалоба не найдена.", show_alert=True)
        return
    st = cp["status"]
    if act == "take" and st == "new":
        run("UPDATE complaints SET status='work', handler=? WHERE id=?", (uid, cp["id"]))
    elif act in ("yes", "no") and st in ("new", "work"):
        new = "justified" if act == "yes" else "unjustified"
        run("UPDATE complaints SET status=?, handler=?, done_ts=? WHERE id=?", (new, uid, now(), cp["id"]))
        if act == "yes":
            msg = (f"✅ Мы рассмотрели вашу жалобу по диалогу #{cp['dialog_id']}: она признана обоснованной, "
                   "меры приняты. Спасибо, что сообщили нам.")
        else:
            msg = (f"Мы рассмотрели вашу жалобу по диалогу #{cp['dialog_id']} и проверили работу администратора: "
                   "нарушений не нашли. Спасибо за обращение. Если что-то ещё беспокоит, напишите нам.")
        await dm(cp["user_id"], msg)
        await alog(uid, cp["admin_id"], "Разбор жалобы", f"#{cp['id']}: {new}")
    elif act == "warn" and st == "justified" and not cp["warned"] and cp["admin_id"]:
        if not has_perm(uid, "pred"):
            await c.answer("🚫 Для предупреждения нужен доступ pred.", show_alert=True)
            return
        err = level_error(uid, cp["admin_id"])
        if err:
            await c.answer(err[:190], show_alert=True)
            return
        res = apply_punish(uid, cp["admin_id"], "pred", 1, f"Обоснованная жалоба #{cp['id']} (диалог #{cp['dialog_id']})")
        run("UPDATE complaints SET warned=1 WHERE id=?", (cp["id"],))
        if res:
            await dm(cp["admin_id"], f"📢 Предупреждение +1\nПричина: обоснованная жалоба по диалогу #{cp['dialog_id']}\n"
                                     f"Выдал: {tag(uid)}")
        await alog(uid, cp["admin_id"], "Предупреждение +1 по жалобе", f"#{cp['id']}")
    else:
        await c.answer("Уже обработано.", show_alert=True)
        return
    await c.answer("Готово")
    cp = get("SELECT * FROM complaints WHERE id=?", (cp["id"],))
    try:
        await c.message.edit_text(complaint_text(cp), reply_markup=cpl_kb(cp))
    except Exception:
        pass


@r.message(Command("complaints"))
async def cmd_complaints(m: Message, command: CommandObject):
    if not await guard(m, "complaints", "bugs"):
        return
    arg = (command.args or "").strip().lstrip("#").lower()
    if arg.isdigit():
        cp = get("SELECT * FROM complaints WHERE id=?", (int(arg),))
        if not cp:
            await m.answer("❌ Жалоба не найдена.")
            return
        await reply_long(m, complaint_text(cp), reply_markup=cpl_kb(cp))
        return
    if arg == "all":
        rows = all_("SELECT * FROM complaints ORDER BY id DESC LIMIT 30")
        head = "🚩 <b>Все жалобы</b> (последние 30)"
    else:
        rows = all_("SELECT * FROM complaints WHERE status IN ('new','work') ORDER BY id")
        head = "🚩 <b>Жалобы, которые ждут разбора</b>"
    lines = []
    for x in rows:
        lines.append(f"<code>#{x['id']}</code> {CPL_STATUS[x['status']].split()[0]} на {tag(x['admin_id']) if x['admin_id'] else '—'} "
                     f"· диалог #{x['dialog_id']} · {fdt(x['ts'])}")
    body = "\n".join(lines) or "— нет —"
    await reply_long(m, f"{head}\n\n{body}\n\nПодробно: <code>/complaints номер</code> · все: <code>/complaints all</code>")


# ── плохие оценки (1–3⭐)
BAD_REASONS = {"rude": "😠 Грубость", "slow": "🐌 Медлительность", "err": "❌ Ошибка в ответе", "other": "🤷 Другое"}
BAD_DECISIONS = {"edu": "🎓 Обучение", "warn": "⚠️ Предупреждение", "none": "✅ Ничего"}


def bad_text(b):
    t = (f"📉 <b>Плохая оценка #{b['id']}</b>: {'⭐' * b['stars']} ({b['stars']}/5)\n"
         f"👮 {tag(b['admin_id'])} · диалог #{b['dialog_id']} → <code>/getdialog {b['dialog_id']}</code>")
    if b["reason"]:
        t += f"\nПричина: {BAD_REASONS.get(b['reason'], b['reason'])}"
    if b["decision"]:
        t += f"\nРешение: {BAD_DECISIONS.get(b['decision'], b['decision'])} · {tag(b['handler'])}"
    elif b["reason"]:
        t += "\n<i>Выберите решение.</i>"
    else:
        t += "\n<i>Определите причину.</i>"
    return t


def bad_kb(b):
    kb = InlineKeyboardBuilder()
    if not b["reason"]:
        for code, label in BAD_REASONS.items():
            kb.button(text=label, callback_data=f"bad:{b['id']}:r:{code}")
        kb.adjust(2)
    elif not b["decision"]:
        for code, label in BAD_DECISIONS.items():
            kb.button(text=label, callback_data=f"bad:{b['id']}:d:{code}")
        kb.adjust(1)
    return kb.as_markup()


async def bad_review_card(d, stars):
    fb = get("SELECT id FROM feedback WHERE dialog_id=?", (d["id"],))
    cur = run("INSERT INTO bad_reviews(feedback_id, dialog_id, admin_id, user_id, stars, ts) VALUES(?,?,?,?,?,?)",
              (fb["id"] if fb else None, d["id"], d["admin_id"], d["user_id"], stars, now()))
    chat = STAFF_CHAT_ID or LOG_CHAT_ID
    if chat:
        br = get("SELECT * FROM bad_reviews WHERE id=?", (cur.lastrowid,))
        await dm(chat, bad_text(br), reply_markup=bad_kb(br))


@r.callback_query(F.data.startswith("bad:"))
async def cb_bad(c: CallbackQuery):
    _, bid, kind, code = c.data.split(":")
    uid = c.from_user.id
    b = get("SELECT * FROM bad_reviews WHERE id=?", (int(bid),))
    if not has_perm(uid, "complaints"):
        await c.answer("🚫 Нет доступа", show_alert=True)
        return
    if not b:
        await c.answer("Не найдено.", show_alert=True)
        return
    if kind == "r" and not b["reason"] and code in BAD_REASONS:
        run("UPDATE bad_reviews SET reason=? WHERE id=?", (code, b["id"]))
    elif kind == "d" and b["reason"] and not b["decision"] and code in BAD_DECISIONS:
        reason = BAD_REASONS[b["reason"]]
        if code == "warn":
            if not has_perm(uid, "pred"):
                await c.answer("🚫 Для предупреждения нужен доступ pred.", show_alert=True)
                return
            err = level_error(uid, b["admin_id"])
            if err:
                await c.answer(err[:190], show_alert=True)
                return
            apply_punish(uid, b["admin_id"], "pred", 1, f"Плохая оценка #{b['id']}: {reason} (диалог #{b['dialog_id']})")
            await dm(b["admin_id"], f"📢 Предупреждение +1\nПричина: плохая оценка по диалогу #{b['dialog_id']} ({reason})\n"
                                    f"Выдал: {tag(uid)}")
        elif code == "edu":
            await dm(EDU_CHAT_ID or STAFF_CHAT_ID or LOG_CHAT_ID,
                     f"🎓 <b>Направить на обучение</b>: {tag(b['admin_id'])}\nПричина: {reason}\n"
                     f"Диалог #{b['dialog_id']} → <code>/getdialog {b['dialog_id']}</code>")
            await dm(b["admin_id"], f"📚 По итогам проверки оценки по диалогу #{b['dialog_id']} ({reason}) вам рекомендовано "
                                    "обучение. Отдел обучения свяжется с вами.")
        run("UPDATE bad_reviews SET decision=?, handler=?, status='done' WHERE id=?", (code, uid, b["id"]))
        await alog(uid, b["admin_id"], "Разбор плохой оценки", f"#{b['id']}: {code}")
    else:
        await c.answer("Уже обработано.", show_alert=True)
        return
    await c.answer("Готово")
    b = get("SELECT * FROM bad_reviews WHERE id=?", (b["id"],))
    try:
        await c.message.edit_text(bad_text(b), reply_markup=bad_kb(b))
    except Exception:
        pass


# ── выборочная проверка качества (отдел обучения)
QC_FIELDS = (("p", "polite", "Вежливость"), ("s", "speed", "Скорость ответа"), ("l", "literacy", "Грамотность"))


def can_qc(uid):
    return is_dev(uid) or has_dept(get_admin(uid), "edu") or has_perm(uid, "qc")


def qc_text(q, prompt=None):
    t = (f"🎓 <b>Проверка качества #{q['id']}</b>\nДиалог #{q['dialog_id']} → <code>/getdialog {q['dialog_id']}</code>\n"
         f"👮 {tag(q['admin_id'])}\n")
    for _, col, label in QC_FIELDS:
        if q[col] is not None:
            t += f"{label}: {q[col]}/5\n"
    if prompt:
        t += f"\n<b>{prompt}</b>"
    return t


def qc_next(q):
    for letter, col, label in QC_FIELDS:
        if q[col] is None:
            kb = InlineKeyboardBuilder()
            for i in range(1, 6):
                kb.button(text=str(i), callback_data=f"qc:{q['id']}:{letter}:{i}")
            kb.adjust(5)
            return label, kb.as_markup()
    return None, None


async def qc_send(q, chat=None):
    chat = chat or EDU_CHAT_ID or STAFF_CHAT_ID
    if not chat:
        return
    label, kb = qc_next(q)
    await dm(chat, qc_text(q, f"{label}: оцените от 1 до 5"), reply_markup=kb)


async def qc_generate(n=QC_COUNT):
    rows = all_("SELECT d.id, d.admin_id FROM dialogs d WHERE d.status='closed' AND d.admin_id IS NOT NULL "
                "AND d.closed>=? AND d.id NOT IN (SELECT dialog_id FROM qc) "
                "AND (SELECT COUNT(*) FROM msgs m WHERE m.dialog_id=d.id AND m.role='admin')>=2", (now() - 86400,))
    pick = random.sample(list(rows), min(n, len(rows)))
    for x in pick:
        cur = run("INSERT INTO qc(dialog_id, admin_id, ts) VALUES(?,?,?)", (x["id"], x["admin_id"], now()))
        await qc_send(get("SELECT * FROM qc WHERE id=?", (cur.lastrowid,)))
    return len(pick)


@r.callback_query(F.data.startswith("qc:"))
async def cb_qc(c: CallbackQuery):
    _, qid, letter, score = c.data.split(":")
    uid = c.from_user.id
    if not can_qc(uid):
        await c.answer("🚫 Проверяет отдел обучения.", show_alert=True)
        return
    q = get("SELECT * FROM qc WHERE id=?", (int(qid),))
    col = {x[0]: x[1] for x in QC_FIELDS}.get(letter)
    if not q or q["status"] != "new" or not col or q[col] is not None or not score.isdigit():
        await c.answer("Уже оценено.", show_alert=True)
        return
    run(f"UPDATE qc SET {col}=?, reviewer=? WHERE id=?", (int(score), uid, q["id"]))
    q = get("SELECT * FROM qc WHERE id=?", (q["id"],))
    label, kb = qc_next(q)
    await c.answer("Принято")
    if kb:
        try:
            await c.message.edit_text(qc_text(q, f"{label}: оцените от 1 до 5"), reply_markup=kb)
        except Exception:
            pass
        return
    avg = (q["polite"] + q["speed"] + q["literacy"]) / 3
    run("UPDATE qc SET status='done', done_ts=? WHERE id=?", (now(), q["id"]))
    a = get("SELECT rating, rating_cnt FROM admins WHERE user_id=?", (q["admin_id"],))
    if a:
        cnt = a["rating_cnt"] + 1
        run("UPDATE admins SET rating=?, rating_cnt=? WHERE user_id=?",
            ((a["rating"] * a["rating_cnt"] + avg) / cnt, cnt, q["admin_id"]))
    await dm(q["admin_id"], f"📋 Проверка качества диалога #{q['dialog_id']}: вежливость {q['polite']}, "
                            f"скорость {q['speed']}, грамотность {q['literacy']}. Итог: <b>{avg:.1f}/5</b>. "
                            "Оценка учтена в рейтинге.")
    await alog(uid, q["admin_id"], "Проверка качества", f"диалог #{q['dialog_id']}: {avg:.1f}/5")
    try:
        await c.message.edit_text(qc_text(q) + f"\n✅ Итог: <b>{avg:.1f}/5</b> · проверил {tag(uid)}")
    except Exception:
        pass


def qc_summary(since):
    rows = all_("SELECT * FROM qc WHERE status='done' AND done_ts>=?", (since,))
    pend = get("SELECT COUNT(*) c FROM qc WHERE status='new'")["c"]
    if not rows and not pend:
        return ""
    t = "\n\n🎓 <b>Проверка качества</b>\n"
    if rows:
        avgs = [(x["polite"] + x["speed"] + x["literacy"]) / 3 for x in rows]
        t += f"Проверено: {len(rows)} · средняя: {sum(avgs) / len(avgs):.2f}/5\n"
        for x, a in zip(rows, avgs):
            t += f"• {tag(x['admin_id'])} — {a:.1f} (диалог #{x['dialog_id']})\n"
    if pend:
        t += f"⏳ Ждут проверки: {pend} (/qc)\n"
    return t


@r.message(Command("qc"))
async def cmd_qc(m: Message, command: CommandObject):
    if not await guard(m, "admin", "work"):
        return
    if not can_qc(m.from_user.id):
        await m.answer("🚫 Проверку качества ведёт отдел обучения.")
        return
    if (command.args or "").strip().lower() == "new":
        n = await qc_generate()
        await m.answer(f"✅ Выбрано диалогов для проверки: {n}." if n else "Подходящих закрытых диалогов за сутки нет.")
        return
    pend = all_("SELECT * FROM qc WHERE status='new' ORDER BY id LIMIT 5")
    if not pend:
        await m.answer("Нет диалогов, ожидающих проверки. Выбрать новые: <code>/qc new</code>")
        return
    for q in pend:
        label, kb = qc_next(q)
        await m.answer(qc_text(q, f"{label}: оцените от 1 до 5"), reply_markup=kb)


# ── ping, status, ошибки
def uptime_str():
    sec = max(0, now() - START_TS)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    return (f"{d} д " if d else "") + f"{h} ч {rem // 60} мин"


@r.message(Command("ping"))
async def cmd_ping(m: Message):
    k = chat_kind(m)
    if k is None or k == "info":
        return
    t0 = time.perf_counter()
    msg = await m.answer("🏓 Пинг…")
    ms = (time.perf_counter() - t0) * 1000
    text = (f"🏓 <b>Понг!</b> Бот работает.\n⏱ Время ответа: <b>{ms:.0f} мс</b> ({ms / 1000:.2f} с)\n"
            f"🕒 Работает без перезапуска: {uptime_str()}")
    if hasattr(msg, "edit_text"):
        try:
            await msg.edit_text(text)
            return
        except Exception:
            pass
    await m.answer(text)


@r.message(Command("status"))
async def cmd_status(m: Message):
    if not await guard(m, "status", "work"):
        return
    q = all_("SELECT dept, crisis, created FROM dialogs WHERE status='waiting'")
    oldest = max((now() - x["created"] for x in q), default=0)
    adm = all_("SELECT duty, current_dialog FROM admins WHERE level>=1")
    on = sum(1 for a in adm if a["duty"])
    busy = sum(1 for a in adm if a["current_dialog"])
    errs = get("SELECT COUNT(*) c FROM errors WHERE ts>=?", (now() - 86400,))["c"]
    last_err = get("SELECT ts, place FROM errors ORDER BY id DESC LIMIT 1")
    lb = setting("last_backup_ts")
    try:
        size = os.path.getsize(DB_PATH) / 1024 / 1024
    except OSError:
        size = 0
    open_bugs = get("SELECT COUNT(*) c FROM bugs WHERE status IN ('new','confirmed')")["c"]
    t = (f"🩺 <b>Состояние бота</b>\n\n🟢 Работает · {uptime_str()}\n"
         f"🗄 База: <b>{size:.2f} МБ</b>\n"
         f"📨 Очередь: <b>{len(q)}</b> (тех.: {sum(1 for x in q if x['dept'] == 'tech')}, "
         f"🚨 срочных: {sum(1 for x in q if x['crisis'])})"
         + (f", самое долгое ожидание {oldest // 60} мин." if q else "") + "\n"
         f"👮 Администраторов: {len(adm)} · на дежурстве {on} · в диалогах {busy}\n"
         f"💾 Последний бэкап: {fdt(int(lb)) if lb.isdigit() else 'ещё не делали'}\n"
         f"🛑 Ошибок за 24 ч: <b>{errs}</b>" + (f" (последняя: {fdt(last_err['ts'])}, {esc(last_err['place'])})" if last_err else "") + "\n"
         f"🐞 Открытых багов: {open_bugs}\n"
         f"🛠 Тех. работы: {'включены' if setting('maint') == '1' else 'выключены'}")
    await m.answer(t)


_err_seen = {}


async def report_error(place, exc):
    try:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        run("INSERT INTO errors(ts, place, text) VALUES(?,?,?)", (now(), place[:80], tb[-3000:]))
        sig = (place, type(exc).__name__, str(exc)[:80])
        if now() - _err_seen.get(sig, 0) < 60:
            return
        _err_seen[sig] = now()
        text = f"🛑 <b>Ошибка бота</b> · {esc(place)}\n<pre>{esc(tb[-3300:])}</pre>"
        if DEV_CHAT_ID:
            await dm(DEV_CHAT_ID, text)
        else:
            for d_ in sorted(DEV_IDS):
                await dm(d_, text)
    except Exception:
        logging.exception("report_error failed")


async def on_error(event: ErrorEvent):
    upd = getattr(event, "update", None)
    await report_error(f"update {getattr(upd, 'update_id', '?')}", event.exception)
    return True


# ── /export
EXPORT_PERIODS = {"день": 86400, "сутки": 86400, "day": 86400, "неделя": 7 * 86400, "week": 7 * 86400,
                  "месяц": 30 * 86400, "month": 30 * 86400, "год": 365 * 86400, "year": 365 * 86400,
                  "все": 0, "всё": 0, "all": 0}


@r.message(Command("export"))
async def cmd_export(m: Message, command: CommandObject):
    if not await guard(m, "export", "work"):
        return
    arg = (command.args or "неделя").strip().lower()
    if arg not in EXPORT_PERIODS:
        await m.answer("Использование: <code>/export [день|неделя|месяц|год|все]</code>")
        return
    sec = EXPORT_PERIODS[arg]
    since = now() - sec if sec else 0
    one = lambda sql, a=(): (get(sql, a)[0] or 0)  # noqa: E731
    label = {86400: "за день", 7 * 86400: "за неделю", 30 * 86400: "за месяц", 365 * 86400: "за год", 0: "за всё время"}[sec]
    out = [f"NEXUS — статистика {label}", f"Сформировано: {fdt(now())}", "", "== ОБЩЕЕ =="]
    out += [
        f"Пользователей всего: {one('SELECT COUNT(*) FROM users')}",
        f"Новых пользователей: {one('SELECT COUNT(*) FROM users WHERE first_seen>=?', (since,))}",
        f"Активных пользователей: {one('SELECT COUNT(DISTINCT sender) FROM msgs WHERE role=? AND ts>=?', ('user', since))}",
        f"Обращений: {one('SELECT COUNT(*) FROM dialogs WHERE created>=?', (since,))}",
        f"Закрыто диалогов: {one('SELECT COUNT(*) FROM dialogs WHERE status=? AND closed>=?', ('closed', since))}",
        f"Сообщений от людей: {one('SELECT COUNT(*) FROM msgs WHERE role=? AND ts>=?', ('user', since))}",
        f"Ответов администраторов: {one('SELECT COUNT(*) FROM msgs WHERE role=? AND ts>=?', ('admin', since))}",
        f"Кризисных обращений: {one('SELECT COUNT(*) FROM dialogs WHERE crisis=1 AND created>=?', (since,))}",
        f"Оценок: {one('SELECT COUNT(*) FROM feedback WHERE ts>=?', (since,))}, средняя: {one('SELECT AVG(stars) FROM feedback WHERE ts>=?', (since,)):.2f}",
        f"Оценок 5⭐: {one('SELECT COUNT(*) FROM feedback WHERE stars=5 AND ts>=?', (since,))}, плохих (1–3): {one('SELECT COUNT(*) FROM feedback WHERE stars<=3 AND ts>=?', (since,))}",
        f"Жалоб: {one('SELECT COUNT(*) FROM complaints WHERE ts>=?', (since,))}, обоснованных: {one('SELECT COUNT(*) FROM complaints WHERE status=? AND ts>=?', ('justified', since))}",
        f"Багов: {one('SELECT COUNT(*) FROM bugs WHERE ts>=?', (since,))}, исправлено: {one('SELECT COUNT(*) FROM bugs WHERE status=? AND closed>=?', ('fixed', since))}",
        f"Заявок на админку: {one('SELECT COUNT(*) FROM apps WHERE ts>=?', (since,))}",
        f"Проверок качества: {one('SELECT COUNT(*) FROM qc WHERE status=? AND done_ts>=?', ('done', since))}",
        f"Ошибок бота: {one('SELECT COUNT(*) FROM errors WHERE ts>=?', (since,))}",
        "", "== ПО АДМИНИСТРАТОРАМ ==",
        "админ | ур. | отделы | диалогов закрыто | ответов | средняя оценка | отзывов | 5⭐ | баллы | пред/выг | жалоб | качество",
    ]
    for a in all_("SELECT * FROM admins WHERE level>=1 ORDER BY level DESC, user_id"):
        uid = a["user_id"]
        fb = get("SELECT AVG(stars) a, COUNT(*) c, SUM(stars=5) f FROM feedback WHERE admin_id=? AND ts>=?", (uid, since))
        qa = get("SELECT AVG((polite+speed+literacy)/3.0) a FROM qc WHERE admin_id=? AND status='done' AND done_ts>=?", (uid, since))
        out.append(" | ".join([
            ptag(uid), str(a["level"]), dept_label(a["dept"]),
            str(one("SELECT COUNT(*) FROM dialogs WHERE admin_id=? AND status='closed' AND closed>=?", (uid, since))),
            str(one("SELECT COUNT(*) FROM msgs WHERE role='admin' AND sender=? AND ts>=?", (uid, since))),
            f"{fb['a']:.2f}" if fb["c"] else "-", str(fb["c"]), str(fb["f"] or 0), str(a["points"]),
            f"{a['warns']}/{a['vigs']}",
            str(one("SELECT COUNT(*) FROM complaints WHERE admin_id=? AND ts>=?", (uid, since))),
            f"{qa['a']:.2f}" if qa["a"] else "-"]))
    days = {}
    for sql, key in (("SELECT strftime('%Y-%m-%d', first_seen + 10800, 'unixepoch') d, COUNT(*) c FROM users WHERE first_seen>=? GROUP BY d", "users"),
                     ("SELECT strftime('%Y-%m-%d', created + 10800, 'unixepoch') d, COUNT(*) c FROM dialogs WHERE created>=? GROUP BY d", "dialogs"),
                     ("SELECT strftime('%Y-%m-%d', ts + 10800, 'unixepoch') d, COUNT(*) c FROM msgs WHERE role='admin' AND ts>=? GROUP BY d", "answers")):
        for x in all_(sql, (since,)):
            days.setdefault(x["d"], {})[key] = x["c"]
    out += ["", "== ПО ДНЯМ ==", "дата | новых пользователей | обращений | ответов админов"]
    for dkey in sorted(days):
        v = days[dkey]
        out.append(f"{dkey} | {v.get('users', 0)} | {v.get('dialogs', 0)} | {v.get('answers', 0)}")
    await alog(m.from_user.id, None, "Экспорт статистики (/export)", label)
    await m.answer_document(BufferedInputFile("\n".join(out).encode("utf-8"), filename=f"stats_{arg}.txt"),
                            caption=esc(f"📎 Статистика {label}"))


# ───────────────────────── v3: псевдонимы, наставники, заявки, тренажёр ─────────────────────────
def pseudo_error(new, uid):
    if (not 2 <= len(new) <= 32 or not re.fullmatch(r"[\w\-]+(?: [\w\-]+)*", new)
            or new.replace(" ", "").isdigit()):
        return "Псевдоним: 2–32 символа, буквы, цифры, «_» и «-», можно из нескольких слов."
    for x in all_("SELECT user_id, pseudo FROM admins WHERE pseudo IS NOT NULL"):
        if x["user_id"] != uid and x["pseudo"].casefold() == new.casefold():
            return "Такой псевдоним уже занят."
    return None


@r.message(Command("setpsevd"))
async def cmd_setpsevd(m: Message, command: CommandObject):
    if not await guard(m, "psevd", "any"):
        return
    p = (command.args or "").split(maxsplit=1)
    if len(p) < 2:
        await m.answer("Использование: <code>/setpsevd @юз псевдоним</code> (можно из нескольких слов).\n"
                       "Сам администратор сменить псевдоним не может.")
        return
    t = await target_or_fail(m, p[0], need_admin=True)
    if not t:
        return
    new = " ".join(p[1].split())
    err = pseudo_error(new, t["user_id"])
    if err:
        await m.answer("❌ " + err)
        return
    run("UPDATE admins SET pseudo=? WHERE user_id=?", (new, t["user_id"]))
    await alog(m.from_user.id, t["user_id"], "Смена псевдонима", new)
    await m.answer(f"✅ Псевдоним {tag(t['user_id'])}: <b>{esc(new)}</b>")
    await dm(t["user_id"], f"🎭 Вам установили псевдоним: <b>{esc(new)}</b> (его видят пользователи).")


def pick_mentor(newbie):
    cands = [a["user_id"] for a in all_("SELECT user_id, dept FROM admins WHERE level>=1")
             if has_dept(a, "edu") and a["user_id"] != newbie]
    if not cands:
        return None
    load = {c: get("SELECT COUNT(*) c FROM mentors WHERE mentor=? AND done=0", (c,))["c"] for c in cands}
    return min(cands, key=lambda c: load[c])


async def after_appoint(actor, uid, lvl):
    """Новый администратор: закрепляем наставника и просим назначающего выбрать псевдоним."""
    mentor = None if get("SELECT 1 FROM mentors WHERE newbie=?", (uid,)) else pick_mentor(uid)
    if mentor:
        run("INSERT INTO mentors(newbie, mentor, assigned_by, ts) VALUES(?,?,?,?)", (uid, mentor, actor, now()))
        await dm(mentor, f"🎓 Вы наставник нового администратора {tag(uid)}. Я пришлю его первые "
                         f"{MENTOR_DIALOGS} диалогов для обратной связи. /mentor")
        await dm(uid, f"🎓 Ваш наставник: {tag(mentor)}. Он посмотрит ваши первые диалоги и поможет советом.")
        note = f"\n🎓 Наставник: {tag(mentor)}"
    else:
        note = "\n🎓 Наставник не назначен: <code>/mentor @новичок @наставник</code>"
    set_input(actor, "pseudo", uid, 0, str(lvl))
    kb = InlineKeyboardBuilder()
    kb.button(text="🎲 Оставить случайный", callback_data=f"psd:{uid}")
    await dm(actor, f"🎭 Введите псевдоним для {tag(uid)} (его будут видеть пользователи) одним сообщением.\n"
                    f"Сейчас случайный: <b>{esc(pseudo_of(uid))}</b>. Отмена: /cancel{note}", reply_markup=kb.as_markup())


@r.callback_query(F.data.startswith("psd:"))
async def cb_psd(c: CallbackQuery):
    d = get("SELECT * FROM inputs WHERE user_id=? AND kind='pseudo'", (c.from_user.id,))
    if not d or str(d["ref"]) != c.data[4:]:
        await c.answer("Уже обработано.", show_alert=True)
        return
    clear_input(c.from_user.id)
    await c.answer("Оставили случайный")
    try:
        await c.message.edit_text(f"🎭 Псевдонимом остался случайный: <b>{esc(pseudo_of(d['ref']))}</b>.")
    except Exception:
        pass
    await offer_info(c.from_user.id, "appoint", d["ref"], None, int(d["data"] or 1))


async def in_pseudo(m: Message, d):
    new = " ".join(m.text.split())
    err = pseudo_error(new, d["ref"])
    if err:
        await m.answer(f"❌ {err}\nПопробуйте ещё раз или нажмите «Оставить случайный». Отмена: /cancel")
        return
    run("UPDATE admins SET pseudo=? WHERE user_id=?", (new, d["ref"]))
    clear_input(m.from_user.id)
    await alog(m.from_user.id, d["ref"], "Псевдоним при назначении", new)
    await m.answer(f"✅ Псевдоним: <b>{esc(new)}</b>")
    await dm(d["ref"], f"🎭 Ваш псевдоним: <b>{esc(new)}</b> (его видят пользователи).")
    await offer_info(m.from_user.id, "appoint", d["ref"], None, int(d["data"] or 1))


# ── транскрипт диалога
def dialog_transcript(d, reveal):
    user_ = tag(d["user_id"]) if reveal else anon_id(d["user_id"])
    adm = tag(d["admin_id"]) if d["admin_id"] else "—"
    head = (f"💬 <b>Диалог #{d['id']}</b> · {d['status']}\n👤 {user_}\n👮 {adm}\n"
            f"🕒 {fdt(d['created'])} → {fdt(d['closed'])}\n\n")
    lines = []
    for x in all_("SELECT * FROM msgs WHERE dialog_id=? ORDER BY id", (d["id"],)):
        who_ = "🧑" if x["role"] == "user" else "👮"
        body = x["text"] or f"[{x['ctype']}]"
        stamp = datetime.fromtimestamp(x["ts"], TZ).strftime("%d.%m %H:%M")
        lines.append(f"{who_} {stamp} {esc(body)}")
    return head + ("\n".join(lines) or "— сообщений нет —")


# ── наставники
async def notify_mentor(d):
    mt = get("SELECT * FROM mentors WHERE newbie=? AND done=0", (d["admin_id"],))
    if not mt:
        return
    n = get("SELECT COUNT(*) c FROM dialogs WHERE admin_id=? AND status='closed'", (d["admin_id"],))["c"]
    if n > MENTOR_DIALOGS:
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="👁 Посмотреть диалог", callback_data=f"mn:view:{d['id']}")
    kb.button(text="✍️ Обратная связь", callback_data=f"mn:note:{d['id']}")
    kb.adjust(1)
    await dm(mt["mentor"], f"🎓 Диалог новичка {tag(d['admin_id'])} #{d['id']} закрыт ({n}/{MENTOR_DIALOGS}).",
             reply_markup=kb.as_markup())


@r.callback_query(F.data.startswith("mn:"))
async def cb_mentor(c: CallbackQuery):
    _, act, did = c.data.split(":")
    d = get("SELECT * FROM dialogs WHERE id=?", (int(did),))
    mt = get("SELECT * FROM mentors WHERE newbie=?", (d["admin_id"],)) if d and d["admin_id"] else None
    uid = c.from_user.id
    if not mt or (mt["mentor"] != uid and not is_dev(uid)):
        await c.answer("🚫 Это не ваш подопечный.", show_alert=True)
        return
    if act == "view":
        await c.answer()
        await dm_long(uid, dialog_transcript(d, can_see_users(uid)))
    elif act == "note":
        if get("SELECT 1 FROM mentor_notes WHERE dialog_id=?", (d["id"],)):
            await c.answer("Обратная связь по этому диалогу уже дана.", show_alert=True)
            return
        set_input(uid, "mentor_note", d["id"])
        await c.answer("Напишите обратную связь в личные сообщения боту")
        await dm(uid, f"✍️ Напишите обратную связь по диалогу #{d['id']} одним сообщением. Отмена: /cancel")


async def in_mentor_note(m: Message, d):
    dlg = get("SELECT * FROM dialogs WHERE id=?", (d["ref"],))
    mt = get("SELECT * FROM mentors WHERE newbie=?", (dlg["admin_id"],)) if dlg else None
    clear_input(m.from_user.id)
    if not mt:
        await m.answer("Наставничество не найдено.")
        return
    run("INSERT INTO mentor_notes(newbie, mentor, dialog_id, text, ts) VALUES(?,?,?,?,?)",
        (mt["newbie"], m.from_user.id, dlg["id"], m.text[:1500], now()))
    reviewed = mt["reviewed"] + 1
    done = 1 if reviewed >= MENTOR_DIALOGS else 0
    run("UPDATE mentors SET reviewed=?, done=? WHERE newbie=?", (reviewed, done, mt["newbie"]))
    await dm(mt["newbie"], f"📝 Обратная связь от наставника {tag(m.from_user.id)} по диалогу #{dlg['id']}:\n\n{esc(m.text[:1500])}")
    await m.answer(f"✅ Отправлено ({reviewed}/{MENTOR_DIALOGS}).")
    if done:
        await dm(mt["newbie"], "🎉 Наставничество завершено: вы прошли первые диалоги. Спасибо наставнику!")
        await dm(m.from_user.id, f"🎓 Наставничество над {tag(mt['newbie'])} завершено.")
    await alog(m.from_user.id, mt["newbie"], "Обратная связь наставника", f"диалог #{dlg['id']}")


@r.message(Command("mentor"))
async def cmd_mentor(m: Message, command: CommandObject):
    if not await guard(m, "admin", "any"):
        return
    uid = m.from_user.id
    p = (command.args or "").split()
    if not p:
        my = get("SELECT * FROM mentors WHERE newbie=?", (uid,))
        mine = all_("SELECT * FROM mentors WHERE mentor=? AND done=0", (uid,))
        t = ""
        if my:
            state = " · завершено" if my["done"] else ""
            t += f"🎓 Ваш наставник: {tag(my['mentor'])} · обратная связь: {my['reviewed']}/{MENTOR_DIALOGS}{state}\n"
        if mine:
            t += "\n<b>Ваши подопечные:</b>\n"
            for x in mine:
                n = get("SELECT COUNT(*) c FROM dialogs WHERE admin_id=? AND status='closed'", (x["newbie"],))["c"]
                t += f"• {tag(x['newbie'])} — диалогов {n}, обратная связь {x['reviewed']}/{MENTOR_DIALOGS}\n"
        await m.answer(t or "У вас нет наставника и подопечных.\nНазначить: <code>/mentor @новичок @наставник</code> (нужен доступ mentor).")
        return
    if p[0].lower() == "off" and len(p) == 2:
        if not has_perm(uid, "mentor"):
            await m.answer("🚫 У вас нет доступа к назначению наставников.")
            return
        t = await target_or_fail(m, p[1], need_admin=True)
        if t:
            run("DELETE FROM mentors WHERE newbie=?", (t["user_id"],))
            await alog(uid, t["user_id"], "Снят наставник")
            await m.answer("✅ Наставник снят.")
        return
    if len(p) == 2:
        if not has_perm(uid, "mentor"):
            await m.answer("🚫 У вас нет доступа к назначению наставников.")
            return
        nb = await target_or_fail(m, p[0], need_admin=True)
        mt = await target_or_fail(m, p[1], need_admin=True) if nb else None
        if not nb or not mt:
            return
        if nb["user_id"] == mt["user_id"]:
            await m.answer("❌ Наставник и новичок должны быть разными людьми.")
            return
        run("INSERT OR REPLACE INTO mentors(newbie, mentor, assigned_by, ts, reviewed, done) VALUES(?,?,?,?,0,0)",
            (nb["user_id"], mt["user_id"], uid, now()))
        await dm(mt["user_id"], f"🎓 Вы наставник администратора {tag(nb['user_id'])}. Я пришлю его первые {MENTOR_DIALOGS} диалогов.")
        await dm(nb["user_id"], f"🎓 Ваш наставник: {tag(mt['user_id'])}.")
        await alog(uid, nb["user_id"], "Назначен наставник", tag(mt["user_id"]))
        await m.answer(f"✅ Наставник {tag(mt['user_id'])} закреплён за {tag(nb['user_id'])}.")
        return
    t = await target_or_fail(m, p[0], need_admin=True)
    if t:
        x = get("SELECT * FROM mentors WHERE newbie=?", (t["user_id"],))
        await m.answer(f"🎓 Наставник {tag(t['user_id'])}: {tag(x['mentor'])} · {x['reviewed']}/{MENTOR_DIALOGS}" if x
                       else "У этого администратора нет наставника.")


# ── заявки на админку
APP_QUESTIONS = [
    "📝 <b>1/5.</b> Сколько вам лет?",
    "⏰ <b>2/5.</b> Сколько часов в день вы сможете отвечать и в какое время (МСК)?",
    "💼 <b>3/5.</b> Есть ли опыт поддержки, модерации или волонтёрства? Расскажите.",
    "💬 <b>4/5.</b> Почему вы хотите стать администратором?",
    "🆘 <b>5/5.</b> Как вы поступите, если человек напишет, что хочет причинить себе вред?",
]
APP_LABELS = ("Возраст", "Время для ответов", "Опыт", "Почему хочет стать админом", "Действия при угрозе вреда себе")
APP_STATUS = {"new": "🆕 новая", "training": "🎯 на тренажёре", "accepted": "✅ принята",
              "rejected": "❌ отклонена", "granted": "🛡 админка выдана"}


def app_text(a):
    t = f"📋 <b>Заявка #{a['id']}</b> · {APP_STATUS.get(a['status'], a['status'])}\n👤 {tag(a['user_id'])} · {fdt(a['ts'])}\n"
    for label, key in zip(APP_LABELS, ("a1", "a2", "a3", "a4", "a5")):
        t += f"\n<b>{label}:</b> {esc(a[key])}"
    if a["note"]:
        t += f"\n\n💬 Комментарий: {esc(a['note'])}"
    if a["handler"]:
        t += f"\n✍️ {tag(a['handler'])}"
    return t


def app_kb(a):
    kb = InlineKeyboardBuilder()
    if a["status"] == "new":
        kb.button(text="🎯 Отправить на тренажёр", callback_data=f"app:{a['id']}:train")
    if a["status"] in ("new", "training"):
        kb.button(text="✅ Принять", callback_data=f"app:{a['id']}:ok")
        kb.button(text="❌ Отклонить", callback_data=f"app:{a['id']}:no")
    if a["status"] == "accepted":
        kb.button(text="🛡 Выдать админку (1 ур.)", callback_data=f"app:{a['id']}:grant")
    kb.adjust(1)
    return kb.as_markup()


@r.message(Command("apply"), F.chat.type == "private")
async def cmd_apply(m: Message):
    uid = m.from_user.id
    if is_admin(uid):
        await m.answer("Вы уже администратор 🙂")
        return
    if get("SELECT 1 FROM apps WHERE user_id=? AND status IN ('new','training','accepted')", (uid,)):
        await m.answer("Ваша заявка уже на рассмотрении. Мы сообщим о решении.")
        return
    rej = get("SELECT done_ts FROM apps WHERE user_id=? AND status='rejected' AND done_ts>?", (uid, now() - 7 * 86400))
    if rej:
        days = max(1, 7 - (now() - rej["done_ts"]) // 86400)
        await m.answer(f"Повторно подать заявку можно через {days} дн.")
        return
    set_input(uid, "apply", 0, 0, "[]")
    await m.answer("📋 <b>Заявка на должность администратора</b>\nОтветьте на 5 вопросов. Отмена: /cancel\n\n" + APP_QUESTIONS[0])


async def in_apply(m: Message, d):
    answers = json.loads(d["data"] or "[]") + [m.text[:700]]
    if len(answers) < 5:
        set_input(m.from_user.id, "apply", 0, len(answers), json.dumps(answers, ensure_ascii=False))
        await m.answer(APP_QUESTIONS[len(answers)])
        return
    clear_input(m.from_user.id)
    cur = run("INSERT INTO apps(user_id, a1, a2, a3, a4, a5, ts) VALUES(?,?,?,?,?,?,?)",
              (m.from_user.id, *answers, now()))
    a = get("SELECT * FROM apps WHERE id=?", (cur.lastrowid,))
    await m.answer(f"🙏 Спасибо! Заявка #{a['id']} отправлена. Мы рассмотрим её и сообщим о решении. "
                   "Возможно, вам предложат пройти тренажёр (/train).")
    chat = STAFF_CHAT_ID or LOG_CHAT_ID
    if chat:
        await dm_long(chat, app_text(a), reply_markup=app_kb(a))
    await alog(m.from_user.id, None, "Заявка на админку", f"#{a['id']}")


async def grant_from_app(actor, uid):
    if get("SELECT 1 FROM admins WHERE user_id=? AND level>=1", (uid,)):
        return False
    if get("SELECT 1 FROM admins WHERE user_id=?", (uid,)):
        run("UPDATE admins SET level=1 WHERE user_id=?", (uid,))
    else:
        run("INSERT INTO admins(user_id, level, dept, since) VALUES(?,1,'none',?)", (uid, now()))
    pseudo_of(uid)
    await alog(actor, uid, "Выдача админки по заявке", "уровень 1")
    await dm(uid, "🎉 Вам выдана админка Nexus, уровень 1. Команды: /help")
    await after_appoint(actor, uid, 1)
    return True


@r.callback_query(F.data.startswith("app:"))
async def cb_app(c: CallbackQuery):
    _, aid, act = c.data.split(":")
    uid = c.from_user.id
    a = get("SELECT * FROM apps WHERE id=?", (int(aid),))
    if not has_perm(uid, "apps"):
        await c.answer("🚫 Нет доступа", show_alert=True)
        return
    if not a:
        await c.answer("Заявка не найдена.", show_alert=True)
        return
    if act == "train" and a["status"] == "new":
        run("UPDATE apps SET status='training', handler=? WHERE id=?", (uid, a["id"]))
        await dm(a["user_id"], "🎯 Вам открыт тренажёр: пять учебных обращений. Начать: /train")
    elif act in ("ok", "no") and a["status"] in ("new", "training"):
        set_input(uid, "app_note", a["id"], 0, act)
        await c.answer("Напишите комментарий в личные сообщения боту")
        await dm(uid, f"✍️ Напишите комментарий к решению по заявке #{a['id']} "
                      f"({'принять' if act == 'ok' else 'отклонить'}) одним сообщением. «-» — без комментария. Отмена: /cancel")
        return
    elif act == "grant" and a["status"] == "accepted":
        if not has_perm(uid, "makeadmin"):
            await c.answer("🚫 Для выдачи админки нужен доступ makeadmin.", show_alert=True)
            return
        err = level_error(uid, a["user_id"], 1)
        if err:
            await c.answer(err[:190], show_alert=True)
            return
        if not await grant_from_app(uid, a["user_id"]):
            await c.answer("Он уже администратор.", show_alert=True)
            return
        run("UPDATE apps SET status='granted' WHERE id=?", (a["id"],))
    else:
        await c.answer("Уже обработано.", show_alert=True)
        return
    await c.answer("Готово")
    a = get("SELECT * FROM apps WHERE id=?", (a["id"],))
    try:
        await c.message.edit_text(app_text(a), reply_markup=app_kb(a))
    except Exception:
        pass


async def in_app_note(m: Message, d):
    a = get("SELECT * FROM apps WHERE id=?", (d["ref"],))
    clear_input(m.from_user.id)
    if not a or a["status"] not in ("new", "training"):
        await m.answer("Заявка уже обработана.")
        return
    note = "" if m.text.strip() == "-" else m.text.strip()[:500]
    ok = d["data"] == "ok"
    run("UPDATE apps SET status=?, handler=?, note=?, done_ts=? WHERE id=?",
        ("accepted" if ok else "rejected", m.from_user.id, note, now(), a["id"]))
    extra = f"\n\nКомментарий: {esc(note)}" if note else ""
    if ok:
        await dm(a["user_id"], "🎉 Ваша заявка на должность администратора принята! Администрация скоро свяжется с вами." + extra)
    else:
        await dm(a["user_id"], "К сожалению, заявка отклонена. Спасибо за интерес к Nexus. "
                               "Повторно подать заявку можно через 7 дней." + extra)
    a = get("SELECT * FROM apps WHERE id=?", (a["id"],))
    chat = STAFF_CHAT_ID or LOG_CHAT_ID
    if chat:
        await dm_long(chat, app_text(a), reply_markup=app_kb(a))
    await alog(m.from_user.id, a["user_id"], "Решение по заявке", f"#{a['id']}: {a['status']}")
    await m.answer("✅ Готово.")


@r.message(Command("apps"))
async def cmd_apps(m: Message, command: CommandObject):
    if not await guard(m, "apps", "bugs"):
        return
    arg = (command.args or "").strip().lstrip("#")
    if arg.isdigit():
        a = get("SELECT * FROM apps WHERE id=?", (int(arg),))
        if not a:
            await m.answer("❌ Заявка не найдена.")
            return
        await reply_long(m, app_text(a), reply_markup=app_kb(a))
        return
    rows = all_("SELECT * FROM apps WHERE status IN ('new','training','accepted') ORDER BY id")
    body = "\n".join(f"<code>#{x['id']}</code> {APP_STATUS[x['status']].split()[0]} {tag(x['user_id'])} · {fdt(x['ts'])}"
                     for x in rows) or "— нет —"
    await reply_long(m, f"📋 <b>Заявки на рассмотрении</b>\n\n{body}\n\nПодробно: <code>/apps номер</code>")


# ── тренажёр
SCENARIOS = [
    ("Мысли о самоповреждении", "Мне так плохо... хочу порезаться, не знаю, что делать.",
     "Спокойный тон, прямой вопрос о безопасности, не оставлять одного, позвать старших, не обещать тайну."),
    ("Одиночество", "Поговори со мной, пожалуйста. Мне не с кем поговорить.",
     "Тепло, активное слушание, открытые вопросы, без советов вроде «просто не думай»."),
    ("Просьба контакта", "Дай свой юз или номер, хочу общаться с тобой вне бота.",
     "Вежливо отказать, объяснить правила, остаться в боте, не раскрывать личные данные."),
    ("Агрессия", "Ты тупой бот, вы ничем не помогаете, зачем вы вообще нужны?",
     "Не принимать на свой счёт, не грубить, признать чувства, вернуть разговор к теме."),
    ("Несовершеннолетний", "Мне 13, родители ругают меня, я хочу сбежать из дома.",
     "Бережно, безопасность, предложить обратиться к доверенному взрослому, не советовать побег."),
    ("Суицидальные мысли", "Я устал жить. Наверное, завтра всё закончится.",
     "Прямой вопрос о суициде, есть ли план, кризис-протокол, /transfer, не оставлять одного."),
    ("Угроза другому", "Я хочу ударить одноклассника, он меня достал.",
     "Выслушать, снизить накал, безопасность, не одобрять насилие."),
    ("Ты человек?", "Ты вообще живой человек? Расскажи про себя.",
     "Честно, не раскрывать личные данные, вернуть фокус на собеседнике."),
    ("Просьба о способе вреда", "Подскажи, чем можно навредить себе, чтобы никто не заметил?",
     "Отказать в информации, проявить заботу, кризис-протокол, позвать старших."),
    ("Реклама и спам", "Заходи на мой канал, там дешёвые услуги! Переходи по ссылке.",
     "Вежливо напомнить о правилах, не вступать в обсуждение рекламы."),
]
TRAIN_COUNT = 5


def scenario_text(tr):
    idxs = json.loads(tr["scen"])
    step = len(json.loads(tr["answers"] or "[]"))
    title, text, _ = SCENARIOS[idxs[step]]
    return (f"🎯 <b>Тренажёр {step + 1}/{len(idxs)}</b> — {esc(title)}\n\n🧑 Пользователь пишет:\n"
            f"<i>«{esc(text)}»</i>\n\n✍️ Напишите, как вы ответите, одним сообщением. Отмена: /cancel")


@r.message(Command("train"), F.chat.type == "private")
async def cmd_train(m: Message):
    uid = m.from_user.id
    if not is_admin(uid) and not get("SELECT 1 FROM apps WHERE user_id=? AND status IN ('new','training','accepted')", (uid,)):
        await m.answer("Тренажёр доступен кандидатам (подайте заявку: /apply) и администраторам.")
        return
    tr = get("SELECT * FROM trainer WHERE user_id=? AND status='active'", (uid,))
    if not tr:
        app = get("SELECT id FROM apps WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,))
        cur = run("INSERT INTO trainer(user_id, app_id, scen, answers, ts) VALUES(?,?,?,?,?)",
                  (uid, app["id"] if app else None,
                   json.dumps(random.sample(range(len(SCENARIOS)), TRAIN_COUNT)), "[]", now()))
        tr = get("SELECT * FROM trainer WHERE id=?", (cur.lastrowid,))
        await m.answer("🎯 <b>Тренажёр</b>: пять учебных обращений. Отвечайте так, как ответили бы живому человеку. "
                       "Результат оценит тех. администратор.")
    set_input(uid, "train", tr["id"])
    await m.answer(scenario_text(tr))


async def in_train(m: Message, d):
    tr = get("SELECT * FROM trainer WHERE id=?", (d["ref"],))
    if not tr or tr["status"] != "active":
        clear_input(m.from_user.id)
        await m.answer("Сессия тренажёра закрыта. Начать заново: /train")
        return
    answers = json.loads(tr["answers"] or "[]") + [m.text[:700]]
    run("UPDATE trainer SET answers=? WHERE id=?", (json.dumps(answers, ensure_ascii=False), tr["id"]))
    tr = get("SELECT * FROM trainer WHERE id=?", (tr["id"],))
    idxs = json.loads(tr["scen"])
    if len(answers) < len(idxs):
        await m.answer(scenario_text(tr))
        return
    clear_input(m.from_user.id)
    run("UPDATE trainer SET status='review' WHERE id=?", (tr["id"],))
    await m.answer("🙏 Спасибо! Ответы отправлены на проверку. Результат придёт сюда.")
    parts = [f"🎯 <b>Тренажёр #{tr['id']}</b> · {tag(tr['user_id'])}" + (f" · заявка #{tr['app_id']}" if tr["app_id"] else "")]
    for i, (idx, ans) in enumerate(zip(idxs, answers), 1):
        title, text, hint = SCENARIOS[idx]
        parts.append(f"\n<b>{i}. {esc(title)}</b>\n🧑 <i>{esc(text)}</i>\n✍️ {esc(ans)}\n<i>Критерии: {esc(hint)}</i>")
    kb = InlineKeyboardBuilder()
    for i in range(1, 6):
        kb.button(text=f"{i}⭐", callback_data=f"trn:{tr['id']}:{i}")
    kb.adjust(5)
    chat = STAFF_CHAT_ID or LOG_CHAT_ID
    if chat:
        await dm_long(chat, "\n".join(parts) + "\n\n<b>Оцените работу (4–5 = зачёт):</b>", reply_markup=kb.as_markup())


@r.callback_query(F.data.startswith("trn:"))
async def cb_trn(c: CallbackQuery):
    _, tid, score = c.data.split(":")
    uid = c.from_user.id
    tr = get("SELECT * FROM trainer WHERE id=?", (int(tid),))
    if not has_perm(uid, "apps"):
        await c.answer("🚫 Нет доступа", show_alert=True)
        return
    if not tr or tr["status"] != "review":
        await c.answer("Уже оценено.", show_alert=True)
        return
    run("UPDATE trainer SET status='grading', reviewer=? WHERE id=?", (uid, tr["id"]))
    set_input(uid, "train_note", tr["id"], 0, score)
    await c.answer("Напишите комментарий в личные сообщения боту")
    await dm(uid, f"✍️ Напишите комментарий к оценке {score}/5 по тренажёру #{tr['id']} одним сообщением. «-» — без комментария.")


async def in_train_note(m: Message, d):
    tr = get("SELECT * FROM trainer WHERE id=?", (d["ref"],))
    clear_input(m.from_user.id)
    if not tr or tr["status"] != "grading":
        await m.answer("Эта проверка уже закрыта.")
        return
    score = int(d["data"] or 0)
    note = "" if m.text.strip() == "-" else m.text.strip()[:500]
    run("UPDATE trainer SET status='done', score=?, note=?, done_ts=? WHERE id=?", (score, note, now(), tr["id"]))
    passed = score >= 4
    extra = f"\n\nКомментарий: {esc(note)}" if note else ""
    if passed:
        await dm(tr["user_id"], f"🎉 Тренажёр пройден: <b>{score}/5</b>. Зачёт!" + extra)
    else:
        await dm(tr["user_id"], f"Тренажёр: <b>{score}/5</b>. Пока без зачёта. Попробуйте ещё раз: /train" + extra)
    chat = STAFF_CHAT_ID or LOG_CHAT_ID
    if chat:
        await dm(chat, f"🎯 Тренажёр #{tr['id']} ({tag(tr['user_id'])}): <b>{score}/5</b> — "
                       f"{'зачёт' if passed else 'без зачёта'} · оценил {tag(m.from_user.id)}"
                       + (f" · заявка #{tr['app_id']}: <code>/apps {tr['app_id']}</code>" if tr["app_id"] else ""))
    await alog(m.from_user.id, tr["user_id"], "Оценка тренажёра", f"#{tr['id']}: {score}/5")
    await m.answer("✅ Готово.")


# ── двойной контроль ЧС и апелляции
def can_confirm_ban(uid):
    return is_dev(uid) or has_dept(get_admin(uid), "main")


async def do_fullban(actor, uid, reason):
    run("UPDATE users SET banned=1, ban_reason=?, ban_notified=0, appealed=0 WHERE user_id=?", (reason, uid))
    await end_for(uid)
    clear_input(uid)
    await alog(actor, uid, "Полный ЧС (/fullban)", reason)


BAN_STATUS = {"new": "🆕 ждёт решения", "approved": "✅ подтверждена", "rejected": "❌ отклонена"}


def ban_text(b):
    t = (f"⛔ <b>Заявка на ЧС #{b['id']}</b> · {BAN_STATUS.get(b['status'], b['status'])}\n"
         f"👤 Кого: {tag(b['target'])}\n✍️ Подал: {tag(b['requester'])}\n📝 Причина: {esc(b['reason'])}")
    if b["by_id"]:
        t += f"\n🔎 Решил: {tag(b['by_id'])}"
    return t


def ban_kb(b):
    kb = InlineKeyboardBuilder()
    if b["status"] == "new":
        kb.button(text="✅ Подтвердить ЧС", callback_data=f"ban:{b['id']}:ok")
        kb.button(text="❌ Отклонить", callback_data=f"ban:{b['id']}:no")
    kb.adjust(2)
    return kb.as_markup()


@r.callback_query(F.data.startswith("ban:"))
async def cb_ban(c: CallbackQuery):
    _, rid, act = c.data.split(":")
    uid = c.from_user.id
    if not can_confirm_ban(uid):
        await c.answer("🚫 Решает главная администрация.", show_alert=True)
        return
    b = get("SELECT * FROM ban_requests WHERE id=?", (int(rid),))
    if not b or b["status"] != "new":
        await c.answer("Уже обработано.", show_alert=True)
        return
    if act == "ok":
        if is_dev(b["target"]):
            await c.answer("🛡 Вписанных в код банить нельзя.", show_alert=True)
            return
        await do_fullban(uid, b["target"], b["reason"])
    run("UPDATE ban_requests SET status=?, by_id=?, done_ts=? WHERE id=?",
        ("approved" if act == "ok" else "rejected", uid, now(), b["id"]))
    b = get("SELECT * FROM ban_requests WHERE id=?", (b["id"],))
    verdict = "подтверждена" if act == "ok" else "отклонена главной администрацией"
    await dm(b["requester"], f"{'⛔' if act == 'ok' else '❌'} Заявка на ЧС #{b['id']} {verdict}.")
    await c.answer("Готово")
    try:
        await c.message.edit_text(ban_text(b), reply_markup=ban_kb(b))
    except Exception:
        pass


APPEAL_STATUS = {"new": "🆕 новая", "approved": "✅ ЧС снят", "rejected": "❌ отказано"}


def appeal_text(a):
    t = f"🙏 <b>Апелляция #{a['id']}</b> · {APPEAL_STATUS.get(a['status'], a['status'])}\n👤 {tag(a['user_id'])} · {fdt(a['ts'])}\n"
    u = get("SELECT ban_reason FROM users WHERE user_id=?", (a["user_id"],))
    if u and u["ban_reason"]:
        t += f"⛔ Причина ЧС: {esc(u['ban_reason'])}\n"
    t += f"\n{esc(a['text'])}"
    if a["by_id"]:
        t += f"\n\n✍️ Решил: {tag(a['by_id'])}"
    return t


def appeal_kb(a):
    kb = InlineKeyboardBuilder()
    if a["status"] == "new":
        kb.button(text="✅ Снять ЧС", callback_data=f"ap:{a['id']}:ok")
        kb.button(text="❌ Отказать", callback_data=f"ap:{a['id']}:no")
    kb.adjust(2)
    return kb.as_markup()


async def banned_flow(event, u):
    """Забаненные: бот молчит, кроме одного уведомления и одной просьбы о пересмотре (/appeal)."""
    if isinstance(event, CallbackQuery):
        await event.answer()
        return
    if getattr(event.chat, "type", "") != "private":
        return
    text = (event.text or "").strip()
    row = get("SELECT ban_notified, appealed FROM users WHERE user_id=?", (u.id,))
    inp = get("SELECT * FROM inputs WHERE user_id=? AND kind='appeal'", (u.id,))
    if text.startswith("/cancel"):
        clear_input(u.id)
        await event.answer("Хорошо, отменено.")
        return
    if text.startswith("/appeal"):
        if row and row["appealed"]:
            await event.answer("Вы уже отправили просьбу о пересмотре. Дождитесь решения.")
            return
        set_input(u.id, "appeal")
        await event.answer("✍️ Одним сообщением напишите, почему вы считаете блокировку ошибочной. Отмена: /cancel")
        return
    if inp and text and not text.startswith("/"):
        cur = run("INSERT INTO appeals(user_id, text, ts) VALUES(?,?,?)", (u.id, text[:1500], now()))
        clear_input(u.id)
        run("UPDATE users SET appealed=1 WHERE user_id=?", (u.id,))
        await event.answer("Спасибо. Мы передали вашу просьбу на рассмотрение. Решение придёт сюда.")
        a = get("SELECT * FROM appeals WHERE id=?", (cur.lastrowid,))
        chat = STAFF_CHAT_ID or LOG_CHAT_ID
        if chat:
            await dm(chat, appeal_text(a), reply_markup=appeal_kb(a))
        await alog(u.id, None, "Апелляция из ЧС", f"#{a['id']}")
        return
    if row and not row["ban_notified"]:
        run("UPDATE users SET ban_notified=1 WHERE user_id=?", (u.id,))
        await event.answer("Доступ к боту ограничен. Если вы считаете это ошибкой, отправьте /appeal.")


@r.callback_query(F.data.startswith("ap:"))
async def cb_appeal(c: CallbackQuery):
    _, aid, act = c.data.split(":")
    uid = c.from_user.id
    if not has_perm(uid, "appeals"):
        await c.answer("🚫 Нет доступа", show_alert=True)
        return
    a = get("SELECT * FROM appeals WHERE id=?", (int(aid),))
    if not a or a["status"] != "new":
        await c.answer("Уже обработано.", show_alert=True)
        return
    if act == "ok":
        run("UPDATE users SET banned=0, ban_reason=NULL, ban_notified=0, appealed=0 WHERE user_id=?", (a["user_id"],))
        await dm(a["user_id"], "✅ Блокировка снята. Добро пожаловать обратно: /start")
    else:
        await dm(a["user_id"], "Мы рассмотрели вашу просьбу: решение осталось прежним.")
    run("UPDATE appeals SET status=?, by_id=?, done_ts=? WHERE id=?",
        ("approved" if act == "ok" else "rejected", uid, now(), a["id"]))
    await alog(uid, a["user_id"], "Решение по апелляции", f"#{a['id']}: {act}")
    a = get("SELECT * FROM appeals WHERE id=?", (a["id"],))
    await c.answer("Готово")
    try:
        await c.message.edit_text(appeal_text(a), reply_markup=appeal_kb(a))
    except Exception:
        pass


@r.message(Command("appeals"))
async def cmd_appeals(m: Message):
    if not await guard(m, "appeals", "bugs"):
        return
    rows = all_("SELECT * FROM appeals WHERE status='new' ORDER BY id")
    if not rows:
        await m.answer("Новых апелляций нет.")
        return
    for a in rows[:10]:
        await m.answer(appeal_text(a), reply_markup=appeal_kb(a))


# ───────────────────────── v3: база знаний, ревизия доступов, отчёты, тревога очереди ─────────────────────────
def can_kb(uid):
    return is_dev(uid) or has_dept(get_admin(uid), "edu") or has_perm(uid, "kb")


def kb_article(x):
    return f"📚 <b>{x['id']}. {esc(x['title'])}</b>\n\n{esc(x['text'])}"


@r.message(Command("kb"))
async def cmd_kb(m: Message, command: CommandObject):
    if not await guard(m, "admin", "any"):
        return
    arg = (command.args or "").strip()
    rows = all_("SELECT * FROM kb ORDER BY id")
    if not arg:
        body = "\n".join(f"<code>{x['id']}</code> {esc(x['title'])}" for x in rows) or "— статей пока нет —"
        await reply_long(m, f"📚 <b>База знаний</b>\n\n{body}\n\nПоиск: <code>/kb слово</code> · статья: <code>/kb номер</code>")
        return
    if arg.lstrip("#").isdigit():
        x = get("SELECT * FROM kb WHERE id=?", (int(arg.lstrip("#")),))
        await reply_long(m, kb_article(x) if x else "❌ Статья не найдена.")
        return
    words = arg.casefold().split()
    found = [x for x in rows if all(w in f"{x['title']} {x['text']}".casefold() for w in words)]
    if not found:
        await m.answer("Ничего не найдено. Список статей: /kb")
    elif len(found) == 1:
        await reply_long(m, kb_article(found[0]))
    else:
        body = "\n".join(f"<code>{x['id']}</code> {esc(x['title'])}" for x in found[:20])
        await reply_long(m, f"🔎 Найдено статей: {len(found)}\n\n{body}\n\nОткрыть: <code>/kb номер</code>")


@r.message(Command("kbadd"))
async def cmd_kbadd(m: Message, command: CommandObject):
    if not await guard(m, "admin", "any"):
        return
    if not can_kb(m.from_user.id):
        await m.answer("🚫 Базу знаний ведёт отдел обучения (или доступ kb).")
        return
    if "|" not in (command.args or ""):
        await m.answer("Использование: <code>/kbadd название | текст статьи</code>")
        return
    title, body = (x.strip() for x in command.args.split("|", 1))
    if not title or not body:
        await m.answer("Использование: <code>/kbadd название | текст статьи</code>")
        return
    cur = run("INSERT INTO kb(title, text, by_id, ts) VALUES(?,?,?,?)", (title[:100], body[:3000], m.from_user.id, now()))
    await alog(m.from_user.id, None, "База знаний: статья добавлена", title[:80])
    await m.answer(f"✅ Статья №{cur.lastrowid} добавлена. Поиск: <code>/kb слово</code>")


@r.message(Command("kbedit"))
async def cmd_kbedit(m: Message, command: CommandObject):
    if not await guard(m, "admin", "any"):
        return
    if not can_kb(m.from_user.id):
        await m.answer("🚫 Базу знаний ведёт отдел обучения (или доступ kb).")
        return
    if "|" not in (command.args or ""):
        await m.answer("Использование: <code>/kbedit номер | новый текст</code>")
        return
    num, body = (x.strip() for x in command.args.split("|", 1))
    num = num.lstrip("#")
    x = get("SELECT * FROM kb WHERE id=?", (int(num),)) if num.isdigit() else None
    if not x or not body:
        await m.answer("❌ Статья не найдена или пустой текст.")
        return
    run("UPDATE kb SET text=? WHERE id=?", (body[:3000], x["id"]))
    await alog(m.from_user.id, None, "База знаний: статья изменена", x["title"][:80])
    await m.answer(f"✅ Статья №{x['id']} обновлена.")


@r.message(Command("kbdel"))
async def cmd_kbdel(m: Message, command: CommandObject):
    if not await guard(m, "admin", "any"):
        return
    if not can_kb(m.from_user.id):
        await m.answer("🚫 Базу знаний ведёт отдел обучения (или доступ kb).")
        return
    arg = (command.args or "").strip().lstrip("#")
    x = get("SELECT * FROM kb WHERE id=?", (int(arg),)) if arg.isdigit() else None
    if not x:
        await m.answer("Использование: <code>/kbdel номер</code>")
        return
    run("DELETE FROM kb WHERE id=?", (x["id"],))
    await alog(m.from_user.id, None, "База знаний: статья удалена", x["title"][:80])
    await m.answer(f"🗑 Статья «{esc(x['title'])}» удалена.")


# ── ревизия доступов
def access_rows():
    out = []
    for a in all_("SELECT * FROM admins WHERE level>=1 ORDER BY level DESC, user_id"):
        uid = a["user_id"]
        if is_dev(uid):
            continue
        ps = all_("SELECT perm, granted_ts FROM perms WHERE user_id=?", (uid,))
        if not ps:
            continue
        used, unused = [], []
        for p in ps:
            u = get("SELECT last_ts FROM perm_use WHERE user_id=? AND perm=?", (uid, p["perm"]))
            base = p["granted_ts"] or a["since"] or 0
            if u and now() - u["last_ts"] < ACCESS_DAYS * 86400:
                used.append((p["perm"], u["last_ts"]))
            elif not u and now() - base < ACCESS_DAYS * 86400:
                used.append((p["perm"], None))
            else:
                unused.append((p["perm"], u["last_ts"] if u else None))
        answers = get("SELECT COUNT(*) c FROM msgs WHERE role='admin' AND sender=? AND ts>=?",
                      (uid, now() - ACCESS_DAYS * 86400))["c"]
        out.append({"uid": uid, "used": used, "unused": unused, "inactive": answers == 0})
    return out


def access_line(x):
    def label(p):
        return f"{p[0]} ({ago(p[1])} назад)" if p[1] else f"{p[0]} (не использовался)"
    t = f"• {tag(x['uid'])}" + (" 💤 не отвечал 30 дней" if x["inactive"] else "")
    if x["used"]:
        t += "\n   ✅ " + ", ".join(label(p) if p[1] else p[0] for p in x["used"])
    if x["unused"]:
        t += "\n   ⚠️ лишнее: " + ", ".join(label(p) for p in x["unused"])
    return t


def access_kb(rows):
    kb = InlineKeyboardBuilder()
    for x in [y for y in rows if y["unused"]][:8]:
        u = get("SELECT username, name FROM users WHERE user_id=?", (x["uid"],))
        who_ = "@" + u["username"] if u and u["username"] else str(x["uid"])
        kb.button(text=f"🗑 Снять лишнее у {who_}", callback_data=f"acr:{x['uid']}")
    kb.adjust(1)
    return kb.as_markup()


def access_text(rows):
    t = f"🔐 <b>Ревизия доступов</b> (порог: {ACCESS_DAYS} дней без использования)\n\n"
    t += "\n".join(access_line(x) for x in rows) or "— выданных доступов нет —"
    n = sum(1 for x in rows if x["unused"])
    t += f"\n\nЛишние доступы у {n} админ. Кнопки ниже показывают первых 8, остальных: <code>/access @юз</code>."
    return t


@r.message(Command("access"))
async def cmd_access(m: Message, command: CommandObject):
    if not await guard(m, "access", "logs"):
        return
    rows = access_rows()
    arg = (command.args or "").strip()
    if arg:
        t = await target_or_fail(m, arg, need_admin=True)
        if not t:
            return
        rows = [x for x in rows if x["uid"] == t["user_id"]]
        if not rows:
            await m.answer("У этого администратора нет выданных доступов.")
            return
    await alog(m.from_user.id, None, "Ревизия доступов (/access)")
    await reply_long(m, access_text(rows), reply_markup=access_kb(rows))


@r.callback_query(F.data.startswith("acr:"))
async def cb_acr(c: CallbackQuery):
    uid = c.from_user.id
    target = int(c.data[4:])
    if not has_perm(uid, "access"):
        await c.answer("🚫 Нет доступа", show_alert=True)
        return
    err = level_error(uid, target)
    if err:
        await c.answer(err[:190], show_alert=True)
        return
    row = next((x for x in access_rows() if x["uid"] == target), None)
    if not row or not row["unused"]:
        await c.answer("Лишних доступов нет.", show_alert=True)
        return
    names = [p[0] for p in row["unused"]]
    for p in names:
        run("DELETE FROM perms WHERE user_id=? AND perm=?", (target, p))
    await alog(uid, target, "Сняты неиспользуемые доступы", ", ".join(names))
    await dm(target, "🔐 По итогам ревизии сняты неиспользуемые доступы: " + ", ".join(names))
    await c.answer(f"Снято: {len(names)}", show_alert=True)


async def send_access_report():
    if not STAFF_CHAT_ID:
        return
    rows = access_rows()
    await dm_long(STAFF_CHAT_ID, "📅 <b>Ежемесячная ревизия доступов</b>\n\n" + access_text(rows), reply_markup=access_kb(rows))


# ── тревога: большая очередь
async def queue_alert():
    chat = INFO_CHAT_ID or ADMIN_CHAT_ID
    if not chat:
        return
    rows = all_("SELECT dept, crisis, created FROM dialogs WHERE status='waiting'")
    if not rows:
        return
    oldest = max(now() - x["created"] for x in rows)
    if len(rows) < QUEUE_ALERT and oldest < QUEUE_WAIT_ALERT * 60:
        return
    last = setting("last_queue_alert", "0")
    if last.isdigit() and now() - int(last) < QUEUE_ALERT_COOLDOWN * 60:
        return
    run("INSERT OR REPLACE INTO settings(key,value) VALUES('last_queue_alert',?)", (str(now()),))
    tech = sum(1 for x in rows if x["dept"] == "tech")
    urgent = sum(1 for x in rows if x["crisis"])
    await dm(chat, f"🚨 <b>Очередь слишком большая! Берите тикеты!</b>\n📨 В очереди: <b>{len(rows)}</b> "
                   f"(🛠 тех.: {tech}, 🚨 срочных: {urgent})\n⏳ Самое долгое ожидание: {oldest // 60} мин.\n/queue")


# ── недельный отчёт
WEEK_METRICS = (
    ("dialogs", "📨 Обращений", 1), ("answers", "💬 Ответов админов", 1), ("rating", "⭐ Средняя оценка", 1),
    ("new_users", "🆕 Новых пользователей", 1), ("complaints", "🚩 Жалоб", -1), ("bad", "📉 Плохих оценок", -1),
    ("bugs", "🐞 Новых багов", -1), ("fixed", "🛠 Исправлено багов", 1), ("qc", "🎓 Качество (проверки)", 1),
    ("errors", "🛑 Ошибок бота", -1),
)


def week_metrics(a, b):
    def one(sql, args=()):
        return get(sql, args)[0] or 0
    return {
        "dialogs": one("SELECT COUNT(*) FROM dialogs WHERE created>=? AND created<?", (a, b)),
        "answers": one("SELECT COUNT(*) FROM msgs WHERE role='admin' AND ts>=? AND ts<?", (a, b)),
        "rating": one("SELECT AVG(stars) FROM feedback WHERE ts>=? AND ts<?", (a, b)),
        "new_users": one("SELECT COUNT(*) FROM users WHERE first_seen>=? AND first_seen<?", (a, b)),
        "complaints": one("SELECT COUNT(*) FROM complaints WHERE ts>=? AND ts<?", (a, b)),
        "bad": one("SELECT COUNT(*) FROM feedback WHERE stars<=3 AND ts>=? AND ts<?", (a, b)),
        "bugs": one("SELECT COUNT(*) FROM bugs WHERE ts>=? AND ts<?", (a, b)),
        "fixed": one("SELECT COUNT(*) FROM bugs WHERE status='fixed' AND closed>=? AND closed<?", (a, b)),
        "qc": one("SELECT AVG((polite+speed+literacy)/3.0) FROM qc WHERE status='done' AND done_ts>=? AND done_ts<?", (a, b)),
        "errors": one("SELECT COUNT(*) FROM errors WHERE ts>=? AND ts<?", (a, b)),
    }


def weekly_text():
    t0 = now()
    cur = week_metrics(t0 - 7 * 86400, t0)
    prev = week_metrics(t0 - 14 * 86400, t0 - 7 * 86400)
    lines, better, worse = [], [], []
    for key, label, sign in WEEK_METRICS:
        c_, p_ = cur[key], prev[key]
        diff = c_ - p_
        arrow = "▲" if diff > 1e-9 else "▼" if diff < -1e-9 else "•"
        val = f"{c_:.2f}" if key in ("rating", "qc") else str(int(c_))
        dtxt = f"{diff:+.2f}" if key in ("rating", "qc") else f"{int(diff):+d}"
        lines.append(f"{label}: <b>{val}</b> ({arrow} {dtxt})")
        if diff * sign > 1e-9:
            better.append(label.split(' ', 1)[1])
        elif diff * sign < -1e-9:
            worse.append(label.split(' ', 1)[1])
    cnt = lambda sql: get(sql)[0]  # noqa: E731
    todo = []
    for n, text in (
        (cnt("SELECT COUNT(*) FROM complaints WHERE status IN ('new','work')"), "жалоб ждут разбора — /complaints"),
        (cnt("SELECT COUNT(*) FROM bad_reviews WHERE decision IS NULL"), "плохих оценок без решения"),
        (cnt("SELECT COUNT(*) FROM bugs WHERE status IN ('new','confirmed')"), "открытых багов — /bugs"),
        (cnt("SELECT COUNT(*) FROM apps WHERE status IN ('new','training','accepted')"), "заявок на админку — /apps"),
        (cnt("SELECT COUNT(*) FROM appeals WHERE status='new'"), "апелляций из ЧС — /appeals"),
        (cnt("SELECT COUNT(*) FROM ban_requests WHERE status='new'"), "заявок на ЧС ждут главную администрацию"),
        (cnt("SELECT COUNT(*) FROM qc WHERE status='new'"), "проверок качества не завершены — /qc"),
    ):
        if n:
            todo.append(f"• {n} {text}")
    low = sum(1 for a in all_("SELECT user_id FROM admins WHERE level>=1")
              if not is_dev(a["user_id"]) and get("SELECT COUNT(*) c FROM msgs WHERE role='admin' AND sender=? AND ts>=?",
                                                  (a["user_id"], t0 - 7 * 86400))["c"] < LOW_ACTIVITY_ANSWERS * 3)
    if low:
        todo.append(f"• {low} админ. с низкой активностью за неделю")
    t = "📆 <b>Недельный отчёт</b>\n\n" + "\n".join(lines)
    t += "\n\n✅ <b>Улучшилось:</b> " + (", ".join(better) or "—")
    t += "\n⚠️ <b>Ухудшилось:</b> " + (", ".join(worse) or "—")
    t += "\n\n🎯 <b>Что делаем дальше:</b>\n" + ("\n".join(todo) or "• всё под контролем")
    t += "\n\nОбсудите и запишите задачи кнопкой ниже (список задач: /works в беседе главной администрации)."
    return t


async def send_weekly():
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Записать задачу", callback_data="wk:add")
    text = weekly_text()
    for chat in {c for c in (STAFF_CHAT_ID, MAIN_CHAT_ID) if c}:
        await dm_long(chat, text, reply_markup=kb.as_markup())


@r.callback_query(F.data == "wk:add")
async def cb_work_add(c: CallbackQuery):
    if not has_perm(c.from_user.id, "admin"):
        await c.answer("🚫 Только для администраторов.", show_alert=True)
        return
    set_input(c.from_user.id, "work_add")
    await c.answer("Напишите задачу в личные сообщения боту")
    await dm(c.from_user.id, "📝 Напишите задачу одним сообщением. Она попадёт в /works. Отмена: /cancel")


async def in_work_add(m: Message, d):
    clear_input(m.from_user.id)
    cur = run("INSERT INTO works(text, status, by_id, ts) VALUES(?,'open',?,?)", (m.text[:500], m.from_user.id, now()))
    await alog(m.from_user.id, None, "Добавлена задача (/works)", m.text[:100])
    await m.answer(f"✅ Задача #{cur.lastrowid} записана в /works.")


@r.message(Command("weekly"))
async def cmd_weekly(m: Message):
    if not await guard(m, "status", "work"):
        return
    await reply_long(m, weekly_text())


# ── ввод текста после кнопок и форм
INPUT_HANDLERS = {
    "pseudo": in_pseudo, "mentor_note": in_mentor_note, "apply": in_apply, "app_note": in_app_note,
    "train": in_train, "train_note": in_train_note, "work_add": in_work_add,
}


async def generic_input(m: Message):
    d = get("SELECT * FROM inputs WHERE user_id=?", (m.from_user.id,))
    if not d or not m.text:
        return False
    h = INPUT_HANDLERS.get(d["kind"])
    if not h:
        clear_input(m.from_user.id)
        return False
    await h(m, d)
    return True


# ───────────────────────── ПСЕВДОНИМЫ, ТЕКСТЫ, ТЕХ. РАБОТЫ ─────────────────────────
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
    if await bag_input(m) or await info_input(m) or await generic_input(m):
        return
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
        cur = run("INSERT INTO complaints(dialog_id, admin_id, user_id, text, ts) VALUES(?,?,?,?,?)",
                  (did, d2["admin_id"] if d2 else None, uid, m.text[:2000], now()))
        run("UPDATE users SET pending_review=0 WHERE user_id=?", (uid,))
        await m.answer("Спасибо, мы получили вашу жалобу и обязательно разберём её. 💙")
        target = STAFF_CHAT_ID or LOG_CHAT_ID
        if target and d2:
            cr = get("SELECT * FROM complaints WHERE id=?", (cur.lastrowid,))
            await dm(target, complaint_text(cr), reply_markup=cpl_kb(cr))
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
            if fb["stars"] <= 3 and (STAFF_CHAT_ID or LOG_CHAT_ID):
                await dm(STAFF_CHAT_ID or LOG_CHAT_ID,
                         f"💬 Отзыв к плохой оценке (диалог #{did}, {fb['stars']}⭐): {esc(m.text[:1000])}")
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
    t += qc_summary(since)
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
    run("INSERT OR REPLACE INTO settings(key,value) VALUES('last_backup_ts',?)", (str(now()),))


async def daily_jobs():
    t = datetime.now(TZ)
    today = t.strftime("%Y-%m-%d")
    if t.hour >= SUMMARY_HOUR and setting("last_summary") != today:
        run("INSERT OR REPLACE INTO settings(key,value) VALUES('last_summary',?)", (today,))
        await send_summary()
        await send_audit()
    if t.hour >= BACKUP_HOUR and setting("last_backup") != today:
        run("INSERT OR REPLACE INTO settings(key,value) VALUES('last_backup',?)", (today,))
        await send_backup()
    if t.hour >= QC_HOUR and setting("last_qc") != today:
        run("INSERT OR REPLACE INTO settings(key,value) VALUES('last_qc',?)", (today,))
        await qc_generate()
    if t.weekday() == 0 and t.hour >= WEEKLY_HOUR and setting("last_weekly") != today:
        run("INSERT OR REPLACE INTO settings(key,value) VALUES('last_weekly',?)", (today,))
        await send_weekly()
    if t.day == 1 and t.hour >= SUMMARY_HOUR and setting("last_access") != today[:7]:
        run("INSERT OR REPLACE INTO settings(key,value) VALUES('last_access',?)", (today[:7],))
        await send_access_report()


async def scheduler():
    while True:
        try:
            await silence_check()
            await queue_alert()
            await daily_jobs()
        except Exception as e:
            logging.exception("scheduler error")
            await report_error("scheduler", e)
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
    dp.errors.register(on_error)
    await BOT.delete_webhook(drop_pending_updates=True)
    for _a in all_("SELECT user_id FROM admins WHERE level>=1"):
        pseudo_of(_a["user_id"])
    asyncio.create_task(scheduler())
    await dp.start_polling(BOT)


if __name__ == "__main__":
    asyncio.run(main())
