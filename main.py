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
from groq import Groq

load_dotenv()

BOT_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_NAME = os.getenv("DATABASE_PATH", "bot.db")
WEB_PORT = int(os.getenv("PORT", 10000))
GROQ_API_SECRET = os.getenv("GROQ_API_KEY")

# --- CONFIGURATION FOR STATUS CHANNELS ---
CHANNEL_ONLINE_ID = 1533920905258995905   # 🟢 SPRITEGG ONLINE
CHANNEL_UPDATING_ID = 1533921928224702685 # 🔵 SPRITEGG UPDATING
CHANNEL_OFFLINE_ID = 1533922000005894224  # 🔴 SPRITEGG OFFLINE

COLOR_NEUTRAL = 0x2B2D31
COLOR_SUCCESS = 0x2ECC71
COLOR_WARNING = 0xF1C40F
COLOR_DANGER = 0xE74C3C
COLOR_INFO = 0x3498DB
COLOR_PURPLE = 0x9B59B6

groq_api_client = None
if GROQ_API_SECRET:
    try:
        groq_api_client = Groq(api_key=GROQ_API_SECRET)
    except Exception as initialization_exception:
        print(f"Failed to initialize Groq client: {initialization_exception}")

sticky_messages = {}
user_cooldowns = {}

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

class DatabaseController:
    def __init__(self, db_path: str = DATABASE_NAME):
        self.db_path = db_path

    async def initialize_database(self):
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
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_vouches (
                    guild_id INTEGER,
                    target_id INTEGER,
                    giver_id INTEGER,
                    reason TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, target_id, giver_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sprites_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    description TEXT
                )
            """)
            await conn.commit()

    async def execute(self, query: str, params: tuple = ()):
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(query, params)
            await conn.commit()
            return cursor

    async def fetchone(self, query: str, params: tuple = ()):
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute(query, params) as cursor:
                return await cursor.fetchone()

    async def fetchall(self, query: str, params: tuple = ()):
        async with aiosqlite.connect(self.db_path) as conn:
            async with conn.execute(query, params) as cursor:
                return await cursor.fetchall()

db_controller = DatabaseController()

async def dispatch_audit_log(guild: discord.Guild, embed: discord.Embed):
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

class ExtendedBotClient(commands.Bot):
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

    async def set_status(self, status: str):
        channel_mapping = {
            'online': (CHANNEL_ONLINE_ID, CHANNEL_UPDATING_ID, CHANNEL_OFFLINE_ID),
            'updating': (CHANNEL_UPDATING_ID, CHANNEL_ONLINE_ID, CHANNEL_OFFLINE_ID),
            'offline': (CHANNEL_OFFLINE_ID, CHANNEL_ONLINE_ID, CHANNEL_UPDATING_ID)
        }

        if status not in channel_mapping:
            return

        show_id, hide_id_1, hide_id_2 = channel_mapping[status]
        target_ids = {show_id: True, hide_id_1: False, hide_id_2: False}

        for chan_id, should_be_visible in target_ids.items():
            if not chan_id:
                continue
            channel = self.get_channel(chan_id)
            if channel and isinstance(channel, discord.VoiceChannel):
                try:
                    current_overwrite = channel.overwrites_for(channel.guild.default_role)
                    if current_overwrite.view_channel != should_be_visible:
                        current_overwrite.view_channel = should_be_visible
                        await channel.set_permissions(channel.guild.default_role, overwrite=current_overwrite)
                except Exception as e:
                    print(f"Failed to change visibility of channel {chan_id}: {e}")

    @tasks.loop(minutes=1)
    async def background_giveaway_loop(self):
        current_time = time.time()
        rows = await db_controller.fetchall("SELECT message_id, channel_id, guild_id, prize_name FROM giveaway_system WHERE ends_at <= ? AND is_ended = 0", (current_time,))
        for row in rows:
            msg_id, chan_id, guild_id, prize = row
            await db_controller.execute("UPDATE giveaway_system SET is_ended = 1 WHERE message_id = ?", (msg_id,))
            guild = self.get_guild(guild_id)
            if not guild:
                continue
            channel = guild.get_channel(chan_id)
            if not channel:
                continue
            try:
                msg = await channel.fetch_message(msg_id)
                users = []
                for reaction in msg.reactions:
                    if str(reaction.emoji) == "🎉":
                        async for user in reaction.users():
                            if not user.bot:
                                users.append(user)
                        break
                if users:
                    winner = random.choice(users)
                    await channel.send(embed=success_embed("Giveaway Ended!", f"Congratulations {winner.mention}! You won the **{prize}**!"))
                else:
                    await channel.send(embed=info_embed("Giveaway Ended", f"Giveaway for **{prize}** ended, but no valid entries were found."))
            except Exception:
                pass

    @tasks.loop(minutes=1)
    async def background_tempban_loop(self):
        current_time = time.time()
        rows = await db_controller.fetchall("SELECT guild_id, target_id FROM temporary_bans WHERE expiry_timestamp <= ?", (current_time,))
        for row in rows:
            guild_id, target_id = row
            guild = self.get_guild(guild_id)
            if guild:
                try:
                    await guild.unban(discord.Object(id=target_id), reason="Temporary ban expired.")
                except Exception:
                    pass
            await db_controller.execute("DELETE FROM temporary_bans WHERE guild_id = ? AND target_id = ?", (guild_id, target_id))

    async def setup_hook(self):
        await db_controller.initialize_database()
        
        await self.load_extension("Sprites")
        print("Sprites cog successfully loaded.")
        
        await self.set_status('updating')

        self.background_giveaway_loop.start()
        self.background_tempban_loop.start()

        vouch_group = app_commands.Group(name="vouch", description="Manage and view trade vouches.")

        @vouch_group.command(name="give", description="Vouch for someone you traded with.")
        async def vouch_give(interaction: discord.Interaction, user: discord.Member, reason: Optional[str] = "No reason provided"):
            if user.id == interaction.user.id:
                return await interaction.response.send_message(embed=error_embed("Vouch Error", "You cannot vouch for yourself."), ephemeral=True)
            if user.bot:
                return await interaction.response.send_message(embed=error_embed("Vouch Error", "You cannot vouch for a bot."), ephemeral=True)

            try:
                await db_controller.execute(
                    "INSERT INTO user_vouches (guild_id, target_id, giver_id, reason) VALUES (?, ?, ?, ?)",
                    (interaction.guild.id, user.id, interaction.user.id, reason)
                )
            except aiosqlite.IntegrityError:
                return await interaction.response.send_message(embed=error_embed("Already Vouched", f"You have already vouched for {user.mention} in this server."), ephemeral=True)

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
            await interaction.response.send_message(embed=embed)

        @vouch_group.command(name="leaderboard", description="View the top vouched users in the server.")
        async def vouch_leaderboard(interaction: discord.Interaction):
            rows = await db_controller.fetchall(
                "SELECT target_id, COUNT(*) as cnt FROM user_vouches WHERE guild_id = ? GROUP BY target_id ORDER BY cnt DESC LIMIT 10",
                (interaction.guild.id,)
            )
            if not rows:
                return await interaction.response.send_message(embed=info_embed("Vouch Leaderboard", "No vouches have been recorded in this server yet."), ephemeral=True)

            medals = ["👑", "🥈", "🥉"]
            desc = ""
            for index, (target_id, count) in enumerate(rows):
                prefix = medals[index] if index < 3 else f"`{index + 1}.`"
                desc += f"{prefix} <@{target_id}> — **{count}** vouches\n"

            embed = make_embed("🏆 Vouch Leaderboard", desc, COLOR_PURPLE)
            await interaction.response.send_message(embed=embed)

        self.tree.add_command(vouch_group)

        @self.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
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
        print(f"Successfully logged in as {self.user} (ID: {self.user.id})")
        await self.set_status('online')
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="over server security | /help"))
        for guild in self.guilds:
            try:
                self.server_invite_cache[guild.id] = await guild.invites()
            except Exception:
                pass

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if self.user.mentioned_in(message) and not message.mention_everyone:
            current_time = time.time()
            last_used = user_cooldowns.get(message.author.id, 0)
            if current_time - last_used < 5:
                remaining = int(5 - (current_time - last_used))
                return await message.reply(f"Please wait {remaining} more seconds before using AI again.", delete_after=5)
            
            user_cooldowns[message.author.id] = current_time
            clean_text = message.content.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()
            
            if not clean_text:
                return await message.channel.send(f"Hello {message.author.mention}! How can I assist you with your queries today?")

            async with message.channel.typing():
                if groq_api_client:
                    try:
                        chat_completion = groq_api_client.chat.completions.create(
                            messages=[
                                {
                                    "role": "user",
                                    "content": clean_text,
                                }
                            ],
                            model="llama-3.3-70b-versatile",
                        )
                        reply_text = chat_completion.choices[0].message.content
                        if not reply_text:
                            await message.reply("Groq returned an empty response.")
                        else:
                            await message.reply(reply_text[:1900])
                    except Exception as err:
                        traceback.print_exc()
                        err_msg = str(err)
                        if "429" in err_msg or "rate_limit" in err_msg.lower():
                            await message.reply("API rate limit reached. Please try your request again shortly.")
                        else:
                            await message.reply(f"AI error encountered: `{type(err).__name__}`")
                else:
                    await message.reply("Groq API key is not configured.")

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

async def handle_ping(request):
    return web.Response(text="Bot is running and healthy!")

async def start_web_server(client: ExtendedBotClient):
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()
    print(f"Web server started on port {WEB_PORT}")

async def main():
    client = ExtendedBotClient()
    await asyncio.gather(
        start_web_server(client),
        client.start(BOT_TOKEN)
    )

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Error: DISCORD_TOKEN is missing in the environment variables.")
    else:
        asyncio.run(main())
