import aiosqlite
import asyncio
import time
import logging

logger = logging.getLogger("giveaway_bot")

class DatabaseController:
    def __init__(self, db_path: str = "bot_database.db"):
        self.db_path = db_path
        self.db: aiosqlite.Connection = None
        self.lock = asyncio.Lock()

    async def connect(self):
        self.db = await aiosqlite.connect(self.db_path)
        await self.db.execute("PRAGMA journal_mode=WAL;")
        await self.create_tables()

    async def initialize_database(self):
        """Alias voor connect() zodat bestaande aanroepen in main.py direct werken."""
        await self.connect()

    async def close(self):
        if self.db:
            await self.db.close()

    async def create_tables(self):
        async with self.lock:
            # 1. Zorg dat de basistabel bestaat
            await self.db.execute("""
                CREATE TABLE IF NOT EXISTS giveaway_system (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER UNIQUE,
                    channel_id INTEGER,
                    prize TEXT,
                    winner_count INTEGER,
                    ends_at INTEGER,
                    status TEXT DEFAULT 'ACTIVE',
                    processing_started_at INTEGER DEFAULT 0,
                    processing_owner TEXT DEFAULT NULL,
                    result_message_id INTEGER DEFAULT 0
                )
            """)
            
            # 2. Automatische migratie: controleer op oude kolomnamen zoals 'prize_name'
            cursor = await self.db.execute("PRAGMA table_info(giveaway_system);")
            columns = [row[1] for row in await cursor.fetchall()]
            
            if "prize" not in columns:
                if "prize_name" in columns:
                    await self.db.execute("ALTER TABLE giveaway_system RENAME COLUMN prize_name TO prize;")
                    logger.info("Database migratie: kolom 'prize_name' succesvol hernoemd naar 'prize'.")
                else:
                    await self.db.execute("ALTER TABLE giveaway_system ADD COLUMN prize TEXT;")
                    logger.info("Database migratie: kolom 'prize' toegevoegd aan giveaway_system.")

            await self.db.commit()

    # --- GENERIEKE HELPER METHODES ---

    async def execute(self, query: str, parameters: tuple = ()):
        """Voert een losse query uit (INSERT, UPDATE, DELETE) en commit direct."""
        async with self.lock:
            cursor = await self.db.execute(query, parameters)
            await self.db.commit()
            return cursor

    async def fetchall(self, query: str, parameters: tuple = ()):
        """Haalt meerdere rijen op met een SELECT query."""
        async with self.lock:
            async with self.db.execute(query, parameters) as cursor:
                return await cursor.fetchall()

    async def fetchone(self, query: str, parameters: tuple = ()):
        """Haalt één rij op met een SELECT query."""
        async with self.lock:
            async with self.db.execute(query, parameters) as cursor:
                return await cursor.fetchone()

    # --- SPECIFIEKE GIVEAWAY METHODES ---

    async def claim_giveaway(self, worker_token: str, current_time: int):
        """
        Claimt een actieve giveaway waarvan de eindtijd (ends_at) is verstreken,
        óf waarvan de verwerkingslease is verlopen (>900 sec).
        """
        async with self.lock:
            query = """
                UPDATE giveaway_system
                SET status = 'PROCESSING',
                    processing_owner = ?,
                    processing_started_at = ?
                WHERE id = (
                    SELECT id FROM giveaway_system
                    WHERE (status = 'ACTIVE' AND ends_at <= ?)
                       OR (status = 'PROCESSING' AND processing_started_at < ? - 900)
                    LIMIT 1
                )
                RETURNING id, message_id, channel_id, prize, winner_count, result_message_id;
            """
            try:
                cursor = await self.db.execute(query, (worker_token, current_time, current_time, current_time))
                row = await cursor.fetchone()
                await self.db.commit()
                
                if row:
                    return {
                        "id": row[0],
                        "message_id": row[1],
                        "channel_id": row[2],
                        "prize": row[3],
                        "winner_count": row[4],
                        "result_message_id": row[5]
                    }
            except Exception as e:
                logger.error(f"Fout bij claimen giveaway: {e}")
            return None

    async def refresh_lease(self, giveaway_id: int, worker_token: str, current_time: int):
        """Verlengt de lease (heartbeat) om time-outs bij zware taken te voorkomen."""
        async with self.lock:
            query = """
                UPDATE giveaway_system
                SET processing_started_at = ?
                WHERE id = ? AND processing_owner = ? AND status = 'PROCESSING'
            """
            await self.db.execute(query, (current_time, giveaway_id, worker_token))
            await self.db.commit()

    async def set_result_pending(self, giveaway_id: int, worker_token: str, result_message_id: int):
        """
        Slaat de result_message_id alvast op *voordat* de status op COMPLETED gaat.
        """
        async with self.lock:
            query = """
                UPDATE giveaway_system
                SET result_message_id = ?
                WHERE id = ? AND processing_owner = ?
            """
            await self.db.execute(query, (result_message_id, giveaway_id, worker_token))
            await self.db.commit()

    async def finalize_giveaway(self, giveaway_id: int, worker_token: str) -> bool:
        """Zet de giveaway definitief op COMPLETED."""
        async with self.lock:
            query = """
                UPDATE giveaway_system
                SET status = 'COMPLETED',
                    processing_owner = NULL
                WHERE id = ? AND processing_owner = ?
            """
            cursor = await self.db.execute(query, (giveaway_id, worker_token))
            await self.db.commit()
            return cursor.rowcount > 0

    async def release_giveaway_lease(self, giveaway_id: int, worker_token: str):
        """Zet de giveaway terug naar ACTIVE bij een onverwachte fout of tijdelijke storing."""
        async with self.lock:
            query = """
                UPDATE giveaway_system
                SET status = 'ACTIVE',
                    processing_owner = NULL,
                    processing_started_at = 0
                WHERE id = ? AND processing_owner = ?
            """
            await self.db.execute(query, (giveaway_id, worker_token))
            await self.db.commit()

    async def mark_giveaway_completed_safely(self, giveaway_id: int, worker_token: str, result_message_id: int = 0):
        """Markeert als COMPLETED als het originele giveaway-bericht op Discord definitief weg is."""
        async with self.lock:
            query = """
                UPDATE giveaway_system
                SET status = 'COMPLETED',
                    result_message_id = ?,
                    processing_owner = NULL
                WHERE id = ? AND processing_owner = ?
            """
            await self.db.execute(query, (result_message_id, giveaway_id, worker_token))
            await self.db.commit()
