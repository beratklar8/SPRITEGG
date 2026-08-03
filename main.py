import os
import re
import time
import random
import asyncio
import datetime
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite
from aiohttp import web
from dotenv import load_dotenv
from google import genai

load_dotenv()

# =====================================================================
# CONFIGURATION & ENVIRONMENT SETUP
# =====================================================================
BOT_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_NAME = os.getenv("DATABASE_PATH", "bot.db")
WEB_PORT = int(os.getenv("PORT", 10000))
GEMINI_API_SECRET = os.getenv("GEMINI_API_KEY")

COLOR_NEUTRAL = 0x2B2D31
COLOR_SUCCESS = 0x2ECC71
COLOR_WARNING = 0xF1C40F
COLOR_DANGER = 0xE74C3C
COLOR_INFO = 0x3498DB
COLOR_PURPLE = 0x9B59B6

# Initialize Gemini Client safely with advanced safety fallbacks
gemini_api_client = None
if GEMINI_API_SECRET:
    try:
        gemini_api_client = genai.Client(api_key=GEMINI_API_SECRET)
    except Exception as initialization_exception:
        print(f"Failed to initialize Gemini client: {initialization_exception}")

# In-memory stores for active features & cooldowns
sticky_messages = {}
user_cooldowns = {}

# =====================================================================
# EMBED UTILITY FACTORY
# =====================================================================
def make_embed(title: str, description: str, color: int = COLOR_NEUTRAL) -> discord.Embed:
    """Constructs a standardized base embed container with automatic timestamp."""
    embed_instance = discord.Embed(title=title, description=description, color=color)
    embed_instance.timestamp = datetime.datetime.now(datetime.timezone.utc)
    return embed_instance

def success_embed(title: str, description: str) -> discord.Embed:
    """Constructs a success status embed."""
    return make_embed(f"✔ {title}", description, COLOR_SUCCESS)

def error_embed(title: str, description: str) -> discord.Embed:
    """Constructs an error alert embed."""
    return make_embed(f"✖ {title}", description, COLOR_DANGER)

def warning_embed(title: str, description: str) -> discord.Embed:
    """Constructs a warning status embed."""
    return make_embed(f"⚠ {title}", description, COLOR_WARNING)

def info_embed(title: str, description: str) -> discord.Embed:
    """Constructs an information status embed."""
    return make_embed(f"ℹ {title}", description, COLOR_INFO)

# =====================================================================
# ADVANCED DATABASE CONTROLLER LAYER
# =====================================================================
class DatabaseController:
    """Manages all asynchronous SQLite database interactions and schema bootstrapping."""
    def __init__(self, db_path: str = DATABASE_NAME):
        self.db_path = db_path

    async def initialize_database(self):
        """Creates all necessary relational tables if they do not already exist."""
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS warning_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    target_id INTEGER,
                    moderator_id INTEGER,
                    reason TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS temporary_bans (
                    guild_id INTEGER,
                    target_id INTEGER,
                    expiry_timestamp REAL,
                    PRIMARY KEY (guild_id, target_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS giveaway_system (
                    message_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    guild_id INTEGER,
                    prize_name TEXT,
                    ends_at REAL,
                    is_ended BOOLEAN DEFAULT 0
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS server_settings (
                    guild_id INTEGER PRIMARY KEY,
                    automod_status BOOLEAN DEFAULT 0,
                    log_channel_id INTEGER
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS reaction_role_bindings (
                    message_id INTEGER,
                    emoji_icon TEXT,
                    role_id INTEGER,
                    PRIMARY KEY (message_id, emoji_icon)
                )
            """)
            await conn.commit()

    async def execute(self, query: str, params: tuple = ()):
        """Executes a modification query against the database safely."""
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(query, params)
            await conn.commit()
            return cursor

    async def fetchone(self, query: str, params: tuple = ()):
        """Fetches a single row from the database safely."""
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute(query, params) as cursor:
                return await cursor.fetchone()

    async def fetchall(self, query: str, params: tuple = ()):
        """Fetches multiple rows from the database safely."""
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute(query, params) as cursor:
                return await cursor.fetchall()

db_controller = DatabaseController()

# =====================================================================
# AUDIT LOG DISPATCHER HELPER
# =====================================================================
async def dispatch_audit_log(guild: discord.Guild, embed: discord.Embed):
    """Dispatches operational logs to the designated server audit channel securely."""
    if not guild:
        return
    res = await db_controller.fetchone("SELECT log_channel_id FROM server_settings WHERE guild_id = ?", (guild.id,))
    if res and res[0]:
        target_channel = guild.get_channel(res[0])
        if target_channel:
            try:
                await target_channel.send(embed=embed)
            except Exception:
                pass

# =====================================================================
# CORE BOT CLIENT IMPLEMENTATION
# =====================================================================
class ExtendedBotClient(commands.Bot):
    """Main application client encapsulating event handlers, filters, and background loops."""
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        intents.guild_messages = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        
        self.invite_link_regex = re.compile(r"(discord\.gg|discord\.com/invite)/[a-zA-Z0-9]+")
        self.server_invite_cache = {}

    async def setup_hook(self):
        """Asynchronous initialization routine for background tasks and command tree synchronization."""
        await db_controller.initialize_database()
        self.background_giveaway_loop.start()
        self.background_tempban_loop.start()
        
        # =====================================================================
        # SLASH COMMANDS REGISTRATION
        # =====================================================================

        @self.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            """Global error handler for all app/slash commands."""
            embed = error_embed("Command Error", str(error))
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="ban", description="Ban a member from the server.")
        @app_commands.default_permissions(ban_members=True)
        async def ban_slash(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason provided"):
            if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
                return await interaction.response.send_message(embed=error_embed("Permission Denied", "You cannot ban someone with an equal or higher role."), ephemeral=True)
            try:
                await member.ban(reason=reason)
                await interaction.response.send_message(embed=success_embed("Member Banned", f"Successfully banned {member.mention}.\nReason: {reason}"))
                await dispatch_audit_log(interaction.guild, error_embed("Member Banned", f"**Member:** {member.mention}\n**Moderator:** {interaction.user.mention}\n**Reason:** {reason}"))
            except Exception as e:
                await interaction.response.send_message(embed=error_embed("Error", f"Failed to ban member: {e}"), ephemeral=True)

        @self.tree.command(name="unban", description="Unban a user by their user ID.")
        @app_commands.default_permissions(ban_members=True)
        async def unban_slash(interaction: discord.Interaction, user_id: str, reason: Optional[str] = "No reason provided"):
            try:
                user_obj = discord.Object(id=int(user_id))
                await interaction.guild.unban(user_obj, reason=reason)
                await interaction.response.send_message(embed=success_embed("User Unbanned", f"Successfully unbanned user ID `{user_id}`."))
                await dispatch_audit_log(interaction.guild, success_embed("User Unbanned", f"**User ID:** `{user_id}`\n**Moderator:** {interaction.user.mention}"))
            except Exception as e:
                await interaction.response.send_message(embed=error_embed("Error", f"Failed to unban user: {e}"), ephemeral=True)

        @self.tree.command(name="tempban", description="Temporary ban a member from the server.")
        @app_commands.default_permissions(ban_members=True)
        async def tempban_slash(interaction: discord.Interaction, member: discord.Member, duration_hours: float, reason: Optional[str] = "No reason provided"):
            if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
                return await interaction.response.send_message(embed=error_embed("Permission Denied", "You cannot temp-ban this user."), ephemeral=True)
            expiry = time.time() + (duration_hours * 3600)
            try:
                await member.ban(reason=reason)
                await db_controller.execute("INSERT OR REPLACE INTO temporary_bans (guild_id, target_id, expiry_timestamp) VALUES (?, ?, ?)", (interaction.guild.id, member.id, expiry))
                await interaction.response.send_message(embed=success_embed("Temporary Ban Applied", f"Successfully banned {member.mention} for {duration_hours} hours."))
                await dispatch_audit_log(interaction.guild, warning_embed("Temporary Ban", f"**Member:** {member.mention}\n**Duration:** {duration_hours}h\n**Moderator:** {interaction.user.mention}"))
            except Exception as e:
                await interaction.response.send_message(embed=error_embed("Error", f"Failed to temp-ban member: {e}"), ephemeral=True)

        @self.tree.command(name="kick", description="Kick a member from the server.")
        @app_commands.default_permissions(kick_members=True)
        async def kick_slash(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason provided"):
            if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
                return await interaction.response.send_message(embed=error_embed("Permission Denied", "You cannot kick this user."), ephemeral=True)
            try:
                await member.kick(reason=reason)
                await interaction.response.send_message(embed=success_embed("Member Kicked", f"Successfully kicked {member.mention}."))
                await dispatch_audit_log(interaction.guild, warning_embed("Member Kicked", f"**Member:** {member.mention}\n**Moderator:** {interaction.user.mention}"))
            except Exception as e:
                await interaction.response.send_message(embed=error_embed("Error", f"Failed to kick member: {e}"), ephemeral=True)

        @self.tree.command(name="timeout", description="Timeout a member for a specified duration in minutes.")
        @app_commands.default_permissions(moderate_members=True)
        async def timeout_slash(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: Optional[str] = "No reason provided"):
            try:
                until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
                await member.timeout(until, reason=reason)
                await interaction.response.send_message(embed=success_embed("Timeout Applied", f"Successfully timed out {member.mention} for {minutes} minutes."))
                await dispatch_audit_log(interaction.guild, warning_embed("Member Timed Out", f"**Member:** {member.mention}\n**Duration:** {minutes}m\n**Moderator:** {interaction.user.mention}"))
            except Exception as e:
                await interaction.response.send_message(embed=error_embed("Error", f"Failed to timeout member: {e}"), ephemeral=True)

        @self.tree.command(name="untimeout", description="Remove timeout from a member.")
        @app_commands.default_permissions(moderate_members=True)
        async def untimeout_slash(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason provided"):
            try:
                await member.timeout(None, reason=reason)
                await interaction.response.send_message(embed=success_embed("Timeout Removed", f"Successfully removed timeout for {member.mention}."))
                await dispatch_audit_log(interaction.guild, success_embed("Timeout Removed", f"**Member:** {member.mention}\n**Moderator:** {interaction.user.mention}"))
            except Exception as e:
                await interaction.response.send_message(embed=error_embed("Error", f"Failed to remove timeout: {e}"), ephemeral=True)

        @self.tree.command(name="purge", description="Bulk delete messages in the channel.")
        @app_commands.default_permissions(manage_messages=True)
        async def purge_slash(interaction: discord.Interaction, amount: int):
            await interaction.response.defer(ephemeral=True)
            deleted = await interaction.channel.purge(limit=amount)
            await interaction.followup.send(embed=success_embed("Purge Complete", f"Successfully deleted {len(deleted)} messages."), ephemeral=True)

        @self.tree.command(name="warn", description="Issue an official warning to a member.")
        @app_commands.default_permissions(moderate_members=True)
        async def warn_slash(interaction: discord.Interaction, member: discord.Member, reason: str):
            await db_controller.execute("INSERT INTO warning_records (guild_id, target_id, moderator_id, reason) VALUES (?, ?, ?, ?)", (interaction.guild.id, member.id, interaction.user.id, reason))
            await interaction.response.send_message(embed=success_embed("Warning Issued", f"Warned {member.mention} for: {reason}"))
            await dispatch_audit_log(interaction.guild, warning_embed("Warning Logged", f"**User:** {member.mention}\n**Moderator:** {interaction.user.mention}\n**Reason:** {reason}"))

        @self.tree.command(name="warnings", description="View all warnings for a specific member.")
        @app_commands.default_permissions(moderate_members=True)
        async def warnings_slash(interaction: discord.Interaction, member: discord.Member):
            rows = await db_controller.fetchall("SELECT id, moderator_id, reason, timestamp FROM warning_records WHERE guild_id = ? AND target_id = ?", (interaction.guild.id, member.id))
            if not rows:
                return await interaction.response.send_message(embed=info_embed("No Warnings", f"{member.mention} has no active warnings recorded."), ephemeral=True)
            
            desc = f"Total warnings for {member.mention}: **{len(rows)}**\n\n"
            for r in rows:
                w_id, mod_id, reason, ts = r
                desc += f"**ID:** `{w_id}` | **Mod:** <@{mod_id}> | **Time:** `{ts}`\n**Reason:** {reason}\n\n"
            
            await interaction.response.send_message(embed=make_embed(f"Warnings for {member.name}", desc, COLOR_WARNING), ephemeral=True)

        @self.tree.command(name="slowmode", description="Set the slowmode delay for the current channel in seconds.")
        @app_commands.default_permissions(manage_channels=True)
        async def slowmode_slash(interaction: discord.Interaction, seconds: int):
            try:
                await interaction.channel.edit(slowmode_delay=seconds)
                await interaction.response.send_message(embed=success_embed("Slowmode Updated", f"Channel slowmode set to `{seconds}` seconds."))
            except Exception as e:
                await interaction.response.send_message(embed=error_embed("Error", f"Failed to update slowmode: {e}"), ephemeral=True)

        @self.tree.command(name="lock", description="Lock the current channel to prevent members from sending messages.")
        @app_commands.default_permissions(manage_channels=True)
        async def lock_slash(interaction: discord.Interaction):
            try:
                await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
                await interaction.response.send_message(embed=success_embed("Channel Locked", "This channel has been locked."))
            except Exception as e:
                await interaction.response.send_message(embed=error_embed("Error", f"Failed to lock channel: {e}"), ephemeral=True)

        @self.tree.command(name="unlock", description="Unlock the current channel.")
        @app_commands.default_permissions(manage_channels=True)
        async def unlock_slash(interaction: discord.Interaction):
            try:
                await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=True)
                await interaction.response.send_message(embed=success_embed("Channel Unlocked", "This channel has been unlocked."))
            except Exception as e:
                await interaction.response.send_message(embed=error_embed("Error", f"Failed to unlock channel: {e}"), ephemeral=True)

        @self.tree.command(name="say", description="Make the bot say something in the channel.")
        @app_commands.default_permissions(manage_messages=True)
        async def say_slash(interaction: discord.Interaction, message: str):
            await interaction.response.send_message(embed=success_embed("Message Sent", "Done!"), ephemeral=True)
            await interaction.channel.send(message)

        @self.tree.command(name="embed", description="Send a custom text inside an embed.")
        @app_commands.default_permissions(manage_messages=True)
        async def embed_slash(interaction: discord.Interaction, title: str, description: str):
            await interaction.response.send_message(embed=success_embed("Embed Sent", "Done!"), ephemeral=True)
            await interaction.channel.send(embed=make_embed(title, description, COLOR_INFO))

        @self.tree.command(name="automod", description="Toggle or configure the AutoMod filter status.")
        @app_commands.default_permissions(administrator=True)
        async def automod_slash(interaction: discord.Interaction, status: bool):
            res = await db_controller.fetchone("SELECT log_channel_id FROM server_settings WHERE guild_id = ?", (interaction.guild.id,))
            old_log = res[0] if res else None
            await db_controller.execute("INSERT OR REPLACE INTO server_settings(guild_id, automod_status, log_channel_id) VALUES (?, ?, ?)", (interaction.guild.id, status, old_log))
            await interaction.response.send_message(embed=success_embed("AutoMod Updated", f"AutoMod system status is now set to: `{status}`"))

        @self.tree.command(name="setlogchannel", description="Set the log channel for server events.")
        @app_commands.default_permissions(administrator=True)
        async def setlogchannel_slash(interaction: discord.Interaction, channel: discord.TextChannel):
            res = await db_controller.fetchone("SELECT automod_status FROM server_settings WHERE guild_id = ?", (interaction.guild.id,))
            old_automod = res[0] if res else 0
            await db_controller.execute("INSERT OR REPLACE INTO server_settings(guild_id, automod_status, log_channel_id) VALUES (?, ?, ?)", (interaction.guild.id, old_automod, channel.id))
            await interaction.response.send_message(embed=success_embed("Log Channel Configured", f"Audit logs will now be sent to {channel.mention}."))

        @self.tree.command(name="giveaway", description="Start a new server giveaway.")
        @app_commands.default_permissions(manage_guild=True)
        async def giveaway_slash(interaction: discord.Interaction, prize: str, duration_minutes: float):
            ends_at = time.time() + (duration_minutes * 60)
            embed = make_embed("🎉 GIVEAWAY 🎉", f"Prize: **{prize}**\nReact with 🎉 to enter!\nEnds: <t:{int(ends_at)}:R>", COLOR_PURPLE)
            await interaction.response.send_message(embed=success_embed("Giveaway Started", "Giveaway message deployed!"), ephemeral=True)
            msg = await interaction.channel.send(embed=embed)
            await msg.add_reaction("🎉")
            await db_controller.execute("INSERT INTO giveaway_system (message_id, channel_id, guild_id, prize_name, ends_at) VALUES (?, ?, ?, ?, ?)", (msg.id, interaction.channel.id, interaction.guild.id, prize, ends_at))

        @self.tree.command(name="sticky", description="Create or clear a sticky message in the channel.")
        @app_commands.default_permissions(manage_messages=True)
        async def sticky_slash(interaction: discord.Interaction, text: Optional[str] = None):
            if not text:
                if interaction.channel.id in sticky_messages:
                    del sticky_messages[interaction.channel.id]
                return await interaction.response.send_message(embed=success_embed("Sticky Removed", "Sticky message cleared for this channel."), ephemeral=True)
            
            msg = await interaction.channel.send(embed=make_embed("Pinned Operational Notice", text))
            sticky_messages[interaction.channel.id] = {"text": text, "message_id": msg.id}
            await interaction.response.send_message(embed=success_embed("Sticky Active", "Sticky message successfully deployed."), ephemeral=True)

        @self.tree.command(name="reactionrole", description="Bind a reaction emoji to a role on a message.")
        @app_commands.default_permissions(administrator=True)
        async def reactionrole_slash(interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
            try:
                m_id = int(message_id)
                msg = await interaction.channel.fetch_message(m_id)
                await msg.add_reaction(emoji)
                await db_controller.execute("INSERT OR REPLACE INTO reaction_role_bindings (message_id, emoji_icon, role_id) VALUES (?, ?, ?)", (m_id, emoji, role.id))
                await interaction.response.send_message(embed=success_embed("Reaction Role Bound", f"Successfully linked {emoji} to {role.mention} on message `{m_id}`."), ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(embed=error_embed("Error", f"Failed to bind reaction role: {e}"), ephemeral=True)

        await self.tree.sync()
        print("Slash commands synced.")

    async def on_ready(self):
        """Fires when the bot establishes connection and completes initialization."""
        print(f"Successfully logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="over server security | /help"))
        for guild in self.guilds:
            try:
                self.server_invite_cache[guild.id] = await guild.invites()
            except Exception:
                pass

    async def on_message(self, message: discord.Message):
        """Comprehensive message event listener handling AI interactions, cooldowns, automod, and sticky posts."""
        if message.author.bot or not message.guild:
            return

        # 1. Gemini AI Integration Handler + Cooldown Management (5s)
        if self.user.mentioned_in(message) and not message.mention_everyone:
            current_time = time.time()
            last_used = user_cooldowns.get(message.author.id, 0)
            if current_time - last_used < 5:
                remaining = int(5 - (current_time - last_used))
                return await message.reply(f"Please wait {remaining} more seconds before using Gemini AI again.", delete_after=5)
            
            user_cooldowns[message.author.id] = current_time
            clean_text = message.content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()
            
            if not clean_text:
                return await message.channel.send(f"Hello {message.author.mention}! How can I assist you with your queries today?")

            async with message.channel.typing():
                if gemini_api_client:
                    try:
                        ai_response = await gemini_api_client.aio.models.generate_content(
                            model="gemini-2.0-flash",
                            contents=clean_text
                        )
                        if not ai_response.text:
                            await message.reply("Gemini returned an empty response.")
                        else:
                            await message.reply(ai_response.text[:1900])
                    except Exception as err:
                        traceback.print_exc()
                        err_msg = str(err)
                        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                            await message.reply("API rate limit reached. Please try your request again shortly.")
                        else:
                            await message.reply(f"AI error encountered: `{type(err).__name__}`")

        # 2. Advanced Automod Filter Suite (Invite Links & Caps)
        settings = await db_controller.fetchone("SELECT automod_status FROM server_settings WHERE guild_id = ?", (message.guild.id,))
        if settings and settings[0] and not message.author.guild_permissions.manage_messages:
            if self.invite_link_regex.search(message.content):
                await message.delete()
                await dispatch_audit_log(message.guild, warning_embed("Automod Action Triggered", f"Blocked unauthorized invite link sent by {message.author.mention} in {message.channel.mention}."))
                return await message.channel.send(embed=warning_embed("Automod Notice", f"{message.author.mention}, server invite links are strictly prohibited."), delete_after=5)

            if len(message.content) > 10 and sum(1 for c in message.content if c.isupper()) / len(message.content) > 0.8:
                await message.delete()
                await dispatch_audit_log(message.guild, warning_embed("Automod Action Triggered", f"Removed uppercase text spam sent by {message.author.mention} in {message.channel.mention}."))
                return await message.channel.send(embed=warning_embed("Automod Notice", f"{message.author.mention}, please avoid excessive capitalization."), delete_after=5)

        # 3. Sticky Messages Handler
        if message.channel.id in sticky_messages:
            sticky_data = sticky_messages[message.channel.id]
            if sticky_data.get("message_id"):
                try:
                    old_msg = await message.channel.fetch_message(sticky_data["message_id"])
                    await old_msg.delete()
                except Exception:
                    pass
            new_sticky = await message.channel.send(embed=make_embed("Pinned Operational Notice", sticky_data["text"]))
            sticky_messages[message.channel.id]["message_id"] = new_sticky.id

        await self.process_commands(message)

    # =====================================================================
    # EXTENSIVE AUDIT LOGGING EVENT LISTENERS
    # =====================================================================
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        used_invite = None
        try:
            old_invites = self.server_invite_cache.get(guild.id, [])
            new_invites = await guild.invites()
            self.server_invite_cache[guild.id] = new_invites
            for new_inv in new_invites:
                for old_inv in old_invites:
                    if new_inv.code == old_inv.code and new_inv.uses > old_inv.uses:
                        used_invite = new_inv
                        break
        except Exception:
            pass

        invite_details = f"Invite: `{used_invite.code}` (Inviter: {used_invite.inviter}, Total Uses: {used_invite.uses})" if used_invite else "Invite tracking data unavailable"
        await dispatch_audit_log(guild, success_embed("Member Joined", f"**Member:** {member.mention} (`{member.id}`)\n{invite_details}"))

    async def on_member_remove(self, member: discord.Member):
        await dispatch_audit_log(member.guild, error_embed("Member Left", f"**Member:** {member.mention} (`{member.id}`)"))

    async def on_member_update(self, before: discord.Member, after: discord.Member):
        guild = after.guild
        old_roles = set(before.roles)
        new_roles = set(after.roles)

        for role in new_roles - old_roles:
            await dispatch_audit_log(guild, success_embed("Role Assigned", f"**Member:** {after.mention}\n**Role:** {role.mention}"))
        for role in old_roles - new_roles:
            await dispatch_audit_log(guild, warning_embed("Role Removed", f"**Member:** {after.mention}\n**Role:** {role.mention}"))

        if before.timed_out_until != after.timed_out_until and after.is_timed_out():
            await dispatch_audit_log(guild, warning_embed("Timeout Applied", f"**Member:** {after.mention}\n**Until:** <t:{int(after.timed_out_until.timestamp())}:F>"))

        if before.nick != after.nick:
            await dispatch_audit_log(guild, make_embed("Nickname Modified", f"**Member:** {after.mention}\nBefore: `{before.nick or before.name}`\nAfter: `{after.nick or after.name}`", COLOR_WARNING))

    async def on_member_ban(self, guild: discord.Guild, user: discord.abc.User):
        await dispatch_audit_log(guild, error_embed("User Banned", f"**User:** {user.mention} (`{user.id}`)"))

    async def on_member_unban(self, guild: discord.Guild, user: discord.abc.User):
        await dispatch_audit_log(guild, success_embed("User Unbanned", f"**User:** {user.mention} (`{user.id}`)"))

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        if not before.channel and after.channel:
            await dispatch_audit_log(guild, success_embed("Voice Channel Joined", f"**Member:** {member.mention} joined {after.channel.mention}"))
        elif before.channel and not after.channel:
            await dispatch_audit_log(guild, error_embed("Voice Channel Left", f"**Member:** {member.mention} left {before.channel.mention}"))
        elif before.channel and after.channel and before.channel.id != after.channel.id:
            await dispatch_audit_log(guild, make_embed("Voice Channel Switched", f"**Member:** {member.mention} moved from {before.channel.mention} to {after.channel.mention}", COLOR_WARNING))

    async def on_guild_role_create(self, role: discord.Role):
        await dispatch_audit_log(role.guild, success_embed("Role Created", f"**Role:** {role.mention}"))

    async def on_guild_role_delete(self, role: discord.Role):
        await dispatch_audit_log(role.guild, error_embed("Role Deleted", f"**Role Name:** `{role.name}`"))

    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        await dispatch_audit_log(channel.guild, success_embed("Channel Created", f"**Channel:** {channel.mention}"))

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        await dispatch_audit_log(channel.guild, error_embed("Channel Deleted", f"**Channel Name:** `{channel.name}`"))

    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        content_txt = message.content or "[No text data available]"
        await dispatch_audit_log(
            message.guild, 
            warning_embed(
                "Message Deleted", 
                f"**Author:** {message.author.mention}\n**Channel:** {message.channel.mention}\n**Content:**\n```{content_txt[:900]}```"
            )
        )

    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not before.guild or before.author.bot or before.content == after.content:
            return
        
        before_text = before.content or "[No text data available]"
        after_text = after.content or "[No text data available]"
        
        embed = make_embed(
            "Message Edited", 
            f"**Author:** {before.author.mention}\n**Channel:** {before.channel.mention}\n**Before:**\n```{before_text[:900]}```\n**After:**\n```{after_text[:900]}```", 
            COLOR_WARNING
        )
        await dispatch_audit_log(before.guild, embed)

    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        if user.bot:
            return
        res = await db_controller.fetchone(
            "SELECT role_id FROM reaction_role_bindings WHERE message_id = ? AND emoji_icon = ?",
            (reaction.message.id, str(reaction.emoji))
        )
        if res:
            role = reaction.message.guild.get_role(res[0])
            member = reaction.message.guild.get_member(user.id)
            if role and member:
                try:
                    await member.add_roles(role, reason="Reaction Role Assignment")
                except Exception:
                    pass

    async def on_reaction_remove(self, reaction: discord.Reaction, user: discord.User):
        if user.bot:
            return
        res = await db_controller.fetchone(
            "SELECT role_id FROM reaction_role_bindings WHERE message_id = ? AND emoji_icon = ?",
            (reaction.message.id, str(reaction.emoji))
        )
        if res:
            role = reaction.message.guild.get_role(res[0])
            member = reaction.message.guild.get_member(user.id)
            if role and member:
                try:
                    await member.remove_roles(role, reason="Reaction Role Revocation")
                except Exception:
                    pass

    # =====================================================================
    # BACKGROUND MAINTENANCE TASKS
    # =====================================================================
    @tasks.loop(seconds=30)
    async def background_giveaway_loop(self):
        """Monitors and processes concluded giveaways automatically."""
        current_time = time.time()
        expired_giveaways = await db_controller.fetchall(
            "SELECT message_id, channel_id, guild_id, prize_name FROM giveaway_system WHERE ends_at <= ? AND is_ended = 0",
            (current_time,)
        )

        for g in expired_giveaways:
            msg_id, chan_id, guild_id, prize = g
            await db_controller.execute("UPDATE giveaway_system SET is_ended = 1 WHERE message_id = ?", (msg_id,))
            
            guild = self.get_guild(guild_id)
            if not guild:
                continue
            channel = guild.get_channel(chan_id)
            if not channel:
                continue

            try:
                msg = await channel.fetch_message(msg_id)
            except Exception:
                continue

            winner = None
            for reaction in msg.reactions:
                if str(reaction.emoji) == "🎉":
                    users = [u async for u in reaction.users() if not u.bot]
                    if users:
                        winner = random.choice(users)
                    break

            if winner:
                await channel.send(embed=success_embed("Giveaway Concluded!", f"Congratulations {winner.mention}! You won **{prize}**!"))
            else:
                await channel.send(embed=warning_embed("Giveaway Concluded", f"The giveaway for **{prize}** ended, but no valid entries were recorded."))

    @background_giveaway_loop.before_loop
    async def before_giveaway_loop(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=30)
    async def background_tempban_loop(self):
        """Monitors and revokes expired temporary server bans automatically."""
        current_time = time.time()
        expired_bans = await db_controller.fetchall(
            "SELECT guild_id, target_id FROM temporary_bans WHERE expiry_timestamp <= ?",
            (current_time,)
        )

        for b in expired_bans:
            guild_id, target_id = b
            guild = self.get_guild(guild_id)
            if guild:
                try:
                    user = discord.Object(id=target_id)
                    await guild.unban(user, reason="Temporary ban period expired.")
                    await dispatch_audit_log(guild, success_embed("Temporary Ban Expired", f"User ID `{target_id}` has been automatically unbanned."))
                except Exception:
                    pass
            await db_controller.execute("DELETE FROM temporary_bans WHERE guild_id = ? AND target_id = ?", (guild_id, target_id))

    @background_tempban_loop.before_loop
    async def before_tempban_loop(self):
        await self.wait_until_ready()

# =====================================================================
# WEB SERVER FOR RENDER HEALTH CHECKS
# =====================================================================
async def handle_health_check(request):
    return web.Response(text="Bot is operational and healthy!", status=200)

async def start_web_server(client: ExtendedBotClient):
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()

# =====================================================================
# BOT EXECUTION ENTRYPOINT
# =====================================================================
async def main():
    bot_client = ExtendedBotClient()
    
    # Start the web server concurrently alongside the bot run loop (vital for Render)
    await start_web_server(bot_client)
    
    if not BOT_TOKEN:
        print("Error: DISCORD_TOKEN is missing from environment variables.")
        return

    async with bot_client:
        await bot_client.start(BOT_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
