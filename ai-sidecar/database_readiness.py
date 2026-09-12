"""Local SQLite checks. Error evidence never contains SQL, paths or user rows."""
import errno
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

MESSAGES = {
    "DATABASE_NOT_CREATED": "本地记忆库文件尚未创建",
    "DATABASE_BUSY": "本地记忆库正被其他任务占用",
    "DATABASE_READ_ONLY": "本地记忆库或所在目录为只读",
    "DATABASE_DISK_FULL": "磁盘空间不足，无法写入本地记忆库",
    "DATABASE_ACCESS_DENIED": "无法访问本地记忆库或所在目录",
    "DATABASE_CORRUPT": "本地记忆库完整性检查未通过",
    "DATABASE_IO_ERROR": "读写本地记忆库时发生磁盘输入输出错误",
    "DATABASE_MIGRATION_INCOMPLETE": "本地记忆库升级尚未完成",
    "DATABASE_MIGRATION_FAILED": "本地记忆库升级失败",
    "DATABASE_MIGRATION_TIMEOUT": "本地记忆库仍在初始化，等待已超时",
    "DATABASE_PATH_MISMATCH": "核心服务与初始化器使用的记忆库位置不一致",
    "DATABASE_INITIALIZATION_FAILED": "本地记忆库检查失败",
}

class DatabaseFailure(Exception):
    def __init__(self, code: str, evidence: Optional[Dict[str, Any]] = None):
        super().__init__(MESSAGES[code])
        self.code = code
        self.evidence = evidence or {}


def classify(error: Exception) -> DatabaseFailure:
    numeric = getattr(error, "sqlite_errorcode", None)
    primary = numeric & 255 if isinstance(numeric, int) else None
    code = {5: "DATABASE_BUSY", 6: "DATABASE_BUSY", 8: "DATABASE_READ_ONLY",
            13: "DATABASE_DISK_FULL", 11: "DATABASE_CORRUPT", 26: "DATABASE_CORRUPT",
            3: "DATABASE_ACCESS_DENIED", 14: "DATABASE_ACCESS_DENIED",
            23: "DATABASE_ACCESS_DENIED", 10: "DATABASE_IO_ERROR"}.get(primary)
    # Python 3.9 does not expose SQLite numeric codes. Match only stable SQLite
    # messages locally; never persist their raw text (it may contain user data).
    if code is None and isinstance(error, sqlite3.Error):
        message = str(error).lower()
        for fragment, candidate in (
            ("locked", "DATABASE_BUSY"), ("readonly", "DATABASE_READ_ONLY"),
            ("read-only", "DATABASE_READ_ONLY"), ("disk is full", "DATABASE_DISK_FULL"),
            ("malformed", "DATABASE_CORRUPT"), ("not a database", "DATABASE_CORRUPT"),
            ("unable to open database", "DATABASE_ACCESS_DENIED"),
            ("disk i/o error", "DATABASE_IO_ERROR"),
        ):
            if fragment in message:
                code = candidate
                break
    if isinstance(error, OSError):
        code = {errno.EACCES: "DATABASE_ACCESS_DENIED", errno.EPERM: "DATABASE_ACCESS_DENIED",
                errno.EROFS: "DATABASE_READ_ONLY", errno.ENOSPC: "DATABASE_DISK_FULL",
                errno.EIO: "DATABASE_IO_ERROR"}.get(error.errno, code)
    evidence = {"exception_type": type(error).__name__}
    if isinstance(numeric, int):
        evidence["sqlite_errorcode"] = numeric
    if isinstance(error, OSError) and isinstance(error.errno, int):
        evidence["errno"] = error.errno
    return DatabaseFailure(code or "DATABASE_INITIALIZATION_FAILED", evidence)


def validate(database: Path, required_migrations: Optional[Sequence[str]] = None, check_write: bool = True,
             check_integrity: bool = True) -> None:
    try:
        if not database.exists():
            raise DatabaseFailure("DATABASE_NOT_CREATED")
        # mode=rw prevents a race with a removed file from silently creating an empty DB.
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True, timeout=1)) as conn:
            # Full page scans belong to initialization/repair, not status polling.
            if check_integrity:
                result = conn.execute("PRAGMA quick_check").fetchone()
                if not result or result[0] != "ok":
                    raise DatabaseFailure("DATABASE_CORRUPT")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if {"schema_migrations", "captures", "timelines", "creation_skills"} - tables:
                raise DatabaseFailure("DATABASE_MIGRATION_INCOMPLETE")
            if required_migrations is not None:
                applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
                missing = set(required_migrations) - applied
                if missing:
                    raise DatabaseFailure("DATABASE_MIGRATION_INCOMPLETE", {"missing_migration_count": len(missing)})
            if not check_write:
                return
            # A committed write to main is essential: TEMP tables can succeed on a read-only DB.
            with conn:
                conn.execute("CREATE TABLE IF NOT EXISTS main.initialization_write_probe (token TEXT PRIMARY KEY)")
                token = uuid.uuid4().hex
                conn.execute("INSERT INTO main.initialization_write_probe(token) VALUES (?)", (token,))
            try:
                row = conn.execute("SELECT token FROM main.initialization_write_probe WHERE token=?", (token,)).fetchone()
                if row != (token,):
                    raise DatabaseFailure("DATABASE_IO_ERROR")
            finally:
                with conn:
                    conn.execute("DELETE FROM main.initialization_write_probe WHERE token=?", (token,))
    except DatabaseFailure:
        raise
    except (sqlite3.Error, OSError) as error:
        raise classify(error) from error
