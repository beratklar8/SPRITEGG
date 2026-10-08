import asyncio
import logging
import os
from contextlib import asynccontextmanager

import aiosqlite


logger = logging.getLogger("bot.database")


class DatabaseController:
    def __init__(
        self,
        db_path: str = "bot_database.db",
    ):
        self.db_path = db_path
        self.connection: aiosqlite.Connection | None = None

        self.operation_lock = asyncio.Lock()
        self.initialization_lock = asyncio.Lock()

    async def _ensure_column(
        self,
        connection: aiosqlite.Connection,
        table: str,
        column: str,
        definition: str,
    ):
        async with connection.execute(
            f"PRAGMA table_info({table})"
        ) as cursor:
            rows = await cursor.fetchall()

        existing_columns = {
            row[1]
            for row in rows
        }

        if column not in existing_columns:
            await connection.execute(
                f"ALTER TABLE {table} "
                f"ADD COLUMN {column} {definition}"
            )

            logger.info(
                "Added missing column %s.%s",
                table,
                column,
            )

    async def initialize_database(self):
        if self.connection is not None:
            return

        async with self.initialization_lock:
            if self.connection is not None:
                return

            connection = None

            try:
                db_dir = os.path.dirname(
                    os.path.abspath(
                        self.db_path
                    )
                )

                os.makedirs(
                    db_dir,
                    exist_ok=True,
                )

                connection = await aiosqlite.connect(
                    self.db_path
                )

                await connection.execute(
                    "PRAGMA journal_mode=WAL;"
                )

                await connection.execute(
                    "PRAGMA synchronous=FULL;"
                )

                await connection.execute(
                    "PRAGMA foreign_keys=ON;"
                )

                await connection.execute(
                    "PRAGMA busy_timeout=5000;"
                )

                # -------------------------
                # GIVEAWAYS
                # -------------------------

                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS giveaway_system (
                        message_id INTEGER PRIMARY KEY,
                        channel_id INTEGER,
                        guild_id INTEGER,
                        prize TEXT,
                        ends_at REAL,
                        winners INTEGER,
                        host_id INTEGER,
                        status TEXT DEFAULT 'ACTIVE',
                        processing_started_at REAL DEFAULT 0,
                        result_message_id INTEGER DEFAULT 0,
                        req_daily INTEGER DEFAULT 0,
                        req_weekly INTEGER DEFAULT 0,
                        req_monthly INTEGER DEFAULT 0,
                        req_total INTEGER DEFAULT 0,
                        bypass_role_id INTEGER DEFAULT 0,
                        end_color TEXT,
                        retry_count INTEGER DEFAULT 0,
                        last_error TEXT,
                        result_winners TEXT,
                        result_participant_count INTEGER DEFAULT 0
                    )
                    """
                )

                giveaway_columns = [
                    (
                        "channel_id",
                        "INTEGER",
                    ),
                    (
                        "guild_id",
                        "INTEGER",
                    ),
                    (
                        "prize",
                        "TEXT",
                    ),
                    (
                        "ends_at",
                        "REAL",
                    ),
                    (
                        "winners",
                        "INTEGER",
                    ),
                    (
                        "host_id",
                        "INTEGER",
                    ),
                    (
                        "status",
                        "TEXT DEFAULT 'ACTIVE'",
                    ),
                    (
                        "processing_started_at",
                        "REAL DEFAULT 0",
                    ),
                    (
                        "result_message_id",
                        "INTEGER DEFAULT 0",
                    ),
                    (
                        "req_daily",
                        "INTEGER DEFAULT 0",
                    ),
                    (
                        "req_weekly",
                        "INTEGER DEFAULT 0",
                    ),
                    (
                        "req_monthly",
                        "INTEGER DEFAULT 0",
                    ),
                    (
                        "req_total",
                        "INTEGER DEFAULT 0",
                    ),
                    (
                        "bypass_role_id",
                        "INTEGER DEFAULT 0",
                    ),
                    (
                        "end_color",
                        "TEXT",
                    ),
                    (
                        "retry_count",
                        "INTEGER DEFAULT 0",
                    ),
                    (
                        "last_error",
                        "TEXT",
                    ),
                    (
                        "result_winners",
                        "TEXT",
                    ),
                    (
                        "result_participant_count",
                        "INTEGER DEFAULT 0",
                    ),
                ]

                for column, definition in giveaway_columns:
                    await self._ensure_column(
                        connection,
                        "giveaway_system",
                        column,
                        definition,
                    )

                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS giveaway_participants (
                        message_id INTEGER,
                        user_id INTEGER,
                        PRIMARY KEY (message_id, user_id),
                        FOREIGN KEY (message_id)
                            REFERENCES giveaway_system(message_id)
                            ON DELETE CASCADE
                    )
                    """
                )

                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS giveaway_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        message_id INTEGER UNIQUE,
                        guild_id INTEGER,
                        prize TEXT,
                        participant_count INTEGER,
                        winners TEXT,
                        completed_at REAL
                    )
                    """
                )

                await connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_giveaway_history_guild_completed
                    ON giveaway_history(
                        guild_id,
                        completed_at DESC
                    )
                    """
                )

                # -------------------------
                # USER ACTIVITY
                # -------------------------

                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_activity (
                        guild_id INTEGER,
                        user_id INTEGER,
                        message_count INTEGER DEFAULT 0,
                        daily_message_count INTEGER DEFAULT 0,
                        week_message_count INTEGER DEFAULT 0,
                        month_message_count INTEGER DEFAULT 0,
                        last_daily_date TEXT,
                        last_weekly_date TEXT,
                        last_monthly_date TEXT,
                        PRIMARY KEY (guild_id, user_id)
                    )
                    """
                )

                for col, definition in [
                    (
                        "message_count",
                        "INTEGER DEFAULT 0",
                    ),
                    (
                        "daily_message_count",
                        "INTEGER DEFAULT 0",
                    ),
                    (
                        "week_message_count",
                        "INTEGER DEFAULT 0",
                    ),
                    (
                        "month_message_count",
                        "INTEGER DEFAULT 0",
                    ),
                    (
                        "last_daily_date",
                        "TEXT",
                    ),
                    (
                        "last_weekly_date",
                        "TEXT",
                    ),
                    (
                        "last_monthly_date",
                        "TEXT",
                    ),
                ]:
                    await self._ensure_column(
                        connection,
                        "user_activity",
                        col,
                        definition,
                    )

                # -------------------------
                # TRUST / VOUCH
                # -------------------------

                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_vouch_network (
                        guild_id INTEGER,
                        user_id INTEGER,
                        trust_score INTEGER DEFAULT 25,
                        vouches_given INTEGER DEFAULT 0,
                        vouch_positive INTEGER DEFAULT 0,
                        vouch_negative INTEGER DEFAULT 0,
                        PRIMARY KEY (guild_id, user_id)
                    )
                    """
                )

                for col, definition in [
                    (
                        "trust_score",
                        "INTEGER DEFAULT 25",
                    ),
                    (
                        "vouches_given",
                        "INTEGER DEFAULT 0",
                    ),
                    (
                        "vouch_positive",
                        "INTEGER DEFAULT 0",
                    ),
                    (
                        "vouch_negative",
                        "INTEGER DEFAULT 0",
                    ),
                ]:
                    await self._ensure_column(
                        connection,
                        "user_vouch_network",
                        col,
                        definition,
                    )

                await connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_vouch_network_leaderboard
                    ON user_vouch_network(
                        guild_id,
                        trust_score DESC,
                        user_id ASC
                    )
                    """
                )

                # This is the persistent vouch history.
                # It stores:
                # - who gave the vouch
                # - who received it
                # - + or -
                # - reason
                # - timestamp
                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vouch_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        target_id INTEGER,
                        giver_id INTEGER,
                        vouch_type TEXT NOT NULL
                            CHECK (
                                vouch_type IN (
                                    'POSITIVE',
                                    'NEGATIVE'
                                )
                            ),
                        reason TEXT,
                        timestamp REAL
                    )
                    """
                )

                # Existing installations may contain duplicates
                # from older versions before the unique rule existed.
                # Keep the oldest record.
                await connection.execute(
                    """
                    DELETE FROM vouch_history
                    WHERE id NOT IN (
                        SELECT MIN(id)
                        FROM vouch_history
                        GROUP BY
                            guild_id,
                            target_id,
                            giver_id
                    )
                    """
                )

                await connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_unique_vouch
                    ON vouch_history(
                        guild_id,
                        target_id,
                        giver_id
                    )
                    """
                )

                await connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                    idx_vouch_history_guild_time
                    ON vouch_history(
                        guild_id,
                        timestamp DESC
                    )
                    """
                )

                # -------------------------
                # OWNER TRANSACTION LOG
                # -------------------------

                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS transaction_log_config (
                        guild_id INTEGER PRIMARY KEY,
                        channel_id INTEGER NOT NULL
                    )
                    """
                )

                # -------------------------
                # TEMP BANS
                # -------------------------

                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS temporary_bans (
                        guild_id INTEGER,
                        target_id INTEGER,
                        expiry_timestamp REAL,
                        PRIMARY KEY (guild_id, target_id)
                    )
                    """
                )

                await connection.commit()

                self.connection = connection

                logger.info(
                    "Database initialized successfully: %s",
                    self.db_path,
                )

            except Exception:
                logger.exception(
                    "Database initialization failed."
                )

                if connection is not None:
                    await connection.close()

                raise

    @asynccontextmanager
    async def transaction(self):
        """
        Run several SQLite statements atomically
        under the DB operation lock.
        """
        if self.connection is None:
            raise RuntimeError(
                "Database connection is not initialized."
            )

        async with self.operation_lock:
            try:
                await self.connection.execute(
                    "BEGIN"
                )

                yield self.connection

                await self.connection.commit()

            except Exception:
                await self.connection.rollback()
                raise

    async def execute(
        self,
        query,
        params=(),
    ) -> int:
        if self.connection is None:
            raise RuntimeError(
                "Database connection is not initialized."
            )

        async with self.operation_lock:
            async with self.connection.execute(
                query,
                params,
            ) as cursor:
                rowcount = cursor.rowcount

            await self.connection.commit()

            return rowcount

    async def executemany(
        self,
        query,
        params_list,
    ) -> int:
        if self.connection is None:
            raise RuntimeError(
                "Database connection is not initialized."
            )

        async with self.operation_lock:
            async with self.connection.executemany(
                query,
                params_list,
            ) as cursor:
                rowcount = cursor.rowcount

            await self.connection.commit()

            return rowcount

    async def fetchone(
        self,
        query,
        params=(),
    ):
        if self.connection is None:
            raise RuntimeError(
                "Database connection is not initialized."
            )

        async with self.operation_lock:
            async with self.connection.execute(
                query,
                params,
            ) as cursor:
                return await cursor.fetchone()

    async def fetchall(
        self,
        query,
        params=(),
    ):
        if self.connection is None:
            raise RuntimeError(
                "Database connection is not initialized."
            )

        async with self.operation_lock:
            async with self.connection.execute(
                query,
                params,
            ) as cursor:
                return await cursor.fetchall()

    async def close(self):
        if self.connection is None:
            return

        async with self.operation_lock:
            try:
                await self.connection.close()

            except Exception:
                logger.exception(
                    "Failed to close database connection."
                )

            finally:
                self.connection = None

        logger.info(
            "Database connection closed."
            )
