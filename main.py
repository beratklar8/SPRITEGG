import asyncio
import io
import json
import logging
import os
import random
import re
from datetime import datetime, timezone
from typing import Optional

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import tasks

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return None

try:
    from groq import Groq
except ImportError:
    Groq = None

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None

from database import DatabaseController


load_dotenv()


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("bot")


# ============================================================
# ENVIRONMENT
# ============================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

GROQ_MODEL = os.getenv(
    "GROQ_MODEL",
    "llama-3.3-70b-versatile",
)

BOT_OWNER_ID = int(
    os.getenv("BOT_OWNER_ID", "0") or 0
)

PORT = int(
    os.getenv("PORT", "10000") or 10000
)

_default_db_dir = (
    "/data"
    if os.path.isdir("/data")
    else os.getcwd()
)

DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    os.path.join(
        _default_db_dir,
        "bot_database.db",
    ),
)


# ============================================================
# TRUST CONFIG
# ============================================================

TRADER_ROLE_ID = 1529114068412141639
TRUSTED_TRADER_ROLE_ID = 1529114203204489277

TRADER_THRESHOLD = 25
TRUSTED_TRADER_THRESHOLD = 50

STARTING_TRUST = 0


# ============================================================
# DISCORD INTENTS
# ============================================================

intents = discord.Intents.default()

intents.guilds = True
intents.members = True
intents.message_content = True


# ============================================================
# HELPERS
# ============================================================

def now_ts() -> float:
    return datetime.now(
        timezone.utc
    ).timestamp()


def discord_time(
    timestamp: float,
    style: str = "R",
) -> str:
    return f"<t:{int(timestamp)}:{style}>"


def clamp(
    value: int,
    minimum: int = 0,
    maximum: int = 100,
) -> int:
    return max(
        minimum,
        min(
            maximum,
            int(value),
        ),
    )


def parse_duration(
    value: str,
) -> Optional[int]:

    text = (
        value
        .strip()
        .lower()
        .replace(" ", "")
    )

    if not text:
        return None

    matches = re.findall(
        r"(\d+)(s|m|h|d|w)",
        text,
    )

    if not matches:
        return None

    rebuilt = "".join(
        f"{number}{unit}"
        for number, unit in matches
    )

    if rebuilt != text:
        return None

    multipliers = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 604800,
    }

    seconds = sum(
        int(number) * multipliers[unit]
        for number, unit in matches
    )

    if seconds <= 0:
        return None

    return seconds


def parse_color(
    value: Optional[str],
) -> discord.Color:

    if not value:
        return discord.Color.blurple()

    raw = (
        value
        .strip()
        .lower()
        .lstrip("#")
    )

    try:
        return discord.Color(
            int(raw, 16)
        )
    except (
        TypeError,
        ValueError,
    ):
        return discord.Color.blurple()


def owner_only():

    async def predicate(
        interaction: discord.Interaction,
    ) -> bool:

        return (
            BOT_OWNER_ID != 0
            and interaction.user.id == BOT_OWNER_ID
        )

    return app_commands.check(
        predicate
    )


# ============================================================
# INTERACTION HELPERS
# ============================================================

async def acknowledge(
    interaction: discord.Interaction,
    *,
    ephemeral: bool = True,
) -> bool:
    """
    Acknowledge an interaction immediately.

    thinking=True creates a deferred channel-message response,
    which can safely be completed with interaction.followup.
    """

    try:

        if interaction.response.is_done():
            return False

        await interaction.response.defer(
            ephemeral=ephemeral,
            thinking=True,
        )

        return True

    except discord.InteractionResponded:

        return False

    except discord.NotFound:

        logger.warning(
            "Interaction expired before acknowledgement: %s",
            interaction.id,
        )

        return False

    except discord.HTTPException:

        logger.exception(
            "Discord rejected interaction acknowledgement: %s",
            interaction.id,
        )

        return False

    except Exception:

        logger.exception(
            "Unexpected acknowledgement error: %s",
            interaction.id,
        )

        return False


async def send_interaction_message(
    interaction: discord.Interaction,
    content: Optional[str] = None,
    *,
    embed: Optional[discord.Embed] = None,
    view: Optional[discord.ui.View] = None,
    file: Optional[discord.File] = None,
    ephemeral: bool = True,
):

    try:

        if interaction.response.is_done():

            return await interaction.followup.send(
                content=content,
                embed=embed,
                view=view,
                file=file,
                ephemeral=ephemeral,
            )

        return await interaction.response.send_message(
            content=content,
            embed=embed,
            view=view,
            file=file,
            ephemeral=ephemeral,
        )

    except discord.InteractionResponded:

        try:

            return await interaction.followup.send(
                content=content,
                embed=embed,
                view=view,
                file=file,
                ephemeral=ephemeral,
            )

        except Exception:

            logger.exception(
                "Failed to send interaction follow-up."
            )

    except discord.HTTPException:

        logger.exception(
            "Discord rejected interaction response: %s",
            interaction.id,
        )

    except Exception:

        logger.exception(
            "Unexpected interaction response error: %s",
            interaction.id,
        )

    return None


# ============================================================
# SAFE DISCORD UI
# ============================================================

class SafeView(discord.ui.View):

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ):

        logger.error(
            "View error in %s",
            type(self).__name__,
            exc_info=(
                type(error),
                error,
                error.__traceback__,
            ),
        )

        await send_interaction_message(
            interaction,
            "Something went wrong while processing that action.",
            ephemeral=True,
        )


class SafeModal(discord.ui.Modal):

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ):

        logger.error(
            "Modal error in %s",
            type(self).__name__,
            exc_info=(
                type(error),
                error,
                error.__traceback__,
            ),
        )

        await send_interaction_message(
            interaction,
            "Something went wrong while processing that form.",
            ephemeral=True,
        )


# ============================================================
# BOT
# ============================================================

class VouchBot(discord.Client):

    def __init__(self):

        super().__init__(
            intents=intents,
            status=discord.Status.online,
            activity=discord.Game(
                "Trust & Giveaways"
            ),
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
            if (
                Groq is not None
                and GROQ_API_KEY
            )
            else None
        )

        self.health_runner = None
        self.health_site = None
        self._ready_once = False

    # ========================================================
    # STARTUP
    # ========================================================

    async def setup_hook(self):

        await self.db.initialize_database()

        await self.db.execute(
            """
            UPDATE user_vouch_network
            SET trust_score = ?
            WHERE trust_score = 25
              AND vouches_given = 0
              AND vouch_positive = 0
              AND vouch_negative = 0
            """,
            (STARTING_TRUST,),
        )

        self.add_view(
            TrustPanelView(self)
        )

        await self.recover_giveaways()

        try:

            synced = await self.tree.sync()

            logger.info(
                "Synced %s application commands.",
                len(synced),
            )

        except Exception:

            logger.exception(
                "Failed to sync application commands."
            )

        self.giveaway_loop.start()
        self.maintenance_loop.start()

        await self.start_health_server()

    async def on_ready(self):

        logger.info(
            "Logged in as %s (%s)",
            self.user,
            self.user.id if self.user else "?",
        )

        if not self._ready_once:

            self._ready_once = True

            await self.ensure_all_members()

    async def close(self):

        if self.giveaway_loop.is_running():
            self.giveaway_loop.cancel()

        if self.maintenance_loop.is_running():
            self.maintenance_loop.cancel()

        await self.stop_health_server()

        await self.db.close()

        await super().close()

    # ========================================================
    # HEALTH SERVER
    # ========================================================

    async def start_health_server(self):

        if self.health_runner is not None:
            return

        app = web.Application()

        app.router.add_get(
            "/health",
            self.health,
        )

        app.router.add_get(
            "/api/status",
            self.api_status,
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
            "Health server listening on port %s",
            PORT,
        )

    async def stop_health_server(self):

        if self.health_runner is not None:

            await self.health_runner.cleanup()

            self.health_runner = None
            self.health_site = None

    async def health(
        self,
        request: web.Request,
    ):

        return web.json_response(
            {
                "status": "ok"
            }
        )

    async def api_status(
        self,
        request: web.Request,
    ):

        return web.json_response(
            {
                "status": "ok",
                "ready": self.is_ready(),
                "guilds": len(self.guilds),
                "latency_ms": round(
                    self.latency * 1000,
                    2,
                ),
                "database": (
                    self.db.connection is not None
                ),
            }
        )

    # ========================================================
    # USER / TRUST DATA
    # ========================================================

    async def ensure_user(
        self,
        guild_id: int,
        user_id: int,
    ):

        await self.db.execute(
            """
            INSERT INTO user_vouch_network (
                guild_id,
                user_id,
                trust_score,
                vouches_given,
                vouch_positive,
                vouch_negative
            )
            VALUES (
                ?,
                ?,
                ?,
                0,
                0,
                0
            )
            ON CONFLICT(
                guild_id,
                user_id
            )
            DO NOTHING
            """,
            (
                guild_id,
                user_id,
                STARTING_TRUST,
            ),
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
                ?,
                ?,
                0,
                0,
                0,
                0,
                NULL,
                NULL,
                NULL
            )
            ON CONFLICT(
                guild_id,
                user_id
            )
            DO NOTHING
            """,
            (
                guild_id,
                user_id,
            ),
        )

    async def ensure_all_members(self):

        for guild in self.guilds:

            members = [
                member
                for member in guild.members
                if not member.bot
            ]

            if not members:
                continue

            trust_rows = [
                (
                    guild.id,
                    member.id,
                    STARTING_TRUST,
                )
                for member in members
            ]

            await self.db.executemany(
                """
                INSERT INTO user_vouch_network (
                    guild_id,
                    user_id,
                    trust_score,
                    vouches_given,
                    vouch_positive,
                    vouch_negative
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    0,
                    0,
                    0
                )
                ON CONFLICT(
                    guild_id,
                    user_id
                )
                DO NOTHING
                """,
                trust_rows,
            )

            activity_rows = [
                (
                    guild.id,
                    member.id,
                )
                for member in members
            ]

            await self.db.executemany(
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
                    ?,
                    ?,
                    0,
                    0,
                    0,
                    0,
                    NULL,
                    NULL,
                    NULL
                )
                ON CONFLICT(
                    guild_id,
                    user_id
                )
                DO NOTHING
                """,
                activity_rows,
            )

    async def resolve_member(
        self,
        guild: discord.Guild,
        value: str,
    ) -> Optional[discord.Member]:

        value = value.strip()

        mention = re.fullmatch(
            r"<@!?(\d+)>",
            value,
        )

        if mention:
            value = mention.group(1)

        if value.isdigit():

            member_id = int(value)

            member = guild.get_member(
                member_id
            )

            if member:
                return member

            try:

                return await guild.fetch_member(
                    member_id
                )

            except discord.HTTPException:

                return None

        lowered = value.casefold()

        for member in guild.members:

            if member.bot:
                continue

            if member.name.casefold() == lowered:
                return member

            if member.display_name.casefold() == lowered:
                return member

        return None

    async def get_trust(
        self,
        guild_id: int,
        user_id: int,
    ):

        await self.ensure_user(
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

    async def update_trust_roles(
        self,
        member: discord.Member,
        score: int,
    ):

        trader_role = member.guild.get_role(
            TRADER_ROLE_ID
        )

        trusted_role = member.guild.get_role(
            TRUSTED_TRADER_ROLE_ID
        )

        if (
            trader_role is None
            and trusted_role is None
        ):
            return

        try:

            if trusted_role:

                if score >= TRUSTED_TRADER_THRESHOLD:

                    if trusted_role not in member.roles:

                        await member.add_roles(
                            trusted_role,
                            reason=(
                                "Trust score reached "
                                "trusted threshold"
                            ),
                        )

                elif trusted_role in member.roles:

                    await member.remove_roles(
                        trusted_role,
                        reason=(
                            "Trust score dropped "
                            "below trusted threshold"
                        ),
                    )

            if trader_role:

                if score >= TRADER_THRESHOLD:

                    if trader_role not in member.roles:

                        await member.add_roles(
                            trader_role,
                            reason=(
                                "Trust score reached "
                                "trader threshold"
                            ),
                        )

                elif trader_role in member.roles:

                    await member.remove_roles(
                        trader_role,
                        reason=(
                            "Trust score dropped "
                            "below trader threshold"
                        ),
                    )

        except discord.Forbidden:

            logger.warning(
                "Cannot update trust roles for %s",
                member,
            )

        except discord.HTTPException:

            logger.exception(
                "Failed to update trust roles for %s",
                member,
            )

    async def process_vouch(
        self,
        guild: discord.Guild,
        giver: discord.Member,
        target: discord.Member,
        vouch_type: str,
        reason: str,
    ):

        if giver.id == target.id:

            return (
                False,
                "You cannot vouch yourself.",
                None,
            )

        if giver.bot or target.bot:

            return (
                False,
                "Bots cannot participate in the vouch system.",
                None,
            )

        if vouch_type not in {
            "POSITIVE",
            "NEGATIVE",
        }:

            return (
                False,
                "Invalid vouch type.",
                None,
            )

        reason = (
            reason
            or "No reason provided."
        ).strip()[:1000]

        await self.ensure_user(
            guild.id,
            giver.id,
        )

        await self.ensure_user(
            guild.id,
            target.id,
        )

        existing = await self.db.fetchone(
            """
            SELECT vouch_type
            FROM vouch_history
            WHERE guild_id = ?
              AND target_id = ?
              AND giver_id = ?
            """,
            (
                guild.id,
                target.id,
                giver.id,
            ),
        )

        if existing:

            return (
                False,
                "You have already vouched this user.",
                None,
            )

        delta = (
            1
            if vouch_type == "POSITIVE"
            else -1
        )

        async with self.db.transaction() as conn:

            await conn.execute(
                """
                INSERT INTO vouch_history (
                    guild_id,
                    target_id,
                    giver_id,
                    vouch_type,
                    reason,
                    timestamp
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?
                )
                """,
                (
                    guild.id,
                    target.id,
                    giver.id,
                    vouch_type,
                    reason,
                    now_ts(),
                ),
            )

            await conn.execute(
                """
                INSERT INTO user_vouch_network (
                    guild_id,
                    user_id,
                    trust_score,
                    vouches_given,
                    vouch_positive,
                    vouch_negative
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    0,
                    ?,
                    ?
                )
                ON CONFLICT(
                    guild_id,
                    user_id
                )
                DO UPDATE SET
                    trust_score = MIN(
                        100,
                        MAX(
                            0,
                            user_vouch_network.trust_score + ?
                        )
                    ),
                    vouch_positive =
                        user_vouch_network.vouch_positive + ?,
                    vouch_negative =
                        user_vouch_network.vouch_negative + ?
                """,
                (
                    guild.id,
                    target.id,
                    clamp(
                        STARTING_TRUST + delta
                    ),
                    1
                    if vouch_type == "POSITIVE"
                    else 0,
                    1
                    if vouch_type == "NEGATIVE"
                    else 0,
                    delta,
                    1
                    if vouch_type == "POSITIVE"
                    else 0,
                    1
                    if vouch_type == "NEGATIVE"
                    else 0,
                ),
            )

            await conn.execute(
                """
                UPDATE user_vouch_network
                SET vouches_given =
                    vouches_given + 1
                WHERE guild_id = ?
                  AND user_id = ?
                """,
                (
                    guild.id,
                    giver.id,
                ),
            )

        row = await self.db.fetchone(
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
        )

        score = (
            int(row[0])
            if row
            else STARTING_TRUST
        )

        await self.update_trust_roles(
            target,
            score,
        )

        await self.log_transaction(
            guild,
            giver,
            target,
            vouch_type,
            reason,
            score,
        )

        return (
            True,
            "Vouch added successfully.",
            score,
        )

    async def log_transaction(
        self,
        guild: discord.Guild,
        giver: discord.Member,
        target: discord.Member,
        vouch_type: str,
        reason: str,
        score: int,
    ):

        row = await self.db.fetchone(
            """
            SELECT channel_id
            FROM transaction_log_config
            WHERE guild_id = ?
            """,
            (guild.id,),
        )

        if not row:
            return

        channel_id = int(row[0])

        channel = guild.get_channel(
            channel_id
        )

        if channel is None:

            try:

                channel = await guild.fetch_channel(
                    channel_id
                )

            except discord.HTTPException:

                return

        embed = discord.Embed(
            title="Trust Transaction",
            description=(
                f"**Giver:** {giver.mention}\n"
                f"**Target:** {target.mention}\n"
                f"**Type:** `{vouch_type}`\n"
                f"**Reason:** {reason}\n"
                f"**New trust score:** `{score}`"
            ),
            timestamp=datetime.now(
                timezone.utc
            ),
        )

        try:

            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        except discord.HTTPException:

            logger.exception(
                "Failed to write transaction log."
            )

    async def make_profile_embed(
        self,
        guild: discord.Guild,
        member: discord.Member,
    ) -> discord.Embed:

        row = await self.get_trust(
            guild.id,
            member.id,
        )

        if row:

            (
                score,
                given,
                positive,
                negative,
            ) = map(
                int,
                row,
            )

        else:

            # FIXED
            score = 0
            given = 0
            positive = 0
            negative = 0

        recent = await self.db.fetchall(
            """
            SELECT
                giver_id,
                vouch_type,
                reason,
                timestamp
            FROM vouch_history
            WHERE guild_id = ?
              AND target_id = ?
            ORDER BY timestamp DESC
            LIMIT 5
            """,
            (
                guild.id,
                member.id,
            ),
        )

        if score >= TRUSTED_TRADER_THRESHOLD:

            rank = "Trusted Trader"

        elif score >= TRADER_THRESHOLD:

            rank = "Trader"

        else:

            rank = "Unranked"

        embed = discord.Embed(
            title=(
                f"Trust Profile — "
                f"{member.display_name}"
            ),
            colour=(
                discord.Colour.green()
                if score >= TRUSTED_TRADER_THRESHOLD
                else discord.Colour.blue()
            ),
        )

        embed.set_thumbnail(
            url=member.display_avatar.url
        )

        embed.add_field(
            name="Trust Score",
            value=f"`{score}/100`",
            inline=True,
        )

        embed.add_field(
            name="Rank",
            value=f"`{rank}`",
            inline=True,
        )

        embed.add_field(
            name="Vouches Given",
            value=f"`{given}`",
            inline=True,
        )

        embed.add_field(
            name="Positive",
            value=f"`{positive}`",
            inline=True,
        )

        embed.add_field(
            name="Negative",
            value=f"`{negative}`",
            inline=True,
        )

        if recent:

            lines = []

            for (
                giver_id,
                v_type,
                vouch_reason,
                timestamp,
            ) in recent:

                sign = (
                    "+"
                    if v_type == "POSITIVE"
                    else "-"
                )

                giver = guild.get_member(
                    int(giver_id)
                )

                giver_name = (
                    giver.display_name
                    if giver
                    else f"User {giver_id}"
                )

                lines.append(
                    (
                        f"{sign} **"
                        f"{discord.utils.escape_markdown(giver_name)}"
                        f"** — "
                        f"{discord.utils.escape_markdown(vouch_reason or 'No reason')} "
                        f"{discord_time(float(timestamp))}"
                    )
                )

            embed.add_field(
                name="Recent Vouches",
                value="\n".join(lines)[:1024],
                inline=False,
            )

        else:

            embed.add_field(
                name="Recent Vouches",
                value="No vouches yet.",
                inline=False,
            )

        return embed

    # ========================================================
    # LEADERBOARD
    # ========================================================

    async def render_leaderboard(
        self,
        guild: discord.Guild,
        page: int = 0,
        per_page: int = 10,
    ):

        rows = await self.db.fetchall(
            """
            SELECT
                user_id,
                trust_score,
                vouch_positive,
                vouch_negative
            FROM user_vouch_network
            WHERE guild_id = ?
            ORDER BY
                trust_score DESC,
                user_id ASC
            """,
            (guild.id,),
        )

        total_pages = max(
            1,
            (
                len(rows)
                + per_page
                - 1
            )
            // per_page,
        )

        page = max(
            0,
            min(
                page,
                total_pages - 1,
            ),
        )

        page_rows = rows[
            page * per_page:
            (page + 1) * per_page
        ]

        if Image is None:

            embed = discord.Embed(
                title=(
                    f"Trust Leaderboard — "
                    f"Page {page + 1}/{total_pages}"
                )
            )

            if page_rows:

                lines = []

                for (
                    index,
                    (
                        user_id,
                        score,
                        positive,
                        negative,
                    ),
                ) in enumerate(
                    page_rows,
                    start=(
                        page
                        * per_page
                        + 1
                    ),
                ):

                    member = guild.get_member(
                        int(user_id)
                    )

                    name = (
                        member.display_name
                        if member
                        else f"User {user_id}"
                    )

                    lines.append(
                        (
                            f"**#{index}** "
                            f"{name} — "
                            f"`{score}` "
                            f"(+{positive}/-{negative})"
                        )
                    )

                embed.description = "\n".join(
                    lines
                )

            else:

                embed.description = (
                    "No trust data yet."
                )

            return (
                embed,
                None,
                total_pages,
            )

        width = 1200
        height = 760

        image = Image.new(
            "RGB",
            (
                width,
                height,
            ),
            (
                18,
                18,
                24,
            ),
        )

        draw = ImageDraw.Draw(
            image
        )

        def load_font(size: int):

            candidates = [
                (
                    "/usr/share/fonts/"
                    "truetype/dejavu/"
                    "DejaVuSans-Bold.ttf"
                ),
                (
                    "/usr/share/fonts/"
                    "truetype/dejavu/"
                    "DejaVuSans.ttf"
                ),
            ]

            for path in candidates:

                if not os.path.exists(path):
                    continue

                try:

                    return ImageFont.truetype(
                        path,
                        size,
                    )

                except Exception:
                    pass

            return ImageFont.load_default()

        title_font = load_font(42)
        row_font = load_font(28)
        small_font = load_font(22)

        draw.text(
            (
                50,
                35,
            ),
            f"Trust Leaderboard — {guild.name}",
            font=title_font,
            fill=(
                245,
                245,
                245,
            ),
        )

        draw.text(
            (
                50,
                90,
            ),
            f"Page {page + 1}/{total_pages}",
            font=small_font,
            fill=(
                175,
                175,
                185,
            ),
        )

        y = 150

        for (
            index,
            (
                user_id,
                score,
                positive,
                negative,
            ),
        ) in enumerate(
            page_rows,
            start=(
                page
                * per_page
                + 1
            ),
        ):

            member = guild.get_member(
                int(user_id)
            )

            name = (
                member.display_name
                if member
                else f"User {user_id}"
            )

            if len(name) > 24:
                name = (
                    name[:21]
                    + "..."
                )

            draw.rounded_rectangle(
                (
                    40,
                    y,
                    width - 40,
                    y + 56,
                ),
                radius=14,
                fill=(
                    35,
                    35,
                    45,
                ),
            )

            draw.text(
                (
                    60,
                    y + 10,
                ),
                f"#{index}",
                font=row_font,
                fill=(
                    255,
                    255,
                    255,
                ),
            )

            draw.text(
                (
                    145,
                    y + 11,
                ),
                name,
                font=row_font,
                fill=(
                    235,
                    235,
                    235,
                ),
            )

            draw.text(
                (
                    760,
                    y + 12,
                ),
                f"Score {score}",
                font=row_font,
                fill=(
                    255,
                    255,
                    255,
                ),
            )

            draw.text(
                (
                    950,
                    y + 13,
                ),
                (
                    f"+{positive} / "
                    f"-{negative}"
                ),
                font=small_font,
                fill=(
                    190,
                    190,
                    200,
                ),
            )

            y += 68

        if not page_rows:

            draw.text(
                (
                    50,
                    180,
                ),
                "No trust data yet.",
                font=row_font,
                fill=(
                    230,
                    230,
                    230,
                ),
            )

        data = io.BytesIO()

        image.save(
            data,
            format="PNG",
        )

        data.seek(0)

        file = discord.File(
            data,
            filename="leaderboard.png",
        )

        embed = discord.Embed(
            title=(
                f"Trust Leaderboard — "
                f"Page {page + 1}/{total_pages}"
            ),
        )

        embed.set_image(
            url="attachment://leaderboard.png"
        )

        return (
            embed,
            file,
            total_pages,
        )

    # ========================================================
    # GIVEAWAY RECOVERY
    # ========================================================

    async def recover_giveaways(self):

        rows = await self.db.fetchall(
            """
            SELECT
                message_id,
                status,
                ends_at,
                processing_started_at
            FROM giveaway_system
            WHERE status IN (
                'ACTIVE',
                'PROCESSING'
            )
            """
        )

        current = now_ts()

        for (
            message_id,
            status,
            ends_at,
            processing_started_at,
        ) in rows:

            message_id = int(
                message_id
            )

            try:

                self.add_view(
                    GiveawayJoinView(
                        self,
                        message_id,
                    ),
                    message_id=message_id,
                )

            except Exception:

                logger.exception(
                    "Failed to register giveaway view for %s",
                    message_id,
                )

            if (
                status == "PROCESSING"
                and current
                - float(
                    processing_started_at or 0
                )
                > 120
            ):

                await self.db.execute(
                    """
                    UPDATE giveaway_system
                    SET
                        status = 'ACTIVE',
                        retry_count =
                            retry_count + 1,
                        last_error = ?
                    WHERE message_id = ?
                      AND status = 'PROCESSING'
                    """,
                    (
                        "Recovered stale processing state during startup.",
                        message_id,
                    ),
                )

            if float(ends_at or 0) <= current:

                asyncio.create_task(
                    self.finish_giveaway(
                        message_id,
                        "startup recovery",
                    )
                )

    # ========================================================
    # GIVEAWAY FINISH
    # ========================================================

    async def finish_giveaway(
        self,
        message_id: int,
        reason: str = "scheduled",
    ) -> bool:

        row = await self.db.fetchone(
            """
            SELECT
                channel_id,
                guild_id,
                prize,
                winners,
                ends_at,
                host_id,
                status,
                req_daily,
                req_weekly,
                req_monthly,
                req_total,
                bypass_role_id,
                retry_count,
                end_color
            FROM giveaway_system
            WHERE message_id = ?
            """,
            (message_id,),
        )

        if not row:
            return False

        (
            channel_id,
            guild_id,
            prize,
            winners,
            ends_at,
            host_id,
            status,
            req_daily,
            req_weekly,
            req_monthly,
            req_total,
            bypass_role_id,
            retry_count,
            end_color,
        ) = row

        if status == "ENDED":
            return False

        if status == "PROCESSING":
            return False

        claimed = await self.db.execute(
            """
            UPDATE giveaway_system
            SET
                status = 'PROCESSING',
                processing_started_at = ?,
                last_error = NULL
            WHERE message_id = ?
              AND status = 'ACTIVE'
            """,
            (
                now_ts(),
                message_id,
            ),
        )

        if claimed != 1:
            return False

        try:

            guild = self.get_guild(
                int(guild_id)
            )

            if guild is None:

                guild = await self.fetch_guild(
                    int(guild_id)
                )

            channel = guild.get_channel(
                int(channel_id)
            )

            if channel is None:

                channel = await guild.fetch_channel(
                    int(channel_id)
                )

            participant_rows = await self.db.fetchall(
                """
                SELECT user_id
                FROM giveaway_participants
                WHERE message_id = ?
                """,
                (message_id,),
            )

            participant_ids = [
                int(row[0])
                for row in participant_rows
            ]

            eligible = []

            for user_id in participant_ids:

                member = guild.get_member(
                    user_id
                )

                if member is None:

                    try:

                        member = await guild.fetch_member(
                            user_id
                        )

                    except discord.HTTPException:

                        continue

                if member.bot:
                    continue

                bypass = (
                    int(bypass_role_id or 0) > 0
                    and any(
                        role.id == int(bypass_role_id)
                        for role in member.roles
                    )
                )

                if bypass:

                    eligible.append(
                        member
                    )

                    continue

                activity = await self.db.fetchone(
                    """
                    SELECT
                        daily_message_count,
                        week_message_count,
                        month_message_count,
                        message_count
                    FROM user_activity
                    WHERE guild_id = ?
                      AND user_id = ?
                    """,
                    (
                        guild.id,
                        user_id,
                    ),
                )

                if activity:

                    (
                        daily,
                        weekly,
                        monthly,
                        total,
                    ) = map(
                        int,
                        activity,
                    )

                else:

                    daily = 0
                    weekly = 0
                    monthly = 0
                    total = 0

                if (
                    daily >= int(req_daily or 0)
                    and weekly >= int(req_weekly or 0)
                    and monthly >= int(req_monthly or 0)
                    and total >= int(req_total or 0)
                ):

                    eligible.append(
                        member
                    )

            winners_count = max(
                1,
                int(winners),
            )

            selected = (
                random.sample(
                    eligible,
                    min(
                        winners_count,
                        len(eligible),
                    ),
                )
                if eligible
                else []
            )

            winner_mentions = [
                member.mention
                for member in selected
            ]

            winner_ids = [
                member.id
                for member in selected
            ]

            winners_text = json.dumps(
                winner_ids
            )

            history_winners = (
                ", ".join(
                    winner_mentions
                )
                if winner_mentions
                else "No eligible winners"
            )

            completed_at = now_ts()

            await self.db.execute(
                """
                INSERT INTO giveaway_history (
                    message_id,
                    guild_id,
                    prize,
                    participant_count,
                    winners,
                    completed_at
                )
                VALUES (
                    ?,
                    ?,
                    ?,
                    ?,
                    ?,
                    ?
                )
                ON CONFLICT(message_id)
                DO UPDATE SET
                    participant_count =
                        excluded.participant_count,
                    winners =
                        excluded.winners,
                    completed_at =
                        excluded.completed_at
                """,
                (
                    message_id,
                    guild.id,
                    str(prize),
                    len(participant_ids),
                    winners_text,
                    completed_at,
                ),
            )

            await self.db.execute(
                """
                UPDATE giveaway_system
                SET
                    status = 'ENDED',
                    processing_started_at = 0,
                    result_message_id = 0,
                    result_winners = ?,
                    result_participant_count = ?,
                    last_error = NULL
                WHERE message_id = ?
                """,
                (
                    winners_text,
                    len(participant_ids),
                    message_id,
                ),
            )

            result_embed = discord.Embed(
                title="Giveaway Ended",
                description=(
                    f"**Prize:** {prize}\n"
                    f"**Participants:** "
                    f"`{len(participant_ids)}`\n"
                    f"**Winners:** "
                    f"{history_winners}"
                ),
                colour=parse_color(
                    end_color
                ),
            )

            result_message = await channel.send(
                content=(
                    "Congratulations!"
                    if selected
                    else None
                ),
                embed=result_embed,
                allowed_mentions=discord.AllowedMentions(
                    users=True
                ),
            )

            await self.db.execute(
                """
                UPDATE giveaway_system
                SET result_message_id = ?
                WHERE message_id = ?
                """,
                (
                    result_message.id,
                    message_id,
                ),
            )

            try:

                original = await channel.fetch_message(
                    message_id
                )

                ended_view = GiveawayJoinView(
                    self,
                    message_id,
                    disabled=True,
                )

                ended_embed = discord.Embed(
                    title="Giveaway Ended",
                    description=(
                        f"**Prize:** {prize}\n"
                        f"**Winners:** {history_winners}\n"
                        f"**Ended:** "
                        f"{discord_time(completed_at)}"
                    ),
                    colour=parse_color(
                        end_color
                    ),
                )

                await original.edit(
                    embed=ended_embed,
                    view=ended_view,
                )

            except discord.HTTPException:

                logger.warning(
                    "Could not edit ended giveaway message %s",
                    message_id,
                )

            logger.info(
                "Finished giveaway %s (%s)",
                message_id,
                reason,
            )

            return True

        except Exception as exc:

            logger.exception(
                "Failed to finish giveaway %s",
                message_id,
            )

            await self.db.execute(
                """
                UPDATE giveaway_system
                SET
                    status = 'ACTIVE',
                    processing_started_at = 0,
                    retry_count =
                        retry_count + 1,
                    last_error = ?
                WHERE message_id = ?
                """,
                (
                    str(exc)[:1000],
                    message_id,
                ),
            )

            return False

    # ========================================================
    # GIVEAWAY LOOP
    # ========================================================

    @tasks.loop(seconds=15)
    async def giveaway_loop(self):

        rows = await self.db.fetchall(
            """
            SELECT message_id
            FROM giveaway_system
            WHERE status = 'ACTIVE'
              AND ends_at <= ?
            ORDER BY ends_at ASC
            LIMIT 25
            """,
            (now_ts(),),
        )

        for (
            message_id,
        ) in rows:

            await self.finish_giveaway(
                int(message_id)
            )

    @giveaway_loop.before_loop
    async def before_giveaway_loop(self):

        await self.wait_until_ready()

    # ========================================================
    # MAINTENANCE LOOP
    # ========================================================

    @tasks.loop(seconds=60)
    async def maintenance_loop(self):

        rows = await self.db.fetchall(
            """
            SELECT
                guild_id,
                target_id,
                expiry_timestamp
            FROM temporary_bans
            WHERE expiry_timestamp <= ?
            """,
            (now_ts(),),
        )

        for (
            guild_id,
            target_id,
            expiry,
        ) in rows:

            guild = self.get_guild(
                int(guild_id)
            )

            if guild is not None:

                try:

                    await guild.unban(
                        discord.Object(
                            id=int(target_id)
                        ),
                        reason="Temporary ban expired",
                    )

                except discord.NotFound:
                    pass

                except discord.HTTPException:

                    logger.exception(
                        "Failed to unban %s from %s",
                        target_id,
                        guild_id,
                    )

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

    @maintenance_loop.before_loop
    async def before_maintenance_loop(self):

        await self.wait_until_ready()

    # ========================================================
    # ACTIVITY
    # ========================================================

    async def update_activity(
        self,
        message: discord.Message,
    ):

        if (
            message.guild is None
            or message.author.bot
        ):
            return

        guild_id = message.guild.id
        user_id = message.author.id

        now = datetime.now(
            timezone.utc
        )

        day_key = now.strftime(
            "%Y-%m-%d"
        )

        week_key = now.strftime(
            "%G-W%V"
        )

        month_key = now.strftime(
            "%Y-%m"
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
                ?,
                ?,
                1,
                1,
                1,
                1,
                ?,
                ?,
                ?
            )
            ON CONFLICT(
                guild_id,
                user_id
            )
            DO UPDATE SET

                message_count =
                    user_activity.message_count + 1,

                daily_message_count =
                    CASE
                        WHEN
                            user_activity.last_daily_date = ?
                        THEN
                            user_activity.daily_message_count + 1
                        ELSE
                            1
                    END,

                week_message_count =
                    CASE
                        WHEN
                            user_activity.last_weekly_date = ?
                        THEN
                            user_activity.week_message_count + 1
                        ELSE
                            1
                    END,

                month_message_count =
                    CASE
                        WHEN
                            user_activity.last_monthly_date = ?
                        THEN
                            user_activity.month_message_count + 1
                        ELSE
                            1
                    END,

                last_daily_date = ?,
                last_weekly_date = ?,
                last_monthly_date = ?
            """,
            (
                guild_id,
                user_id,
                day_key,
                week_key,
                month_key,
                day_key,
                week_key,
                month_key,
                day_key,
                week_key,
                month_key,
            ),
        )

    # ========================================================
    # GROQ AI
    # ========================================================

    async def ai_reply(
        self,
        message: discord.Message,
    ):

        if self.groq is None:
            return

        if self.user is None:
            return

        if self.user not in message.mentions:
            return

        if message.content.startswith("/"):
            return

        prompt = re.sub(
            rf"<@!?{self.user.id}>",
            "",
            message.content,
        ).strip()

        if not prompt:

            prompt = (
                "Say hello and ask what "
                "the user needs help with."
            )

        prompt = prompt[:3000]

        try:

            response = await asyncio.to_thread(
                self.groq.chat.completions.create,
                model=GROQ_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a concise, helpful "
                            "Discord bot. Answer clearly "
                            "and never pretend to have "
                            "abilities you do not have."
                        ),
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
                temperature=0.4,
                max_tokens=700,
            )

            content = (
                response
                .choices[0]
                .message
                .content
                .strip()
            )

            if content:

                await message.reply(
                    content[:2000],
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        except Exception:

            logger.exception(
                "Groq request failed."
            )

    # ========================================================
    # MEMBER EVENTS
    # ========================================================

    async def on_member_join(
        self,
        member: discord.Member,
    ):

        await self.ensure_user(
            member.guild.id,
            member.id,
        )

        row = await self.db.fetchone(
            """
            SELECT expiry_timestamp
            FROM temporary_bans
            WHERE guild_id = ?
              AND target_id = ?
            """,
            (
                member.guild.id,
                member.id,
            ),
        )

        if not row:
            return

        expiry = float(row[0])

        if expiry > now_ts():

            try:

                await member.guild.ban(
                    member,
                    reason="Active temporary ban",
                    delete_message_seconds=0,
                )

            except discord.HTTPException:

                logger.exception(
                    "Failed to re-ban member %s",
                    member.id,
                )

        else:

            await self.db.execute(
                """
                DELETE FROM temporary_bans
                WHERE guild_id = ?
                  AND target_id = ?
                """,
                (
                    member.guild.id,
                    member.id,
                ),
            )

    async def on_message(
        self,
        message: discord.Message,
    ):

        if message.author.bot:
            return

        try:

            await self.update_activity(
                message
            )

        except Exception:

            logger.exception(
                "Failed to record message activity."
            )

        await self.ai_reply(
            message
        )

    # ========================================================
    # APP COMMAND ERROR
    # ========================================================

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):

        original = getattr(
            error,
            "original",
            error,
        )

        logger.error(
            "Application command error.",
            exc_info=(
                type(original),
                original,
                original.__traceback__,
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

        elif isinstance(
            original,
            discord.Forbidden,
        ):

            message = (
                "I do not have permission "
                "to perform that action."
            )

        else:

            message = (
                "An unexpected error occurred "
                "while running that command."
            )

        await send_interaction_message(
            interaction,
            message,
            ephemeral=True,
        )


# ============================================================
# TRUST PANEL
# ============================================================

class TrustPanelView(SafeView):

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
        style=discord.ButtonStyle.primary,
        custom_id="trust_panel:profile",
    )
    async def my_profile(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if interaction.guild is None:

            await send_interaction_message(
                interaction,
                "This panel only works inside a server.",
            )

            return

        await acknowledge(
            interaction
        )

        embed = await self.bot.make_profile_embed(
            interaction.guild,
            interaction.user,
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Check User's Vouch",
        style=discord.ButtonStyle.secondary,
        custom_id="trust_panel:check",
    )
    async def check_user(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if interaction.guild is None:

            await send_interaction_message(
                interaction,
                "This panel only works inside a server.",
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
        style=discord.ButtonStyle.success,
        custom_id="trust_panel:vouch",
    )
    async def vouch_user(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if interaction.guild is None:

            await send_interaction_message(
                interaction,
                "This panel only works inside a server.",
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
        custom_id="trust_panel:rewards",
    )
    async def rewards(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        embed = discord.Embed(
            title="Vouch Rewards",
            description=(
                f"**{TRADER_THRESHOLD} trust** → Trader role\n"
                f"**{TRUSTED_TRADER_THRESHOLD} trust** → Trusted Trader role\n\n"
                "Positive vouches increase trust. "
                "Negative vouches reduce trust."
            ),
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Vouch Leaderboard",
        style=discord.ButtonStyle.primary,
        custom_id="trust_panel:leaderboard",
    )
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if interaction.guild is None:

            await send_interaction_message(
                interaction,
                "This panel only works inside a server.",
            )

            return

        await acknowledge(
            interaction
        )

        (
            embed,
            file,
            total_pages,
        ) = await self.bot.render_leaderboard(
            interaction.guild,
            0,
        )

        view = VouchLeaderboardView(
            self.bot,
            0,
            total_pages,
        )

        await interaction.followup.send(
            embed=embed,
            file=file,
            view=view,
            ephemeral=True,
        )


# ============================================================
# VOUCH TARGET VIEW
# ============================================================

class VouchTargetView(SafeView):

    def __init__(
        self,
        bot: VouchBot,
    ):

        super().__init__(
            timeout=300
        )

        self.bot = bot

        self.add_item(
            VouchUserSelect(
                bot
            )
        )

    @discord.ui.button(
        label="Enter Name / ID",
        style=discord.ButtonStyle.secondary,
    )
    async def manual(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        await interaction.response.send_modal(
            VouchMemberModal(
                self.bot
            )
        )


class VouchUserSelect(
    discord.ui.UserSelect
):

    def __init__(
        self,
        bot: VouchBot,
    ):

        super().__init__(
            placeholder="Select a user to vouch",
            min_values=1,
            max_values=1,
        )

        self.bot = bot

    async def callback(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:

            await send_interaction_message(
                interaction,
                "This panel only works inside a server.",
            )

            return

        await acknowledge(
            interaction
        )

        target = self.values[0]

        member = interaction.guild.get_member(
            target.id
        )

        if member is None:

            try:

                member = await interaction.guild.fetch_member(
                    target.id
                )

            except discord.HTTPException:

                member = None

        if member is None:

            await interaction.followup.send(
                "I could not find that member.",
                ephemeral=True,
            )

            return

        if member.bot:

            await interaction.followup.send(
                "Bots cannot be vouched.",
                ephemeral=True,
            )

            return

        await interaction.followup.send(
            (
                f"You selected {member.mention}. "
                "Choose a vouch type:"
            ),
            view=VouchTypeView(
                self.bot,
                member.id,
            ),
            ephemeral=True,
        )


# ============================================================
# VOUCH MEMBER MODAL
# ============================================================

class VouchMemberModal(
    SafeModal
):

    def __init__(
        self,
        bot: VouchBot,
    ):

        super().__init__(
            title="Find User"
        )

        self.bot = bot

        self.value = discord.ui.TextInput(
            label="Name, mention or ID",
            placeholder=(
                "e.g. 123456789012345678"
            ),
            required=True,
            max_length=100,
        )

        self.add_item(
            self.value
        )

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:

            await send_interaction_message(
                interaction,
                "This form only works inside a server.",
            )

            return

        await acknowledge(
            interaction
        )

        member = await self.bot.resolve_member(
            interaction.guild,
            str(self.value.value),
        )

        if member is None:

            await interaction.followup.send(
                "I could not find that user.",
                ephemeral=True,
            )

            return

        if member.bot:

            await interaction.followup.send(
                "Bots cannot be vouched.",
                ephemeral=True,
            )

            return

        await interaction.followup.send(
            (
                f"You selected {member.mention}. "
                "Choose a vouch type:"
            ),
            view=VouchTypeView(
                self.bot,
                member.id,
            ),
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
            timeout=300
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
            title="Vouch Reason"
        )

        self.bot = bot
        self.target_id = target_id
        self.vouch_type = vouch_type

        self.reason = discord.ui.TextInput(
            label="Reason",
            style=discord.TextStyle.paragraph,
            placeholder=(
                "Explain why you are giving this vouch."
            ),
            required=True,
            min_length=2,
            max_length=1000,
        )

        self.add_item(
            self.reason
        )

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:

            await send_interaction_message(
                interaction,
                "This form only works inside a server.",
            )

            return

        await acknowledge(
            interaction
        )

        target = interaction.guild.get_member(
            self.target_id
        )

        if target is None:

            try:

                target = await interaction.guild.fetch_member(
                    self.target_id
                )

            except discord.HTTPException:

                target = None

        if target is None:

            await interaction.followup.send(
                "That user is no longer in the server.",
                ephemeral=True,
            )

            return

        confirm_embed = discord.Embed(
            title="Confirm Vouch",
            description=(
                f"**Target:** {target.mention}\n"
                f"**Type:** "
                f"{'Positive' if self.vouch_type == 'POSITIVE' else 'Negative'}\n"
                f"**Reason:** "
                f"{discord.utils.escape_markdown(str(self.reason.value))}"
            ),
        )

        await interaction.followup.send(
            embed=confirm_embed,
            view=VouchConfirmView(
                self.bot,
                interaction.user.id,
                target.id,
                self.vouch_type,
                str(self.reason.value),
            ),
            ephemeral=True,
        )


# ============================================================
# VOUCH CONFIRMATION
# ============================================================

class VouchConfirmView(
    SafeView
):

    def __init__(
        self,
        bot: VouchBot,
        requester_id: int,
        target_id: int,
        vouch_type: str,
        reason: str,
    ):

        super().__init__(
            timeout=180
        )

        self.bot = bot
        self.requester_id = requester_id
        self.target_id = target_id
        self.vouch_type = vouch_type
        self.reason = reason

    def authorized(
        self,
        interaction: discord.Interaction,
    ) -> bool:

        return (
            interaction.user.id
            == self.requester_id
        )

    @discord.ui.button(
        label="Confirm Vouch",
        style=discord.ButtonStyle.success,
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if not self.authorized(
            interaction
        ):

            await interaction.response.send_message(
                (
                    "This confirmation belongs "
                    "to another user."
                ),
                ephemeral=True,
            )

            return

        if (
            interaction.guild is None
            or not isinstance(
                interaction.user,
                discord.Member,
            )
        ):

            await send_interaction_message(
                interaction,
                "This action only works inside a server.",
            )

            return

        await interaction.response.defer(
            thinking=False
        )

        target = interaction.guild.get_member(
            self.target_id
        )

        if target is None:

            try:

                target = await interaction.guild.fetch_member(
                    self.target_id
                )

            except discord.HTTPException:

                target = None

        if target is None:

            await interaction.edit_original_response(
                content=(
                    "That user is no longer "
                    "in the server."
                ),
                embed=None,
                view=None,
            )

            return

        (
            success,
            message,
            score,
        ) = await self.bot.process_vouch(
            interaction.guild,
            interaction.user,
            target,
            self.vouch_type,
            self.reason,
        )

        for child in self.children:
            child.disabled = True

        if success:

            await interaction.edit_original_response(
                content=(
                    f"✅ {message}\n"
                    f"New trust score for "
                    f"{target.mention}: `{score}`."
                ),
                embed=None,
                view=self,
            )

        else:

            await interaction.edit_original_response(
                content=f"❌ {message}",
                embed=None,
                view=self,
            )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if not self.authorized(
            interaction
        ):

            await interaction.response.send_message(
                (
                    "This confirmation belongs "
                    "to another user."
                ),
                ephemeral=True,
            )

            return

        await interaction.response.edit_message(
            content="Vouch cancelled.",
            embed=None,
            view=None,
        )


# ============================================================
# CHECK MEMBER VIEW
# ============================================================

class CheckMemberView(
    SafeView
):

    def __init__(
        self,
        bot: VouchBot,
    ):

        super().__init__(
            timeout=300
        )

        self.bot = bot

        self.add_item(
            CheckUserSelect(
                bot
            )
        )

    @discord.ui.button(
        label="Enter Name / ID",
        style=discord.ButtonStyle.secondary,
    )
    async def manual(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        await interaction.response.send_modal(
            CheckMemberModal(
                self.bot
            )
        )


class CheckUserSelect(
    discord.ui.UserSelect
):

    def __init__(
        self,
        bot: VouchBot,
    ):

        super().__init__(
            placeholder="Select a user to check",
            min_values=1,
            max_values=1,
        )

        self.bot = bot

    async def callback(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:

            await send_interaction_message(
                interaction,
                "This panel only works inside a server.",
            )

            return

        await acknowledge(
            interaction
        )

        target = interaction.guild.get_member(
            self.values[0].id
        )

        if target is None:

            try:

                target = await interaction.guild.fetch_member(
                    self.values[0].id
                )

            except discord.HTTPException:

                target = None

        if target is None:

            await interaction.followup.send(
                "I could not find that user.",
                ephemeral=True,
            )

            return

        embed = await self.bot.make_profile_embed(
            interaction.guild,
            target,
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )


# ============================================================
# CHECK MEMBER MODAL
# ============================================================

class CheckMemberModal(
    SafeModal
):

    def __init__(
        self,
        bot: VouchBot,
    ):

        super().__init__(
            title="Check User"
        )

        self.bot = bot

        self.value = discord.ui.TextInput(
            label="Name, mention or ID",
            required=True,
            max_length=100,
        )

        self.add_item(
            self.value
        )

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):

        if interaction.guild is None:

            await send_interaction_message(
                interaction,
                "This form only works inside a server.",
            )

            return

        await acknowledge(
            interaction
        )

        member = await self.bot.resolve_member(
            interaction.guild,
            str(self.value.value),
        )

        if member is None:

            await interaction.followup.send(
                "I could not find that user.",
                ephemeral=True,
            )

            return

        embed = await self.bot.make_profile_embed(
            interaction.guild,
            member,
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )


# ============================================================
# LEADERBOARD VIEW
# ============================================================

class VouchLeaderboardView(
    SafeView
):

    def __init__(
        self,
        bot: VouchBot,
        page: int,
        total_pages: int,
    ):

        super().__init__(
            timeout=300
        )

        self.bot = bot
        self.page = page
        self.total_pages = total_pages

        self._update_buttons()

    def _update_buttons(self):

        self.previous.disabled = (
            self.page <= 0
        )

        self.next_page.disabled = (
            self.page >= self.total_pages - 1
        )

    @discord.ui.button(
        label="Previous",
        style=discord.ButtonStyle.secondary,
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if interaction.guild is None:
            return

        await interaction.response.defer(
            thinking=False
        )

        self.page = max(
            0,
            self.page - 1,
        )

        (
            embed,
            file,
            total_pages,
        ) = await self.bot.render_leaderboard(
            interaction.guild,
            self.page,
        )

        self.total_pages = total_pages

        self._update_buttons()

        await interaction.edit_original_response(
            embed=embed,
            attachments=(
                [file]
                if file is not None
                else []
            ),
            view=self,
        )

    @discord.ui.button(
        label="Next",
        style=discord.ButtonStyle.primary,
    )
    async def next_page(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if interaction.guild is None:
            return

        await interaction.response.defer(
            thinking=False
        )

        self.page = min(
            self.total_pages - 1,
            self.page + 1,
        )

        (
            embed,
            file,
            total_pages,
        ) = await self.bot.render_leaderboard(
            interaction.guild,
            self.page,
        )

        self.total_pages = total_pages

        self._update_buttons()

        await interaction.edit_original_response(
            embed=embed,
            attachments=(
                [file]
                if file is not None
                else []
            ),
            view=self,
        )


# ============================================================
# GIVEAWAY JOIN VIEW
# ============================================================

class GiveawayJoinView(
    SafeView
):

    def __init__(
        self,
        bot: VouchBot,
        message_id: int,
        disabled: bool = False,
    ):

        super().__init__(
            timeout=None
        )

        self.bot = bot
        self.message_id = int(
            message_id
        )

        button = discord.ui.Button(
            label="Join Giveaway",
            emoji="🎉",
            style=discord.ButtonStyle.success,
            custom_id=(
                f"giveaway:join:"
                f"{self.message_id}"
            ),
            disabled=disabled,
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
                (
                    "This giveaway is only "
                    "available in a server."
                ),
                ephemeral=True,
            )

            return

        await acknowledge(
            interaction
        )

        row = await self.bot.db.fetchone(
            """
            SELECT
                status,
                ends_at
            FROM giveaway_system
            WHERE message_id = ?
              AND guild_id = ?
            """,
            (
                self.message_id,
                interaction.guild.id,
            ),
        )

        if not row:

            await interaction.followup.send(
                "This giveaway no longer exists.",
                ephemeral=True,
            )

            return

        status, ends_at = row

        if (
            status != "ACTIVE"
            or float(ends_at or 0) <= now_ts()
        ):

            await interaction.followup.send(
                "This giveaway has already ended.",
                ephemeral=True,
            )

            return

        inserted = await self.bot.db.execute(
            """
            INSERT OR IGNORE INTO giveaway_participants (
                message_id,
                user_id
            )
            VALUES (
                ?,
                ?
            )
            """,
            (
                self.message_id,
                interaction.user.id,
            ),
        )

        if inserted == 1:

            await interaction.followup.send(
                "✅ You are entered in the giveaway!",
                ephemeral=True,
            )

        else:

            await interaction.followup.send(
                "You are already entered in this giveaway.",
                ephemeral=True,
            )


# ============================================================
# BOT INSTANCE
# ============================================================

bot = VouchBot()


# ============================================================
# GIVEAWAY COMMAND GROUP
# ============================================================

giveaway_group = app_commands.Group(
    name="giveaway",
    description="Create and manage giveaways",
)

bot.tree.add_command(
    giveaway_group
)


# ============================================================
# /VOUCHPANEL
# ============================================================

@bot.tree.command(
    name="vouchpanel",
    description="Post the trust and vouch panel",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(
    manage_guild=True
)
async def vouchpanel(
    interaction: discord.Interaction,
):

    await acknowledge(
        interaction,
        ephemeral=True,
    )

    embed = discord.Embed(
        title="Trust & Vouch System",
        description=(
            "Use the buttons below to view trust profiles, "
            "check vouches, give vouches, view rewards, "
            "and open the trust leaderboard."
        ),
    )

    await interaction.channel.send(
        embed=embed,
        view=TrustPanelView(bot),
    )

    await interaction.followup.send(
        "✅ Trust panel posted.",
        ephemeral=True,
    )


# ============================================================
# /ACTIVITY
# ============================================================

@bot.tree.command(
    name="activity",
    description="View a user's activity counters",
)
@app_commands.guild_only()
async def activity(
    interaction: discord.Interaction,
    member: Optional[discord.Member] = None,
):

    await acknowledge(
        interaction
    )

    member = (
        member
        or interaction.user
    )

    await bot.ensure_user(
        interaction.guild.id,
        member.id,
    )

    row = await bot.db.fetchone(
        """
        SELECT
            message_count,
            daily_message_count,
            week_message_count,
            month_message_count
        FROM user_activity
        WHERE guild_id = ?
          AND user_id = ?
        """,
        (
            interaction.guild.id,
            member.id,
        ),
    )

    if row:

        (
            total,
            daily,
            weekly,
            monthly,
        ) = map(
            int,
            row,
        )

    else:

        total = 0
        daily = 0
        weekly = 0
        monthly = 0

    embed = discord.Embed(
        title=(
            f"Activity — "
            f"{member.display_name}"
        )
    )

    embed.add_field(
        name="Total",
        value=f"`{total}`",
        inline=True,
    )

    embed.add_field(
        name="Today",
        value=f"`{daily}`",
        inline=True,
    )

    embed.add_field(
        name="This Week",
        value=f"`{weekly}`",
        inline=True,
    )

    embed.add_field(
        name="This Month",
        value=f"`{monthly}`",
        inline=True,
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True,
    )


# ============================================================
# /BOTSTATS
# ============================================================

@bot.tree.command(
    name="botstats",
    description="View bot status",
)
async def botstats(
    interaction: discord.Interaction,
):

    await acknowledge(
        interaction
    )

    embed = discord.Embed(
        title="Bot Statistics"
    )

    embed.add_field(
        name="Guilds",
        value=f"`{len(bot.guilds)}`",
        inline=True,
    )

    embed.add_field(
        name="Latency",
        value=(
            f"`{round(bot.latency * 1000, 1)} ms`"
        ),
        inline=True,
    )

    embed.add_field(
        name="Database",
        value=(
            "`Connected`"
            if bot.db.connection
            else "`Disconnected`"
        ),
        inline=True,
    )

    embed.add_field(
        name="Python",
        value=(
            f"`{os.sys.version.split()[0]}`"
        ),
        inline=True,
    )

    await interaction.followup.send(
        embed=embed,
        ephemeral=True,
    )


# ============================================================
# /SAY
# ============================================================

@bot.tree.command(
    name="say",
    description="Send a message as the bot",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(
    manage_messages=True
)
async def say(
    interaction: discord.Interaction,
    message: str,
):

    await acknowledge(
        interaction
    )

    await interaction.channel.send(
        message,
        allowed_mentions=discord.AllowedMentions.none(),
    )

    await interaction.followup.send(
        "✅ Message sent.",
        ephemeral=True,
    )


# ============================================================
# /SYNC
# ============================================================

@bot.tree.command(
    name="sync",
    description="Sync application commands",
)
@owner_only()
async def sync_commands(
    interaction: discord.Interaction,
):

    await acknowledge(
        interaction
    )

    synced = await bot.tree.sync()

    await interaction.followup.send(
        (
            f"✅ Synced "
            f"{len(synced)} commands."
        ),
        ephemeral=True,
    )


# ============================================================
# /TRANSACTIONLOG
# ============================================================

@bot.tree.command(
    name="transactionlog",
    description="Configure the trust transaction log channel",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(
    manage_guild=True
)
async def transactionlog(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
):

    await acknowledge(
        interaction
    )

    if channel is None:

        row = await bot.db.fetchone(
            """
            SELECT channel_id
            FROM transaction_log_config
            WHERE guild_id = ?
            """,
            (
                interaction.guild.id,
            ),
        )

        if not row:

            await interaction.followup.send(
                (
                    "No transaction log "
                    "channel is configured."
                ),
                ephemeral=True,
            )

            return

        configured = (
            interaction.guild.get_channel(
                int(row[0])
            )
        )

        await interaction.followup.send(
            (
                "Current transaction log: "
                f"{configured.mention if configured else row[0]}"
            ),
            ephemeral=True,
        )

        return

    await bot.db.execute(
        """
        INSERT INTO transaction_log_config (
            guild_id,
            channel_id
        )
        VALUES (
            ?,
            ?
        )
        ON CONFLICT(
            guild_id
        )
        DO UPDATE SET
            channel_id = excluded.channel_id
        """,
        (
            interaction.guild.id,
            channel.id,
        ),
    )

    await interaction.followup.send(
        (
            "✅ Transaction log set to "
            f"{channel.mention}."
        ),
        ephemeral=True,
    )


# ============================================================
# /TEMPBAN
# ============================================================

@bot.tree.command(
    name="tempban",
    description="Temporarily ban a member",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(
    ban_members=True
)
async def tempban(
    interaction: discord.Interaction,
    member: discord.Member,
    duration: str,
    reason: str = "No reason provided.",
):

    seconds = parse_duration(
        duration
    )

    if seconds is None:

        await interaction.response.send_message(
            (
                "Invalid duration. "
                "Examples: `30m`, `2h`, `7d`."
            ),
            ephemeral=True,
        )

        return

    await acknowledge(
        interaction
    )

    expiry = (
        now_ts()
        + seconds
    )

    await interaction.guild.ban(
        member,
        reason=reason[:500],
        delete_message_seconds=0,
    )

    await bot.db.execute(
        """
        INSERT INTO temporary_bans (
            guild_id,
            target_id,
            expiry_timestamp
        )
        VALUES (
            ?,
            ?,
            ?
        )
        ON CONFLICT(
            guild_id,
            target_id
        )
        DO UPDATE SET
            expiry_timestamp =
                excluded.expiry_timestamp
        """,
        (
            interaction.guild.id,
            member.id,
            expiry,
        ),
    )

    await interaction.followup.send(
        (
            f"✅ {member} was temporarily banned "
            f"until {discord_time(expiry)}."
        ),
        ephemeral=True,
    )


# ============================================================
# /GIVEAWAY CREATE
# ============================================================

@giveaway_group.command(
    name="create",
    description="Create a giveaway",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(
    manage_guild=True
)
async def giveaway_create(
    interaction: discord.Interaction,
    duration: str,
    winners: app_commands.Range[int, 1, 50],
    prize: str,
    req_daily: app_commands.Range[int, 0, 100000] = 0,
    req_weekly: app_commands.Range[int, 0, 100000] = 0,
    req_monthly: app_commands.Range[int, 0, 100000] = 0,
    req_total: app_commands.Range[int, 0, 1000000] = 0,
    bypass_role: Optional[discord.Role] = None,
    end_color: Optional[str] = None,
):

    seconds = parse_duration(
        duration
    )

    if seconds is None:

        await interaction.response.send_message(
            (
                "Invalid duration. "
                "Examples: `30m`, `2h`, `3d`, `1w`."
            ),
            ephemeral=True,
        )

        return

    if len(
        prize.strip()
    ) > 200:

        await interaction.response.send_message(
            "Prize is too long.",
            ephemeral=True,
        )

        return

    await acknowledge(
        interaction,
        ephemeral=False,
    )

    ends_at = (
        now_ts()
        + seconds
    )

    host = interaction.user

    color_value = (
        end_color.strip()
        if end_color
        else None
    )

    embed = discord.Embed(
        title="🎉 Giveaway",
        description=(
            f"**Prize:** {prize}\n"
            f"**Winners:** `{winners}`\n"
            f"**Ends:** {discord_time(ends_at)}\n"
            f"**Hosted by:** {host.mention}"
        ),
        colour=parse_color(
            color_value
        ),
    )

    requirements = []

    if req_daily:
        requirements.append(
            f"Daily messages: `{req_daily}`"
        )

    if req_weekly:
        requirements.append(
            f"Weekly messages: `{req_weekly}`"
        )

    if req_monthly:
        requirements.append(
            f"Monthly messages: `{req_monthly}`"
        )

    if req_total:
        requirements.append(
            f"Total messages: `{req_total}`"
        )

    if bypass_role:
        requirements.append(
            f"Bypass role: {bypass_role.mention}"
        )

    embed.add_field(
        name="Requirements",
        value=(
            "\n".join(
                requirements
            )
            if requirements
            else "No activity requirements."
        ),
        inline=False,
    )

    # Create the message first.
    message = await interaction.channel.send(
        embed=embed,
        view=GiveawayJoinView(
            bot,
            0,
        ),
    )

    await bot.db.execute(
        """
        INSERT INTO giveaway_system (
            message_id,
            channel_id,
            guild_id,
            prize,
            ends_at,
            winners,
            host_id,
            status,
            req_daily,
            req_weekly,
            req_monthly,
            req_total,
            bypass_role_id,
            end_color
        )
        VALUES (
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            'ACTIVE',
            ?,
            ?,
            ?,
            ?,
            ?,
            ?
        )
        """,
        (
            message.id,
            message.channel.id,
            interaction.guild.id,
            prize.strip(),
            ends_at,
            winners,
            host.id,
            req_daily,
            req_weekly,
            req_monthly,
            req_total,
            (
                bypass_role.id
                if bypass_role
                else 0
            ),
            color_value,
        ),
    )

    # Replace temporary button with correct message ID.
    await message.edit(
        view=GiveawayJoinView(
            bot,
            message.id,
        )
    )

    await interaction.followup.send(
        (
            f"✅ Giveaway created: "
            f"{message.jump_url}"
        ),
        ephemeral=True,
    )


# ============================================================
# /GIVEAWAY END
# ============================================================

@giveaway_group.command(
    name="end",
    description="End a giveaway now",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(
    manage_guild=True
)
async def giveaway_end(
    interaction: discord.Interaction,
    message_id: str,
):

    if not message_id.isdigit():

        await interaction.response.send_message(
            "Message ID must be a number.",
            ephemeral=True,
        )

        return

    await acknowledge(
        interaction
    )

    success = await bot.finish_giveaway(
        int(message_id),
        "manual command",
    )

    await interaction.followup.send(
        (
            "✅ Giveaway ended."
            if success
            else
            "The giveaway could not be ended "
            "(already ended, missing, or failed)."
        ),
        ephemeral=True,
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    if not DISCORD_TOKEN:

        raise RuntimeError(
            "DISCORD_TOKEN is missing from the environment."
        )

    try:

        await bot.start(
            DISCORD_TOKEN
        )

    finally:

        if not bot.is_closed():

            await bot.close()


if __name__ == "__main__":

    asyncio.run(
        main()
    )
