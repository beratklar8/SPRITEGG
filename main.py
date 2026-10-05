import os
import time
import asyncio
import datetime
import traceback
import secrets
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from aiohttp import web
from dotenv import load_dotenv
from groq import AsyncGroq
import zoneinfo

import aiosqlite
from database import DatabaseController

load_dotenv()

BOT_TOKEN = os.getenv("DISCORD_TOKEN")
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0"))
WEB_PORT = int(os.getenv("PORT", 10000))
GROQ_API_SECRET = os.getenv("GROQ_API_KEY")

missing_secrets = []
if not BOT_TOKEN:
    missing_secrets.append("DISCORD_TOKEN")

if missing_secrets:
    raise RuntimeError(f"Critical Error: Missing required environment variables: {', '.join(missing_secrets)}. Secure startup halted.")

db_controller = DatabaseController()

COLOR_NEUTRAL = 0x2B2D31
COLOR_SUCCESS = 0x2ECC71
COLOR_WARNING = 0xF1C40F
COLOR_DANGER = 0xE74C3C
COLOR_INFO = 0x3498DB
COLOR_PURPLE = 0x9B59B6

groq_api_client = None
if GROQ_API_SECRET:
    try:
        groq_api_client = AsyncGroq(api_key=GROQ_API_SECRET)
    except Exception as initialization_exception:
        print(f"Failed to initialize Groq client: {initialization_exception}")

user_cooldowns = {}
ai_locks = {}
giveaway_entry_locks = {}

def get_nl_now() -> datetime.datetime:
    """Returns current datetime in Dutch timezone (Europe/Amsterdam, supporting DST)."""
    return datetime.datetime.now(zoneinfo.ZoneInfo("Europe/Amsterdam"))

def validate_hex_color(color_str: str, default: int) -> int:
    if not color_str or not isinstance(color_str, str):
        return default
    cleaned = color_str.strip().lstrip('#')
    if len(cleaned) == 6 and all(c in "0123456789abcdefABCDEF" for c in cleaned):
        try:
            return int(cleaned, 16)
        except ValueError:
            pass
    return default

def normalize_reason(reason: Optional[str]) -> str:
    reason = reason or "No reason provided"
    return reason[:512]

def is_owner_or_special(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    if BOT_OWNER_ID != 0 and interaction.user.id == BOT_OWNER_ID:
        return True
    return interaction.user == interaction.guild.owner

def has_permission(interaction: discord.Interaction, permission_name: str) -> bool:
    if is_owner_or_special(interaction):
        return True
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False
    permissions = interaction.user.guild_permissions
    if getattr(permissions, "administrator", False):
        return True
    return getattr(permissions, permission_name, False)

async def check_permission_and_respond(interaction: discord.Interaction, permission_name: str) -> bool:
    if not has_permission(interaction, permission_name):
        embed = error_embed("Access Denied", "You do not have permission to use this command.")
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    return True

def can_moderate_member(issuer: discord.Member, target: discord.Member) -> bool:
    if not isinstance(issuer, discord.Member) or not isinstance(target, discord.Member):
        return False
    if issuer.id == target.id:
        return False
    if issuer.guild.owner_id == issuer.id:
        return True
    if target.guild.owner_id == target.id:
        return False
    return issuer.top_role > target.top_role

def can_bot_moderate(guild: discord.Guild, target: discord.Member, permission_needed: str) -> bool:
    bot_member = guild.me
    if not bot_member or not isinstance(target, discord.Member):
        return False
    if target.id == bot_member.id or target.id == guild.owner_id:
        return False
    perms = guild.me.guild_permissions
    if not getattr(perms, permission_needed, False):
        return False
    return bot_member.top_role > target.top_role

def make_embed(title: str, description: str, color: int = COLOR_NEUTRAL) -> discord.Embed:
    embed_instance = discord.Embed(title=title, description=description, color=color)
    embed_instance.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed_instance

def success_embed(title: str, description: str) -> discord.Embed:
    return make_embed(f"✔ {title}", description, COLOR_SUCCESS)

def error_embed(title: str, description: str) -> discord.Embed:
    return make_embed(f"✖ {title}", description, COLOR_DANGER)

def warning_embed(title: str, description: str) -> discord.Embed:
    return make_embed(f"⚠ {title}", description, COLOR_WARNING)

def info_embed(title: str, description: str) -> discord.Embed:
    return make_embed(f"ℹ {title}", description, COLOR_INFO)

async def send_dm_notification(member: discord.Member, action_title: str, reason: str, guild_name: str, extra_info: Optional[str] = None) -> bool:
    try:
        desc = f"An action was taken regarding your account in **{guild_name}**.\n\n**Action:** {action_title}\n**Reason:** {reason}"
        if extra_info:
            desc += f"\n**Details:** {extra_info}"
        embed = make_embed(f"Notification: {action_title}", desc, COLOR_WARNING)
        await member.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False

# --- INTERACTIVE GIVEAWAY UI VIEWS ---

class GiveawayLeaveView(discord.ui.View):
    def __init__(self, active_view):
        super().__init__(timeout=60)
        self.active_view = active_view

    @discord.ui.button(label="Leave Giveaway", style=discord.ButtonStyle.red)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("This action can only be performed inside a server.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        self.active_view.message = interaction.message

        if self.active_view.message_id not in giveaway_entry_locks:
            giveaway_entry_locks[self.active_view.message_id] = asyncio.Lock()

        async with giveaway_entry_locks[self.active_view.message_id]:
            giveaway_info = await db_controller.fetchone(
                "SELECT status FROM giveaway_system WHERE message_id = ?",
                (self.active_view.message_id,)
            )

            if not giveaway_info or giveaway_info[0] != 'ACTIVE':
                return await interaction.followup.send("This giveaway has already ended or is no longer active.", ephemeral=True)

            await db_controller.execute(
                "DELETE FROM giveaway_participants WHERE message_id = ? AND user_id = ?",
                (self.active_view.message_id, interaction.user.id)
            )
            
            count_res = await db_controller.fetchone(
                "SELECT COUNT(*) FROM giveaway_participants WHERE message_id = ?",
                (self.active_view.message_id,)
            )
            count = count_res[0] if count_res else 0

            for child in self.active_view.children:
                if child.custom_id == "enter_giveaway":
                    child.label = str(count)
            
            if self.active_view.message:
                try:
                    await self.active_view.message.edit(view=self.active_view)
                except discord.HTTPException:
                    pass

        await interaction.followup.send("You have successfully left the giveaway.", ephemeral=True)
        self.stop()

class GiveawayActiveView(discord.ui.View):
    def __init__(self, message_id, prize, winners, host):
        super().__init__(timeout=None)
        self.message_id = message_id
        self.prize = prize
        self.winners = winners
        self.host = host
        self.message = None

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        try:
            embed = error_embed("Giveaway Error", "An unexpected error occurred while processing this interaction.")
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            pass

    @discord.ui.button(style=discord.ButtonStyle.secondary, emoji="🎉", custom_id="enter_giveaway")
    async def enter_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("This giveaway can only be entered inside a server.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        self.message = interaction.message

        if self.message_id not in giveaway_entry_locks:
            giveaway_entry_locks[self.message_id] = asyncio.Lock()

        async with giveaway_entry_locks[self.message_id]:
            giveaway_info = await db_controller.fetchone(
                "SELECT req_daily, req_weekly, req_monthly, req_total, bypass_role_id, status, ends_at FROM giveaway_system WHERE message_id = ?",
                (self.message_id,)
            )

            if not giveaway_info:
                return await interaction.followup.send("This giveaway no longer exists.", ephemeral=True)

            req_daily, req_weekly, req_monthly, req_total, bypass_role_id, status, ends_at = giveaway_info

            if status != 'ACTIVE' or time.time() >= ends_at:
                return await interaction.followup.send("This giveaway has already ended.", ephemeral=True)

            bypass = False
            if bypass_role_id and isinstance(interaction.user, discord.Member):
                if any(role.id == bypass_role_id for role in interaction.user.roles):
                    bypass = True

            if not bypass and (req_daily > 0 or req_weekly > 0 or req_monthly > 0 or req_total > 0):
                now_nl = get_nl_now()
                today_str = now_nl.date().isoformat()
                week_start_str = (now_nl.date() - datetime.timedelta(days=now_nl.date().weekday())).isoformat()
                month_start_str = now_nl.date().replace(day=1).isoformat()

                activity = await db_controller.fetchone(
                    "SELECT message_count, daily_message_count, week_message_count, month_message_count, last_daily_date, last_weekly_date, last_monthly_date FROM user_activity WHERE guild_id = ? AND user_id = ?",
                    (interaction.guild.id, interaction.user.id)
                )
                
                user_msgs = activity[0] if activity else 0
                daily_msgs = activity[1] if activity and activity[4] == today_str else 0
                weekly_msgs = activity[2] if activity and activity[5] == week_start_str else 0
                monthly_msgs = activity[3] if activity and activity[6] == month_start_str else 0

                if req_total > 0 and user_msgs < req_total:
                    return await interaction.followup.send(f"You do not meet the total message requirement. Required: **{req_total}**, you have: **{user_msgs}**.", ephemeral=True)
                if req_daily > 0 and daily_msgs < req_daily:
                    return await interaction.followup.send(f"You do not meet the daily message requirement for today. Required: **{req_daily}**, you have: **{daily_msgs}**.", ephemeral=True)
                if req_weekly > 0 and weekly_msgs < req_weekly:
                    return await interaction.followup.send(f"You do not meet the weekly message requirement. Required: **{req_weekly}**, you have: **{weekly_msgs}**.", ephemeral=True)
                if req_monthly > 0 and monthly_msgs < req_monthly:
                    return await interaction.followup.send(f"You do not meet the monthly message requirement. Required: **{req_monthly}**, you have: **{monthly_msgs}**.", ephemeral=True)

            inserted_rows = await db_controller.execute(
                "INSERT OR IGNORE INTO giveaway_participants (message_id, user_id) VALUES (?, ?)",
                (self.message_id, interaction.user.id)
            )

            if inserted_rows == 0:
                leave_view = GiveawayLeaveView(self)
                return await interaction.followup.send("You've already entered this giveaway! If you would like to leave, click the button below.", view=leave_view, ephemeral=True)

            count_res = await db_controller.fetchone(
                "SELECT COUNT(*) FROM giveaway_participants WHERE message_id = ?",
                (self.message_id,)
            )
            count = count_res[0] if count_res else 1
            button.label = str(count)
            if self.message:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    pass

            await interaction.followup.send(f"Entry Confirmed!\nYour entry for the giveaway of **{self.prize}** is confirmed!", ephemeral=True)

    @discord.ui.button(label="Participants", style=discord.ButtonStyle.secondary, emoji="👥", custom_id="view_participants")
    async def participants_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        count_res = await db_controller.fetchone(
            "SELECT COUNT(*) FROM giveaway_participants WHERE message_id = ?",
            (self.message_id,)
        )
        total_count = count_res[0] if count_res else 0
        embed = make_embed("Giveaway Participants", f"Total Participants: **{total_count}**", COLOR_INFO)
        await interaction.followup.send(embed=embed, ephemeral=True)

class GiveawaySetupView(discord.ui.View):
    def __init__(self, prize, winners, duration, host, channel, end_color_hex, req_daily, req_weekly, req_monthly, req_total, bypass_role_id):
        super().__init__(timeout=900)
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

    @discord.ui.button(label="Start", style=discord.ButtonStyle.green, emoji="▶️")
    async def start_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        for child in self.children:
            child.disabled = True
        if interaction.message:
            try:
                await interaction.message.edit(view=self)
            except discord.HTTPException:
                pass

        ends_at = time.time() + (self.duration * 60)
        timestamp = int(ends_at)
        
        desc = (
            f"Click 🎉 button to enter!\n\n"
            f"🎁 Prize: **{self.prize}**\n"
            f"🏆 Winners: **{self.winners}**\n"
            f"⏱ Duration: **{self.duration}** minute(s)\n"
            f"👤 Host: {self.host.mention}\n\n"
            f"Ends at: <t:{timestamp}:R>"
        )
        
        embed_color = validate_hex_color(self.end_color_hex, COLOR_SUCCESS)
        embed = make_embed("🎉 GIVEAWAY ACTIVE 🎉", desc, embed_color)
        
        msg = None
        try:
            msg = await self.channel.send(embed=embed)
            
            # GECORRIGEERD: prize_name gewijzigd naar prize
            await db_controller.execute(
                """INSERT INTO giveaway_system 
                (message_id, channel_id, guild_id, prize, ends_at, winners, status, processing_started_at, result_message_id,
                 req_daily, req_weekly, req_monthly, req_total, bypass_role_id, end_color) 
                VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', 0, 0, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.id, self.channel.id, interaction.guild.id, self.prize, ends_at, self.winners,
                    self.req_daily, self.req_weekly, self.req_monthly, 
                    self.req_total, self.bypass_role_id, self.end_color_hex
                )
            )

            view = GiveawayActiveView(msg.id, self.prize, self.winners, self.host)
            view.message = msg
            
            for child in view.children:
                if child.custom_id == "enter_giveaway":
                    child.label = "0"
            
            await msg.edit(view=view)
            interaction.client.add_view(view, message_id=msg.id)
            
            await interaction.edit_original_response(content="Giveaway started successfully and posted to channel!", embed=None, view=None)
            self.stop()
        except Exception as e:
            print(f"Giveaway creation failed: {e}")
            traceback.print_exc()
            if msg:
                try:
                    await msg.delete()
                except Exception:
                    pass
                try:
                    await db_controller.execute("DELETE FROM giveaway_system WHERE message_id = ?", (msg.id,))
                except Exception:
                    pass
            
            for child in self.children:
                child.disabled = False
            if interaction.message:
                try:
                    await interaction.message.edit(view=self)
                except discord.HTTPException:
                    pass

            await interaction.edit_original_response(content="Failed to create the giveaway. Please try again.", embed=None, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red, emoji="✖️")
    async def cancel_callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Giveaway cancelled.", embed=None, view=None)
        self.stop()

# --- BOT CLIENT CLASS ---

class ExtendedBotClient(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        intents.guild_messages = True
        intents.voice_states = True
        intents.reactions = True
        super().__init__(command_prefix="!", intents=intents)

    @tasks.loop(hours=6)
    async def cleanup_memory_caches_loop(self):
        try:
            now = time.time()
            stale_keys = [k for k, last_used in user_cooldowns.items() if now - last_used > 86400]
            for k in stale_keys:
                user_cooldowns.pop(k, None)
                ai_locks.pop(k, None)
            stale_g_locks = [k for k, lock in list(giveaway_entry_locks.items()) if not lock.locked()]
            for k in stale_g_locks:
                giveaway_entry_locks.pop(k, None)
        except Exception as e:
            print(f"Error cleaning up memory caches: {e}")

    @tasks.loop(minutes=1)
    async def background_giveaway_loop(self):
        try:
            current_time = time.time()
            
            lease_timeout_threshold = current_time - 900
            await db_controller.execute(
                "UPDATE giveaway_system SET status = 'ACTIVE', processing_started_at = 0 WHERE status = 'PROCESSING' AND processing_started_at < ?",
                (lease_timeout_threshold,)
            )

            # GECORRIGEERD: prize_name gewijzigd naar prize
            rows = await db_controller.fetchall(
                "SELECT message_id, channel_id, guild_id, prize, winners, end_color FROM giveaway_system WHERE status = 'ACTIVE' AND ends_at <= ?", 
                (current_time,)
            )
            
            for row in rows:
                msg_id, chan_id, guild_id, prize, num_winners, end_color_hex = row
                
                if msg_id not in giveaway_entry_locks:
                    giveaway_entry_locks[msg_id] = asyncio.Lock()

                async with giveaway_entry_locks[msg_id]:
                    claimed_rows = await db_controller.execute(
                        "UPDATE giveaway_system SET status = 'PROCESSING', processing_started_at = ? WHERE message_id = ? AND status = 'ACTIVE'",
                        (current_time, msg_id)
                    )
                    if claimed_rows != 1:
                        continue
                
                    guild = self.get_guild(guild_id)
                    if not guild:
                        await db_controller.execute("UPDATE giveaway_system SET status = 'ACTIVE', processing_started_at = 0 WHERE message_id = ?", (msg_id,))
                        continue
                        
                    channel = guild.get_channel(chan_id)
                    if not channel or not isinstance(channel, discord.TextChannel):
                        await db_controller.execute("UPDATE giveaway_system SET status = 'ACTIVE', processing_started_at = 0 WHERE message_id = ?", (msg_id,))
                        continue
                    
                    try:
                        fresh_record = await db_controller.fetchone(
                            "SELECT result_message_id FROM giveaway_system WHERE message_id = ?",
                            (msg_id,)
                        )
                        result_msg_id = fresh_record[0] if fresh_record else 0

                        if result_msg_id != 0:
                            await db_controller.execute("DELETE FROM giveaway_participants WHERE message_id = ?", (msg_id,))
                            await db_controller.execute("UPDATE giveaway_system SET status = 'COMPLETED', processing_started_at = 0 WHERE message_id = ?", (msg_id,))
                            giveaway_entry_locks.pop(msg_id, None)
                            continue

                        await db_controller.execute(
                            "UPDATE giveaway_system SET processing_started_at = ? WHERE message_id = ? AND status = 'PROCESSING'",
                            (time.time(), msg_id)
                        )

                        part_rows = await db_controller.fetchall(
                            "SELECT user_id FROM giveaway_participants WHERE message_id = ?",
                            (msg_id,)
                        )
                        
                        user_ids = [r[0] for r in part_rows]
                        valid_users = []
                        
                        if user_ids:
                            sys_random = secrets.SystemRandom()
                            sys_random.shuffle(user_ids)
                            
                            for i, uid in enumerate(user_ids):
                                if i > 0 and i % 20 == 0:
                                    await db_controller.execute(
                                        "UPDATE giveaway_system SET processing_started_at = ? WHERE message_id = ? AND status = 'PROCESSING'",
                                        (time.time(), msg_id)
                                    )

                                if len(valid_users) >= num_winners * 3:
                                    break
                                u = guild.get_member(uid)
                                if not u:
                                    try:
                                        u = await guild.fetch_member(uid)
                                    except discord.HTTPException:
                                        pass
                                if u:
                                    valid_users.append(u)

                        embed_color = validate_hex_color(end_color_hex, COLOR_SUCCESS)
                        sent_result_msg = None

                        if valid_users:
                            selected_winners = sys_random.sample(valid_users, min(num_winners, len(valid_users)))
                            winners_mention = "\n".join([winner.mention for winner in selected_winners])
                            
                            ended_desc = f"🎉 Giveaway Ended!\n\n🎁 Prize: **{prize}**\n\n🏆 Winner(s):\n{winners_mention}\n\nCongratulations! 🎉"
                            sent_result_msg = await channel.send(embed=make_embed("Giveaway Ended", ended_desc, embed_color))
                        else:
                            ended_desc = f"🎉 Giveaway Ended!\n\n🎁 Prize: **{prize}**\n\n❌ No valid participants were found."
                            sent_result_msg = await channel.send(embed=make_embed("Giveaway Ended", ended_desc, COLOR_WARNING))

                        result_message_id_to_save = sent_result_msg.id if sent_result_msg else 0

                        try:
                            msg = await channel.fetch_message(msg_id)
                            if msg:
                                await msg.edit(view=None)
                        except Exception:
                            pass

                        await db_controller.execute("DELETE FROM giveaway_participants WHERE message_id = ?", (msg_id,))
                        await db_controller.execute(
                            "UPDATE giveaway_system SET status = 'COMPLETED', processing_started_at = 0, result_message_id = ? WHERE message_id = ?",
                            (result_message_id_to_save, msg_id)
                        )
                        giveaway_entry_locks.pop(msg_id, None)

                    except (discord.NotFound, discord.HTTPException) as api_err:
                        print(f"API error while processing giveaway {msg_id}: {api_err}")
                        fresh_record = await db_controller.fetchone(
                            "SELECT result_message_id FROM giveaway_system WHERE message_id = ?",
                            (msg_id,)
                        )
                        if fresh_record and fresh_record[0] != 0:
                            await db_controller.execute(
                                "UPDATE giveaway_system SET status = 'COMPLETED', processing_started_at = 0 WHERE message_id = ? AND status = 'PROCESSING'",
                                (msg_id,)
                            )
                        else:
                            await db_controller.execute(
                                "UPDATE giveaway_system SET status = 'COMPLETED', processing_started_at = 0 WHERE message_id = ? AND status = 'PROCESSING'",
                                (msg_id,)
                            )
                    except Exception as loop_err:
                        print(f"Unexpected error processing giveaway {msg_id}: {loop_err}")
                        await db_controller.execute(
                            "UPDATE giveaway_system SET status = 'COMPLETED', processing_started_at = 0 WHERE message_id = ? AND status = 'PROCESSING'",
                            (msg_id,)
                        )
        except Exception as e:
            print(f"Critical error in background_giveaway_loop: {e}")

    @background_giveaway_loop.before_loop
    async def before_giveaway_loop(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=1)
    async def background_tempban_loop(self):
        try:
            current_time = time.time()
            rows = await db_controller.fetchall("SELECT guild_id, target_id FROM temporary_bans WHERE expiry_timestamp <= ?", (current_time,))
            for row in rows:
                guild_id, target_id = row
                guild = self.get_guild(guild_id)
                if guild:
                    unbanned_successfully = False
                    try:
                        await guild.unban(discord.Object(id=target_id), reason="Temporary ban expired.")
                        unbanned_successfully = True
                    except discord.NotFound:
                        unbanned_successfully = True
                    except discord.HTTPException as http_err:
                        print(f"HTTPException while unbanning target {target_id} in guild {guild_id}: {http_err}")
                    
                    if unbanned_successfully:
                        await db_controller.execute("DELETE FROM temporary_bans WHERE guild_id = ? AND target_id = ?", (guild_id, target_id))
        except Exception as e:
            print(f"Critical error in background_tempban_loop: {e}")

    @background_tempban_loop.before_loop
    async def before_tempban_loop(self):
        await self.wait_until_ready()

    async def setup_hook(self):
        await db_controller.initialize_database()

        # GECORRIGEERD: prize_name gewijzigd naar prize
        active_giveaways = await db_controller.fetchall("SELECT message_id, channel_id, prize, winners FROM giveaway_system WHERE status = 'ACTIVE'")
        for row in active_giveaways:
            msg_id, channel_id, prize, winners = row
            view = GiveawayActiveView(msg_id, prize, winners, None)
            
            channel = self.get_channel(channel_id)
            if channel and isinstance(channel, discord.TextChannel):
                try:
                    view.message = await channel.fetch_message(msg_id)
                except discord.HTTPException:
                    pass

            part_count_res = await db_controller.fetchone(
                "SELECT COUNT(*) FROM giveaway_participants WHERE message_id = ?",
                (msg_id,)
            )
            count = part_count_res[0] if part_count_res else 0
            for child in view.children:
                if child.custom_id == "enter_giveaway":
                    child.label = str(count)

            self.add_view(view, message_id=msg_id)

        self.background_giveaway_loop.start()
        self.background_tempban_loop.start()
        self.cleanup_memory_caches_loop.start()

        vouch_group = app_commands.Group(name="vouch", description="Manage and view vouches.")

        @vouch_group.command(name="give", description="Vouch for someone.")
        @app_commands.guild_only()
        async def vouch_give(interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = "No reason provided"):
            await interaction.response.defer()
            if user.id == interaction.user.id:
                embed = error_embed("Vouch Error", "You cannot vouch for yourself.")
                return await interaction.followup.send(embed=embed, ephemeral=True)
            if user.bot:
                embed = error_embed("Vouch Error", "You cannot vouch for a bot.")
                return await interaction.followup.send(embed=embed, ephemeral=True)

            norm_reason = normalize_reason(reason)
            try:
                await db_controller.execute(
                    "INSERT INTO user_vouches (guild_id, target_id, giver_id, reason) VALUES (?, ?, ?, ?)",
                    (interaction.guild.id, user.id, interaction.user.id, norm_reason)
                )
            except aiosqlite.IntegrityError:
                embed = error_embed("Already Vouched", f"You have already vouched for {user.mention} in this server.")
                return await interaction.followup.send(embed=embed, ephemeral=True)

            res = await db_controller.fetchone(
                "SELECT COUNT(*) FROM user_vouches WHERE guild_id = ? AND target_id = ?",
                (interaction.guild.id, user.id)
            )
            total_vouches = res[0] if res else 1

            embed = make_embed(
                "✔ Vouch Recorded",
                f"⭐ {interaction.user.mention} submitted a vouch for {user.mention}! Total vouches: **{total_vouches}**",
                COLOR_SUCCESS
            )
            await interaction.followup.send(embed=embed)

        @vouch_group.command(name="leaderboard", description="View the top vouched users in the server.")
        @app_commands.guild_only()
        async def vouch_leaderboard(interaction: discord.Interaction):
            await interaction.response.defer()
            rows = await db_controller.fetchall(
                "SELECT target_id, COUNT(*) as cnt FROM user_vouches WHERE guild_id = ? GROUP BY target_id ORDER BY cnt DESC LIMIT 10",
                (interaction.guild.id,)
            )
            if not rows:
                embed = info_embed("Vouch Leaderboard", "No vouches have been recorded in this server yet.")
                return await interaction.followup.send(embed=embed, ephemeral=True)

            medals = ["👑", "🥈", "🥉"]
            desc = ""
            for index, (target_id, count) in enumerate(rows):
                prefix = medals[index] if index < 3 else f"`{index + 1}.`"
                desc += f"{prefix} <@{target_id}> — **{count}** vouches\n"

            embed = make_embed("🏆 Vouch Leaderboard", desc, COLOR_PURPLE)
            await interaction.followup.send(embed=embed)

        self.tree.add_command(vouch_group)

        giveaway_group = app_commands.Group(name="giveaway", description="Manage giveaways.")

        @giveaway_group.command(name="create", description="Create a new giveaway with setup panel.")
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.describe(
            duration="Giveaway duration in minutes (minimum 1)",
            winners="Number of winners (1-50)",
            prize="Prize name/text",
            channel="Discord text channel where the giveaway should be posted",
            host="Optional user hosting the giveaway",
            required_daily_messages="Minimum required daily messages to enter",
            required_weekly_messages="Minimum required weekly messages to enter",
            required_monthly_messages="Minimum required monthly messages to enter",
            required_total_messages="Minimum required total messages to enter",
            requirement_bypass_role="Role that bypasses message requirements",
            color="Embed color (Hex code like #9B59B6)",
            end_color="Embed color when giveaway ends (Hex code like #2ECC71)"
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
            end_color: Optional[str] = "#2ECC71"
        ):
            if not await check_permission_and_respond(interaction, "manage_guild"):
                return
            
            req_daily = required_daily_messages or 0
            req_weekly = required_weekly_messages or 0
            req_monthly = required_monthly_messages or 0
            req_total = required_total_messages or 0

            MAX_REQUIREMENT = 1_000_000
            requirements = [req_daily, req_weekly, req_monthly, req_total]

            if duration < 1:
                embed = error_embed("Error", "Duration must be at least 1 minute.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            if winners < 1 or winners > 50:
                embed = error_embed("Error", "Winners must be between 1 and 50.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            if len(prize) > 256:
                embed = error_embed("Error", "The prize text cannot exceed 256 characters.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)

            if any(req < 0 or req > MAX_REQUIREMENT for req in requirements):
                embed = error_embed("Error", f"Message requirements must be between 0 and {MAX_REQUIREMENT:,}.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)

            if host is None:
                host = interaction.user

            bot_member = interaction.guild.me
            perms = channel.permissions_for(bot_member)
            if not (perms.send_messages and perms.embed_links):
                embed = error_embed("Error", "I do not have permission to send messages and embed links in that channel.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)

            setup_desc = (
                f"Click 🎉 button to enter!\n\n"
                f"🎁 Prize: **{prize}**\n"
                f"🏆 Winners: **{winners}**\n"
                f"⏱ Duration: **{duration}** minute(s)\n"
                f"👤 Host: {host.mention}\n\n"
                f"*Review settings below and click Start to launch.*"
            )

            embed_color = validate_hex_color(color, COLOR_PURPLE)
            setup_embed = make_embed("🛠️ Giveaway Setup Panel", setup_desc, embed_color)
            
            view = GiveawaySetupView(
                prize=prize,
                winners=winners,
                duration=duration,
                host=host,
                channel=channel,
                end_color_hex=end_color,
                req_daily=req_daily,
                req_weekly=req_weekly,
                req_monthly=req_monthly,
                req_total=req_total,
                bypass_role_id=requirement_bypass_role.id if requirement_bypass_role else 0
            )

            await interaction.response.send_message(embed=setup_embed, view=view, ephemeral=True)

        self.tree.add_command(giveaway_group)

        @self.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            print(f"App command error: {error}")
            traceback.print_exception(type(error), error, error.__traceback__)
            embed = error_embed("Command Error", "An unexpected error occurred while processing this command.")
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="ban", description="Ban a member from the server.")
        @app_commands.guild_only()
        @app_commands.default_permissions(ban_members=True)
        async def ban_slash(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason provided"):
            if not await check_permission_and_respond(interaction, "ban_members"):
                return
            if not can_moderate_member(interaction.user, member):
                embed = error_embed("Error", "You cannot moderate this user due to role hierarchy.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            if not can_bot_moderate(interaction.guild, member, "ban_members"):
                embed = error_embed("Error", "I cannot moderate this user because they have a higher or equal role to me, or I lack permissions.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)

            norm_reason = normalize_reason(reason)
            await interaction.response.defer()
            try:
                dm_sent = await send_dm_notification(member, "Ban", norm_reason, interaction.guild.name)
                await member.ban(reason=norm_reason)
                msg = f"Successfully banned {member.mention}.\nReason: {norm_reason}"
                if not dm_sent:
                    msg += "\n*(Note: Could not send DM notification to user)*"
                embed = success_embed("Member Banned", msg)
                await interaction.followup.send(embed=embed)
            except discord.HTTPException:
                embed = error_embed("Error", "Failed to ban member due to an API error.")
                await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="unban", description="Unban a user by their user ID.")
        @app_commands.guild_only()
        @app_commands.default_permissions(ban_members=True)
        async def unban_slash(interaction: discord.Interaction, user_id: str, reason: Optional[str] = "No reason provided"):
            if not await check_permission_and_respond(interaction, "ban_members"):
                return
            norm_reason = normalize_reason(reason)
            await interaction.response.defer()
            try:
                uid = int(user_id)
                user_obj = discord.Object(id=uid)
                await interaction.guild.unban(user_obj, reason=norm_reason)
                embed = success_embed("User Unbanned", f"Successfully unbanned user ID `{user_id}`.")
                await interaction.followup.send(embed=embed)
            except ValueError:
                embed = error_embed("Error", "Invalid user ID provided.")
                await interaction.followup.send(embed=embed, ephemeral=True)
            except discord.NotFound:
                embed = error_embed("Not Banned", f"User ID `{user_id}` is not currently banned.")
                await interaction.followup.send(embed=embed, ephemeral=True)
            except discord.HTTPException:
                embed = error_embed("Error", "Failed to unban user due to an API error.")
                await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="tempban", description="Temporary ban a member from the server.")
        @app_commands.guild_only()
        @app_commands.default_permissions(ban_members=True)
        async def tempban_slash(interaction: discord.Interaction, member: discord.Member, duration_hours: float, reason: Optional[str] = "No reason provided"):
            if not await check_permission_and_respond(interaction, "ban_members"):
                return
            if duration_hours <= 0 or duration_hours > 8760:
                embed = error_embed("Error", "Duration must be greater than 0 and at most 8760 hours (1 year).")
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            if not can_moderate_member(interaction.user, member):
                embed = error_embed("Error", "You cannot moderate this user due to role hierarchy.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            if not can_bot_moderate(interaction.guild, member, "ban_members"):
                embed = error_embed("Error", "I cannot moderate this user because they have a higher or equal role to me, or I lack permissions.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)

            norm_reason = normalize_reason(reason)
            await interaction.response.defer()
            expiry = time.time() + (duration_hours * 3600)
            
            try:
                await send_dm_notification(member, "Temporary Ban", norm_reason, interaction.guild.name, f"Duration: {duration_hours} hours")
                await member.ban(reason=norm_reason)
                
                try:
                    await db_controller.execute(
                        "INSERT OR REPLACE INTO temporary_bans (guild_id, target_id, expiry_timestamp) VALUES (?, ?, ?)",
                        (interaction.guild.id, member.id, expiry)
                    )
                except Exception as db_err:
                    try:
                        await interaction.guild.unban(discord.Object(id=member.id), reason="Temporary ban registration failed.")
                    except discord.HTTPException as rollback_err:
                        print(f"CRITICAL: Failed to rollback tempban for {member.id} in guild {interaction.guild.id}: {rollback_err}")
                    raise RuntimeError(f"Database error during tempban registration: {db_err}")

                embed = success_embed("Temporary Ban Applied", f"Successfully banned {member.mention} for {duration_hours} hours.")
                await interaction.followup.send(embed=embed)
            except discord.HTTPException:
                embed = error_embed("Error", "Failed to temp-ban member due to a Discord API error.")
                await interaction.followup.send(embed=embed, ephemeral=True)
            except Exception as e:
                embed = error_embed("Error", "Failed to complete temp-ban.")
                await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="kick", description="Kick a member from the server.")
        @app_commands.guild_only()
        @app_commands.default_permissions(kick_members=True)
        async def kick_slash(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason provided"):
            if not await check_permission_and_respond(interaction, "kick_members"):
                return
            if not can_moderate_member(interaction.user, member):
                embed = error_embed("Error", "You cannot moderate this user due to role hierarchy.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            if not can_bot_moderate(interaction.guild, member, "kick_members"):
                embed = error_embed("Error", "I cannot moderate this user because they have a higher or equal role to me, or I lack permissions.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)

            norm_reason = normalize_reason(reason)
            await interaction.response.defer()
            try:
                await member.kick(reason=norm_reason)
                await send_dm_notification(member, "Kick", norm_reason, interaction.guild.name)
                embed = success_embed("Member Kicked", f"Successfully kicked {member.mention}.")
                await interaction.followup.send(embed=embed)
            except discord.HTTPException:
                embed = error_embed("Error", "Failed to kick member due to an API error.")
                await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="timeout", description="Timeout a member for a specified duration in minutes.")
        @app_commands.guild_only()
        @app_commands.default_permissions(moderate_members=True)
        async def timeout_slash(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: Optional[str] = "No reason provided"):
            if not await check_permission_and_respond(interaction, "moderate_members"):
                return
            if minutes <= 0 or minutes > 40320:
                embed = error_embed("Error", "Minutes must be between 1 and 40320 (28 days).")
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            if not can_moderate_member(interaction.user, member):
                embed = error_embed("Error", "You cannot moderate this user due to role hierarchy.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            if not can_bot_moderate(interaction.guild, member, "moderate_members"):
                embed = error_embed("Error", "I cannot moderate this user because they have a higher or equal role to me, or I lack permissions.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)

            norm_reason = normalize_reason(reason)
            await interaction.response.defer()
            try:
                until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
                await member.timeout(until, reason=norm_reason)
                await send_dm_notification(member, "Timeout", norm_reason, interaction.guild.name, f"Duration: {minutes} minutes")
                embed = success_embed("Timeout Applied", f"Successfully timed out {member.mention} for {minutes} minutes.")
                await interaction.followup.send(embed=embed)
            except discord.HTTPException:
                embed = error_embed("Error", "Failed to timeout member due to an API error.")
                await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="untimeout", description="Remove timeout from a member.")
        @app_commands.guild_only()
        @app_commands.default_permissions(moderate_members=True)
        async def untimeout_slash(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason provided"):
            if not await check_permission_and_respond(interaction, "moderate_members"):
                return
            if not can_moderate_member(interaction.user, member):
                embed = error_embed("Error", "You cannot moderate this user due to role hierarchy.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            if not can_bot_moderate(interaction.guild, member, "moderate_members"):
                embed = error_embed("Error", "I cannot moderate this user because they have a higher or equal role to me, or I lack permissions.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)

            norm_reason = normalize_reason(reason)
            await interaction.response.defer()
            try:
                await member.timeout(None, reason=norm_reason)
                embed = success_embed("Timeout Removed", f"Successfully removed timeout for {member.mention}.")
                await interaction.followup.send(embed=embed)
            except discord.HTTPException:
                embed = error_embed("Error", "Failed to remove timeout due to an API error.")
                await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="purge", description="Bulk delete messages in the channel (max 100).")
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_messages=True)
        async def purge_slash(interaction: discord.Interaction, amount: int):
            if not await check_permission_and_respond(interaction, "manage_messages"):
                return
            if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
                embed = error_embed("Error", "This command can only be used in text channels or threads.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            if amount <= 0 or amount > 100:
                embed = error_embed("Error", "Amount must be between 1 and 100.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            await interaction.response.defer(ephemeral=True)
            try:
                deleted = await interaction.channel.purge(limit=amount)
                await interaction.followup.send(embed=success_embed("Purge Complete", f"Successfully deleted {len(deleted)} messages."), ephemeral=True)
            except discord.Forbidden:
                await interaction.followup.send(embed=error_embed("Error", "I do not have permission to delete messages in this channel."), ephemeral=True)
            except discord.HTTPException:
                await interaction.followup.send(embed=error_embed("Error", "Discord rejected the bulk delete request."), ephemeral=True)

        @self.tree.command(name="slowmode", description="Set the slowmode delay for the current channel in seconds.")
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_channels=True)
        async def slowmode_slash(interaction: discord.Interaction, seconds: int):
            if not await check_permission_and_respond(interaction, "manage_channels"):
                return
            if seconds < 0 or seconds > 21600:
                embed = error_embed("Error", "Slowmode must be between 0 and 21600 seconds (6 hours).")
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            await interaction.response.defer()
            try:
                await interaction.channel.edit(slowmode_delay=seconds)
                embed = success_embed("Slowmode Updated", f"Channel slowmode set to `{seconds}` seconds.")
                await interaction.followup.send(embed=embed)
            except discord.HTTPException:
                embed = error_embed("Error", "Failed to update slowmode due to an API error.")
                await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="say", description="Make the bot say something in the channel.")
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_messages=True)
        async def say_slash(interaction: discord.Interaction, message: str):
            if not await check_permission_and_respond(interaction, "manage_messages"):
                return
            if len(message) > 2000:
                embed = error_embed("Error", "The message cannot exceed 2000 characters.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            await interaction.response.defer(ephemeral=True)
            try:
                await interaction.channel.send(message, allowed_mentions=discord.AllowedMentions.none())
                embed = success_embed("Message Sent", "Done!")
                await interaction.followup.send(embed=embed, ephemeral=True)
            except discord.HTTPException:
                embed = error_embed("Error", "Failed to send message in this channel.")
                await interaction.followup.send(embed=embed, ephemeral=True)

        @self.tree.command(name="embed", description="Send a custom text inside an embed.")
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_messages=True)
        async def embed_slash(interaction: discord.Interaction, title: str, description: str):
            if not await check_permission_and_respond(interaction, "manage_messages"):
                return
            if len(title) > 256:
                embed = error_embed("Error", "The title cannot exceed 256 characters.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            if len(description) > 4096:
                embed = error_embed("Error", "The description cannot exceed 4096 characters.")
                return await interaction.response.send_message(embed=embed, ephemeral=True)

            await interaction.response.defer(ephemeral=True)
            try:
                await interaction.channel.send(embed=make_embed(title, description, COLOR_INFO), allowed_mentions=discord.AllowedMentions.none())
                embed = success_embed("Embed Sent", "Done!")
                await interaction.followup.send(embed=embed, ephemeral=True)
            except discord.HTTPException:
                embed = error_embed("Error", "Failed to send embed in this channel.")
                await interaction.followup.send(embed=embed, ephemeral=True)

        await self.tree.sync()
        print("Slash commands synced.")

    async def on_ready(self):
        print(f"Successfully logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="over server security"))

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id
        now_nl = get_nl_now()
        today_str = now_nl.date().isoformat()
        week_start_str = (now_nl.date() - datetime.timedelta(days=now_nl.date().weekday())).isoformat()
        month_start_str = now_nl.date().replace(day=1).isoformat()

        try:
            await db_controller.execute(
                """INSERT INTO user_activity (guild_id, user_id, message_count, daily_message_count, week_message_count, month_message_count, last_daily_date, last_weekly_date, last_monthly_date) 
                   VALUES (?, ?, 1, 1, 1, 1, ?, ?, ?) 
                   ON CONFLICT(guild_id, user_id) 
                   DO UPDATE SET 
                     message_count = message_count + 1,
                     daily_message_count = CASE WHEN last_daily_date = ? THEN daily_message_count + 1 ELSE 1 END,
                     week_message_count = CASE WHEN last_weekly_date = ? THEN week_message_count + 1 ELSE 1 END,
                     month_message_count = CASE WHEN last_monthly_date = ? THEN month_message_count + 1 ELSE 1 END,
                     last_daily_date = ?,
                     last_weekly_date = ?,
                     last_monthly_date = ?""",
                (
                    guild_id, message.author.id, today_str, week_start_str, month_start_str,
                    today_str, week_start_str, month_start_str, today_str, week_start_str, month_start_str
                )
            )
        except Exception as db_err:
            print(f"Activity logging error: {db_err}")

        if self.user.mentioned_in(message) and not message.mention_everyone:
            cooldown_key = (guild_id, message.author.id)
            if cooldown_key not in ai_locks:
                ai_locks[cooldown_key] = asyncio.Lock()
            
            async with ai_locks[cooldown_key]:
                current_time = time.time()
                last_used = user_cooldowns.get(cooldown_key, 0)
                if current_time - last_used < 5:
                    remaining = int(5 - (current_time - last_used))
                    await message.reply(f"Please wait {remaining} more seconds before using AI again.", delete_after=5)
                else:
                    user_cooldowns[cooldown_key] = current_time
                    clean_text = message.content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()
                    
                    if not clean_text:
                        await message.channel.send(f"Hello {message.author.mention}! How can I assist you with your queries today?", allowed_mentions=discord.AllowedMentions(replied_user=True))
                    else:
                        async with message.channel.typing():
                            if groq_api_client:
                                try:
                                    chat_completion = await groq_api_client.chat.completions.create(
                                        messages=[
                                            {
                                                "role": "system",
                                                "content": "You are a concise Discord server assistant. Give always short, direct, and concise answers without long blocks of text. Maximum 1 or 2 sentences."
                                            },
                                            {"role": "user", "content": clean_text}
                                        ],
                                        model="llama-3.3-70b-versatile",
                                        max_tokens=150,
                                    )
                                    reply_text = chat_completion.choices[0].message.content
                                    if not reply_text:
                                        await message.reply("Groq returned an empty response.", allowed_mentions=discord.AllowedMentions(replied_user=True))
                                    else:
                                        for i in range(0, len(reply_text), 1900):
                                            chunk = reply_text[i:i+1900]
                                            if i == 0:
                                                await message.reply(chunk, allowed_mentions=discord.AllowedMentions(replied_user=True))
                                            else:
                                                await message.channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
                                except Exception as err:
                                    traceback.print_exc()
                                    err_msg = str(err)
                                    if "429" in err_msg or "rate_limit" in err_msg.lower():
                                        await message.reply("API rate limit reached. Please try your request again shortly.", allowed_mentions=discord.AllowedMentions(replied_user=True))
                                    else:
                                        await message.reply("An error occurred while communicating with the AI model.", allowed_mentions=discord.AllowedMentions(replied_user=True))
                            else:
                                await message.reply("Groq API key is not configured.", allowed_mentions=discord.AllowedMentions(replied_user=True))

        await self.process_commands(message)

# --- AIOHTTP WEB SERVER & CLEANUP ---

async def handle_health(request):
    client: ExtendedBotClient = request.app['bot']
    if client.is_ready():
        return web.Response(text="Bot is fully running, connected, and ready!", status=200)
    else:
        return web.Response(text="Bot is starting up...", status=503)

async def start_web_server(client: ExtendedBotClient):
    app = web.Application()
    app['bot'] = client
    app.router.add_get("/", handle_health)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    print(f"Web server started on port {WEB_PORT}")
    return runner

async def main():
    client = ExtendedBotClient()
    web_runner = None
    try:
        web_runner = await start_web_server(client)
        await client.start(BOT_TOKEN)
    finally:
        if web_runner:
            await web_runner.cleanup()
        if groq_api_client and hasattr(groq_api_client, 'close'):
            try:
                await groq_api_client.close()
            except Exception:
                pass
        await db_controller.close()
        if not client.is_closed():
            await client.close()

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Error: DISCORD_TOKEN is missing in the environment variables.")
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("Bot shutdown gracefully.")
