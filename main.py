import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

try:
    from groq import Groq
except Exception:
    Groq = None

from database import DatabaseController


# =========================================================
# ENVIRONMENT / LOGGING
# =========================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("giveaway-trust-bot")


# =========================================================
# CONFIG
# =========================================================

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
API_SECRET = os.getenv("API_SECRET", "").strip()
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "").strip()

BOT_OWNER_ID = int(
    os.getenv("BOT_OWNER_ID", "0") or 0
)

ENVIRONMENT = os.getenv(
    "ENVIRONMENT",
    "development",
).lower().strip()

GROQ_MODEL = os.getenv(
    "GROQ_MODEL",
    "llama-3.3-70b-versatile",
).strip()

PORT = int(
    os.getenv("PORT", "10000") or 10000
)


# =========================================================
# DATABASE PATH
# =========================================================

def get_database_path() -> str:
    """
    Use DATABASE_PATH when supplied and writable.
    Otherwise use the current application directory.

    This intentionally does NOT default to /data because
    many Render services do not have a writable /data folder
    unless a persistent disk is mounted there.
    """

    configured = os.getenv(
        "DATABASE_PATH",
        "",
    ).strip()

    if configured:
        requested = Path(configured)

        try:
            requested.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            test_file = (
                requested.parent
                / ".db_write_test"
            )

            with open(
                test_file,
                "w",
                encoding="utf-8",
            ) as file:
                file.write("ok")

            try:
                test_file.unlink()
            except OSError:
                pass

            return str(requested)

        except (PermissionError, OSError):
            logger.warning(
                "DATABASE_PATH '%s' is not writable. "
                "Using local fallback database.",
                configured,
            )

    fallback = (
        Path.cwd()
        / "bot_database.db"
    )

    return str(fallback)


DATABASE_PATH = get_database_path()

logger.info(
    "Using database: %s",
    DATABASE_PATH,
)


# =========================================================
# ROLES
# =========================================================

ROLE_50_ID = 1529114068412141639
ROLE_100_ID = 1529114203204489277


# =========================================================
# DISCORD INTENTS
# =========================================================

intents = discord.Intents.default()

intents.guilds = True
intents.members = True
intents.message_content = True


# =========================================================
# HELPERS
# =========================================================

def now_timestamp() -> int:
    return int(
        time.time()
    )


def format_timestamp(
    timestamp: int | float | None,
) -> str:

    if not timestamp:
        return "Unknown"

    return datetime.fromtimestamp(
        float(timestamp),
        tz=timezone.utc,
    ).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def clamp(
    value: int,
    minimum: int,
    maximum: int,
) -> int:

    return max(
        minimum,
        min(
            maximum,
            value,
        ),
    )


def parse_duration(
    value: str,
) -> Optional[int]:

    match = re.fullmatch(
        r"\s*(\d+)\s*([smhdw])\s*",
        value.lower(),
    )

    if not match:
        return None

    amount = int(
        match.group(1)
    )

    unit = match.group(2)

    multipliers = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 604800,
    }

    seconds = (
        amount
        * multipliers[unit]
    )

    if seconds <= 0:
        return None

    if seconds > (
        30
        * 24
        * 60
        * 60
    ):
        return None

    return seconds


def owner_only():
    async def predicate(
        interaction: discord.Interaction,
    ) -> bool:

        return (
            BOT_OWNER_ID > 0
            and interaction.user.id
            == BOT_OWNER_ID
        )

    return app_commands.check(
        predicate
    )


async def safe_interaction_error(
    interaction: discord.Interaction,
    message: str,
):
    """
    Safely answer an interaction even when it
    has already been acknowledged.
    """

    try:

        if interaction.response.is_done():

            await interaction.followup.send(
                message,
                ephemeral=True,
            )

        else:

            await interaction.response.send_message(
                message,
                ephemeral=True,
            )

    except Exception:

        logger.exception(
            "Could not send interaction error"
        )


# =========================================================
# SAFE VIEW
# =========================================================

class SafeView(discord.ui.View):

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ):

        logger.exception(
            "Discord UI error",
            exc_info=(
                type(error),
                error,
                error.__traceback__,
            ),
        )

        await safe_interaction_error(
            interaction,
            "Something went wrong while processing that interaction.",
        )


# =========================================================
# SAFE MODAL
# =========================================================

class SafeModal(discord.ui.Modal):

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ):

        logger.exception(
            "Discord modal error",
            exc_info=(
                type(error),
                error,
                error.__traceback__,
            ),
        )

        await safe_interaction_error(
            interaction,
            "Something went wrong while processing that form.",
        )


# =========================================================
# BOT
# =========================================================

class GiveawayTrustBot(
    discord.Client
):

    def __init__(self):

        super().__init__(
            intents=intents
        )

        self.tree = (
            app_commands.CommandTree(
                self
            )
        )

        self.db = (
            DatabaseController(
                DATABASE_PATH
            )
        )

        self.groq = None

        if (
            GROQ_API_KEY
            and Groq is not None
        ):

            try:

                self.groq = Groq(
                    api_key=GROQ_API_KEY
                )

            except Exception:

                logger.exception(
                    "Could not initialize Groq"
                )

        self.health_runner = None
        self.health_site = None

    # =====================================================
    # SETUP HOOK
    # =====================================================

    async def setup_hook(
        self,
    ):

        logger.info(
            "Initializing database..."
        )

        await self.db.init()

        logger.info(
            "Database initialized."
        )

        # Persistent trust panel
        self.add_view(
            TrustPanelView(self)
        )

        # Recover active giveaways
        await self.recover_giveaways()

        # Start background loops
        self.giveaway_loop.start()
        self.temp_ban_loop.start()
        self.activity_loop.start()

        # Health server
        await self.start_health_server()

        # Sync commands
        try:

            synced = await self.tree.sync()

            logger.info(
                "Synced %s application command(s).",
                len(synced),
            )

        except Exception:

            logger.exception(
                "Application command sync failed"
            )

    # =====================================================
    # READY
    # =====================================================

    async def on_ready(
        self,
    ):

        logger.info(
            "Logged in as %s",
            self.user,
        )

        for guild in self.guilds:

            try:

                for member in guild.members:

                    if member.bot:
                        continue

                    await self.db.ensure_trust_user(
                        guild.id,
                        member.id,
                    )

            except Exception:

                logger.exception(
                    "Could not initialize trust users "
                    "for guild %s",
                    guild.id,
                )

    # =====================================================
    # CLOSE
    # =====================================================

    async def close(
        self,
    ):

        loops = (
            self.giveaway_loop,
            self.temp_ban_loop,
            self.activity_loop,
        )

        for loop in loops:

            try:

                if loop.is_running():
                    loop.cancel()

            except Exception:
                pass

        if self.health_runner:

            try:
                await self.health_runner.cleanup()
            except Exception:
                pass

            self.health_runner = None
            self.health_site = None

        try:
            await self.db.close()
        except Exception:
            logger.exception(
                "Could not close database"
            )

        await super().close()

    # =====================================================
    # HEALTH SERVER
    # =====================================================

    async def start_health_server(
        self,
    ):

        if self.health_runner is not None:
            return

        app = web.Application()

        app.router.add_get(
            "/",
            self.health_root,
        )

        app.router.add_get(
            "/health",
            self.health_root,
        )

        app.router.add_get(
            "/api/health",
            self.health_root,
        )

        self.health_runner = (
            web.AppRunner(app)
        )

        await self.health_runner.setup()

        self.health_site = web.TCPSite(
            self.health_runner,
            "0.0.0.0",
            PORT,
        )

        await self.health_site.start()

        logger.info(
            "Health server running on port %s",
            PORT,
        )

    async def health_root(
        self,
        request: web.Request,
    ):

        return web.json_response(
            {
                "ok": True,
                "bot": (
                    self.user.name
                    if self.user
                    else None
                ),
                "guilds": len(
                    self.guilds
                ),
                "time": now_timestamp(),
            }
        )

    # =====================================================
    # ACTIVITY
    # =====================================================

    async def record_activity(
        self,
        guild_id: int,
        user_id: int,
        *,
        message: bool = False,
        command: bool = False,
    ):

        timestamp = now_timestamp()

        await self.db.execute(
            """
            INSERT INTO user_activity (
                guild_id,
                user_id,
                messages,
                commands,
                last_active
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET
                messages =
                    user_activity.messages
                    + excluded.messages,
                commands =
                    user_activity.commands
                    + excluded.commands,
                last_active =
                    excluded.last_active
            """,
            (
                guild_id,
                user_id,
                int(message),
                int(command),
                timestamp,
            ),
        )

    # =====================================================
    # MEMBER RESOLVER
    # =====================================================

    async def resolve_member(
        self,
        guild: discord.Guild,
        value: str | int,
    ) -> Optional[discord.Member]:

        raw = str(value).strip()

        # Mention
        if raw.startswith("<@"):

            raw = (
                raw
                .replace("<@", "")
                .replace("!", "")
                .replace(">", "")
            )

        # ID
        try:

            user_id = int(raw)

            member = guild.get_member(
                user_id
            )

            if member:
                return member

            try:

                return await guild.fetch_member(
                    user_id
                )

            except discord.HTTPException:

                return None

        except ValueError:
            pass

        # Username/display name
        lowered = raw.lower()

        for member in guild.members:

            if member.bot:
                continue

            if (
                member.name.lower()
                == lowered
                or
                member.display_name.lower()
                == lowered
            ):

                return member

        return None

    # =====================================================
    # TRUST ROLES
    # =====================================================

    async def update_trust_roles(
        self,
        member: discord.Member,
        trust: int,
    ):

        role50 = member.guild.get_role(
            ROLE_50_ID
        )

        role100 = member.guild.get_role(
            ROLE_100_ID
        )

        try:

            if role50:

                if trust >= 50:

                    await member.add_roles(
                        role50,
                        reason="Trust reached 50",
                    )

                else:

                    await member.remove_roles(
                        role50,
                        reason="Trust below 50",
                    )

            if role100:

                if trust >= 100:

                    await member.add_roles(
                        role100,
                        reason="Trust reached 100",
                    )

                else:

                    await member.remove_roles(
                        role100,
                        reason="Trust below 100",
                    )

        except discord.HTTPException:

            logger.exception(
                "Could not update trust roles "
                "for member %s",
                member.id,
            )

    # =====================================================
    # TRUST PROFILE
    # =====================================================

    async def build_profile_embed(
        self,
        guild: discord.Guild,
        member: discord.Member,
    ) -> discord.Embed:

        row = await self.db.get_trust_user(
            guild.id,
            member.id,
        )

        trust = int(
            row["trust"]
            if row
            else 25
        )

        given = int(
            row["vouches_given"]
            if row
            else 0
        )

        received = int(
            row["vouches_received"]
            if row
            else 0
        )

        embed = discord.Embed(
            title=(
                f"{member.display_name}'s "
                "Vouch Profile"
            )
        )

        embed.set_thumbnail(
            url=member.display_avatar.url
        )

        embed.add_field(
            name="Trust",
            value=f"**{trust}/100**",
            inline=True,
        )

        embed.add_field(
            name="Vouches Given",
            value=str(given),
            inline=True,
        )

        embed.add_field(
            name="Vouches Received",
            value=str(received),
            inline=True,
        )

        embed.set_footer(
            text="Trust starts at 25/100"
        )

        return embed

    # =====================================================
    # VOUCH
    # =====================================================

    async def record_vouch(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        amount: int,
        reason: str,
    ):

        guild = interaction.guild

        if guild is None:

            return await interaction.response.send_message(
                "This can only be used in a server.",
                ephemeral=True,
            )

        if target.bot:

            return await interaction.response.send_message(
                "You cannot vouch a bot.",
                ephemeral=True,
            )

        if (
            target.id
            == interaction.user.id
        ):

            return await interaction.response.send_message(
                "You cannot vouch yourself.",
                ephemeral=True,
            )

        amount = (
            5
            if amount > 0
            else -5
        )

        # IMPORTANT:
        # Answer the Discord interaction immediately.
        await interaction.response.defer(
            ephemeral=True
        )

        try:

            created = (
                await self.db.record_vouch(
                    guild.id,
                    target.id,
                    interaction.user.id,
                    amount,
                    reason,
                )
            )

        except Exception:

            logger.exception(
                "Vouch database error"
            )

            return await interaction.followup.send(
                "Something went wrong while saving the vouch.",
                ephemeral=True,
            )

        if not created:

            return await interaction.followup.send(
                "You have already vouched this user. "
                "You can only vouch the same user once.",
                ephemeral=True,
            )

        try:

            row = await self.db.get_trust_user(
                guild.id,
                target.id,
            )

            trust = int(
                row["trust"]
                if row
                else 25
            )

            await self.update_trust_roles(
                target,
                trust,
            )

            direction = (
                "+Vouch"
                if amount > 0
                else "-Vouch"
            )

            embed = discord.Embed(
                title=f"{direction} recorded",
                description=(
                    f"{target.mention} now has "
                    f"**{trust}/100 Trust**."
                ),
                timestamp=datetime.now(
                    timezone.utc
                ),
            )

            embed.add_field(
                name="Given by",
                value=interaction.user.mention,
            )

            embed.add_field(
                name="Reason",
                value=(
                    reason[:200]
                    if reason
                    else "No reason provided"
                ),
                inline=False,
            )

            # Tell the user first.
            await interaction.followup.send(
                embed=embed,
                ephemeral=True,
            )

            # Then write log.
            await self.send_transaction_log(
                guild,
                embed,
            )

        except Exception:

            logger.exception(
                "Vouch processing failed"
            )

            await safe_interaction_error(
                interaction,
                "The vouch was saved, but something went wrong "
                "while finishing the operation.",
            )

    # =====================================================
    # TRANSACTION LOG
    # =====================================================

    async def send_transaction_log(
        self,
        guild: discord.Guild,
        embed: discord.Embed,
    ):

        try:

            row = await self.db.fetchone(
                """
                SELECT channel_id, enabled
                FROM transaction_log_config
                WHERE guild_id = ?
                """,
                (guild.id,),
            )

            if not row:
                return

            if not row["enabled"]:
                return

            channel = guild.get_channel(
                row["channel_id"]
            )

            if channel is None:

                try:

                    channel = (
                        await self.fetch_channel(
                            row["channel_id"]
                        )
                    )

                except discord.HTTPException:

                    return

            await channel.send(
                embed=embed
            )

        except Exception:

            logger.exception(
                "Transaction log failed"
            )

    # =====================================================
    # GIVEAWAY CREATE
    # =====================================================

    async def create_giveaway_record(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
        prize: str,
        winners: int,
        end_at: int,
        host_id: int,
    ) -> int:

        cursor = await self.db.execute(
            """
            INSERT INTO giveaway_system (
                guild_id,
                channel_id,
                message_id,
                prize,
                winners,
                end_at,
                host_id,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
            """,
            (
                guild_id,
                channel_id,
                message_id,
                prize,
                winners,
                end_at,
                host_id,
                now_timestamp(),
            ),
        )

        return int(
            cursor.lastrowid
        )

    # =====================================================
    # RECOVER GIVEAWAYS
    # =====================================================

    async def recover_giveaways(
        self,
    ):

        rows = await self.db.fetchall(
            """
            SELECT *
            FROM giveaway_system
            WHERE status = 'ACTIVE'
            """
        )

        for row in rows:

            try:

                self.add_view(
                    GiveawayJoinView(
                        self,
                        int(row["id"]),
                    ),
                    message_id=int(
                        row["message_id"]
                    ),
                )

            except Exception:

                logger.exception(
                    "Could not recover giveaway %s",
                    row["id"],
                )

    # =====================================================
    # FINISH GIVEAWAY
    # =====================================================

    async def finish_giveaway(
        self,
        giveaway_id: int,
        forced: bool = False,
    ) -> bool:

        # Prevent two loop runs / manual end at once.
        async with self.db._lock:

            db = self.db.require_db()

            await db.execute(
                "BEGIN"
            )

            try:

                cursor = await db.execute(
                    """
                    SELECT *
                    FROM giveaway_system
                    WHERE id = ?
                      AND status = 'ACTIVE'
                    """,
                    (giveaway_id,),
                )

                row = await cursor.fetchone()

                if row is None:

                    await db.rollback()

                    return False

                await db.execute(
                    """
                    UPDATE giveaway_system
                    SET status = 'PROCESSING'
                    WHERE id = ?
                    """,
                    (giveaway_id,),
                )

                await db.commit()

            except Exception:

                await db.rollback()

                raise

        try:

            participants = (
                await self.db.fetchall(
                    """
                    SELECT user_id
                    FROM giveaway_participants
                    WHERE giveaway_id = ?
                    """,
                    (giveaway_id,),
                )
            )

            user_ids = [
                int(
                    participant["user_id"]
                )
                for participant
                in participants
            ]

            winner_count = min(
                int(row["winners"]),
                len(user_ids),
            )

            if winner_count:

                winners = random.sample(
                    user_ids,
                    winner_count,
                )

            else:

                winners = []

            winner_mentions = [
                f"<@{user_id}>"
                for user_id in winners
            ]

            if winner_mentions:

                winner_text = (
                    ", ".join(
                        winner_mentions
                    )
                )

            else:

                winner_text = (
                    "No winner — "
                    "not enough participants."
                )

            embed = discord.Embed(
                title="🎉 Giveaway Ended",
                description=(
                    f"**Prize:** {row['prize']}\n\n"
                    f"**Winner(s):** {winner_text}"
                ),
            )

            embed.add_field(
                name="Participants",
                value=str(
                    len(user_ids)
                ),
            )

            guild = self.get_guild(
                int(row["guild_id"])
            )

            channel = (
                guild.get_channel(
                    int(row["channel_id"])
                )
                if guild
                else None
            )

            if channel:

                try:

                    message = (
                        await channel.fetch_message(
                            int(
                                row["message_id"]
                            )
                        )
                    )

                    await message.edit(
                        embed=embed,
                        view=None,
                    )

                except discord.HTTPException:

                    try:

                        await channel.send(
                            embed=embed
                        )

                    except discord.HTTPException:
                        pass

                if winners:

                    try:

                        await channel.send(
                            (
                                "🎉 Giveaway winner(s): "
                                f"{winner_text}"
                            )
                        )

                    except discord.HTTPException:
                        pass

            await self.db.execute(
                """
                UPDATE giveaway_system
                SET status = 'COMPLETED'
                WHERE id = ?
                """,
                (giveaway_id,),
            )

            await self.db.execute(
                """
                INSERT INTO giveaway_history (
                    giveaway_id,
                    guild_id,
                    winner_ids,
                    participant_count,
                    ended_at,
                    prize
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    giveaway_id,
                    int(row["guild_id"]),
                    json.dumps(winners),
                    len(user_ids),
                    now_timestamp(),
                    row["prize"],
                ),
            )

            return True

        except Exception:

            logger.exception(
                "Giveaway finishing failed"
            )

            try:

                await self.db.execute(
                    """
                    UPDATE giveaway_system
                    SET status = 'ACTIVE'
                    WHERE id = ?
                    """,
                    (giveaway_id,),
                )

            except Exception:

                logger.exception(
                    "Could not restore giveaway status"
                )

            return False

    # =====================================================
    # TEMP BAN LOOP
    # =====================================================

    @tasks.loop(seconds=15)
    async def temp_ban_loop(
        self,
    ):

        try:

            rows = await self.db.fetchall(
                """
                SELECT guild_id, user_id
                FROM temporary_bans
                WHERE unban_at <= ?
                """,
                (now_timestamp(),),
            )

            for row in rows:

                guild = self.get_guild(
                    int(row["guild_id"])
                )

                if guild:

                    try:

                        await guild.unban(
                            discord.Object(
                                id=int(
                                    row["user_id"]
                                )
                            ),
                            reason=(
                                "Temporary ban expired"
                            ),
                        )

                    except discord.HTTPException:
                        pass

                await self.db.execute(
                    """
                    DELETE FROM temporary_bans
                    WHERE guild_id = ?
                      AND user_id = ?
                    """,
                    (
                        row["guild_id"],
                        row["user_id"],
                    ),
                )

        except Exception:

            logger.exception(
                "Temporary ban loop failed"
            )

    @temp_ban_loop.before_loop
    async def before_temp_ban_loop(
        self,
    ):

        await self.wait_until_ready()

    # =====================================================
    # GIVEAWAY LOOP
    # =====================================================

    @tasks.loop(seconds=5)
    async def giveaway_loop(
        self,
    ):

        try:

            rows = await self.db.fetchall(
                """
                SELECT id
                FROM giveaway_system
                WHERE status = 'ACTIVE'
                  AND end_at <= ?
                LIMIT 20
                """,
                (now_timestamp(),),
            )

            for row in rows:

                await self.finish_giveaway(
                    int(row["id"])
                )

        except Exception:

            logger.exception(
                "Giveaway loop failed"
            )

    @giveaway_loop.before_loop
    async def before_giveaway_loop(
        self,
    ):

        await self.wait_until_ready()

    # =====================================================
    # ACTIVITY LOOP
    # =====================================================

    @tasks.loop(seconds=60)
    async def activity_loop(
        self,
    ):

        return

    @activity_loop.before_loop
    async def before_activity_loop(
        self,
    ):

        await self.wait_until_ready()

    # =====================================================
    # GROQ
    # =====================================================

    async def ask_ai(
        self,
        content: str,
    ) -> Optional[str]:

        if not self.groq:
            return None

        def call_groq():

            response = (
                self.groq.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a helpful "
                                "Discord bot. "
                                "Keep responses concise."
                            ),
                        },
                        {
                            "role": "user",
                            "content": content,
                        },
                    ],
                    temperature=0.5,
                    max_tokens=500,
                )
            )

            return (
                response
                .choices[0]
                .message
                .content
                .strip()
            )

        try:

            return await asyncio.to_thread(
                call_groq
            )

        except Exception:

            logger.exception(
                "Groq request failed"
            )

            return None

    # =====================================================
    # MEMBER JOIN
    # =====================================================

    async def on_member_join(
        self,
        member: discord.Member,
    ):

        try:

            await self.db.ensure_trust_user(
                member.guild.id,
                member.id,
            )

        except Exception:

            logger.exception(
                "Could not initialize trust user"
            )

    # =====================================================
    # MESSAGE
    # =====================================================

    async def on_message(
        self,
        message: discord.Message,
    ):

        if message.author.bot:
            return

        if message.guild:

            try:

                await self.record_activity(
                    message.guild.id,
                    message.author.id,
                    message=True,
                )

            except Exception:

                logger.exception(
                    "Could not record message activity"
                )

        if (
            self.user
            and self.user in message.mentions
        ):

            clean = (
                message.content
                .replace(
                    f"<@{self.user.id}>",
                    "",
                )
                .replace(
                    f"<@!{self.user.id}>",
                    "",
                )
                .strip()
            )

            if clean:

                answer = (
                    await self.ask_ai(
                        clean
                    )
                )

                if answer:

                    try:

                        await message.reply(
                            answer[:1900],
                            mention_author=False,
                        )

                    except discord.HTTPException:
                        pass


# =========================================================
# TRUST PANEL
# =========================================================

class TrustPanelView(
    SafeView
):

    def __init__(
        self,
        bot: GiveawayTrustBot,
    ):

        super().__init__(
            timeout=None
        )

        self.bot = bot

    # -----------------------------------------------------
    # CHECK MY VOUCH
    # -----------------------------------------------------

    @discord.ui.button(
        label="Check My Vouch",
        style=discord.ButtonStyle.primary,
        custom_id="trust:check_me",
    )
    async def check_me(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if interaction.guild is None:

            return await interaction.response.send_message(
                "This can only be used in a server.",
                ephemeral=True,
            )

        await interaction.response.defer(
            ephemeral=True
        )

        member = interaction.user

        if not isinstance(
            member,
            discord.Member,
        ):

            try:

                member = (
                    await interaction.guild.fetch_member(
                        interaction.user.id
                    )
                )

            except discord.HTTPException:

                return await interaction.followup.send(
                    "Could not load your member profile.",
                    ephemeral=True,
                )

        embed = (
            await self.bot.build_profile_embed(
                interaction.guild,
                member,
            )
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )

    # -----------------------------------------------------
    # CHECK USER
    # -----------------------------------------------------

    @discord.ui.button(
        label="Check User's Vouch",
        style=discord.ButtonStyle.secondary,
        custom_id="trust:check_user",
    )
    async def check_user(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if interaction.guild is None:

            return await interaction.response.send_message(
                "This can only be used in a server.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Choose a user below:",
            view=CheckMemberView(
                self.bot
            ),
            ephemeral=True,
        )

    # -----------------------------------------------------
    # VOUCH USER
    # -----------------------------------------------------

    @discord.ui.button(
        label="Vouch A User",
        style=discord.ButtonStyle.success,
        custom_id="trust:vouch_user",
    )
    async def vouch_user(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if interaction.guild is None:

            return await interaction.response.send_message(
                "This can only be used in a server.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Choose a user below:",
            view=VouchTargetView(
                self.bot
            ),
            ephemeral=True,
        )

    # -----------------------------------------------------
    # REWARDS
    # -----------------------------------------------------

    @discord.ui.button(
        label="Vouch Rewards",
        style=discord.ButtonStyle.secondary,
        custom_id="trust:rewards",
    )
    async def rewards(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        embed = discord.Embed(
            title="Vouch Rewards",
            description=(
                "Trust runs from 0 to 100.\n"
                "Two roles, both automatic:\n\n"
                f"• **50** · <@&{ROLE_50_ID}>\n"
                f"• **100** · <@&{ROLE_100_ID}>"
            ),
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )

    # -----------------------------------------------------
    # LEADERBOARD
    # -----------------------------------------------------

    @discord.ui.button(
        label="Vouch Leaderboard",
        style=discord.ButtonStyle.primary,
        custom_id="trust:leaderboard",
    )
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if interaction.guild is None:

            return await interaction.response.send_message(
                "This can only be used in a server.",
                ephemeral=True,
            )

        await interaction.response.defer(
            ephemeral=True
        )

        view = VouchLeaderboardView(
            self.bot,
            interaction.guild,
            0,
        )

        embed = await view.build_embed()

        await interaction.followup.send(
            embed=embed,
            view=view,
            ephemeral=True,
        )


# =========================================================
# CHECK MEMBER VIEW
# =========================================================

class CheckMemberView(
    SafeView
):

    def __init__(
        self,
        bot: GiveawayTrustBot,
    ):

        super().__init__(
            timeout=120
        )

        self.bot = bot

        # IMPORTANT:
        # UserSelect is created normally.
        # No @discord.ui.UserSelect decorator.
        self.user_select = (
            discord.ui.UserSelect(
                placeholder="Select a user",
                min_values=1,
                max_values=1,
            )
        )

        self.user_select.callback = (
            self.user_select_callback
        )

        self.add_item(
            self.user_select
        )

        enter = discord.ui.Button(
            label="Enter User ID / Name",
            style=discord.ButtonStyle.secondary,
        )

        enter.callback = (
            self.enter_name
        )

        self.add_item(
            enter
        )

    async def user_select_callback(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:

            return await interaction.response.send_message(
                "This can only be used in a server.",
                ephemeral=True,
            )

        await interaction.response.defer(
            ephemeral=True
        )

        member = self.user_select.values[0]

        embed = (
            await self.bot.build_profile_embed(
                interaction.guild,
                member,
            )
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )

    async def enter_name(
        self,
        interaction: discord.Interaction,
    ):

        await interaction.response.send_modal(
            CheckMemberModal(
                self.bot
            )
        )


# =========================================================
# CHECK MEMBER MODAL
# =========================================================

class CheckMemberModal(
    SafeModal
):

    member_input = discord.ui.TextInput(
        label="User ID or Name",
        placeholder="User ID or name",
        max_length=100,
    )

    def __init__(
        self,
        bot: GiveawayTrustBot,
    ):

        super().__init__(
            title="Check Member"
        )

        self.bot = bot

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:

            return await interaction.response.send_message(
                "This can only be used in a server.",
                ephemeral=True,
            )

        await interaction.response.defer(
            ephemeral=True
        )

        member = (
            await self.bot.resolve_member(
                interaction.guild,
                self.member_input.value,
            )
        )

        if member is None:

            return await interaction.followup.send(
                "Member not found.",
                ephemeral=True,
            )

        embed = (
            await self.bot.build_profile_embed(
                interaction.guild,
                member,
            )
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )


# =========================================================
# VOUCH TARGET VIEW
# =========================================================

class VouchTargetView(
    SafeView
):

    def __init__(
        self,
        bot: GiveawayTrustBot,
    ):

        super().__init__(
            timeout=120
        )

        self.bot = bot

        self.user_select = (
            discord.ui.UserSelect(
                placeholder="Select a user",
                min_values=1,
                max_values=1,
            )
        )

        self.user_select.callback = (
            self.user_select_callback
        )

        self.add_item(
            self.user_select
        )

        enter = discord.ui.Button(
            label="Enter User ID / Name",
            style=discord.ButtonStyle.secondary,
        )

        enter.callback = (
            self.enter_name
        )

        self.add_item(
            enter
        )

    async def user_select_callback(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:

            return await interaction.response.send_message(
                "This can only be used in a server.",
                ephemeral=True,
            )

        target = self.user_select.values[0]

        if target.bot:

            return await interaction.response.send_message(
                "You cannot vouch a bot.",
                ephemeral=True,
            )

        if (
            target.id
            == interaction.user.id
        ):

            return await interaction.response.send_message(
                "You cannot vouch yourself.",
                ephemeral=True,
            )

        await interaction.response.send_message(
            "Choose your vouch type:",
            view=VouchTypeView(
                self.bot,
                target,
            ),
            ephemeral=True,
        )

    async def enter_name(
        self,
        interaction: discord.Interaction,
    ):

        await interaction.response.send_modal(
            VouchMemberModal(
                self.bot
            )
        )


# =========================================================
# VOUCH MEMBER MODAL
# =========================================================

class VouchMemberModal(
    SafeModal
):

    member_input = discord.ui.TextInput(
        label="User ID or Name",
        placeholder="User ID or name",
        max_length=100,
    )

    def __init__(
        self,
        bot: GiveawayTrustBot,
    ):

        super().__init__(
            title="Vouch A User"
        )

        self.bot = bot

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:

            return await interaction.response.send_message(
                "This can only be used in a server.",
                ephemeral=True,
            )

        await interaction.response.defer(
            ephemeral=True
        )

        member = (
            await self.bot.resolve_member(
                interaction.guild,
                self.member_input.value,
            )
        )

        if member is None:

            return await interaction.followup.send(
                "Member not found.",
                ephemeral=True,
            )

        if member.bot:

            return await interaction.followup.send(
                "You cannot vouch a bot.",
                ephemeral=True,
            )

        if (
            member.id
            == interaction.user.id
        ):

            return await interaction.followup.send(
                "You cannot vouch yourself.",
                ephemeral=True,
            )

        await interaction.followup.send(
            "Choose your vouch type:",
            view=VouchTypeView(
                self.bot,
                member,
            ),
            ephemeral=True,
        )


# =========================================================
# VOUCH TYPE VIEW
# =========================================================

class VouchTypeView(
    SafeView
):

    def __init__(
        self,
        bot: GiveawayTrustBot,
        target: discord.Member,
    ):

        super().__init__(
            timeout=120
        )

        self.bot = bot
        self.target = target

    @discord.ui.button(
        label="+Vouch",
        style=discord.ButtonStyle.success,
    )
    async def positive(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        await interaction.response.send_modal(
            VouchReasonModal(
                self.bot,
                self.target,
                5,
            )
        )

    @discord.ui.button(
        label="-Vouch",
        style=discord.ButtonStyle.danger,
    )
    async def negative(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        await interaction.response.send_modal(
            VouchReasonModal(
                self.bot,
                self.target,
                -5,
            )
        )


# =========================================================
# VOUCH REASON MODAL
# =========================================================

class VouchReasonModal(
    SafeModal
):

    reason = discord.ui.TextInput(
        label="Reason",
        placeholder="Reason",
        max_length=200,
        required=True,
    )

    def __init__(
        self,
        bot: GiveawayTrustBot,
        target: discord.Member,
        amount: int,
    ):

        super().__init__(
            title="Vouch Reason"
        )

        self.bot = bot
        self.target = target
        self.amount = amount

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):

        await self.bot.record_vouch(
            interaction,
            self.target,
            self.amount,
            str(
                self.reason.value
            ).strip(),
        )


# =========================================================
# LEADERBOARD VIEW
# =========================================================

class VouchLeaderboardView(
    SafeView
):

    def __init__(
        self,
        bot: GiveawayTrustBot,
        guild: discord.Guild,
        page: int = 0,
    ):

        super().__init__(
            timeout=180
        )

        self.bot = bot
        self.guild = guild
        self.page = page
        self.per_page = 100

        self.refresh_buttons()

    def refresh_buttons(
        self,
    ):

        self.clear_items()

        previous = discord.ui.Button(
            label="<",
            style=discord.ButtonStyle.secondary,
            disabled=(
                self.page <= 0
            ),
        )

        next_button = discord.ui.Button(
            label=">",
            style=discord.ButtonStyle.secondary,
        )

        previous.callback = (
            self.previous_page
        )

        next_button.callback = (
            self.next_page
        )

        self.add_item(
            previous
        )

        self.add_item(
            next_button
        )

    async def count_members(
        self,
    ) -> int:

        return len(
            [
                member
                for member
                in self.guild.members
                if not member.bot
            ]
        )

    async def build_embed(
        self,
    ) -> discord.Embed:

        offset = (
            self.page
            * self.per_page
        )

        rows = await self.bot.db.fetchall(
            """
            SELECT
                user_id,
                trust,
                vouches_received
            FROM trust_users
            WHERE guild_id = ?
            ORDER BY
                trust DESC,
                vouches_received DESC
            LIMIT ?
            OFFSET ?
            """,
            (
                self.guild.id,
                self.per_page,
                offset,
            ),
        )

        valid_ids = {
            member.id
            for member
            in self.guild.members
            if not member.bot
        }

        rows = [
            row
            for row in rows
            if int(
                row["user_id"]
            )
            in valid_ids
        ]

        embed = discord.Embed(
            title="Vouch Leaderboard"
        )

        if not rows:

            embed.description = (
                "No members found."
            )

            return embed

        lines = []

        for index, row in enumerate(
            rows,
            start=offset + 1,
        ):

            lines.append(
                (
                    f"**{index}.** "
                    f"<@{row['user_id']}> "
                    f"— **{row['trust']}/100** Trust "
                    f"· {row['vouches_received']} received"
                )
            )

        embed.description = (
            "\n".join(lines)
        )

        embed.set_footer(
            text=(
                f"Page {self.page + 1}"
            )
        )

        return embed

    async def previous_page(
        self,
        interaction: discord.Interaction,
    ):

        await interaction.response.defer()

        if self.page <= 0:
            return

        self.page -= 1

        self.refresh_buttons()

        embed = (
            await self.build_embed()
        )

        await interaction.edit_original_response(
            embed=embed,
            view=self,
        )

    async def next_page(
        self,
        interaction: discord.Interaction,
    ):

        await interaction.response.defer()

        member_count = (
            await self.count_members()
        )

        max_page = max(
            0,
            (
                member_count - 1
            )
            // self.per_page,
        )

        if self.page >= max_page:
            return

        self.page += 1

        self.refresh_buttons()

        embed = (
            await self.build_embed()
        )

        await interaction.edit_original_response(
            embed=embed,
            view=self,
        )


# =========================================================
# GIVEAWAY EMBED
# =========================================================

def make_giveaway_embed(
    prize: str,
    winners: int,
    end_at: int,
    host: discord.abc.User,
) -> discord.Embed:

    embed = discord.Embed(
        title="🎉 Giveaway",
        description=(
            f"**Prize:** {prize}"
        ),
    )

    embed.add_field(
        name="Winners",
        value=str(winners),
        inline=True,
    )

    embed.add_field(
        name="Ends",
        value=f"<t:{end_at}:R>",
        inline=True,
    )

    embed.add_field(
        name="Hosted by",
        value=host.mention,
        inline=False,
    )

    embed.set_footer(
        text="Click Join Giveaway to enter"
    )

    return embed


# =========================================================
# GIVEAWAY JOIN VIEW
# =========================================================

class GiveawayJoinView(
    SafeView
):

    def __init__(
        self,
        bot: GiveawayTrustBot,
        giveaway_id: int,
    ):

        super().__init__(
            timeout=None
        )

        self.bot = bot
        self.giveaway_id = giveaway_id

        button = discord.ui.Button(
            label="Join Giveaway",
            style=discord.ButtonStyle.success,
            custom_id=(
                f"giveaway:join:{giveaway_id}"
            ),
        )

        button.callback = (
            self.join_callback
        )

        self.add_item(
            button
        )

    async def join_callback(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:

            return await interaction.response.send_message(
                "This can only be used in a server.",
                ephemeral=True,
            )

        # Immediate acknowledgement.
        await interaction.response.defer(
            ephemeral=True
        )

        try:

            row = await self.bot.db.fetchone(
                """
                SELECT *
                FROM giveaway_system
                WHERE id = ?
                """,
                (self.giveaway_id,),
            )

            if row is None:

                return await interaction.followup.send(
                    "This giveaway no longer exists.",
                    ephemeral=True,
                )

            if row["status"] != "ACTIVE":

                return await interaction.followup.send(
                    "This giveaway is no longer active.",
                    ephemeral=True,
                )

            if int(
                row["end_at"]
            ) <= now_timestamp():

                await self.bot.finish_giveaway(
                    self.giveaway_id
                )

                return await interaction.followup.send(
                    "This giveaway has ended.",
                    ephemeral=True,
                )

            try:

                await self.bot.db.execute(
                    """
                    INSERT INTO giveaway_participants (
                        giveaway_id,
                        user_id,
                        joined_at
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        self.giveaway_id,
                        interaction.user.id,
                        now_timestamp(),
                    ),
                )

            except aiosqlite.IntegrityError:

                return await interaction.followup.send(
                    "You are already entered.",
                    ephemeral=True,
                )

            await interaction.followup.send(
                "You are entered! 🎉",
                ephemeral=True,
            )

        except Exception:

            logger.exception(
                "Giveaway join failed"
            )

            await interaction.followup.send(
                "Could not join the giveaway.",
                ephemeral=True,
            )


# =========================================================
# BOT INSTANCE
# =========================================================

bot = GiveawayTrustBot()


# =========================================================
# GIVEAWAY GROUP
# =========================================================

giveaway_group = app_commands.Group(
    name="giveaway",
    description="Giveaway commands",
)

bot.tree.add_command(
    giveaway_group
)


# =========================================================
# /activity
# =========================================================

@bot.tree.command(
    name="activity",
    description="Show your activity stats",
)
async def activity_command(
    interaction: discord.Interaction,
):

    if interaction.guild is None:

        return await interaction.response.send_message(
            "This can only be used in a server.",
            ephemeral=True,
        )

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        await bot.record_activity(
            interaction.guild.id,
            interaction.user.id,
            command=True,
        )

        row = await bot.db.fetchone(
            """
            SELECT
                messages,
                commands,
                last_active
            FROM user_activity
            WHERE guild_id = ?
              AND user_id = ?
            """,
            (
                interaction.guild.id,
                interaction.user.id,
            ),
        )

        embed = discord.Embed(
            title=(
                f"{interaction.user.display_name}"
                "'s Activity"
            ),
        )

        embed.add_field(
            name="Messages",
            value=str(
                row["messages"]
                if row
                else 0
            ),
        )

        embed.add_field(
            name="Commands",
            value=str(
                row["commands"]
                if row
                else 0
            ),
        )

        embed.add_field(
            name="Last active",
            value=format_timestamp(
                row["last_active"]
                if row
                else None
            ),
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )

    except Exception:

        logger.exception(
            "Activity command failed"
        )

        await interaction.followup.send(
            "Could not load activity.",
            ephemeral=True,
        )


# =========================================================
# /botstats
# =========================================================

@bot.tree.command(
    name="botstats",
    description="Show bot statistics",
)
async def botstats_command(
    interaction: discord.Interaction,
):

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        row = await bot.db.fetchone(
            """
            SELECT COUNT(*) AS count
            FROM giveaway_system
            """
        )

        giveaway_count = int(
            row["count"]
            if row
            else 0
        )

        embed = discord.Embed(
            title="Bot Stats"
        )

        embed.add_field(
            name="Servers",
            value=str(
                len(bot.guilds)
            ),
        )

        embed.add_field(
            name="Users cached",
            value=str(
                len(bot.users)
            ),
        )

        embed.add_field(
            name="Giveaways",
            value=str(
                giveaway_count
            ),
        )

        embed.add_field(
            name="Latency",
            value=(
                f"{round(bot.latency * 1000)} ms"
            ),
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )

    except Exception:

        logger.exception(
            "Botstats command failed"
        )

        await interaction.followup.send(
            "Could not load bot stats.",
            ephemeral=True,
        )


# =========================================================
# /say
# =========================================================

@bot.tree.command(
    name="say",
    description="Make the bot send a message",
)
@app_commands.describe(
    message="Message to send",
)
async def say_command(
    interaction: discord.Interaction,
    message: str,
):

    if interaction.guild is None:

        return await interaction.response.send_message(
            "This can only be used in a server.",
            ephemeral=True,
        )

    if not interaction.user.guild_permissions.manage_messages:

        return await interaction.response.send_message(
            "You need Manage Messages to use this.",
            ephemeral=True,
        )

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        await interaction.channel.send(
            message
        )

        await interaction.followup.send(
            "Sent.",
            ephemeral=True,
        )

    except discord.HTTPException:

        await interaction.followup.send(
            "I could not send that message.",
            ephemeral=True,
        )


# =========================================================
# /vouchpanel
# =========================================================

@bot.tree.command(
    name="vouchpanel",
    description="Post the vouch panel",
)
async def vouchpanel_command(
    interaction: discord.Interaction,
):

    if interaction.guild is None:

        return await interaction.response.send_message(
            "This can only be used in a server.",
            ephemeral=True,
        )

    if not interaction.user.guild_permissions.manage_guild:

        return await interaction.response.send_message(
            "You need Manage Server to use this.",
            ephemeral=True,
        )

    await interaction.response.defer(
        ephemeral=True
    )

    embed = discord.Embed(
        title="Vouch Panel",
        description=(
            "Vouches show who is safe to trade sprites with. "
            "Everyone starts at **25 Trust** out of 100.\n\n"
            "**How it works**\n"
            "• Traded with someone? Hit **Vouch A User**.\n"
            "• Pick **+Vouch** or **-Vouch**.\n"
            "• +Vouch raises Trust. -Vouch lowers it.\n"
            "• Check anyone with **Check User's Vouch** before you trade.\n\n"
            "**Ranks**\n"
            f"• **<@&{ROLE_50_ID}>** · 50\n"
            f"• **<@&{ROLE_100_ID}>** · 100\n\n"
            "*Only vouch people you actually traded with. "
            "Fake, spam or revenge vouches can get you permanently banned.*"
        ),
    )

    try:

        await interaction.channel.send(
            embed=embed,
            view=TrustPanelView(bot),
        )

    except discord.HTTPException:

        return await interaction.followup.send(
            "I could not post the vouch panel here.",
            ephemeral=True,
        )

    await interaction.followup.send(
        "Vouch panel posted.",
        ephemeral=True,
    )


# =========================================================
# /sync
# =========================================================

@bot.tree.command(
    name="sync",
    description="Sync application commands",
)
@owner_only()
async def sync_command(
    interaction: discord.Interaction,
):

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        synced = await bot.tree.sync()

        await interaction.followup.send(
            f"Synced {len(synced)} command(s).",
            ephemeral=True,
        )

    except Exception:

        logger.exception(
            "Manual sync failed"
        )

        await interaction.followup.send(
            "Sync failed. Check the bot logs.",
            ephemeral=True,
        )


# =========================================================
# /tempban
# =========================================================

@bot.tree.command(
    name="tempban",
    description="Temporarily ban a member",
)
@app_commands.describe(
    member="Member to ban",
    duration="For example 10m, 2h or 1d",
    reason="Ban reason",
)
@app_commands.checks.has_permissions(
    ban_members=True
)
async def tempban_command(
    interaction: discord.Interaction,
    member: discord.Member,
    duration: str,
    reason: str = "Temporary ban",
):

    if interaction.guild is None:

        return await interaction.response.send_message(
            "This can only be used in a server.",
            ephemeral=True,
        )

    seconds = parse_duration(
        duration
    )

    if seconds is None:

        return await interaction.response.send_message(
            "Invalid duration. Use e.g. `10m`, `2h`, `1d` or `1w`.",
            ephemeral=True,
        )

    if member.id == interaction.user.id:

        return await interaction.response.send_message(
            "You cannot temp-ban yourself.",
            ephemeral=True,
        )

    if (
        member.top_role
        >= interaction.user.top_role
        and
        interaction.user.id
        != interaction.guild.owner_id
    ):

        return await interaction.response.send_message(
            "You cannot ban someone with an equal/higher role.",
            ephemeral=True,
        )

    await interaction.response.defer(
        ephemeral=True
    )

    unban_at = (
        now_timestamp()
        + seconds
    )

    try:

        await member.ban(
            reason=reason[:500],
            delete_message_days=0,
        )

        await bot.db.execute(
            """
            INSERT INTO temporary_bans (
                guild_id,
                user_id,
                unban_at,
                reason
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET
                unban_at = excluded.unban_at,
                reason = excluded.reason
            """,
            (
                interaction.guild.id,
                member.id,
                unban_at,
                reason[:500],
            ),
        )

    except discord.HTTPException:

        return await interaction.followup.send(
            "I could not ban that member.",
            ephemeral=True,
        )

    except Exception:

        logger.exception(
            "Temporary ban failed"
        )

        return await interaction.followup.send(
            "The temporary ban could not be saved.",
            ephemeral=True,
        )

    await interaction.followup.send(
        f"{member} was banned until <t:{unban_at}:F>.",
        ephemeral=True,
    )


# =========================================================
# /transactionlog
# =========================================================

@bot.tree.command(
    name="transactionlog",
    description="Configure vouch transaction logs",
)
@app_commands.describe(
    channel="Channel for transaction logs",
    enabled="Enable or disable logging",
)
@owner_only()
async def transactionlog_command(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    enabled: bool = True,
):

    if interaction.guild is None:

        return await interaction.response.send_message(
            "This can only be used in a server.",
            ephemeral=True,
        )

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        await bot.db.execute(
            """
            INSERT INTO transaction_log_config (
                guild_id,
                channel_id,
                enabled
            )
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET
                channel_id = excluded.channel_id,
                enabled = excluded.enabled
            """,
            (
                interaction.guild.id,
                channel.id,
                int(enabled),
            ),
        )

    except Exception:

        logger.exception(
            "Transaction log config failed"
        )

        return await interaction.followup.send(
            "Could not update transaction log settings.",
            ephemeral=True,
        )

    await interaction.followup.send(
        (
            "Transaction logging "
            f"{'enabled' if enabled else 'disabled'} "
            f"in {channel.mention}."
        ),
        ephemeral=True,
    )


# =========================================================
# /giveaway create
# =========================================================

@giveaway_group.command(
    name="create",
    description="Create a giveaway",
)
@app_commands.describe(
    prize="Giveaway prize",
    duration="Duration, e.g. 10m, 2h or 1d",
    winners="Number of winners",
)
@app_commands.checks.has_permissions(
    manage_guild=True
)
async def giveaway_create(
    interaction: discord.Interaction,
    prize: str,
    duration: str,
    winners: int = 1,
):

    if interaction.guild is None:

        return await interaction.response.send_message(
            "This can only be used in a server.",
            ephemeral=True,
        )

    seconds = parse_duration(
        duration
    )

    if seconds is None:

        return await interaction.response.send_message(
            "Invalid duration. Use e.g. `10m`, `2h`, `1d` or `1w`.",
            ephemeral=True,
        )

    if winners < 1 or winners > 50:

        return await interaction.response.send_message(
            "Winners must be between 1 and 50.",
            ephemeral=True,
        )

    await interaction.response.defer(
        ephemeral=True
    )

    end_at = (
        now_timestamp()
        + seconds
    )

    try:

        embed = make_giveaway_embed(
            prize,
            winners,
            end_at,
            interaction.user,
        )

        message = (
            await interaction.channel.send(
                embed=embed
            )
        )

        giveaway_id = (
            await bot.create_giveaway_record(
                interaction.guild.id,
                interaction.channel.id,
                message.id,
                prize[:1000],
                winners,
                end_at,
                interaction.user.id,
            )
        )

        view = GiveawayJoinView(
            bot,
            giveaway_id,
        )

        await message.edit(
            view=view
        )

        bot.add_view(
            GiveawayJoinView(
                bot,
                giveaway_id,
            ),
            message_id=message.id,
        )

        await interaction.followup.send(
            f"Giveaway created: `{giveaway_id}`.",
            ephemeral=True,
        )

    except Exception:

        logger.exception(
            "Giveaway creation failed"
        )

        await interaction.followup.send(
            "I could not create the giveaway.",
            ephemeral=True,
        )


# =========================================================
# /giveaway end
# =========================================================

@giveaway_group.command(
    name="end",
    description="End a giveaway early",
)
@app_commands.describe(
    giveaway_id="Giveaway ID",
)
@app_commands.checks.has_permissions(
    manage_guild=True
)
async def giveaway_end(
    interaction: discord.Interaction,
    giveaway_id: int,
):

    if interaction.guild is None:

        return await interaction.response.send_message(
            "This can only be used in a server.",
            ephemeral=True,
        )

    await interaction.response.defer(
        ephemeral=True
    )

    try:

        row = await bot.db.fetchone(
            """
            SELECT
                id,
                status,
                guild_id
            FROM giveaway_system
            WHERE id = ?
            """,
            (giveaway_id,),
        )

        if (
            row is None
            or
            int(row["guild_id"])
            != interaction.guild.id
        ):

            return await interaction.followup.send(
                "Giveaway not found in this server.",
                ephemeral=True,
            )

        if row["status"] != "ACTIVE":

            return await interaction.followup.send(
                "That giveaway is not active.",
                ephemeral=True,
            )

        success = (
            await bot.finish_giveaway(
                giveaway_id,
                forced=True,
            )
        )

        await interaction.followup.send(
            (
                "Giveaway ended."
                if success
                else
                "I could not end the giveaway."
            ),
            ephemeral=True,
        )

    except Exception:

        logger.exception(
            "Giveaway end failed"
        )

        await interaction.followup.send(
            "Something went wrong while ending the giveaway.",
            ephemeral=True,
        )


# =========================================================
# APPLICATION COMMAND ERROR
# =========================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):

    logger.error(
        "Application command error: %r",
        error,
        exc_info=(
            type(error),
            error,
            error.__traceback__,
        ),
    )

    if isinstance(
        error,
        app_commands.MissingPermissions,
    ):

        message = (
            "You do not have the required permissions."
        )

    elif isinstance(
        error,
        app_commands.CheckFailure,
    ):

        message = (
            "You are not allowed to use this command."
        )

    else:

        message = (
            "Something went wrong while processing "
            "that command."
        )

    await safe_interaction_error(
        interaction,
        message,
    )


# =========================================================
# MAIN
# =========================================================

async def main():

    if not TOKEN:

        raise RuntimeError(
            "DISCORD_TOKEN is missing"
        )

    try:

        await bot.start(
            TOKEN
        )

    finally:

        try:

            if not bot.is_closed():

                await bot.close()

        except Exception:

            logger.exception(
                "Error while closing bot"
            )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    asyncio.run(
        main()
            )
