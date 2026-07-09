from __future__ import annotations

import csv
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BankError(Exception):
    pass


class Database:
    def __init__(self, path: Path):
        self.path = path

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=5000")
        try:
            yield db
        finally:
            await db.close()

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as db:
            await db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    username TEXT,
                    balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
                    held INTEGER NOT NULL DEFAULT 0 CHECK(held >= 0),
                    frozen INTEGER NOT NULL DEFAULT 0,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    is_main_admin INTEGER NOT NULL DEFAULT 0,
                    has_license INTEGER NOT NULL DEFAULT 0,
                    verified_employer INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT
                );
                CREATE TABLE IF NOT EXISTS whitelist (
                    telegram_id INTEGER,
                    username TEXT,
                    name TEXT NOT NULL,
                    UNIQUE(telegram_id),
                    UNIQUE(username)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_username
                    ON users(lower(username)) WHERE username IS NOT NULL;
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id INTEGER REFERENCES users(telegram_id),
                    receiver_id INTEGER REFERENCES users(telegram_id),
                    amount INTEGER NOT NULL CHECK(amount > 0),
                    kind TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    creator_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    reward INTEGER NOT NULL CHECK(reward > 0),
                    deadline TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    worker_id INTEGER REFERENCES users(telegram_id),
                    report_type TEXT,
                    report_value TEXT,
                    rejection_reason TEXT,
                    task_type TEXT NOT NULL DEFAULT 'single',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_assignments (
                    task_id INTEGER NOT NULL REFERENCES tasks(id),
                    worker_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    status TEXT NOT NULL DEFAULT 'working',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(task_id,worker_id)
                );
                CREATE TABLE IF NOT EXISTS election_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE REFERENCES users(telegram_id),
                    program TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft','pending','approved')),
                    adjusted_votes INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS election_photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id INTEGER NOT NULL
                        REFERENCES election_candidates(id) ON DELETE CASCADE,
                    file_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    UNIQUE(candidate_id,position)
                );
                CREATE TABLE IF NOT EXISTS election_votes (
                    voter_id INTEGER PRIMARY KEY REFERENCES users(telegram_id),
                    candidate_id INTEGER NOT NULL
                        REFERENCES election_candidates(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS balance_reset_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_by INTEGER NOT NULL REFERENCES users(telegram_id),
                    created_at TEXT NOT NULL,
                    restored_at TEXT,
                    restored_by INTEGER REFERENCES users(telegram_id)
                );
                CREATE TABLE IF NOT EXISTS balance_reset_items (
                    snapshot_id INTEGER NOT NULL
                        REFERENCES balance_reset_snapshots(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL REFERENCES users(telegram_id),
                    balance INTEGER NOT NULL,
                    held INTEGER NOT NULL,
                    PRIMARY KEY(snapshot_id,user_id)
                );
                INSERT OR IGNORE INTO settings(key,value) VALUES
                    ('transfers_stopped','0'), ('budget','0');
            """)
            columns = {
                row["name"] for row in await (
                    await db.execute("PRAGMA table_info(users)")
                ).fetchall()
            }
            if "verified_employer" not in columns:
                await db.execute(
                    "ALTER TABLE users ADD COLUMN verified_employer INTEGER NOT NULL DEFAULT 0"
                )
            task_columns = {
                row["name"] for row in await (
                    await db.execute("PRAGMA table_info(tasks)")
                ).fetchall()
            }
            if "task_type" not in task_columns:
                await db.execute(
                    "ALTER TABLE tasks ADD COLUMN task_type TEXT NOT NULL DEFAULT 'single'"
                )
            election_media_columns = {
                row["name"] for row in await (
                    await db.execute("PRAGMA table_info(election_photos)")
                ).fetchall()
            }
            if "media_type" not in election_media_columns:
                await db.execute(
                    "ALTER TABLE election_photos ADD COLUMN media_type TEXT NOT NULL DEFAULT 'photo'"
                )
            await db.commit()

    async def election_start_candidate(self, user_id: int, program: str) -> int:
        async with self.connect() as db:
            existing = await (await db.execute(
                "SELECT id,status FROM election_candidates WHERE user_id=?", (user_id,)
            )).fetchone()
            if existing and existing["status"] in {"pending", "approved"}:
                raise BankError("У вас уже есть активная анкета.")
            if existing:
                await db.execute("DELETE FROM election_candidates WHERE id=?", (existing["id"],))
            cur = await db.execute(
                "INSERT INTO election_candidates(user_id,program,created_at) VALUES(?,?,?)",
                (user_id, program, now()),
            )
            await db.commit()
            return cur.lastrowid

    async def election_add_media(
        self, candidate_id: int, file_id: str, media_type: str = "photo"
    ) -> int:
        if media_type not in {"photo", "video", "audio"}:
            raise BankError("Неподдерживаемый тип медиафайла.")
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            total = (await (await db.execute(
                "SELECT COUNT(*) n FROM election_photos WHERE candidate_id=?",
                (candidate_id,),
            )).fetchone())["n"]
            type_count = (await (await db.execute(
                "SELECT COUNT(*) n FROM election_photos "
                "WHERE candidate_id=? AND media_type=?",
                (candidate_id, media_type),
            )).fetchone())["n"]
            if total >= 15:
                raise BankError("В анкете может быть не больше 15 медиафайлов.")
            if media_type == "photo" and type_count >= 5:
                raise BankError("Можно прикрепить не больше 5 фотографий.")
            position = (await (await db.execute(
                "SELECT COALESCE(MAX(position),-1)+1 n FROM election_photos "
                "WHERE candidate_id=?", (candidate_id,),
            )).fetchone())["n"]
            await db.execute(
                "INSERT INTO election_photos(candidate_id,file_id,position,media_type) "
                "VALUES(?,?,?,?)",
                (candidate_id, file_id, position, media_type),
            )
            await db.commit()
            return total + 1

    async def election_add_photo(self, candidate_id: int, file_id: str) -> int:
        return await self.election_add_media(candidate_id, file_id, "photo")

    async def election_submit(self, candidate_id: int, user_id: int) -> Any:
        async with self.connect() as db:
            cur = await db.execute(
                "UPDATE election_candidates SET status='pending' "
                "WHERE id=? AND user_id=? AND status='draft' AND "
                "(SELECT COUNT(*) FROM election_photos "
                " WHERE candidate_id=? AND media_type='photo') BETWEEN 1 AND 5",
                (candidate_id, user_id, candidate_id),
            )
            if cur.rowcount != 1:
                raise BankError("Для отправки анкеты нужно прикрепить от 1 до 5 фотографий.")
            await db.commit()
        return await self.election_candidate(candidate_id)

    async def election_candidate(self, candidate_id: int) -> Any:
        async with self.connect() as db:
            row = await (await db.execute("""
                SELECT c.*,u.username,u.name,
                       (SELECT COUNT(*) FROM election_votes v WHERE v.candidate_id=c.id)
                       + c.adjusted_votes AS votes
                FROM election_candidates c JOIN users u ON u.telegram_id=c.user_id
                WHERE c.id=?
            """, (candidate_id,))).fetchone()
            if not row:
                return None
            media = await (await db.execute(
                "SELECT id,file_id,media_type FROM election_photos "
                "WHERE candidate_id=? ORDER BY position",
                (candidate_id,),
            )).fetchall()
            return row, media

    async def election_begin_edit(self, candidate_id: int, user_id: int) -> None:
        async with self.connect() as db:
            cur = await db.execute(
                "UPDATE election_candidates SET status='draft' WHERE id=? AND user_id=?",
                (candidate_id, user_id),
            )
            if cur.rowcount != 1:
                raise BankError("Анкета не найдена.")
            await db.commit()

    async def election_update_program(
        self, candidate_id: int, user_id: int, program: str
    ) -> Any:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute("""
                UPDATE election_candidates
                SET program=?,status='pending'
                WHERE id=? AND user_id=? AND
                      (SELECT COUNT(*) FROM election_photos
                       WHERE candidate_id=? AND media_type='photo') BETWEEN 1 AND 5
            """, (program, candidate_id, user_id, candidate_id))
            if cur.rowcount != 1:
                raise BankError(
                    "Анкета не найдена или в ней нет обязательной фотографии."
                )
            await db.commit()
        return await self.election_candidate(candidate_id)

    async def election_delete_media(
        self, candidate_id: int, media_id: int, user_id: int
    ) -> None:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            owner = await (await db.execute(
                "SELECT 1 FROM election_candidates WHERE id=? AND user_id=?",
                (candidate_id, user_id),
            )).fetchone()
            if not owner:
                raise BankError("Анкета не найдена.")
            cur = await db.execute(
                "DELETE FROM election_photos WHERE id=? AND candidate_id=?",
                (media_id, candidate_id),
            )
            if cur.rowcount != 1:
                raise BankError("Медиафайл уже удалён.")
            await db.execute(
                "UPDATE election_candidates SET status='draft' WHERE id=?",
                (candidate_id,),
            )
            await db.commit()

    async def election_candidate_by_user(self, user_id: int) -> Any:
        async with self.connect() as db:
            row = await (await db.execute(
                "SELECT id FROM election_candidates WHERE user_id=?", (user_id,)
            )).fetchone()
        return await self.election_candidate(row["id"]) if row else None

    async def election_approved(self) -> list[Any]:
        async with self.connect() as db:
            ids = await (await db.execute(
                "SELECT id FROM election_candidates WHERE status='approved' ORDER BY id"
            )).fetchall()
        return [candidate for row in ids if (candidate := await self.election_candidate(row["id"]))]

    async def election_moderate(self, candidate_id: int, approve: bool) -> Any:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute(
                "SELECT user_id FROM election_candidates WHERE id=? AND status='pending'",
                (candidate_id,),
            )).fetchone()
            if not row:
                raise BankError("Заявка уже обработана или удалена.")
            if approve:
                await db.execute(
                    "UPDATE election_candidates SET status='approved' WHERE id=?",
                    (candidate_id,),
                )
            else:
                await db.execute("DELETE FROM election_candidates WHERE id=?", (candidate_id,))
            await db.commit()
            return row["user_id"]

    async def election_vote(self, candidate_id: int, voter_id: int) -> int:
        async with self.connect() as db:
            try:
                await db.execute(
                    "INSERT INTO election_votes(voter_id,candidate_id,created_at) "
                    "SELECT ?,id,? FROM election_candidates WHERE id=? AND status='approved'",
                    (voter_id, now(), candidate_id),
                )
                changes = (await (await db.execute("SELECT changes() n")).fetchone())["n"]
                if not changes:
                    raise BankError("Кандидат больше не участвует в выборах.")
                await db.commit()
            except aiosqlite.IntegrityError:
                raise BankError("Вы уже проголосовали на этих выборах.")
        candidate = await self.election_candidate(candidate_id)
        return candidate[0]["votes"]

    async def election_vote_choice(self, voter_id: int) -> Any:
        async with self.connect() as db:
            row = await (await db.execute("""
                SELECT c.id,c.user_id,c.program,c.status,c.adjusted_votes,c.created_at,
                       u.username,u.name,
                       (SELECT COUNT(*) FROM election_votes x WHERE x.candidate_id=c.id)
                       + c.adjusted_votes AS votes
                FROM election_votes v
                JOIN election_candidates c ON c.id=v.candidate_id
                JOIN users u ON u.telegram_id=c.user_id
                WHERE v.voter_id=?
            """, (voter_id,))).fetchone()
            return row

    async def election_cancel_vote(self, voter_id: int) -> tuple[int, int]:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            vote = await (await db.execute(
                "SELECT candidate_id FROM election_votes WHERE voter_id=?",
                (voter_id,),
            )).fetchone()
            if not vote:
                raise BankError("У вас нет активного голоса.")
            candidate_id = vote["candidate_id"]
            await db.execute("DELETE FROM election_votes WHERE voter_id=?", (voter_id,))
            candidate = await (await db.execute("""
                SELECT (SELECT COUNT(*) FROM election_votes WHERE candidate_id=c.id)
                       + adjusted_votes AS votes
                FROM election_candidates c WHERE id=?
            """, (candidate_id,))).fetchone()
            await db.commit()
            return candidate_id, candidate["votes"]

    async def election_voter_ids(self, candidate_id: int) -> list[int]:
        async with self.connect() as db:
            rows = await (await db.execute(
                "SELECT voter_id FROM election_votes WHERE candidate_id=?",
                (candidate_id,),
            )).fetchall()
            return [row["voter_id"] for row in rows]

    async def election_delete(self, candidate_id: int) -> int:
        async with self.connect() as db:
            row = await (await db.execute(
                "SELECT user_id FROM election_candidates WHERE id=?", (candidate_id,)
            )).fetchone()
            if not row:
                raise BankError("Кандидат не найден.")
            await db.execute("DELETE FROM election_candidates WHERE id=?", (candidate_id,))
            await db.commit()
            return row["user_id"]

    async def election_adjust_votes(self, candidate_id: int, delta: int) -> int:
        async with self.connect() as db:
            actual = (await (await db.execute(
                "SELECT COUNT(*) n FROM election_votes WHERE candidate_id=?", (candidate_id,)
            )).fetchone())["n"]
            row = await (await db.execute(
                "SELECT adjusted_votes FROM election_candidates WHERE id=? AND status='approved'",
                (candidate_id,),
            )).fetchone()
            if not row:
                raise BankError("Одобренный кандидат не найден.")
            total = actual + row["adjusted_votes"] + delta
            if total < 0:
                raise BankError("Итоговый счётчик голосов не может быть отрицательным.")
            await db.execute(
                "UPDATE election_candidates SET adjusted_votes=adjusted_votes+? WHERE id=?",
                (delta, candidate_id),
            )
            await db.commit()
            return total

    async def main_admin_ids(self) -> list[int]:
        async with self.connect() as db:
            rows = await (await db.execute(
                "SELECT telegram_id FROM users WHERE is_main_admin=1"
            )).fetchall()
            return [row["telegram_id"] for row in rows]

    async def admin_ids(self) -> list[int]:
        async with self.connect() as db:
            rows = await (await db.execute(
                "SELECT telegram_id FROM users WHERE is_admin=1 OR is_main_admin=1"
            )).fetchall()
            return [row["telegram_id"] for row in rows]

    async def reset_all_balances(self, actor_id: int) -> tuple[int, int]:
        """Save every user balance/hold and atomically reset both values to zero."""
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                "INSERT INTO balance_reset_snapshots(created_by,created_at) VALUES(?,?)",
                (actor_id, now()),
            )
            snapshot_id = cur.lastrowid
            await db.execute("""
                INSERT INTO balance_reset_items(snapshot_id,user_id,balance,held)
                SELECT ?,telegram_id,balance,held FROM users
            """, (snapshot_id,))
            count = (await (await db.execute(
                "SELECT COUNT(*) n FROM balance_reset_items WHERE snapshot_id=?",
                (snapshot_id,),
            )).fetchone())["n"]
            await db.execute("UPDATE users SET balance=0,held=0")
            await db.commit()
            return snapshot_id, count

    async def restore_last_balance_reset(self, actor_id: int) -> tuple[int, int]:
        """Restore the newest reset that has not already been restored."""
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            snapshot = await (await db.execute("""
                SELECT id FROM balance_reset_snapshots
                WHERE restored_at IS NULL ORDER BY id DESC LIMIT 1
            """)).fetchone()
            if not snapshot:
                raise BankError("Нет обнуления, которое можно отменить.")
            snapshot_id = snapshot["id"]
            await db.execute("""
                UPDATE users
                SET balance=(
                        SELECT i.balance FROM balance_reset_items i
                        WHERE i.snapshot_id=? AND i.user_id=users.telegram_id
                    ),
                    held=(
                        SELECT i.held FROM balance_reset_items i
                        WHERE i.snapshot_id=? AND i.user_id=users.telegram_id
                    )
                WHERE EXISTS(
                    SELECT 1 FROM balance_reset_items i
                    WHERE i.snapshot_id=? AND i.user_id=users.telegram_id
                )
            """, (snapshot_id, snapshot_id, snapshot_id))
            count = (await (await db.execute(
                "SELECT COUNT(*) n FROM balance_reset_items WHERE snapshot_id=?",
                (snapshot_id,),
            )).fetchone())["n"]
            await db.execute(
                "UPDATE balance_reset_snapshots SET restored_at=?,restored_by=? WHERE id=?",
                (now(), actor_id, snapshot_id),
            )
            await db.commit()
            return snapshot_id, count

    async def import_whitelist(self, path: Path, main_admins: frozenset[str]) -> int:
        if not path.exists():
            raise FileNotFoundError(f"Не найден whitelist CSV: {path}")
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        async with self.connect() as db:
            for row in rows:
                raw_id = (row.get("User ID") or "").strip()
                username = (row.get("Username") or "").strip().lstrip("@") or None
                name = (row.get("Name") or "Гражданин").strip()
                if not raw_id and not username:
                    continue
                await db.execute("""
                    INSERT INTO whitelist(telegram_id,username,name) VALUES(?,?,?)
                    ON CONFLICT DO NOTHING
                """, (int(raw_id) if raw_id else None, username.lower() if username else None, name))
                if not raw_id:
                    continue
                is_main = int(bool(username and username.lower() in main_admins))
                await db.execute("""
                    INSERT INTO users(telegram_id,name,username,is_admin,is_main_admin)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(telegram_id) DO UPDATE SET
                    name=excluded.name,
                        username=COALESCE(excluded.username,users.username),
                        is_admin=MAX(users.is_admin,excluded.is_admin),
                        is_main_admin=MAX(users.is_main_admin,excluded.is_main_admin)
                """, (int(raw_id), name,
                      username, is_main, is_main))
            await db.commit()
        return len(rows)

    async def authorize(self, telegram_id: int, username: str | None) -> Any:
        async with self.connect() as db:
            user = await (await db.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            )).fetchone()
            if not user and username:
                user = await (await db.execute(
                    "SELECT * FROM users WHERE lower(username)=lower(?)", (username.lstrip("@"),)
                )).fetchone()
                if user:
                    await db.execute(
                        "UPDATE users SET telegram_id=? WHERE telegram_id=?",
                        (telegram_id, user["telegram_id"]),
                    )
                    await db.commit()
                    user = await (await db.execute(
                        "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
                    )).fetchone()
            if not user and username:
                allowed = await (await db.execute(
                    "SELECT * FROM whitelist WHERE telegram_id IS NULL AND lower(username)=lower(?)",
                    (username.lstrip("@"),),
                )).fetchone()
                if allowed:
                    await db.execute(
                        "INSERT INTO users(telegram_id,name,username) VALUES(?,?,?)",
                        (telegram_id, allowed["name"], username.lstrip("@")),
                    )
                    await db.execute(
                        "UPDATE whitelist SET telegram_id=? WHERE username=?",
                        (telegram_id, allowed["username"]),
                    )
                    await db.commit()
                    user = await (await db.execute(
                        "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
                    )).fetchone()
            if user:
                await db.execute(
                    "UPDATE users SET username=COALESCE(?,username), first_seen=COALESCE(first_seen,?) "
                    "WHERE telegram_id=?",
                    (username.lstrip("@") if username else None, now(), telegram_id),
                )
                await db.commit()
            return user

    async def user(self, telegram_id: int) -> Any:
        async with self.connect() as db:
            return await (await db.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            )).fetchone()

    async def user_by_username(self, username: str) -> Any:
        async with self.connect() as db:
            return await (await db.execute(
                "SELECT * FROM users WHERE lower(username)=lower(?)",
                (username.strip().lstrip("@"),),
            )).fetchone()

    async def recipients(self, viewer_id: int, sort: str = "name",
                         search: str = "", limit: int = 40,
                         include_self: bool = False) -> list[Any]:
        order = {
            "name": "lower(u.username), lower(u.name)",
            "received_count": "received_count DESC, lower(u.username)",
            "sent_count": "sent_count DESC, lower(u.username)",
            "sent_amount": "sent_amount DESC, lower(u.username)",
        }.get(sort, "lower(u.username), lower(u.name)")
        pattern = f"%{search.strip().lstrip('@').lower()}%"
        self_filter = "" if include_self else "u.telegram_id<>? AND"
        params = (
            (pattern, pattern, limit)
            if include_self
            else (viewer_id, pattern, pattern, limit)
        )
        async with self.connect() as db:
            return await (await db.execute(f"""
                SELECT u.*,
                    (SELECT COUNT(*) FROM transactions t WHERE t.receiver_id=u.telegram_id) received_count,
                    (SELECT COUNT(*) FROM transactions t WHERE t.sender_id=u.telegram_id) sent_count,
                    (SELECT COALESCE(SUM(t.amount),0) FROM transactions t WHERE t.sender_id=u.telegram_id) sent_amount
                FROM users u
                WHERE {self_filter} u.username IS NOT NULL
                  AND (lower(u.username) LIKE ? OR lower(u.name) LIKE ?)
                ORDER BY {order} LIMIT ?
            """, params)).fetchall()

    async def history(self, uid: int, limit: int = 10) -> list[Any]:
        async with self.connect() as db:
            return await (await db.execute("""
                SELECT t.*, s.username sender, r.username receiver
                FROM transactions t
                LEFT JOIN users s ON s.telegram_id=t.sender_id
                LEFT JOIN users r ON r.telegram_id=t.receiver_id
                WHERE sender_id=? OR receiver_id=?
                ORDER BY id DESC LIMIT ?
            """, (uid, uid, limit))).fetchall()

    async def transfer(self, sender: int, receiver: int, amount: int,
                       kind: str = "transfer", note: str = "") -> None:
        if amount <= 0 or sender == receiver:
            raise BankError("Некорректная сумма или получатель.")
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            stopped = await (await db.execute(
                "SELECT value FROM settings WHERE key='transfers_stopped'"
            )).fetchone()
            src = await (await db.execute(
                "SELECT * FROM users WHERE telegram_id=?", (sender,)
            )).fetchone()
            dst = await (await db.execute(
                "SELECT * FROM users WHERE telegram_id=?", (receiver,)
            )).fetchone()
            if not src or not dst:
                raise BankError("Пользователь не найден.")
            if stopped["value"] == "1":
                raise BankError("Переводы временно остановлены государством.")
            if src["frozen"] or dst["frozen"]:
                raise BankError("Один из счетов заморожен.")
            if src["balance"] - src["held"] < amount:
                raise BankError("Недостаточно доступных средств.")
            changed = await db.execute(
                "UPDATE users SET balance=balance-? WHERE telegram_id=? "
                "AND balance-held>=?", (amount, sender, amount)
            )
            if changed.rowcount != 1:
                raise BankError("Недостаточно средств.")
            await db.execute("UPDATE users SET balance=balance+? WHERE telegram_id=?",
                             (amount, receiver))
            await db.execute(
                "INSERT INTO transactions(sender_id,receiver_id,amount,kind,note,created_at) "
                "VALUES(?,?,?,?,?,?)", (sender, receiver, amount, kind, note, now())
            )
            await db.commit()

    async def admin_money(self, uid: int, amount: int, credit: bool, note: str) -> None:
        if amount <= 0:
            raise BankError("Сумма должна быть положительной.")
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            user = await (await db.execute(
                "SELECT * FROM users WHERE telegram_id=?", (uid,)
            )).fetchone()
            if not user:
                raise BankError("Пользователь не найден.")
            if not credit and user["balance"] - user["held"] < amount:
                raise BankError("Недостаточно свободных средств для списания.")
            delta = amount if credit else -amount
            await db.execute("UPDATE users SET balance=balance+? WHERE telegram_id=?",
                             (delta, uid))
            if not credit:
                await db.execute("UPDATE settings SET value=CAST(value AS INTEGER)+? "
                                 "WHERE key='budget'", (amount,))
            await db.execute(
                "INSERT INTO transactions(sender_id,receiver_id,amount,kind,note,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (None if credit else uid, uid if credit else None, amount,
                 "emission" if credit else "tax", note, now()),
            )
            await db.commit()

    async def toggle_flag(self, uid: int, field: str) -> int:
        if field not in {"frozen", "is_admin", "is_main_admin", "verified_employer"}:
            raise ValueError(field)
        async with self.connect() as db:
            if field == "is_main_admin":
                current = await (await db.execute(
                    "SELECT is_main_admin FROM users WHERE telegram_id=?", (uid,)
                )).fetchone()
                if not current:
                    raise BankError("Пользователь не найден.")
                value = 1 - current["is_main_admin"]
                await db.execute(
                    "UPDATE users SET is_main_admin=?,is_admin=1 WHERE telegram_id=?",
                    (value, uid),
                )
            else:
                await db.execute(
                    f"UPDATE users SET {field}=1-{field} WHERE telegram_id=?", (uid,)
                )
            await db.commit()
            row = await (await db.execute(
                f"SELECT {field} FROM users WHERE telegram_id=?", (uid,)
            )).fetchone()
            return int(row[field])

    async def all_user_ids(self) -> list[int]:
        async with self.connect() as db:
            rows = await (await db.execute(
                "SELECT telegram_id FROM users WHERE first_seen IS NOT NULL"
            )).fetchall()
            return [row["telegram_id"] for row in rows]

    async def remove_admin(self, uid: int) -> bool:
        """Remove both ordinary and main admin rights immediately."""
        async with self.connect() as db:
            cur = await db.execute(
                "UPDATE users SET is_admin=0,is_main_admin=0 "
                "WHERE telegram_id=? AND (is_admin=1 OR is_main_admin=1)",
                (uid,),
            )
            await db.commit()
            return cur.rowcount == 1

    async def toggle_stop(self) -> int:
        async with self.connect() as db:
            await db.execute(
                "UPDATE settings SET value=CASE value WHEN '1' THEN '0' ELSE '1' END "
                "WHERE key='transfers_stopped'"
            )
            await db.commit()
            row = await (await db.execute(
                "SELECT value FROM settings WHERE key='transfers_stopped'"
            )).fetchone()
            return int(row["value"])

    async def audit(self) -> tuple[int, int, int]:
        async with self.connect() as db:
            row = await (await db.execute(
                "SELECT COALESCE(SUM(balance),0) total,COUNT(*) count FROM users"
            )).fetchone()
            budget = await (await db.execute(
                "SELECT value FROM settings WHERE key='budget'"
            )).fetchone()
            return row["total"], int(budget["value"]), row["count"]

    async def verified_employers(self) -> list[Any]:
        async with self.connect() as db:
            return await (await db.execute(
                "SELECT * FROM users WHERE verified_employer=1 ORDER BY name"
            )).fetchall()

    async def create_task(self, creator: int, title: str, description: str,
                          reward: int, deadline: str | None,
                          task_type: str = "single") -> int:
        if task_type not in {"single", "multi"}:
            raise BankError("Неизвестный тип задачи.")
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            owner = await (await db.execute(
                "SELECT * FROM users WHERE telegram_id=?", (creator,)
            )).fetchone()
            if not owner:
                raise BankError("Пользователь не найден.")
            if not (owner["is_admin"] or owner["is_main_admin"]
                    or owner["verified_employer"]):
                raise BankError("Нет доступа к публикации задач.")
            if owner["frozen"]:
                raise BankError("Счёт заморожен.")
            active = await (await db.execute(
                "SELECT COUNT(*) n FROM tasks WHERE creator_id=? "
                "AND status NOT IN ('done','cancelled')",
                (creator,),
            )).fetchone()
            task_limit = 50 if owner["verified_employer"] else 10
            if active["n"] >= task_limit:
                raise BankError(
                    f"Можно иметь не более {task_limit} активных задач."
                )
            if owner["balance"] - owner["held"] < reward:
                raise BankError("Недостаточно денег для холда награды.")
            await db.execute("UPDATE users SET held=held+? WHERE telegram_id=?",
                             (reward, creator))
            cur = await db.execute(
                "INSERT INTO tasks(creator_id,title,description,reward,deadline,task_type,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (creator, title, description, reward, deadline, task_type, now()),
            )
            await db.commit()
            return cur.lastrowid

    async def tasks(self, uid: int, mode: str) -> list[Any]:
        where, args = {
            "open": (
                "t.status='open' AND t.creator_id<>? AND "
                "(t.task_type='single' OR NOT EXISTS("
                "SELECT 1 FROM task_assignments a WHERE a.task_id=t.id AND a.worker_id=?))",
                (uid, uid),
            ),
            "worker": (
                "((t.task_type='single' AND t.worker_id=?) OR "
                "(t.task_type='multi' AND EXISTS(SELECT 1 FROM task_assignments a "
                "WHERE a.task_id=t.id AND a.worker_id=?))) "
                "AND t.status IN ('open','working','review')",
                (uid, uid),
            ),
            "owner": ("t.creator_id=?", (uid,)),
        }[mode]
        async with self.connect() as db:
            return await (await db.execute(f"""
                SELECT t.*,u.username creator,u.verified_employer,w.username worker FROM tasks t
                JOIN users u ON u.telegram_id=t.creator_id
                LEFT JOIN users w ON w.telegram_id=t.worker_id
                WHERE {where}
                ORDER BY u.verified_employer DESC,t.id DESC LIMIT 20
            """, args)).fetchall()

    async def task(self, task_id: int) -> Any:
        async with self.connect() as db:
            return await (await db.execute("""
                SELECT t.*,u.username creator,u.verified_employer,w.username worker FROM tasks t
                JOIN users u ON u.telegram_id=t.creator_id
                LEFT JOIN users w ON w.telegram_id=t.worker_id WHERE t.id=?
            """, (task_id,))).fetchone()

    async def take_task(self, task_id: int, worker: int, max_active: int) -> None:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            user = await (await db.execute(
                "SELECT frozen FROM users WHERE telegram_id=?", (worker,)
            )).fetchone()
            count = await (await db.execute(
                """SELECT COUNT(*) n FROM tasks t
                   WHERE t.status IN ('open','working','review') AND
                   ((t.task_type='single' AND t.worker_id=?) OR
                    (t.task_type='multi' AND EXISTS(
                       SELECT 1 FROM task_assignments a
                       WHERE a.task_id=t.id AND a.worker_id=?)))""",
                (worker, worker),
            )).fetchone()
            if not user or user["frozen"]:
                raise BankError("Ваш счёт заморожен.")
            if count["n"] >= max_active:
                raise BankError(f"Можно выполнять не более {max_active} задач.")
            task = await (await db.execute(
                "SELECT * FROM tasks WHERE id=? AND status='open' AND creator_id<>?",
                (task_id, worker),
            )).fetchone()
            if not task:
                raise BankError("Задача уже занята или недоступна.")
            if task["task_type"] == "single":
                cur = await db.execute(
                    "UPDATE tasks SET status='working',worker_id=? "
                    "WHERE id=? AND status='open'", (worker, task_id)
                )
                if cur.rowcount != 1:
                    raise BankError("Задача уже занята.")
            else:
                try:
                    await db.execute(
                        "INSERT INTO task_assignments(task_id,worker_id,created_at) "
                        "VALUES(?,?,?)", (task_id, worker, now())
                    )
                except aiosqlite.IntegrityError:
                    raise BankError("Вы уже взяли эту задачу.")
            await db.commit()

    async def submit_report(self, task_id: int, worker: int,
                            report_type: str, value: str) -> int:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            task = await (await db.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            )).fetchone()
            if not task:
                raise BankError("Задача не найдена.")
            if task["task_type"] == "single":
                cur = await db.execute(
                    "UPDATE tasks SET status='review',report_type=?,report_value=? "
                    "WHERE id=? AND worker_id=? AND status='working'",
                    (report_type, value, task_id, worker),
                )
            else:
                assignment = await (await db.execute(
                    "SELECT 1 FROM task_assignments WHERE task_id=? AND worker_id=?",
                    (task_id, worker),
                )).fetchone()
                if not assignment:
                    raise BankError("Вы не брали эту задачу.")
                cur = await db.execute(
                    "UPDATE tasks SET status='review',worker_id=?,report_type=?,report_value=? "
                    "WHERE id=? AND status='open'",
                    (worker, report_type, value, task_id),
                )
            if cur.rowcount != 1:
                raise BankError("Задача уже заморожена другим отчётом.")
            await db.commit()
            row = await (await db.execute(
                "SELECT creator_id FROM tasks WHERE id=?", (task_id,)
            )).fetchone()
            return row["creator_id"]

    async def accept_task(self, task_id: int, owner: int) -> int:
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            stopped = await (await db.execute(
                "SELECT value FROM settings WHERE key='transfers_stopped'"
            )).fetchone()
            if stopped["value"] == "1":
                raise BankError("Выплаты остановлены государством.")
            task = await (await db.execute(
                "SELECT * FROM tasks WHERE id=? AND creator_id=? AND status='review'",
                (task_id, owner),
            )).fetchone()
            if not task:
                raise BankError("Отчёт уже обработан.")
            await db.execute("UPDATE users SET balance=balance-?,held=held-? WHERE telegram_id=?",
                             (task["reward"], task["reward"], owner))
            await db.execute("UPDATE users SET balance=balance+? WHERE telegram_id=?",
                             (task["reward"], task["worker_id"]))
            await db.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
            await db.execute(
                "INSERT INTO transactions(sender_id,receiver_id,amount,kind,note,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (owner, task["worker_id"], task["reward"], "task",
                 f"Задача #{task_id}", now()),
            )
            await db.commit()
            return task["worker_id"]

    async def reject_task(self, task_id: int, owner: int, reason: str) -> int:
        async with self.connect() as db:
            row = await (await db.execute(
                "SELECT worker_id FROM tasks WHERE id=? AND creator_id=? AND status='review'",
                (task_id, owner),
            )).fetchone()
            if not row:
                raise BankError("Отчёт уже обработан.")
            task = await (await db.execute(
                "SELECT task_type FROM tasks WHERE id=?", (task_id,)
            )).fetchone()
            next_status = "open" if task["task_type"] == "multi" else "working"
            await db.execute(
                "UPDATE tasks SET status=?,worker_id=CASE WHEN task_type='multi' "
                "THEN NULL ELSE worker_id END,rejection_reason=? WHERE id=?",
                (next_status, reason, task_id),
            )
            await db.commit()
            return row["worker_id"]

    async def cancel_task(self, task_id: int, actor_id: int) -> int:
        """Cancel task and release its reward. Creator has 1 minute; admins always."""
        async with self.connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            task = await (await db.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            )).fetchone()
            actor = await (await db.execute(
                "SELECT is_admin,is_main_admin FROM users WHERE telegram_id=?",
                (actor_id,),
            )).fetchone()
            if not task or task["status"] in {"done", "cancelled"}:
                raise BankError("Задачу уже нельзя отменить.")
            created = datetime.fromisoformat(task["created_at"])
            age = (datetime.now(timezone.utc) - created).total_seconds()
            is_admin = bool(actor and (actor["is_admin"] or actor["is_main_admin"]))
            if actor_id != task["creator_id"] and not is_admin:
                raise BankError("Можно отменять только свои задачи.")
            if actor_id == task["creator_id"] and age > 60 and not is_admin:
                raise BankError("Отменить задачу можно только в течение 1 минуты.")
            await db.execute(
                "UPDATE tasks SET status='cancelled' WHERE id=? AND status NOT IN ('done','cancelled')",
                (task_id,),
            )
            await db.execute(
                "UPDATE users SET held=held-? WHERE telegram_id=? AND held>=?",
                (task["reward"], task["creator_id"], task["reward"]),
            )
            await db.commit()
            return task["creator_id"]

    async def export_rows(self) -> list[Any]:
        async with self.connect() as db:
            return await (await db.execute(
                "SELECT name,telegram_id,username,balance,held,frozen,is_admin,verified_employer "
                "FROM users ORDER BY balance DESC,name"
            )).fetchall()

    async def export_transactions(self) -> list[Any]:
        async with self.connect() as db:
            return await (await db.execute("""
                SELECT t.id,t.created_at,t.kind,t.amount,t.note,
                       t.sender_id,s.username sender_username,
                       t.receiver_id,r.username receiver_username
                FROM transactions t
                LEFT JOIN users s ON s.telegram_id=t.sender_id
                LEFT JOIN users r ON r.telegram_id=t.receiver_id
                ORDER BY t.id
            """)).fetchall()
