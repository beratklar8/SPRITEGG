import os
import aiosqlite
from dotenv import load_dotenv

load_dotenv()

DB_NAME = "bot_database.db"
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
EPIC_REDIRECT_URI = os.getenv("EPIC_REDIRECT_URI")
EPIC_CLIENT_ID = os.getenv("EPIC_CLIENT_ID", "your-epic-client-id")
EPIC_CLIENT_SECRET = os.getenv("EPIC_CLIENT_SECRET", "your-epic-client-secret")
API_SECRET = os.getenv("API_SECRET", "super-secret-api-key")

if ENVIRONMENT == "production":
    if not EPIC_REDIRECT_URI or not EPIC_REDIRECT_URI.startswith("https://"):
        raise ValueError("CRITICAL: EPIC_REDIRECT_URI must use HTTPS in production.")
else:
    EPIC_REDIRECT_URI = EPIC_REDIRECT_URI or "http://localhost:10000/epic/callback"

class DatabaseController:
    """Controller voor het beheren van de SQLite database en tabellen inclusief helper methodes."""
    def __init__(self, db_path: str = DB_NAME):
        self.db_path = db_path

    async def initialize_database(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS server_settings (
                    guild_id INTEGER PRIMARY KEY,
                    automod_status BOOLEAN DEFAULT 0
                )
            """)
            await db.execute("""
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
            await db.execute("""
                CREATE TABLE IF NOT EXISTS giveaway_system (
                    message_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    guild_id INTEGER,
                    prize_name TEXT,
                    ends_at REAL,
                    winners INTEGER,
                    is_ended INTEGER DEFAULT 0,
                    req_daily INTEGER DEFAULT 0,
                    req_weekly INTEGER DEFAULT 0,
                    req_monthly INTEGER DEFAULT 0,
                    req_total INTEGER DEFAULT 0,
                    bypass_role_id INTEGER DEFAULT 0,
                    end_color TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS giveaway_participants (
                    message_id INTEGER,
                    user_id INTEGER,
                    PRIMARY KEY (message_id, user_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_vouches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    target_id INTEGER,
                    giver_id INTEGER,
                    reason TEXT,
                    UNIQUE(guild_id, target_id, giver_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS warning_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    target_id INTEGER,
                    moderator_id INTEGER,
                    reason TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS temporary_bans (
                    guild_id INTEGER,
                    target_id INTEGER,
                    expiry_timestamp REAL,
                    PRIMARY KEY (guild_id, target_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sticky_messages (
                    channel_id INTEGER PRIMARY KEY,
                    guild_id INTEGER,
                    text TEXT,
                    message_id INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS reaction_role_bindings (
                    message_id INTEGER,
                    emoji_icon TEXT,
                    role_id INTEGER,
                    PRIMARY KEY (message_id, emoji_icon)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state TEXT PRIMARY KEY,
                    discord_id INTEGER,
                    expires_at REAL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS epic_accounts (
                    discord_id INTEGER PRIMARY KEY,
                    epic_account_id TEXT,
                    access_token TEXT,
                    refresh_token TEXT,
                    updated_at TIMESTAMP
                )
            """)
            await db.commit()

    async def execute(self, query: str, parameters: tuple = ()) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(query, parameters)
            await db.commit()
            return cursor.rowcount

    async def fetchone(self, query: str, parameters: tuple = ()):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(query, parameters) as cursor:
                return await cursor.fetchone()

    async def fetchall(self, query: str, parameters: tuple = ()):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(query, parameters) as cursor:
                return await cursor.fetchall()
