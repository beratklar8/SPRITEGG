import os
import aiosqlite
import asyncio

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "bot.db")

class DatabaseController:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._lock = asyncio.Lock()

    async def initialize_database(self):
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute("PRAGMA journal_mode=WAL;")
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS warning_records (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        target_id INTEGER,
                        moderator_id INTEGER,
                        reason TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS temporary_bans (
                        guild_id INTEGER,
                        target_id INTEGER,
                        expiry_timestamp REAL,
                        PRIMARY KEY (guild_id, target_id)
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS giveaway_system (
                        message_id INTEGER PRIMARY KEY,
                        channel_id INTEGER,
                        guild_id INTEGER,
                        prize_name TEXT,
                        ends_at REAL,
                        winners INTEGER,
                        is_ended BOOLEAN DEFAULT 0,
                        req_daily INTEGER DEFAULT 0,
                        req_weekly INTEGER DEFAULT 0,
                        req_monthly INTEGER DEFAULT 0,
                        req_total INTEGER DEFAULT 0,
                        bypass_role_id INTEGER DEFAULT 0,
                        end_color TEXT
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS server_settings (
                        guild_id INTEGER PRIMARY KEY,
                        automod_status BOOLEAN DEFAULT 0
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS reaction_role_bindings (
                        message_id INTEGER,
                        emoji_icon TEXT,
                        role_id INTEGER,
                        PRIMARY KEY (message_id, emoji_icon)
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_vouches (
                        guild_id INTEGER,
                        target_id INTEGER,
                        giver_id INTEGER,
                        reason TEXT,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (guild_id, target_id, giver_id)
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS sticky_messages (
                        channel_id INTEGER PRIMARY KEY,
                        guild_id INTEGER,
                        text TEXT,
                        message_id INTEGER
                    )
                """)
                # --- TABELLEN VOOR SPRITE TRADING ---
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS sprites (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        rarity TEXT NOT NULL,
                        image TEXT,
                        released BOOLEAN DEFAULT 1
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS epic_accounts (
                        discord_id INTEGER PRIMARY KEY,
                        epic_account_id TEXT,
                        epic_display_name TEXT,
                        access_token TEXT,
                        refresh_token TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_sprites (
                        discord_id INTEGER,
                        sprite_id TEXT,
                        obtained_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (discord_id, sprite_id)
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        seller_id INTEGER,
                        buyer_id INTEGER,
                        status TEXT DEFAULT 'pending',
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS trade_items (
                        trade_id INTEGER,
                        user_id INTEGER,
                        sprite_id TEXT
                    )
                """)
                await conn.commit()

    async def execute(self, query: str, params: tuple = ()):
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.execute(query, params)
                await conn.commit()
                return cursor

    async def fetchone(self, query: str, params: tuple = ()):
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as conn:
                async with conn.execute(query, params) as cursor:
                    return await cursor.fetchone()

    async def fetchall(self, query: str, params: tuple = ()):
        async with self._lock:
            async with aiosqlite.connect(self.db_path) as conn:
                async with conn.execute(query, params) as cursor:
                    return await cursor.fetchall()
