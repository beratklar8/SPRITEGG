import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

import aiosqlite

log = logging.getLogger(__name__)

ACTIVE = "ACTIVE"
PROCESSING = "PROCESSING"
PROCESSING_RESULT = "PROCESSING_RESULT"
COMPLETED = "COMPLETED"
FAILED = "FAILED"

DB_VERSION = 4


def now() -> float:
    return time.time()


class Database:
    def __init__(self, path: str = "giveaways.db"):
        self.path = path
        self.conn: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()

    async def connect(self):
        if self.conn is not None:
            return

        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row

        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=FULL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.execute("PRAGMA busy_timeout=10000")
        await self.conn.commit()

        await self.migrate()

    async def close(self):
        async with self.lock:
            if self.conn is not None:
                await self.conn.close()
                self.conn = None

    def _db(self):
        if self.conn is None:
            raise RuntimeError("Database is not connected")

        return self.conn

    @asynccontextmanager
    async def transaction(self):
        db = self._db()

        async with self.lock:
            await db.execute("BEGIN IMMEDIATE")

            try:
                yield db
            except BaseException:
                await db.rollback()
                raise
            else:
                await db.commit()

    # ============================================================
    # MIGRATIONS
    # ============================================================

    async def migrate(self):
        db = self._db()

        async with self.lock:
            row = await db.execute_fetchone(
                "PRAGMA user_version"
            )

            version = int(row[0])

            if version < 1:
                await db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS giveaway_system (
                        message_id INTEGER PRIMARY KEY,
                        guild_id INTEGER NOT NULL,
                        channel_id INTEGER NOT NULL,

                        prize TEXT NOT NULL,

                        status TEXT NOT NULL,

                        winner_count INTEGER NOT NULL,
                        max_participants INTEGER NOT NULL,
                        participant_count INTEGER NOT NULL DEFAULT 0,

                        expires_at REAL NOT NULL,

                        processing_token TEXT,
                        processing_started_at REAL,

                        result_send_owner_token TEXT,
                        result_send_started_at REAL,

                        result_message_id INTEGER,
                        result_winners TEXT,
                        final_participant_count INTEGER,

                        retry_count INTEGER NOT NULL DEFAULT 0,
                        next_retry_at REAL,

                        error_code TEXT,
                        error_message TEXT,

                        creation_token TEXT UNIQUE,

                        created_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS giveaway_participants (
                        message_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        joined_at REAL NOT NULL,

                        PRIMARY KEY(message_id, user_id),

                        FOREIGN KEY(message_id)
                            REFERENCES giveaway_system(message_id)
                            ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS giveaway_creation_intents (
                        creation_token TEXT PRIMARY KEY,

                        state TEXT NOT NULL,

                        guild_id INTEGER NOT NULL,
                        channel_id INTEGER NOT NULL,
                        message_id INTEGER,

                        payload TEXT NOT NULL,

                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,

                        error_message TEXT
                    );

                    PRAGMA user_version = 1;
                    """
                )

                version = 1

            if version < 2:
                await db.executescript(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_giveaway_status_expiry
                    ON giveaway_system(status, expires_at);

                    CREATE INDEX IF NOT EXISTS
                    idx_giveaway_retry
                    ON giveaway_system(status, next_retry_at);

                    CREATE INDEX IF NOT EXISTS
                    idx_giveaway_processing_lease
                    ON giveaway_system(
                        status,
                        processing_started_at
                    );

                    CREATE INDEX IF NOT EXISTS
                    idx_giveaway_result_lease
                    ON giveaway_system(
                        status,
                        result_send_started_at
                    );

                    CREATE INDEX IF NOT EXISTS
                    idx_participants_message
                    ON giveaway_participants(message_id);

                    CREATE INDEX IF NOT EXISTS
                    idx_creation_intents_state
                    ON giveaway_creation_intents(
                        state,
                        updated_at
                    );

                    PRAGMA user_version = 2;
                    """
                )

                version = 2

            if version < 3:
                columns = await db.execute_fetchall(
                    "PRAGMA table_info(giveaway_system)"
                )

                names = {
                    row["name"]
                    for row in columns
                }

                if "error_message" not in names:
                    await db.execute(
                        """
                        ALTER TABLE giveaway_system
                        ADD COLUMN error_message TEXT
                        """
                    )

                await db.execute(
                    "PRAGMA user_version = 3"
                )

                version = 3

            if version < 4:
                columns = await db.execute_fetchall(
                    "PRAGMA table_info(giveaway_system)"
                )

                names = {
                    row["name"]
                    for row in columns
                }

                if "prize" not in names:
                    await db.execute(
                        """
                        ALTER TABLE giveaway_system
                        ADD COLUMN prize TEXT NOT NULL DEFAULT ''
                        """
                    )

                await db.execute(
                    "PRAGMA user_version = 4"
                )

            await db.commit()

    async def integrity_check(self):
        db = self._db()

        result = await db.execute_fetchone(
            "PRAGMA integrity_check"
        )

        if result[0] != "ok":
            raise RuntimeError(
                f"SQLite integrity check failed: {result[0]}"
            )

        foreign = await db.execute_fetchall(
            "PRAGMA foreign_key_check"
        )

        if foreign:
            raise RuntimeError(
                "SQLite foreign key check failed"
            )

    # ============================================================
    # CREATION INTENTS
    # ============================================================

    async def create_intent(
        self,
        creation_token: str,
        guild_id: int,
        channel_id: int,
        payload: dict,
    ):
        async with self.transaction() as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO
                giveaway_creation_intents
                (
                    creation_token,
                    state,
                    guild_id,
                    channel_id,
                    payload,
                    created_at,
                    updated_at
                )
                VALUES (
                    ?,
                    'PENDING',
                    ?,
                    ?,
                    ?,
                    ?,
                    ?
                )
                """,
                (
                    creation_token,
                    guild_id,
                    channel_id,
                    json.dumps(
                        payload,
                        separators=(",", ":"),
                    ),
                    now(),
                    now(),
                ),
            )

    async def set_intent_message(
        self,
        creation_token: str,
        message_id: int,
    ) -> bool:
        async with self.transaction() as db:
            cur = await db.execute(
                """
                UPDATE giveaway_creation_intents

                SET
                    state='MESSAGE_CREATED',
                    message_id=?,
                    updated_at=?

                WHERE
                    creation_token=?
                    AND state='PENDING'
                """,
                (
                    message_id,
                    now(),
                    creation_token,
                ),
            )

            return cur.rowcount == 1

    async def set_intent_db_created(
        self,
        creation_token: str,
    ) -> bool:
        async with self.transaction() as db:
            cur = await db.execute(
                """
                UPDATE giveaway_creation_intents

                SET
                    state='DB_CREATED',
                    updated_at=?

                WHERE
                    creation_token=?
                    AND state='MESSAGE_CREATED'
                """,
                (
                    now(),
                    creation_token,
                ),
            )

            return cur.rowcount == 1

    async def set_intent_activation_pending(
        self,
        creation_token: str,
    ) -> bool:
        async with self.transaction() as db:
            cur = await db.execute(
                """
                UPDATE giveaway_creation_intents

                SET
                    state='ACTIVATION_PENDING',
                    updated_at=?

                WHERE
                    creation_token=?
                    AND state IN (
                        'MESSAGE_CREATED',
                        'DB_CREATED'
                    )
                """,
                (
                    now(),
                    creation_token,
                ),
            )

            return cur.rowcount == 1

    async def complete_intent(
        self,
        creation_token: str,
    ) -> bool:
        async with self.transaction() as db:
            cur = await db.execute(
                """
                UPDATE giveaway_creation_intents

                SET
                    state='COMPLETED',
                    updated_at=?

                WHERE
                    creation_token=?
                    AND state IN (
                        'DB_CREATED',
                        'ACTIVATION_PENDING'
                    )
                """,
                (
                    now(),
                    creation_token,
                ),
            )

            return cur.rowcount == 1

    async def fail_intent(
        self,
        creation_token: str,
        error: str,
    ):
        async with self.transaction() as db:
            await db.execute(
                """
                UPDATE giveaway_creation_intents

                SET
                    state='FAILED',
                    updated_at=?,
                    error_message=?

                WHERE creation_token=?
                """,
                (
                    now(),
                    str(error)[:1000],
                    creation_token,
                ),
            )

    async def get_recovery_intents(self):
        db = self._db()

        return await db.execute_fetchall(
            """
            SELECT *
            FROM giveaway_creation_intents
            WHERE state IN (
                'PENDING',
                'MESSAGE_CREATED',
                'DB_CREATED',
                'ACTIVATION_PENDING'
            )
            ORDER BY created_at
            """
        )

    # ============================================================
    # GIVEAWAYS
    # ============================================================

    async def create_giveaway(
        self,
        *,
        message_id: int,
        guild_id: int,
        channel_id: int,
        prize: str,
        winner_count: int,
        max_participants: int,
        expires_at: float,
        creation_token: str,
    ) -> bool:
        async with self.transaction() as db:
            cur = await db.execute(
                """
                INSERT OR IGNORE INTO giveaway_system
                (
                    message_id,
                    guild_id,
                    channel_id,
                    prize,
                    status,
                    winner_count,
                    max_participants,
                    participant_count,
                    expires_at,
                    creation_token,
                    created_at
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    ?,
                    'ACTIVE',
                    ?,
                    ?,
                    0,
                    ?,
                    ?,
                    ?
                )
                """,
                (
                    message_id,
                    guild_id,
                    channel_id,
                    prize,
                    winner_count,
                    max_participants,
                    expires_at,
                    creation_token,
                    now(),
                ),
            )

            if cur.rowcount != 1:
                return False

            await db.execute(
                """
                UPDATE giveaway_creation_intents

                SET
                    state='DB_CREATED',
                    updated_at=?

                WHERE
                    creation_token=?
                    AND state='MESSAGE_CREATED'
                """,
                (
                    now(),
                    creation_token,
                ),
            )

            return True

    async def get_giveaway(
        self,
        message_id: int,
    ):
        db = self._db()

        return await db.execute_fetchone(
            """
            SELECT *
            FROM giveaway_system
            WHERE message_id=?
            """,
            (message_id,),
        )

    async def get_expired_giveaways(
        self,
        limit: int = 25,
    ):
        db = self._db()

        return await db.execute_fetchall(
            """
            SELECT message_id

            FROM giveaway_system

            WHERE
                status='ACTIVE'
                AND expires_at <= ?

            ORDER BY expires_at ASC

            LIMIT ?
            """,
            (
                now(),
                limit,
            ),
        )

    async def get_result_retries(
        self,
        limit: int = 25,
    ):
        db = self._db()

        return await db.execute_fetchall(
            """
            SELECT message_id

            FROM giveaway_system

            WHERE
                status='PROCESSING_RESULT'
                AND (
                    next_retry_at IS NULL
                    OR next_retry_at <= ?
                )

            ORDER BY
                COALESCE(next_retry_at, 0) ASC

            LIMIT ?
            """,
            (
                now(),
                limit,
            ),
        )

    # ============================================================
    # PROCESSING
    # ============================================================

    async def claim_processing(
        self,
        message_id: int,
        processing_token: str,
    ) -> bool:
        async with self.transaction() as db:
            cur = await db.execute(
                """
                UPDATE giveaway_system

                SET
                    status='PROCESSING',
                    processing_token=?,
                    processing_started_at=?,
                    error_code=NULL,
                    error_message=NULL

                WHERE
                    message_id=?
                    AND status='ACTIVE'
                    AND expires_at <= ?
                """,
                (
                    processing_token,
                    now(),
                    message_id,
                    now(),
                ),
            )

            return cur.rowcount == 1

    async def save_processing_result(
        self,
        message_id: int,
        processing_token: str,
        winners: list[int],
        participant_count: int,
    ) -> bool:
        async with self.transaction() as db:
            cur = await db.execute(
                """
                UPDATE giveaway_system

                SET
                    status='PROCESSING_RESULT',
                    result_winners=?,
                    final_participant_count=?,
                    participant_count=?,
                    processing_token=NULL,
                    processing_started_at=NULL,
                    retry_count=0,
                    next_retry_at=NULL

                WHERE
                    message_id=?
                    AND status='PROCESSING'
                    AND processing_token=?
                """,
                (
                    json.dumps(
                        winners,
                        separators=(",", ":"),
                    ),
                    participant_count,
                    participant_count,
                    message_id,
                    processing_token,
                ),
            )

            return cur.rowcount == 1

    async def fail_processing(
        self,
        message_id: int,
        processing_token: str,
        error_code: str,
        error_message: str,
    ) -> bool:
        async with self.transaction() as db:
            cur = await db.execute(
                """
                UPDATE giveaway_system

                SET
                    status='FAILED',
                    processing_token=NULL,
                    processing_started_at=NULL,
                    error_code=?,
                    error_message=?,
                    next_retry_at=NULL

                WHERE
                    message_id=?
                    AND status='PROCESSING'
                    AND processing_token=?
                """,
                (
                    error_code[:100],
                    str(error_message)[:1000],
                    message_id,
                    processing_token,
                ),
            )

            return cur.rowcount == 1

    # ============================================================
    # RESULT LEASE
    # ============================================================

    async def claim_result(
        self,
        message_id: int,
        owner_token: str,
        lease_seconds: int = 120,
    ) -> bool:
        cutoff = now() - lease_seconds

        async with self.transaction() as db:
            cur = await db.execute(
                """
                UPDATE giveaway_system

                SET
                    result_send_owner_token=?,
                    result_send_started_at=?

                WHERE
                    message_id=?
                    AND status='PROCESSING_RESULT'
                    AND (
                        result_send_owner_token IS NULL
                        OR result_send_started_at < ?
                    )
                """,
                (
                    owner_token,
                    now(),
                    message_id,
                    cutoff,
                ),
            )

            return cur.rowcount == 1

    async def complete_result(
        self,
        message_id: int,
        owner_token: str,
        result_message_id: int,
    ) -> bool:
        async with self.transaction() as db:
            cur = await db.execute(
                """
                UPDATE giveaway_system

                SET
                    status='COMPLETED',
                    result_message_id=?,
                    result_send_owner_token=NULL,
                    result_send_started_at=NULL,
                    next_retry_at=NULL,
                    error_code=NULL,
                    error_message=NULL

                WHERE
                    message_id=?
                    AND status='PROCESSING_RESULT'
                    AND result_send_owner_token=?
                    AND result_winners IS NOT NULL
                    AND final_participant_count IS NOT NULL
                """,
                (
                    result_message_id,
                    message_id,
                    owner_token,
                ),
            )

            return cur.rowcount == 1

    async def result_failed(
        self,
        message_id: int,
        owner_token: str,
        retry_count: int,
        next_retry_at: float | None,
        error_code: str,
        error_message: str,
        permanent: bool,
    ) -> bool:
        async with self.transaction() as db:
            cur = await db.execute(
                """
                UPDATE giveaway_system

                SET
                    status=?,
                    retry_count=?,
                    next_retry_at=?,
                    error_code=?,
                    error_message=?,
                    result_send_owner_token=NULL,
                    result_send_started_at=NULL

                WHERE
                    message_id=?
                    AND status='PROCESSING_RESULT'
                    AND result_send_owner_token=?
                """,
                (
                    "FAILED"
                    if permanent
                    else "PROCESSING_RESULT",
                    retry_count,
                    None
                    if permanent
                    else next_retry_at,
                    error_code[:100],
                    str(error_message)[:1000],
                    message_id,
                    owner_token,
                ),
            )

            return cur.rowcount == 1

    # ============================================================
    # PARTICIPANTS
    # ============================================================

    async def add_participant(
        self,
        message_id: int,
        user_id: int,
    ) -> tuple[bool, str]:
        async with self.transaction() as db:
            giveaway = await db.execute_fetchone(
                """
                SELECT
                    status,
                    max_participants

                FROM giveaway_system

                WHERE message_id=?
                """,
                (message_id,),
            )

            if giveaway is None:
                return False, "NOT_FOUND"

            if giveaway["status"] != ACTIVE:
                return False, "CLOSED"

            existing = await db.execute_fetchone(
                """
                SELECT 1

                FROM giveaway_participants

                WHERE
                    message_id=?
                    AND user_id=?
                """,
                (
                    message_id,
                    user_id,
                ),
            )

            if existing:
                return False, "ALREADY_JOINED"

            count = await db.execute_fetchone(
                """
                SELECT COUNT(*) AS count

                FROM giveaway_participants

                WHERE message_id=?
                """,
                (message_id,),
            )

            if count["count"] >= giveaway[
                "max_participants"
            ]:
                return False, "FULL"

            await db.execute(
                """
                INSERT INTO giveaway_participants
                (
                    message_id,
                    user_id,
                    joined_at
                )
                VALUES (?, ?, ?)
                """,
                (
                    message_id,
                    user_id,
                    now(),
                ),
            )

            await db.execute(
                """
                UPDATE giveaway_system

                SET participant_count=(
                    SELECT COUNT(*)
                    FROM giveaway_participants
                    WHERE message_id=?
                )

                WHERE message_id=?
                """,
                (
                    message_id,
                    message_id,
                ),
            )

            return True, "JOINED"

    async def get_participants(
        self,
        message_id: int,
    ) -> list[int]:
        db = self._db()

        rows = await db.execute_fetchall(
            """
            SELECT user_id

            FROM giveaway_participants

            WHERE message_id=?

            ORDER BY joined_at ASC
            """,
            (message_id,),
        )

        return [
            int(row["user_id"])
            for row in rows
        ]

    async def reconcile_participant_count(
        self,
        message_id: int,
    ):
        async with self.transaction() as db:
            await db.execute(
                """
                UPDATE giveaway_system

                SET participant_count=(
                    SELECT COUNT(*)
                    FROM giveaway_participants
                    WHERE message_id=?
                )

                WHERE message_id=?
                """,
                (
                    message_id,
                    message_id,
                ),
            )

    # ============================================================
    # RECOVERY
    # ============================================================

    async def stale_processing(
        self,
        cutoff: float,
    ):
        db = self._db()

        return await db.execute_fetchall(
            """
            SELECT
                message_id,
                processing_token,
                result_winners

            FROM giveaway_system

            WHERE
                status='PROCESSING'
                AND processing_started_at < ?
            """,
            (cutoff,),
        )

    async def recover_processing(
        self,
        message_id: int,
        processing_token: str,
        has_result: bool,
    ) -> bool:
        target = (
            "PROCESSING_RESULT"
            if has_result
            else "ACTIVE"
        )

        async with self.transaction() as db:
            cur = await db.execute(
                """
                UPDATE giveaway_system

                SET
                    status=?,
                    processing_token=NULL,
                    processing_started_at=NULL,
                    next_retry_at=?

                WHERE
                    message_id=?
                    AND status='PROCESSING'
                    AND processing_token=?
                """,
                (
                    target,
                    now()
                    if target == "PROCESSING_RESULT"
                    else None,
                    message_id,
                    processing_token,
                ),
            )

            return cur.rowcount == 1

    async def stale_result_leases(
        self,
        cutoff: float,
    ):
        db = self._db()

        return await db.execute_fetchall(
            """
            SELECT
                message_id,
                result_send_owner_token

            FROM giveaway_system

            WHERE
                status='PROCESSING_RESULT'
                AND result_send_owner_token IS NOT NULL
                AND result_send_started_at < ?
            """,
            (cutoff,),
        )

    async def recover_result_lease(
        self,
        message_id: int,
        owner_token: str,
    ) -> bool:
        async with self.transaction() as db:
            cur = await db.execute(
                """
                UPDATE giveaway_system

                SET
                    result_send_owner_token=NULL,
                    result_send_started_at=NULL,
                    next_retry_at=?

                WHERE
                    message_id=?
                    AND status='PROCESSING_RESULT'
                    AND result_send_owner_token=?
                """,
                (
                    now(),
                    message_id,
                    owner_token,
                ),
            )

            return cur.rowcount == 1
