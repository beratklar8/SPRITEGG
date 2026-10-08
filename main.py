import asyncio
import io
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
from PIL import Image, ImageDraw, ImageFont

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
else:
    # Render's /data directory only exists when a persistent disk is mounted.
    # The project directory is writable on a normal Render service.
    DATABASE_PATH = os.path.join(os.getcwd(), "bot_database.db")

TRADER_ROLE_ID = 1529114068412141639
TRUSTED_TRADER_ROLE_ID = 1529114203204489277
TRADER_THRESHOLD = 25
TRUSTED_TRADER_THRESHOLD = 50
STARTING_TRUST = 0


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

        # Old versions created new profiles at 25. Reset only untouched legacy
        # default profiles; users who already earned a different score keep it.
        await self.db.execute(
            "UPDATE user_vouch_network SET trust_score = 0 WHERE trust_score = 25 AND vouches_given = 0 AND vouch_positive = 0 AND vouch_negative = 0"
        )

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

            # Give existing non-bot guild members the default 0 Trust profile.
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
            VALUES (?, ?, 0, 0, 0, 0)
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
            VALUES (?, ?, 0, 0, 0, 0)
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

    @staticmethod
    def _font(size: int, bold: bool = False):
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ]
        for path in candidates:
            if os.path.exists(path):
                return ImageFont.truetype(path, size=size)
        return ImageFont.load_default()

    async def generate_leaderboard_image(self, guild: discord.Guild, entries, page: int, total_pages: int) -> discord.File:
        width, height = 1400, 900
        image = Image.new("RGB", (width, height), (15, 17, 24))
        draw = ImageDraw.Draw(image)

        # Header
        draw.rounded_rectangle((40, 35, width - 40, 155), radius=28, fill=(28, 32, 45), outline=(70, 78, 100), width=2)
        draw.text((75, 58), "VOUCH LEADERBOARD", font=self._font(42, True), fill=(245, 247, 250))
        draw.text((78, 112), f"{guild.name}  •  Page {page + 1}/{total_pages}", font=self._font(22), fill=(160, 168, 185))

        rows = entries[page * VouchLeaderboardView.PER_PAGE:(page + 1) * VouchLeaderboardView.PER_PAGE]
        rows = rows[:10]
        y = 185

        avatar_tasks = []
        for member, stats in rows:
            avatar_tasks.append(self._read_avatar(member))
        avatars = await asyncio.gather(*avatar_tasks, return_exceptions=True)

        for idx, ((member, stats), avatar_data) in enumerate(zip(rows, avatars), start=page * VouchLeaderboardView.PER_PAGE + 1):
            row_y = y + (idx - (page * VouchLeaderboardView.PER_PAGE + 1)) * 68
            if idx <= 3:
                fill = (43, 39, 24)
                outline = (173, 145, 58)
            else:
                fill = (24, 27, 37)
                outline = (48, 53, 68)
            draw.rounded_rectangle((45, row_y, width - 45, row_y + 58), radius=18, fill=fill, outline=outline, width=2)

            rank_text = f"#{idx}"
            draw.text((68, row_y + 14), rank_text, font=self._font(23, True), fill=(238, 241, 247))

            if isinstance(avatar_data, bytes):
                try:
                    avatar = Image.open(io.BytesIO(avatar_data)).convert("RGB").resize((42, 42))
                    mask = Image.new("L", (42, 42), 0)
                    ImageDraw.Draw(mask).ellipse((0, 0, 42, 42), fill=255)
                    image.paste(avatar, (135, row_y + 8), mask)
                except Exception:
                    pass

            name = member.display_name[:26]
            draw.text((195, row_y + 8), name, font=self._font(21, True), fill=(245, 247, 250))
            draw.text((195, row_y + 34), f"{stats['given']} vouches given", font=self._font(16), fill=(145, 153, 170))
            draw.text((width - 255, row_y + 12), f"{stats['trust']}", font=self._font(28, True), fill=(100, 210, 160))
            draw.text((width - 150, row_y + 19), "TRUST", font=self._font(14, True), fill=(150, 158, 175))

        if not rows:
            draw.text((75, 220), "No users found.", font=self._font(26), fill=(170, 178, 194))

        draw.text((75, height - 52), "Level 0: Trade Banned  •  1–24: No role  •  25–49: Trader  •  50–100: Trusted Trader", font=self._font(18), fill=(125, 133, 150))

        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        buffer.seek(0)
        return discord.File(buffer, filename="vouch-leaderboard.png")

    async def _read_avatar(self, member: discord.Member):
        try:
            return await member.display_avatar.read()
        except Exception:
            return None

    async def update_vouch_roles(self, guild: discord.Guild, user_id: int, trust_score: int):
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

        trader_role = guild.get_role(TRADER_ROLE_ID)
        trusted_role = guild.get_role(TRUSTED_TRADER_ROLE_ID)

        try:
            if trusted_role is not None:
                if trust_score >= TRUSTED_TRADER_THRESHOLD:
                    if trusted_role not in member.roles:
                        await member.add_roles(trusted_role, reason="Vouch level reached 50")
                elif trusted_role in member.roles:
                    await member.remove_roles(trusted_role, reason="Vouch level fell below 50")

            if trader_role is not None:
                if TRADER_THRESHOLD <= trust_score < TRUSTED_TRADER_THRESHOLD:
                    if trader_role not in member.roles:
                        await member.add_roles(trader_role, reason="Vouch level reached 25")
                elif trader_role in member.roles:
                    await member.remove_roles(trader_role, reason="Vouch level is no longer in Trader range")
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

    async def process_vouch(
        self,
        guild: discord.Guild,
        giver: discord.abc.User,
        target_id: int,
        vouch_type: str,
        reason: str,
    ):
        reason = reason.strip()
        if not reason:
            return {"ok": False, "message": "❌ Reason is required."}

        if len(reason) > 200:
            return {"ok": False, "message": "❌ Reason is too long. Keep it short (max 200 characters)."}

        giver_id = giver.id
        if target_id == giver_id:
            return {"ok": False, "message": "❌ You cannot vouch yourself."}

        target = guild.get_member(target_id)
        if target is None:
            try:
                target = await guild.fetch_member(target_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                target = None

        if target is None:
            return {"ok": False, "message": "❌ That user is no longer in this server."}

        if target.bot:
            return {"ok": False, "message": "❌ You cannot vouch a bot."}

        if vouch_type not in {"POSITIVE", "NEGATIVE"}:
            return {"ok": False, "message": "❌ Invalid vouch type."}

        delta = 1 if vouch_type == "POSITIVE" else -1
        timestamp = now_timestamp()

        try:
            async with self.db.transaction() as connection:
                await connection.execute(
                    """
                    INSERT OR IGNORE INTO user_vouch_network (
                        guild_id, user_id, trust_score, vouches_given, vouch_positive, vouch_negative
                    ) VALUES (?, ?, 0, 0, 0, 0)
                    """,
                    (guild.id, target_id),
                )
                await connection.execute(
                    """
                    INSERT OR IGNORE INTO user_vouch_network (
                        guild_id, user_id, trust_score, vouches_given, vouch_positive, vouch_negative
                    ) VALUES (?, ?, 0, 0, 0, 0)
                    """,
                    (guild.id, giver_id),
                )

                async with connection.execute(
                    "SELECT trust_score FROM user_vouch_network WHERE guild_id = ? AND user_id = ?",
                    (guild.id, target_id),
                ) as cursor:
                    row = await cursor.fetchone()

                old_score = int(row[0]) if row else STARTING_TRUST

                cursor = await connection.execute(
                    """
                    INSERT OR IGNORE INTO vouch_history (
                        guild_id, target_id, giver_id, vouch_type, reason, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (guild.id, target_id, giver_id, vouch_type, reason, timestamp),
                )

                if cursor.rowcount == 0:
                    return {"ok": False, "message": "❌ You already vouched this user. You can only vouch someone once."}

                new_score = clamp(old_score + delta)

                await connection.execute(
                    "UPDATE user_vouch_network SET trust_score = ? WHERE guild_id = ? AND user_id = ?",
                    (new_score, guild.id, target_id),
                )

                if vouch_type == "POSITIVE":
                    await connection.execute(
                        """
                        UPDATE user_vouch_network
                        SET vouches_given = vouches_given + 1, vouch_positive = vouch_positive + 1
                        WHERE guild_id = ? AND user_id = ?
                        """,
                        (guild.id, giver_id),
                    )
                else:
                    await connection.execute(
                        """
                        UPDATE user_vouch_network
                        SET vouches_given = vouches_given + 1, vouch_negative = vouch_negative + 1
                        WHERE guild_id = ? AND user_id = ?
                        """,
                        (guild.id, giver_id),
                    )
        except aiosqlite.Error:
            logger.exception("Failed to save vouch transaction.")
            return {"ok": False, "message": "❌ The vouch could not be saved. Try again."}

        await self.update_vouch_roles(guild, target_id, new_score)
        await self.send_transaction_log(
            guild=guild, giver=giver, target=target, vouch_type=vouch_type, reason=reason,
            old_score=old_score, new_score=new_score, timestamp=timestamp,
        )

        return {
            "ok": True,
            "target": target,
            "old_score": old_score,
            "new_score": new_score,
            "timestamp": timestamp,
        }

    async def record_vouch(self, interaction: discord.Interaction, target_id: int, vouch_type: str, reason: str):
        if interaction.guild is None:
            await interaction.response.send_message("❌ This can only be used inside a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        result = await self.process_vouch(interaction.guild, interaction.user, target_id, vouch_type, reason)
        if not result["ok"]:
            await interaction.followup.send(result["message"], ephemeral=True)
            return

        label = "+Vouch" if vouch_type == "POSITIVE" else "-Vouch"
        delta = 1 if vouch_type == "POSITIVE" else -1
        embed = discord.Embed(
            title=f"{label} recorded",
            description=(
                f"{result['target'].mention} is now at **{result['new_score']}/100 Trust**.\n\n"
                f"**Reason:** {reason.strip()}"
            ),
            color=discord.Color.green() if delta > 0 else discord.Color.red(),
        )
        embed.set_footer(text=f"Trust: {self.trust_bar(result['new_score'])}")
        await interaction.followup.send(embed=embed, ephemeral=True)

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

        await interaction.response.defer(ephemeral=True)

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
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="botstats", description="Show bot statistics.")
    @owner_only()
    async def botstats_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
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
        await interaction.followup.send(embed=embed, ephemeral=True)

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
            "Build a trustworthy trading history with real, honest vouches. Everyone starts at **Level 0**.\n\n"
            "**How the vouch form works**\n"
            "**1.** Choose the trader you actually dealt with.\n"
            "**2.** Pick **+Vouch** or **-Vouch**.\n"
            "**3.** Give a short, specific reason.\n"
            "**4.** Review everything and confirm.\n\n"
            "**Level milestones**\n"
            "⛔ **0** · Trade Banned status\n"
            "🔹 **1–24** · No role\n"
            "🟢 **25–49** · Trader\n"
            "👑 **50–100** · Trusted Trader\n\n"
            "Only vouch for trades that really happened. Do not submit passwords, payment details, tokens, or other sensitive information. Fake, spam, random, or revenge vouches may be reviewed by staff."
        )

        embed = discord.Embed(
            title="Trader Vouch System",
            description=description,
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Quick actions",
            value=(
                "👤 Check your own vouch\n"
                "🔎 Inspect another trader\n"
                "✅ Submit a vouch\n"
                "🏆 See rewards\n"
                "📊 Open the leaderboard"
            ),
            inline=False,
        )
        embed.set_footer(text="Vouch Bot · Real trades only")

        try:
            await interaction.response.send_message(
                "⏳ Posting the Vouch panel...",
                ephemeral=True,
            )
        except Exception:
            logger.exception("Failed to send initial /vouchpanel response")
            return

        try:
            channel = interaction.channel
            if channel is None or not hasattr(channel, "send"):
                await interaction.edit_original_response(
                    content="❌ I cannot send the Vouch panel in this channel."
                )
                return

            await channel.send(
                embed=embed,
                view=TrustPanelView(self),
                allowed_mentions=discord.AllowedMentions.none(),
            )

            await interaction.edit_original_response(
                content="✅ **Vouch panel posted.**"
            )
        except Exception as exc:
            logger.exception("Failed while posting /vouchpanel")
            try:
                await interaction.edit_original_response(
                    content=(
                        "❌ I could not post the Vouch panel. "
                        f"Check the bot's channel permissions. (`{type(exc).__name__}`)"
                    )
                )
            except Exception:
                logger.exception("Failed to update /vouchpanel error response")

    @app_commands.command(name="sync", description="Sync slash commands.")
    @owner_only()
    async def sync_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        synced = await self.tree.sync()
        await interaction.followup.send(
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

        await interaction.response.defer(ephemeral=True)

        try:
            await interaction.guild.ban(
                user,
                reason=f"Temporary ban by {interaction.user}",
                delete_message_seconds=0,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ I cannot ban that user. Check my Ban Members permission and role hierarchy.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.followup.send(
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

        await interaction.followup.send(
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

        await interaction.response.defer(ephemeral=True)

        permissions = channel.permissions_for(interaction.guild.me)
        if not permissions.send_messages or not permissions.embed_links:
            await interaction.followup.send(
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

        await interaction.followup.send(
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

        # FIXED: Zorg ervoor dat Discord direct wordt gedefereerd zodat de 3-seconden timeout niet optreedt
        await interaction.response.defer(ephemeral=True)

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
        await interaction.response.defer(ephemeral=True)
        try:
            parsed_id = int(message_id)
        except ValueError:
            await interaction.followup.send(
                "❌ Invalid giveaway message ID.",
                ephemeral=True,
            )
            return

        row = await self.db.fetchone(
            "SELECT status FROM giveaway_system WHERE message_id = ?",
            (parsed_id,),
        )
        if row is None:
            await interaction.followup.send(
                "❌ Giveaway not found.",
                ephemeral=True,
            )
            return

        if row[0] != "ACTIVE":
            await interaction.followup.send(
                "❌ Giveaway is not active.",
                ephemeral=True,
            )
            return

        await self.db.execute(
            "UPDATE giveaway_system SET ends_at = ? WHERE message_id = ?",
            (now_timestamp(), parsed_id),
        )
        await self.finish_giveaway(parsed_id)
        await interaction.followup.send(
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
        logger.error(
            "Slash command error in /%s (%s)",
            getattr(getattr(interaction, "command", None), "qualified_name", "unknown"),
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )

        if isinstance(error, app_commands.CheckFailure):
            message = "❌ You do not have permission to use this command."
        else:
            message = (
                "❌ Something went wrong while running this command. "
                "The error has been logged."
            )

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            logger.exception("Could not send slash-command error response")


# -------------------------
# Trust / Vouch GUI
# -------------------------


def vouch_level_name(score: int) -> str:
    score = int(score)
    if score >= TRUSTED_TRADER_THRESHOLD:
        return "Trusted Trader"
    if score >= TRADER_THRESHOLD:
        return "Trader"
    if score == 0:
        return "Trade Banned"
    return "No role yet"


def vouch_level_description(score: int) -> str:
    score = int(score)
    if score >= TRUSTED_TRADER_THRESHOLD:
        return "Trusted Trader status unlocked."
    if score >= TRADER_THRESHOLD:
        return "Trader status unlocked."
    if score == 0:
        return "Level 0 status: Trade Banned."
    return "Build your reputation through real trades."


async def build_profile_embed_method(
    bot: GiveawayTrustBot,
    guild: discord.Guild,
    member: discord.Member,
) -> discord.Embed:
    profile = await bot.get_trust_profile(guild.id, member.id)
    trust_score, vouches_given, positive, negative = map(int, profile)

    if trust_score >= TRUSTED_TRADER_THRESHOLD:
        level_name = "Trusted Trader"
        color = discord.Color.gold()
        level_icon = "👑"
    elif trust_score >= TRADER_THRESHOLD:
        level_name = "Trader"
        color = discord.Color.green()
        level_icon = "🟢"
    elif trust_score == 0:
        level_name = "Trade Banned"
        color = discord.Color.red()
        level_icon = "⛔"
    else:
        level_name = "No role yet"
        color = discord.Color.blurple()
        level_icon = "🔹"

    history = await bot.db.fetchall(
        """
        SELECT giver_id, vouch_type, reason, timestamp
        FROM vouch_history
        WHERE guild_id = ? AND target_id = ?
        ORDER BY timestamp DESC
        LIMIT 5
        """,
        (guild.id, member.id),
    )

    embed = discord.Embed(
        title=f"Vouch Profile · {member.display_name}",
        description=(
            f"{level_icon} **{level_name}**\n"
            f"{vouch_level_description(trust_score)}"
        ),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(
        name="Vouch Level",
        value=f"**{trust_score}/100**\n`{bot.trust_bar(trust_score)}`",
        inline=False,
    )

    next_target = None
    if trust_score < TRADER_THRESHOLD:
        next_target = TRADER_THRESHOLD
    elif trust_score < TRUSTED_TRADER_THRESHOLD:
        next_target = TRUSTED_TRADER_THRESHOLD

    if next_target is not None:
        embed.add_field(
            name="Next Milestone",
            value=f"`{next_target - trust_score}` level(s) to **{vouch_level_name(next_target)}**",
            inline=False,
        )

    embed.add_field(
        name="+Vouch received",
        value=f"`{positive}`",
        inline=True,
    )
    embed.add_field(
        name="-Vouch received",
        value=f"`{negative}`",
        inline=True,
    )
    embed.add_field(
        name="Vouches given",
        value=f"`{vouches_given}`",
        inline=True,
    )

    if history:
        history_lines = []
        for giver_id, vouch_type, reason, timestamp in history:
            giver = guild.get_member(int(giver_id))
            giver_name = giver.display_name if giver else f"User {giver_id}"
            sign = "+" if vouch_type == "POSITIVE" else "-"
            safe_name = discord.utils.escape_markdown(giver_name)
            safe_reason = discord.utils.escape_markdown(reason or "No reason provided.")
            history_lines.append(
                f"**{sign} {safe_name}** — {safe_reason[:180]}\n"
                f"<t:{int(float(timestamp))}:R>"
            )

        history_value = "\n\n".join(history_lines)
    else:
        history_value = "No vouch history yet."

    embed.add_field(
        name="Vouch History",
        value=history_value[:1024],
        inline=False,
    )

    embed.set_footer(
        text="Only real trade experiences should be vouched."
    )
    return embed


GiveawayTrustBot.build_profile_embed = build_profile_embed_method


class TrustPanelView(discord.ui.View):
    def __init__(self, bot: GiveawayTrustBot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Check My Vouch",
        emoji="👤",
        style=discord.ButtonStyle.primary,
        custom_id="trust:check_me",
    )
    async def check_me(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This panel only works inside a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        embed = await self.bot.build_profile_embed(
            interaction.guild,
            interaction.user,
        )
        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Check User's Vouch",
        emoji="🔎",
        style=discord.ButtonStyle.secondary,
        custom_id="trust:check_user",
    )
    async def check_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This panel only works inside a server.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Check User's Vouch",
            description=(
                "Choose a member from the selector below, or search by name / ID.\n\n"
                "You can view their current level, received vouches and recent vouch history."
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=CheckMemberView(self.bot),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Vouch A User",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="trust:vouch_user",
    )
    async def vouch_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This panel only works inside a server.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Vouch A User",
            description=(
                "Complete the vouch form in a few steps.\n\n"
                "**1.** Select the person you traded with.\n"
                "**2.** Pick **+Vouch** or **-Vouch**.\n"
                "**3.** Explain what happened.\n"
                "**4.** Review everything and confirm.\n\n"
                "Only vouch for trades that actually happened. Do not include passwords, payment details, tokens, or other sensitive information."
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text="Nothing is saved until you press Confirm Vouch.")

        await interaction.response.send_message(
            embed=embed,
            view=VouchTargetView(self.bot),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Vouch Rewards",
        emoji="🏆",
        style=discord.ButtonStyle.secondary,
        custom_id="trust:rewards",
    )
    async def rewards(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Vouch Rewards",
            description=(
                "Your vouch level is based on the vouches you receive. "
                "Positive vouches add 1 level and negative vouches remove 1 level, with a floor of 0 and a cap of 100."
            ),
            color=discord.Color.gold(),
        )

        embed.add_field(
            name="Level 0",
            value="⛔ **Trade Banned**\nStatus level only; this does not automatically ban a Discord account.",
            inline=False,
        )
        embed.add_field(
            name="Levels 1–24",
            value="🔹 **No role**\nKeep trading and build a strong vouch history.",
            inline=False,
        )
        embed.add_field(
            name="Levels 25–49",
            value="🟢 **Trader**\nTrader role milestone unlocked.",
            inline=False,
        )
        embed.add_field(
            name="Levels 50–100",
            value="👑 **Trusted Trader**\nTrusted Trader role milestone unlocked.",
            inline=False,
        )
        embed.set_footer(text="Real trades only. Fake or revenge vouches may be reviewed by staff.")

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Vouch Leaderboard",
        emoji="📊",
        style=discord.ButtonStyle.secondary,
        custom_id="trust:leaderboard",
    )
    async def leaderboard(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This panel only works inside a server.",
                ephemeral=True,
            )
            return

        # FIXED: De leaderboard maakt een afbeelding via Pillow (wat langer kan duren). 
        # Door direct te deferren voorkomen we een "Application did not respond" timeout.
        await interaction.response.defer(ephemeral=True)
        view = VouchLeaderboardView(
            self.bot,
            interaction.guild,
            page=0,
        )
        embed, file = await view.build_message()
        await interaction.followup.send(
            embed=embed,
            file=file,
            view=view,
            ephemeral=True,
        )


class VouchTargetView(discord.ui.View):
    def __init__(self, bot: GiveawayTrustBot):
        super().__init__(timeout=180)
        self.bot = bot
        self.user_select = discord.ui.UserSelect(
            placeholder="Select the person you traded with...",
            min_values=1,
            max_values=1,
        )
        self.user_select.callback = self.user_select_callback
        self.add_item(self.user_select)

    async def user_select_callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This action only works inside a server.",
                ephemeral=True,
            )
            return

        target = self.user_select.values[0]
        if not isinstance(target, discord.Member):
            target = interaction.guild.get_member(target.id)

        if target is None:
            await interaction.response.send_message(
                "❌ I could not find that member in this server.",
                ephemeral=True,
            )
            return

        if target.bot:
            await interaction.response.send_message(
                "❌ Bots cannot receive vouches.",
                ephemeral=True,
            )
            return

        if target.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ You cannot vouch yourself.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Vouch A User · Step 2 of 4",
            description=(
                f"You selected {target.mention}.\n\n"
                "Now choose whether this was a positive or negative trade experience."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(
            embed=embed,
            view=VouchTypeView(self.bot, target.id),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Search by Name / ID",
        emoji="🔍",
        style=discord.ButtonStyle.secondary,
    )
    async def enter_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This action only works inside a server.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(VouchMemberModal(self.bot))


class VouchMemberModal(discord.ui.Modal, title="Find User"):
    user_input = discord.ui.TextInput(
        label="Who are you vouching for?",
        placeholder="Username, display name, mention or Discord ID",
        required=True,
        max_length=100,
    )

    def __init__(self, bot: GiveawayTrustBot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This form only works inside a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        target = await self.bot.resolve_member(
            interaction.guild,
            str(self.user_input.value).strip(),
        )

        if target is None:
            await interaction.followup.send(
                "❌ I could not find that user. Try their exact name or Discord ID.",
                ephemeral=True,
            )
            return

        if target.bot:
            await interaction.followup.send(
                "❌ Bots cannot receive vouches.",
                ephemeral=True,
            )
            return

        if target.id == interaction.user.id:
            await interaction.followup.send(
                "❌ You cannot vouch yourself.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Vouch A User · Step 2 of 4",
            description=(
                f"You selected {target.mention}.\n\n"
                "Now choose the type of vouch you want to give."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.followup.send(
            embed=embed,
            view=VouchTypeView(self.bot, target.id),
            ephemeral=True,
        )


class VouchTypeView(discord.ui.View):
    def __init__(self, bot: GiveawayTrustBot, target_id: int):
        super().__init__(timeout=180)
        self.bot = bot
        self.target_id = target_id

    @discord.ui.button(
        label="+Vouch",
        emoji="✅",
        style=discord.ButtonStyle.success,
    )
    async def positive(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            VouchReasonModal(
                self.bot,
                self.target_id,
                "POSITIVE",
            )
        )

    @discord.ui.button(
        label="-Vouch",
        emoji="⚠️",
        style=discord.ButtonStyle.danger,
    )
    async def negative(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            VouchReasonModal(
                self.bot,
                self.target_id,
                "NEGATIVE",
            )
        )


class VouchReasonModal(discord.ui.Modal):
    def __init__(self, bot: GiveawayTrustBot, target_id: int, vouch_type: str):
        title = "+Vouch Reason" if vouch_type == "POSITIVE" else "-Vouch Reason"
        super().__init__(title=title)
        self.bot = bot
        self.target_id = target_id
        self.vouch_type = vouch_type

        self.reason = discord.ui.TextInput(
            label="Why are you giving this vouch?",
            placeholder=(
                "Example: Trade was completed quickly, they sent first, and everything matched the agreement."
            ),
            required=True,
            min_length=3,
            max_length=200,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This form only works inside a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        target = interaction.guild.get_member(self.target_id)
        if target is None:
            try:
                target = await interaction.guild.fetch_member(self.target_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                target = None

        if target is None:
            await interaction.followup.send(
                "❌ That user is no longer in this server.",
                ephemeral=True,
            )
            return

        reason = str(self.reason.value).strip()
        label = "+Vouch" if self.vouch_type == "POSITIVE" else "-Vouch"
        color = discord.Color.green() if self.vouch_type == "POSITIVE" else discord.Color.red()

        embed = discord.Embed(
            title="Confirm Vouch · Step 4 of 4",
            description=(
                "Please check the details below. Nothing is stored until you confirm."
            ),
            color=color,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Who", value=target.mention, inline=False)
        embed.add_field(name="Vouch type", value=label, inline=True)
        embed.add_field(name="Current level", value=f"`{(await self.bot.get_trust_profile(interaction.guild.id, target.id))[0]}/100`", inline=True)
        embed.add_field(
            name="Why",
            value=discord.utils.escape_markdown(reason),
            inline=False,
        )
        embed.add_field(
            name="Reminder",
            value=(
                "Only use this for a real trade. Do not post passwords, payment details, tokens, or other private information."
            ),
            inline=False,
        )
        embed.set_footer(text="Confirm Vouch saves it to the database. Cancel discards it.")

        await interaction.followup.send(
            embed=embed,
            view=VouchConfirmView(
                self.bot,
                target.id,
                self.vouch_type,
                reason,
                interaction.user.id,
            ),
            ephemeral=True,
        )


class VouchConfirmView(discord.ui.View):
    def __init__(self, bot: GiveawayTrustBot, target_id: int, vouch_type: str, reason: str, giver_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.target_id = target_id
        self.vouch_type = vouch_type
        self.reason = reason
        self.giver_id = giver_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.giver_id:
            await interaction.response.send_message(
                "❌ This confirmation belongs to another user.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Confirm Vouch",
        emoji="✅",
        style=discord.ButtonStyle.success,
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.edit_message(
                content="❌ This action only works inside a server.",
                embed=None,
                view=None,
            )
            return

        await interaction.response.defer()
        result = await self.bot.process_vouch(
            interaction.guild,
            interaction.user,
            self.target_id,
            self.vouch_type,
            self.reason,
        )

        for item in self.children:
            item.disabled = True

        if not result["ok"]:
            await interaction.edit_original_response(
                content=result["message"],
                embed=None,
                view=self,
            )
            return

        label = "+Vouch" if self.vouch_type == "POSITIVE" else "-Vouch"
        delta_text = "+1 level" if self.vouch_type == "POSITIVE" else "-1 level"
        new_score = result["new_score"]

        embed = discord.Embed(
            title=f"{label} submitted",
            description=(
                f"{result['target'].mention} now has **{new_score}/100** vouch level.\n\n"
                f"**Change:** `{delta_text}`\n"
                f"**Reason:** {discord.utils.escape_markdown(self.reason)}"
            ),
            color=discord.Color.green() if self.vouch_type == "POSITIVE" else discord.Color.red(),
        )
        embed.add_field(
            name="New status",
            value=vouch_level_name(new_score),
            inline=True,
        )
        embed.add_field(
            name="Progress",
            value=f"`{self.bot.trust_bar(new_score)}`",
            inline=True,
        )
        embed.set_footer(text="The vouch has been saved successfully.")

        await interaction.edit_original_response(
            content=None,
            embed=embed,
            view=self,
        )

    @discord.ui.button(
        label="Cancel",
        emoji="✖️",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.giver_id:
            await interaction.response.send_message(
                "❌ This confirmation belongs to another user.",
                ephemeral=True,
            )
            return

        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            content="❌ Vouch cancelled. Nothing was saved.",
            embed=None,
            view=self,
        )


class CheckMemberView(discord.ui.View):
    def __init__(self, bot: GiveawayTrustBot):
        super().__init__(timeout=180)
        self.bot = bot
        self.user_select = discord.ui.UserSelect(
            placeholder="Select a user to inspect...",
            min_values=1,
            max_values=1,
        )
        self.user_select.callback = self.user_select_callback
        self.add_item(self.user_select)

    async def user_select_callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(z
                "❌ This action only works inside a server.",
                ephemeral=True,
            )
            return

        target = self.user_select.values[0]
        if not isinstance(target, discord.Member):
            target = interaction.guild.get_member(target.id)

        if target is None:
            await interaction.response.send_message(
                "❌ I could not find that user.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        embed = await self.bot.build_profile_embed(
            interaction.guild,
            target,
        )
        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Search by Name / ID",
        emoji="🔍",
        style=discord.ButtonStyle.secondary,
    )
    async def enter_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This action only works inside a server.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(CheckMemberModal(self.bot))


class CheckMemberModal(discord.ui.Modal, title="Check User"):
    user_input = discord.ui.TextInput(
        label="Who do you want to check?",
        placeholder="Username, display name, mention or Discord ID",
        required=True,
        max_length=100,
    )

    def __init__(self, bot: GiveawayTrustBot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ This form only works inside a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        target = await self.bot.resolve_member(
            interaction.guild,
            str(self.user_input.value).strip(),
        )

        if target is None:
            await interaction.followup.send(
                "❌ I could not find that user. Try their exact name or Discord ID.",
                ephemeral=True,
            )
            return

        embed = await self.bot.build_profile_embed(
            interaction.guild,
            target,
        )
        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )


class VouchLeaderboardView(discord.ui.View):
    PER_PAGE = 10

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
            SELECT user_id, trust_score, vouches_given, vouch_positive, vouch_negative
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
                {"trust": 0, "given": 0, "positive": 0, "negative": 0},
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

    async def build_message(self):
        entries = await self.get_entries()
        total = len(entries)
        self.total_pages = max(
            1,
            (total + self.PER_PAGE - 1) // self.PER_PAGE,
        )
        self.page = max(
            0,
            min(self.page, self.total_pages - 1),
        )

        file = await self.bot.generate_leaderboard_image(
            self.guild,
            entries,
            self.page,
            self.total_pages,
        )

        embed = discord.Embed(
            title="Vouch Leaderboard",
            description=(
                "Top traders in this server, ranked by current vouch level."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_image(url="attachment://vouch-leaderboard.png")
        embed.set_footer(
            text=f"Page {self.page + 1}/{self.total_pages} · {total} users"
        )
        self._update_buttons()
        return embed, file

    def _update_buttons(self):
        if hasattr(self, "previous"):
            self.previous.disabled = self.page <= 0
        if hasattr(self, "next_page"):
            self.next_page.disabled = (
                self.page >= max(0, self.total_pages - 1)
            )

    @discord.ui.button(
        label="Back",
        emoji="◀️",
        style=discord.ButtonStyle.secondary,
    )
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.page = max(0, self.page - 1)
        embed, file = await self.build_message()
        await interaction.edit_original_response(
            embed=embed,
            attachments=[file],
            view=self,
        )

    @discord.ui.button(
        label="Next",
        emoji="▶️",
        style=discord.ButtonStyle.primary,
    )
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.page = min(
            self.total_pages - 1,
            self.page + 1,
        )
        embed, file = await self.build_message()
        await interaction.edit_original_response(
            embed=embed,
            attachments=[file],
            view=self,
        )


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

        await interaction.response.defer(ephemeral=True)

        row = await self.bot.db.fetchone(
            """
            SELECT status, ends_at
            FROM giveaway_system
            WHERE message_id = ?
            """,
            (self.message_id,),
        )

        if row is None:
            await interaction.followup.send("❌ Giveaway not found.", ephemeral=True)
            return

        status, ends_at = row
        if status != "ACTIVE" or float(ends_at) <= now_timestamp():
            await interaction.followup.send("❌ This giveaway has ended.", ephemeral=True)
            return

        inserted = await self.bot.db.execute(
            """
            INSERT OR IGNORE INTO giveaway_participants (message_id, user_id)
            VALUES (?, ?)
            """,
            (self.message_id, interaction.user.id),
        )

        if inserted == 0:
            await interaction.followup.send(
                "❌ You are already entered.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "✅ You are entered in the giveaway!",
            ephemeral=True,
        )


# -------------------------
# Main
# -------------------------

bot = GiveawayTrustBot()

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
