import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from groq import Groq

from database import DatabaseController


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
API_SECRET = os.getenv("API_SECRET", "")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")

BOT_OWNER_ID = int(
    os.getenv("BOT_OWNER_ID", "0") or "0"
)

ENVIRONMENT = os.getenv(
    "ENVIRONMENT",
    "",
).lower()

GROQ_MODEL = os.getenv(
    "GROQ_MODEL",
    "llama-3.3-70b-versatile",
)

if os.getenv("DATABASE_PATH"):
    DB_PATH = os.getenv("DATABASE_PATH")
elif ENVIRONMENT in {"production", "prod", "render"}:
    DB_PATH = "/data/bot_database.db"
else:
    DB_PATH = "bot_database.db"

PORT = int(os.getenv("PORT", "10000"))

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("bot")


# ============================================================
# INTENTS & BOT SETUP
# ============================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True


class GiveawayTrustBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
        )

        self.db = DatabaseController(DB_PATH)

        self.groq = (
            Groq(api_key=GROQ_API_KEY)
            if GROQ_API_KEY
            else None
        )

        self.health_runner = None
        self.health_site = None

    async def setup_hook(self):
        await self.db.initialize_database()

        # Persistent Trust views
        self.add_view(TrustPanelView())
        self.add_view(TrustProfileView())

        # Restore active giveaway buttons after restart
        try:
            active_giveaways = await self.db.fetchall(
                """
                SELECT message_id
                FROM giveaway_system
                WHERE status = 'ACTIVE'
                """
            )

            for row in active_giveaways:
                self.add_view(
                    GiveawayJoinView(int(row[0]))
                )

            logger.info(
                "Restored %s active giveaway views.",
                len(active_giveaways),
            )

        except Exception:
            logger.exception(
                "Failed to restore giveaway views."
            )

        if not giveaway_loop.is_running():
            giveaway_loop.start()

        if not temporary_ban_loop.is_running():
            temporary_ban_loop.start()

        if not activity_loop.is_running():
            activity_loop.start()

        try:
            synced = await self.tree.sync()

            logger.info(
                "Synchronized %s application commands.",
                len(synced),
            )

        except Exception:
            logger.exception(
                "Failed to synchronize commands."
            )

        await start_health_server()

    async def close(self):
        if giveaway_loop.is_running():
            giveaway_loop.cancel()

        if temporary_ban_loop.is_running():
            temporary_ban_loop.cancel()

        if activity_loop.is_running():
            activity_loop.cancel()

        if self.health_runner:
            try:
                await self.health_runner.cleanup()
            except Exception:
                logger.exception(
                    "Failed to stop health server."
                )

            self.health_runner = None
            self.health_site = None

        await self.db.close()

        await super().close()


bot = GiveawayTrustBot()


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_timestamp() -> float:
    return time.time()


def format_timestamp(timestamp: float) -> str:
    return f"<t:{int(timestamp)}:F>"


def clamp(
    value: int,
    minimum: int,
    maximum: int,
) -> int:
    return max(
        minimum,
        min(maximum, value),
    )


async def is_bot_or_server_owner(
    interaction: discord.Interaction,
) -> bool:
    if BOT_OWNER_ID and interaction.user.id == BOT_OWNER_ID:
        return True

    if (
        interaction.guild
        and interaction.user.id == interaction.guild.owner_id
    ):
        return True

    return False


def owner_only():
    async def predicate(
        interaction: discord.Interaction,
    ):
        if await is_bot_or_server_owner(interaction):
            return True

        raise app_commands.CheckFailure(
            "This command is only available to the bot owner or server owner."
        )

    return app_commands.check(predicate)


async def safe_interaction_error(
    interaction: discord.Interaction,
    message: str,
):
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
            "Failed to send interaction error."
        )


# ============================================================
# ACTIVITY
# ============================================================

async def record_activity(
    guild_id: int,
    user_id: int,
):
    current = datetime.now(timezone.utc)

    daily = current.strftime("%Y-%m-%d")

    iso = current.isocalendar()
    weekly = f"{iso.year}-W{iso.week:02d}"

    monthly = current.strftime("%Y-%m")

    await bot.db.execute(
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
        ON CONFLICT(guild_id, user_id)
        DO UPDATE SET
            message_count =
                user_activity.message_count + 1,

            daily_message_count =
                CASE
                    WHEN user_activity.last_daily_date != ?
                    THEN 1
                    ELSE user_activity.daily_message_count + 1
                END,

            week_message_count =
                CASE
                    WHEN user_activity.last_weekly_date != ?
                    THEN 1
                    ELSE user_activity.week_message_count + 1
                END,

            month_message_count =
                CASE
                    WHEN user_activity.last_monthly_date != ?
                    THEN 1
                    ELSE user_activity.monthly_message_count + 1
                END,

            last_daily_date = ?,
            last_weekly_date = ?,
            last_monthly_date = ?
        """,
        (
            guild_id,
            user_id,
            daily,
            weekly,
            monthly,
            daily,
            weekly,
            monthly,
            daily,
            weekly,
            monthly,
        ),
    )


# ============================================================
# TRUST / VOUCH HELPERS
# ============================================================

async def ensure_trust_user(
    guild_id: int,
    user_id: int,
):
    await bot.db.execute(
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
        (
            guild_id,
            user_id,
        ),
    )


async def get_trust_profile(
    guild_id: int,
    user_id: int,
):
    await ensure_trust_user(
        guild_id,
        user_id,
    )

    return await bot.db.fetchone(
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


def trust_bar(score: int) -> str:
    score = clamp(
        score,
        0,
        100,
    )

    filled = round(score / 10)
    empty = 10 - filled

    return "█" * filled + "—" * empty


async def build_profile_embed(
    guild: discord.Guild,
    member: discord.Member,
) -> discord.Embed:
    profile = await get_trust_profile(
        guild.id,
        member.id,
    )

    score = int(profile[0])
    given = int(profile[1])
    positive = int(profile[2])
    negative = int(profile[3])

    rows = await bot.db.fetchall(
        """
        SELECT user_id
        FROM user_vouch_network
        WHERE guild_id = ?
        ORDER BY trust_score DESC, user_id ASC
        """,
        (
            guild.id,
        ),
    )

    rank_position = "Unknown"
    total_traders = len(rows)

    for index, row in enumerate(
        rows,
        start=1,
    ):
        if int(row[0]) == member.id:
            rank_position = str(index)
            break

    needed_for_master = max(
        0,
        100 - score,
    )

    if score >= 100:
        color = discord.Color.gold()
    elif score == 0:
        color = discord.Color.red()
    else:
        color = discord.Color.blurple()

    embed = discord.Embed(
        title=f"{member.display_name}",
        color=color,
    )

    embed.set_thumbnail(
        url=member.display_avatar.url
    )

    embed.description = (
        f"Trader • Rank **#{rank_position}** of "
        f"{total_traders} traders\n\n"
        f"**{score} / 100 Trust**\n"
        f"`{trust_bar(score)}`\n\n"
        f"*{needed_for_master} more Trust to reach "
        f"**Master Trader***\n\n"
        f"**Stats**\n"
        f"• +Vouch received · {positive}\n"
        f"• -Vouch received · {negative}\n"
        f"• Vouches given · {given}"
    )

    return embed


async def resolve_member(
    guild: discord.Guild,
    value: str,
):
    value = value.strip()

    if value.startswith("<@") and value.endswith(">"):
        value = (
            value
            .replace("<@", "")
            .replace("<@!", "")
            .replace(">", "")
        )

    if value.isdigit():
        try:
            member = guild.get_member(
                int(value)
            )

            if member:
                return member

            return await guild.fetch_member(
                int(value)
            )

        except (
            discord.NotFound,
            discord.HTTPException,
        ):
            return None

    lowered = value.lower()

    for member in guild.members:
        if (
            member.name.lower() == lowered
            or member.display_name.lower() == lowered
        ):
            return member

    for member in guild.members:
        if (
            lowered in member.name.lower()
            or lowered in member.display_name.lower()
        ):
            return member

    return None


# ============================================================
# TRUST PANEL
# ============================================================

class TrustPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Check My Vouch",
        style=discord.ButtonStyle.primary,
        custom_id="trust:profile",
    )
    async def my_profile(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not interaction.guild:
            return

        member = interaction.guild.get_member(
            interaction.user.id
        )

        if not member:
            await interaction.response.send_message(
                "Unable to find your profile.",
                ephemeral=True,
            )
            return

        embed = await build_profile_embed(
            interaction.guild,
            member,
        )

        await interaction.response.send_message(
            embed=embed,
            view=TrustProfileView(),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Check User's Vouch",
        style=discord.ButtonStyle.primary,
        custom_id="trust:check",
    )
    async def check_member(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_message(
            "Select the member you want to check.",
            view=CheckMemberView(),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Vouch A User",
        style=discord.ButtonStyle.success,
        custom_id="trust:submit",
    )
    async def submit_vouch(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "This can only be used inside a server.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Select the member you want to vouch for.",
            view=VouchTargetView(),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Vouch Rewards",
        style=discord.ButtonStyle.secondary,
        custom_id="trust:rewards",
    )
    async def rep_rewards(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        embed = discord.Embed(
            title="Vouch Rewards",
            description=(
                "Trust runs from 0 to 100.\n"
                "Two roles, both automatic:\n\n"
                "• **0** · Trade Banned. Appeal in a ticket.\n"
                "• **100** · Master Trader"
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
        if not interaction.guild:
            return

        rows = await bot.db.fetchall(
            """
            SELECT user_id, trust_score
            FROM user_vouch_network
            WHERE guild_id = ?
            ORDER BY trust_score DESC, user_id ASC
            LIMIT 10
            """,
            (
                interaction.guild.id,
            ),
        )

        embed = discord.Embed(
            title="Sprite Trading - Leaderboard",
            color=discord.Color.gold(),
        )

        if not rows:
            embed.description = (
                "No vouch data available yet."
            )
        else:
            lines = []

            for position, row in enumerate(
                rows,
                start=1,
            ):
                user_id = int(row[0])
                score = int(row[1])

                member = interaction.guild.get_member(
                    user_id
                )

                name = (
                    member.display_name
                    if member
                    else f"User {user_id}"
                )

                lines.append(
                    f"**{position}.** {name} — `{score}/100 Trust`"
                )

            embed.description = "\n".join(
                lines
            )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )


class TrustProfileView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Refresh",
        style=discord.ButtonStyle.secondary,
        custom_id="trust:refresh",
    )
    async def refresh(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not interaction.guild:
            return

        member = interaction.guild.get_member(
            interaction.user.id
        )

        if not member:
            return

        embed = await build_profile_embed(
            interaction.guild,
            member,
        )

        await interaction.response.edit_message(
            embed=embed,
            view=self,
        )


# ============================================================
# VOUCH SELECTION
# ============================================================

class VouchTargetSelect(
    discord.ui.UserSelect
):
    def __init__(self):
        super().__init__(
            placeholder="Select a member...",
            min_values=1,
            max_values=1,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        if not interaction.guild:
            return

        target = self.values[0]

        if target.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ You cannot vouch for yourself.",
                ephemeral=True,
            )
            return

        member = interaction.guild.get_member(
            target.id
        )

        if member and member.bot:
            await interaction.response.send_message(
                "❌ You cannot vouch for a bot.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Choose a vouch type for "
            f"**{target.display_name}**.",
            view=VouchTypeView(target.id),
            ephemeral=True,
        )


class VouchTargetView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(VouchTargetSelect())

    @discord.ui.button(
        label="Enter Name / ID",
        style=discord.ButtonStyle.secondary,
    )
    async def enter_name(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_modal(
            VouchMemberModal()
        )


class VouchMemberModal(
    discord.ui.Modal,
    title="Submit Vouch",
):
    member_input = discord.ui.TextInput(
        label="Member Name or ID",
        placeholder="Username, display name or Discord ID",
        required=True,
        max_length=100,
    )

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):
        if not interaction.guild:
            return

        target = await resolve_member(
            interaction.guild,
            str(self.member_input),
        )

        if not target:
            await interaction.response.send_message(
                "❌ Member not found.",
                ephemeral=True,
            )
            return

        if target.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ You cannot vouch for yourself.",
                ephemeral=True,
            )
            return

        if target.bot:
            await interaction.response.send_message(
                "❌ You cannot vouch for a bot.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Choose a vouch type for "
            f"**{target.display_name}**.",
            view=VouchTypeView(target.id),
            ephemeral=True,
        )


class VouchTypeView(discord.ui.View):
    def __init__(
        self,
        target_id: int,
    ):
        super().__init__(timeout=120)
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
        await self.submit(
            interaction,
            "POSITIVE",
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
        await self.submit(
            interaction,
            "NEGATIVE",
        )

    async def submit(
        self,
        interaction: discord.Interaction,
        vouch_type: str,
    ):
        if not interaction.guild:
            return

        if self.target_id == interaction.user.id:
            await interaction.response.send_message(
                "❌ You cannot vouch for yourself.",
                ephemeral=True,
            )
            return

        target = interaction.guild.get_member(
            self.target_id
        )

        if not target:
            await interaction.response.send_message(
                "❌ That member is no longer in the server.",
                ephemeral=True,
            )
            return

        if target.bot:
            await interaction.response.send_message(
                "❌ You cannot vouch for a bot.",
                ephemeral=True,
            )
            return

        try:
            await bot.db.execute(
                """
                INSERT INTO vouch_history (
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
                    interaction.guild.id,
                    target.id,
                    interaction.user.id,
                    vouch_type,
                    None,
                    now_timestamp(),
                ),
            )

        except aiosqlite.IntegrityError:
            await interaction.response.send_message(
                "❌ You have already vouched for this member.",
                ephemeral=True,
            )
            return

        await ensure_trust_user(
            interaction.guild.id,
            target.id,
        )

        await ensure_trust_user(
            interaction.guild.id,
            interaction.user.id,
        )

        if vouch_type == "POSITIVE":
            await bot.db.execute(
                """
                UPDATE user_vouch_network
                SET
                    trust_score =
                        MIN(100, trust_score + 5),
                    vouch_positive =
                        vouch_positive + 1
                WHERE guild_id = ?
                AND user_id = ?
                """,
                (
                    interaction.guild.id,
                    target.id,
                ),
            )

            change = "+5"

        else:
            await bot.db.execute(
                """
                UPDATE user_vouch_network
                SET
                    trust_score =
                        MAX(0, trust_score - 15),
                    vouch_negative =
                        vouch_negative + 1
                WHERE guild_id = ?
                AND user_id = ?
                """,
                (
                    interaction.guild.id,
                    target.id,
                ),
            )

            change = "-15"

        await bot.db.execute(
            """
            UPDATE user_vouch_network
            SET vouches_given =
                vouches_given + 1
            WHERE guild_id = ?
            AND user_id = ?
            """,
            (
                interaction.guild.id,
                interaction.user.id,
            ),
        )

        await interaction.response.send_message(
            f"✅ Your {vouch_type.lower()} vouch for "
            f"**{target.display_name}** was recorded.\n"
            f"Trust change: `{change}`",
            ephemeral=True,
        )


# ============================================================
# CHECK MEMBER
# ============================================================

class CheckMemberSelect(
    discord.ui.UserSelect
):
    def __init__(self):
        super().__init__(
            placeholder="Select a member...",
            min_values=1,
            max_values=1,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        if not interaction.guild:
            return

        target = self.values[0]

        member = interaction.guild.get_member(
            target.id
        )

        if not member:
            await interaction.response.send_message(
                "❌ Member not found.",
                ephemeral=True,
            )
            return

        embed = await build_profile_embed(
            interaction.guild,
            member,
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )


class CheckMemberView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(CheckMemberSelect())

    @discord.ui.button(
        label="Enter Name / ID",
        style=discord.ButtonStyle.secondary,
    )
    async def enter_name(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_modal(
            CheckMemberModal()
        )


class CheckMemberModal(
    discord.ui.Modal,
    title="Check Member",
):
    member_input = discord.ui.TextInput(
        label="Member Name or ID",
        placeholder="Username, display name or Discord ID",
        required=True,
        max_length=100,
    )

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):
        if not interaction.guild:
            return

        target = await resolve_member(
            interaction.guild,
            str(self.member_input),
        )

        if not target:
            await interaction.response.send_message(
                "❌ Member not found.",
                ephemeral=True,
            )
            return

        embed = await build_profile_embed(
            interaction.guild,
            target,
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )


# ============================================================
# GIVEAWAYS
# ============================================================

class GiveawayJoinButton(
    discord.ui.Button
):
    def __init__(
        self,
        message_id: int,
    ):
        super().__init__(
            label="🎉 Enter Giveaway",
            style=discord.ButtonStyle.success,
            custom_id=f"giveaway:enter:{message_id}",
        )

        self.message_id = message_id

    async def callback(
        self,
        interaction: discord.Interaction,
    ):
        if not interaction.guild:
            return

        giveaway = await bot.db.fetchone(
            """
            SELECT
                prize,
                ends_at,
                status
            FROM giveaway_system
            WHERE message_id = ?
            """,
            (
                self.message_id,
            ),
        )

        if not giveaway:
            await interaction.response.send_message(
                "❌ Giveaway not found.",
                ephemeral=True,
            )
            return

        prize = giveaway[0]
        ends_at = float(giveaway[1])
        status = giveaway[2]

        if (
            status != "ACTIVE"
            or ends_at <= now_timestamp()
        ):
            await interaction.response.send_message(
                "❌ This giveaway has ended.",
                ephemeral=True,
            )
            return

        existing = await bot.db.fetchone(
            """
            SELECT user_id
            FROM giveaway_participants
            WHERE message_id = ?
            AND user_id = ?
            """,
            (
                self.message_id,
                interaction.user.id,
            ),
        )

        if existing:
            await interaction.response.send_message(
                "ℹ️ You are already entered.",
                ephemeral=True,
            )
            return

        try:
            await bot.db.execute(
                """
                INSERT INTO giveaway_participants (
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

        except aiosqlite.IntegrityError:
            await interaction.response.send_message(
                "ℹ️ You are already entered.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"🎉 You entered **{prize}**!",
            ephemeral=True,
        )


class GiveawayJoinView(
    discord.ui.View
):
    def __init__(
        self,
        message_id: Optional[int] = None,
    ):
        super().__init__(timeout=None)

        if message_id is not None:
            self.add_item(
                GiveawayJoinButton(message_id)
            )


async def finish_giveaway(
    message_id: int,
    force: bool = False,
):
    current_time = now_timestamp()

    # Atomically claim the giveaway.
    # Only one worker can change ACTIVE -> PROCESSING.
    claimed = await bot.db.execute(
        """
        UPDATE giveaway_system
        SET
            status = 'PROCESSING',
            processing_started_at = ?
        WHERE message_id = ?
        AND status = 'ACTIVE'
        AND (
            ends_at <= ?
            OR ? = 1
        )
        """,
        (
            current_time,
            message_id,
            current_time,
            1 if force else 0,
        ),
    )

    if claimed != 1:
        return False

    giveaway = await bot.db.fetchone(
        """
        SELECT
            channel_id,
            guild_id,
            prize,
            ends_at,
            winners
        FROM giveaway_system
        WHERE message_id = ?
        """,
        (
            message_id,
        ),
    )

    if not giveaway:
        return False

    (
        channel_id,
        guild_id,
        prize,
        ends_at,
        winner_count,
    ) = giveaway

    try:
        participants = await bot.db.fetchall(
            """
            SELECT user_id
            FROM giveaway_participants
            WHERE message_id = ?
            """,
            (
                message_id,
            ),
        )

        participant_ids = [
            int(row[0])
            for row in participants
        ]

        selected = []

        if participant_ids:
            selected = random.sample(
                participant_ids,
                min(
                    int(winner_count),
                    len(participant_ids),
                ),
            )

        channel = bot.get_channel(
            int(channel_id)
        )

        if channel is None:
            try:
                channel = await bot.fetch_channel(
                    int(channel_id)
                )
            except Exception:
                logger.exception(
                    "Failed to fetch giveaway channel."
                )
                channel = None

        result_message = None

        if channel:
            if selected:
                winners_text = ", ".join(
                    f"<@{uid}>"
                    for uid in selected
                )

                description = (
                    f"🎉 **Giveaway ended!**\n\n"
                    f"**Prize:** {prize}\n"
                    f"**Winner(s):** {winners_text}\n"
                    f"**Participants:** "
                    f"{len(participant_ids)}"
                )
            else:
                description = (
                    f"🎉 **Giveaway ended!**\n\n"
                    f"**Prize:** {prize}\n\n"
                    f"No valid participants."
                )

            embed = discord.Embed(
                title="🎉 Giveaway Result",
                description=description,
                color=discord.Color.gold(),
            )

            try:
                result_message = await channel.send(
                    embed=embed
                )
            except discord.HTTPException:
                logger.exception(
                    "Failed to send giveaway result."
                )

        winners_json = json.dumps(
            selected
        )

        await bot.db.execute(
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
                result_message.id
                if result_message
                else 0,
                winners_json,
                len(participant_ids),
                message_id,
            ),
        )

        await bot.db.execute(
            """
            INSERT OR REPLACE INTO giveaway_history (
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
                winners_json,
                now_timestamp(),
            ),
        )

        return True

    except Exception as exc:
        logger.exception(
            "Failed to finish giveaway %s.",
            message_id,
        )

        # Allow retry instead of leaving it stuck in PROCESSING.
        await bot.db.execute(
            """
            UPDATE giveaway_system
            SET
                status = 'ACTIVE',
                retry_count = retry_count + 1,
                last_error = ?
            WHERE message_id = ?
            AND status = 'PROCESSING'
            """,
            (
                str(exc)[:1000],
                message_id,
            ),
        )

        return False


class GiveawayGroup(app_commands.Group):
    def __init__(self):
        super().__init__(
            name="giveaway",
            description="Manage giveaways.",
        )

    @app_commands.command(
        name="create",
        description="Create a giveaway.",
    )
    @owner_only()
    @app_commands.describe(
        prize="Giveaway prize.",
        duration_minutes="Duration in minutes.",
        winners="Number of winners.",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        prize: str,
        duration_minutes: int,
        winners: int = 1,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        if (
            duration_minutes < 1
            or duration_minutes > 43200
        ):
            await interaction.response.send_message(
                "❌ Duration must be between 1 minute and 30 days.",
                ephemeral=True,
            )
            return

        if winners < 1:
            await interaction.response.send_message(
                "❌ Winners must be at least 1.",
                ephemeral=True,
            )
            return

        ends_at = (
            now_timestamp()
            + duration_minutes * 60
        )

        await interaction.response.defer(
            ephemeral=True
        )

        if not interaction.channel:
            await interaction.followup.send(
                "❌ No channel available.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🎉 Giveaway",
            description=(
                f"**Prize:** {prize}\n\n"
                f"🏆 **Winners:** {winners}\n"
                f"⏰ **Ends:** "
                f"{format_timestamp(ends_at)}\n"
                f"👥 **Participants:** 0"
            ),
            color=discord.Color.blurple(),
        )

        embed.set_footer(
            text=f"Hosted by {interaction.user.display_name}"
        )

        message = await interaction.channel.send(
            embed=embed
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

        await message.edit(
            view=GiveawayJoinView(
                message.id
            )
        )

        await interaction.followup.send(
            f"✅ Giveaway created: {message.jump_url}",
            ephemeral=True,
        )

    @app_commands.command(
        name="end",
        description="End a giveaway immediately.",
    )
    @owner_only()
    async def end(
        self,
        interaction: discord.Interaction,
        message_id: str,
    ):
        if not message_id.isdigit():
            await interaction.response.send_message(
                "❌ Invalid message ID.",
                ephemeral=True,
            )
            return

        success = await finish_giveaway(
            int(message_id),
            force=True,
        )

        if not success:
            await interaction.response.send_message(
                "❌ Giveaway could not be ended.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "✅ Giveaway ended.",
            ephemeral=True,
        )


bot.tree.add_command(
    GiveawayGroup()
)


# ============================================================
# COMMANDS
# ============================================================

@bot.tree.command(
    name="activity",
    description="View member activity.",
)
@owner_only()
@app_commands.describe(
    member="Member to inspect."
)
async def activity(
    interaction: discord.Interaction,
    member: Optional[discord.Member] = None,
):
    if not interaction.guild:
        return

    target = member or interaction.user

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
            target.id,
        ),
    )

    embed = discord.Embed(
        title="📊 Activity",
        color=discord.Color.blurple(),
    )

    if not row:
        embed.description = (
            f"No activity data for {target.mention}."
        )
    else:
        embed.description = (
            f"**Member:** {target.mention}\n\n"
            f"💬 Total: `{row[0]}`\n"
            f"📅 Today: `{row[1]}`\n"
            f"📆 This week: `{row[2]}`\n"
            f"🗓️ This month: `{row[3]}`"
        )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="botstats",
    description="View bot statistics.",
)
@owner_only()
async def botstats(
    interaction: discord.Interaction,
):
    guild_count = len(bot.guilds)

    member_count = sum(
        guild.member_count or 0
        for guild in bot.guilds
    )

    embed = discord.Embed(
        title="🤖 Bot Statistics",
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="Servers",
        value=str(guild_count),
        inline=True,
    )

    embed.add_field(
        name="Members",
        value=str(member_count),
        inline=True,
    )

    embed.add_field(
        name="Latency",
        value=f"{round(bot.latency * 1000)} ms",
        inline=True,
    )

    embed.add_field(
        name="Database",
        value=DB_PATH,
        inline=False,
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@bot.tree.command(
    name="say",
    description="Send a message as the bot.",
)
@owner_only()
@app_commands.describe(
    message="Message to send."
)
async def say(
    interaction: discord.Interaction,
    message: str,
):
    await interaction.response.defer(
        ephemeral=True
    )

    if not interaction.channel:
        await interaction.followup.send(
            "❌ No channel available.",
            ephemeral=True,
        )
        return

    await interaction.channel.send(
        message,
        allowed_mentions=discord.AllowedMentions.none(),
    )

    await interaction.followup.send(
        "✅ Message sent.",
        ephemeral=True,
    )


@bot.tree.command(
    name="vouchpanel",
    description="Create the Trader Vouch System panel.",
)
@owner_only()
async def vouchpanel(
    interaction: discord.Interaction,
):
    if not interaction.channel:
        await interaction.response.send_message(
            "❌ No channel available.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="Trader Vouch System",
        description=(
            "Vouches show who is safe to trade sprites with. "
            "Everyone starts at **25 Trust** out of 100.\n\n"
            "**How it works**\n"
            "• Traded with someone? Hit **Vouch A User**.\n"
            "• Pick **+Vouch** or **-Vouch**.\n"
            "• +Vouch raises Trust. -Vouch lowers it.\n"
            "• Check anyone with **Check User's Vouch** before you trade.\n\n"
            "**Ranks**\n"
            "• **Trade Banned** · 0\n"
            "• **Trader** · 1 - 99\n"
            "• **Master Trader** · 100\n\n"
            "*Only vouch people you actually traded with. Fake, spam or revenge vouches can get you permanently banned.*"
        ),
        color=discord.Color.blurple(),
    )

    await interaction.channel.send(
        embed=embed,
        view=TrustPanelView(),
    )

    await interaction.response.send_message(
        "✅ Vouch System panel created.",
        ephemeral=True,
    )


@bot.tree.command(
    name="sync",
    description="Synchronize application commands.",
)
@owner_only()
async def sync_commands(
    interaction: discord.Interaction,
):
    await interaction.response.defer(
        ephemeral=True
    )

    synced = await bot.tree.sync()

    await interaction.followup.send(
        f"✅ Synchronized `{len(synced)}` commands.",
        ephemeral=True,
    )


@bot.tree.command(
    name="tempban",
    description="Temporarily ban a member.",
)
@owner_only()
@app_commands.describe(
    member="Member to ban.",
    duration_minutes="Ban duration in minutes.",
    reason="Ban reason.",
)
async def tempban(
    interaction: discord.Interaction,
    member: discord.Member,
    duration_minutes: int,
    reason: str = "Temporary ban",
):
    if not interaction.guild:
        return

    if duration_minutes < 1:
        await interaction.response.send_message(
            "❌ Duration must be at least 1 minute.",
            ephemeral=True,
        )
        return

    expiry = (
        now_timestamp()
        + duration_minutes * 60
    )

    try:
        await member.ban(
            reason=reason,
            delete_message_days=0,
        )

    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I do not have permission to ban this member.",
            ephemeral=True,
        )
        return

    except discord.HTTPException:
        await interaction.response.send_message(
            "❌ Discord rejected the ban request.",
            ephemeral=True,
        )
        return

    await bot.db.execute(
        """
        INSERT OR REPLACE INTO temporary_bans (
            guild_id,
            target_id,
            expiry_timestamp
        )
        VALUES (?, ?, ?)
        """,
        (
            interaction.guild.id,
            member.id,
            expiry,
        ),
    )

    await interaction.response.send_message(
        f"🔨 {member} was temporarily banned until "
        f"{format_timestamp(expiry)}.",
        ephemeral=True,
    )


# ============================================================
# GROQ AI
# ============================================================

async def ask_ai(
    user: discord.User,
    guild: Optional[discord.Guild],
    prompt: str,
):
    if not bot.groq:
        return None

    try:
        response = await asyncio.to_thread(
            bot.groq.chat.completions.create,
            model=GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful Discord bot assistant. "
                        "Answer clearly and concisely."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Server: "
                        f"{guild.name if guild else 'DM'}\n"
                        f"User: {user.display_name}\n\n"
                        f"Question:\n{prompt}"
                    ),
                },
            ],
            temperature=0.4,
            max_tokens=700,
        )

        content = response.choices[0].message.content

        if not content:
            return None

        return content[:1900]

    except Exception:
        logger.exception(
            "Groq request failed."
        )
        return None


# ============================================================
# MESSAGE HANDLER
# ============================================================

@bot.event
async def on_message(
    message: discord.Message,
):
    if message.author.bot:
        return

    if message.guild:
        try:
            await record_activity(
                message.guild.id,
                message.author.id,
            )
        except Exception:
            logger.exception(
                "Failed to record activity."
            )

    # AI is NOT a slash command.
    # The bot responds only when it is mentioned.
    if (
        bot.user
        and bot.user in message.mentions
    ):
        prompt = (
            message.content
            .replace(
                f"<@{bot.user.id}>",
                "",
            )
            .replace(
                f"<@!{bot.user.id}>",
                "",
            )
            .strip()
        )

        if not prompt:
            await message.reply(
                "👋 Mention me with a question and I will help.",
                mention_author=False,
            )
            return

        async with message.channel.typing():
            response = await ask_ai(
                message.author,
                message.guild,
                prompt,
            )

        if response:
            await message.reply(
                response,
                mention_author=False,
            )
        else:
            await message.reply(
                "❌ I could not process that request right now.",
                mention_author=False,
            )

        return

    await bot.process_commands(
        message
    )


# ============================================================
# READY
# ============================================================

@bot.event
async def on_ready():
    logger.info(
        "Logged in as %s (%s).",
        bot.user,
        bot.user.id if bot.user else "unknown",
    )


# ============================================================
# COMMAND ERROR HANDLER
# ============================================================

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    if isinstance(
        error,
        app_commands.CheckFailure,
    ):
        await safe_interaction_error(
            interaction,
            "❌ This command is only available to the bot owner or server owner.",
        )
        return

    logger.exception(
        "Application command error: %s",
        error,
    )

    await safe_interaction_error(
        interaction,
        "❌ Something went wrong while executing that command.",
    )


# ============================================================
# GIVEAWAY LOOP
# ============================================================

@tasks.loop(seconds=15)
async def giveaway_loop():
    try:
        rows = await bot.db.fetchall(
            """
            SELECT message_id
            FROM giveaway_system
            WHERE status = 'ACTIVE'
            AND ends_at <= ?
            LIMIT 20
            """,
            (
                now_timestamp(),
            ),
        )

        for row in rows:
            try:
                await finish_giveaway(
                    int(row[0])
                )
            except Exception:
                logger.exception(
                    "Failed to process giveaway %s.",
                    row[0],
                )

    except Exception:
        logger.exception(
            "Giveaway loop failed."
        )


# ============================================================
# TEMPORARY BAN LOOP
# ============================================================

@tasks.loop(seconds=30)
async def temporary_ban_loop():
    try:
        rows = await bot.db.fetchall(
            """
            SELECT
                guild_id,
                target_id
            FROM temporary_bans
            WHERE expiry_timestamp <= ?
            """,
            (
                now_timestamp(),
            ),
        )

        for guild_id, target_id in rows:
            guild = bot.get_guild(
                int(guild_id)
            )

            if not guild:
                # Keep it in the database.
                # The bot may see the guild again later.
                continue

            try:
                await guild.unban(
                    discord.Object(
                        id=int(target_id)
                    ),
                    reason="Temporary ban expired.",
                )

            except discord.NotFound:
                # Already unbanned.
                pass

            except discord.Forbidden:
                logger.warning(
                    "No permission to unban %s in guild %s.",
                    target_id,
                    guild_id,
                )
                continue

            except discord.HTTPException:
                logger.exception(
                    "Discord HTTP error while unbanning %s in guild %s.",
                    target_id,
                    guild_id,
                )
                continue

            # Delete only after successful unban
            # or NotFound (already unbanned).
            await bot.db.execute(
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

    except Exception:
        logger.exception(
            "Temporary ban loop failed."
        )


# ============================================================
# ACTIVITY LOOP
# ============================================================

@tasks.loop(hours=1)
async def activity_loop():
    try:
        await bot.db.fetchone(
            "SELECT 1"
        )
    except Exception:
        logger.exception(
            "Activity maintenance failed."
        )


# ============================================================
# HEALTH SERVER
# ============================================================

async def health_handler(
    request: web.Request,
):
    return web.json_response(
        {
            "status": "ok",
            "bot_ready": bot.is_ready(),
            "guilds": len(bot.guilds),
        }
    )


async def api_status_handler(
    request: web.Request,
):
    if API_SECRET:
        provided_secret = request.headers.get(
            "Authorization",
            "",
        )

        if provided_secret.startswith(
            "Bearer "
        ):
            provided_secret = provided_secret[7:]

        if provided_secret != API_SECRET:
            return web.json_response(
                {
                    "error": "Unauthorized"
                },
                status=401,
            )

    return web.json_response(
        {
            "status": "ok",
            "bot_ready": bot.is_ready(),
            "guilds": len(bot.guilds),
            "database": DB_PATH,
        }
    )


async def start_health_server():
    if bot.health_runner:
        return

    app = web.Application()

    app.router.add_get(
        "/health",
        health_handler,
    )

    app.router.add_get(
        "/api/status",
        api_status_handler,
    )

    bot.health_runner = web.AppRunner(
        app
    )

    await bot.health_runner.setup()

    bot.health_site = web.TCPSite(
        bot.health_runner,
        "0.0.0.0",
        PORT,
    )

    await bot.health_site.start()

    logger.info(
        "Health server listening on 0.0.0.0:%s.",
        PORT,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    logger.info(
        "Starting bot."
    )

    bot.run(
        DISCORD_TOKEN,
        log_handler=None,
    )


if __name__ == "__main__":
    main()
