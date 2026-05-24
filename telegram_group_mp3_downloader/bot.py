from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import BotCommand, Chat, Message, Update
from telegram.error import BadRequest, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

LOGGER = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".mp3", ".m4a", ".flac", ".wav"}
DOWNLOADABLE_EXTENSIONS = {".mp3", ".m4a"}
DEFAULT_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
OMITTED_PREVIEW_LIMIT = 12
GROUP_CHAT_TYPES = {"group", "supergroup"}
MIME_EXTENSION_MAP = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(timezone.utc).isoformat()


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on", "si", "sí"}


def parse_int_set(value: str | None) -> set[int]:
    result: set[int] = set()
    if not value:
        return result
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        result.add(int(chunk))
    return result


def resolve_config_path(value: str, default: str) -> Path:
    path = Path(value or default).expanduser()
    if path.is_absolute():
        return path
    return (Path.cwd() / path).resolve()


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: Path
    download_base: Path
    default_download_dir: Path | None
    allowed_user_ids: set[int]
    allowed_chat_ids: set[int]
    allow_absolute_download_paths: bool
    bot_api_base_url: str
    bot_api_file_base_url: str
    app_timezone: ZoneInfo
    progress_edit_seconds: float
    scheduler_poll_seconds: float
    max_download_bytes: int

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        if not bot_token:
            raise RuntimeError("BOT_TOKEN is required")
        timezone_name = os.getenv("APP_TIMEZONE", "America/Mexico_City").strip()
        return cls(
            bot_token=bot_token,
            db_path=resolve_config_path(os.getenv("DB_PATH", ""), "./data/bot.sqlite3"),
            download_base=resolve_config_path(os.getenv("DOWNLOAD_BASE", ""), "./downloads"),
            default_download_dir=resolve_config_path(os.getenv("DEFAULT_DOWNLOAD_DIR", ""), "")
            if os.getenv("DEFAULT_DOWNLOAD_DIR", "").strip()
            else None,
            allowed_user_ids=parse_int_set(os.getenv("ALLOWED_USER_IDS")),
            allowed_chat_ids=parse_int_set(os.getenv("ALLOWED_CHAT_IDS")),
            allow_absolute_download_paths=parse_bool(os.getenv("ALLOW_ABSOLUTE_DOWNLOAD_PATHS"), False),
            bot_api_base_url=os.getenv("BOT_API_BASE_URL", "https://api.telegram.org/bot").rstrip("/"),
            bot_api_file_base_url=os.getenv("BOT_API_FILE_BASE_URL", "https://api.telegram.org/file/bot").rstrip("/"),
            app_timezone=ZoneInfo(timezone_name),
            progress_edit_seconds=float(os.getenv("PROGRESS_EDIT_SECONDS", "2.5")),
            scheduler_poll_seconds=float(os.getenv("SCHEDULER_POLL_SECONDS", "30")),
            max_download_bytes=int(os.getenv("MAX_DOWNLOAD_BYTES", str(DEFAULT_MAX_DOWNLOAD_BYTES))),
        )


class Store:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.lock = asyncio.Lock()

    async def init(self) -> None:
        async with self.lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY,
                    type TEXT NOT NULL,
                    title TEXT,
                    username TEXT,
                    default_download_dir TEXT,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    file_unique_id TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    file_name TEXT,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    mime_type TEXT,
                    from_user_id INTEGER,
                    from_username TEXT,
                    message_date TEXT,
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_id, file_unique_id),
                    FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_files_chat_extension ON files(chat_id, extension);
                CREATE INDEX IF NOT EXISTS idx_files_chat_seen ON files(chat_id, first_seen_at);

                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_pk INTEGER NOT NULL,
                    target_dir TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    bytes_downloaded INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
                    error TEXT,
                    UNIQUE(file_pk, target_dir),
                    FOREIGN KEY(file_pk) REFERENCES files(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);

                CREATE TABLE IF NOT EXISTS scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    scanned_at TEXT NOT NULL,
                    known_files INTEGER NOT NULL,
                    total_size INTEGER NOT NULL,
                    new_since_last INTEGER NOT NULL,
                    downloaded_missing INTEGER NOT NULL DEFAULT 0,
                    target_dir TEXT,
                    status TEXT NOT NULL,
                    notes TEXT,
                    FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    created_by INTEGER,
                    notify_chat_id INTEGER,
                    interval_minutes INTEGER NOT NULL,
                    do_download INTEGER NOT NULL DEFAULT 0,
                    target_dir TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    next_run_at TEXT NOT NULL,
                    last_run_at TEXT,
                    FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS user_targets (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                );
                """
            )
            columns = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(chats)").fetchall()
            }
            if "default_download_dir" not in columns:
                self.conn.execute("ALTER TABLE chats ADD COLUMN default_download_dir TEXT")
            schedule_columns = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(schedules)").fetchall()
            }
            if "notify_chat_id" not in schedule_columns:
                self.conn.execute("ALTER TABLE schedules ADD COLUMN notify_chat_id INTEGER")
            self.conn.commit()

    async def close(self) -> None:
        async with self.lock:
            self.conn.close()

    async def upsert_chat(self, chat: Chat) -> None:
        async with self.lock:
            self.conn.execute(
                """
                INSERT INTO chats(chat_id, type, title, username, last_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    type=excluded.type,
                    title=excluded.title,
                    username=excluded.username,
                    last_seen_at=excluded.last_seen_at
                """,
                (chat.id, chat.type, chat.title or chat.full_name, chat.username, utc_iso()),
            )
            self.conn.commit()

    async def get_default_download_dir(self, chat_id: int) -> str | None:
        async with self.lock:
            row = self.conn.execute(
                "SELECT default_download_dir FROM chats WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
            return row["default_download_dir"] if row else None

    async def get_chat_row(self, chat_id: int) -> sqlite3.Row | None:
        async with self.lock:
            return self.conn.execute(
                "SELECT * FROM chats WHERE chat_id=?",
                (chat_id,),
            ).fetchone()

    async def list_groups(self) -> list[sqlite3.Row]:
        async with self.lock:
            return list(
                self.conn.execute(
                    """
                    SELECT * FROM chats
                    WHERE type IN ('group', 'supergroup')
                    ORDER BY last_seen_at DESC, title COLLATE NOCASE
                    """
                ).fetchall()
            )

    async def get_user_target(self, user_id: int) -> sqlite3.Row | None:
        async with self.lock:
            return self.conn.execute(
                """
                SELECT chats.*
                FROM user_targets
                JOIN chats ON chats.chat_id=user_targets.chat_id
                WHERE user_targets.user_id=? AND chats.type IN ('group', 'supergroup')
                """,
                (user_id,),
            ).fetchone()

    async def set_user_target(self, user_id: int, chat_id: int) -> None:
        async with self.lock:
            self.conn.execute(
                """
                INSERT INTO user_targets(user_id, chat_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    updated_at=excluded.updated_at
                """,
                (user_id, chat_id, utc_iso()),
            )
            self.conn.commit()

    async def set_default_download_dir(self, chat_id: int, target_dir: str | None) -> None:
        async with self.lock:
            self.conn.execute(
                "UPDATE chats SET default_download_dir=?, last_seen_at=? WHERE chat_id=?",
                (target_dir, utc_iso(), chat_id),
            )
            self.conn.commit()

    async def upsert_file(self, metadata: dict[str, Any]) -> tuple[int, bool]:
        async with self.lock:
            existing = self.conn.execute(
                "SELECT id FROM files WHERE chat_id=? AND file_unique_id=?",
                (metadata["chat_id"], metadata["file_unique_id"]),
            ).fetchone()
            created = existing is None
            self.conn.execute(
                """
                INSERT INTO files(
                    chat_id, message_id, file_unique_id, file_id, source_kind,
                    extension, file_name, file_size, mime_type, from_user_id,
                    from_username, message_date, first_seen_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, file_unique_id) DO UPDATE SET
                    file_id=excluded.file_id,
                    file_name=COALESCE(excluded.file_name, files.file_name),
                    file_size=excluded.file_size,
                    mime_type=COALESCE(excluded.mime_type, files.mime_type),
                    updated_at=excluded.updated_at
                """,
                (
                    metadata["chat_id"],
                    metadata["message_id"],
                    metadata["file_unique_id"],
                    metadata["file_id"],
                    metadata["source_kind"],
                    metadata["extension"],
                    metadata.get("file_name"),
                    metadata.get("file_size") or 0,
                    metadata.get("mime_type"),
                    metadata.get("from_user_id"),
                    metadata.get("from_username"),
                    metadata.get("message_date"),
                    metadata["first_seen_at"],
                    metadata["updated_at"],
                ),
            )
            row = self.conn.execute(
                "SELECT id FROM files WHERE chat_id=? AND file_unique_id=?",
                (metadata["chat_id"], metadata["file_unique_id"]),
            ).fetchone()
            self.conn.commit()
            return int(row["id"]), created

    async def latest_scan(self, chat_id: int) -> sqlite3.Row | None:
        async with self.lock:
            return self.conn.execute(
                "SELECT * FROM scans WHERE chat_id=? ORDER BY scanned_at DESC, id DESC LIMIT 1",
                (chat_id,),
            ).fetchone()

    async def stats(self, chat_id: int) -> dict[str, Any]:
        async with self.lock:
            rows = self.conn.execute(
                """
                SELECT extension, COUNT(*) AS count, COALESCE(SUM(file_size), 0) AS size
                FROM files
                WHERE chat_id=?
                GROUP BY extension
                ORDER BY extension
                """,
                (chat_id,),
            ).fetchall()
            total = self.conn.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(file_size), 0) AS size FROM files WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
            return {
                "total_count": int(total["count"]),
                "total_size": int(total["size"]),
                "by_extension": [
                    {"extension": row["extension"], "count": int(row["count"]), "size": int(row["size"])}
                    for row in rows
                ],
            }

    async def count_new_since(self, chat_id: int, since_iso: str | None) -> int:
        async with self.lock:
            if not since_iso:
                row = self.conn.execute("SELECT COUNT(*) AS count FROM files WHERE chat_id=?", (chat_id,)).fetchone()
            else:
                row = self.conn.execute(
                    "SELECT COUNT(*) AS count FROM files WHERE chat_id=? AND first_seen_at > ?",
                    (chat_id, since_iso),
                ).fetchone()
            return int(row["count"])

    async def create_scan(
        self,
        chat_id: int,
        known_files: int,
        total_size: int,
        new_since_last: int,
        status: str,
        downloaded_missing: int = 0,
        target_dir: str | None = None,
        notes: str | None = None,
    ) -> int:
        async with self.lock:
            cur = self.conn.execute(
                """
                INSERT INTO scans(
                    chat_id, scanned_at, known_files, total_size, new_since_last,
                    downloaded_missing, target_dir, status, notes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    utc_iso(),
                    known_files,
                    total_size,
                    new_since_last,
                    downloaded_missing,
                    target_dir,
                    status,
                    notes,
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    async def list_files(self, chat_id: int) -> list[sqlite3.Row]:
        async with self.lock:
            return list(
                self.conn.execute(
                    """
                    SELECT * FROM files
                    WHERE chat_id=?
                    ORDER BY message_date, message_id, id
                    """,
                    (chat_id,),
                ).fetchall()
            )

    async def get_download(self, file_pk: int, target_dir: str) -> sqlite3.Row | None:
        async with self.lock:
            return self.conn.execute(
                "SELECT * FROM downloads WHERE file_pk=? AND target_dir=?",
                (file_pk, target_dir),
            ).fetchone()

    async def mark_download_started(self, file_pk: int, target_dir: str, file_path: str) -> None:
        async with self.lock:
            stamp = utc_iso()
            self.conn.execute(
                """
                INSERT INTO downloads(file_pk, target_dir, file_path, status, bytes_downloaded, started_at, updated_at)
                VALUES (?, ?, ?, 'downloading', 0, ?, ?)
                ON CONFLICT(file_pk, target_dir) DO UPDATE SET
                    file_path=excluded.file_path,
                    status='downloading',
                    bytes_downloaded=0,
                    started_at=excluded.started_at,
                    completed_at=NULL,
                    updated_at=excluded.updated_at,
                    error=NULL
                """,
                (file_pk, target_dir, file_path, stamp, stamp),
            )
            self.conn.commit()

    async def mark_download_progress(self, file_pk: int, target_dir: str, bytes_downloaded: int) -> None:
        async with self.lock:
            self.conn.execute(
                """
                UPDATE downloads
                SET bytes_downloaded=?, updated_at=?
                WHERE file_pk=? AND target_dir=?
                """,
                (bytes_downloaded, utc_iso(), file_pk, target_dir),
            )
            self.conn.commit()

    async def mark_download_done(self, file_pk: int, target_dir: str, file_path: str, bytes_downloaded: int) -> None:
        async with self.lock:
            stamp = utc_iso()
            self.conn.execute(
                """
                INSERT INTO downloads(file_pk, target_dir, file_path, status, bytes_downloaded, completed_at, updated_at)
                VALUES (?, ?, ?, 'downloaded', ?, ?, ?)
                ON CONFLICT(file_pk, target_dir) DO UPDATE SET
                    file_path=excluded.file_path,
                    status='downloaded',
                    bytes_downloaded=excluded.bytes_downloaded,
                    completed_at=excluded.completed_at,
                    updated_at=excluded.updated_at,
                    error=NULL
                """,
                (file_pk, target_dir, file_path, bytes_downloaded, stamp, stamp),
            )
            self.conn.commit()

    async def mark_download_error(self, file_pk: int, target_dir: str, file_path: str, error: str) -> None:
        async with self.lock:
            stamp = utc_iso()
            self.conn.execute(
                """
                INSERT INTO downloads(file_pk, target_dir, file_path, status, updated_at, error)
                VALUES (?, ?, ?, 'error', ?, ?)
                ON CONFLICT(file_pk, target_dir) DO UPDATE SET
                    file_path=excluded.file_path,
                    status='error',
                    updated_at=excluded.updated_at,
                    error=excluded.error
                """,
                (file_pk, target_dir, file_path, stamp, error[:1000]),
            )
            self.conn.commit()

    async def create_schedule(
        self,
        chat_id: int,
        created_by: int | None,
        notify_chat_id: int | None,
        interval_minutes: int,
        do_download: bool,
        target_dir: str | None,
    ) -> int:
        async with self.lock:
            created_at = now_utc()
            next_run_at = created_at + timedelta(minutes=interval_minutes)
            cur = self.conn.execute(
                """
                INSERT INTO schedules(
                    chat_id, created_by, notify_chat_id, interval_minutes, do_download, target_dir,
                    enabled, created_at, next_run_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    chat_id,
                    created_by,
                    notify_chat_id,
                    interval_minutes,
                    1 if do_download else 0,
                    target_dir,
                    created_at.isoformat(),
                    next_run_at.isoformat(),
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    async def list_schedules(self, chat_id: int) -> list[sqlite3.Row]:
        async with self.lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM schedules WHERE chat_id=? AND enabled=1 ORDER BY id",
                    (chat_id,),
                ).fetchall()
            )

    async def disable_schedule(self, chat_id: int, schedule_id: int) -> bool:
        async with self.lock:
            cur = self.conn.execute(
                "UPDATE schedules SET enabled=0 WHERE chat_id=? AND id=? AND enabled=1",
                (chat_id, schedule_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    async def due_schedules(self) -> list[sqlite3.Row]:
        async with self.lock:
            return list(
                self.conn.execute(
                    """
                    SELECT * FROM schedules
                    WHERE enabled=1 AND next_run_at <= ?
                    ORDER BY next_run_at, id
                    """,
                    (utc_iso(),),
                ).fetchall()
            )

    async def mark_schedule_ran(self, schedule_id: int, interval_minutes: int) -> None:
        async with self.lock:
            stamp = now_utc()
            next_run_at = stamp + timedelta(minutes=interval_minutes)
            self.conn.execute(
                """
                UPDATE schedules
                SET last_run_at=?, next_run_at=?
                WHERE id=?
                """,
                (stamp.isoformat(), next_run_at.isoformat(), schedule_id),
            )
            self.conn.commit()


@dataclass
class DownloadState:
    total_known_files: int
    total_download_bytes: int
    target_dir: Path
    eligible_files: int = 0
    done_files: int = 0
    done_bytes: int = 0
    existing_files: int = 0
    omitted_type_files: int = 0
    omitted_type_bytes: int = 0
    omitted_size_files: int = 0
    omitted_size_bytes: int = 0
    error_files: int = 0
    current_name: str = ""
    current_bytes: int = 0
    current_total: int = 0
    last_error: str = ""
    omitted_preview: list[str] = field(default_factory=list)
    omitted_preview_overflow: int = 0
    started_at: float = field(default_factory=time.monotonic)


@dataclass
class TargetChat:
    chat_id: int
    chat: Chat
    from_private: bool


def get_store(context: ContextTypes.DEFAULT_TYPE | Application) -> Store:
    holder = context.application if hasattr(context, "application") else context
    return holder.bot_data["store"]


def get_config(context: ContextTypes.DEFAULT_TYPE | Application) -> Config:
    holder = context.application if hasattr(context, "application") else context
    return holder.bot_data["config"]


def is_group_chat_type(chat_type: str | None) -> bool:
    return chat_type in GROUP_CHAT_TYPES


def display_chat_title(chat: Chat | None, row: sqlite3.Row | None = None) -> str:
    if chat:
        return chat.title or chat.full_name or chat.username or str(chat.id)
    if row:
        return row["title"] or row["username"] or str(row["chat_id"])
    return "grupo desconocido"


def format_group_list(rows: list[sqlite3.Row]) -> str:
    lines = ["Grupos conocidos:"]
    for row in rows:
        title = row["title"] or row["username"] or "sin titulo"
        lines.append(f"{title} | ID: {row['chat_id']} | tipo: {row['type']}")
    lines.append("")
    lines.append("Usa /usar_grupo <id> para consultar ese grupo por privado.")
    return "\n".join(lines)


def human_size(size: int | None) -> str:
    value = float(size or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def safe_filename(value: str, fallback: str = "archivo") -> str:
    value = value.strip().replace("\\", "_").replace("/", "_")
    value = re.sub(r"[\x00-\x1f<>:\"|?*]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value[:140] or fallback


def chat_folder_name(chat: Chat | None, chat_id: int) -> str:
    title = ""
    if chat:
        title = chat.title or chat.full_name or chat.username or ""
    safe_title = safe_filename(title, "chat")
    return f"{safe_title}_{abs(chat_id)}"


def is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def resolve_target_dir(
    config: Config,
    chat: Chat | None,
    chat_id: int,
    folder_arg: str | None,
    default_dir: str | None = None,
) -> Path:
    requested = (folder_arg or "").strip() or (default_dir or "").strip()
    if not requested:
        target = config.download_base / chat_folder_name(chat, chat_id)
        return target.resolve()

    candidate = Path(requested).expanduser()
    if candidate.is_absolute():
        target = candidate.resolve()
        if not config.allow_absolute_download_paths and not is_relative_to(target, config.download_base.resolve()):
            raise ValueError(
                "Las rutas absolutas estan deshabilitadas. Usa una subcarpeta o activa "
                "ALLOW_ABSOLUTE_DOWNLOAD_PATHS=true."
            )
        return target

    target = (config.download_base / candidate).resolve()
    base = config.download_base.resolve()
    if not is_relative_to(target, base):
        raise ValueError("La carpeta debe quedar dentro de DOWNLOAD_BASE.")
    return target


async def resolve_download_target(
    context: ContextTypes.DEFAULT_TYPE | Application,
    chat: Chat | None,
    chat_id: int,
    folder_arg: str | None,
) -> Path:
    config = get_config(context)
    chat_default_dir = await get_store(context).get_default_download_dir(chat_id)
    default_dir = chat_default_dir or (str(config.default_download_dir) if config.default_download_dir else None)
    return resolve_target_dir(get_config(context), chat, chat_id, folder_arg, default_dir)


def local_path_for_file(target_dir: Path, file_row: sqlite3.Row) -> Path:
    extension = file_row["extension"]
    original_name = file_row["file_name"] or f"{file_row['file_unique_id']}{extension}"
    safe_name = safe_filename(original_name, f"{file_row['file_unique_id']}{extension}")
    if not safe_name.lower().endswith(extension):
        safe_name = f"{safe_name}{extension}"
    unique = safe_filename(str(file_row["file_unique_id"])[:16], "file")
    stem = Path(safe_name).stem[:90] or "audio"
    filename = f"{stem}__{unique}{extension}"
    return target_dir / extension.lstrip(".") / filename


def file_label(file_row: sqlite3.Row) -> str:
    name = file_row["file_name"] or f"{file_row['file_unique_id']}{file_row['extension']}"
    return safe_filename(name, "archivo")


def add_omitted_preview(state: DownloadState, reason: str, file_row: sqlite3.Row) -> None:
    size = human_size(int(file_row["file_size"] or 0))
    item = f"{reason}: {file_label(file_row)} ({file_row['extension']}, {size})"
    if len(state.omitted_preview) < OMITTED_PREVIEW_LIMIT:
        state.omitted_preview.append(item)
    else:
        state.omitted_preview_overflow += 1


def detect_extension(file_name: str | None, mime_type: str | None) -> str | None:
    if file_name:
        ext = Path(file_name).suffix.lower()
        if ext in SUPPORTED_EXTENSIONS:
            return ext
    if mime_type:
        return MIME_EXTENSION_MAP.get(mime_type.lower())
    return None


def extract_media_metadata(message: Message) -> dict[str, Any] | None:
    media = None
    source_kind = ""
    if message.audio:
        media = message.audio
        source_kind = "audio"
    elif message.document:
        media = message.document
        source_kind = "document"
    if media is None:
        return None

    file_name = getattr(media, "file_name", None)
    mime_type = getattr(media, "mime_type", None)
    extension = detect_extension(file_name, mime_type)
    if extension is None:
        return None

    user = message.from_user
    return {
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "file_unique_id": getattr(media, "file_unique_id", None) or f"message:{message.message_id}",
        "file_id": media.file_id,
        "source_kind": source_kind,
        "extension": extension,
        "file_name": file_name,
        "file_size": getattr(media, "file_size", 0) or 0,
        "mime_type": mime_type,
        "from_user_id": user.id if user else None,
        "from_username": user.username if user else None,
        "message_date": message.date.astimezone(timezone.utc).isoformat() if message.date else None,
        "first_seen_at": utc_iso(),
        "updated_at": utc_iso(),
    }


def is_command_allowed(update: Update, config: Config) -> tuple[bool, str]:
    chat = update.effective_chat
    user = update.effective_user
    if chat and chat.type != "private" and config.allowed_chat_ids and chat.id not in config.allowed_chat_ids:
        return False, "Este grupo no esta autorizado en ALLOWED_CHAT_IDS."
    if user and config.allowed_user_ids and user.id not in config.allowed_user_ids:
        return False, "Tu usuario no esta autorizado en ALLOWED_USER_IDS."
    return True, ""


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    config = get_config(context)
    allowed, reason = is_command_allowed(update, config)
    if not allowed and update.effective_message:
        await update.effective_message.reply_text(reason)
    if allowed and update.effective_chat:
        await get_store(context).upsert_chat(update.effective_chat)
    return allowed


async def fetch_target_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    row: sqlite3.Row | None,
) -> Chat | None:
    try:
        chat = await context.bot.get_chat(chat_id)
        await get_store(context).upsert_chat(chat)
        return chat
    except TelegramError as exc:
        LOGGER.warning("Could not fetch target chat %s: %s", chat_id, exc)
        return None


async def resolve_target_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> TargetChat | None:
    source_chat = update.effective_chat
    message = update.effective_message
    if not source_chat:
        return None

    if source_chat.type != "private":
        if not is_group_chat_type(source_chat.type):
            if message:
                await message.reply_text("Este comando necesita un grupo o un privado con grupo seleccionado.")
            return None
        return TargetChat(chat_id=source_chat.id, chat=source_chat, from_private=False)

    user = update.effective_user
    if not user:
        if message:
            await message.reply_text("No pude identificar tu usuario para elegir grupo.")
        return None

    store = get_store(context)
    config = get_config(context)
    target_row = await store.get_user_target(user.id)
    if target_row is None:
        groups = await store.list_groups()
        if len(groups) == 1:
            target_row = groups[0]
            await store.set_user_target(user.id, int(target_row["chat_id"]))
        elif not groups:
            if message:
                await message.reply_text(
                    "Todavia no tengo ningun grupo registrado. Manda cualquier mensaje o /info_grupo dentro del grupo una vez; despues ya puedes consultarme por privado."
                )
            return None
        else:
            if message:
                await message.reply_text(format_group_list(groups))
            return None

    target_chat_id = int(target_row["chat_id"])
    if config.allowed_chat_ids and target_chat_id not in config.allowed_chat_ids:
        if message:
            await message.reply_text("El grupo seleccionado no esta autorizado en ALLOWED_CHAT_IDS.")
        return None

    target_chat = await fetch_target_chat(context, target_chat_id, target_row)
    if target_chat is None:
        if message:
            await message.reply_text("No pude acceder al grupo seleccionado. Revisa que siga agregado al grupo.")
        return None
    return TargetChat(chat_id=target_chat_id, chat=target_chat, from_private=True)


def format_stats(title: str, stats: dict[str, Any], new_since_last: int, include_note: bool = True) -> str:
    lines = [
        "Resumen de audio",
        f"Grupo: {title}",
        f"Total: {stats['total_count']} archivos | {human_size(stats['total_size'])}",
        f"Nuevos desde el ultimo scan: {new_since_last}",
        "",
        "Por tipo:",
    ]
    if stats["by_extension"]:
        for item in stats["by_extension"]:
            lines.append(f"{item['extension']}: {item['count']} archivos | {human_size(item['size'])}")
    else:
        lines.append("Sin archivos catalogados todavia.")

    if include_note:
        lines.extend(
            [
                "",
                "Nota: con Bot API solo puedo contar archivos que me hayan llegado desde que estoy en el grupo.",
            ]
        )
    return "\n".join(lines)


def progress_text(state: DownloadState, final: bool = False) -> str:
    processed = (
        state.done_files
        + state.existing_files
        + state.omitted_type_files
        + state.omitted_size_files
        + state.error_files
    )
    percent = 0.0
    if state.total_download_bytes:
        percent = min(100.0, (state.done_bytes + state.current_bytes) * 100 / state.total_download_bytes)
    elif final:
        percent = 100.0
    elapsed = max(1.0, time.monotonic() - state.started_at)
    speed = (state.done_bytes + state.current_bytes) / elapsed
    title = "Descarga terminada" if final else "Descarga en curso"
    lines = [
        title,
        f"Destino: {state.target_dir}",
        f"Archivos conocidos: {processed}/{state.total_known_files}",
        f"Descargables: {state.eligible_files} | ok {state.done_files} | ya existentes {state.existing_files} | errores {state.error_files}",
        f"Omitidos: tipo {state.omitted_type_files} | peso {state.omitted_size_files}",
        f"Datos descargables: {human_size(state.done_bytes + state.current_bytes)} / {human_size(state.total_download_bytes)} ({percent:.1f}%)",
        f"Velocidad: {human_size(int(speed))}/s",
    ]
    if state.current_name and not final:
        lines.append(f"Actual: {state.current_name} ({human_size(state.current_bytes)} / {human_size(state.current_total)})")
    if state.last_error:
        lines.append(f"Ultimo error: {state.last_error[:220]}")
    if final and state.omitted_preview:
        lines.append("")
        lines.append("Lista de omitidos:")
        lines.extend(state.omitted_preview)
        if state.omitted_preview_overflow:
            lines.append(f"... y {state.omitted_preview_overflow} mas.")
    return "\n".join(lines)


async def safe_edit(message: Message, text: str) -> None:
    try:
        await message.edit_text(text)
    except BadRequest as exc:
        if "Message is not modified" not in str(exc):
            LOGGER.warning("Could not edit progress message: %s", exc)
    except RetryAfter as exc:
        await asyncio.sleep(float(exc.retry_after))
    except (TimedOut, TelegramError) as exc:
        LOGGER.warning("Could not edit progress message: %s", exc)


async def maybe_edit_progress(
    message: Message,
    state: DownloadState,
    config: Config,
    last_edit: list[float],
    force: bool = False,
    final: bool = False,
) -> None:
    now = time.monotonic()
    if force or now - last_edit[0] >= config.progress_edit_seconds:
        last_edit[0] = now
        await safe_edit(message, progress_text(state, final=final))


async def stream_download(
    context: ContextTypes.DEFAULT_TYPE | Application,
    file_row: sqlite3.Row,
    destination: Path,
    state: DownloadState,
    progress_message: Message,
    last_edit: list[float],
) -> int:
    config = get_config(context)
    tg_file = await context.bot.get_file(file_row["file_id"])
    file_path = tg_file.file_path
    if not file_path:
        raise RuntimeError("Telegram no devolvio file_path para este archivo.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_destination = destination.with_name(f"{destination.name}.part")
    state.current_name = destination.name
    state.current_total = int(file_row["file_size"] or 0)
    state.current_bytes = 0

    parsed = urlparse(file_path)
    if parsed.scheme in {"http", "https"}:
        timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", file_path) as response:
                response.raise_for_status()
                with temp_destination.open("wb") as handle:
                    async for chunk in response.aiter_bytes(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        state.current_bytes += len(chunk)
                        await maybe_edit_progress(progress_message, state, config, last_edit)
    else:
        source = Path(file_path)
        if not source.exists():
            raise RuntimeError(f"El Bot API local devolvio una ruta que no existe: {source}")
        await asyncio.to_thread(shutil.copyfile, source, temp_destination)
        state.current_bytes = temp_destination.stat().st_size
        await maybe_edit_progress(progress_message, state, config, last_edit, force=True)

    temp_destination.replace(destination)
    return destination.stat().st_size


async def create_scan_snapshot(context: ContextTypes.DEFAULT_TYPE | Application, chat_id: int) -> tuple[dict[str, Any], int]:
    store = get_store(context)
    latest = await store.latest_scan(chat_id)
    stats = await store.stats(chat_id)
    new_since_last = await store.count_new_since(chat_id, latest["scanned_at"] if latest else None)
    await store.create_scan(
        chat_id=chat_id,
        known_files=stats["total_count"],
        total_size=stats["total_size"],
        new_since_last=new_since_last,
        status="ok",
    )
    return stats, new_since_last


async def download_missing_for_chat(
    context: ContextTypes.DEFAULT_TYPE | Application,
    chat_id: int,
    chat: Chat | None,
    target_dir: Path,
    progress_message: Message,
) -> DownloadState:
    store = get_store(context)
    config = get_config(context)
    holder = context.application if hasattr(context, "application") else context
    lock_map: dict[int, asyncio.Lock] = holder.bot_data.setdefault("download_locks", {})
    lock = lock_map.setdefault(chat_id, asyncio.Lock())
    if lock.locked():
        await safe_edit(progress_message, "Ya hay una descarga en curso para este grupo.")
        return DownloadState(0, 0, target_dir)

    async with lock:
        target_dir.mkdir(parents=True, exist_ok=True)
        rows = await store.list_files(chat_id)
        planned: list[tuple[sqlite3.Row, Path]] = []
        existing = 0
        omitted_type = 0
        omitted_type_bytes = 0
        omitted_size = 0
        omitted_size_bytes = 0
        for row in rows:
            size = int(row["file_size"] or 0)
            extension = row["extension"]
            if extension not in DOWNLOADABLE_EXTENSIONS:
                omitted_type += 1
                omitted_type_bytes += size
                continue
            if config.max_download_bytes > 0 and size > config.max_download_bytes:
                omitted_size += 1
                omitted_size_bytes += size
                continue
            destination = local_path_for_file(target_dir, row)
            download = await store.get_download(int(row["id"]), str(target_dir))
            expected_size = size
            exists_ok = destination.exists() and (expected_size == 0 or destination.stat().st_size == expected_size)
            if download and download["status"] == "downloaded" and exists_ok:
                existing += 1
                continue
            if exists_ok:
                await store.mark_download_done(int(row["id"]), str(target_dir), str(destination), destination.stat().st_size)
                existing += 1
                continue
            planned.append((row, destination))

        state = DownloadState(
            total_known_files=len(rows),
            total_download_bytes=sum(int(row["file_size"] or 0) for row, _ in planned),
            target_dir=target_dir,
            eligible_files=len(planned) + existing,
            existing_files=existing,
            omitted_type_files=omitted_type,
            omitted_type_bytes=omitted_type_bytes,
            omitted_size_files=omitted_size,
            omitted_size_bytes=omitted_size_bytes,
        )
        for row in rows:
            size = int(row["file_size"] or 0)
            extension = row["extension"]
            if extension not in DOWNLOADABLE_EXTENSIONS:
                add_omitted_preview(state, "tipo", row)
            elif config.max_download_bytes > 0 and size > config.max_download_bytes:
                add_omitted_preview(state, "peso", row)

        last_edit = [0.0]
        await maybe_edit_progress(progress_message, state, config, last_edit, force=True)

        for row, destination in planned:
            file_pk = int(row["id"])
            try:
                await store.mark_download_started(file_pk, str(target_dir), str(destination))
                downloaded_bytes = await stream_download(context, row, destination, state, progress_message, last_edit)
                await store.mark_download_done(file_pk, str(target_dir), str(destination), downloaded_bytes)
                state.done_files += 1
                state.done_bytes += downloaded_bytes
                state.current_name = ""
                state.current_bytes = 0
                state.current_total = 0
                await maybe_edit_progress(progress_message, state, config, last_edit, force=True)
            except Exception as exc:  # noqa: BLE001 - report per-file errors and continue.
                LOGGER.exception("Download failed for file %s", row["id"])
                state.error_files += 1
                state.last_error = f"{row['file_name'] or row['file_unique_id']}: {exc}"
                await store.mark_download_error(file_pk, str(target_dir), str(destination), str(exc))
                state.current_name = ""
                state.current_bytes = 0
                state.current_total = 0
                await maybe_edit_progress(progress_message, state, config, last_edit, force=True)

        await maybe_edit_progress(progress_message, state, config, last_edit, force=True, final=True)
        return state


async def observe_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    config = get_config(context)
    if chat.type != "private" and config.allowed_chat_ids and chat.id not in config.allowed_chat_ids:
        return
    await get_store(context).upsert_chat(chat)
    metadata = extract_media_metadata(message)
    if metadata is None:
        return
    _, created = await get_store(context).upsert_file(metadata)
    if created:
        LOGGER.info(
            "Indexed %s from chat %s message %s (%s)",
            metadata["file_name"],
            chat.id,
            message.message_id,
            human_size(metadata["file_size"]),
        )


async def observe_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat or not is_group_chat_type(chat.type):
        return
    config = get_config(context)
    if config.allowed_chat_ids and chat.id not in config.allowed_chat_ids:
        return
    await get_store(context).upsert_chat(chat)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    text = "\n".join(
        [
            "Comandos disponibles:",
            "/info_grupo - confirma que estoy en el grupo correcto",
            "/grupos - lista grupos conocidos para usar por privado",
            "/usar_grupo <id> - fija el grupo que consulto por privado",
            "/scan o /resumen - conteo total y por tipo",
            "/carpeta [ruta] - muestra o guarda la carpeta persistente",
            "/descargar [carpeta] - baja faltantes a una carpeta",
            "/sincronizar [carpeta] - scan + descarga faltantes",
            "/programar <minutos> [descargar] [carpeta] - agenda escaneo o descarga",
            "/programaciones - lista agendas activas",
            "/cancelar_programacion <id> - cancela una agenda",
            "/whoami - muestra tu ID para permisos",
            "",
            "La descarga solo toma .mp3 y .m4a de hasta 20 MB; lo demas sale como omitido.",
            "",
            "Tip: para ver audios nuevos del grupo, agregame como admin o desactiva Privacy Mode en BotFather.",
        ]
    )
    await update.effective_message.reply_text(text)


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    user = update.effective_user
    chat = update.effective_chat
    lines = [
        f"Tu user_id: {user.id if user else 'desconocido'}",
        f"Chat_id actual: {chat.id if chat else 'desconocido'}",
    ]
    if chat and chat.type == "private" and user:
        target = await get_store(context).get_user_target(user.id)
        if target:
            lines.append(f"Grupo seleccionado: {target['title'] or target['chat_id']} ({target['chat_id']})")
        else:
            lines.append("Grupo seleccionado: ninguno")
    await update.effective_message.reply_text("\n".join(lines))


async def groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    rows = await get_store(context).list_groups()
    if not rows:
        await update.effective_message.reply_text(
            "Todavia no tengo grupos registrados. Manda cualquier mensaje o /info_grupo dentro del grupo una vez."
        )
        return
    await update.effective_message.reply_text(format_group_list(rows))


async def use_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    user = update.effective_user
    if not user:
        await update.effective_message.reply_text("No pude identificar tu usuario.")
        return
    rows = await get_store(context).list_groups()
    if not context.args:
        if len(rows) == 1:
            row = rows[0]
            await get_store(context).set_user_target(user.id, int(row["chat_id"]))
            await update.effective_message.reply_text(
                f"Grupo seleccionado: {row['title'] or row['chat_id']} ({row['chat_id']})"
            )
            return
        if rows:
            await update.effective_message.reply_text(format_group_list(rows))
        else:
            await update.effective_message.reply_text(
                "No tengo grupos registrados todavia. Manda cualquier mensaje o /info_grupo dentro del grupo una vez."
            )
        return

    try:
        chat_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Usa el ID numerico del grupo: /usar_grupo <id>")
        return

    row = await get_store(context).get_chat_row(chat_id)
    if not row or not is_group_chat_type(row["type"]):
        await update.effective_message.reply_text("No encontre ese grupo. Revisa /grupos.")
        return
    await get_store(context).set_user_target(user.id, chat_id)
    await update.effective_message.reply_text(f"Grupo seleccionado: {row['title'] or chat_id} ({chat_id})")


async def group_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    target = await resolve_target_chat(update, context)
    if not target:
        return
    chat = target.chat
    bot_user = await context.bot.get_me()
    full_chat = await context.bot.get_chat(chat.id)
    bot_member = await context.bot.get_chat_member(chat.id, bot_user.id)
    members_line = "Miembros: no disponible"
    try:
        count = await context.bot.get_chat_member_count(chat.id)
        members_line = f"Miembros: {count}"
    except TelegramError:
        pass
    config = get_config(context)
    chat_default_dir = await get_store(context).get_default_download_dir(chat.id)
    default_dir = chat_default_dir or (str(config.default_download_dir) if config.default_download_dir else None)
    resolved_dir = resolve_target_dir(get_config(context), chat, chat.id, None, default_dir)

    lines = [
        "Info del grupo",
        f"Titulo: {full_chat.title or chat.title or full_chat.full_name}",
        f"ID: {chat.id}",
        f"Tipo: {chat.type}",
        f"Username: @{full_chat.username}" if full_chat.username else "Username: sin username",
        members_line,
        f"Bot: @{bot_user.username}",
        f"Estado del bot: {bot_member.status}",
        f"Carpeta de descarga: {resolved_dir}",
        "",
        "Si no aparecen archivos nuevos, revisa Privacy Mode o hazme admin.",
    ]
    if target.from_private:
        lines.insert(1, "Consultado desde privado")
    await update.effective_message.reply_text("\n".join(lines))


async def folder_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    target = await resolve_target_chat(update, context)
    if not target:
        return
    chat = target.chat
    folder_arg = " ".join(context.args).strip()
    store = get_store(context)
    config = get_config(context)

    if folder_arg.lower() in {"reset", "default", "quitar", "limpiar"}:
        await store.set_default_download_dir(target.chat_id, None)
        fallback = resolve_target_dir(
            config,
            chat,
            target.chat_id,
            None,
            str(config.default_download_dir) if config.default_download_dir else None,
        )
        await update.effective_message.reply_text(f"Carpeta persistente eliminada. Usare: {fallback}")
        return

    if folder_arg:
        try:
            target_dir = resolve_target_dir(config, chat, target.chat_id, folder_arg, None)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        await store.set_default_download_dir(target.chat_id, str(target_dir))
        await update.effective_message.reply_text(f"Carpeta persistente guardada:\n{target_dir}")
        return

    chat_default_dir = await store.get_default_download_dir(target.chat_id)
    default_dir = chat_default_dir or (str(config.default_download_dir) if config.default_download_dir else None)
    target_dir = resolve_target_dir(config, chat, target.chat_id, None, default_dir)
    source = "persistente" if chat_default_dir else "global" if config.default_download_dir else "automatica"
    await update.effective_message.reply_text(f"Carpeta actual ({source}):\n{target_dir}")


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    target = await resolve_target_chat(update, context)
    if not target:
        return
    chat = target.chat
    stats, new_since_last = await create_scan_snapshot(context, target.chat_id)
    title = chat.title or chat.full_name or str(chat.id)
    await update.effective_message.reply_text(format_stats(title, stats, new_since_last))


async def download_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    target = await resolve_target_chat(update, context)
    if not target:
        return
    chat = target.chat
    folder_arg = " ".join(context.args).strip()
    try:
        target_dir = await resolve_download_target(context, chat, target.chat_id, folder_arg)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    stats, new_since_last = await create_scan_snapshot(context, target.chat_id)
    message = await update.effective_message.reply_text(f"Preparando descarga...\nDestino: {target_dir}")
    state = await download_missing_for_chat(context, target.chat_id, chat, target_dir, message)
    await get_store(context).create_scan(
        chat_id=target.chat_id,
        known_files=stats["total_count"],
        total_size=stats["total_size"],
        new_since_last=new_since_last,
        downloaded_missing=state.done_files,
        target_dir=str(target_dir),
        status="downloaded" if state.error_files == 0 else "partial",
    )


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    target = await resolve_target_chat(update, context)
    if not target:
        return
    chat = target.chat
    stats, new_since_last = await create_scan_snapshot(context, target.chat_id)
    folder_arg = " ".join(context.args).strip()
    try:
        target_dir = await resolve_download_target(context, chat, target.chat_id, folder_arg)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    title = chat.title or chat.full_name or str(chat.id)
    message = await update.effective_message.reply_text(
        format_stats(title, stats, new_since_last, include_note=False)
        + f"\n\nDescargando faltantes en: {target_dir}"
    )
    await download_missing_for_chat(context, target.chat_id, chat, target_dir, message)


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    target = await resolve_target_chat(update, context)
    if not target:
        return
    chat = target.chat
    if not context.args:
        await update.effective_message.reply_text(
            "Uso: /programar <minutos> [descargar] [carpeta]\nEjemplo: /programar 60 descargar Musica"
        )
        return
    try:
        interval = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("El primer argumento debe ser un numero de minutos.")
        return
    if interval < 1:
        await update.effective_message.reply_text("El intervalo minimo es 1 minuto.")
        return

    do_download = len(context.args) >= 2 and context.args[1].lower() in {"descargar", "download", "sync", "sincronizar"}
    folder_arg = " ".join(context.args[2:]).strip() if do_download else ""
    target_dir: Path | None = None
    if do_download:
        try:
            target_dir = await resolve_download_target(context, chat, target.chat_id, folder_arg)
        except ValueError as exc:
            await update.effective_message.reply_text(str(exc))
            return

    schedule_id = await get_store(context).create_schedule(
        chat_id=target.chat_id,
        created_by=update.effective_user.id if update.effective_user else None,
        notify_chat_id=update.effective_chat.id if update.effective_chat else target.chat_id,
        interval_minutes=interval,
        do_download=do_download,
        target_dir=str(target_dir) if target_dir and folder_arg else None,
    )
    action = "scan + descarga" if do_download else "scan"
    target_text = f"\nDestino: {target_dir}" if target_dir else ""
    await update.effective_message.reply_text(
        f"Programacion #{schedule_id} creada: {action} cada {interval} minutos.{target_text}"
    )


async def schedules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    target = await resolve_target_chat(update, context)
    if not target:
        return
    rows = await get_store(context).list_schedules(target.chat_id)
    if not rows:
        await update.effective_message.reply_text("No hay programaciones activas para este grupo.")
        return
    config = get_config(context)
    chat_default_dir = await get_store(context).get_default_download_dir(target.chat_id)
    default_dir = chat_default_dir or (str(config.default_download_dir) if config.default_download_dir else None)
    default_target = resolve_target_dir(config, target.chat, target.chat_id, None, default_dir)
    lines = [f"Programaciones activas para {display_chat_title(target.chat)}:"]
    for row in rows:
        next_run = datetime.fromisoformat(row["next_run_at"]).astimezone(config.app_timezone)
        action = "scan + descarga" if row["do_download"] else "scan"
        if row["do_download"]:
            target = f" -> {row['target_dir']}" if row["target_dir"] else f" -> carpeta persistente/default: {default_target}"
        else:
            target = ""
        lines.append(f"#{row['id']}: {action} cada {row['interval_minutes']} min, proxima {next_run:%Y-%m-%d %H:%M}{target}")
    await update.effective_message.reply_text("\n".join(lines))


async def cancel_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update, context):
        return
    target = await resolve_target_chat(update, context)
    if not target:
        return
    if not context.args:
        await update.effective_message.reply_text("Uso: /cancelar_programacion <id>")
        return
    try:
        schedule_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("El id debe ser numerico.")
        return
    disabled = await get_store(context).disable_schedule(target.chat_id, schedule_id)
    if disabled:
        await update.effective_message.reply_text(f"Programacion #{schedule_id} cancelada.")
    else:
        await update.effective_message.reply_text(f"No encontre una programacion activa #{schedule_id} en este grupo.")


async def run_scheduled_job(app: Application, row: sqlite3.Row) -> None:
    chat_id = int(row["chat_id"])
    store = get_store(app)
    await store.mark_schedule_ran(int(row["id"]), int(row["interval_minutes"]))
    try:
        chat = await app.bot.get_chat(chat_id)
        stats, new_since_last = await create_scan_snapshot(app, chat_id)
        title = chat.title or chat.full_name or str(chat_id)
        notify_chat_id = int(row["notify_chat_id"] or chat_id)
        if row["do_download"]:
            target_dir = Path(row["target_dir"]) if row["target_dir"] else await resolve_download_target(app, chat, chat_id, None)
            message = await app.bot.send_message(
                chat_id=notify_chat_id,
                text=format_stats(title, stats, new_since_last, include_note=False)
                + f"\n\nProgramacion #{row['id']}: descargando faltantes en {target_dir}",
            )
            await download_missing_for_chat(app, chat_id, chat, target_dir, message)
        else:
            await app.bot.send_message(
                chat_id=notify_chat_id,
                text=f"Programacion #{row['id']}\n" + format_stats(title, stats, new_since_last),
            )
    except Exception:
        LOGGER.exception("Scheduled job %s failed", row["id"])


async def scheduler_loop(app: Application) -> None:
    config = get_config(app)
    while True:
        try:
            due = await get_store(app).due_schedules()
            for row in due:
                app.create_task(run_scheduled_job(app, row), update=None)
        except Exception:
            LOGGER.exception("Scheduler loop failed")
        await asyncio.sleep(config.scheduler_poll_seconds)


async def post_init(app: Application) -> None:
    await get_store(app).init()
    await app.bot.set_my_commands(
        [
            BotCommand("ayuda", "Ver comandos"),
            BotCommand("info_grupo", "Ver informacion del grupo"),
            BotCommand("grupos", "Listar grupos conocidos"),
            BotCommand("usar_grupo", "Elegir grupo para privado"),
            BotCommand("scan", "Contar audios conocidos"),
            BotCommand("carpeta", "Ver o guardar carpeta persistente"),
            BotCommand("descargar", "Descargar faltantes"),
            BotCommand("sincronizar", "Scan y descarga de faltantes"),
            BotCommand("programar", "Agendar scan o descarga"),
            BotCommand("programaciones", "Listar agendas"),
            BotCommand("cancelar_programacion", "Cancelar agenda"),
            BotCommand("whoami", "Ver IDs para permisos"),
        ]
    )
    app.bot_data["scheduler_task"] = asyncio.create_task(scheduler_loop(app), name="scheduler_loop")
    LOGGER.info("Bot initialized")


async def post_shutdown(app: Application) -> None:
    task = app.bot_data.get("scheduler_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await get_store(app).close()


def build_application(config: Config) -> Application:
    store = Store(config.db_path)
    builder = (
        Application.builder()
        .token(config.bot_token)
        .base_url(config.bot_api_base_url)
        .base_file_url(config.bot_api_file_base_url)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .concurrent_updates(True)
    )
    app = builder.build()
    app.bot_data["config"] = config
    app.bot_data["store"] = store
    app.add_handler(CommandHandler(["start", "ayuda", "help"], help_command), group=0)
    app.add_handler(CommandHandler("whoami", whoami_command), group=0)
    app.add_handler(CommandHandler("grupos", groups_command), group=0)
    app.add_handler(CommandHandler("usar_grupo", use_group_command), group=0)
    app.add_handler(CommandHandler("info_grupo", group_info_command), group=0)
    app.add_handler(CommandHandler(["scan", "resumen"], scan_command), group=0)
    app.add_handler(CommandHandler(["carpeta", "setcarpeta"], folder_command), group=0)
    app.add_handler(CommandHandler("descargar", download_command), group=0)
    app.add_handler(CommandHandler(["sincronizar", "sync"], sync_command), group=0)
    app.add_handler(CommandHandler("programar", schedule_command), group=0)
    app.add_handler(CommandHandler("programaciones", schedules_command), group=0)
    app.add_handler(CommandHandler("cancelar_programacion", cancel_schedule_command), group=0)
    app.add_handler(ChatMemberHandler(observe_chat_member, ChatMemberHandler.MY_CHAT_MEMBER), group=1)
    app.add_handler(MessageHandler(~filters.COMMAND, observe_message), group=1)
    return app


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    config = Config.from_env()
    config.download_base.mkdir(parents=True, exist_ok=True)
    app = build_application(config)
    LOGGER.info("Starting polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
