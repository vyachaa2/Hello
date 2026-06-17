"""
Highrise Designer Privilege Bot
================================
Запуск:
    highrise bot:Bot <ROOM_ID> <API_TOKEN>

Требования:
    pip install highrise-bot-sdk==25.1.0
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

from highrise import BaseBot, RoomPermissions
from highrise.models import (
    AnchorPosition,
    CurrencyItem,
    Item,
    Position,
    SessionMetadata,
    User,
)

# ============================================================
#  НАСТРОЙКИ (меняйте здесь)
# ============================================================
FREE_DESIGNER: bool = True          # True — бесплатно; False — за золото
DESIGNER_COST: int = 100            # Цена в золоте (игнорируется при FREE_DESIGNER=True)
DESIGNER_DURATION: int = 60         # Длительность привилегии в секундах
MIN_ACCOUNT_AGE_DAYS: int = 7       # Минимальный возраст аккаунта в днях
WARNING_SECONDS: int = 30           # За сколько секунд до конца предупреждать
REQUEST_COOLDOWN: int = 300         # Кулдаун (сек) между запросами "+" для одного юзера
DEFAULT_ROT_INTERVAL: int = 120     # Интервал ротации сообщений по умолчанию (сек)
REQUIRE_INBOX: bool = True          # True — пользователь должен написать боту в inbox
REQUIRE_POST_COMMENT: bool = False  # True — пользователь должен прокомментировать публикацию
# ============================================================

DB_PATH = "bot_data.db"
VALID_FACINGS = {"FrontRight", "FrontLeft", "BackRight", "BackLeft"}
GOLD_TYPES = {"gold", "earned_gold"}

# Разделы admin-панели
ADMIN_SECTIONS = {
    "1": "designer",
    "2": "rotation",
    "3": "greetings",
    "4": "spawns",
    "5": "outfit",
    "6": "inbox_sec",
    "7": "settings",
    "8": "stats",
    "9": "post",
    "10": "moderation",
    "11": "voice",
    "12": "room",
}

# ═══════════════════════════════════════════════════════════
#  База данных
# ═══════════════════════════════════════════════════════════

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id          TEXT PRIMARY KEY,
            username         TEXT NOT NULL,
            total_time_sec   INTEGER DEFAULT 0,
            messages_count   INTEGER DEFAULT 0,
            designer_count   INTEGER DEFAULT 0,
            is_blacklisted   INTEGER DEFAULT 0,
            blacklist_reason TEXT,
            blacklisted_by   TEXT,
            has_messaged_bot INTEGER DEFAULT 0,
            last_conv_id     TEXT,
            last_seen        TEXT
        )
    """)

    for col_sql in [
        "ALTER TABLE users ADD COLUMN has_messaged_bot INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN last_conv_id TEXT",
    ]:
        try:
            c.execute(col_sql)
        except Exception:
            pass

    c.execute("""
        CREATE TABLE IF NOT EXISTS designer_sessions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          TEXT NOT NULL,
            username         TEXT NOT NULL,
            granted_at       TEXT NOT NULL,
            timer_started_at TEXT,
            expires_at       TEXT,
            cost_paid        INTEGER DEFAULT 0,
            is_active        INTEGER DEFAULT 1,
            granted_by       TEXT DEFAULT 'bot'
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bot_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS rotating_messages (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            message   TEXT NOT NULL,
            is_active INTEGER DEFAULT 1
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS greetings (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            message   TEXT NOT NULL,
            is_active INTEGER DEFAULT 1
        )
    """)

    conn.commit()
    conn.close()


def db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


# ── Пользователи ──────────────────────────────────────────

def upsert_user(user_id: str, username: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (user_id, username),
        )
        conn.execute(
            "UPDATE users SET username = ?, last_seen = ? WHERE user_id = ?",
            (username, datetime.now(timezone.utc).isoformat(), user_id),
        )


def get_user_row(user_id: str) -> Optional[dict]:
    with db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_name(username: str) -> Optional[dict]:
    with db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def mark_inbox(user_id: str, conv_id: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE users SET has_messaged_bot = 1, last_conv_id = ? WHERE user_id = ?",
            (conv_id, user_id),
        )


# ── Сессии дизайнера ──────────────────────────────────────

def get_active_session(user_id: str) -> Optional[dict]:
    with db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM designer_sessions WHERE user_id = ? AND is_active = 1 "
            "ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def create_session(user_id: str, username: str, cost_paid: int, granted_by: str) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO designer_sessions (user_id, username, granted_at, cost_paid, granted_by) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, datetime.now(timezone.utc).isoformat(), cost_paid, granted_by),
        )
        conn.execute(
            "UPDATE users SET designer_count = designer_count + 1 WHERE user_id = ?",
            (user_id,),
        )
        return cur.lastrowid


def start_session_timer(session_id: int, duration: int) -> datetime:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=duration)
    with db() as conn:
        conn.execute(
            "UPDATE designer_sessions SET timer_started_at = ?, expires_at = ? WHERE id = ?",
            (now.isoformat(), expires.isoformat(), session_id),
        )
    return expires


def close_session(session_id: int) -> None:
    with db() as conn:
        conn.execute("UPDATE designer_sessions SET is_active = 0 WHERE id = ?", (session_id,))


def close_all_user_sessions(user_id: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE designer_sessions SET is_active = 0 WHERE user_id = ? AND is_active = 1",
            (user_id,),
        )


# ── Настройки ─────────────────────────────────────────────

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with db() as conn:
        row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value)
        )


def del_setting(key: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM bot_settings WHERE key = ?", (key,))


# ── Ротируемые сообщения ──────────────────────────────────

def rot_add(message: str) -> int:
    with db() as conn:
        return conn.execute(
            "INSERT INTO rotating_messages (message) VALUES (?)", (message,)
        ).lastrowid


def rot_remove(msg_id: int) -> bool:
    with db() as conn:
        return conn.execute(
            "UPDATE rotating_messages SET is_active = 0 WHERE id = ?", (msg_id,)
        ).rowcount > 0


def rot_list() -> list[dict]:
    with db() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT id, message, is_active FROM rotating_messages ORDER BY id"
        ).fetchall()]


def rot_active_messages() -> list[str]:
    with db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT message FROM rotating_messages WHERE is_active = 1 ORDER BY id"
        ).fetchall()]


# ── Приветствия ───────────────────────────────────────────

def greet_add(message: str) -> int:
    with db() as conn:
        return conn.execute(
            "INSERT INTO greetings (message) VALUES (?)", (message,)
        ).lastrowid


def greet_remove(msg_id: int) -> bool:
    with db() as conn:
        return conn.execute(
            "UPDATE greetings SET is_active = 0 WHERE id = ?", (msg_id,)
        ).rowcount > 0


def greet_list() -> list[dict]:
    with db() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT id, message, is_active FROM greetings ORDER BY id"
        ).fetchall()]


def greet_active_messages() -> list[str]:
    with db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT message FROM greetings WHERE is_active = 1 ORDER BY id"
        ).fetchall()]


# ── Позиции ───────────────────────────────────────────────

def parse_position(args: str) -> Optional[Position]:
    parts = args.strip().split()
    if len(parts) < 3:
        return None
    try:
        x, y, z = float(parts[0]), float(parts[1]), float(parts[2])
        facing = parts[3] if len(parts) > 3 and parts[3] in VALID_FACINGS else "FrontRight"
        return Position(x, y, z, facing)
    except ValueError:
        return None


def pos_to_str(p: Position) -> str:
    return f"{p.x} {p.y} {p.z} {p.facing}"


def str_to_pos(s: str) -> Optional[Position]:
    return parse_position(s)


# ── Форматирование ────────────────────────────────────────

def fmt_time(seconds: int) -> str:
    h, rem = divmod(max(0, int(seconds)), 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h: parts.append(f"{h}ч")
    if m: parts.append(f"{m}мин")
    if s or not parts: parts.append(f"{s}сек")
    return " ".join(parts)


def fmt_price() -> str:
    return "бесплатно" if FREE_DESIGNER else f"{DESIGNER_COST} зол."


# ── Разбор команды ────────────────────────────────────────

def parse_cmd(msg: str) -> tuple[str, str]:
    s = msg.strip().lstrip("!")
    parts = s.split(None, 1)
    cmd = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    return cmd, rest


# ═══════════════════════════════════════════════════════════
#  Бот
# ═══════════════════════════════════════════════════════════

class Bot(BaseBot):

    def __init__(self) -> None:
        init_db()
        self.room_owner_id: str = ""
        self.join_times: dict[str, datetime] = {}
        self.designer_timers: dict[str, asyncio.Task] = {}
        self.pending_gold: dict[str, int] = {}
        self.last_request: dict[str, datetime] = {}
        self._rot_task: Optional[asyncio.Task] = None
        self._rot_index: int = 0
        self._users_in_room: set[str] = set()
        self._user_convs: dict[str, str] = {}
        # Admin inbox panel state: user_id → current section name
        self._admin_state: dict[str, str] = {}
        self._move_whisper_cooldown: dict[str, datetime] = {}

    # ── Запуск ────────────────────────────────────────────

    async def on_start(self, session_metadata: SessionMetadata) -> None:
        global DESIGNER_COST, FREE_DESIGNER
        self.room_owner_id = session_metadata.room_info.owner_id
        print(f"[BOT] Подключён к '{session_metadata.room_info.room_name}' "
              f"(owner={self.room_owner_id})")

        saved_cost = get_setting("designer_cost")
        saved_free = get_setting("free_designer")
        if saved_cost is not None:
            DESIGNER_COST = int(saved_cost)
        if saved_free is not None:
            FREE_DESIGNER = (saved_free == "1")

        try:
            resp = await self.highrise.get_room_users()
            if hasattr(resp, "content"):
                self._users_in_room = {
                    u.id for u, _ in resp.content if u.id != self.highrise.my_id
                }
        except Exception:
            pass

        spawn_str = get_setting("bot_spawn")
        if spawn_str:
            pos = str_to_pos(spawn_str)
            if pos:
                try:
                    await self.highrise.walk_to(pos)
                except Exception as exc:
                    print(f"[BOT] spawn ошибка: {exc}")

        mode = "бесплатно" if FREE_DESIGNER else f"{DESIGNER_COST} золота"
        await self.highrise.chat(
            f"🎨 Бот дизайнера онлайн! "
            f"Напишите '+' чтобы получить привилегию ({mode}, {DESIGNER_DURATION} сек)."
        )

        self._start_rotation()

    # ── Вход / Выход ──────────────────────────────────────

    async def on_user_join(self, user: User, position: Position | AnchorPosition) -> None:
        upsert_user(user.id, user.username)
        self.join_times[user.id] = datetime.now(timezone.utc)
        self._users_in_room.add(user.id)

        await self._send_greeting(user)

        user_spawn_str = get_setting("user_spawn")
        if user_spawn_str:
            pos = str_to_pos(user_spawn_str)
            if pos:
                try:
                    await asyncio.sleep(1.5)
                    await self.highrise.teleport(user.id, pos)
                except Exception as exc:
                    print(f"[BOT] teleport {user.username}: {exc}")

        session = get_active_session(user.id)
        if not session:
            return

        if session["timer_started_at"] is None:
            start_session_timer(session["id"], DESIGNER_DURATION)
            remaining = DESIGNER_DURATION
            await self.highrise.send_whisper(
                user.id,
                f"⏳ Таймер дизайнера запущен! У вас {fmt_time(remaining)}. "
                f"Привилегия будет снята автоматически.",
            )
        else:
            expires_at = datetime.fromisoformat(session["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                await self._revoke_designer(user.id, user.username, session["id"])
                return
            await self.highrise.send_whisper(
                user.id,
                f"⏳ Ваша привилегия дизайнера: осталось {fmt_time(int(remaining))}.",
            )

        self._cancel_timer(user.id)
        task = asyncio.create_task(
            self._designer_timer_task(user.id, user.username, session["id"], int(remaining))
        )
        self.designer_timers[user.id] = task

    async def on_whisper(self, user: User, message: str) -> None:
        upsert_user(user.id, user.username)
        if await self._is_staff(user.id):
            await self._handle_staff_command(user, message.strip())

    async def on_moderate(
        self,
        moderator_id: str,
        target_user_id: str,
        moderation_type: str,
        duration: int | None,
    ) -> None:
        labels = {
            "kick": "выгнал",
            "ban": "забанил",
            "unban": "разбанил",
            "mute": "замутил",
            "unmute": "размутил",
        }
        action = labels.get(moderation_type, moderation_type)
        dur = f" на {fmt_time(duration)}" if duration else ""
        print(f"[MOD] {moderator_id} {action} {target_user_id}{dur}")

    async def on_voice_change(
        self,
        users: list,
        seconds_left: int,
    ) -> None:
        print(f"[VOICE] голос изменён, осталось {seconds_left}с., участников: {len(users)}")

    async def on_user_leave(self, user: User) -> None:
        self._users_in_room.discard(user.id)
        self._move_whisper_cooldown.pop(user.id, None)
        if user.id in self.join_times:
            elapsed = int(
                (datetime.now(timezone.utc) - self.join_times.pop(user.id)).total_seconds()
            )
            with db() as conn:
                conn.execute(
                    "UPDATE users SET total_time_sec = total_time_sec + ? WHERE user_id = ?",
                    (elapsed, user.id),
                )

    # ── Движение ──────────────────────────────────────────

    async def on_user_move(self, user: User, position: Position | AnchorPosition) -> None:
        req_inbox = get_setting("require_inbox", "1" if REQUIRE_INBOX else "0")
        if req_inbox != "1":
            return

        row = get_user_row(user.id)
        if row and row.get("has_messaged_bot", 0):
            return

        user_spawn_str = get_setting("user_spawn")
        if not user_spawn_str:
            return
        pos = str_to_pos(user_spawn_str)
        if not pos:
            return

        try:
            await self.highrise.teleport(user.id, pos)
        except Exception as exc:
            print(f"[BOT] on_user_move teleport {user.username}: {exc}")
            return

        try:
            await self.highrise.send_whisper(
                user.id,
                "📩 Чтобы передвигаться по комнате, сначала напишите боту любое сообщение в ЛС.",
            )
        except Exception:
            pass

    # ── Чат ───────────────────────────────────────────────

    async def on_chat(self, user: User, message: str) -> None:
        upsert_user(user.id, user.username)
        with db() as conn:
            conn.execute(
                "UPDATE users SET messages_count = messages_count + 1 WHERE user_id = ?",
                (user.id,),
            )

        msg = message.strip()

        if await self._is_staff(user.id):
            if await self._handle_staff_command(user, msg):
                return

        if msg == "+":
            await self._handle_designer_request(user)

    # ── Inbox ─────────────────────────────────────────────

    async def on_message(
        self, user_id: str, conversation_id: str, is_new_conversation: bool
    ) -> None:
        self._user_convs[user_id] = conversation_id

        with db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
                (user_id, "unknown"),
            )
            conn.execute(
                "UPDATE users SET has_messaged_bot = 1, last_conv_id = ? WHERE user_id = ?",
                (conversation_id, user_id),
            )

        # Admin panel для владельца
        if user_id == self.room_owner_id:
            await self._handle_admin_inbox(user_id, conversation_id, is_new_conversation)
            return

        # Обычный пользователь
        if is_new_conversation:
            try:
                mode = "бесплатно" if FREE_DESIGNER else f"{DESIGNER_COST} зол."
                await self.highrise.send_message(
                    conversation_id,
                    "👋 Привет! Теперь ты можешь передвигаться по комнате.",
                )
            except Exception as exc:
                print(f"[BOT] inbox reply error: {exc}")

    # ── Чаевые ────────────────────────────────────────────

    async def on_tip(self, sender: User, receiver: User, tip: CurrencyItem | Item) -> None:
        if receiver.id != self.highrise.my_id:
            return
        if FREE_DESIGNER:
            return
        if not isinstance(tip, CurrencyItem) or tip.type not in GOLD_TYPES:
            await self.highrise.send_whisper(
                sender.id, "ℹ️ Для оплаты принимается только золото."
            )
            return

        upsert_user(sender.id, sender.username)
        accumulated = self.pending_gold.get(sender.id, 0) + tip.amount
        self.pending_gold[sender.id] = accumulated

        if accumulated >= DESIGNER_COST:
            self.pending_gold.pop(sender.id, None)
            if get_active_session(sender.id):
                await self.highrise.send_whisper(
                    sender.id,
                    f"✅ У вас уже есть активная привилегия! "
                    f"Излишек ({accumulated - DESIGNER_COST} зол.) не возвращается.",
                )
                return
            await self._grant_designer(sender, cost_paid=DESIGNER_COST, granted_by="payment")
        else:
            need = DESIGNER_COST - accumulated
            await self.highrise.send_whisper(
                sender.id,
                f"💰 Получено {tip.amount} зол. Накоплено: {accumulated}/{DESIGNER_COST}. "
                f"Осталось: {need} зол.",
            )

    # ═══════════════════════════════════════════════════════
    #  Логика дизайнера
    # ═══════════════════════════════════════════════════════

    async def _handle_designer_request(self, user: User) -> None:
        row = get_user_row(user.id)

        if row and row["is_blacklisted"]:
            await self.highrise.send_whisper(user.id, "🚫 Вы в чёрном списке.")
            return

        if get_active_session(user.id):
            await self.highrise.send_whisper(user.id, "✅ У вас уже есть активная привилегия!")
            return

        # Проверка inbox
        req_inbox = get_setting("require_inbox", "1" if REQUIRE_INBOX else "0")
        if req_inbox == "1":
            has_msg = row and row.get("has_messaged_bot", 0)
            if not has_msg:
                await self.highrise.send_whisper(
                    user.id,
                    "📩 Сначала напишите боту личное сообщение (inbox) — затем снова напишите '+'.",
                )
                return

        # Проверка комментария под публикацией
        req_post = get_setting("require_post_comment", "1" if REQUIRE_POST_COMMENT else "0")
        if req_post == "1":
            post_id = get_setting("required_post_id", "")
            if post_id:
                has_commented = await self._check_post_comment(user.id, post_id)
                if not has_commented:
                    await self.highrise.send_whisper(
                        user.id,
                        f"📌 Для получения привилегии сначала оставьте комментарий под этой публикацией:\n"
                        f"https://high.rs/post?id={post_id}",
                    )
                    return

        # Кулдаун
        last = self.last_request.get(user.id)
        if last:
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            if elapsed < REQUEST_COOLDOWN:
                wait = int(REQUEST_COOLDOWN - elapsed)
                await self.highrise.send_whisper(
                    user.id, f"⏱ Подождите {fmt_time(wait)} перед следующим запросом."
                )
                return
        self.last_request[user.id] = datetime.now(timezone.utc)

        # Возраст аккаунта
        age_days = await self._get_account_age_days(user.id)
        if age_days is not None and age_days <= MIN_ACCOUNT_AGE_DAYS:
            await self.highrise.send_whisper(
                user.id,
                f"❌ Аккаунт слишком новый ({age_days} дн.). "
                f"Необходимо более {MIN_ACCOUNT_AGE_DAYS} дней.",
            )
            return

        if FREE_DESIGNER:
            await self._grant_designer(user, cost_paid=0, granted_by="bot")
        else:
            pending = self.pending_gold.get(user.id, 0)
            need = DESIGNER_COST - pending
            await self.highrise.send_whisper(
                user.id,
                f"💰 Отправьте {need} золота боту для получения привилегии. "
                f"(Накоплено: {pending} зол.)",
            )

    async def _grant_designer(self, user: User, *, cost_paid: int, granted_by: str) -> None:
        session_id = create_session(user.id, user.username, cost_paid, granted_by)
        try:
            await self.highrise.change_room_privilege(user.id, RoomPermissions(designer=True))
        except Exception as exc:
            print(f"[BOT] grant designer {user.username}: {exc}")
            close_session(session_id)
            await self.highrise.send_whisper(
                user.id, "❌ Не удалось выдать привилегию. Попробуйте позже."
            )
            return

        await self.highrise.send_whisper(
            user.id,
            f"🎨 Привилегия дизайнера выдана на {fmt_time(DESIGNER_DURATION)}!\n"
            f"Перезайдите — таймер запустится когда вы вернётесь.",
        )
        await self.highrise.chat(f"🎨 {user.username} получил привилегию дизайнера!")

    async def _revoke_designer(self, user_id: str, username: str, session_id: int) -> None:
        close_session(session_id)
        self._cancel_timer(user_id)
        try:
            await self.highrise.change_room_privilege(user_id, RoomPermissions(designer=False))
        except Exception as exc:
            print(f"[BOT] revoke designer {username}: {exc}")
        try:
            await self.highrise.send_whisper(user_id, "⌛ Ваша привилегия дизайнера истекла.")
        except Exception:
            pass
        await self.highrise.chat(f"🎨 Привилегия дизайнера {username} истекла.")

    async def _designer_timer_task(
        self, user_id: str, username: str, session_id: int, duration: int
    ) -> None:
        if duration > WARNING_SECONDS:
            await asyncio.sleep(duration - WARNING_SECONDS)
            try:
                await self.highrise.send_whisper(
                    user_id,
                    f"⚠️ Привилегия дизайнера истечёт через {fmt_time(WARNING_SECONDS)}!",
                )
            except Exception:
                pass
            await asyncio.sleep(WARNING_SECONDS)
        else:
            await asyncio.sleep(duration)
        await self._revoke_designer(user_id, username, session_id)

    def _cancel_timer(self, user_id: str) -> None:
        task = self.designer_timers.pop(user_id, None)
        if task and not task.done():
            task.cancel()

    async def _check_post_comment(self, user_id: str, post_id: str) -> bool:
        """Возвращает True если user_id оставил комментарий под post_id."""
        try:
            resp = await self.webapi.get_post(post_id)
            post = resp.post
            return any(c.author_id == user_id for c in post.comments)
        except Exception as exc:
            print(f"[BOT] post comment check {user_id}: {exc}")
            return True  # при ошибке API не блокируем

    # ═══════════════════════════════════════════════════════
    #  Ротация сообщений
    # ═══════════════════════════════════════════════════════

    def _start_rotation(self) -> None:
        if self._rot_task and not self._rot_task.done():
            self._rot_task.cancel()
        self._rot_task = asyncio.create_task(self._rotation_loop())

    async def _rotation_loop(self) -> None:
        while True:
            interval = int(get_setting("rot_interval", str(DEFAULT_ROT_INTERVAL)))
            await asyncio.sleep(interval)

            if not self._users_in_room:
                continue
            if get_setting("rot_enabled", "1") != "1":
                continue

            messages = rot_active_messages()
            if not messages:
                continue

            self._rot_index = self._rot_index % len(messages)
            text = messages[self._rot_index].replace("{price}", fmt_price())
            self._rot_index += 1

            try:
                await self.highrise.chat(text)
            except Exception as exc:
                print(f"[BOT] rotation error: {exc}")

    # ═══════════════════════════════════════════════════════
    #  Приветствия
    # ═══════════════════════════════════════════════════════

    async def _send_greeting(self, user: User) -> None:
        if get_setting("greet_enabled", "1") != "1":
            return
        msgs = greet_active_messages()
        if not msgs:
            return
        text = random.choice(msgs).replace("{username}", user.username)
        try:
            await self.highrise.send_whisper(user.id, text)
        except Exception:
            pass

    # ═══════════════════════════════════════════════════════
    #  Роутер команд персонала (чат комнаты)
    # ═══════════════════════════════════════════════════════

    async def _is_staff(self, user_id: str) -> bool:
        if user_id == self.room_owner_id:
            return True
        try:
            priv = await self.highrise.get_room_privilege(user_id)
            if isinstance(priv, RoomPermissions):
                return priv.moderator is True
        except Exception:
            pass
        return False

    async def _handle_staff_command(self, mod: User, msg: str) -> bool:
        cmd, rest = parse_cmd(msg)
        is_owner = (mod.id == self.room_owner_id)

        match cmd:
            case "help":
                await self._cmd_help(mod); return True
            case "wallet":
                await self._cmd_wallet(mod); return True
            case "stats":
                await self._cmd_stats(mod); return True
            case "top":
                await self._cmd_top(mod); return True
            case "pending":
                await self._cmd_pending(mod); return True
            case "blacklist":
                await self._cmd_show_blacklist(mod); return True

            case "history" | "hist":
                limit = int(rest) if rest.isdigit() else 10
                await self._cmd_history(mod, limit); return True

            case "give":
                await self._cmd_give(mod, rest.lstrip("@")); return True
            case "take":
                await self._cmd_take(mod, rest.lstrip("@")); return True
            case "ban":
                parts = rest.split(None, 1)
                await self._cmd_ban(mod, parts[0].lstrip("@"), parts[1] if len(parts) > 1 else "")
                return True
            case "unban":
                await self._cmd_unban(mod, rest.lstrip("@")); return True
            case "profile":
                await self._cmd_profile(mod, rest.lstrip("@")); return True

            case "setcost":
                if is_owner:
                    await self._cmd_setcost(mod, rest)
                else:
                    await self.highrise.send_whisper(mod.id, "❌ Только владелец.")
                return True

            case "setbotspawn":
                await self._cmd_set_spawn(mod, "bot_spawn", rest); return True
            case "clearbotspawn":
                del_setting("bot_spawn")
                await self.highrise.send_whisper(mod.id, "✅ Точка появления бота сброшена.")
                return True
            case "setuserspawn":
                await self._cmd_set_spawn(mod, "user_spawn", rest); return True
            case "clearuserspawn":
                del_setting("user_spawn")
                await self.highrise.send_whisper(mod.id, "✅ Точка появления пользователей сброшена.")
                return True
            case "spawns":
                await self._cmd_show_spawns(mod); return True
            case "botgo":
                await self._cmd_bot_go_spawn(mod); return True

            case "copyoutfit":
                await self._cmd_copy_outfit(mod, rest.lstrip("@")); return True
            case "additem":
                await self._cmd_add_item(mod, rest); return True
            case "removeitem":
                await self._cmd_remove_item(mod, rest); return True
            case "botoutfit":
                await self._cmd_show_outfit(mod); return True

            case "addmsg":
                await self._cmd_addmsg(mod, rest); return True
            case "removemsg":
                if rest.isdigit():
                    await self._cmd_removemsg(mod, int(rest))
                else:
                    await self.highrise.send_whisper(mod.id, "❌ Формат: removemsg <ID>")
                return True
            case "listmsg":
                await self._cmd_listmsg(mod); return True
            case "setinterval":
                if rest.isdigit() and int(rest) >= 10:
                    set_setting("rot_interval", rest)
                    self._start_rotation()
                    await self.highrise.send_whisper(mod.id, f"✅ Интервал: {rest} сек.")
                else:
                    await self.highrise.send_whisper(mod.id, "❌ Формат: setinterval <сек, мин. 10>")
                return True
            case "roton":
                set_setting("rot_enabled", "1")
                self._start_rotation()
                await self.highrise.send_whisper(mod.id, "✅ Ротация включена."); return True
            case "rotoff":
                set_setting("rot_enabled", "0")
                await self.highrise.send_whisper(mod.id, "✅ Ротация выключена."); return True

            case "addgreet":
                await self._cmd_addgreet(mod, rest); return True
            case "removegreet":
                if rest.isdigit():
                    ok = greet_remove(int(rest))
                    msg_text = f"✅ Приветствие #{rest} удалено." if ok else f"❌ Приветствие #{rest} не найдено."
                    await self.highrise.send_whisper(mod.id, msg_text)
                else:
                    await self.highrise.send_whisper(mod.id, "❌ Формат: removegreet <ID>")
                return True
            case "listgreet":
                await self._cmd_listgreet(mod); return True
            case "greeton":
                set_setting("greet_enabled", "1")
                await self.highrise.send_whisper(mod.id, "✅ Приветствия включены."); return True
            case "greetoff":
                set_setting("greet_enabled", "0")
                await self.highrise.send_whisper(mod.id, "✅ Приветствия выключены."); return True

            case "crd":
                await self._cmd_coordinates(mod, rest.lstrip("@")); return True

            case "broadcast":
                await self._cmd_broadcast(mod, rest); return True

            case "inboxon":
                set_setting("require_inbox", "1")
                await self.highrise.send_whisper(mod.id, "✅ Требование inbox включено."); return True
            case "inboxoff":
                set_setting("require_inbox", "0")
                await self.highrise.send_whisper(mod.id, "✅ Требование inbox выключено."); return True

            # ── Публикация ─────────────────────────────────
            case "setpost":
                if is_owner:
                    await self._cmd_setpost(mod, rest)
                else:
                    await self.highrise.send_whisper(mod.id, "❌ Только владелец.")
                return True
            case "clearpost":
                if is_owner:
                    del_setting("required_post_id")
                    set_setting("require_post_comment", "0")
                    await self.highrise.send_whisper(mod.id, "✅ Публикация сброшена, проверка выключена.")
                else:
                    await self.highrise.send_whisper(mod.id, "❌ Только владелец.")
                return True
            case "poston":
                if is_owner:
                    if get_setting("required_post_id"):
                        set_setting("require_post_comment", "1")
                        await self.highrise.send_whisper(mod.id, "✅ Проверка комментария включена.")
                    else:
                        await self.highrise.send_whisper(mod.id, "❌ Сначала задайте публикацию: setpost <ID>")
                else:
                    await self.highrise.send_whisper(mod.id, "❌ Только владелец.")
                return True
            case "postoff":
                if is_owner:
                    set_setting("require_post_comment", "0")
                    await self.highrise.send_whisper(mod.id, "✅ Проверка комментария выключена.")
                else:
                    await self.highrise.send_whisper(mod.id, "❌ Только владелец.")
                return True

        return False

    # ═══════════════════════════════════════════════════════
    #  ADMIN INBOX PANEL
    # ═══════════════════════════════════════════════════════

    async def _get_latest_inbox_message(self, conv_id: str) -> Optional[str]:
        """Получает текст последнего сообщения в беседе."""
        try:
            resp = await self.highrise.get_messages(conv_id)
            if hasattr(resp, "messages") and resp.messages:
                return resp.messages[0].content
        except Exception as exc:
            print(f"[BOT] get_messages error: {exc}")
        return None

    async def _admin_send(self, conv_id: str, text: str) -> None:
        try:
            await self.highrise.send_message(conv_id, text)
        except Exception as exc:
            print(f"[BOT] admin send error: {exc}")

    async def _handle_admin_inbox(
        self, user_id: str, conv_id: str, is_new: bool
    ) -> None:
        """Главный роутер admin-панели."""
        if is_new:
            self._admin_state[user_id] = ""
            await self._admin_main_menu(conv_id)
            return

        text = await self._get_latest_inbox_message(conv_id)
        if not text:
            await self._admin_main_menu(conv_id)
            return

        text = text.strip()
        nav = text.lower()

        # Глобальная навигация назад
        if nav in ("0", "меню", "menu", "назад", "back", "назад", "главное"):
            self._admin_state[user_id] = ""
            await self._admin_main_menu(conv_id)
            return

        current = self._admin_state.get(user_id, "")

        if current == "":
            # Главное меню — ждём цифру
            if text in ADMIN_SECTIONS:
                section = ADMIN_SECTIONS[text]
                self._admin_state[user_id] = section
                await self._admin_section_menu(conv_id, section)
            else:
                await self._admin_main_menu(conv_id)

        elif current == "designer":
            await self._admin_designer_cmd(user_id, conv_id, text)

        elif current == "rotation":
            await self._admin_rotation_cmd(user_id, conv_id, text)

        elif current == "greetings":
            await self._admin_greetings_cmd(user_id, conv_id, text)

        elif current == "spawns":
            await self._admin_spawns_cmd(user_id, conv_id, text)

        elif current == "outfit":
            await self._admin_outfit_cmd(user_id, conv_id, text)

        elif current == "inbox_sec":
            await self._admin_inbox_cmd(user_id, conv_id, text)

        elif current == "settings":
            await self._admin_settings_cmd(user_id, conv_id, text)

        elif current == "stats":
            await self._admin_stats_cmd(user_id, conv_id, text)

        elif current == "post":
            await self._admin_post_cmd(user_id, conv_id, text)

        elif current == "moderation":
            await self._admin_moderation_cmd(user_id, conv_id, text)

        elif current == "voice":
            await self._admin_voice_cmd(user_id, conv_id, text)

        elif current == "room":
            await self._admin_room_cmd(user_id, conv_id, text)

    async def _admin_main_menu(self, conv_id: str) -> None:
        req_inbox = get_setting("require_inbox", "1" if REQUIRE_INBOX else "0") == "1"
        req_post = get_setting("require_post_comment", "0") == "1"
        post_id = get_setting("required_post_id", "—")
        mode = fmt_price()

        lines = [
            "🎮 ПАНЕЛЬ УПРАВЛЕНИЯ",
            "━━━━━━━━━━━━━━━━━━━━",
            f"Режим: {mode} | {fmt_time(DESIGNER_DURATION)}",
            f"Inbox: {'✅' if req_inbox else '❌'}  Пост: {'✅' if req_post else '❌'}",
            "",
            "1 — 🎨 Дизайнер",
            "2 — 🔁 Ротация сообщений",
            "3 — 👋 Приветствия",
            "4 — 📍 Точки появления",
            "5 — 👗 Наряд бота",
            "6 — 📩 Inbox / Рассылка",
            "7 — ⚙️ Настройки",
            "8 — 📊 Статистика",
            "9 — 📌 Публикация",
            "10 — 🛡️ Модерация",
            "11 — 🎙️ Голос",
            "12 — 🏠 Комната",
            "",
            "Отправь номер раздела",
        ]
        await self._admin_send(conv_id, "\n".join(lines))

    async def _admin_section_menu(self, conv_id: str, section: str) -> None:
        menus = {
            "designer": (
                "🎨 ДИЗАЙНЕР\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "give Ник — выдать привилегию\n"
                "take Ник — забрать привилегию\n"
                "ban Ник [причина] — в ЧС\n"
                "unban Ник — убрать из ЧС\n"
                "blacklist — показать ЧС\n"
                "profile Ник — профиль\n"
                "history [N] — история выдач\n"
                "wallet — кошелёк бота\n"
                "top — топ-10 по времени\n"
                "pending — незав. платежи\n"
                "setcost N / setcost 0 — цена\n"
                "\n0 — главное меню"
            ),
            "rotation": (
                "🔁 РОТАЦИЯ СООБЩЕНИЙ\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "list — список сообщений\n"
                "add текст — добавить\n"
                "remove N — отключить по ID\n"
                "interval N — интервал (сек, мин.10)\n"
                "on / off — вкл/выкл ротацию\n"
                "\nПеременная {price} в тексте\n"
                "\n0 — главное меню"
            ),
            "greetings": (
                "👋 ПРИВЕТСТВИЯ\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "list — список приветствий\n"
                "add текст — добавить\n"
                "remove N — удалить по ID\n"
                "on / off — вкл/выкл\n"
                "\nПеременная {username} в тексте\n"
                "\n0 — главное меню"
            ),
            "spawns": (
                "📍 ТОЧКИ ПОЯВЛЕНИЯ\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "show — показать точки\n"
                "bot x y z [facing] — точка бота\n"
                "user x y z [facing] — точка юзеров\n"
                "clearbot — сбросить точку бота\n"
                "clearuser — сбросить точку юзеров\n"
                "go — отправить бота на его точку\n"
                "\n0 — главное меню"
            ),
            "outfit": (
                "👗 НАРЯД БОТА\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "show — текущий наряд\n"
                "copy Ник — скопировать у игрока\n"
                "add item_id — надеть предмет\n"
                "remove item_id — снять предмет\n"
                "\n0 — главное меню"
            ),
            "inbox_sec": (
                "📩 INBOX / РАССЫЛКА\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "broadcast текст — рассылка всем\n"
                "inboxon / inboxoff — требование DM\n"
                "\n0 — главное меню"
            ),
            "settings": (
                "⚙️ НАСТРОЙКИ\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "cost N — цена (0 = бесплатно)\n"
                "inboxon / inboxoff — требование inbox\n"
                "poston / postoff — требование поста\n"
                "show — текущие настройки\n"
                "\n0 — главное меню"
            ),
            "stats": (
                "📊 СТАТИСТИКА\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "show — общая статистика\n"
                "top — топ-10 по времени\n"
                "history [N] — последние выдачи\n"
                "pending — незав. платежи\n"
                "blacklist — чёрный список\n"
                "\n0 — главное меню"
            ),
            "post": (
                "📌 ПУБЛИКАЦИЯ\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "set post_id — задать ID публикации\n"
                "clear — сбросить публикацию\n"
                "on / off — вкл/выкл проверку\n"
                "show — текущая публикация\n"
                "check Ник — проверить пользователя\n"
                "\npost_id — часть ссылки после /post/\n"
                "\n0 — главное меню"
            ),
            "moderation": (
                "🛡️ МОДЕРАЦИЯ\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "kick Ник — выгнать из комнаты\n"
                "ban Ник [мин] — забанить (опц. длит.)\n"
                "unban Ник — разбанить\n"
                "mute Ник [мин] — замутить (опц. длит.)\n"
                "unmute Ник — размутить\n"
                "move Ник RoomID — переместить в комнату\n"
                "\n0 — главное меню"
            ),
            "voice": (
                "🎙️ ГОЛОС\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "status — статус голоса в комнате\n"
                "add Ник — пригласить в голос\n"
                "remove Ник — убрать из голоса\n"
                "buy — купить время голоса\n"
                "\n0 — главное меню"
            ),
            "room": (
                "🏠 КОМНАТА\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "boost [N] — купить буст комнаты\n"
                "inventory — инвентарь бота\n"
                "tip Ник bars — отправить голду (1/5/10/50/100/500/1k/5000/10k)\n"
                "\n0 — главное меню"
            ),
        }
        text = menus.get(section, "Раздел не найден.\n\n0 — главное меню")
        await self._admin_send(conv_id, text)

    # ── Обработчики команд в разделах ─────────────────────

    async def _admin_designer_cmd(self, user_id: str, conv_id: str, text: str) -> None:
        cmd, rest = parse_cmd(text)
        fake_mod = User(id=user_id, username="owner")

        match cmd:
            case "give":
                await self._admin_exec_designer_action(conv_id, "give", rest.lstrip("@"))
            case "take":
                await self._admin_exec_designer_action(conv_id, "take", rest.lstrip("@"))
            case "ban":
                parts = rest.split(None, 1)
                uname = parts[0].lstrip("@")
                reason = parts[1] if len(parts) > 1 else ""
                await self._admin_exec_ban(conv_id, uname, reason)
            case "unban":
                await self._admin_exec_unban(conv_id, rest.lstrip("@"))
            case "blacklist":
                await self._admin_exec_blacklist(conv_id)
            case "profile":
                await self._admin_exec_profile(conv_id, rest.lstrip("@"))
            case "history" | "hist":
                limit = int(rest) if rest.isdigit() else 10
                await self._admin_exec_history(conv_id, limit)
            case "wallet":
                await self._admin_exec_wallet(conv_id)
            case "top":
                await self._admin_exec_top(conv_id)
            case "pending":
                await self._admin_exec_pending(conv_id)
            case "setcost" | "cost":
                await self._admin_exec_setcost(conv_id, rest)
            case _:
                await self._admin_section_menu(conv_id, "designer")

    async def _admin_rotation_cmd(self, user_id: str, conv_id: str, text: str) -> None:
        cmd, rest = parse_cmd(text)
        match cmd:
            case "list":
                messages = rot_list()
                if not messages:
                    await self._admin_send(conv_id, "📋 Нет сообщений.\n\n0 — главное меню"); return
                interval = get_setting("rot_interval", str(DEFAULT_ROT_INTERVAL))
                rot_on = get_setting("rot_enabled", "1") == "1"
                lines = [f"📋 Ротация ({'вкл' if rot_on else 'выкл'}, {interval}с.):"]
                for m in messages:
                    status = "🟢" if m["is_active"] else "🔴"
                    preview = m["message"][:50] + ("…" if len(m["message"]) > 50 else "")
                    lines.append(f"{status} #{m['id']}: {preview}")
                lines.append("\n0 — главное меню")
                await self._admin_send(conv_id, "\n".join(lines))
            case "add":
                if not rest:
                    await self._admin_send(conv_id, "❌ Формат: add текст")
                else:
                    mid = rot_add(rest)
                    await self._admin_send(conv_id, f"✅ Сообщение #{mid} добавлено.")
            case "remove":
                if rest.isdigit():
                    ok = rot_remove(int(rest))
                    await self._admin_send(conv_id, f"✅ #{rest} отключено." if ok else f"❌ #{rest} не найдено.")
                else:
                    await self._admin_send(conv_id, "❌ Формат: remove N")
            case "interval":
                if rest.isdigit() and int(rest) >= 10:
                    set_setting("rot_interval", rest)
                    self._start_rotation()
                    await self._admin_send(conv_id, f"✅ Интервал: {rest} сек.")
                else:
                    await self._admin_send(conv_id, "❌ Минимум 10 секунд.")
            case "on":
                set_setting("rot_enabled", "1")
                self._start_rotation()
                await self._admin_send(conv_id, "✅ Ротация включена.")
            case "off":
                set_setting("rot_enabled", "0")
                await self._admin_send(conv_id, "✅ Ротация выключена.")
            case _:
                await self._admin_section_menu(conv_id, "rotation")

    async def _admin_greetings_cmd(self, user_id: str, conv_id: str, text: str) -> None:
        cmd, rest = parse_cmd(text)
        match cmd:
            case "list":
                msgs = greet_list()
                if not msgs:
                    await self._admin_send(conv_id, "📋 Нет приветствий.\n\n0 — главное меню"); return
                on = get_setting("greet_enabled", "1") == "1"
                lines = [f"📋 Приветствия ({'вкл' if on else 'выкл'}):"]
                for m in msgs:
                    status = "🟢" if m["is_active"] else "🔴"
                    preview = m["message"][:50] + ("…" if len(m["message"]) > 50 else "")
                    lines.append(f"{status} #{m['id']}: {preview}")
                lines.append("\n0 — главное меню")
                await self._admin_send(conv_id, "\n".join(lines))
            case "add":
                if not rest:
                    await self._admin_send(conv_id, "❌ Формат: add текст")
                else:
                    gid = greet_add(rest)
                    await self._admin_send(conv_id, f"✅ Приветствие #{gid} добавлено.")
            case "remove":
                if rest.isdigit():
                    ok = greet_remove(int(rest))
                    await self._admin_send(conv_id, f"✅ #{rest} удалено." if ok else f"❌ #{rest} не найдено.")
                else:
                    await self._admin_send(conv_id, "❌ Формат: remove N")
            case "on":
                set_setting("greet_enabled", "1")
                await self._admin_send(conv_id, "✅ Приветствия включены.")
            case "off":
                set_setting("greet_enabled", "0")
                await self._admin_send(conv_id, "✅ Приветствия выключены.")
            case _:
                await self._admin_section_menu(conv_id, "greetings")

    async def _admin_spawns_cmd(self, user_id: str, conv_id: str, text: str) -> None:
        cmd, rest = parse_cmd(text)
        match cmd:
            case "show":
                await self._admin_send(conv_id,
                    f"📍 Точки:\n"
                    f"Бот: {get_setting('bot_spawn') or 'не задана'}\n"
                    f"Юзеры: {get_setting('user_spawn') or 'не задана'}"
                )
            case "bot":
                pos = parse_position(rest)
                if not pos:
                    await self._admin_send(conv_id, "❌ Формат: bot x y z [facing]")
                else:
                    set_setting("bot_spawn", pos_to_str(pos))
                    try:
                        await self.highrise.walk_to(pos)
                    except Exception:
                        pass
                    await self._admin_send(conv_id, f"✅ Точка бота: {pos_to_str(pos)}")
            case "user":
                pos = parse_position(rest)
                if not pos:
                    await self._admin_send(conv_id, "❌ Формат: user x y z [facing]")
                else:
                    set_setting("user_spawn", pos_to_str(pos))
                    await self._admin_send(conv_id, f"✅ Точка юзеров: {pos_to_str(pos)}")
            case "clearbot":
                del_setting("bot_spawn")
                await self._admin_send(conv_id, "✅ Точка бота сброшена.")
            case "clearuser":
                del_setting("user_spawn")
                await self._admin_send(conv_id, "✅ Точка юзеров сброшена.")
            case "go":
                spawn_str = get_setting("bot_spawn")
                if not spawn_str:
                    await self._admin_send(conv_id, "❌ Точка бота не задана.")
                else:
                    pos = str_to_pos(spawn_str)
                    if pos:
                        try:
                            await self.highrise.walk_to(pos)
                            await self._admin_send(conv_id, "✅ Бот перемещён.")
                        except Exception as exc:
                            await self._admin_send(conv_id, f"❌ Ошибка: {exc}")
            case _:
                await self._admin_section_menu(conv_id, "spawns")

    async def _admin_outfit_cmd(self, user_id: str, conv_id: str, text: str) -> None:
        cmd, rest = parse_cmd(text)
        match cmd:
            case "show":
                try:
                    outfit = await self._get_bot_outfit()
                    if not outfit:
                        await self._admin_send(conv_id, "👗 Бот без наряда."); return
                    lines = [f"👗 Наряд бота ({len(outfit)} предм.):"]
                    for item in outfit:
                        lines.append(f"  {item.id}")
                    await self._admin_send(conv_id, "\n".join(lines))
                except Exception as exc:
                    await self._admin_send(conv_id, f"❌ Ошибка: {exc}")
            case "copy":
                target = rest.lstrip("@")
                if not target:
                    await self._admin_send(conv_id, "❌ Формат: copy Ник")
                else:
                    row = get_user_by_name(target)
                    if not row:
                        await self._admin_send(conv_id, f"❌ '{target}' не найден в БД.")
                    else:
                        try:
                            resp = await self.highrise.get_user_outfit(row["user_id"])
                            if not hasattr(resp, "outfit"):
                                await self._admin_send(conv_id, f"❌ Ошибка: {resp}")
                            else:
                                await self.highrise.set_outfit(resp.outfit)
                                await self._admin_send(conv_id, f"✅ Наряд скопирован у {target}.")
                        except Exception as exc:
                            await self._admin_send(conv_id, f"❌ Ошибка: {exc}")
            case "add":
                if not rest:
                    await self._admin_send(conv_id, "❌ Формат: add item_id")
                else:
                    try:
                        outfit = await self._get_bot_outfit()
                        if any(i.id == rest for i in outfit):
                            await self._admin_send(conv_id, "ℹ️ Предмет уже надет.")
                        else:
                            outfit.append(Item(type="clothing", amount=1, id=rest))
                            await self.highrise.set_outfit(outfit)
                            await self._admin_send(conv_id, f"✅ Предмет {rest} надет.")
                    except Exception as exc:
                        await self._admin_send(conv_id, f"❌ Ошибка: {exc}")
            case "remove":
                if not rest:
                    await self._admin_send(conv_id, "❌ Формат: remove item_id")
                else:
                    try:
                        outfit = await self._get_bot_outfit()
                        new_outfit = [i for i in outfit if i.id != rest]
                        if len(new_outfit) == len(outfit):
                            await self._admin_send(conv_id, f"ℹ️ Предмет {rest} не найден.")
                        else:
                            await self.highrise.set_outfit(new_outfit)
                            await self._admin_send(conv_id, f"✅ Предмет {rest} снят.")
                    except Exception as exc:
                        await self._admin_send(conv_id, f"❌ Ошибка: {exc}")
            case _:
                await self._admin_section_menu(conv_id, "outfit")

    async def _admin_inbox_cmd(self, user_id: str, conv_id: str, text: str) -> None:
        cmd, rest = parse_cmd(text)
        match cmd:
            case "broadcast":
                if not rest:
                    await self._admin_send(conv_id, "❌ Формат: broadcast текст")
                else:
                    await self._admin_exec_broadcast(conv_id, rest)
            case "inboxon":
                set_setting("require_inbox", "1")
                await self._admin_send(conv_id, "✅ Требование inbox включено.")
            case "inboxoff":
                set_setting("require_inbox", "0")
                await self._admin_send(conv_id, "✅ Требование inbox выключено.")
            case _:
                await self._admin_section_menu(conv_id, "inbox_sec")

    async def _admin_settings_cmd(self, user_id: str, conv_id: str, text: str) -> None:
        cmd, rest = parse_cmd(text)
        match cmd:
            case "cost":
                await self._admin_exec_setcost(conv_id, rest)
            case "inboxon":
                set_setting("require_inbox", "1")
                await self._admin_send(conv_id, "✅ Требование inbox включено.")
            case "inboxoff":
                set_setting("require_inbox", "0")
                await self._admin_send(conv_id, "✅ Требование inbox выключено.")
            case "poston":
                if get_setting("required_post_id"):
                    set_setting("require_post_comment", "1")
                    await self._admin_send(conv_id, "✅ Проверка комментария включена.")
                else:
                    await self._admin_send(conv_id, "❌ Сначала задайте публикацию в разделе 9.")
            case "postoff":
                set_setting("require_post_comment", "0")
                await self._admin_send(conv_id, "✅ Проверка комментария выключена.")
            case "show":
                req_inbox = get_setting("require_inbox", "1" if REQUIRE_INBOX else "0") == "1"
                req_post = get_setting("require_post_comment", "0") == "1"
                post_id = get_setting("required_post_id", "—")
                cooldown = REQUEST_COOLDOWN
                age = MIN_ACCOUNT_AGE_DAYS
                await self._admin_send(conv_id,
                    f"⚙️ Настройки:\n"
                    f"Цена: {fmt_price()}\n"
                    f"Длительность: {fmt_time(DESIGNER_DURATION)}\n"
                    f"Кулдаун: {fmt_time(cooldown)}\n"
                    f"Мин. возраст: {age} дн.\n"
                    f"Inbox: {'✅' if req_inbox else '❌'}\n"
                    f"Пост: {'✅' if req_post else '❌'} ({post_id})\n"
                    f"В комнате: {len(self._users_in_room)}"
                )
            case _:
                await self._admin_section_menu(conv_id, "settings")

    async def _admin_post_cmd(self, user_id: str, conv_id: str, text: str) -> None:
        cmd, rest = parse_cmd(text)
        match cmd:
            case "set":
                if not rest:
                    await self._admin_send(conv_id,
                        "❌ Укажите ID публикации.\n"
                        "Пример: set abc123def456\n\n"
                        "ID берётся из ссылки на публикацию."
                    )
                else:
                    set_setting("required_post_id", rest)
                    await self._admin_send(conv_id, f"✅ Публикация установлена: {rest}")
            case "clear":
                del_setting("required_post_id")
                set_setting("require_post_comment", "0")
                await self._admin_send(conv_id, "✅ Публикация сброшена, проверка выключена.")
            case "on":
                if get_setting("required_post_id"):
                    set_setting("require_post_comment", "1")
                    await self._admin_send(conv_id, "✅ Проверка комментария включена.")
                else:
                    await self._admin_send(conv_id, "❌ Сначала задайте публикацию: set post_id")
            case "off":
                set_setting("require_post_comment", "0")
                await self._admin_send(conv_id, "✅ Проверка комментария выключена.")
            case "show":
                post_id = get_setting("required_post_id", "")
                req_post = get_setting("require_post_comment", "0") == "1"
                if post_id:
                    await self._admin_send(conv_id,
                        f"📌 Публикация: {post_id}\n"
                        f"Проверка: {'✅ включена' if req_post else '❌ выключена'}"
                    )
                else:
                    await self._admin_send(conv_id, "📌 Публикация не задана.")
            case "check":
                target = rest.lstrip("@")
                if not target:
                    await self._admin_send(conv_id, "❌ Формат: check Ник")
                else:
                    post_id = get_setting("required_post_id", "")
                    if not post_id:
                        await self._admin_send(conv_id, "❌ Публикация не задана.")
                    else:
                        row = get_user_by_name(target)
                        if not row:
                            await self._admin_send(conv_id, f"❌ '{target}' не найден в БД.")
                        else:
                            has = await self._check_post_comment(row["user_id"], post_id)
                            await self._admin_send(conv_id,
                                f"{'✅' if has else '❌'} {target}: "
                                f"{'прокомментировал' if has else 'не комментировал'}"
                            )
            case _:
                await self._admin_section_menu(conv_id, "post")

    async def _admin_moderation_cmd(self, user_id: str, conv_id: str, text: str) -> None:
        cmd, rest = parse_cmd(text)
        parts = rest.split(None, 1)
        target_name = parts[0].lstrip("@") if parts else ""
        extra = parts[1] if len(parts) > 1 else ""

        if cmd in ("kick", "ban", "unban", "mute", "unmute"):
            if not target_name:
                await self._admin_send(conv_id, f"❌ Формат: {cmd} Ник"); return
            row = get_user_by_name(target_name)
            if not row:
                await self._admin_send(conv_id, f"❌ '{target_name}' не найден в БД."); return
            duration = None
            if extra.isdigit():
                duration = int(extra) * 60
            try:
                await self.highrise.moderate_room(row["user_id"], cmd, duration)
                dur_str = f" на {fmt_time(duration)}" if duration else ""
                await self._admin_send(conv_id, f"✅ {cmd} → {target_name}{dur_str}.")
            except Exception as exc:
                await self._admin_send(conv_id, f"❌ Ошибка: {exc}")

        elif cmd == "move":
            if not target_name or not extra:
                await self._admin_send(conv_id, "❌ Формат: move Ник RoomID"); return
            row = get_user_by_name(target_name)
            if not row:
                await self._admin_send(conv_id, f"❌ '{target_name}' не найден в БД."); return
            try:
                await self.highrise.move_user_to_room(row["user_id"], extra)
                await self._admin_send(conv_id, f"✅ {target_name} перемещён в комнату {extra}.")
            except Exception as exc:
                await self._admin_send(conv_id, f"❌ Ошибка: {exc}")

        else:
            await self._admin_section_menu(conv_id, "moderation")

    async def _admin_voice_cmd(self, user_id: str, conv_id: str, text: str) -> None:
        cmd, rest = parse_cmd(text)

        match cmd:
            case "status":
                try:
                    resp = await self.highrise.get_voice_status()
                    if hasattr(resp, "seconds_left"):
                        users_info = ", ".join(
                            f"{uid}({st})" for uid, st in resp.users.items()
                        ) or "нет"
                        await self._admin_send(conv_id,
                            f"🎙️ Голос: {fmt_time(resp.seconds_left)} осталось\n"
                            f"Участники: {users_info}"
                        )
                    else:
                        await self._admin_send(conv_id, f"❌ {resp}")
                except Exception as exc:
                    await self._admin_send(conv_id, f"❌ Ошибка: {exc}")

            case "add":
                target = rest.lstrip("@")
                if not target:
                    await self._admin_send(conv_id, "❌ Формат: add Ник"); return
                row = get_user_by_name(target)
                if not row:
                    await self._admin_send(conv_id, f"❌ '{target}' не найден в БД."); return
                try:
                    await self.highrise.add_user_to_voice(row["user_id"])
                    await self._admin_send(conv_id, f"✅ {target} приглашён в голос.")
                except Exception as exc:
                    await self._admin_send(conv_id, f"❌ Ошибка: {exc}")

            case "remove":
                target = rest.lstrip("@")
                if not target:
                    await self._admin_send(conv_id, "❌ Формат: remove Ник"); return
                row = get_user_by_name(target)
                if not row:
                    await self._admin_send(conv_id, f"❌ '{target}' не найден в БД."); return
                try:
                    await self.highrise.remove_user_from_voice(row["user_id"])
                    await self._admin_send(conv_id, f"✅ {target} убран из голоса.")
                except Exception as exc:
                    await self._admin_send(conv_id, f"❌ Ошибка: {exc}")

            case "buy":
                try:
                    result = await self.highrise.buy_voice_time()
                    labels = {
                        "success": "✅ Время голоса куплено!",
                        "insufficient_funds": "❌ Недостаточно средств.",
                        "only_token_bought": "ℹ️ Куплен только токен.",
                    }
                    await self._admin_send(conv_id, labels.get(str(result), f"❓ {result}"))
                except Exception as exc:
                    await self._admin_send(conv_id, f"❌ Ошибка: {exc}")

            case _:
                await self._admin_section_menu(conv_id, "voice")

    async def _admin_room_cmd(self, user_id: str, conv_id: str, text: str) -> None:
        cmd, rest = parse_cmd(text)

        match cmd:
            case "boost":
                amount = int(rest) if rest.isdigit() and int(rest) > 0 else 1
                try:
                    result = await self.highrise.buy_room_boost(amount=amount)
                    labels = {
                        "success": f"✅ Куплено бустов: {amount}.",
                        "insufficient_funds": "❌ Недостаточно средств.",
                        "only_token_bought": "ℹ️ Куплен только токен.",
                    }
                    await self._admin_send(conv_id, labels.get(str(result), f"❓ {result}"))
                except Exception as exc:
                    await self._admin_send(conv_id, f"❌ Ошибка: {exc}")

            case "inventory":
                try:
                    resp = await self.highrise.get_inventory()
                    if hasattr(resp, "items"):
                        if not resp.items:
                            await self._admin_send(conv_id, "🎒 Инвентарь пуст."); return
                        lines = [f"🎒 Инвентарь ({len(resp.items)} пред.):"]
                        for item in resp.items[:30]:
                            lines.append(f"  {item.id}")
                        if len(resp.items) > 30:
                            lines.append(f"  … и ещё {len(resp.items) - 30}")
                        await self._admin_send(conv_id, "\n".join(lines))
                    else:
                        await self._admin_send(conv_id, f"❌ {resp}")
                except Exception as exc:
                    await self._admin_send(conv_id, f"❌ Ошибка: {exc}")

            case "tip":
                parts = rest.split(None, 1)
                if len(parts) < 2:
                    await self._admin_send(conv_id,
                        "❌ Формат: tip Ник bars\n"
                        "bars: 1/5/10/50/100/500/1k/5000/10k"
                    ); return
                target = parts[0].lstrip("@")
                bar = parts[1].strip()
                valid = {"1","5","10","50","100","500","1k","5000","10k"}
                if bar not in valid:
                    await self._admin_send(conv_id, f"❌ Допустимые значения: {', '.join(sorted(valid))}"); return
                row = get_user_by_name(target)
                if not row:
                    await self._admin_send(conv_id, f"❌ '{target}' не найден в БД."); return
                try:
                    result = await self.highrise.tip_user(row["user_id"], f"gold_bar_{bar}")
                    if result == "success":
                        await self._admin_send(conv_id, f"✅ Отправлено {bar} голды → {target}.")
                    else:
                        await self._admin_send(conv_id, "❌ Недостаточно средств.")
                except Exception as exc:
                    await self._admin_send(conv_id, f"❌ Ошибка: {exc}")

            case _:
                await self._admin_section_menu(conv_id, "room")

    # ── Хелперы для admin-команд ─────────────────────────

    async def _admin_stats_cmd(self, user_id: str, conv_id: str, text: str) -> None:
        cmd, rest = parse_cmd(text)
        match cmd:
            case "show":
                with db() as conn:
                    total_users  = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                    total_grants = conn.execute("SELECT COUNT(*) FROM designer_sessions").fetchone()[0]
                    active_now   = conn.execute("SELECT COUNT(*) FROM designer_sessions WHERE is_active=1").fetchone()[0]
                    blacklisted  = conn.execute("SELECT COUNT(*) FROM users WHERE is_blacklisted=1").fetchone()[0]
                    inbox_users  = conn.execute("SELECT COUNT(*) FROM users WHERE has_messaged_bot=1").fetchone()[0]
                interval  = get_setting("rot_interval", str(DEFAULT_ROT_INTERVAL))
                rot_on    = get_setting("rot_enabled", "1") == "1"
                rot_count = len(rot_active_messages())
                greet_on  = get_setting("greet_enabled", "1") == "1"
                inbox_req = get_setting("require_inbox", "1" if REQUIRE_INBOX else "0") == "1"
                req_post  = get_setting("require_post_comment", "0") == "1"
                post_id   = get_setting("required_post_id", "—")
                await self._admin_send(conv_id, "\n".join([
                    "📊 СТАТИСТИКА",
                    "━━━━━━━━━━━━━━━━━━━━",
                    f"👥 Пользователей: {total_users}",
                    f"   из них писали inbox: {inbox_users}",
                    f"🎨 Выдач дизайнера: {total_grants}",
                    f"   активных сейчас: {active_now}",
                    f"🚫 В чёрном списке: {blacklisted}",
                    f"💰 Цена: {fmt_price()} | {fmt_time(DESIGNER_DURATION)}",
                    f"🔁 Ротация: {'вкл' if rot_on else 'выкл'} | {rot_count} сообщ. | {interval}с.",
                    f"👋 Приветствия: {'вкл' if greet_on else 'выкл'} | {len(greet_active_messages())} шт.",
                    f"📩 Требование inbox: {'✅' if inbox_req else '❌'}",
                    f"📌 Требование поста: {'✅' if req_post else '❌'} ({post_id})",
                    f"🏠 В комнате сейчас: {len(self._users_in_room)}",
                ]))
            case "top":
                await self._admin_exec_top(conv_id)
            case "history" | "hist":
                limit = int(rest) if rest.isdigit() else 10
                await self._admin_exec_history(conv_id, limit)
            case "pending":
                await self._admin_exec_pending(conv_id)
            case "blacklist":
                await self._admin_exec_blacklist(conv_id)
            case _:
                await self._admin_section_menu(conv_id, "stats")

    async def _admin_exec_designer_action(self, conv_id: str, action: str, target_username: str) -> None:
        if not target_username:
            await self._admin_send(conv_id, f"❌ Укажите ник: {action} Ник"); return
        row = get_user_by_name(target_username)
        if not row:
            await self._admin_send(conv_id, f"❌ '{target_username}' не найден в БД."); return

        if action == "give":
            close_all_user_sessions(row["user_id"])
            self._cancel_timer(row["user_id"])
            target = User(id=row["user_id"], username=row["username"])
            session_id = create_session(row["user_id"], row["username"], 0, "owner(inbox)")
            try:
                await self.highrise.change_room_privilege(row["user_id"], RoomPermissions(designer=True))
                await self._admin_send(conv_id, f"✅ Привилегия выдана {target_username}.")
            except Exception as exc:
                close_session(session_id)
                await self._admin_send(conv_id, f"❌ Ошибка: {exc}")
        elif action == "take":
            session = get_active_session(row["user_id"])
            if not session:
                await self._admin_send(conv_id, f"ℹ️ У {target_username} нет активной привилегии."); return
            self._cancel_timer(row["user_id"])
            await self._revoke_designer(row["user_id"], row["username"], session["id"])
            await self._admin_send(conv_id, f"✅ Привилегия снята с {target_username}.")

    async def _admin_exec_ban(self, conv_id: str, target_username: str, reason: str) -> None:
        if not target_username:
            await self._admin_send(conv_id, "❌ Формат: ban Ник [причина]"); return
        row = get_user_by_name(target_username)
        if not row:
            await self._admin_send(conv_id, f"❌ '{target_username}' не найден."); return
        with db() as conn:
            conn.execute(
                "UPDATE users SET is_blacklisted = 1, blacklist_reason = ?, blacklisted_by = ? "
                "WHERE user_id = ?",
                (reason or None, "owner", row["user_id"]),
            )
        session = get_active_session(row["user_id"])
        if session:
            self._cancel_timer(row["user_id"])
            await self._revoke_designer(row["user_id"], row["username"], session["id"])
        r = f" ({reason})" if reason else ""
        await self._admin_send(conv_id, f"🚫 {target_username} добавлен в ЧС{r}.")

    async def _admin_exec_unban(self, conv_id: str, target_username: str) -> None:
        if not target_username:
            await self._admin_send(conv_id, "❌ Формат: unban Ник"); return
        row = get_user_by_name(target_username)
        if not row:
            await self._admin_send(conv_id, f"❌ '{target_username}' не найден."); return
        with db() as conn:
            conn.execute(
                "UPDATE users SET is_blacklisted = 0, blacklist_reason = NULL, blacklisted_by = NULL "
                "WHERE user_id = ?", (row["user_id"],)
            )
        await self._admin_send(conv_id, f"✅ {target_username} убран из ЧС.")

    async def _admin_exec_blacklist(self, conv_id: str) -> None:
        with db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT username, blacklist_reason, blacklisted_by FROM users WHERE is_blacklisted = 1"
            ).fetchall()
        if not rows:
            await self._admin_send(conv_id, "🚫 Чёрный список пуст."); return
        lines = ["🚫 Чёрный список:"]
        for r in rows:
            lines.append(f"  {r['username']} | {r['blacklist_reason'] or '—'}")
        await self._admin_send(conv_id, "\n".join(lines))

    async def _admin_exec_profile(self, conv_id: str, target_username: str) -> None:
        if not target_username:
            await self._admin_send(conv_id, "❌ Формат: profile Ник"); return
        row = get_user_by_name(target_username)
        if not row:
            await self._admin_send(conv_id, f"❌ '{target_username}' не найден."); return
        t = row["total_time_sec"]
        session = get_active_session(row["user_id"])
        des_status = "нет"
        if session:
            if session["expires_at"]:
                exp = datetime.fromisoformat(session["expires_at"])
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                rem = (exp - datetime.now(timezone.utc)).total_seconds()
                des_status = f"активен, {fmt_time(int(rem))}"
            else:
                des_status = "выдан, ждёт перезахода"
        lines = [
            f"👤 {row['username']}",
            f"🕐 Время: {fmt_time(t)}",
            f"💬 Сообщений: {row['messages_count']}",
            f"🎨 Получений: {row['designer_count']}",
            f"📩 Inbox: {'да' if row.get('has_messaged_bot') else 'нет'}",
            f"🎨 Дизайнер: {des_status}",
            f"🚫 ЧС: {'да (' + (row['blacklist_reason'] or '—') + ')' if row['is_blacklisted'] else 'нет'}",
        ]
        await self._admin_send(conv_id, "\n".join(lines))

    async def _admin_exec_history(self, conv_id: str, limit: int) -> None:
        with db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT username, granted_at, cost_paid, granted_by, is_active "
                "FROM designer_sessions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        if not rows:
            await self._admin_send(conv_id, "📋 История пуста."); return
        lines = [f"📋 Последние {limit}:"]
        for r in rows:
            status = "✅" if r["is_active"] else "❌"
            dt = r["granted_at"][:16].replace("T", " ")
            cost = f"💰{r['cost_paid']}" if r["cost_paid"] else "🆓"
            lines.append(f"{status} {r['username']} | {dt} | {cost}")
        await self._admin_send(conv_id, "\n".join(lines))

    async def _admin_exec_wallet(self, conv_id: str) -> None:
        try:
            resp = await self.highrise.get_wallet()
            if hasattr(resp, "content"):
                lines = ["💰 Кошелёк:"] + [f"  {i.type}: {i.amount}" for i in resp.content]
                await self._admin_send(conv_id, "\n".join(lines))
            else:
                await self._admin_send(conv_id, f"❌ Ошибка: {resp}")
        except Exception as exc:
            await self._admin_send(conv_id, f"❌ Ошибка: {exc}")

    async def _admin_exec_top(self, conv_id: str) -> None:
        with db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT username, total_time_sec, messages_count, designer_count "
                "FROM users ORDER BY total_time_sec DESC LIMIT 10"
            ).fetchall()
        if not rows:
            await self._admin_send(conv_id, "📈 Нет данных."); return
        lines = ["📈 Топ-10 по времени:"]
        for i, r in enumerate(rows, 1):
            lines.append(f"  {i}. {r['username']} — {fmt_time(r['total_time_sec'])}")
        await self._admin_send(conv_id, "\n".join(lines))

    async def _admin_exec_pending(self, conv_id: str) -> None:
        if not self.pending_gold:
            await self._admin_send(conv_id, "💰 Нет ожидающих платежей."); return
        lines = ["💰 Незавершённые платежи:"]
        for uid, amount in self.pending_gold.items():
            row = get_user_row(uid)
            name = row["username"] if row else uid
            lines.append(f"  {name}: {amount}/{DESIGNER_COST} зол.")
        await self._admin_send(conv_id, "\n".join(lines))

    async def _admin_exec_setcost(self, conv_id: str, value: str) -> None:
        global DESIGNER_COST, FREE_DESIGNER
        if value.lower() in ("0", "free", "бесплатно"):
            FREE_DESIGNER = True; DESIGNER_COST = 0
            await self._admin_send(conv_id, "✅ Привилегия теперь бесплатна.")
        elif value.isdigit() and int(value) > 0:
            DESIGNER_COST = int(value); FREE_DESIGNER = False
            await self._admin_send(conv_id, f"✅ Цена: {DESIGNER_COST} зол.")
        else:
            await self._admin_send(conv_id, "❌ Пример: cost 100 или cost 0")

    async def _admin_exec_broadcast(self, conv_id: str, text: str) -> None:
        with db() as conn:
            rows = conn.execute(
                "SELECT last_conv_id FROM users WHERE has_messaged_bot = 1 AND last_conv_id IS NOT NULL"
            ).fetchall()
        conv_ids = [r[0] for r in rows]
        if not conv_ids:
            await self._admin_send(conv_id, "ℹ️ Нет пользователей с inbox."); return

        await self._admin_send(conv_id, f"📤 Рассылка {len(conv_ids)} пользователям…")
        sent = errors = 0
        for cid in conv_ids:
            if cid == conv_id:  # не отправлять самому себе (владельцу)
                continue
            try:
                await self.highrise.send_message(cid, text)
                sent += 1
            except Exception:
                errors += 1
            await asyncio.sleep(0.3)
        await self._admin_send(conv_id, f"✅ Готово. Отправлено: {sent}, ошибок: {errors}.")

    # ═══════════════════════════════════════════════════════
    #  Реализация команд — дизайнер (чат)
    # ═══════════════════════════════════════════════════════

    async def _cmd_give(self, mod: User, target_username: str) -> None:
        if not target_username:
            await self.highrise.send_whisper(mod.id, "❌ Укажите ник: give @ник"); return
        row = get_user_by_name(target_username)
        if not row:
            await self.highrise.send_whisper(mod.id, f"❌ '{target_username}' не найден в БД.")
            return
        close_all_user_sessions(row["user_id"])
        self._cancel_timer(row["user_id"])
        target = User(id=row["user_id"], username=row["username"])
        await self._grant_designer(target, cost_paid=0, granted_by=mod.username)
        await self.highrise.send_whisper(mod.id, f"✅ Привилегия выдана {target_username}.")

    async def _cmd_take(self, mod: User, target_username: str) -> None:
        if not target_username:
            await self.highrise.send_whisper(mod.id, "❌ Укажите ник: take @ник"); return
        row = get_user_by_name(target_username)
        if not row:
            await self.highrise.send_whisper(mod.id, f"❌ '{target_username}' не найден в БД.")
            return
        session = get_active_session(row["user_id"])
        if not session:
            await self.highrise.send_whisper(mod.id, f"ℹ️ У {target_username} нет активной привилегии.")
            return
        self._cancel_timer(row["user_id"])
        await self._revoke_designer(row["user_id"], row["username"], session["id"])
        await self.highrise.send_whisper(mod.id, f"✅ Привилегия снята с {target_username}.")

    async def _cmd_ban(self, mod: User, target_username: str, reason: str) -> None:
        if not target_username:
            await self.highrise.send_whisper(mod.id, "❌ Укажите ник: ban @ник [причина]"); return
        row = get_user_by_name(target_username)
        if not row:
            await self.highrise.send_whisper(mod.id, f"❌ '{target_username}' не найден.")
            return
        with db() as conn:
            conn.execute(
                "UPDATE users SET is_blacklisted = 1, blacklist_reason = ?, blacklisted_by = ? "
                "WHERE user_id = ?",
                (reason or None, mod.username, row["user_id"]),
            )
        session = get_active_session(row["user_id"])
        if session:
            self._cancel_timer(row["user_id"])
            await self._revoke_designer(row["user_id"], row["username"], session["id"])
        r = f" (причина: {reason})" if reason else ""
        await self.highrise.send_whisper(mod.id, f"🚫 {target_username} добавлен в ЧС{r}.")

    async def _cmd_unban(self, mod: User, target_username: str) -> None:
        if not target_username:
            await self.highrise.send_whisper(mod.id, "❌ Укажите ник: unban @ник"); return
        row = get_user_by_name(target_username)
        if not row:
            await self.highrise.send_whisper(mod.id, f"❌ '{target_username}' не найден.")
            return
        with db() as conn:
            conn.execute(
                "UPDATE users SET is_blacklisted = 0, blacklist_reason = NULL, blacklisted_by = NULL "
                "WHERE user_id = ?",
                (row["user_id"],),
            )
        await self.highrise.send_whisper(mod.id, f"✅ {target_username} убран из ЧС.")

    async def _cmd_wallet(self, mod: User) -> None:
        try:
            resp = await self.highrise.get_wallet()
            if hasattr(resp, "content"):
                lines = ["💰 Кошелёк бота:"] + [f"  {i.type}: {i.amount}" for i in resp.content]
                await self._whisper(mod.id, lines)
            else:
                await self.highrise.send_whisper(mod.id, f"❌ Ошибка: {resp}")
        except Exception as exc:
            await self.highrise.send_whisper(mod.id, f"❌ Ошибка: {exc}")

    async def _cmd_history(self, mod: User, limit: int = 10) -> None:
        with db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT username, granted_at, cost_paid, granted_by, is_active "
                "FROM designer_sessions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        if not rows:
            await self.highrise.send_whisper(mod.id, "📋 История пуста."); return
        lines = [f"📋 Последние {limit} выдач:"]
        for r in rows:
            status = "✅" if r["is_active"] else "❌"
            dt = r["granted_at"][:16].replace("T", " ")
            cost = f"💰{r['cost_paid']}" if r["cost_paid"] else "🆓"
            lines.append(f"{status} {r['username']} | {dt} | {cost} | от {r['granted_by']}")
        await self._whisper(mod.id, lines)

    async def _cmd_profile(self, mod: User, target_username: str) -> None:
        if not target_username:
            await self.highrise.send_whisper(mod.id, "❌ Укажите ник: profile @ник"); return
        row = get_user_by_name(target_username)
        if not row:
            await self.highrise.send_whisper(mod.id, f"❌ '{target_username}' не найден в БД.")
            return
        t = row["total_time_sec"]
        lines = [
            f"👤 Профиль: {row['username']}",
            f"🕐 Время в комнате: {fmt_time(t)}",
            f"💬 Сообщений: {row['messages_count']}",
            f"🎨 Получений дизайнера: {row['designer_count']}",
            f"📩 Писал в inbox: {'Да' if row.get('has_messaged_bot') else 'Нет'}",
            f"🚫 В ЧС: {'Да (' + (row['blacklist_reason'] or '—') + ')' if row['is_blacklisted'] else 'Нет'}",
        ]
        session = get_active_session(row["user_id"])
        if session:
            if session["expires_at"]:
                exp = datetime.fromisoformat(session["expires_at"])
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                rem = (exp - datetime.now(timezone.utc)).total_seconds()
                lines.append(f"⏳ Дизайнер активен, осталось: {fmt_time(int(rem))}")
            else:
                lines.append("⏳ Дизайнер выдан, ожидает перезахода")
        await self._whisper(mod.id, lines)

    async def _cmd_show_blacklist(self, mod: User) -> None:
        with db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT username, blacklist_reason, blacklisted_by FROM users WHERE is_blacklisted = 1"
            ).fetchall()
        if not rows:
            await self.highrise.send_whisper(mod.id, "🚫 Чёрный список пуст."); return
        lines = ["🚫 Чёрный список:"]
        for r in rows:
            lines.append(f"  {r['username']} | {r['blacklist_reason'] or '—'} | {r['blacklisted_by'] or '—'}")
        await self._whisper(mod.id, lines)

    async def _cmd_stats(self, mod: User) -> None:
        with db() as conn:
            total_users  = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            total_grants = conn.execute("SELECT COUNT(*) FROM designer_sessions").fetchone()[0]
            active_now   = conn.execute("SELECT COUNT(*) FROM designer_sessions WHERE is_active=1").fetchone()[0]
            blacklisted  = conn.execute("SELECT COUNT(*) FROM users WHERE is_blacklisted=1").fetchone()[0]
            inbox_users  = conn.execute("SELECT COUNT(*) FROM users WHERE has_messaged_bot=1").fetchone()[0]
        interval  = get_setting("rot_interval", str(DEFAULT_ROT_INTERVAL))
        rot_on    = get_setting("rot_enabled", "1") == "1"
        rot_count = len(rot_active_messages())
        greet_on  = get_setting("greet_enabled", "1") == "1"
        inbox_req = get_setting("require_inbox", "1" if REQUIRE_INBOX else "0") == "1"
        req_post  = get_setting("require_post_comment", "0") == "1"
        post_id   = get_setting("required_post_id", "—")
        lines = [
            "📊 Статистика:",
            f"  Пользователей: {total_users} (inbox: {inbox_users})",
            f"  Выдач дизайнера: {total_grants} (активных: {active_now})",
            f"  В ЧС: {blacklisted}",
            f"  Цена: {fmt_price()} | {fmt_time(DESIGNER_DURATION)}",
            f"  Ротация: {'вкл' if rot_on else 'выкл'} | {rot_count} сообщ. | {interval}с.",
            f"  Приветствия: {'вкл' if greet_on else 'выкл'} | {len(greet_active_messages())} шт.",
            f"  Inbox-требование: {'вкл' if inbox_req else 'выкл'}",
            f"  Пост: {'вкл' if req_post else 'выкл'} ({post_id})",
            f"  В комнате сейчас: {len(self._users_in_room)}",
        ]
        await self._whisper(mod.id, lines)

    async def _cmd_top(self, mod: User) -> None:
        with db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT username, total_time_sec, messages_count, designer_count "
                "FROM users ORDER BY total_time_sec DESC LIMIT 10"
            ).fetchall()
        if not rows:
            await self.highrise.send_whisper(mod.id, "📈 Нет данных."); return
        lines = ["📈 Топ-10 по времени:"]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"  {i}. {r['username']} — {fmt_time(r['total_time_sec'])} "
                f"| 💬{r['messages_count']} | 🎨{r['designer_count']}"
            )
        await self._whisper(mod.id, lines)

    async def _cmd_pending(self, mod: User) -> None:
        if not self.pending_gold:
            await self.highrise.send_whisper(mod.id, "💰 Нет ожидающих платежей."); return
        lines = ["💰 Незавершённые платежи:"]
        for uid, amount in self.pending_gold.items():
            row = get_user_row(uid)
            name = row["username"] if row else uid
            lines.append(f"  {name}: {amount}/{DESIGNER_COST} зол.")
        await self._whisper(mod.id, lines)

    async def _cmd_setcost(self, mod: User, value: str) -> None:
        global DESIGNER_COST, FREE_DESIGNER
        if value.lower() in ("0", "free", "бесплатно"):
            FREE_DESIGNER = True; DESIGNER_COST = 0
            set_setting("free_designer", "1")
            set_setting("designer_cost", "0")
            await self.highrise.send_whisper(mod.id, "✅ Привилегия теперь бесплатна.")
            await self.highrise.chat("🎨 Привилегия дизайнера теперь бесплатна! Напишите '+'.")
        elif value.isdigit() and int(value) > 0:
            DESIGNER_COST = int(value); FREE_DESIGNER = False
            set_setting("free_designer", "0")
            set_setting("designer_cost", value)
            await self.highrise.send_whisper(mod.id, f"✅ Цена: {DESIGNER_COST} зол.")
            await self.highrise.chat(f"🎨 Цена на привилегию дизайнера: {DESIGNER_COST} золота.")
        else:
            await self.highrise.send_whisper(mod.id, "❌ Формат: setcost 100 или setcost 0")

    async def _cmd_setpost(self, mod: User, post_id: str) -> None:
        if not post_id:
            current = get_setting("required_post_id", "")
            req = get_setting("require_post_comment", "0") == "1"
            await self.highrise.send_whisper(
                mod.id,
                f"📌 Текущая публикация: {current or 'не задана'} | "
                f"Проверка: {'вкл' if req else 'выкл'}\n"
                f"Формат: setpost <ID публикации>"
            )
            return
        set_setting("required_post_id", post_id)
        await self.highrise.send_whisper(mod.id, f"✅ Публикация установлена: {post_id}")

    # ═══════════════════════════════════════════════════════
    #  Реализация команд — точки появления
    # ═══════════════════════════════════════════════════════

    async def _cmd_set_spawn(self, mod: User, key: str, args: str) -> None:
        label = "бота" if key == "bot_spawn" else "пользователей"
        if not args:
            current = get_setting(key)
            cmd_name = "setbotspawn" if key == "bot_spawn" else "setuserspawn"
            await self.highrise.send_whisper(
                mod.id,
                f"📍 Точка {label}: {current or 'не задана'}. "
                f"Формат: {cmd_name} x y z [facing]"
            )
            return
        pos = parse_position(args)
        if not pos:
            await self.highrise.send_whisper(
                mod.id, "❌ Формат: x y z [FrontRight|FrontLeft|BackRight|BackLeft]"
            )
            return
        set_setting(key, pos_to_str(pos))
        await self.highrise.send_whisper(
            mod.id,
            f"✅ Точка {label}: {pos.x} {pos.y} {pos.z} {pos.facing}"
        )
        if key == "bot_spawn":
            try:
                await self.highrise.walk_to(pos)
            except Exception as exc:
                print(f"[BOT] walk_to: {exc}")

    async def _cmd_show_spawns(self, mod: User) -> None:
        await self._whisper(mod.id, [
            "📍 Точки появления:",
            f"  Бот: {get_setting('bot_spawn') or 'не задана'}",
            f"  Пользователи: {get_setting('user_spawn') or 'не задана'}",
        ])

    async def _cmd_bot_go_spawn(self, mod: User) -> None:
        spawn_str = get_setting("bot_spawn")
        if not spawn_str:
            await self.highrise.send_whisper(mod.id, "❌ Точка бота не задана."); return
        pos = str_to_pos(spawn_str)
        if not pos:
            await self.highrise.send_whisper(mod.id, "❌ Ошибка чтения точки."); return
        try:
            await self.highrise.walk_to(pos)
            await self.highrise.send_whisper(mod.id, "✅ Бот перемещён.")
        except Exception as exc:
            await self.highrise.send_whisper(mod.id, f"❌ Ошибка: {exc}")

    # ═══════════════════════════════════════════════════════
    #  Реализация команд — наряд бота
    # ═══════════════════════════════════════════════════════

    async def _get_bot_outfit(self) -> list[Item]:
        resp = await self.highrise.get_user_outfit(self.highrise.my_id)
        if not hasattr(resp, "outfit"):
            raise RuntimeError(f"Ошибка получения наряда: {resp}")
        return list(resp.outfit)

    async def _cmd_copy_outfit(self, mod: User, target_username: str) -> None:
        if not target_username:
            await self.highrise.send_whisper(mod.id, "❌ Укажите ник: copyoutfit @ник"); return
        row = get_user_by_name(target_username)
        if not row:
            await self.highrise.send_whisper(mod.id, f"❌ '{target_username}' не найден в БД.")
            return
        try:
            resp = await self.highrise.get_user_outfit(row["user_id"])
            if not hasattr(resp, "outfit"):
                await self.highrise.send_whisper(mod.id, f"❌ Ошибка: {resp}"); return
            await self.highrise.set_outfit(resp.outfit)
            await self.highrise.send_whisper(
                mod.id, f"✅ Наряд скопирован у {target_username} ({len(resp.outfit)} предм.)."
            )
        except Exception as exc:
            await self.highrise.send_whisper(mod.id, f"❌ Ошибка: {exc}")

    async def _cmd_add_item(self, mod: User, item_id: str) -> None:
        if not item_id:
            await self.highrise.send_whisper(mod.id, "❌ Укажите item_id: additem <id>"); return
        try:
            outfit = await self._get_bot_outfit()
            if any(i.id == item_id for i in outfit):
                await self.highrise.send_whisper(mod.id, "ℹ️ Предмет уже надет."); return
            outfit.append(Item(type="clothing", amount=1, id=item_id))
            await self.highrise.set_outfit(outfit)
            await self.highrise.send_whisper(mod.id, f"✅ Предмет {item_id} надет.")
        except Exception as exc:
            await self.highrise.send_whisper(mod.id, f"❌ Ошибка: {exc}")

    async def _cmd_remove_item(self, mod: User, item_id: str) -> None:
        if not item_id:
            await self.highrise.send_whisper(mod.id, "❌ Укажите item_id: removeitem <id>"); return
        try:
            outfit = await self._get_bot_outfit()
            new_outfit = [i for i in outfit if i.id != item_id]
            if len(new_outfit) == len(outfit):
                await self.highrise.send_whisper(mod.id, f"ℹ️ Предмет {item_id} не найден."); return
            await self.highrise.set_outfit(new_outfit)
            await self.highrise.send_whisper(mod.id, f"✅ Предмет {item_id} снят.")
        except Exception as exc:
            await self.highrise.send_whisper(mod.id, f"❌ Ошибка: {exc}")

    async def _cmd_show_outfit(self, mod: User) -> None:
        try:
            outfit = await self._get_bot_outfit()
            if not outfit:
                await self.highrise.send_whisper(mod.id, "👗 Бот без наряда."); return
            lines = [f"👗 Наряд бота ({len(outfit)} предм.):"] + [f"  {i.id}" for i in outfit]
            await self._whisper(mod.id, lines)
        except Exception as exc:
            await self.highrise.send_whisper(mod.id, f"❌ Ошибка: {exc}")

    # ═══════════════════════════════════════════════════════
    #  Реализация команд — ротация сообщений
    # ═══════════════════════════════════════════════════════

    async def _cmd_addmsg(self, mod: User, text: str) -> None:
        if not text:
            await self.highrise.send_whisper(mod.id, "❌ Пустое сообщение."); return
        msg_id = rot_add(text)
        preview = text[:60] + ("…" if len(text) > 60 else "")
        await self.highrise.send_whisper(mod.id, f"✅ Сообщение #{msg_id} добавлено: {preview}")

    async def _cmd_removemsg(self, mod: User, msg_id: int) -> None:
        ok = rot_remove(msg_id)
        await self.highrise.send_whisper(
            mod.id,
            f"✅ Сообщение #{msg_id} отключено." if ok else f"❌ Сообщение #{msg_id} не найдено."
        )

    async def _cmd_listmsg(self, mod: User) -> None:
        messages = rot_list()
        if not messages:
            await self.highrise.send_whisper(mod.id, "📋 Нет сообщений."); return
        interval = get_setting("rot_interval", str(DEFAULT_ROT_INTERVAL))
        rot_on = get_setting("rot_enabled", "1") == "1"
        lines = [f"📋 Ротация ({'вкл' if rot_on else 'выкл'}, {interval}с.):"]
        for m in messages:
            status = "🟢" if m["is_active"] else "🔴"
            preview = m["message"][:50] + ("…" if len(m["message"]) > 50 else "")
            lines.append(f"  {status} #{m['id']}: {preview}")
        await self._whisper(mod.id, lines)

    # ═══════════════════════════════════════════════════════
    #  Реализация команд — приветствия
    # ═══════════════════════════════════════════════════════

    async def _cmd_addgreet(self, mod: User, text: str) -> None:
        if not text:
            await self.highrise.send_whisper(mod.id, "❌ Пустое приветствие."); return
        gid = greet_add(text)
        preview = text[:60] + ("…" if len(text) > 60 else "")
        await self.highrise.send_whisper(mod.id, f"✅ Приветствие #{gid} добавлено: {preview}")

    async def _cmd_listgreet(self, mod: User) -> None:
        msgs = greet_list()
        if not msgs:
            await self.highrise.send_whisper(mod.id, "📋 Нет приветствий."); return
        on = get_setting("greet_enabled", "1") == "1"
        lines = [f"📋 Приветствия ({'вкл' if on else 'выкл'}):"]
        for m in msgs:
            status = "🟢" if m["is_active"] else "🔴"
            preview = m["message"][:50] + ("…" if len(m["message"]) > 50 else "")
            lines.append(f"  {status} #{m['id']}: {preview}")
        await self._whisper(mod.id, lines)

    # ═══════════════════════════════════════════════════════
    #  Реализация команд — координаты
    # ═══════════════════════════════════════════════════════

    async def _cmd_coordinates(self, mod: User, target_username: str) -> None:
        if not target_username:
            await self.highrise.send_whisper(mod.id, "❌ Укажите ник: crd @ник"); return
        try:
            resp = await self.highrise.get_room_users()
            if not hasattr(resp, "content"):
                await self.highrise.send_whisper(mod.id, f"❌ Ошибка: {resp}"); return
            for user, pos in resp.content:
                if user.username.lower() == target_username.lower():
                    if isinstance(pos, Position):
                        await self.highrise.send_whisper(
                            mod.id,
                            f"📍 {user.username}: x={pos.x} y={pos.y} z={pos.z} ({pos.facing})"
                        )
                    else:
                        await self.highrise.send_whisper(
                            mod.id,
                            f"📍 {user.username}: anchor={pos.entity_id} ix={pos.anchor_ix}"
                        )
                    return
            await self.highrise.send_whisper(mod.id, f"❌ {target_username} не найден в комнате.") 
        except Exception as exc:
            await self.highrise.send_whisper(mod.id, f"❌ Ошибка: {exc}")

    # ═══════════════════════════════════════════════════════
    #  Реализация команд — broadcast
    # ═══════════════════════════════════════════════════════

    async def _cmd_broadcast(self, mod: User, text: str) -> None:
        if not text:
            await self.highrise.send_whisper(mod.id, "❌ Укажите текст: broadcast текст"); return

        with db() as conn:
            rows = conn.execute(
                "SELECT last_conv_id FROM users WHERE has_messaged_bot = 1 AND last_conv_id IS NOT NULL"
            ).fetchall()

        conv_ids = [r[0] for r in rows]
        if not conv_ids:
            await self.highrise.send_whisper(mod.id, "ℹ️ Нет пользователей написавших боту в inbox.")
            return

        await self.highrise.send_whisper(mod.id, f"📤 Начинаю рассылку {len(conv_ids)} пользователям…")
        sent = errors = 0
        for conv_id in conv_ids:
            try:
                await self.highrise.send_message(conv_id, text)
                sent += 1
            except Exception:
                errors += 1
            await asyncio.sleep(0.3)

        await self.highrise.send_whisper(mod.id, f"✅ Рассылка завершена. Отправлено: {sent}, ошибок: {errors}.")

    # ═══════════════════════════════════════════════════════
    #  !help
    # ═══════════════════════════════════════════════════════

    async def _cmd_help(self, mod: User) -> None:
        sections = [
            ["📖 Дизайнер:",
             "  give/take @ник | ban/unban @ник [причина]",
             "  profile @ник | blacklist | history [N]",
             "  wallet | stats | top | pending | setcost N"],
            ["📍 Точки появления:",
             "  setbotspawn x y z [facing] | clearbotspawn",
             "  setuserspawn x y z [facing] | clearuserspawn",
             "  spawns | botgo"],
            ["👗 Наряд бота:",
             "  copyoutfit @ник | additem id | removeitem id | botoutfit"],
            ["🔁 Ротация ({price} в тексте):",
             "  addmsg текст | removemsg N | listmsg",
             "  setinterval сек(≥10) | roton | rotoff"],
            ["👋 Приветствия ({username} в тексте):",
             "  addgreet текст | removegreet N | listgreet",
             "  greeton | greetoff"],
            ["📩 Inbox:",
             "  broadcast текст | inboxon / inboxoff"],
            ["📌 Публикация (только владелец):",
             "  setpost <ID> | clearpost | poston | postoff"],
            ["📍 Прочее:",
             "  crd @ник — координаты пользователя",
             "  Команды с ! и без, любой регистр"],
            ["💌 Admin-панель в inbox:",
             "  Напишите боту в личку — откроется меню управления"],
        ]
        for section in sections:
            await self._whisper(mod.id, section)
            await asyncio.sleep(0.4)

    # ═══════════════════════════════════════════════════════
    #  Вспомогательные методы
    # ═══════════════════════════════════════════════════════

    async def _whisper(self, user_id: str, lines: list[str], limit: int = 200) -> None:
        chunk: list[str] = []
        size = 0
        for line in lines:
            needed = len(line) + (1 if chunk else 0)
            if chunk and size + needed > limit:
                await self.highrise.send_whisper(user_id, "\n".join(chunk))
                await asyncio.sleep(0.3)
                chunk = [line]
                size = len(line)
            else:
                chunk.append(line)
                size += needed
        if chunk:
            await self.highrise.send_whisper(user_id, "\n".join(chunk))

    async def _get_account_age_days(self, user_id: str) -> Optional[int]:
        try:
            resp = await self.webapi.get_user(user_id)
            joined_at = resp.user.joined_at
            now = datetime.now(timezone.utc)
            if hasattr(joined_at, "tzinfo") and joined_at.tzinfo is None:
                joined_at = joined_at.replace(tzinfo=timezone.utc)
            return (now - joined_at).days
        except Exception as exc:
            print(f"[BOT] account age {user_id}: {exc}")
            return None
