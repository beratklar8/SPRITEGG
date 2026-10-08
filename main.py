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
logger = logging.getLogger("vouch-bot")


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0") or "0")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
PORT = int(os.getenv("PORT", "10000"))

DATABASE_PATH = os.getenv("DATABASE_PATH", "").strip()

if not DATABASE_PATH:
    DATABASE_PATH = os.path.join(
        os.getcwd(),
        "bot_database.db",
    )


TRADER_ROLE_ID = 1529114068412141639
TRUSTED_TRADER_ROLE_ID = 1529114203204489277

TRADER_THRESHOLD = 25
TRUSTED_TRADER_THRESHOLD = 50
STARTING_TRUST = 0


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True


# ============================================================
# HELPERS
# ============================================================

def now_ts() -> float:
    return time.time()


def discord_time(timestamp: float) -> str:
    return discord.utils.format_dt(
        datetime.fromtimestamp(
            timestamp,
            tz=timezone.utc,
        ),
        style="F",
    )


def clamp(
    value: int,
    low: int = 0,
    high: int = 100,
) -> int:
    return max(
        low,
        min(high, int(value)),
    )


def parse_duration(
    value: str,
) -> Optional[int]:
    value = value.strip().lower()

    match = re.fullmatch(
        r"(\d+)\s*([smhd]?)",
        value,
    )

    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)

    multiplier = {
        "": 60,
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
    async def predicate(
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id == BOT_OWNER_ID:
            return True

        if (
            interaction.guild
            and interaction.user.id
            == interaction.guild.owner_id
        ):
            return True

        raise app_commands.CheckFailure(
            "Owner only"
        )

    return app_commands.check(
        predicate
    )


# ============================================================
# SAFE UI
# ============================================================

class SafeView(discord.ui.View):

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:

        logger.exception(
            "UI callback failed: %s",
            type(item).__name__,
            exc_info=error,
        )

        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ Something went wrong. Please try again.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "❌ Something went wrong. Please try again.",
                    ephemeral=True,
                )
        except Exception:
            logger.exception(
                "Could not send UI error response."
            )


class SafeModal(discord.ui.Modal):

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:

        logger.exception(
            "Modal callback failed.",
            exc_info=error,
        )

        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ Something went wrong. Please try again.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "❌ Something went wrong. Please try again.",
                    ephemeral=True,
                )
        except Exception:
            logger.exception(
                "Could not send modal error response."
            )


# ============================================================
# BOT
# ============================================================

class VouchBot(discord.Client):

    def __init__(self):
        super().__init__(
            intents=intents
        )

        self.tree = app_commands.CommandTree(
            self
        )

        self.db = DatabaseController(
            DATABASE_PATH
        )

        self.groq = (
            Groq(
                api_key=GROQ_API_KEY
            )
            if GROQ_API_KEY
            else None
        )

        self.health_runner = None
        self.health_site = None

        self.ready_once = False

        self.giveaway_group = (
            app_commands.Group(
                name="giveaway",
                description="Giveaway commands",
            )
        )

    # ========================================================
    # LIFECYCLE
    # ========================================================

    async def setup_hook(self):

        await self.db.initialize_database()

        # Oude versie gebruikte 25 als default.
        # Alleen untouched rows gaan terug naar 0.
        await self.db.execute(
            """
            UPDATE user_vouch_network
            SET trust_score = 0
            WHERE trust_score = 25
              AND vouches_given = 0
              AND vouch_positive = 0
              AND vouch_negative = 0
            """
        )

        # Persistente panel.
        self.add_view(
            TrustPanelView(self)
        )

        # Recover giveaways.
        await self.db.execute(
            """
            UPDATE giveaway_system
            SET
                status = 'ACTIVE',
                processing_started_at = 0
            WHERE status = 'PROCESSING'
            """
        )

        giveaway_rows = await self.db.fetchall(
            """
            SELECT message_id
            FROM giveaway_system
            WHERE status = 'ACTIVE'
            """
        )

        for row in giveaway_rows:
            message_id = int(row[0])

            self.add_view(
                GiveawayJoinView(
                    self,
                    message_id,
                ),
                message_id=message_id,
            )

        try:
            self.giveaway_group.add_command(
                self.giveaway_create
            )
        except app_commands.CommandAlreadyRegistered:
            pass

        try:
            self.giveaway_group.add_command(
                self.giveaway_end
            )
        except app_commands.CommandAlreadyRegistered:
            pass

        if (
            self.giveaway_group
            not in self.tree.get_commands()
        ):
            self.tree.add_command(
                self.giveaway_group
            )

        await self.tree.sync()

        if not self.giveaway_loop.is_running():
            self.giveaway_loop.start()

        if not self.temp_ban_loop.is_running():
            self.temp_ban_loop.start()

        await self.start_health_server()

    async def on_ready(
        self,
    ):

        if not self.ready_once:

            self.ready_once = True

            logger.info(
                "Logged in as %s (%s)",
                self.user,
                self.user.id
                if self.user
                else "?",
            )

            for guild in self.guilds:
                try:
                    await self.ensure_guild_trust_users(
                        guild
                    )
                except Exception:
                    logger.exception(
                        "Failed to initialize Trust users for guild %s",
                        guild.id,
                    )

        logger.info(
            "Connected to %d guild(s).",
            len(self.guilds),
        )

    async def close(
        self,
    ):

        for loop in (
            self.giveaway_loop,
            self.temp_ban_loop,
        ):
            if loop.is_running():
                loop.cancel()

        if self.health_runner is not None:
            try:
                await self.health_runner.cleanup()
            except Exception:
                logger.exception(
                    "Health server cleanup failed."
                )

            self.health_runner = None
            self.health_site = None

        await self.db.close()

        await super().close()

    # ========================================================
    # HEALTH SERVER
    # ========================================================

    async def start_health_server(
        self,
    ):

        if self.health_runner is not None:
            return

        app = web.Application()

        app.router.add_get(
            "/health",
            self.health,
        )

        app.router.add_get(
            "/api/status",
            self.status,
        )

        self.health_runner = web.AppRunner(
            app
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

    async def health(
        self,
        request: web.Request,
    ) -> web.Response:

        return web.json_response(
            {
                "status": "ok",
                "guilds": len(self.guilds),
            }
        )

    async def status(
        self,
        request: web.Request,
    ) -> web.Response:

        return web.json_response(
            {
                "status": "online",
                "guilds": len(self.guilds),
                "latency_ms": round(
                    self.latency * 1000,
                    2,
                ),
                "database":
                    self.db.connection
                    is not None,
            }
        )

    # ========================================================
    # TRUST
    # ========================================================

    async def ensure_trust_user(
        self,
        guild_id: int,
        user_id: int,
    ):

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
            (
                guild_id,
                user_id,
            ),
        )

    async def ensure_guild_trust_users(
        self,
        guild: discord.Guild,
    ):

        members = [
            member
            for member in guild.members
            if not member.bot
        ]

        if not members:
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
            [
                (
                    guild.id,
                    member.id,
                )
                for member in members
            ],
        )

    async def get_trust_profile(
        self,
        guild_id: int,
        user_id: int,
    ):

        await self.ensure_trust_user(
            guild_id,
            user_id,
        )

        return await self.db.fetchone(
            """
            SELECT
                trust_score,
                vouches_given,
                vouch_positive,
                vouch_negative
            FROM user_vouch_network
            WHERE guild_id = ?
              AND user_id = ?
            """,
            (
                guild_id,
                user_id,
            ),
        )

    @staticmethod
    def trust_bar(
        score: int,
    ) -> str:

        score = clamp(score)

        filled = round(
            score / 10
        )

        return (
            "█" * filled
            + "░" * (10 - filled)
        )

    async def build_profile_embed(
        self,
        guild: discord.Guild,
        member: discord.Member,
    ) -> discord.Embed:

        row = await self.get_trust_profile(
            guild.id,
            member.id,
        )

        (
            score,
            given,
            positive,
            negative,
        ) = map(
            int,
            row,
        )

        if (
            score
            >= TRUSTED_TRADER_THRESHOLD
        ):
            rank = (
                f"<@&{TRUSTED_TRADER_ROLE_ID}>"
            )
            color = discord.Color.gold()

        elif (
            score
            >= TRADER_THRESHOLD
        ):
            rank = (
                f"<@&{TRADER_ROLE_ID}>"
            )
            color = discord.Color.green()

        else:
            rank = "No reward role yet"
            color = discord.Color.blurple()

        embed = discord.Embed(
            title=(
                f"Vouch Profile — "
                f"{member.display_name}"
            ),
            color=color,
        )

        embed.set_thumbnail(
            url=member.display_avatar.url
        )

        embed.add_field(
            name="Trust",
            value=(
                f"**{score}/100**\n"
                f"{self.trust_bar(score)}"
            ),
            inline=False,
        )

        embed.add_field(
            name="Rank",
            value=rank,
            inline=True,
        )

        embed.add_field(
            name="Vouches Given",
            value=str(given),
            inline=True,
        )

        embed.add_field(
            name="+Vouch",
            value=str(positive),
            inline=True,
        )

        embed.add_field(
            name="-Vouch",
            value=str(negative),
            inline=True,
        )

        embed.set_footer(
            text="Everyone starts at 0 Trust"
        )

        return embed

    async def resolve_member(
        self,
        guild: discord.Guild,
        value: str,
    ) -> Optional[discord.Member]:

        value = value.strip()

        if not value:
            return None

        mention = re.fullmatch(
            r"<@!?(\\d+)>",
            value,
        )

        if mention:
            value = mention.group(1)

        if value.isdigit():

            member = guild.get_member(
                int(value)
            )

            if member:
                return member

            try:
                return await guild.fetch_member(
                    int(value)
                )
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                return None

        lowered = value.lower()

        for member in guild.members:

            if (
                member.name.lower()
                == lowered
            ):
                return member

            if (
                member.display_name.lower()
                == lowered
            ):
                return member

            if (
                member.global_name
                and
                member.global_name.lower()
                == lowered
            ):
                return member

        return None

    async def update_vouch_roles(
        self,
        guild: discord.Guild,
        user_id: int,
        score: int,
    ):

        member = guild.get_member(
            user_id
        )

        if member is None:

            try:
                member = await guild.fetch_member(
                    user_id
                )
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                return

        trader_role = guild.get_role(
            TRADER_ROLE_ID
        )

        trusted_role = guild.get_role(
            TRUSTED_TRADER_ROLE_ID
        )

        try:

            # Trusted Trader at 50
            if (
                trusted_role
                and
                score >= TRUSTED_TRADER_THRESHOLD
            ):

                if trusted_role not in member.roles:
                    await member.add_roles(
                        trusted_role,
                        reason="Trust reached 50",
                    )

            elif (
                trusted_role
                and
                score < TRUSTED_TRADER_THRESHOLD
            ):

                if trusted_role in member.roles:
                    await member.remove_roles(
                        trusted_role,
                        reason="Trust below 50",
                    )

            # Trader at 25
            if (
                trader_role
                and
                score >= TRADER_THRESHOLD
            ):

                if trader_role not in member.roles:
                    await member.add_roles(
                        trader_role,
                        reason="Trust reached 25",
                    )

            elif (
                trader_role
                and
                score < TRADER_THRESHOLD
            ):

                if trader_role in member.roles:
                    await member.remove_roles(
                        trader_role,
                        reason="Trust below 25",
                    )

        except discord.Forbidden:

            logger.warning(
                "Cannot manage Trust roles in guild %s. "
                "Check Manage Roles and role hierarchy.",
                guild.id,
            )

        except discord.HTTPException:

            logger.exception(
                "Trust role update failed."
            )

    async def process_vouch(
        self,
        guild: discord.Guild,
        giver: discord.abc.User,
        target_id: int,
        vouch_type: str,
        reason: str,
    ):

        if guild is None:
            return {
                "ok": False,
                "message": "❌ Server only.",
            }

        reason = reason.strip()

        if not reason:
            return {
                "ok": False,
                "message":
                    "❌ Reason is required.",
            }

        if len(reason) > 200:
            return {
                "ok": False,
                "message":
                    "❌ Reason is too long (max 200 characters).",
            }

        if target_id == giver.id:
            return {
                "ok": False,
                "message":
                    "❌ You cannot vouch yourself.",
            }

        if vouch_type not in {
            "POSITIVE",
            "NEGATIVE",
        }:
            return {
                "ok": False,
                "message":
                    "❌ Invalid vouch type.",
            }

        target = guild.get_member(
            target_id
        )

        if target is None:

            try:
                target = await guild.fetch_member(
                    target_id
                )
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                target = None

        if target is None:
            return {
                "ok": False,
                "message":
                    "❌ That user is no longer in this server.",
            }

        if target.bot:
            return {
                "ok": False,
                "message":
                    "❌ You cannot vouch a bot.",
            }

        delta = (
            1
            if vouch_type == "POSITIVE"
            else -1
        )

        timestamp = now_ts()

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
                    VALUES (?, ?, 0, 0, 0, 0)
                    """,
                    (
                        guild.id,
                        target.id,
                    ),
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
                    VALUES (?, ?, 0, 0, 0, 0)
                    """,
                    (
                        guild.id,
                        giver.id,
                    ),
                )

                async with connection.execute(
                    """
                    SELECT trust_score
                    FROM user_vouch_network
                    WHERE guild_id = ?
                      AND user_id = ?
                    """,
                    (
                        guild.id,
                        target.id,
                    ),
                ) as cursor:

                    row = await cursor.fetchone()

                old_score = (
                    int(row[0])
                    if row
                    else STARTING_TRUST
                )

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
                        target.id,
                        giver.id,
                        vouch_type,
                        reason,
                        timestamp,
                    ),
                )

                if cursor.rowcount == 0:
                    return {
                        "ok": False,
                        "message":
                            "❌ You already vouched this user.",
                    }

                new_score = clamp(
                    old_score + delta
                )

                await connection.execute(
                    """
                    UPDATE user_vouch_network
                    SET trust_score = ?
                    WHERE guild_id = ?
                      AND user_id = ?
                    """,
                    (
                        new_score,
                        guild.id,
                        target.id,
                    ),
                )

                await connection.execute(
                    """
                    UPDATE user_vouch_network
                    SET
                        vouches_given =
                            vouches_given + 1,
                        vouch_positive =
                            vouch_positive + ?,
                        vouch_negative =
                            vouch_negative + ?
                    WHERE guild_id = ?
                      AND user_id = ?
                    """,
                    (
                        1
                        if vouch_type
                        == "POSITIVE"
                        else 0,
                        1
                        if vouch_type
                        == "NEGATIVE"
                        else 0,
                        guild.id,
                        giver.id,
                    ),
                )

        except aiosqlite.Error:

            logger.exception(
                "Saving vouch failed."
            )

            return {
                "ok": False,
                "message":
                    "❌ The vouch could not be saved.",
            }

        await self.update_vouch_roles(
            guild,
            target.id,
            new_score,
        )

        await self.send_transaction_log(
            guild,
            giver,
            target,
            vouch_type,
            reason,
            old_score,
            new_score,
            timestamp,
        )

        return {
            "ok": True,
            "target": target,
            "old_score": old_score,
            "new_score": new_score,
        }

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
            (
                guild.id,
            ),
        )

        if row is None:
            return

        channel = guild.get_channel(
            int(row[0])
        )

        if channel is None:

            try:
                channel = await guild.fetch_channel(
                    int(row[0])
                )
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                return

        if not isinstance(
            channel,
            discord.TextChannel,
        ):
            return

        label = (
            "+Vouch"
            if vouch_type
            == "POSITIVE"
            else "-Vouch"
        )

        embed = discord.Embed(
            title="Vouch Transaction",
            color=(
                discord.Color.green()
                if vouch_type
                == "POSITIVE"
                else discord.Color.red()
            ),
            timestamp=datetime.fromtimestamp(
                timestamp,
                tz=timezone.utc,
            ),
        )

        embed.add_field(
            name="Vouched by",
            value=giver.mention,
            inline=True,
        )

        embed.add_field(
            name="Vouched user",
            value=target.mention,
            inline=True,
        )

        embed.add_field(
            name="Type",
            value=label,
            inline=True,
        )

        embed.add_field(
            name="Trust",
            value=(
                f"{old_score} "
                f"→ **{new_score}**"
            ),
            inline=True,
        )

        embed.add_field(
            name="Reason",
            value=reason,
            inline=False,
        )

        try:
            await channel.send(
                embed=embed
            )
        except (
            discord.Forbidden,
            discord.HTTPException,
        ):
            logger.exception(
                "Could not send transaction log."
            )

    # ========================================================
    # LEADERBOARD
    # ========================================================

    @staticmethod
    def font(
        size: int,
        bold: bool = False,
    ):

        candidates = [

            (
                "/usr/share/fonts/truetype/dejavu/"
                "DejaVuSans-Bold.ttf"
                if bold
                else
                "/usr/share/fonts/truetype/dejavu/"
                "DejaVuSans.ttf"
            ),

            (
                "/usr/share/fonts/truetype/liberation2/"
                "LiberationSans-Bold.ttf"
                if bold
                else
                "/usr/share/fonts/truetype/liberation2/"
                "LiberationSans-Regular.ttf"
            ),
        ]

        for path in candidates:

            if os.path.exists(path):
                return ImageFont.truetype(
                    path,
                    size,
                )

        return ImageFont.load_default()

    async def get_leaderboard_entries(
        self,
        guild: discord.Guild,
    ):

        await self.ensure_guild_trust_users(
            guild
        )

        rows = await self.db.fetchall(
            """
            SELECT
                user_id,
                trust_score,
                vouches_given
            FROM user_vouch_network
            WHERE guild_id = ?
            """,
            (
                guild.id,
            ),
        )

        stats = {
            int(row[0]): {
                "trust": int(row[1]),
                "given": int(row[2]),
            }
            for row in rows
        }

        entries = []

        for member in guild.members:

            if member.bot:
                continue

            entries.append(
                (
                    member,
                    stats.get(
                        member.id,
                        {
                            "trust": 0,
                            "given": 0,
                        },
                    ),
                )
            )

        entries.sort(
            key=lambda item: (
                -item[1]["trust"],
                -item[1]["given"],
                item[0].display_name.lower(),
                item[0].id,
            )
        )

        return entries

    async def avatar_bytes(
        self,
        member: discord.Member,
    ):

        try:
            return await member.display_avatar.read()
        except Exception:
            return None

    async def make_leaderboard_file(
        self,
        guild: discord.Guild,
        entries,
        page: int,
        total_pages: int,
    ) -> discord.File:

        width = 1400
        height = 900

        image = Image.new(
            "RGB",
            (
                width,
                height,
            ),
            (14, 17, 25),
        )

        draw = ImageDraw.Draw(
            image
        )

        draw.rounded_rectangle(
            (
                35,
                30,
                width - 35,
                145,
            ),
            radius=28,
            fill=(28, 33, 46),
            outline=(67, 77, 97),
            width=2,
        )

        draw.text(
            (
                70,
                48,
            ),
            "VOUCH LEADERBOARD",
            font=self.font(
                42,
                True,
            ),
            fill=(245, 247, 250),
        )

        draw.text(
            (
                72,
                103,
            ),
            (
                f"{guild.name}"
                f"  •  Page "
                f"{page + 1}/{total_pages}"
            ),
            font=self.font(
                22
            ),
            fill=(156, 166, 185),
        )

        start = (
            page
            * VouchLeaderboardView.PER_PAGE
        )

        visible = entries[
            start:
            start
            + VouchLeaderboardView.PER_PAGE
        ]

        avatars = await asyncio.gather(
            *(
                self.avatar_bytes(member)
                for member, _ in visible
            ),
            return_exceptions=True,
        )

        for offset, (
            (member, stat),
            avatar_data,
        ) in enumerate(
            zip(
                visible,
                avatars,
            )
        ):

            rank = start + offset + 1

            row_y = (
                175
                + offset * 68
            )

            top = rank <= 3

            fill = (
                (43, 39, 24)
                if top
                else
                (24, 28, 38)
            )

            outline = (
                (174, 145, 62)
                if top
                else
                (49, 56, 72)
            )

            draw.rounded_rectangle(
                (
                    40,
                    row_y,
                    width - 40,
                    row_y + 56,
                ),
                radius=17,
                fill=fill,
                outline=outline,
                width=2,
            )

            draw.text(
                (
                    65,
                    row_y + 12,
                ),
                f"#{rank}",
                font=self.font(
                    23,
                    True,
                ),
                fill=(239, 243, 248),
            )

            avatar_ok = isinstance(
                avatar_data,
                bytes,
            )

            if avatar_ok:

                try:
                    avatar = (
                        Image.open(
                            io.BytesIO(
                                avatar_data
                            )
                        )
                        .convert("RGB")
                        .resize(
                            (
                                40,
                                40,
                            )
                        )
                    )

                    mask = Image.new(
                        "L",
                        (
                            40,
                            40,
                        ),
                        0,
                    )

                    ImageDraw.Draw(
                        mask
                    ).ellipse(
                        (
                            0,
                            0,
                            40,
                            40,
                        ),
                        fill=255,
                    )

                    image.paste(
                        avatar,
                        (
                            135,
                            row_y + 8,
                        ),
                        mask,
                    )

                except Exception:
                    avatar_ok = False

            if not avatar_ok:

                initial = (
                    (
                        member.display_name[:1]
                        or "?"
                    ).upper()
                )

                draw.ellipse(
                    (
                        135,
                        row_y + 8,
                        175,
                        row_y + 48,
                    ),
                    fill=(62, 69, 88),
                )

                draw.text(
                    (
                        147,
                        row_y + 10,
                    ),
                    initial,
                    font=self.font(
                        20,
                        True,
                    ),
                    fill=(235, 238, 245),
                )

            draw.text(
                (
                    195,
                    row_y + 6,
                ),
                member.display_name[:27],
                font=self.font(
                    21,
                    True,
                ),
                fill=(245, 247, 250),
            )

            draw.text(
                (
                    195,
                    row_y + 32,
                ),
                (
                    f"{stat['given']} "
                    f"vouches given"
                ),
                font=self.font(
                    16
                ),
                fill=(148, 157, 175),
            )

            draw.text(
                (
                    width - 260,
                    row_y + 7,
                ),
                str(stat["trust"]),
                font=self.font(
                    28,
                    True,
                ),
                fill=(99, 212, 162),
            )

            draw.text(
                (
                    width - 150,
                    row_y + 17,
                ),
                "TRUST",
                font=self.font(
                    14,
                    True,
                ),
                fill=(150, 159, 177),
            )

        if not visible:

            draw.text(
                (
                    75,
                    210,
                ),
                "No users found.",
                font=self.font(
                    28
                ),
                fill=(180, 188, 201),
            )

        draw.text(
            (
                70,
                height - 45,
            ),
            (
                "0 start  •  "
                "25 Trader  •  "
                "50 Trusted Trader"
            ),
            font=self.font(
                18
            ),
            fill=(126, 136, 154),
        )

        buffer = io.BytesIO()

        image.save(
            buffer,
            format="PNG",
            optimize=True,
        )

        buffer.seek(0)

        return discord.File(
            buffer,
            filename="vouch-leaderboard.png",
        )

    # ========================================================
    # GIVEAWAYS
    # ========================================================

    async def finish_giveaway(
        self,
        message_id: int,
    ):

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
            (
                now_ts(),
                message_id,
                now_ts(),
            ),
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
            (
                message_id,
            ),
        )

        if row is None:
            return

        (
            channel_id,
            guild_id,
            prize,
            winners,
            host_id,
            retries,
        ) = row

        try:

            channel = self.get_channel(
                int(channel_id)
            )

            if channel is None:
                channel = await self.fetch_channel(
                    int(channel_id)
                )

            participants = await self.db.fetchall(
                """
                SELECT user_id
                FROM giveaway_participants
                WHERE message_id = ?
                """,
                (
                    message_id,
                ),
            )

            ids = [
                int(row[0])
                for row in participants
            ]

            random.shuffle(ids)

            selected = ids[
                :max(
                    0,
                    int(winners),
                )
            ]

            winner_text = (
                "No eligible winners."
            )

            if selected:

                winner_text = ", ".join(
                    f"<@{uid}>"
                    for uid in selected
                )

            embed = discord.Embed(
                title="Giveaway Ended",
                description=(
                    f"**Prize:** {prize}\n"
                    f"**Winners:** {winner_text}\n"
                    f"**Participants:** {len(ids)}"
                ),
                color=discord.Color.gold(),
            )

            embed.set_footer(
                text=(
                    f"Hosted by "
                    f"<@{int(host_id)}>"
                )
            )

            await channel.send(
                embed=embed
            )

            await self.db.execute(
                """
                UPDATE giveaway_system
                SET
                    status = 'COMPLETED',
                    result_winners = ?,
                    result_participant_count = ?,
                    last_error = NULL
                WHERE message_id = ?
                """,
                (
                    json.dumps(selected),
                    len(ids),
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
                    len(ids),
                    json.dumps(selected),
                    now_ts(),
                ),
            )

        except Exception as exc:

            logger.exception(
                "Giveaway %s failed.",
                message_id,
            )

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
                    int(retries or 0) + 1,
                    str(exc)[:1000],
                    message_id,
                ),
            )

    @tasks.loop(
        seconds=15
    )
    async def giveaway_loop(
        self,
    ):

        try:

            rows = await self.db.fetchall(
                """
                SELECT message_id
                FROM giveaway_system
                WHERE status = 'ACTIVE'
                  AND ends_at <= ?
                """,
                (
                    now_ts(),
                ),
            )

            for row in rows:
                await self.finish_giveaway(
                    int(row[0])
                )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Giveaway loop failed."
            )

    @giveaway_loop.before_loop
    async def before_giveaway_loop(
        self,
    ):
        await self.wait_until_ready()

    @tasks.loop(
        seconds=30
    )
    async def temp_ban_loop(
        self,
    ):

        try:

            rows = await self.db.fetchall(
                """
                SELECT guild_id, target_id
                FROM temporary_bans
                WHERE expiry_timestamp <= ?
                """,
                (
                    now_ts(),
                ),
            )

            for guild_id, target_id in rows:

                guild = self.get_guild(
                    int(guild_id)
                )

                if guild is None:
                    continue

                try:

                    await guild.unban(
                        discord.Object(
                            id=int(target_id)
                        ),
                        reason=(
                            "Temporary ban expired"
                        ),
                    )

                except discord.NotFound:
                    pass

                except discord.Forbidden:
                    logger.warning(
                        "Cannot unban %s in guild %s.",
                        target_id,
                        guild_id,
                    )
                    continue

                except discord.HTTPException:
                    logger.exception(
                        "Unban failed for %s.",
                        target_id,
                    )
                    continue

                await self.db.execute(
                    """
                    DELETE FROM temporary_bans
                    WHERE guild_id = ?
                      AND target_id = ?
                    """,
                    (
                        guild_id,
                        target_id,
                    ),
                )

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception(
                "Temporary ban loop failed."
            )

    @temp_ban_loop.before_loop
    async def before_temp_ban_loop(
        self,
    ):
        await self.wait_until_ready()

    # ========================================================
    # AI
    # ========================================================

    async def ask_ai(
        self,
        prompt: str,
    ) -> str:

        if self.groq is None:
            return (
                "AI is not configured."
            )

        def request():

            response = (
                self.groq.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content":
                                "You are a concise, "
                                "friendly Discord assistant.",
                        },
                        {
                            "role": "user",
                            "content": prompt,
                        },
                    ],
                    temperature=0.7,
                    max_tokens=600,
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
                request
            )

        except Exception:

            logger.exception(
                "Groq request failed."
            )

            return (
                "❌ AI is temporarily unavailable."
            )

    # ========================================================
    # COMMANDS
    # ========================================================

    @app_commands.command(
        name="vouchpanel",
        description="Post the vouch panel.",
    )
    @owner_only()
    async def vouchpanel_command(
        self,
        interaction: discord.Interaction,
    ):

        if (
            interaction.guild is None
            or interaction.channel is None
        ):
            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        # IMPORTANT:
        # Respond before channel.send.
        await interaction.response.defer(
            ephemeral=True
        )

        embed = discord.Embed(
            title="Trader Vouch System",
            description=(
                "Your Trust score shows how reliable "
                "you are when trading. Everyone starts "
                "at **0 Trust** out of 100.\n\n"

                "**How it works**\n"
                "Use **Vouch A User** after a real trade. "
                "Pick **+Vouch** or **-Vouch** and add "
                "a short reason.\n"
                "Use **Check User's Vouch** before "
                "trading with someone.\n\n"

                "**Vouch Rewards**\n"
                f"**25** · "
                f"<@&{TRADER_ROLE_ID}>\n"
                f"**50** · "
                f"<@&{TRUSTED_TRADER_ROLE_ID}>\n\n"

                "Only use vouches for real trades."
            ),
            color=discord.Color.blurple(),
        )

        try:

            await interaction.channel.send(
                embed=embed,
                view=TrustPanelView(self),
            )

            await interaction.followup.send(
                "✅ Vouch panel posted.",
                ephemeral=True,
            )

        except Exception:

            logger.exception(
                "Posting vouch panel failed."
            )

            await interaction.followup.send(
                "❌ I couldn't post the panel. "
                "Check my channel permissions.",
                ephemeral=True,
            )

    @app_commands.command(
        name="activity",
        description="Show server activity.",
    )
    @owner_only()
    async def activity_command(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            ephemeral=True
        )

        total = await self.db.fetchone(
            """
            SELECT COALESCE(
                SUM(message_count),
                0
            )
            FROM user_activity
            WHERE guild_id = ?
            """,
            (
                interaction.guild.id,
            ),
        )

        active = await self.db.fetchone(
            """
            SELECT COUNT(*)
            FROM user_activity
            WHERE guild_id = ?
              AND message_count > 0
            """,
            (
                interaction.guild.id,
            ),
        )

        embed = discord.Embed(
            title="Server Activity",
            description=(
                f"**Messages:** "
                f"{int(total[0])}\n"
                f"**Active users:** "
                f"{int(active[0])}"
            ),
            color=discord.Color.blurple(),
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )

    @app_commands.command(
        name="botstats",
        description="Show bot statistics.",
    )
    @owner_only()
    async def botstats_command(
        self,
        interaction: discord.Interaction,
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        vouches = await self.db.fetchone(
            "SELECT COUNT(*) FROM vouch_history"
        )

        giveaways = await self.db.fetchone(
            "SELECT COUNT(*) FROM giveaway_history"
        )

        embed = discord.Embed(
            title="Bot Stats",
            description=(
                f"**Guilds:** "
                f"{len(self.guilds)}\n"
                f"**Vouches:** "
                f"{int(vouches[0])}\n"
                f"**Giveaways:** "
                f"{int(giveaways[0])}\n"
                f"**Latency:** "
                f"{round(self.latency * 1000, 2)} ms"
            ),
            color=discord.Color.blurple(),
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )

    @app_commands.command(
        name="say",
        description="Send a message as the bot.",
    )
    @app_commands.describe(
        message="Message to send"
    )
    @owner_only()
    async def say_command(
        self,
        interaction: discord.Interaction,
        message: str,
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        if interaction.channel is not None:

            await interaction.channel.send(
                message
            )

        await interaction.followup.send(
            "✅ Sent.",
            ephemeral=True,
        )

    @app_commands.command(
        name="sync",
        description="Sync slash commands.",
    )
    @owner_only()
    async def sync_command(
        self,
        interaction: discord.Interaction,
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        synced = await self.tree.sync()

        await interaction.followup.send(
            f"✅ Synced {len(synced)} command(s).",
            ephemeral=True,
        )

    @app_commands.command(
        name="transactionlog",
        description="Set the vouch transaction log channel.",
    )
    @app_commands.describe(
        channel="The transaction log channel"
    )
    @owner_only()
    async def transactionlog_command(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ):

        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            ephemeral=True
        )

        await self.db.execute(
            """
            INSERT INTO transaction_log_config (
                guild_id,
                channel_id
            )
            VALUES (?, ?)
            ON CONFLICT(guild_id)
            DO UPDATE SET
                channel_id =
                    excluded.channel_id
            """,
            (
                interaction.guild.id,
                channel.id,
            ),
        )

        await interaction.followup.send(
            f"✅ Vouch logs will be sent to {channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="tempban",
        description="Temporarily ban a member.",
    )
    @app_commands.describe(
        user="Member to ban",
        duration="e.g. 30m, 2h, 1d",
    )
    @owner_only()
    async def tempban_command(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
    ):

        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        seconds = parse_duration(
            duration
        )

        if seconds is None:
            await interaction.response.send_message(
                (
                    "❌ Invalid duration. "
                    "Example: `30m`, `2h`, `1d`."
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            ephemeral=True
        )

        try:

            await interaction.guild.ban(
                user,
                reason=(
                    f"Temporary ban by "
                    f"{interaction.user}"
                ),
                delete_message_seconds=0,
            )

        except discord.Forbidden:

            await interaction.followup.send(
                (
                    "❌ I cannot ban that member. "
                    "Check permissions and role hierarchy."
                ),
                ephemeral=True,
            )
            return

        except discord.HTTPException:

            await interaction.followup.send(
                "❌ Discord rejected the ban.",
                ephemeral=True,
            )
            return

        await self.db.execute(
            """
            INSERT INTO temporary_bans (
                guild_id,
                target_id,
                expiry_timestamp
            )
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, target_id)
            DO UPDATE SET
                expiry_timestamp =
                    excluded.expiry_timestamp
            """,
            (
                interaction.guild.id,
                user.id,
                now_ts() + seconds,
            ),
        )

        await interaction.followup.send(
            (
                f"✅ {user.mention} was banned "
                f"for `{duration}`."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="create",
        description="Create a giveaway.",
    )
    @app_commands.describe(
        prize="Prize",
        duration="e.g. 30m, 2h, 1d",
        winners="Winner count",
    )
    @owner_only()
    async def giveaway_create(
        self,
        interaction: discord.Interaction,
        prize: str,
        duration: str,
        winners: int,
    ):

        if (
            interaction.guild is None
            or interaction.channel is None
        ):
            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        seconds = parse_duration(
            duration
        )

        if seconds is None:
            await interaction.response.send_message(
                "❌ Invalid duration.",
                ephemeral=True,
            )
            return

        if not 1 <= winners <= 100:
            await interaction.response.send_message(
                "❌ Winners must be between 1 and 100.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            ephemeral=True
        )

        ends = now_ts() + seconds

        embed = discord.Embed(
            title="Giveaway",
            description=(
                f"**Prize:** {prize}\n"
                f"**Winners:** {winners}\n"
                f"**Ends:** "
                f"{discord_time(ends)}\n\n"
                "Click the button below to enter."
            ),
            color=discord.Color.blurple(),
        )

        embed.set_footer(
            text=f"Hosted by {interaction.user}"
        )

        message = await interaction.channel.send(
            embed=embed
        )

        view = GiveawayJoinView(
            self,
            message.id,
        )

        await message.edit(
            view=view
        )

        self.add_view(
            view,
            message_id=message.id,
        )

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
                ends,
                winners,
                interaction.user.id,
            ),
        )

        await interaction.followup.send(
            (
                f"✅ Giveaway created: "
                f"{message.jump_url}"
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="end",
        description="End a giveaway early.",
    )
    @app_commands.describe(
        message_id="Giveaway message ID"
    )
    @owner_only()
    async def giveaway_end(
        self,
        interaction: discord.Interaction,
        message_id: str,
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        try:
            giveaway_id = int(
                message_id
            )
        except ValueError:

            await interaction.followup.send(
                "❌ Invalid message ID.",
                ephemeral=True,
            )
            return

        row = await self.db.fetchone(
            """
            SELECT status
            FROM giveaway_system
            WHERE message_id = ?
            """,
            (
                giveaway_id,
            ),
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
            """
            UPDATE giveaway_system
            SET ends_at = ?
            WHERE message_id = ?
            """,
            (
                now_ts(),
                giveaway_id,
            ),
        )

        await self.finish_giveaway(
            giveaway_id
        )

        await interaction.followup.send(
            "✅ Giveaway ended.",
            ephemeral=True,
        )

    # ========================================================
    # EVENTS
    # ========================================================

    async def on_member_join(
        self,
        member: discord.Member,
    ):

        if not member.bot:

            await self.ensure_trust_user(
                member.guild.id,
                member.id,
            )

    async def on_message(
        self,
        message: discord.Message,
    ):

        if message.author.bot:
            return

        if message.guild is not None:

            try:

                today = (
                    datetime.now(
                        timezone.utc
                    )
                    .date()
                    .isoformat()
                )

                week = (
                    datetime.now(
                        timezone.utc
                    )
                    .strftime("%G-W%V")
                )

                month = (
                    datetime.now(
                        timezone.utc
                    )
                    .strftime("%Y-%m")
                )

                await self.db.execute(
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
                    VALUES (
                        ?, ?, 1, 1, 1, 1, ?, ?, ?
                    )
                    ON CONFLICT(guild_id, user_id)
                    DO UPDATE SET
                        message_count =
                            message_count + 1,
                        daily_message_count =
                            daily_message_count + 1,
                        week_message_count =
                            week_message_count + 1,
                        month_message_count =
                            month_message_count + 1
                    """,
                    (
                        message.guild.id,
                        message.author.id,
                        today,
                        week,
                        month,
                    ),
                )

            except Exception:

                logger.exception(
                    "Activity update failed."
                )

        if (
            self.user
            and self.user in message.mentions
        ):

            prompt = message.content

            prompt = prompt.replace(
                f"<@{self.user.id}>",
                "",
            )

            prompt = prompt.replace(
                f"<@!{self.user.id}>",
                "",
            )

            prompt = prompt.strip()

            if not prompt:

                await message.reply(
                    "Mention me with a question and I’ll answer."
                )
                return

            answer = await self.ask_ai(
                prompt
            )

            await message.reply(
                answer[:2000]
            )

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):

        if isinstance(
            error,
            app_commands.CheckFailure,
        ):

            message = (
                "❌ You do not have permission "
                "to use this command."
            )

        else:

            logger.exception(
                "Slash command error",
                exc_info=error,
            )

            message = (
                "❌ Something went wrong. "
                "Check the bot logs."
            )

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
                "Could not send command error response."
            )


# ============================================================
# PANEL
# ============================================================

class TrustPanelView(
    SafeView
):

    def __init__(
        self,
        bot: VouchBot,
    ):
        super().__init__(
            timeout=None
        )

        self.bot = bot

    @discord.ui.button(
        label="My Profile",
        style=discord.ButtonStyle.secondary,
        custom_id="trust:my_profile",
    )
    async def my_profile(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if interaction.guild is None:

            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        # Acknowledge the button immediately.
        await interaction.response.defer()

        try:

            embed = await self.bot.build_profile_embed(
                interaction.guild,
                interaction.user,
            )

            await interaction.followup.send(
                embed=embed,
                ephemeral=True,
            )

        except Exception:

            logger.exception(
                "My Profile failed."
            )

            await interaction.followup.send(
                "❌ I couldn't load your profile.",
                ephemeral=True,
            )

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

            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Select a user or enter a Name / ID.",
            view=CheckMemberView(
                self.bot
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Vouch A User",
        style=discord.ButtonStyle.primary,
        custom_id="trust:vouch_user",
    )
    async def vouch_user(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if interaction.guild is None:

            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Select a user or enter a Name / ID.",
            view=VouchTargetView(
                self.bot
            ),
            ephemeral=True,
        )

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
                "Trust starts at **0**.\n\n"
                f"**25** · "
                f"<@&{TRADER_ROLE_ID}>\n\n"
                f"**50** · "
                f"<@&{TRUSTED_TRADER_ROLE_ID}>"
            ),
            color=discord.Color.gold(),
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Vouch Leaderboard",
        style=discord.ButtonStyle.secondary,
        custom_id="trust:leaderboard",
    )
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if interaction.guild is None:

            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        # Acknowledge immediately before image generation.
        await interaction.response.defer(
            ephemeral=True
        )

        try:

            view = VouchLeaderboardView(
                self.bot,
                interaction.guild,
                0,
            )

            embed, file = (
                await view.render()
            )

            await interaction.followup.send(
                embed=embed,
                file=file,
                view=view,
                ephemeral=True,
            )

        except Exception:

            logger.exception(
                "Opening leaderboard failed."
            )

            await interaction.followup.send(
                "❌ I couldn't load the leaderboard.",
                ephemeral=True,
            )


# ============================================================
# VOUCH TARGET
# ============================================================

class VouchTargetView(
    SafeView
):

    def __init__(
        self,
        bot: VouchBot,
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
            self.user_selected
        )

        self.add_item(
            self.user_select
        )

    async def user_selected(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:

            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        # FIX: defer before processing select.
        await interaction.response.defer()

        try:

            target = (
                self.user_select.values[0]
            )

            if not isinstance(
                target,
                discord.Member,
            ):

                target = (
                    interaction.guild.get_member(
                        target.id
                    )
                )

            if target is None:

                await interaction.followup.send(
                    "❌ User not found.",
                    ephemeral=True,
                )
                return

            if target.bot:

                await interaction.followup.send(
                    "❌ You cannot vouch a bot.",
                    ephemeral=True,
                )
                return

            if (
                target.id
                == interaction.user.id
            ):

                await interaction.followup.send(
                    "❌ You cannot vouch yourself.",
                    ephemeral=True,
                )
                return

            await interaction.followup.send(
                (
                    f"You selected "
                    f"**{target.display_name}**. "
                    "Choose the vouch type:"
                ),
                view=VouchTypeView(
                    self.bot,
                    target.id,
                ),
                ephemeral=True,
            )

        except Exception:

            logger.exception(
                "Vouch target select failed."
            )

            await interaction.followup.send(
                "❌ I couldn't process that user.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Enter Name / ID",
        style=discord.ButtonStyle.secondary,
    )
    async def enter_name(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if interaction.guild is None:

            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            VouchMemberModal(
                self.bot
            )
        )


# ============================================================
# VOUCH MEMBER MODAL
# ============================================================

class VouchMemberModal(
    SafeModal,
    title="Find User",
):

    user_input = discord.ui.TextInput(
        label="Name / ID",
        placeholder=(
            "Username, display name or Discord ID"
        ),
        required=True,
        max_length=100,
    )

    def __init__(
        self,
        bot: VouchBot,
    ):
        super().__init__()
        self.bot = bot

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:

            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            ephemeral=True
        )

        try:

            target = await self.bot.resolve_member(
                interaction.guild,
                str(
                    self.user_input
                ).strip(),
            )

            if target is None:

                await interaction.followup.send(
                    "❌ User not found.",
                    ephemeral=True,
                )
                return

            if target.bot:

                await interaction.followup.send(
                    "❌ You cannot vouch a bot.",
                    ephemeral=True,
                )
                return

            if (
                target.id
                == interaction.user.id
            ):

                await interaction.followup.send(
                    "❌ You cannot vouch yourself.",
                    ephemeral=True,
                )
                return

            await interaction.followup.send(
                (
                    f"You selected "
                    f"**{target.display_name}**. "
                    "Choose the vouch type:"
                ),
                view=VouchTypeView(
                    self.bot,
                    target.id,
                ),
                ephemeral=True,
            )

        except Exception:

            logger.exception(
                "Vouch member modal failed."
            )

            await interaction.followup.send(
                "❌ I couldn't find that user.",
                ephemeral=True,
            )


# ============================================================
# VOUCH TYPE
# ============================================================

class VouchTypeView(
    SafeView
):

    def __init__(
        self,
        bot: VouchBot,
        target_id: int,
    ):
        super().__init__(
            timeout=120
        )

        self.bot = bot
        self.target_id = target_id

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
                self.target_id,
                "POSITIVE",
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
                self.target_id,
                "NEGATIVE",
            )
        )


# ============================================================
# VOUCH REASON MODAL
# ============================================================

class VouchReasonModal(
    SafeModal
):

    def __init__(
        self,
        bot: VouchBot,
        target_id: int,
        vouch_type: str,
    ):

        super().__init__(
            title=(
                "+Vouch"
                if vouch_type == "POSITIVE"
                else "-Vouch"
            )
        )

        self.bot = bot
        self.target_id = target_id
        self.vouch_type = vouch_type

        self.reason = discord.ui.TextInput(
            label="Reason",
            placeholder="Explain the trade briefly",
            required=True,
            max_length=200,
            style=discord.TextStyle.paragraph,
        )

        self.add_item(
            self.reason
        )

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:

            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        # THIS is one of the important fixes.
        # Defer BEFORE fetch_member.
        await interaction.response.defer(
            ephemeral=True
        )

        try:

            target = (
                interaction.guild.get_member(
                    self.target_id
                )
            )

            if target is None:

                try:

                    target = (
                        await interaction.guild.fetch_member(
                            self.target_id
                        )
                    )

                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):

                    target = None

            if target is None:

                await interaction.followup.send(
                    (
                        "❌ That user is no longer "
                        "in this server."
                    ),
                    ephemeral=True,
                )
                return

            reason = str(
                self.reason
            ).strip()

            if not reason:

                await interaction.followup.send(
                    "❌ Reason cannot be empty.",
                    ephemeral=True,
                )
                return

            label = (
                "+Vouch"
                if self.vouch_type
                == "POSITIVE"
                else "-Vouch"
            )

            embed = discord.Embed(
                title="Confirm Vouch",
                description=(
                    "Review this before it is saved."
                ),
                color=(
                    discord.Color.green()
                    if self.vouch_type
                    == "POSITIVE"
                    else discord.Color.red()
                ),
            )

            embed.add_field(
                name="User",
                value=target.mention,
                inline=False,
            )

            embed.add_field(
                name="Type",
                value=label,
                inline=True,
            )

            embed.add_field(
                name="Reason",
                value=reason,
                inline=False,
            )

            embed.set_footer(
                text=(
                    "The vouch is only saved "
                    "after Confirm Vouch."
                )
            )

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

        except Exception:

            logger.exception(
                "Vouch reason modal failed."
            )

            await interaction.followup.send(
                "❌ I couldn't prepare the confirmation.",
                ephemeral=True,
            )


# ============================================================
# VOUCH CONFIRM
# ============================================================

class VouchConfirmView(
    SafeView
):

    def __init__(
        self,
        bot: VouchBot,
        target_id: int,
        vouch_type: str,
        reason: str,
        giver_id: int,
    ):
        super().__init__(
            timeout=120
        )

        self.bot = bot
        self.target_id = target_id
        self.vouch_type = vouch_type
        self.reason = reason
        self.giver_id = giver_id

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:

        if (
            interaction.user.id
            != self.giver_id
        ):

            if not interaction.response.is_done():

                await interaction.response.send_message(
                    (
                        "❌ This confirmation belongs "
                        "to another user."
                    ),
                    ephemeral=True,
                )

            return False

        return True

    @discord.ui.button(
        label="Confirm Vouch",
        style=discord.ButtonStyle.success,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if interaction.guild is None:

            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        # Immediate acknowledgement.
        await interaction.response.defer()

        for item in self.children:
            item.disabled = True

        try:

            result = await self.bot.process_vouch(
                interaction.guild,
                interaction.user,
                self.target_id,
                self.vouch_type,
                self.reason,
            )

            # Disable the original buttons.
            if interaction.message is not None:

                try:

                    await interaction.message.edit(
                        view=self
                    )

                except (
                    discord.NotFound,
                    discord.HTTPException,
                ):
                    pass

            if not result["ok"]:

                await interaction.followup.send(
                    result["message"],
                    ephemeral=True,
                )
                return

            label = (
                "+Vouch"
                if self.vouch_type
                == "POSITIVE"
                else "-Vouch"
            )

            embed = discord.Embed(
                title=(
                    f"{label} confirmed"
                ),
                description=(
                    f"{result['target'].mention} "
                    f"is now at "
                    f"**{result['new_score']}/100 Trust**."
                    f"\n\n"
                    f"**Reason:** {self.reason}"
                ),
                color=(
                    discord.Color.green()
                    if self.vouch_type
                    == "POSITIVE"
                    else discord.Color.red()
                ),
            )

            embed.set_footer(
                text=(
                    self.bot.trust_bar(
                        result["new_score"]
                    )
                )
            )

            await interaction.followup.send(
                embed=embed,
                ephemeral=True,
            )

        except Exception:

            logger.exception(
                "Confirm vouch failed."
            )

            await interaction.followup.send(
                "❌ The vouch could not be completed.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.danger,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        for item in self.children:
            item.disabled = True

        await interaction.response.edit_message(
            content="❌ Vouch cancelled.",
            embed=None,
            view=self,
        )


# ============================================================
# CHECK MEMBER
# ============================================================

class CheckMemberView(
    SafeView
):

    def __init__(
        self,
        bot: VouchBot,
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
            self.user_selected
        )

        self.add_item(
            self.user_select
        )

    async def user_selected(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:

            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        # Acknowledge first.
        await interaction.response.defer()

        try:

            target = (
                self.user_select.values[0]
            )

            if not isinstance(
                target,
                discord.Member,
            ):

                target = (
                    interaction.guild.get_member(
                        target.id
                    )
                )

            if target is None:

                await interaction.followup.send(
                    "❌ User not found.",
                    ephemeral=True,
                )
                return

            embed = (
                await self.bot.build_profile_embed(
                    interaction.guild,
                    target,
                )
            )

            await interaction.followup.send(
                embed=embed,
                ephemeral=True,
            )

        except Exception:

            logger.exception(
                "Check-user select failed."
            )

            await interaction.followup.send(
                "❌ I couldn't load that profile.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Enter Name / ID",
        style=discord.ButtonStyle.secondary,
    )
    async def enter_name(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if interaction.guild is None:

            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(
            CheckMemberModal(
                self.bot
            )
        )


class CheckMemberModal(
    SafeModal,
    title="Find User",
):

    user_input = discord.ui.TextInput(
        label="Name / ID",
        placeholder=(
            "Username, display name or Discord ID"
        ),
        required=True,
        max_length=100,
    )

    def __init__(
        self,
        bot: VouchBot,
    ):
        super().__init__()
        self.bot = bot

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:

            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            ephemeral=True
        )

        try:

            target = await self.bot.resolve_member(
                interaction.guild,
                str(
                    self.user_input
                ).strip(),
            )

            if target is None:

                await interaction.followup.send(
                    "❌ User not found.",
                    ephemeral=True,
                )
                return

            embed = (
                await self.bot.build_profile_embed(
                    interaction.guild,
                    target,
                )
            )

            await interaction.followup.send(
                embed=embed,
                ephemeral=True,
            )

        except Exception:

            logger.exception(
                "Check member modal failed."
            )

            await interaction.followup.send(
                "❌ I couldn't load that profile.",
                ephemeral=True,
            )


# ============================================================
# LEADERBOARD VIEW
# ============================================================

class VouchLeaderboardView(
    SafeView
):

    PER_PAGE = 10

    def __init__(
        self,
        bot: VouchBot,
        guild: discord.Guild,
        page: int = 0,
    ):
        super().__init__(
            timeout=180
        )

        self.bot = bot
        self.guild = guild
        self.page = page
        self.total_pages = 1

        # Button state gets fixed after render().
        self.previous.disabled = True
        self.next_page.disabled = True

    async def render(self):

        entries = (
            await self.bot.get_leaderboard_entries(
                self.guild
            )
        )

        self.total_pages = max(
            1,
            (
                len(entries)
                + self.PER_PAGE
                - 1
            )
            // self.PER_PAGE,
        )

        self.page = max(
            0,
            min(
                self.page,
                self.total_pages - 1,
            ),
        )

        file = (
            await self.bot.make_leaderboard_file(
                self.guild,
                entries,
                self.page,
                self.total_pages,
            )
        )

        embed = discord.Embed(
            color=discord.Color.blurple()
        )

        embed.set_image(
            url=(
                "attachment://"
                "vouch-leaderboard.png"
            )
        )

        embed.set_footer(
            text=(
                f"Page "
                f"{self.page + 1}/"
                f"{self.total_pages}"
                f" • "
                f"{len(entries)} users"
            )
        )

        self.previous.disabled = (
            self.page <= 0
        )

        self.next_page.disabled = (
            self.page
            >= self.total_pages - 1
        )

        return (
            embed,
            file,
        )

    @discord.ui.button(
        label="‹",
        style=discord.ButtonStyle.secondary,
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if self.page <= 0:

            await interaction.response.send_message(
                "❌ You are already on the first page.",
                ephemeral=True,
            )
            return

        # Component interaction: acknowledge immediately.
        await interaction.response.defer()

        try:

            self.page -= 1

            embed, file = (
                await self.render()
            )

            if interaction.message is not None:

                await interaction.message.edit(
                    embed=embed,
                    attachments=[file],
                    view=self,
                )

        except Exception:

            logger.exception(
                "Leaderboard previous page failed."
            )

            await interaction.followup.send(
                "❌ I couldn't change the page.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="›",
        style=discord.ButtonStyle.secondary,
    )
    async def next_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if (
            self.page
            >= self.total_pages - 1
        ):

            await interaction.response.send_message(
                "❌ You are already on the last page.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        try:

            self.page += 1

            embed, file = (
                await self.render()
            )

            if interaction.message is not None:

                await interaction.message.edit(
                    embed=embed,
                    attachments=[file],
                    view=self,
                )

        except Exception:

            logger.exception(
                "Leaderboard next page failed."
            )

            await interaction.followup.send(
                "❌ I couldn't change the page.",
                ephemeral=True,
            )


# ============================================================
# GIVEAWAY VIEW
# ============================================================

class GiveawayJoinView(
    SafeView
):

    def __init__(
        self,
        bot: VouchBot,
        message_id: int,
    ):
        super().__init__(
            timeout=None
        )

        self.bot = bot
        self.message_id = message_id

        button = discord.ui.Button(
            label="Enter Giveaway",
            style=discord.ButtonStyle.success,
            custom_id=(
                f"giveaway:enter:"
                f"{message_id}"
            ),
        )

        button.callback = self.join

        self.add_item(
            button
        )

    async def join(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:

            await interaction.response.send_message(
                "❌ Server only.",
                ephemeral=True,
            )
            return

        # Acknowledge before DB work.
        await interaction.response.defer()

        try:

            row = await self.bot.db.fetchone(
                """
                SELECT status, ends_at
                FROM giveaway_system
                WHERE message_id = ?
                """,
                (
                    self.message_id,
                ),
            )

            if row is None:

                await interaction.followup.send(
                    "❌ Giveaway not found.",
                    ephemeral=True,
                )
                return

            status, ends_at = row

            if (
                status != "ACTIVE"
                or
                float(ends_at)
                <= now_ts()
            ):

                await interaction.followup.send(
                    "❌ Giveaway has ended.",
                    ephemeral=True,
                )
                return

            inserted = await self.bot.db.execute(
                """
                INSERT OR IGNORE INTO giveaway_participants (
                    message_id,
                    user_id
                )
                VALUES (?, ?)
                """,
                (
                    self.message_id,
                    interaction.user.id,
                ),
            )

            if inserted:

                await interaction.followup.send(
                    "✅ You are entered!",
                    ephemeral=True,
                )

            else:

                await interaction.followup.send(
                    "❌ You are already entered.",
                    ephemeral=True,
                )

        except Exception:

            logger.exception(
                "Giveaway join failed."
            )

            await interaction.followup.send(
                "❌ I couldn't process the entry.",
                ephemeral=True,
            )


# ============================================================
# COMMAND REGISTRATION
# ============================================================

bot = VouchBot()


bot.tree.add_command(
    bot.vouchpanel_command
)

bot.tree.add_command(
    bot.activity_command
)

bot.tree.add_command(
    bot.botstats_command
)

bot.tree.add_command(
    bot.say_command
)

bot.tree.add_command(
    bot.sync_command
)

bot.tree.add_command(
    bot.transactionlog_command
)

bot.tree.add_command(
    bot.tempban_command
)


# ============================================================
# START
# ============================================================

async def main():

    if not DISCORD_TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN is missing."
        )

    async with bot:
        await bot.start(
            DISCORD_TOKEN
        )


if __name__ == "__main__":
    asyncio.run(
        main()
            )
