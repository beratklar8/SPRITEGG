import asyncio
import datetime
import json
import logging
import os
import secrets
import sqlite3
import time
from typing import Optional, Tuple, Dict

import discord
from discord import app_commands
from discord.ext import commands, tasks
from aiohttp import web
from dotenv import load_dotenv
from groq import AsyncGroq
import zoneinfo

from database import DatabaseController


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger("bot")


def get_int_env(
    name: str,
    default: int,
) -> int:
    value = os.getenv(name)

    if value is None or not value.strip():
        return default

    try:
        return int(value)
    except ValueError:
        raise RuntimeError(
            f"Invalid integer environment variable: "
            f"{name}={value}"
        )


BOT_TOKEN = os.getenv("DISCORD_TOKEN")
BOT_OWNER_ID = get_int_env("BOT_OWNER_ID", 0)
WEB_PORT = get_int_env("PORT", 10000)
GROQ_API_SECRET = os.getenv("GROQ_API_KEY")
DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "bot_database.db",
)
GROQ_MODEL = os.getenv(
    "GROQ_MODEL",
    "llama-3.3-70b-versatile",
)

if not BOT_TOKEN:
    raise RuntimeError(
        "Missing DISCORD_TOKEN environment variable."
    )


# ============================================================
# DATABASE
# ============================================================

db_controller = DatabaseController(
    DATABASE_PATH
)


# ============================================================
# COLORS
# ============================================================

COLOR_NEUTRAL = 0x2B2D31
COLOR_SUCCESS = 0x2ECC71
COLOR_WARNING = 0xF1C40F
COLOR_DANGER = 0xE74C3C
COLOR_INFO = 0x3498DB
COLOR_PURPLE = 0x9B59B6


# ============================================================
# GROQ
# ============================================================

groq_api_client = None

if GROQ_API_SECRET:
    try:
        groq_api_client = AsyncGroq(
            api_key=GROQ_API_SECRET
        )
        logger.info("Groq AI client initialized.")
    except Exception:
        logger.exception(
            "Failed to initialize Groq client."
        )


# ============================================================
# MEMORY / LOCKS
# ============================================================

user_cooldowns = {}
ai_locks = {}
giveaway_entry_locks = {}

message_buffer: Dict[
    Tuple[int, int],
    int
] = {}

buffer_lock = asyncio.Lock()


def get_giveaway_lock(
    message_id: int,
) -> asyncio.Lock:

    lock = giveaway_entry_locks.get(
        message_id
    )

    if lock is None:
        lock = asyncio.Lock()
        giveaway_entry_locks[
            message_id
        ] = lock

    return lock


# ============================================================
# TIME
# ============================================================

def get_nl_now() -> datetime.datetime:
    return datetime.datetime.now(
        zoneinfo.ZoneInfo("Europe/Amsterdam")
    )


# ============================================================
# EMBEDS
# ============================================================

def make_embed(
    title: str,
    description: str,
    color: int = COLOR_NEUTRAL,
    footer_text: Optional[str] = None,
) -> discord.Embed:

    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
    )

    embed.timestamp = datetime.datetime.now(
        datetime.timezone.utc
    )

    if footer_text:
        embed.set_footer(
            text=footer_text
        )

    return embed


def success_embed(
    title: str,
    description: str,
) -> discord.Embed:
    return make_embed(
        f"✔ {title}",
        description,
        COLOR_SUCCESS,
    )


def error_embed(
    title: str,
    description: str,
) -> discord.Embed:
    return make_embed(
        f"✖ {title}",
        description,
        COLOR_DANGER,
    )


def warning_embed(
    title: str,
    description: str,
) -> discord.Embed:
    return make_embed(
        f"⚠ {title}",
        description,
        COLOR_WARNING,
    )


def info_embed(
    title: str,
    description: str,
) -> discord.Embed:
    return make_embed(
        f"ℹ {title}",
        description,
        COLOR_INFO,
    )


# ============================================================
# HELPERS
# ============================================================

def validate_hex_color(
    color_str: Optional[str],
    default: int,
) -> Tuple[int, Optional[str]]:

    if not color_str:
        return default, None

    if not isinstance(color_str, str):
        return default, None

    cleaned = color_str.strip().lstrip("#")

    if (
        len(cleaned) == 6
        and all(
            c in "0123456789abcdefABCDEF"
            for c in cleaned
        )
    ):
        try:
            return int(cleaned, 16), None
        except ValueError:
            pass

    return default, (
        f"Invalid hex color format: #{cleaned}"
    )


def normalize_reason(
    reason: Optional[str],
) -> str:
    return (
        reason or "No reason provided"
    )[:512]


def is_owner_or_special(
    interaction: discord.Interaction,
) -> bool:

    if not interaction.guild:
        return False

    if (
        BOT_OWNER_ID != 0
        and interaction.user.id == BOT_OWNER_ID
    ):
        return True

    return (
        interaction.user
        == interaction.guild.owner
    )


def has_permission(
    interaction: discord.Interaction,
    permission_name: str,
) -> bool:

    if is_owner_or_special(interaction):
        return True

    if (
        not interaction.guild
        or not isinstance(
            interaction.user,
            discord.Member,
        )
    ):
        return False

    permissions = (
        interaction.user.guild_permissions
    )

    if permissions.administrator:
        return True

    return getattr(
        permissions,
        permission_name,
        False,
    )


async def check_permission_and_respond(
    interaction: discord.Interaction,
    permission_name: str,
) -> bool:

    if has_permission(
        interaction,
        permission_name,
    ):
        return True

    embed = error_embed(
        "Access Denied",
        "You do not have permission to use this command.",
    )

    if interaction.response.is_done():
        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )

    return False


# ============================================================
# ACTIVITY TRACKING
# ============================================================

async def flush_user_activity(
    guild_id: int,
    user_id: int,
):

    async with buffer_lock:
        count = message_buffer.pop(
            (guild_id, user_id),
            0,
        )

    if count <= 0:
        return

    now_nl = get_nl_now()

    today_str = now_nl.date().isoformat()

    week_start_str = (
        now_nl.date()
        - datetime.timedelta(
            days=now_nl.date().weekday()
        )
    ).isoformat()

    month_start_str = (
        now_nl.date()
        .replace(day=1)
        .isoformat()
    )

    try:
        await db_controller.transaction(
            [
                (
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
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id)
                    DO UPDATE SET
                        message_count =
                            message_count + ?,

                        daily_message_count =
                            CASE
                                WHEN last_daily_date = ?
                                THEN daily_message_count + ?
                                ELSE ?
                            END,

                        week_message_count =
                            CASE
                                WHEN last_weekly_date = ?
                                THEN week_message_count + ?
                                ELSE ?
                            END,

                        month_message_count =
                            CASE
                                WHEN last_monthly_date = ?
                                THEN month_message_count + ?
                                ELSE ?
                            END,

                        last_daily_date = ?,
                        last_weekly_date = ?,
                        last_monthly_date = ?
                    """,
                    (
                        guild_id,
                        user_id,
                        count,
                        count,
                        count,
                        count,
                        today_str,
                        week_start_str,
                        month_start_str,

                        count,
                        today_str,
                        count,
                        count,

                        week_start_str,
                        count,
                        count,

                        month_start_str,
                        count,
                        count,

                        today_str,
                        week_start_str,
                        month_start_str,
                    ),
                )
            ]
        )

    except Exception:
        async with buffer_lock:
            key = (
                guild_id,
                user_id,
            )

            message_buffer[key] = (
                message_buffer.get(
                    key,
                    0,
                )
                + count
            )

        logger.exception(
            "Failed to flush activity for user %s",
            user_id,
        )


# ============================================================
# VOUCH MODALS
# ============================================================

class VouchSubmitModal(
    discord.ui.Modal,
    title="Submit Trust Vouch",
):

    reason_input = discord.ui.TextInput(
        label="Feedback / Reason",
        style=discord.TextStyle.paragraph,
        placeholder=(
            "Smooth trade, quick response "
            "and trustworthy!"
        ),
        max_length=300,
        required=True,
    )

    def __init__(
        self,
        target_id: int,
        vouch_type: str,
    ):
        super().__init__()

        self.target_id = target_id
        self.vouch_type = vouch_type

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        if not interaction.guild:
            return await interaction.followup.send(
                "This action can only be used inside a server.",
                ephemeral=True,
            )

        if self.vouch_type not in {
            "POSITIVE",
            "NEGATIVE",
        }:
            return await interaction.followup.send(
                "Invalid vouch type.",
                ephemeral=True,
            )

        if interaction.user.id == self.target_id:
            return await interaction.followup.send(
                "You cannot vouch for yourself.",
                ephemeral=True,
            )

        guild_id = interaction.guild.id
        giver_id = interaction.user.id

        raw_reason = normalize_reason(
            self.reason_input.value
        )

        pos_delta = (
            1
            if self.vouch_type == "POSITIVE"
            else 0
        )

        neg_delta = (
            1
            if self.vouch_type == "NEGATIVE"
            else 0
        )

        score_delta = (
            4
            if self.vouch_type == "POSITIVE"
            else -8
        )

        try:
            existing = await db_controller.fetchone(
                """
                SELECT id
                FROM vouch_history
                WHERE guild_id = ?
                  AND target_id = ?
                  AND giver_id = ?
                LIMIT 1
                """,
                (
                    guild_id,
                    self.target_id,
                    giver_id,
                ),
            )

            if existing:
                return await interaction.followup.send(
                    "You have already submitted feedback for this user.",
                    ephemeral=True,
                )

            await db_controller.transaction(
                [
                    (
                        """
                        INSERT INTO user_vouch_network (
                            guild_id,
                            user_id,
                            trust_score
                        )
                        VALUES (?, ?, 50)
                        ON CONFLICT(guild_id, user_id)
                        DO NOTHING
                        """,
                        (
                            guild_id,
                            self.target_id,
                        ),
                    ),
                    (
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
                            guild_id,
                            self.target_id,
                            giver_id,
                            self.vouch_type,
                            raw_reason,
                            time.time(),
                        ),
                    ),
                    (
                        """
                        UPDATE user_vouch_network
                        SET
                            vouch_positive =
                                vouch_positive + ?,

                            vouch_negative =
                                vouch_negative + ?,

                            trust_score =
                                MAX(
                                    0,
                                    MIN(
                                        100,
                                        trust_score + ?
                                    )
                                )
                        WHERE guild_id = ?
                          AND user_id = ?
                        """,
                        (
                            pos_delta,
                            neg_delta,
                            score_delta,
                            guild_id,
                            self.target_id,
                        ),
                    ),
                    (
                        """
                        INSERT INTO user_vouch_network (
                            guild_id,
                            user_id,
                            vouches_given
                        )
                        VALUES (?, ?, 1)
                        ON CONFLICT(guild_id, user_id)
                        DO UPDATE SET
                            vouches_given =
                                vouches_given + 1
                        """,
                        (
                            guild_id,
                            giver_id,
                        ),
                    ),
                ]
            )

            safe_reason = (
                discord.utils.escape_mentions(
                    discord.utils.escape_markdown(
                        raw_reason
                    )
                )
            )

            label_txt = (
                "Positive (+Vouch)"
                if self.vouch_type == "POSITIVE"
                else "Negative (-Vouch)"
            )

            await interaction.followup.send(
                f"Successfully recorded **{label_txt}** "
                f"for <@{self.target_id}>!\n"
                f"Reason: *{safe_reason}*",
                ephemeral=True,
            )

        except sqlite3.IntegrityError:
            await interaction.followup.send(
                "You have already submitted feedback for this user.",
                ephemeral=True,
            )

        except Exception:
            logger.exception(
                "Vouch submission error for target %s",
                self.target_id,
            )

            await interaction.followup.send(
                "An unexpected database error occurred.",
                ephemeral=True,
            )


# ============================================================
# VOUCH TARGET MODAL
# ============================================================

class VouchTargetModal(
    discord.ui.Modal,
    title="Look Up Member Trust",
):

    query_input = discord.ui.TextInput(
        label="Member ID or Mention",
        placeholder="Paste user ID here",
        required=True,
    )

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        if not interaction.guild:
            return await interaction.followup.send(
                "This action can only be used inside a server.",
                ephemeral=True,
            )

        clean_val = (
            self.query_input.value
            .strip()
            .replace("<@", "")
            .replace(">", "")
            .replace("!", "")
        )

        try:
            target_id = int(clean_val)
        except ValueError:
            return await interaction.followup.send(
                "Invalid user ID provided.",
                ephemeral=True,
            )

        member = interaction.guild.get_member(
            target_id
        )

        if not member:
            try:
                member = await interaction.guild.fetch_member(
                    target_id
                )
            except discord.HTTPException:
                return await interaction.followup.send(
                    "Could not find this user in the server.",
                    ephemeral=True,
                )

        await show_vouch_profile(
            interaction,
            member,
            ephemeral=True,
        )


# ============================================================
# VOUCH ACTION VIEW
# ============================================================

class VouchActionButtons(
    discord.ui.View
):

    def __init__(
        self,
        target_id: int,
    ):
        super().__init__(
            timeout=60
        )

        self.target_id = target_id

    @discord.ui.button(
        label="Give Positive Vouch",
        style=discord.ButtonStyle.green,
        emoji="⭐",
    )
    async def pos_vouch(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_modal(
            VouchSubmitModal(
                self.target_id,
                "POSITIVE",
            )
        )

    @discord.ui.button(
        label="Give Negative Vouch",
        style=discord.ButtonStyle.red,
        emoji="⚠️",
    )
    async def neg_vouch(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await interaction.response.send_modal(
            VouchSubmitModal(
                self.target_id,
                "NEGATIVE",
            )
        )


# ============================================================
# VOUCH DASHBOARD
# ============================================================

class VouchDashboardView(
    discord.ui.View
):

    def __init__(self):
        super().__init__(
            timeout=None
        )

    @discord.ui.button(
        label="My Trust Profile",
        style=discord.ButtonStyle.primary,
        emoji="🛡️",
        custom_id="vouch_my_profile",
    )
    async def my_profile(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not interaction.guild:
            return await interaction.response.send_message(
                "This action can only be used inside a server.",
                ephemeral=True,
            )

        await show_vouch_profile(
            interaction,
            interaction.user,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Check Member",
        style=discord.ButtonStyle.secondary,
        emoji="🔍",
        custom_id="vouch_check_member",
    )
    async def check_member(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not interaction.guild:
            return await interaction.response.send_message(
                "This action can only be used inside a server.",
                ephemeral=True,
            )

        await interaction.response.send_modal(
            VouchTargetModal()
        )

    @discord.ui.button(
        label="Submit Vouch",
        style=discord.ButtonStyle.success,
        emoji="✍️",
        custom_id="vouch_submit_action",
    )
    async def submit_vouch(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not interaction.guild:
            return await interaction.response.send_message(
                "This action can only be used inside a server.",
                ephemeral=True,
            )

        await interaction.response.send_modal(
            VouchTargetModal()
        )

    @discord.ui.button(
        label="Trust Guidelines",
        style=discord.ButtonStyle.secondary,
        emoji="📖",
        custom_id="vouch_guidelines",
    )
    async def guidelines(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        embed = make_embed(
            "Trust Network Guidelines",
            "**Trust Ratings range from 0 to 100.**\n\n"
            "• **0 Trust:** Restricted from trading.\n"
            "• **100 Trust:** Elite verified status.\n\n"
            "*Always verify member trust scores before completing any deals.*",
            COLOR_PURPLE,
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )

    @discord.ui.button(
        label="Leaderboard",
        style=discord.ButtonStyle.secondary,
        emoji="🏆",
        custom_id="vouch_leaderboard",
    )
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        if not interaction.guild:
            return await interaction.followup.send(
                "This action can only be used inside a server.",
                ephemeral=True,
            )

        try:
            rows = await db_controller.fetchall(
                """
                SELECT
                    user_id,
                    trust_score,
                    vouch_positive
                FROM user_vouch_network
                WHERE guild_id = ?
                ORDER BY
                    trust_score DESC,
                    user_id ASC
                LIMIT 10
                """,
                (
                    interaction.guild.id,
                ),
            )

            if not rows:
                return await interaction.followup.send(
                    "No trust data recorded yet.",
                    ephemeral=True,
                )

            desc = ""

            medals = [
                "🥇",
                "🥈",
                "🥉",
            ]

            for idx, (
                uid,
                score,
                pos,
            ) in enumerate(rows):

                prefix = (
                    medals[idx]
                    if idx < 3
                    else f"`{idx + 1}.`"
                )

                desc += (
                    f"{prefix} <@{uid}> — "
                    f"Trust Score: **{score}/100** "
                    f"(+{pos} Positive)\n"
                )

            embed = make_embed(
                "🏆 Community Trust Leaderboard",
                desc,
                COLOR_PURPLE,
            )

            await interaction.followup.send(
                embed=embed,
                ephemeral=True,
            )

        except Exception:
            logger.exception(
                "Error fetching leaderboard"
            )

            await interaction.followup.send(
                "An error occurred while fetching the leaderboard.",
                ephemeral=True,
            )


# ============================================================
# VOUCH PROFILE
# ============================================================

async def show_vouch_profile(
    interaction: discord.Interaction,
    member: discord.Member,
    ephemeral: bool = True,
):

    if not interaction.guild:
        message = (
            "This action can only be used inside a server."
        )

        if interaction.response.is_done():
            return await interaction.followup.send(
                message,
                ephemeral=True,
            )

        return await interaction.response.send_message(
            message,
            ephemeral=True,
        )

    try:
        guild_id = interaction.guild.id

        data = await db_controller.fetchone(
            """
            SELECT
                trust_score,
                vouch_positive,
                vouch_negative,
                vouches_given
            FROM user_vouch_network
            WHERE guild_id = ?
              AND user_id = ?
            """,
            (
                guild_id,
                member.id,
            ),
        )

        score = data[0] if data else 50
        pos = data[1] if data else 0
        neg = data[2] if data else 0
        given = data[3] if data else 0

        blocks = int(score / 10)

        bar = (
            "🟩" * blocks
            + "⬛" * (10 - blocks)
        )

        history_rows = await db_controller.fetchall(
            """
            SELECT
                vouch_type,
                reason
            FROM vouch_history
            WHERE guild_id = ?
              AND target_id = ?
            ORDER BY timestamp DESC
            LIMIT 3
            """,
            (
                guild_id,
                member.id,
            ),
        )

        if history_rows:
            history_text = ""

            for (
                v_type,
                raw_reason,
            ) in history_rows:

                icon = (
                    "⭐"
                    if v_type == "POSITIVE"
                    else "⚠️"
                )

                safe_reason = (
                    discord.utils.escape_mentions(
                        discord.utils.escape_markdown(
                            raw_reason or ""
                        )
                    )
                )

                history_text += (
                    f"{icon} {safe_reason}\n"
                )
        else:
            history_text = (
                "No feedback history yet."
            )

        description = (
            "**Trust Standing**\n"
            f"Score: **{score} / 100**\n"
            f"{bar}\n\n"
            "**Activity Statistics**\n"
            f"• Positive Vouches: `{pos}`\n"
            f"• Negative Vouches: `{neg}`\n"
            f"• Vouches Given: `{given}`\n\n"
            "**Recent Feedback History**\n"
            f"{history_text}"
        )

        embed = discord.Embed(
            title=(
                f"Trust Profile: "
                f"{member.display_name}"
            ),
            description=description,
            color=COLOR_INFO,
        )

        if member.avatar:
            embed.set_thumbnail(
                url=member.avatar.url
            )

        view = VouchActionButtons(
            member.id
        )

        if interaction.response.is_done():
            await interaction.followup.send(
                embed=embed,
                view=view,
                ephemeral=ephemeral,
            )
        else:
            await interaction.response.send_message(
                embed=embed,
                view=view,
                ephemeral=ephemeral,
            )

    except Exception:
        logger.exception(
            "Error showing vouch profile for user %s",
            member.id,
        )

        message = (
            "An error occurred while loading the trust profile."
        )

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


# ============================================================
# GIVEAWAY LEAVE VIEW
# ============================================================

class GiveawayLeaveView(
    discord.ui.View
):

    def __init__(
        self,
        active_view,
        original_message,
    ):
        super().__init__(
            timeout=60
        )

        self.active_view = active_view
        self.original_message = original_message

    @discord.ui.button(
        label="Leave Giveaway",
        style=discord.ButtonStyle.red,
    )
    async def leave_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if not interaction.guild:
            return await interaction.response.send_message(
                "This action can only be performed inside a server.",
                ephemeral=True,
            )

        await interaction.response.defer(
            ephemeral=True
        )

        async with get_giveaway_lock(
            self.active_view.message_id
        ):

            giveaway_info = await db_controller.fetchone(
                """
                SELECT status
                FROM giveaway_system
                WHERE message_id = ?
                """,
                (
                    self.active_view.message_id,
                ),
            )

            if (
                not giveaway_info
                or giveaway_info[0] != "ACTIVE"
            ):
                return await interaction.followup.send(
                    "This giveaway has already ended or is no longer active.",
                    ephemeral=True,
                )

            deleted = await db_controller.execute(
                """
                DELETE FROM giveaway_participants
                WHERE message_id = ?
                  AND user_id = ?
                """,
                (
                    self.active_view.message_id,
                    interaction.user.id,
                ),
            )

            if deleted == 0:
                return await interaction.followup.send(
                    "You were not entered in this giveaway.",
                    ephemeral=True,
                )

            count_res = await db_controller.fetchone(
                """
                SELECT COUNT(*)
                FROM giveaway_participants
                WHERE message_id = ?
                """,
                (
                    self.active_view.message_id,
                ),
            )

            count = (
                count_res[0]
                if count_res
                else 0
            )

            for child in self.active_view.children:
                if child.custom_id == "enter_giveaway":
                    child.label = str(count)

            if self.original_message:
                try:
                    await self.original_message.edit(
                        view=self.active_view
                    )
                except discord.HTTPException:
                    pass

        await interaction.followup.send(
            "You have successfully left the giveaway.",
            ephemeral=True,
        )

        self.stop()


# ============================================================
# ACTIVE GIVEAWAY VIEW
# ============================================================

class GiveawayActiveView(
    discord.ui.View
):

    def __init__(
        self,
        message_id,
        prize,
        winners,
        host,
    ):
        super().__init__(
            timeout=None
        )

        self.message_id = message_id
        self.prize = prize
        self.winners = winners
        self.host = host
        self.message = None

    @discord.ui.button(
        style=discord.ButtonStyle.secondary,
        emoji="🎉",
        custom_id="enter_giveaway",
        label="0",
    )
    async def enter_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        if not interaction.guild:
            return await interaction.response.send_message(
                "This giveaway can only be entered inside a server.",
                ephemeral=True,
            )

        if interaction.user.bot:
            return await interaction.response.send_message(
                "Bots cannot enter giveaways.",
                ephemeral=True,
            )

        await interaction.response.defer(
            ephemeral=True
        )

        self.message = interaction.message

        async with get_giveaway_lock(
            self.message_id
        ):

            giveaway_info = await db_controller.fetchone(
                """
                SELECT
                    req_daily,
                    req_weekly,
                    req_monthly,
                    req_total,
                    bypass_role_id,
                    status,
                    ends_at
                FROM giveaway_system
                WHERE message_id = ?
                """,
                (
                    self.message_id,
                ),
            )

            if not giveaway_info:
                return await interaction.followup.send(
                    "This giveaway no longer exists.",
                    ephemeral=True,
                )

            (
                req_daily,
                req_weekly,
                req_monthly,
                req_total,
                bypass_role_id,
                status,
                ends_at,
            ) = giveaway_info

            if (
                status != "ACTIVE"
                or time.time() >= ends_at
            ):
                return await interaction.followup.send(
                    "This giveaway has already ended.",
                    ephemeral=True,
                )

            bypass = False

            if (
                bypass_role_id
                and isinstance(
                    interaction.user,
                    discord.Member,
                )
            ):
                bypass = any(
                    role.id == bypass_role_id
                    for role in interaction.user.roles
                )

            if not bypass and (
                req_daily > 0
                or req_weekly > 0
                or req_monthly > 0
                or req_total > 0
            ):

                await flush_user_activity(
                    interaction.guild.id,
                    interaction.user.id,
                )

                now_nl = get_nl_now()

                today_str = (
                    now_nl.date().isoformat()
                )

                week_start_str = (
                    now_nl.date()
                    - datetime.timedelta(
                        days=now_nl.date().weekday()
                    )
                ).isoformat()

                month_start_str = (
                    now_nl.date()
                    .replace(day=1)
                    .isoformat()
                )

                activity = await db_controller.fetchone(
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
                    WHERE guild_id = ?
                      AND user_id = ?
                    """,
                    (
                        interaction.guild.id,
                        interaction.user.id,
                    ),
                )

                user_msgs = (
                    activity[0]
                    if activity
                    else 0
                )

                daily_msgs = (
                    activity[1]
                    if activity
                    and activity[4] == today_str
                    else 0
                )

                weekly_msgs = (
                    activity[2]
                    if activity
                    and activity[5] == week_start_str
                    else 0
                )

                monthly_msgs = (
                    activity[3]
                    if activity
                    and activity[6] == month_start_str
                    else 0
                )

                if (
                    req_total > 0
                    and user_msgs < req_total
                ):
                    return await interaction.followup.send(
                        f"Required total messages: "
                        f"**{req_total}**, "
                        f"you have: **{user_msgs}**.",
                        ephemeral=True,
                    )

                if (
                    req_daily > 0
                    and daily_msgs < req_daily
                ):
                    return await interaction.followup.send(
                        f"Required daily messages: "
                        f"**{req_daily}**, "
                        f"you have: **{daily_msgs}**.",
                        ephemeral=True,
                    )

                if (
                    req_weekly > 0
                    and weekly_msgs < req_weekly
                ):
                    return await interaction.followup.send(
                        f"Required weekly messages: "
                        f"**{req_weekly}**, "
                        f"you have: **{weekly_msgs}**.",
                        ephemeral=True,
                    )

                if (
                    req_monthly > 0
                    and monthly_msgs < req_monthly
                ):
                    return await interaction.followup.send(
                        f"Required monthly messages: "
                        f"**{req_monthly}**, "
                        f"you have: **{monthly_msgs}**.",
                        ephemeral=True,
                    )

            inserted_rows = await db_controller.execute(
                """
                INSERT OR IGNORE INTO giveaway_participants
                (
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

            if inserted_rows == 0:
                leave_view = GiveawayLeaveView(
                    self,
                    self.message,
                )

                return await interaction.followup.send(
                    "You've already entered! "
                    "Click below if you want to leave.",
                    view=leave_view,
                    ephemeral=True,
                )

            count_res = await db_controller.fetchone(
                """
                SELECT COUNT(*)
                FROM giveaway_participants
                WHERE message_id = ?
                """,
                (
                    self.message_id,
                ),
            )

            count = (
                count_res[0]
                if count_res
                else 1
            )

            button.label = str(count)

            if self.message:
                try:
                    await self.message.edit(
                        view=self
                    )
                except discord.HTTPException:
                    pass

            await interaction.followup.send(
                f"Entry confirmed for **{self.prize}**!",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Participants",
        style=discord.ButtonStyle.secondary,
        emoji="👥",
        custom_id="view_participants",
    )
    async def participants_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        await interaction.response.defer(
            ephemeral=True
        )

        count_res = await db_controller.fetchone(
            """
            SELECT COUNT(*)
            FROM giveaway_participants
            WHERE message_id = ?
            """,
            (
                self.message_id,
            ),
        )

        total_count = (
            count_res[0]
            if count_res
            else 0
        )

        embed = make_embed(
            "Giveaway Participants",
            f"Total Entries: **{total_count}**",
            COLOR_INFO,
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True,
        )


# ============================================================
# GIVEAWAY SETUP VIEW
# ============================================================

class GiveawaySetupView(
    discord.ui.View
):

    def __init__(
        self,
        prize,
        winners,
        duration,
        host,
        channel,
        end_color_hex,
        req_daily,
        req_weekly,
        req_monthly,
        req_total,
        bypass_role_id,
    ):

        super().__init__(
            timeout=900
        )

        self.prize = prize
        self.winners = winners
        self.duration = duration
        self.host = host
        self.channel = channel
        self.end_color_hex = end_color_hex

        self.req_daily = req_daily
        self.req_weekly = req_weekly
        self.req_monthly = req_monthly
        self.req_total = req_total
        self.bypass_role_id = bypass_role_id

        self.start_lock = asyncio.Lock()

    @discord.ui.button(
        label="Start",
        style=discord.ButtonStyle.green,
        emoji="▶️",
    )
    async def start_callback(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        async with self.start_lock:

            if interaction.response.is_done():
                return

            await interaction.response.defer(
                ephemeral=True
            )

            for child in self.children:
                child.disabled = True

            if interaction.message:
                try:
                    await interaction.message.edit(
                        view=self
                    )
                except discord.HTTPException:
                    pass

            ends_at = (
                time.time()
                + self.duration * 60
            )

            timestamp = int(ends_at)

            safe_prize = (
                discord.utils.escape_mentions(
                    discord.utils.escape_markdown(
                        self.prize
                    )
                )
            )

            description = (
                "Click 🎉 button to enter!\n\n"
                f"🎁 Prize: **{safe_prize}**\n"
                f"🏆 Winners: **{self.winners}**\n"
                f"⏱ Duration: **{self.duration}** minute(s)\n"
                f"👤 Host: {self.host.mention}\n\n"
                f"Ends at: <t:{timestamp}:R>"
            )

            embed_color, color_err = (
                validate_hex_color(
                    self.end_color_hex,
                    COLOR_SUCCESS,
                )
            )

            if color_err:
                return await interaction.edit_original_response(
                    content=f"Error: {color_err}",
                    embed=None,
                    view=None,
                )

            embed = make_embed(
                "🎉 GIVEAWAY ACTIVE 🎉",
                description,
                embed_color,
            )

            message = None

            try:
                message = await self.channel.send(
                    embed=embed
                )

                await db_controller.execute(
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
                        processing_started_at,
                        result_message_id,
                        req_daily,
                        req_weekly,
                        req_monthly,
                        req_total,
                        bypass_role_id,
                        end_color,
                        retry_count,
                        last_error,
                        result_winners,
                        result_participant_count
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?,
                        'ACTIVE',
                        0,
                        0,
                        ?, ?, ?, ?, ?, ?,
                        0,
                        NULL,
                        NULL,
                        0
                    )
                    """,
                    (
                        message.id,
                        self.channel.id,
                        interaction.guild.id,
                        self.prize,
                        ends_at,
                        self.winners,
                        self.host.id,
                        self.req_daily,
                        self.req_weekly,
                        self.req_monthly,
                        self.req_total,
                        self.bypass_role_id,
                        self.end_color_hex,
                    ),
                )

                view = GiveawayActiveView(
                    message.id,
                    self.prize,
                    self.winners,
                    self.host,
                )

                view.message = message

                for child in view.children:
                    if child.custom_id == "enter_giveaway":
                        child.label = "0"

                await message.edit(
                    view=view
                )

                interaction.client.add_view(
                    view,
                    message_id=message.id,
                )

                await interaction.edit_original_response(
                    content="Giveaway started successfully!",
                    embed=None,
                    view=None,
                )

                self.stop()

            except Exception:
                logger.exception(
                    "Giveaway creation failed."
                )

                if message:
                    try:
                        await db_controller.execute(
                            """
                            DELETE FROM giveaway_system
                            WHERE message_id = ?
                            """,
                            (
                                message.id,
                            ),
                        )
                    except Exception:
                        logger.exception(
                            "Giveaway database cleanup failed."
                        )

                    try:
                        await message.delete()
                    except Exception:
                        pass

                try:
                    await interaction.edit_original_response(
                        content="Failed to create the giveaway.",
                        embed=None,
                        view=None,
                    )
                except Exception:
                    pass

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.red,
        emoji="✖️",
    )
    async def cancel_callback(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):

        async with self.start_lock:

            for child in self.children:
                child.disabled = True

            if interaction.message:
                try:
                    await interaction.message.edit(
                        view=self
                    )
                except discord.HTTPException:
                    pass

            await interaction.response.edit_message(
                content="Giveaway cancelled.",
                embed=None,
                view=None,
            )

            self.stop()


# ============================================================
# BOT
# ============================================================

class ExtendedBotClient(
    commands.Bot
):

    def __init__(self):

        intents = discord.Intents.default()

        intents.guilds = True
        intents.members = True
        intents.message_content = True
        intents.guild_messages = True
        intents.voice_states = True
        intents.reactions = True

        super().__init__(
            command_prefix="!",
            intents=intents,
        )

        self.giveaway_tasks = set()

        self.ai_semaphore = asyncio.Semaphore(
            3
        )

        self.shutdown_started = False

    # ========================================================
    # ACTIVITY FLUSH LOOP
    # ========================================================

    @tasks.loop(seconds=3.0)
    async def flush_activity_buffer_loop(self):

        async with buffer_lock:

            if not message_buffer:
                return

            snapshot = dict(
                message_buffer
            )

            message_buffer.clear()

        now_nl = get_nl_now()

        today_str = (
            now_nl.date().isoformat()
        )

        week_start_str = (
            now_nl.date()
            - datetime.timedelta(
                days=now_nl.date().weekday()
            )
        ).isoformat()

        month_start_str = (
            now_nl.date()
            .replace(day=1)
            .isoformat()
        )

        queries = []

        for (
            guild_user,
            count,
        ) in snapshot.items():

            guild_id, user_id = guild_user

            queries.append(
                (
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
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, user_id)
                    DO UPDATE SET
                        message_count =
                            message_count + ?,

                        daily_message_count =
                            CASE
                                WHEN last_daily_date = ?
                                THEN daily_message_count + ?
                                ELSE ?
                            END,

                        week_message_count =
                            CASE
                                WHEN last_weekly_date = ?
                                THEN week_message_count + ?
                                ELSE ?
                            END,

                        month_message_count =
                            CASE
                                WHEN last_monthly_date = ?
                                THEN month_message_count + ?
                                ELSE ?
                            END,

                        last_daily_date = ?,
                        last_weekly_date = ?,
                        last_monthly_date = ?
                    """,
                    (
                        guild_id,
                        user_id,
                        count,
                        count,
                        count,
                        count,
                        today_str,
                        week_start_str,
                        month_start_str,

                        count,
                        today_str,
                        count,
                        count,

                        week_start_str,
                        count,
                        count,

                        month_start_str,
                        count,
                        count,

                        today_str,
                        week_start_str,
                        month_start_str,
                    ),
                )
            )

        try:
            await db_controller.transaction(
                queries
            )

        except Exception:
            async with buffer_lock:
                for key, count in snapshot.items():
                    message_buffer[key] = (
                        message_buffer.get(
                            key,
                            0,
                        )
                        + count
                    )

            logger.exception(
                "Failed to flush activity buffer."
            )

    @flush_activity_buffer_loop.before_loop
    async def before_flush_loop(self):
        await self.wait_until_ready()

    # ========================================================
    # MEMORY CLEANUP
    # ========================================================

    @tasks.loop(hours=6)
    async def cleanup_memory_caches_loop(self):

        try:
            now = time.time()

            for key, last_used in list(
                user_cooldowns.items()
            ):

                if now - last_used > 86400:

                    lock = ai_locks.get(key)

                    if (
                        lock is None
                        or not lock.locked()
                    ):
                        user_cooldowns.pop(
                            key,
                            None,
                        )

                        ai_locks.pop(
                            key,
                            None,
                        )

        except Exception:
            logger.exception(
                "Error cleaning memory caches."
            )

    @cleanup_memory_caches_loop.before_loop
    async def before_cleanup_loop(self):
        await self.wait_until_ready()

    # ========================================================
    # GIVEAWAY PROCESSOR
    # ========================================================

    async def process_giveaway(
        self,
        row,
    ):

        (
            msg_id,
            chan_id,
            guild_id,
            prize,
            num_winners,
            end_color_hex,
        ) = row

        async with get_giveaway_lock(
            msg_id
        ):

            try:

                check_status = await db_controller.fetchone(
                    """
                    SELECT
                        status,
                        result_message_id,
                        retry_count,
                        result_winners,
                        result_participant_count
                    FROM giveaway_system
                    WHERE message_id = ?
                    """,
                    (
                        msg_id,
                    ),
                )

                if not check_status:
                    return

                (
                    status,
                    current_result_message_id,
                    retry_count,
                    stored_winners_json,
                    stored_participant_count,
                ) = check_status

                if status in {
                    "COMPLETED",
                    "CANCELLED",
                    "FAILED",
                }:
                    return

                guild = self.get_guild(
                    guild_id
                )

                channel = None

                if guild:
                    channel = guild.get_channel(
                        chan_id
                    )

                if guild and not channel:
                    try:
                        channel = await guild.fetch_channel(
                            chan_id
                        )
                    except Exception:
                        channel = None

                # --------------------------------------------------------
                # Recover an already-sent result message.
                # --------------------------------------------------------

                if current_result_message_id:
                    if channel and isinstance(
                        channel,
                        discord.TextChannel,
                    ):
                        try:
                            await channel.fetch_message(
                                current_result_message_id
                            )

                            winners_json = (
                                stored_winners_json
                                or json.dumps([])
                            )

                            participant_count = (
                                stored_participant_count
                                or 0
                            )

                            await db_controller.transaction(
                                [
                                    (
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
                                            msg_id,
                                            guild_id,
                                            prize,
                                            participant_count,
                                            winners_json,
                                            time.time(),
                                        ),
                                    ),
                                    (
                                        """
                                        DELETE FROM giveaway_participants
                                        WHERE message_id = ?
                                        """,
                                        (
                                            msg_id,
                                        ),
                                    ),
                                    (
                                        """
                                        UPDATE giveaway_system
                                        SET
                                            status = 'COMPLETED',
                                            processing_started_at = 0,
                                            last_error = NULL
                                        WHERE message_id = ?
                                        """,
                                        (
                                            msg_id,
                                        ),
                                    ),
                                ]
                            )

                            logger.info(
                                "Recovered completed giveaway %s.",
                                msg_id,
                            )

                            return

                        except discord.NotFound:
                            await db_controller.execute(
                                """
                                UPDATE giveaway_system
                                SET result_message_id = 0
                                WHERE message_id = ?
                                """,
                                (
                                    msg_id,
                                ),
                            )

                        except discord.HTTPException:
                            logger.warning(
                                "Could not verify result message %s.",
                                current_result_message_id,
                            )
                            return

                # --------------------------------------------------------
                # Claim processing.
                # --------------------------------------------------------

                current_time = time.time()

                affected = await db_controller.execute(
                    """
                    UPDATE giveaway_system
                    SET
                        status = 'PROCESSING',
                        processing_started_at = ?
                    WHERE message_id = ?
                      AND status IN ('ACTIVE', 'PROCESSING')
                    """,
                    (
                        current_time,
                        msg_id,
                    ),
                )

                if affected != 1:
                    return

                if not guild:
                    await db_controller.execute(
                        """
                        UPDATE giveaway_system
                        SET
                            status = 'ACTIVE',
                            processing_started_at = 0
                        WHERE message_id = ?
                        """,
                        (
                            msg_id,
                        ),
                    )
                    return

                if not channel or not isinstance(
                    channel,
                    discord.TextChannel,
                ):
                    await db_controller.execute(
                        """
                        UPDATE giveaway_system
                        SET
                            status = 'CANCELLED',
                            processing_started_at = 0
                        WHERE message_id = ?
                        """,
                        (
                            msg_id,
                        ),
                    )
                    return

                # --------------------------------------------------------
                # Get participants.
                # --------------------------------------------------------

                participant_rows = (
                    await db_controller.fetchall(
                        """
                        SELECT user_id
                        FROM giveaway_participants
                        WHERE message_id = ?
                        """,
                        (
                            msg_id,
                        ),
                    )
                )

                user_ids = [
                    row[0]
                    for row in participant_rows
                ]

                valid_users = []

                for user_id in user_ids:

                    member = guild.get_member(
                        user_id
                    )

                    if not member:
                        try:
                            member = await guild.fetch_member(
                                user_id
                            )
                        except Exception:
                            member = None

                    if member:
                        valid_users.append(
                            member
                        )

                embed_color, _ = validate_hex_color(
                    end_color_hex,
                    COLOR_SUCCESS,
                )

                selected_winner_ids = []

                if valid_users:
                    selected_winners = (
                        secrets.SystemRandom().sample(
                            valid_users,
                            min(
                                num_winners,
                                len(valid_users),
                            ),
                        )
                    )

                    selected_winner_ids = [
                        member.id
                        for member in selected_winners
                    ]

                winners_json = json.dumps(
                    selected_winner_ids
                )

                # --------------------------------------------------------
                # Persist result BEFORE sending Discord message.
                # --------------------------------------------------------

                await db_controller.execute(
                    """
                    UPDATE giveaway_system
                    SET
                        result_winners = ?,
                        result_participant_count = ?
                    WHERE message_id = ?
                    """,
                    (
                        winners_json,
                        len(user_ids),
                        msg_id,
                    ),
                )

                footer_marker = (
                    f"Giveaway ID: {msg_id}"
                )

                safe_prize = (
                    discord.utils.escape_mentions(
                        discord.utils.escape_markdown(
                            prize
                        )
                    )
                )

                if selected_winner_ids:

                    winners_mention = "\n".join(
                        f"<@{user_id}>"
                        for user_id in selected_winner_ids
                    )

                    ended_description = (
                        "🎉 Giveaway Ended!\n\n"
                        f"🎁 Prize: **{safe_prize}**\n\n"
                        f"🏆 Winner(s):\n"
                        f"{winners_mention}\n\n"
                        "Congratulations!"
                    )

                    result_message = (
                        await channel.send(
                            embed=make_embed(
                                "Giveaway Ended",
                                ended_description,
                                embed_color,
                                footer_text=footer_marker,
                            )
                        )
                    )

                else:

                    result_message = (
                        await channel.send(
                            embed=make_embed(
                                "Giveaway Ended",
                                (
                                    "🎉 Giveaway Ended!\n\n"
                                    f"🎁 Prize: **{safe_prize}**\n\n"
                                    "❌ No valid participants."
                                ),
                                COLOR_WARNING,
                                footer_text=footer_marker,
                            )
                        )
                    )

                await db_controller.execute(
                    """
                    UPDATE giveaway_system
                    SET result_message_id = ?
                    WHERE message_id = ?
                    """,
                    (
                        result_message.id,
                        msg_id,
                    ),
                )

                try:
                    original_message = (
                        await channel.fetch_message(
                            msg_id
                        )
                    )

                    await original_message.edit(
                        view=None
                    )

                except Exception:
                    pass

                await db_controller.transaction(
                    [
                        (
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
                                msg_id,
                                guild_id,
                                prize,
                                len(user_ids),
                                winners_json,
                                time.time(),
                            ),
                        ),
                        (
                            """
                            DELETE FROM giveaway_participants
                            WHERE message_id = ?
                            """,
                            (
                                msg_id,
                            ),
                        ),
                        (
                            """
                            UPDATE giveaway_system
                            SET
                                status = 'COMPLETED',
                                processing_started_at = 0,
                                result_message_id = ?,
                                last_error = NULL
                            WHERE message_id = ?
                            """,
                            (
                                result_message.id,
                                msg_id,
                            ),
                        ),
                    ]
                )

                logger.info(
                    "Giveaway %s completed.",
                    msg_id,
                )

            except Exception as error:

                logger.exception(
                    "Error processing giveaway %s",
                    msg_id,
                )

                retry_count = (
                    retry_count or 0
                ) + 1

                error_text = str(error)

                if retry_count >= 5:

                    await db_controller.execute(
                        """
                        UPDATE giveaway_system
                        SET
                            status = 'FAILED',
                            processing_started_at = 0,
                            retry_count = ?,
                            last_error = ?
                        WHERE message_id = ?
                        """,
                        (
                            retry_count,
                            error_text,
                            msg_id,
                        ),
                    )

                else:

                    await db_controller.execute(
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
                            retry_count,
                            error_text,
                            msg_id,
                        ),
                    )

    # ========================================================
    # GIVEAWAY LOOP
    # ========================================================

    @tasks.loop(minutes=1)
    async def background_giveaway_loop(self):

        try:

            current_time = time.time()

            rows = await db_controller.fetchall(
                """
                SELECT
                    message_id,
                    channel_id,
                    guild_id,
                    prize,
                    winners,
                    end_color
                FROM giveaway_system
                WHERE status = 'ACTIVE'
                  AND ends_at <= ?
                """,
                (
                    current_time,
                ),
            )

            for row in rows:

                task = asyncio.create_task(
                    self.process_giveaway(row)
                )

                self.giveaway_tasks.add(
                    task
                )

                task.add_done_callback(
                    self.giveaway_tasks.discard
                )

        except Exception:
            logger.exception(
                "Giveaway loop error."
            )

    @background_giveaway_loop.before_loop
    async def before_giveaway_loop(self):
        await self.wait_until_ready()

    # ========================================================
    # SETUP HOOK
    # ========================================================

    async def setup_hook(self):

        await db_controller.initialize_database()

        # Recover giveaways that were stuck in PROCESSING.
        stale_threshold = (
            time.time() - 600
        )

        await db_controller.execute(
            """
            UPDATE giveaway_system
            SET
                status = 'ACTIVE',
                processing_started_at = 0
            WHERE status = 'PROCESSING'
              AND processing_started_at < ?
            """,
            (
                stale_threshold,
            ),
        )

        # Recover expired giveaways immediately.
        expired_rows = (
            await db_controller.fetchall(
                """
                SELECT
                    message_id,
                    channel_id,
                    guild_id,
                    prize,
                    winners,
                    end_color
                FROM giveaway_system
                WHERE status = 'ACTIVE'
                  AND ends_at <= ?
                """,
                (
                    time.time(),
                ),
            )
        )

        for row in expired_rows:

            task = asyncio.create_task(
                self.process_giveaway(row)
            )

            self.giveaway_tasks.add(
                task
            )

            task.add_done_callback(
                self.giveaway_tasks.discard
            )

        # Restore active giveaway buttons.
        active_giveaways = (
            await db_controller.fetchall(
                """
                SELECT
                    message_id,
                    channel_id,
                    prize,
                    winners,
                    host_id
                FROM giveaway_system
                WHERE status = 'ACTIVE'
                """
            )
        )

        for row in active_giveaways:

            (
                message_id,
                channel_id,
                prize,
                winners,
                host_id,
            ) = row

            channel = self.get_channel(
                channel_id
            )

            if not channel:
                try:
                    channel = await self.fetch_channel(
                        channel_id
                    )
                except Exception:
                    channel = None

            message = None

            if isinstance(
                channel,
                discord.TextChannel,
            ):

                try:
                    message = await channel.fetch_message(
                        message_id
                    )

                except discord.NotFound:

                    await db_controller.execute(
                        """
                        UPDATE giveaway_system
                        SET
                            status = 'CANCELLED',
                            processing_started_at = 0
                        WHERE message_id = ?
                        """,
                        (
                            message_id,
                        ),
                    )

                    continue

                except discord.HTTPException:
                    logger.warning(
                        "Could not fetch giveaway message %s.",
                        message_id,
                    )

            host_member = None

            if host_id:
                host_member = self.get_user(
                    host_id
                )

            view = GiveawayActiveView(
                message_id,
                prize,
                winners,
                host_member,
            )

            view.message = message

            count_result = (
                await db_controller.fetchone(
                    """
                    SELECT COUNT(*)
                    FROM giveaway_participants
                    WHERE message_id = ?
                    """,
                    (
                        message_id,
                    ),
                )
            )

            count = (
                count_result[0]
                if count_result
                else 0
            )

            for child in view.children:
                if child.custom_id == "enter_giveaway":
                    child.label = str(count)

            self.add_view(
                view,
                message_id=message_id,
            )

        # Persistent vouch dashboard.
        self.add_view(
            VouchDashboardView()
        )

        # Start background loops.
        if not self.background_giveaway_loop.is_running():
            self.background_giveaway_loop.start()

        if not self.cleanup_memory_caches_loop.is_running():
            self.cleanup_memory_caches_loop.start()

        if not self.flush_activity_buffer_loop.is_running():
            self.flush_activity_buffer_loop.start()

        # ====================================================
        # VOUCH PANEL COMMAND
        # ====================================================

        @self.tree.command(
            name="vouchpanel",
            description="Post the official Community Trust & Vouch panel.",
        )
        @app_commands.guild_only()
        @app_commands.default_permissions(
            manage_guild=True
        )
        async def vouchpanel(
            interaction: discord.Interaction,
        ):

            if not await check_permission_and_respond(
                interaction,
                "manage_guild",
            ):
                return

            bot_member = interaction.guild.me

            if not bot_member:
                try:
                    bot_member = (
                        await interaction.guild.fetch_member(
                            self.user.id
                        )
                    )
                except Exception:
                    bot_member = None

            if bot_member:

                permissions = (
                    interaction.channel.permissions_for(
                        bot_member
                    )
                )

                if not (
                    permissions.send_messages
                    and permissions.embed_links
                ):
                    return await interaction.response.send_message(
                        "I do not have permission to send messages or embed links in this channel.",
                        ephemeral=True,
                    )

            await interaction.response.defer(
                ephemeral=True
            )

            description = (
                "**Community Trust & Vouch Hub**\n\n"
                "Build your reputation and check safety metrics before trading.\n"
                "All members start at **50 Trust out of 100**.\n\n"
                "**How It Works**\n"
                "• Completed a safe trade? Hit **Submit Vouch**.\n"
                "• Choose Positive (+Vouch) or Negative (-Vouch) with a reason.\n"
                "• Positive vouches increase trust; negative ones decrease it.\n\n"
                "*Only vouch for genuine, verified interactions.*"
            )

            embed = make_embed(
                "Trust & Safety Network",
                description,
                COLOR_PURPLE,
            )

            try:

                await interaction.channel.send(
                    embed=embed,
                    view=VouchDashboardView(),
                )

                await interaction.edit_original_response(
                    content="Vouch dashboard panel posted successfully!"
                )

            except discord.Forbidden:

                await interaction.edit_original_response(
                    content="Failed to post panel: Missing permissions."
                )

            except Exception:

                logger.exception(
                    "Error posting vouch panel."
                )

                await interaction.edit_original_response(
                    content="An unexpected error occurred."
                )

        # ====================================================
        # GIVEAWAY COMMAND GROUP
        # ====================================================

        giveaway_group = app_commands.Group(
            name="giveaway",
            description="Manage giveaways.",
        )

        @giveaway_group.command(
            name="create",
            description="Create a new giveaway.",
        )
        @app_commands.guild_only()
        @app_commands.default_permissions(
            manage_guild=True
        )
        @app_commands.describe(
            duration="Giveaway duration in minutes (1 - 43200)",
            winners="Number of winners (1-50)",
            prize="Prize name/text",
            channel="Discord text channel",
            host="Optional user hosting the giveaway",
            required_daily_messages="Minimum daily messages",
            required_weekly_messages="Minimum weekly messages",
            required_monthly_messages="Minimum monthly messages",
            required_total_messages="Minimum total messages",
            requirement_bypass_role="Role that bypasses message requirements",
            color="Setup embed color, e.g. #9B59B6",
            end_color="End embed color, e.g. #2ECC71",
        )
        async def giveaway_create(
            interaction: discord.Interaction,
            duration: int,
            winners: int,
            prize: str,
            channel: discord.TextChannel,
            host: Optional[discord.Member] = None,
            required_daily_messages: Optional[int] = 0,
            required_weekly_messages: Optional[int] = 0,
            required_monthly_messages: Optional[int] = 0,
            required_total_messages: Optional[int] = 0,
            requirement_bypass_role: Optional[discord.Role] = None,
            color: Optional[str] = "#9B59B6",
            end_color: Optional[str] = "#2ECC71",
        ):

            if not await check_permission_and_respond(
                interaction,
                "manage_guild",
            ):
                return

            clean_prize = prize.strip()

            req_daily = (
                required_daily_messages or 0
            )

            req_weekly = (
                required_weekly_messages or 0
            )

            req_monthly = (
                required_monthly_messages or 0
            )

            req_total = (
                required_total_messages or 0
            )

            max_message_requirement = 1_000_000

            if not 1 <= duration <= 43200:
                return await interaction.response.send_message(
                    embed=error_embed(
                        "Error",
                        "Duration must be between 1 and 43200 minutes.",
                    ),
                    ephemeral=True,
                )

            if not 1 <= winners <= 50:
                return await interaction.response.send_message(
                    embed=error_embed(
                        "Error",
                        "Winners must be between 1 and 50.",
                    ),
                    ephemeral=True,
                )

            if (
                not clean_prize
                or len(clean_prize) > 256
            ):
                return await interaction.response.send_message(
                    embed=error_embed(
                        "Error",
                        "The prize must contain 1-256 characters.",
                    ),
                    ephemeral=True,
                )

            requirements = [
                req_daily,
                req_weekly,
                req_monthly,
                req_total,
            ]

            if any(
                req < 0
                or req > max_message_requirement
                for req in requirements
            ):
                return await interaction.response.send_message(
                    embed=error_embed(
                        "Error",
                        "Message requirements must be between 0 and 1,000,000.",
                    ),
                    ephemeral=True,
                )

            if (
                requirement_bypass_role
                and requirement_bypass_role.is_default()
            ):
                return await interaction.response.send_message(
                    embed=error_embed(
                        "Error",
                        "You cannot use @everyone as a bypass role.",
                    ),
                    ephemeral=True,
                )

            _, color_error = validate_hex_color(
                color,
                COLOR_PURPLE,
            )

            if color_error:
                return await interaction.response.send_message(
                    embed=error_embed(
                        "Error",
                        f"Invalid embed color: {color}",
                    ),
                    ephemeral=True,
                )

            _, end_color_error = validate_hex_color(
                end_color,
                COLOR_SUCCESS,
            )

            if end_color_error:
                return await interaction.response.send_message(
                    embed=error_embed(
                        "Error",
                        f"Invalid end embed color: {end_color}",
                    ),
                    ephemeral=True,
                )

            if host is None:
                host = interaction.user

            bot_member = interaction.guild.me

            if not bot_member:
                try:
                    bot_member = (
                        await interaction.guild.fetch_member(
                            self.user.id
                        )
                    )
                except Exception:
                    bot_member = None

            if not bot_member:
                return await interaction.response.send_message(
                    embed=error_embed(
                        "Error",
                        "Could not verify bot permissions.",
                    ),
                    ephemeral=True,
                )

            permissions = (
                channel.permissions_for(
                    bot_member
                )
            )

            if not (
                permissions.view_channel
                and permissions.send_messages
                and permissions.embed_links
                and permissions.read_message_history
            ):
                return await interaction.response.send_message(
                    embed=error_embed(
                        "Error",
                        "I need View Channel, Send Messages, Embed Links and Read Message History in the target channel.",
                    ),
                    ephemeral=True,
                )

            safe_prize = (
                discord.utils.escape_mentions(
                    discord.utils.escape_markdown(
                        clean_prize
                    )
                )
            )

            setup_description = (
                "Click 🎉 button to enter!\n\n"
                f"🎁 Prize: **{safe_prize}**\n"
                f"🏆 Winners: **{winners}**\n"
                f"⏱ Duration: **{duration}** minute(s)\n"
                f"👤 Host: {host.mention}\n\n"
                "*Review the settings and click Start to launch.*"
            )

            embed_color, _ = validate_hex_color(
                color,
                COLOR_PURPLE,
            )

            setup_embed = make_embed(
                "🛠️ Giveaway Setup Panel",
                setup_description,
                embed_color,
            )

            view = GiveawaySetupView(
                prize=clean_prize,
                winners=winners,
                duration=duration,
                host=host,
                channel=channel,
                end_color_hex=end_color,
                req_daily=req_daily,
                req_weekly=req_weekly,
                req_monthly=req_monthly,
                req_total=req_total,
                bypass_role_id=(
                    requirement_bypass_role.id
                    if requirement_bypass_role
                    else 0
                ),
            )

            await interaction.response.send_message(
                embed=setup_embed,
                view=view,
                ephemeral=True,
            )

        self.tree.add_command(
            giveaway_group
        )

        await self.tree.sync()

        logger.info(
            "Slash commands synced successfully."
        )

    # ========================================================
    # READY
    # ========================================================

    async def on_ready(self):

        logger.info(
            "Logged in as %s (ID: %s)",
            self.user,
            self.user.id,
        )

    # ========================================================
    # MESSAGE HANDLER / AI
    # ========================================================

    async def on_message(
        self,
        message: discord.Message,
    ):

        if (
            message.author.bot
            or not message.guild
        ):
            return

        guild_id = message.guild.id
        user_id = message.author.id

        async with buffer_lock:

            key = (
                guild_id,
                user_id,
            )

            message_buffer[key] = (
                message_buffer.get(
                    key,
                    0,
                )
                + 1
            )

        if (
            self.user
            and self.user.mentioned_in(message)
            and not message.mention_everyone
        ):

            cooldown_key = (
                guild_id,
                user_id,
            )

            if cooldown_key not in ai_locks:
                ai_locks[
                    cooldown_key
                ] = asyncio.Lock()

            async with ai_locks[
                cooldown_key
            ]:

                current_time = time.time()

                last_used = user_cooldowns.get(
                    cooldown_key,
                    0,
                )

                if (
                    current_time - last_used
                    < 5
                ):

                    await message.reply(
                        "Please wait a few seconds before asking again.",
                        delete_after=5,
                    )

                else:

                    user_cooldowns[
                        cooldown_key
                    ] = current_time

                    clean_text = (
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

                    if not clean_text:

                        await message.reply(
                            f"Hello {message.author.mention}! "
                            "How can I help you today?"
                        )

                    else:

                        clean_text = clean_text[:500]

                        async with message.channel.typing():

                            if groq_api_client:

                                try:

                                    async with self.ai_semaphore:

                                        completion = (
                                            await asyncio.wait_for(
                                                groq_api_client.chat.completions.create(
                                                    messages=[
                                                        {
                                                            "role": "system",
                                                            "content": (
                                                                "You are a helpful and concise "
                                                                "Discord server assistant. "
                                                                "Keep answers brief."
                                                            ),
                                                        },
                                                        {
                                                            "role": "user",
                                                            "content": clean_text,
                                                        },
                                                    ],
                                                    model=GROQ_MODEL,
                                                    max_tokens=150,
                                                ),
                                                timeout=10,
                                            )
                                        )

                                    reply_text = (
                                        completion
                                        .choices[0]
                                        .message
                                        .content
                                    )

                                    if reply_text:
                                        reply_text = (
                                            reply_text.strip()
                                        )

                                        if len(reply_text) > 2000:
                                            reply_text = (
                                                reply_text[:1997]
                                                + "..."
                                            )

                                        await message.reply(
                                            reply_text,
                                            allowed_mentions=discord.AllowedMentions.none(),
                                        )

                                    else:
                                        await message.reply(
                                            "No response generated."
                                        )

                                except asyncio.TimeoutError:

                                    await message.reply(
                                        "The AI request took too long. Please try again later."
                                    )

                                except Exception:

                                    logger.exception(
                                        "Groq AI error."
                                    )

                                    await message.reply(
                                        "An error occurred while communicating with the AI model."
                                    )

                            else:

                                await message.reply(
                                    "AI client is not configured."
                                )

        await self.process_commands(
            message
        )

    # ========================================================
    # GRACEFUL SHUTDOWN
    # ========================================================

    async def shutdown(self):

        if self.shutdown_started:
            return

        self.shutdown_started = True

        logger.info(
            "Starting graceful shutdown..."
        )

        loops = [
            self.background_giveaway_loop,
            self.cleanup_memory_caches_loop,
            self.flush_activity_buffer_loop,
        ]

        for loop in loops:
            if loop.is_running():
                loop.cancel()

        # Flush activity buffer.
        async with buffer_lock:

            snapshot = dict(
                message_buffer
            )

            message_buffer.clear()

        if snapshot:

            now_nl = get_nl_now()

            today_str = (
                now_nl.date().isoformat()
            )

            week_start_str = (
                now_nl.date()
                - datetime.timedelta(
                    days=now_nl.date().weekday()
                )
            ).isoformat()

            month_start_str = (
                now_nl.date()
                .replace(day=1)
                .isoformat()
            )

            queries = []

            for (
                guild_user,
                count,
            ) in snapshot.items():

                guild_id, user_id = guild_user

                queries.append(
                    (
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
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(guild_id, user_id)
                        DO UPDATE SET
                            message_count =
                                message_count + ?,

                            daily_message_count =
                                CASE
                                    WHEN last_daily_date = ?
                                    THEN daily_message_count + ?
                                    ELSE ?
                                END,

                            week_message_count =
                                CASE
                                    WHEN last_weekly_date = ?
                                    THEN week_message_count + ?
                                    ELSE ?
                                END,

                            month_message_count =
                                CASE
                                    WHEN last_monthly_date = ?
                                    THEN month_message_count + ?
                                    ELSE ?
                                END,

                            last_daily_date = ?,
                            last_weekly_date = ?,
                            last_monthly_date = ?
                        """,
                        (
                            guild_id,
                            user_id,
                            count,
                            count,
                            count,
                            count,
                            today_str,
                            week_start_str,
                            month_start_str,

                            count,
                            today_str,
                            count,
                            count,

                            week_start_str,
                            count,
                            count,

                            month_start_str,
                            count,
                            count,

                            today_str,
                            week_start_str,
                            month_start_str,
                        ),
                    )
                )

            try:
                await db_controller.transaction(
                    queries
                )

                logger.info(
                    "Final activity buffer flushed."
                )

            except Exception:

                logger.exception(
                    "Final activity buffer flush failed."
                )

        # Stop giveaway tasks.
        if self.giveaway_tasks:

            active_tasks = list(
                self.giveaway_tasks
            )

            for task in active_tasks:
                if not task.done():
                    task.cancel()

            await asyncio.gather(
                *active_tasks,
                return_exceptions=True,
            )

            self.giveaway_tasks.clear()

        if not self.is_closed():
            await self.close()

        logger.info(
            "Discord client closed."
        )


# ============================================================
# WEB SERVER
# ============================================================

async def handle_health(
    request,
):

    client: ExtendedBotClient = (
        request.app["bot"]
    )

    if client.is_ready():

        try:
            await db_controller.fetchone(
                "SELECT 1"
            )

            return web.json_response(
                {
                    "status": "healthy",
                    "bot": "online",
                    "database": "online",
                },
                status=200,
            )

        except Exception:

            return web.json_response(
                {
                    "status": "unhealthy",
                    "bot": "online",
                    "database": "offline",
                },
                status=503,
            )

    return web.json_response(
        {
            "status": "starting",
            "bot": "starting",
        },
        status=503,
    )


async def start_web_server(
    client: ExtendedBotClient,
):

    app = web.Application()

    app["bot"] = client

    app.router.add_get(
        "/",
        handle_health,
    )

    app.router.add_get(
        "/health",
        handle_health,
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        WEB_PORT,
    )

    await site.start()

    logger.info(
        "HTTP health server listening on port %s",
        WEB_PORT,
    )

    return runner


# ============================================================
# MAIN
# ============================================================

async def main():

    client = ExtendedBotClient()

    web_runner = None

    try:

        # Initialize database before health server.
        await db_controller.initialize_database()

        web_runner = await start_web_server(
            client
        )

        logger.info(
            "Starting Discord bot..."
        )

        await client.start(
            BOT_TOKEN
        )

    except Exception:

        logger.exception(
            "Bot stopped because of an error."
        )

        raise

    finally:

        logger.info(
            "Starting shutdown sequence..."
        )

        try:
            await client.shutdown()
        except Exception:
            logger.exception(
                "Error during bot shutdown."
            )

        if web_runner:

            try:
                await web_runner.cleanup()
            except Exception:
                logger.exception(
                    "Error shutting down HTTP server."
                )

        try:
            await db_controller.close()
        except Exception:
            logger.exception(
                "Error closing database."
            )

        logger.info(
            "Shutdown complete."
        )


if __name__ == "__main__":

    try:
        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped manually."
    )
