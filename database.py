import aiosqlite
import os

class DatabaseController:
    def __init__(self, db_path="bot_database.db"):
        self.db_path = db_path
        self.connection = None

    async def initialize_database(self):
        """Initializes the SQLite database connection and creates required tables."""
        self.connection = await aiosqlite.connect(self.db_path)
        await self.connection.execute("PRAGMA journal_mode=WAL;")
        
        # Giveaway System Table
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS giveaway_system (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER,
                guild_id INTEGER,
                prize TEXT,
                ends_at REAL,
                winners INTEGER,
                status TEXT,
                processing_started_at REAL,
                result_message_id INTEGER,
                req_daily INTEGER,
                req_weekly INTEGER,
                req_monthly INTEGER,
                req_total INTEGER,
                bypass_role_id INTEGER,
                end_color TEXT
            )
        """)

        # Giveaway Participants Table
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS giveaway_participants (
                message_id INTEGER,
                user_id INTEGER,
                PRIMARY KEY (message_id, user_id)
            )
        """)

        # User Activity Table
        await self.connection.execute("""
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
        """)

        # User Vouches Table
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS user_vouches (
                guild_id INTEGER,
                target_id INTEGER,
                giver_id INTEGER,
                reason TEXT,
                PRIMARY KEY (guild_id, target_id, giver_id)
            )
        """)

        # Temporary Bans Table
        await self.connection.execute("""
            CREATE TABLE IF NOT EXISTS temporary_bans (
                guild_id INTEGER,
                target_id INTEGER,
                expiry_timestamp REAL,
                PRIMARY KEY (guild_id, target_id)
            )
        """)

        # Safety Migration: Check if older tables are missing new columns and add them automatically
        async with self.connection.execute("PRAGMA table_info(giveaway_system);") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
        
        if "winners" not in columns:
            await self.connection.execute("ALTER TABLE giveaway_system ADD COLUMN winners INTEGER DEFAULT 1;")
        if "prize" not in columns:
            await self.connection.execute("ALTER TABLE giveaway_system ADD COLUMN prize TEXT;")
        if "end_color" not in columns:
            await self.connection.execute("ALTER TABLE giveaway_system ADD COLUMN end_color TEXT;")

        await self.connection.commit()

    async def execute(self, query, params=()):
        """Executes a write query (INSERT, UPDATE, DELETE) and commits changes."""
        async with self.connection.execute(query, params) as cursor:
            await self.connection.commit()
            return cursor.rowcount

    async def fetchone(self, query, params=()):
        """Fetches a single row from the database."""
        async with self.connection.execute(query, params) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, query, params=()):
        """Fetches multiple rows from the database."""
        async with self.connection.execute(query, params) as cursor:
            return await cursor.fetchall()

    async def close(self):
        """Closes the database connection cleanly."""
        if self.connection:
            await self.connection.close()
