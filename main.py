import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from groq import Groq

from database import DatabaseController


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("bot")


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
API_SECRET = os.getenv("API_SECRET", "")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0") or 0)
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
PORT = int(os.getenv("PORT", "10000"))

if os.getenv("DATABASE_PATH"):
    DATABASE_PATH = os.getenv("DATABASE_PATH")
elif ENVIRONMENT in {"production", "render"}:
    DATABASE_PATH = "/data/bot_database.db"
else:
    DATABASE_PATH = "bot_database.db"

ROLE_50_ID = 1529114068412141639
ROLE_100_ID = 1529114203204489277


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True


# -------------------------
# Helpers
# -------------------------

def now_timestamp() -> float:
    return time.time()


def format_timestamp(timestamp: float) -> str:
    return discord.utils.format_dt(
        datetime.fromtimestamp(timestamp, tz=timezone.utc),
        style="F",
    )


def clamp(value: int, minimum: int = 0, maximum: int = 100) -> int:
    return max(minimum, min(maximum, value))


def parse_duration(value: str) -> Optional[int]:
    """Accept 30, 30s, 10m, 2h, 1d and return seconds."""
    value = value.strip().lower()
    if not value:
        return None

    match = re.fullmatch(r"(\d+)\s*([smhd]?)", value)
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)

    multiplier = {
        "": 60,   # bare numbers are minutes
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
    }[unit]

    seconds = amount * multiplier
    if seconds <= 0:
        return None
    return seconds


def owner_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == BOT_OWNER_ID:
            return True

        if interaction.guild is not None and interaction.user.id == interaction.guild.owner_id:
            return True

        raise app_commands.CheckFailure("Only the bot owner/server owner can use this command.")

    return app_commands.check(predicate)


class GiveawayTrustBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.db = DatabaseController(DATABASE_PATH)
        self.groq = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

        self.giveaway_group = app_commands.Group(
            name="giveaway",
            description="Giveaway management commands.",
        )

        self.health_runner: web.AppRunner | None = None
        self.health_site: web.TCPSite | None = None
        self.ready_once = False

    # -------------------------
    # Lifecycle
    # -------------------------

    async def setup_hook(self):
        await self.db.initialize_database()

        # Persistent Trust panel.
        self.add_view(TrustPanelView(self))

        # Recover giveaways after restarts.
        await self.db.execute(
            """
            UPDATE giveaway_system
            SET status = 'ACTIVE', processing_started_at = 0
            WHERE status = 'PROCESSING'
            """
        )

        active_giveaways = await self.db.fetchall(
            """
            SELECT message_id
            FROM giveaway_system
            WHERE status = 'ACTIVE'
            """
        )

        for row in active_giveaways:
            self.add_view(
                GiveawayJoinView(self, int(row[0])),
                message_id=int(row[0]),
            )

        try:
            self.giveaway_group.add_command(self.giveaway_create)
        except app_commands.CommandAlreadyRegistered:
            pass

        try:
            self.giveaway_group.add_command(self.giveaway_end)
        except app_commands.CommandAlreadyRegistered:
            pass

        if self.giveaway_group not in self.tree.get_commands():
            self.tree.add_command(self.giveaway_group)

        await self.tree.sync()

        if not self.giveaway_loop.is_running():
            self.giveaway_loop.start()
        if not self.temp_ban_loop.is_running():
            self.temp_ban_loop.start()
        if not self.activity_loop.is_running():
            self.activity_loop.start()

        await self.start_health_server()

    async def on_ready(self):
        if not self.ready_once:
            self.ready_once = True
            logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")

            # Give existing non-bot guild members the default 25 Trust profile.
            for guild in self.guilds:
                await self.ensure_guild_trust_users(guild)

        logger.info("Connected to %d guild(s).", len(self.guilds))

    async def close(self):
        for loop in (
            self.giveaway_loop,
            self.temp_ban_loop,
            self.activity_loop,
        ):
            if loop.is_running():
                loop.cancel()

        if self.health_runner is not None:
            try:
                await self.health_runner.cleanup()
            except Exception:
                logger.exception("Failed to clean up health server.")
            finally:
                self.health_runner = None
                self.health_site = None

        await self.db.close()
        await super().close()

    # -------------------------
    # Health server
    # -------------------------

    async def health_handler(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "bot": self.user.name if self.user else None,
                "guilds": len(self.guilds),
                "timestamp": now_timestamp(),
            }
        )

    async def api_status_handler(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "online",
                "guilds": len(self.guilds),
                "latency_ms": round(self.latency * 1000, 2),
                "database": bool(self.db.connection is not None),
            }
        )

    async def start_health_server(self):
        app = web.Application()
        app.router.add_get("/health", self.health_handler)
        app.router.add_get("/api/status", self.api_status_handler)

        self.health_runner = web.AppRunner(app)
        await self.health_runner.setup()
        self.health_site = web.TCPSite(
            self.health_runner,
            "0.0.0.0",
            PORT,
        )
        await self.health_site.start()
        logger.info("Health server listening on 0.0.0.0:%s", PORT)

    # -------------------------
    # Activity
    # -------------------------

    async def record_activity(self, guild_id: int, user_id: int):
        if self.db.connection is None:
            return

        today = datetime.now(timezone.utc).date().isoformat()
        week_key = datetime.now(timezone.utc).strftime("%G-W%V")
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")

        async with self.db.transaction() as connection:
            async with connection.execute(
                """
                SELECT
                    message_count,
                    daily_message_count,
                    week_message_count,
                    month_message_count,
                    last_daily_date,
                    last_weekly_date,
                    last_monthly_date
                FROM user_activity
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ) as cursor:
                row = await cursor.fetchone()

            if row is None:
                await connection.execute(
                    """
                    INSERT INTO user_activity (
                        guild_id,
                        user_id,
                        message_count,
                        daily_message_count,
                        week_message_count,
                        month_message_count,
                        last_daily_date,
                        last_weekly_date,
                        last_monthly_date
                    )
                    VALUES (?, ?, 1, 1, 1, 1, ?, ?, ?)
                    """,
                    (guild_id, user_id, today, week_key, month_key),
                )
                return

            message_count, daily, weekly, monthly, last_daily, last_weekly, last_monthly = row

            if last_daily != today:
                daily = 0
            if last_weekly != week_key:
                weekly = 0
            if last_monthly != month_key:
                monthly = 0

            await connection.execute(
                """
                UPDATE user_activity
                SET
                    message_count = ?,
                    daily_message_count = ?,
                    week_message_count = ?,
                    month_message_count = ?,
                    last_daily_date = ?,
                    last_weekly_date = ?,
                    last_monthly_date = ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (
                    int(message_count) + 1,
                    int(daily) + 1,
                    int(weekly) + 1,
                    int(monthly) + 1,
                    today,
                    week_key,
                    month_key,
                    guild_id,
                    user_id,
                ),
            )

    # -------------------------
    # Trust / Vouch
    # -------------------------

    async def ensure_trust_user(self, guild_id: int, user_id: int):
        await self.db.execute(
            """
            INSERT OR IGNORE INTO user_vouch_network (
                guild_id,
                user_id,
                trust_score,
                vouches_given,
                vouch_positive,
                vouch_negative
            )
            VALUES (?, ?, 25, 0, 0, 0)
            """,
            (guild_id, user_id),
        )

    async def ensure_guild_trust_users(self, guild: discord.Guild):
        users = [member for member in guild.members if not member.bot]
        if not users:
            return

        await self.db.executemany(
            """
            INSERT OR IGNORE INTO user_vouch_network (
                guild_id,
                user_id,
                trust_score,
                vouches_given,
                vouch_positive,
                vouch_negative
            )
            VALUES (?, ?, 25, 0, 0, 0)
            """,
            [(guild.id, member.id) for member in users],
        )

    async def get_trust_profile(self, guild_id: int, user_id: int):
        await self.ensure_trust_user(guild_id, user_id)
        return await self.db.fetchone(
            """
            SELECT
                trust_score,
                vouches_given,
                vouch_positive,
                vouch_negative
            FROM user_vouch_network
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        )

    @staticmethod
    def trust_bar(score: int) -> str:
        filled = round(score / 10)
        return "█" * filled + "░" * (10 - filled)

    async def update_vouch_roles(self, guild: discord.Guild, user_id: int, trust_score: int):
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

        role_50 = guild.get_role(ROLE_50_ID)
        role_100 = guild.get_role(ROLE_100_ID)

        try:
            if role_50 is not None:
                if trust_score >= 50:
                    if role_50 not in member.roles:
                        await member.add_roles(role_50, reason="Vouch Trust reached 50")
                elif role_50 in member.roles:
                    await member.remove_roles(role_50, reason="Vouch Trust fell below 50")

            if role_100 is not None:
                if trust_score >= 100:
                    if role_100 not in member.roles:
                        await member.add_roles(role_100, reason="Vouch Trust reached 100")
                elif role_100 in member.roles:
                    await member.remove_roles(role_100, reason="Vouch Trust fell below 100")
        except discord.Forbidden:
            logger.warning("Missing Manage Roles or role hierarchy is incorrect in guild %s.", guild.id)
        except discord.HTTPException:
            logger.exception("Failed to update Trust roles for %s in guild %s.", user_id, guild.id)

    async def resolve_member(self, guild: discord.Guild, value: str) -> Optional[discord.Member]:
        value = value.strip()
        if not value:
            return None

        mention_match = re.fullmatch(r"<@!?(\d+)>", value)
        if mention_match:
            value = mention_match.group(1)

        if value.isdigit():
            member = guild.get_member(int(value))
            if member is not None:
                return member
            try:
                return await guild.fetch_member(int(value))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None

        lowered = value.lower()
        for member in guild.members:
            if member.name.lower() == lowered:
                return member
            if member.display_name.lower() == lowered:
                return member
            if member.global_name and member.global_name.lower() == lowered:
                return member

        return None

    async def record_vouch(
        self,
        interaction: discord.Interaction,
        target_id: int,
        vouch_type: str,
        reason: str,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This can only be used inside a server.",
                ephemeral=True,
            )
            return

        reason = reason.strip()
        if not reason:
            await interaction.response.send_message(
                "❌ Reason is required.",
                ephemeral=True,
            )
            return

        if len(reason) > 200:
            await interaction.response.send_message(
                "❌ Reason is too long. Keep it short (max 200 characters).",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        giver_id = interaction.user.id

        if target_id == giver_id:
            await interaction.response.send_message(
                "❌ You cannot vouch yourself.",
                ephemeral=True,
            )
            return

        target = guild.get_member(target_id)
        if target is None:
            try:
                target = await guild.fetch_member(target_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                target = None

        if target is None:
            await interaction.response.send_message(
                "❌ That user is no longer in this server.",
                ephemeral=True,
            )
            return

        if target.bot:
            await interaction.response.send_message(
                "❌ You cannot vouch a bot.",
                ephemeral=True,
            )
            return

        delta = 1 if vouch_type == "POSITIVE" else -1
        label = "+Vouch" if vouch_type == "POSITIVE" else "-Vouch"
        timestamp = now_timestamp()

        try:
            async with self.db.transaction() as connection:
                await connection.execute(
                    """
                    INSERT OR IGNORE INTO user_vouch_network (
                        guild_id,
                        user_id,
                        trust_score,
                        vouches_given,
                        vouch_positive,
                        vouch_negative
                    )
                    VALUES (?, ?, 25, 0, 0, 0)
                    """,
                    (guild.id, target_id),
                )
                await connection.execute(
                    """
                    INSERT OR IGNORE INTO user_vouch_network (
                        guild_id,
                        user_id,
                        trust_score,
                        vouches_given,
                        vouch_positive,
                        vouch_negative
                    )
                    VALUES (?, ?, 25, 0, 0, 0)
                    """,
                    (guild.id, giver_id),
                )

                async with connection.execute(
                    """
                    SELECT trust_score
                    FROM user_vouch_network
                    WHERE guild_id = ? AND user_id = ?
                    """,
                    (guild.id, target_id),
                ) as cursor:
                    row = await cursor.fetchone()

                old_score = int(row[0]) if row else 25

                cursor = await connection.execute(
                    """
                    INSERT OR IGNORE INTO vouch_history (
                        guild_id,
                        target_id,
                        giver_id,
                        vouch_type,
                        reason,
                        timestamp
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        guild.id,
                        target_id,
                        giver_id,
                        vouch_type,
                        reason,
                        timestamp,
                    ),
                )

                if cursor.rowcount == 0:
                    await interaction.response.send_message(
                        "❌ You already vouched this user. You can only vouch someone once.",
                        ephemeral=True,
                    )
                    return

                new_score = clamp(old_score + delta)

                await connection.execute(
                    """
                    UPDATE user_vouch_network
                    SET trust_score = ?
                    WHERE guild_id = ? AND user_id = ?
                    """,
                    (new_score, guild.id, target_id),
                )

                if vouch_type == "POSITIVE":
                    await connection.execute(
                        """
                        UPDATE user_vouch_network
                        SET
                            vouches_given = vouches_given + 1,
                            vouch_positive = vouch_positive + 1
                        WHERE guild_id = ? AND user_id = ?
                        """,
                        (guild.id, giver_id),
                    )
                else:
                    await connection.execute(
                        """
                        UPDATE user_vouch_network
                        SET
                            vouches_given = vouches_given + 1,
                            vouch_negative = vouch_negative + 1
                        WHERE guild_id = ? AND user_id = ?
                        """,
                        (guild.id, giver_id),
                    )

        except aiosqlite.Error:
            logger.exception("Failed to save vouch transaction.")
            await interaction.response.send_message(
                "❌ The vouch could not be saved. Try again.",
                ephemeral=True,
            )
            return

        await self.update_vouch_roles(guild, target_id, new_score)
        await self.send_transaction_log(
            guild=guild,
            giver=interaction.user,
            target=target,
            vouch_type=vouch_type,
            reason=reason,
            old_score=old_score,
            new_score=new_score,
            timestamp=timestamp,
        )

        embed = discord.Embed(
            title=f"{label} recorded",
            description=(
                f"{target.mention} is now at **{new_score}/100 Trust**.\n\n"
                f"**Reason:** {reason}"
            ),
            color=discord.Color.green() if delta > 0 else discord.Color.red(),
        )
        embed.set_footer(text=f"Trust: {self.trust_bar(new_score)}")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def send_transaction_log(
        self,
        guild: discord.Guild,
        giver: discord.abc.User,
        target: discord.abc.User,
        vouch_type: str,
        reason: str,
        old_score: int,
        new_score: int,
        timestamp: float,
    ):
        row = await self.db.fetchone(
            """
            SELECT channel_id
            FROM transaction_log_config
            WHERE guild_id = ?
            """,
            (guild.id,),
        )

        if row is None:
            return

        channel_id = int(row[0])
        channel = guild.get_channel(channel_id)

        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

        if not isinstance(channel, discord.TextChannel):
            return

        label = "+Vouch" if vouch_type == "POSITIVE" else "-Vouch"
        embed = discord.Embed(
            title="Vouch Transaction",
            color=discord.Color.green() if vouch_type == "POSITIVE" else discord.Color.red(),
            timestamp=datetime.fromtimestamp(timestamp, tz=timezone.utc),
        )
        embed.add_field(name="Vouched by", value=f"{giver.mention} (`{giver.id}`)", inline=False)
        embed.add_field(name="Vouched user", value=f"{target.mention} (`{target.id}`)", inline=False)
        embed.add_field(name="Type", value=label, inline=True)
        embed.add_field(name="Trust", value=f"{old_score} → **{new_score}**", inline=True)
        embed.add_field(name="Reason", value=discord.utils.escape_markdown(reason), inline=False)
        embed.add_field(name="Time", value=format_timestamp(timestamp), inline=False)
        embed.set_footer(text=f"Guild: {guild.name}")

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning("Cannot send transaction log in channel %s.", channel_id)
        except discord.HTTPException:
            logger.exception("Failed to send transaction log in channel %s.", channel_id)

    # -------------------------
    # Giveaway system
    # -------------------------

    async def finish_giveaway(self, message_id: int):
        claimed = await self.db.execute(
            """
            UPDATE giveaway_system
            SET
                status = 'PROCESSING',
                processing_started_at = ?
            WHERE message_id = ?
              AND status = 'ACTIVE'
              AND ends_at <= ?
            """,
            (now_timestamp(), message_id, now_timestamp()),
        )

        if claimed != 1:
            return

        row = await self.db.fetchone(
            """
            SELECT
                channel_id,
                guild_id,
                prize,
                winners,
                host_id,
                retry_count
            FROM giveaway_system
            WHERE message_id = ?
            """,
            (message_id,),
        )

        if row is None:
            return

        channel_id, guild_id, prize, winner_count, host_id, retry_count = row

        try:
            channel = self.get_channel(int(channel_id))
            if channel is None:
                channel = await self.fetch_channel(int(channel_id))

            participant_rows = await self.db.fetchall(
                """
                SELECT user_id
                FROM giveaway_participants
                WHERE message_id = ?
                """,
                (message_id,),
            )

            participant_ids = [int(r[0]) for r in participant_rows]
            random.shuffle(participant_ids)
            selected = participant_ids[: max(0, int(winner_count))]

            winner_text = "No eligible winners."
            if selected:
                winner_text = ", ".join(f"<@{user_id}>" for user_id in selected)

            result_embed = discord.Embed(
                title="Giveaway Ended",
                description=(
                    f"**Prize:** {prize}\n"
                    f"**Winners:** {winner_text}\n"
                    f"**Participants:** {len(participant_ids)}"
                ),
                color=discord.Color.gold(),
            )
            result_embed.set_footer(text=f"Hosted by <@{int(host_id)}>")

            result_message = await channel.send(embed=result_embed)

            await self.db.execute(
                """
                UPDATE giveaway_system
                SET
                    status = 'COMPLETED',
                    result_message_id = ?,
                    result_winners = ?,
                    result_participant_count = ?,
                    last_error = NULL
                WHERE message_id = ?
                """,
                (
                    result_message.id,
                    json.dumps(selected),
                    len(participant_ids),
                    message_id,
                ),
            )

            await self.db.execute(
                """
                INSERT OR IGNORE INTO giveaway_history (
                    message_id,
                    guild_id,
                    prize,
                    participant_count,
                    winners,
                    completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    guild_id,
                    prize,
                    len(participant_ids),
                    json.dumps(selected),
                    now_timestamp(),
                ),
            )

            try:
                await channel.fetch_message(message_id)
            except Exception:
                pass

        except Exception as exc:
            logger.exception("Failed to finish giveaway %s", message_id)
            await self.db.execute(
                """
                UPDATE giveaway_system
                SET
                    status = 'ACTIVE',
                    processing_started_at = 0,
                    retry_count = ?,
                    last_error = ?
                WHERE message_id = ?
                """,
                (
                    int(retry_count or 0) + 1,
                    str(exc)[:1000],
                    message_id,
                ),
            )

    @tasks.loop(seconds=15)
    async def giveaway_loop(self):
        try:
            rows = await self.db.fetchall(
                """
                SELECT message_id
                FROM giveaway_system
                WHERE status = 'ACTIVE'
                  AND ends_at <= ?
                """,
                (now_timestamp(),),
            )

            for row in rows:
                await self.finish_giveaway(int(row[0]))
        except Exception:
            logger.exception("Giveaway loop failed.")

    @giveaway_loop.before_loop
    async def before_giveaway_loop(self):
        await self.wait_until_ready()

    # -------------------------
    # Temporary bans
    # -------------------------

    @tasks.loop(seconds=30)
    async def temp_ban_loop(self):
        try:
            rows = await self.db.fetchall(
                """
                SELECT guild_id, target_id
                FROM temporary_bans
                WHERE expiry_timestamp <= ?
                """,
                (now_timestamp(),),
            )

            for guild_id, target_id in rows:
                guild = self.get_guild(int(guild_id))
                if guild is None:
                    continue

                try:
                    await guild.unban(
                        discord.Object(id=int(target_id)),
                        reason="Temporary ban expired",
                    )
                except discord.NotFound:
                    # Already unbanned. The desired state is still unbanned.
                    pass
                except discord.Forbidden:
                    logger.warning("Cannot unban %s in guild %s.", target_id, guild_id)
                    continue
                except discord.HTTPException:
                    logger.exception("HTTP error unbanning %s in guild %s.", target_id, guild_id)
                    continue

                await self.db.execute(
                    """
                    DELETE FROM temporary_bans
                    WHERE guild_id = ? AND target_id = ?
                    """,
                    (guild_id, target_id),
                )
        except Exception:
            logger.exception("Temporary ban loop failed.")

    @temp_ban_loop.before_loop
    async def before_temp_ban_loop(self):
        await self.wait_until_ready()

    # -------------------------
    # Activity loop
    # -------------------------

    @tasks.loop(hours=1)
    async def activity_loop(self):
        # Activity counters are rolled forward lazily whenever a message is recorded.
        return

    @activity_loop.before_loop
    async def before_activity_loop(self):
        await self.wait_until_ready()

    # -------------------------
    # AI
    # -------------------------

    async def ask_ai(self, prompt: str) -> str:
        if self.groq is None:
            return "AI is not configured yet."

        def run_request():
            response = self.groq.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a helpful Discord bot. Keep answers concise and friendly.",
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                temperature=0.7,
                max_tokens=600,
            )
            return response.choices[0].message.content.strip()

        try:
            return await asyncio.to_thread(run_request)
        except Exception:
            logger.exception("Groq request failed.")
            return "❌ AI is temporarily unavailable."

    # -------------------------
    # Commands
    # -------------------------

    @app_commands.command(name="activity", description="Show server activity statistics.")
    @owner_only()
    async def activity_command(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return

        total_messages_row = await self.db.fetchone(
            "SELECT COALESCE(SUM(message_count), 0) FROM user_activity WHERE guild_id = ?",
            (interaction.guild.id,),
        )
        active_users_row = await self.db.fetchone(
            "SELECT COUNT(*) FROM user_activity WHERE guild_id = ? AND message_count > 0",
            (interaction.guild.id,),
        )

        embed = discord.Embed(
            title="Server Activity",
            description=(
                f"**Total messages:** {int(total_messages_row[0])}\n"
                f"**Active users:** {int(active_users_row[0])}"
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="botstats", description="Show bot statistics.")
    @owner_only()
    async def botstats_command(self, interaction: discord.Interaction):
        guild_count = len(self.guilds)
        vouch_count_row = await self.db.fetchone("SELECT COUNT(*) FROM vouch_history")
        giveaway_count_row = await self.db.fetchone("SELECT COUNT(*) FROM giveaway_history")

        embed = discord.Embed(
            title="Bot Stats",
            description=(
                f"**Guilds:** {guild_count}\n"
                f"**Vouches:** {int(vouch_count_row[0])}\n"
                f"**Completed giveaways:** {int(giveaway_count_row[0])}\n"
                f"**Latency:** {round(self.latency * 1000, 2)} ms"
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="say", description="Make the bot send a message.")
    @app_commands.describe(message="Message to send")
    @owner_only()
    async def say_command(self, interaction: discord.Interaction, message: str):
        await interaction.response.defer(ephemeral=True)
        await interaction.channel.send(message)
        await interaction.followup.send("✅ Sent.", ephemeral=True)

    @app_commands.command(name="vouchpanel", description="Post the Trader Vouch System panel.")
    @owner_only()
    async def vouchpanel_command(self, interaction: discord.Interaction):
        description = (
            "Vouches show who is safe to trade sprites with. Everyone starts at **25 Trust** out of 100.\n\n"
            "**How it works**\n"
            "• Traded with someone? Hit **Vouch A User**.\n"
            "• Pick **+Vouch** or **-Vouch**.\n"
            "• +Vouch raises Trust. -Vouch lowers it.\n"
            "• Check anyone with **Check User's Vouch** before you trade.\n\n"
            "**Ranks**\n"
            "• **<@&1529114068412141639>** · 50\n"
            "• **<@&1529114203204489277>** · 100\n\n"
            "*Only vouch people you actually traded with. Fake, spam or revenge vouches can get you permanently banned.*"
        )

        embed = discord.Embed(
            title="Trader Vouch System",
            description=description,
            color=discord.Color.blurple(),
        )

        await interaction.channel.send(
            embed=embed,
            view=TrustPanelView(self),
        )
        await interaction.response.send_message("✅ Vouch panel posted.", ephemeral=True)

    @app_commands.command(name="sync", description="Sync slash commands.")
    @owner_only()
    async def sync_command(self, interaction: discord.Interaction):
        synced = await self.tree.sync()
        await interaction.response.send_message(
            f"✅ Synced {len(synced)} command(s).",
            ephemeral=True,
        )

    @app_commands.command(name="tempban", description="Temporarily ban a member.")
    @app_commands.describe(user="User to ban", duration="Duration, e.g. 30m, 2h, 1d")
    @owner_only()
    async def tempban_command(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return

        seconds = parse_duration(duration)
        if seconds is None:
            await interaction.response.send_message(
                "❌ Invalid duration. Example: `30m`, `2h`, `1d`.",
                ephemeral=True,
            )
            return

        try:
            await interaction.guild.ban(
                user,
                reason=f"Temporary ban by {interaction.user}",
                delete_message_seconds=0,
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I cannot ban that user. Check my Ban Members permission and role hierarchy.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ Discord rejected the ban request.",
                ephemeral=True,
            )
            return

        await self.db.execute(
            """
            INSERT INTO temporary_bans (guild_id, target_id, expiry_timestamp)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, target_id)
            DO UPDATE SET expiry_timestamp = excluded.expiry_timestamp
            """,
            (interaction.guild.id, user.id, now_timestamp() + seconds),
        )

        await interaction.response.send_message(
            f"✅ {user.mention} was temporarily banned for `{duration}`.",
            ephemeral=True,
        )

    @app_commands.command(name="transactionlog", description="Set the vouch transaction log channel.")
    @app_commands.describe(channel="Channel where vouch transaction logs will be posted")
    @owner_only()
    async def transactionlog_command(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return

        permissions = channel.permissions_for(interaction.guild.me)
        if not permissions.send_messages or not permissions.embed_links:
            await interaction.response.send_message(
                "❌ I need **Send Messages** and **Embed Links** in that channel.",
                ephemeral=True,
            )
            return

        await self.db.execute(
            """
            INSERT INTO transaction_log_config (guild_id, channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET channel_id = excluded.channel_id
            """,
            (interaction.guild.id, channel.id),
        )

        await interaction.response.send_message(
            f"✅ Vouch transaction logs are now sent to {channel.mention}.",
            ephemeral=True,
        )

    async def _create_giveaway(self, interaction: discord.Interaction, prize: str, duration: str, winners: int):
        if interaction.guild is None or interaction.channel is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return

        seconds = parse_duration(duration)
        if seconds is None:
            await interaction.response.send_message(
                "❌ Invalid duration. Example: `30m`, `2h`, `1d`.",
                ephemeral=True,
            )
            return

        if winners < 1 or winners > 100:
            await interaction.response.send_message(
                "❌ Winners must be between 1 and 100.",
                ephemeral=True,
            )
            return

        ends_at = now_timestamp() + seconds
        embed = discord.Embed(
            title="Giveaway",
            description=(
                f"**Prize:** {prize}\n"
                f"**Winners:** {winners}\n"
                f"**Ends:** {format_timestamp(ends_at)}\n\n"
                "Click the button below to enter."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Hosted by {interaction.user}")

        await interaction.response.defer(ephemeral=True)
        message = await interaction.channel.send(embed=embed)

        await self.db.execute(
            """
            INSERT INTO giveaway_system (
                message_id,
                channel_id,
                guild_id,
                prize,
                ends_at,
                winners,
                host_id,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
            """,
            (
                message.id,
                interaction.channel.id,
                interaction.guild.id,
                prize,
                ends_at,
                winners,
                interaction.user.id,
            ),
        )

        view = GiveawayJoinView(self, message.id)
        self.add_view(view, message_id=message.id)
        await message.edit(view=view)

        await interaction.followup.send(
            f"✅ Giveaway created: {message.jump_url}",
            ephemeral=True,
        )

    @app_commands.command(name="create", description="Create a giveaway.")
    @app_commands.describe(
        prize="Giveaway prize",
        duration="Duration, e.g. 30m, 2h, 1d",
        winners="Number of winners",
    )
    @owner_only()
    async def giveaway_create(
        self,
        interaction: discord.Interaction,
        prize: str,
        duration: str,
        winners: int,
    ):
        await self._create_giveaway(interaction, prize, duration, winners)

    @app_commands.command(name="end", description="End a giveaway early.")
    @app_commands.describe(message_id="Giveaway message ID")
    @owner_only()
    async def giveaway_end(
        self,
        interaction: discord.Interaction,
        message_id: str,
    ):
        try:
            parsed_id = int(message_id)
        except ValueError:
            await interaction.response.send_message(
                "❌ Invalid giveaway message ID.",
                ephemeral=True,
            )
            return

        row = await self.db.fetchone(
            "SELECT status FROM giveaway_system WHERE message_id = ?",
            (parsed_id,),
        )
        if row is None:
            await interaction.response.send_message(
                "❌ Giveaway not found.",
                ephemeral=True,
            )
            return

        if row[0] != "ACTIVE":
            await interaction.response.send_message(
                "❌ Giveaway is not active.",
                ephemeral=True,
            )
            return

        await self.db.execute(
            "UPDATE giveaway_system SET ends_at = ? WHERE message_id = ?",
            (now_timestamp(), parsed_id),
        )
        await self.finish_giveaway(parsed_id)
        await interaction.response.send_message(
            "✅ Giveaway ended.",
            ephemeral=True,
        )

    # -------------------------
    # Events
    # -------------------------

    async def on_member_join(self, member: discord.Member):
        if not member.bot:
            await self.ensure_trust_user(member.guild.id, member.id)

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if message.guild is not None:
            await self.record_activity(message.guild.id, message.author.id)

        if self.user is not None and self.user in message.mentions:
            content = message.content
            content = content.replace(f"<@{self.user.id}>", "")
            content = content.replace(f"<@!{self.user.id}>", "")
            prompt = content.strip()

            if not prompt:
                await message.reply("Mention me with a question and I’ll answer.")
                return

            answer = await self.ask_ai(prompt)
            await message.reply(answer[:2000])
            return

        await self.process_commands(message)

    async def on_command_error(self, ctx: discord.Message, error: Exception):
        logger.exception("Message command error", exc_info=error)

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            message = "❌ You do not have permission to use this command."
        else:
            logger.exception("Slash command error", exc_info=error)
            message = "❌ Something went wrong."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


# -------------------------
# Trust GUI
# -------------------------

class TrustPanelView(discord.ui.View):
    def __init__(self, bot: GiveawayTrustBot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Check My Vouch",
        style=discord.ButtonStyle.secondary,
        custom_id="trust:check_me",
    )
    async def check_me(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return
        embed = await self.bot.build_profile_embed(interaction.guild, interaction.user)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Check User's Vouch",
        style=discord.ButtonStyle.secondary,
        custom_id="trust:check_user",
    )
    async def check_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select a user or use Enter Name / ID.",
            view=CheckMemberView(self.bot),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Vouch A User",
        style=discord.ButtonStyle.primary,
        custom_id="trust:vouch_user",
    )
    async def vouch_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select the user you want to vouch, or use Enter Name / ID.",
            view=VouchTargetView(self.bot),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Vouch Rewards",
        style=discord.ButtonStyle.secondary,
        custom_id="trust:rewards",
    )
    async def rewards(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Vouch Rewards",
            description=(
                "Trust runs from 0 to 100.\n"
                "Two roles, both automatic:\n\n"
                "• **50** · <@&1529114068412141639>\n"
                "• **100** · <@&1529114203204489277>"
            ),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Vouch Leaderboard",
        style=discord.ButtonStyle.secondary,
        custom_id="trust:leaderboard",
    )
    async def leaderboard(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return

        view = VouchLeaderboardView(self.bot, interaction.guild, page=0)
        embed = await view.build_embed()
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def build_unused(self):
        return None


async def build_profile_embed_method(
    bot: GiveawayTrustBot,
    guild: discord.Guild,
    member: discord.Member,
) -> discord.Embed:
    profile = await bot.get_trust_profile(guild.id, member.id)
    trust_score, vouches_given, positive, negative = profile

    embed = discord.Embed(
        title=f"Vouch Profile — {member.display_name}",
        color=discord.Color.green() if trust_score >= 50 else discord.Color.blurple(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Trust", value=f"**{trust_score}/100**\n{bot.trust_bar(trust_score)}", inline=False)
    embed.add_field(name="Vouches Given", value=str(vouches_given), inline=True)
    embed.add_field(name="+Vouch", value=str(positive), inline=True)
    embed.add_field(name="-Vouch", value=str(negative), inline=True)

    role_text = []
    if trust_score >= 100:
        role_text.append(f"<@&{ROLE_100_ID}>")
        role_text.append(f"<@&{ROLE_50_ID}>")
    elif trust_score >= 50:
        role_text.append(f"<@&{ROLE_50_ID}>")

    embed.add_field(
        name="Reward Roles",
        value=", ".join(role_text) if role_text else "None",
        inline=False,
    )
    return embed


# Bind as an instance method so the panel/view code stays simple.
GiveawayTrustBot.build_profile_embed = build_profile_embed_method


class VouchTargetView(discord.ui.View):
    def __init__(self, bot: GiveawayTrustBot):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.UserSelect(
        placeholder="Select a user",
        min_values=1,
        max_values=1,
    )
    async def user_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return

        target = select.values[0]
        if target.id == interaction.user.id:
            await interaction.response.send_message("❌ You cannot vouch yourself.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Choose **+Vouch** or **-Vouch**.",
            view=VouchTypeView(self.bot, target.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Enter Name / ID",
        style=discord.ButtonStyle.secondary,
    )
    async def enter_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return
        await interaction.response.send_modal(VouchMemberModal(self.bot))


class VouchMemberModal(discord.ui.Modal, title="Find User"):
    user_input = discord.ui.TextInput(
        label="Name / ID",
        placeholder="Username, display name or Discord ID",
        required=True,
        max_length=100,
    )

    def __init__(self, bot: GiveawayTrustBot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return

        target = await self.bot.resolve_member(interaction.guild, str(self.user_input).strip())
        if target is None:
            await interaction.response.send_message("❌ User not found in this server.", ephemeral=True)
            return

        if target.id == interaction.user.id:
            await interaction.response.send_message("❌ You cannot vouch yourself.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Choose **+Vouch** or **-Vouch**.",
            view=VouchTypeView(self.bot, target.id),
            ephemeral=True,
        )


class VouchTypeView(discord.ui.View):
    def __init__(self, bot: GiveawayTrustBot, target_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.target_id = target_id

    @discord.ui.button(label="+Vouch", style=discord.ButtonStyle.success)
    async def positive(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            VouchReasonModal(self.bot, self.target_id, "POSITIVE")
        )

    @discord.ui.button(label="-Vouch", style=discord.ButtonStyle.danger)
    async def negative(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            VouchReasonModal(self.bot, self.target_id, "NEGATIVE")
        )


class VouchReasonModal(discord.ui.Modal):
    def __init__(self, bot: GiveawayTrustBot, target_id: int, vouch_type: str):
        label = "+Vouch" if vouch_type == "POSITIVE" else "-Vouch"
        super().__init__(title=label)
        self.bot = bot
        self.target_id = target_id
        self.vouch_type = vouch_type

        self.reason = discord.ui.TextInput(
            label="Reason",
            placeholder="Reason",
            required=True,
            max_length=200,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await self.bot.record_vouch(
            interaction,
            target_id=self.target_id,
            vouch_type=self.vouch_type,
            reason=str(self.reason).strip(),
        )


class CheckMemberView(discord.ui.View):
    def __init__(self, bot: GiveawayTrustBot):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.UserSelect(
        placeholder="Select a user",
        min_values=1,
        max_values=1,
    )
    async def user_select(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return

        target = select.values[0]
        if not isinstance(target, discord.Member):
            target = interaction.guild.get_member(target.id)

        if target is None:
            await interaction.response.send_message("❌ User not found.", ephemeral=True)
            return

        embed = await self.bot.build_profile_embed(interaction.guild, target)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Enter Name / ID",
        style=discord.ButtonStyle.secondary,
    )
    async def enter_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return
        await interaction.response.send_modal(CheckMemberModal(self.bot))


class CheckMemberModal(discord.ui.Modal, title="Find User"):
    user_input = discord.ui.TextInput(
        label="Name / ID",
        placeholder="Username, display name or Discord ID",
        required=True,
        max_length=100,
    )

    def __init__(self, bot: GiveawayTrustBot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return

        target = await self.bot.resolve_member(interaction.guild, str(self.user_input).strip())
        if target is None:
            await interaction.response.send_message("❌ User not found in this server.", ephemeral=True)
            return

        embed = await self.bot.build_profile_embed(interaction.guild, target)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class VouchLeaderboardView(discord.ui.View):
    PER_PAGE = 100

    def __init__(self, bot: GiveawayTrustBot, guild: discord.Guild, page: int = 0):
        super().__init__(timeout=180)
        self.bot = bot
        self.guild = guild
        self.page = page
        self.total_pages = 1
        self._update_buttons()

    async def get_entries(self):
        await self.bot.ensure_guild_trust_users(self.guild)

        rows = await self.bot.db.fetchall(
            """
            SELECT
                user_id,
                trust_score,
                vouches_given,
                vouch_positive,
                vouch_negative
            FROM user_vouch_network
            WHERE guild_id = ?
            """,
            (self.guild.id,),
        )

        by_user = {
            int(row[0]): {
                "trust": int(row[1]),
                "given": int(row[2]),
                "positive": int(row[3]),
                "negative": int(row[4]),
            }
            for row in rows
        }

        entries = []
        for member in self.guild.members:
            if member.bot:
                continue
            stats = by_user.get(
                member.id,
                {
                    "trust": 25,
                    "given": 0,
                    "positive": 0,
                    "negative": 0,
                },
            )
            entries.append((member, stats))

        entries.sort(
            key=lambda item: (
                -item[1]["trust"],
                -item[1]["given"],
                item[0].display_name.lower(),
                item[0].id,
            )
        )
        return entries

    async def build_embed(self):
        entries = await self.get_entries()
        total = len(entries)
        self.total_pages = max(1, (total + self.PER_PAGE - 1) // self.PER_PAGE)
        self.page = max(0, min(self.page, self.total_pages - 1))

        start = self.page * self.PER_PAGE
        page_entries = entries[start : start + self.PER_PAGE]

        lines = []
        for index, (member, stats) in enumerate(page_entries, start=start + 1):
            lines.append(
                f"#{index} {member.mention} — **{stats['trust']}** Trust"
            )

        description = "\n".join(lines) if lines else "No users found."

        embed = discord.Embed(
            title="Vouch Leaderboard",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text=f"Page {self.page + 1}/{self.total_pages} • {total} users • 100 per page"
        )
        self._update_buttons()
        return embed

    def _update_buttons(self):
        # Buttons are created after __init__ by discord.py decorators.
        if hasattr(self, "previous"):
            self.previous.disabled = self.page <= 0
        if hasattr(self, "next_page"):
            self.next_page.disabled = self.page >= max(0, self.total_pages - 1)

    @discord.ui.button(label="<", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page <= 0:
            await interaction.response.defer()
            return
        self.page -= 1
        embed = await self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label=">", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page >= self.total_pages - 1:
            await interaction.response.defer()
            return
        self.page += 1
        embed = await self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)


# -------------------------
# Giveaway GUI
# -------------------------

class GiveawayJoinView(discord.ui.View):
    def __init__(self, bot: GiveawayTrustBot, message_id: int):
        super().__init__(timeout=None)
        self.bot = bot
        self.message_id = message_id

        button = discord.ui.Button(
            label="Enter Giveaway",
            style=discord.ButtonStyle.success,
            custom_id=f"giveaway:enter:{message_id}",
        )
        button.callback = self.join_callback
        self.add_item(button)

    async def join_callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return

        row = await self.bot.db.fetchone(
            """
            SELECT status, ends_at
            FROM giveaway_system
            WHERE message_id = ?
            """,
            (self.message_id,),
        )

        if row is None:
            await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True)
            return

        status, ends_at = row
        if status != "ACTIVE" or float(ends_at) <= now_timestamp():
            await interaction.response.send_message("❌ This giveaway has ended.", ephemeral=True)
            return

        inserted = await self.bot.db.execute(
            """
            INSERT OR IGNORE INTO giveaway_participants (message_id, user_id)
            VALUES (?, ?)
            """,
            (self.message_id, interaction.user.id),
        )

        if inserted == 0:
            await interaction.response.send_message(
                "❌ You are already entered.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "✅ You are entered in the giveaway!",
            ephemeral=True,
        )


# -------------------------
# Main
# -------------------------

bot = GiveawayTrustBot()

# Register non-group slash commands.
bot.tree.add_command(bot.activity_command)
bot.tree.add_command(bot.botstats_command)
bot.tree.add_command(bot.say_command)
bot.tree.add_command(bot.vouchpanel_command)
bot.tree.add_command(bot.sync_command)
bot.tree.add_command(bot.tempban_command)
bot.tree.add_command(bot.transactionlog_command)


async def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing.")

    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
