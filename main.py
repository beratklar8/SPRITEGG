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
DATABASE_NAME = os.getenv("DATABASE_PATH", "bot_production_v4.db")
WEB_PORT = int(os.getenv("PORT", 8080))
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

# In-memory stores for active features
sticky_messages = {}
afk_users = {}
user_eco_cache = {}
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
    """Manages all asynchronous SQLite database interactions and extensive schema bootstrapping."""
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
                CREATE TABLE IF NOT EXISTS ticket_settings (
                    guild_id INTEGER PRIMARY KEY,
                    category_id INTEGER
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS active_tickets (
                    channel_id INTEGER PRIMARY KEY,
                    guild_id INTEGER,
                    target_id INTEGER
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
                    log_channel_id INTEGER,
                    welcome_channel_id INTEGER,
                    welcome_message TEXT
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
                CREATE TABLE IF NOT EXISTS vouch_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    recipient_id INTEGER,
                    author_id INTEGER,
                    comment TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS economy_balances (
                    guild_id INTEGER,
                    user_id INTEGER,
                    wallet INTEGER DEFAULT 0,
                    bank INTEGER DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS leveling_system (
                    guild_id INTEGER,
                    user_id INTEGER,
                    xp INTEGER DEFAULT 0,
                    level INTEGER DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS custom_tags (
                    guild_id INTEGER,
                    tag_name TEXT,
                    tag_content TEXT,
                    PRIMARY KEY (guild_id, tag_name)
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    target_id INTEGER,
                    author_id INTEGER,
                    note_content TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
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
# PERSISTENT VIEWS: TICKET SYSTEM INTERFACE
# =====================================================================
class TicketCreationView(discord.ui.View):
    """Persistent user interface panel for handling community help tickets."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Support Ticket", style=discord.ButtonStyle.primary, custom_id="persistent_ticket_open_btn_v4")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        res = await db_controller.fetchone("SELECT category_id FROM ticket_settings WHERE guild_id = ?", (guild.id,))
        cat_id = res[0] if res else None
        category_obj = guild.get_channel(cat_id) if cat_id else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        ticket_chan = await guild.create_text_channel(
            name=f"ticket-{interaction.user.name}",
            category=category_obj,
            overwrites=overwrites
        )

        await db_controller.execute(
            "INSERT INTO active_tickets (channel_id, guild_id, target_id) VALUES (?, ?, ?)",
            (ticket_chan.id, guild.id, interaction.user.id)
        )

        close_view = discord.ui.View(timeout=None)
        close_btn = discord.ui.Button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="persistent_ticket_close_btn_v4")
        close_view.add_item(close_btn)

        await ticket_chan.send(
            embed=make_embed("Support System", f"Welcome {interaction.user.mention}!\nPlease describe your inquiry in full detail below. Support staff will be with you shortly."),
            view=close_view
        )
        await interaction.response.send_message(content=f"Your ticket has been opened successfully: {ticket_chan.mention}", ephemeral=True)

# =====================================================================
# CORE BOT CLIENT IMPLEMENTATION (EXTENSIVE EVENT ARCHITECTURE)
# =====================================================================
class ExtendedBotClient(commands.Bot):
    """Main application client encapsulating advanced event handlers, filters, and background loops."""
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.reactions = True
        intents.guilds = True
        intents.voice_states = True
        intents.invites = True
        super().__init__(command_prefix="!", intents=intents)
        
        self.invite_link_regex = re.compile(r"(discord\.gg|discord\.com/invite)/[a-zA-Z0-9]+")
        self.url_regex = re.compile(r"https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+")
        self.server_invite_cache = {}

    async def setup_hook(self):
        """Asynchronous initialization routine for views, background tasks, and command tree synchronization."""
        await db_controller.initialize_database()
        self.add_view(TicketCreationView())
        self.background_giveaway_loop.start()
        self.background_tempban_loop.start()
        await self.tree.sync()

    async def on_ready(self):
        """Fires when the bot establishes connection and completes initialization."""
        print(f"Successfully logged in as {self.user} (ID: {self.user.id})")
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="over server security & moderation | /help"))
        for guild in self.guilds:
            try:
                self.server_invite_cache[guild.id] = await guild.invites()
            except Exception:
                pass

    async def on_message(self, message: discord.Message):
        """Comprehensive message event listener handling AI interactions, XP systems, AFK checks, automod, and sticky posts."""
        if message.author.bot or not message.guild:
            return

        # 1. AFK Status System Handler
        if message.author.id in afk_users:
            del afk_users[message.author.id]
            try:
                await message.channel.send(f"Welcome back {message.author.mention}, I have removed your AFK status.", delete_after=5)
            except Exception:
                pass

        for mention in message.mentions:
            if mention.id in afk_users:
                afk_reason = afk_users[mention.id]
                await message.channel.send(f"⚠ **{mention.name}** is currently AFK: {afk_reason}", delete_after=10)

        # 2. Leveling & XP Accumulation Engine
        user_key = (message.guild.id, message.author.id)
        res_xp = await db_controller.fetchone("SELECT xp, level FROM leveling_system WHERE guild_id = ? AND user_id = ?", user_key)
        
        current_xp = res_xp[0] if res_xp else 0
        current_lvl = res_xp[1] if res_xp else 0
        
        gained_xp = random.randint(15, 25)
        new_xp = current_xp + gained_xp
        required_xp = (current_lvl + 1) * 250

        if new_xp >= required_xp:
            current_lvl += 1
            new_xp = 0
            await db_controller.execute(
                "INSERT OR REPLACE INTO leveling_system (guild_id, user_id, xp, level) VALUES (?, ?, ?, ?)",
                (message.guild.id, message.author.id, new_xp, current_lvl)
            )
            try:
                await message.channel.send(embed=success_embed("Level Up!", f"Congratulations {message.author.mention}, you advanced to **Level {current_lvl}**!"), delete_after=8)
            except Exception:
                pass
        else:
            if res_xp:
                await db_controller.execute("UPDATE leveling_system SET xp = ? WHERE guild_id = ? AND user_id = ?", (new_xp, message.guild.id, message.author.id))
            else:
                await db_controller.execute("INSERT INTO leveling_system (guild_id, user_id, xp, level) VALUES (?, ?, ?, 0)", (message.guild.id, message.author.id, new_xp))

        # 3. Gemini AI Integration Handler
        if self.user.mentioned_in(message) and not message.mention_everyone:
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
                        await message.reply(ai_response.text[:1900])
                    except Exception as err:
                        traceback.print_exc()
                        err_msg = str(err)
                        if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                            await message.reply("API rate limit reached. Please try your request again shortly.")
                        else:
                            await message.reply(f"AI error encountered: `{type(err).__name__}`")

        # 4. Advanced Automod Filter Suite
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

        # 5. Sticky Messages Handler
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

        res = await db_controller.fetchone("SELECT welcome_channel_id, welcome_message FROM server_settings WHERE guild_id = ?", (guild.id,))
        if res and res[0] and res[1]:
            w_chan = guild.get_channel(res[0])
            if w_chan:
                try:
                    formatted_msg = res[1].replace("{user}", member.mention).replace("{server}", guild.name)
                    await w_chan.send(formatted_msg)
                except Exception:
                    pass

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
        await dispatch_audit_log(message.guild, warning_embed("Message Deleted", f"**Author:** {message.author.mention}\n**Channel:** {message.channel.mention}\n**Content:**\n```{content_txt[:900]}```"))

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.user.id:
            return
        res = await db_controller.fetchone("SELECT role_id FROM reaction_role_bindings WHERE message_id = ? AND emoji_icon = ?", (payload.message_id, str(payload.emoji)))
        if res:
            guild = self.get_guild(payload.guild_id)
            if guild:
                role = guild.get_role(res[0])
                member = guild.get_member(payload.user_id)
                if role and member:
                    await member.add_roles(role)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        res = await db_controller.fetchone("SELECT role_id FROM reaction_role_bindings WHERE message_id = ? AND emoji_icon = ?", (payload.message_id, str(payload.emoji)))
        if res:
            guild = self.get_guild(payload.guild_id)
            if guild:
                role = guild.get_role(res[0])
                member = guild.get_member(payload.user_id)
                if role and member:
                    await member.remove_roles(role)

    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.component:
            custom_id = interaction.data.get("custom_id")
            if custom_id == "persistent_ticket_close_btn_v4":
                record = await db_controller.fetchone("SELECT target_id FROM active_tickets WHERE channel_id = ?", (interaction.channel_id,))
                if record:
                    await db_controller.execute("DELETE FROM active_tickets WHERE channel_id = ?", (interaction.channel_id,))
                    await interaction.response.send_message("Closing active ticket channel securely...")
                    await dispatch_audit_log(interaction.guild, make_embed("Ticket Closed", f"Channel {interaction.channel.name} was successfully closed."))
                    await interaction.channel.delete()

    @tasks.loop(seconds=15)
    async def background_giveaway_loop(self):
        """Background task loop checking and resolving active giveaways."""
        current_ts = time.time()
        pending = await db_controller.fetchall("SELECT message_id, channel_id, prize_name FROM giveaway_system WHERE ends_at <= ? AND is_ended = 0", (current_ts,))
        for row in pending:
            m_id, c_id, prize = row
            await db_controller.execute("UPDATE giveaway_system SET is_ended = 1 WHERE message_id = ?", (m_id,))
            target_chan = self.get_channel(c_id)
            if not target_chan:
                continue
            try:
                msg = await target_chan.fetch_message(m_id)
                rxn = discord.utils.get(msg.reactions, emoji="🎉")
                users_list = [u async for u in rxn.users() if not u.bot] if rxn else []
                if not users_list:
                    await target_chan.send(f"Giveaway for **{prize}** concluded with no valid participants.")
                else:
                    winner = random.choice(users_list)
                    await target_chan.send(f"🎉 Winner selected: {winner.mention}! Congratulations, you won **{prize}**!")
            except Exception:
                pass

    @tasks.loop(seconds=40)
    async def background_tempban_loop(self):
        """Background task loop managing expiration of temporary user bans."""
        current_ts = time.time()
        expired = await db_controller.fetchall("SELECT guild_id, target_id FROM temporary_bans WHERE expiry_timestamp <= ?", (current_ts,))
        for g_id, u_id in expired:
            await db_controller.execute("DELETE FROM temporary_bans WHERE guild_id = ? AND target_id = ?", (g_id, u_id))
            guild = self.get_guild(g_id)
            if guild:
                try:
                    user_obj = await self.fetch_user(u_id)
                    await guild.unban(user_obj, reason="Temporary ban period expired automatically")
                    await dispatch_audit_log(guild, success_embed("Temporary Ban Lifted", f"User {user_obj} (`{user_obj.id}`) was unbanned automatically."))
                except Exception:
                    pass

bot_client = ExtendedBotClient()

# =====================================================================
# CONFIGURATION COMMANDS
# =====================================================================
@bot_client.tree.command(name="set_log_channel", description="Configure server audit logging channel")
@app_commands.checks.has_permissions(administrator=True)
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    await db_controller.execute(
        "INSERT INTO server_settings (guild_id, log_channel_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET log_channel_id = ?",
        (interaction.guild_id, channel.id, channel.id)
    )
    await interaction.response.send_message(embed=success_embed("Configuration Updated", f"Audit logs will now be directed to {channel.mention}."), ephemeral=True)

@bot_client.tree.command(name="set_welcome", description="Configure automated welcome messages")
@app_commands.checks.has_permissions(administrator=True)
async def set_welcome(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    await db_controller.execute(
        "INSERT INTO server_settings (guild_id, welcome_channel_id, welcome_message) VALUES (?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET welcome_channel_id = ?, welcome_message = ?",
        (interaction.guild_id, channel.id, message, channel.id, message)
    )
    await interaction.response.send_message(embed=success_embed("Welcome Configured", f"Welcome messages enabled in {channel.mention}."), ephemeral=True)

@bot_client.tree.command(name="set_automod", description="Toggle server automated moderation filtering")
@app_commands.checks.has_permissions(administrator=True)
async def set_automod(interaction: discord.Interaction, status: bool):
    await db_controller.execute(
        "INSERT INTO server_settings (guild_id, automod_status) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET automod_status = ?",
        (interaction.guild_id, 1 if status else 0, 1 if status else 0)
    )
    await interaction.response.send_message(embed=success_embed("Automod Status", f"Automated moderation successfully set to **{status}**."), ephemeral=True)

# =====================================================================
# MODERATION SUITE
# =====================================================================
@bot_client.tree.command(name="kick_member", description="Kick a member from the server")
@app_commands.checks.has_permissions(kick_members=True)
async def kick_member(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason specified"):
    try:
        await user.send(embed=error_embed("Notice", f"You were kicked from **{interaction.guild.name}**.\nReason: {reason}"))
    except Exception:
        pass
    await user.kick(reason=reason)
    await dispatch_audit_log(interaction.guild, error_embed("Mod Action: Kick", f"**Target:** {user.mention}\n**Staff:** {interaction.user.mention}\n**Reason:** {reason}"))
    await interaction.response.send_message(embed=success_embed("Success", f"{user.mention} has been kicked successfully."))

@bot_client.tree.command(name="ban_member", description="Ban a member from the server")
@app_commands.checks.has_permissions(ban_members=True)
async def ban_member(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason specified"):
    try:
        await user.send(embed=error_embed("Notice", f"You were banned from **{interaction.guild.name}**.\nReason: {reason}"))
    except Exception:
        pass
    await user.ban(reason=reason)
    await dispatch_audit_log(interaction.guild, error_embed("Mod Action: Ban", f"**Target:** {user.mention}\n**Staff:** {interaction.user.mention}\n**Reason:** {reason}"))
    await interaction.response.send_message(embed=success_embed("Success", f"{user.mention} has been banned successfully."))

@bot_client.tree.command(name="temp_ban", description="Temporarily ban a user for specified minutes")
@app_commands.checks.has_permissions(ban_members=True)
async def temp_ban(interaction: discord.Interaction, user: discord.Member, duration_minutes: int, reason: str = "No reason specified"):
    try:
        await user.send(embed=error_embed("Notice", f"Temporary ban from **{interaction.guild.name}** for {duration_minutes}m.\nReason: {reason}"))
    except Exception:
        pass
    expiry_ts = time.time() + (duration_minutes * 60)
    await user.ban(reason=f"Temp ban {duration_minutes}m: {reason}")
    await db_controller.execute(
        "INSERT OR REPLACE INTO temporary_bans (guild_id, target_id, expiry_timestamp) VALUES (?, ?, ?)",
        (interaction.guild_id, user.id, expiry_ts)
    )
    await dispatch_audit_log(interaction.guild, error_embed("Mod Action: Temp Ban", f"**Target:** {user.mention}\n**Duration:** {duration_minutes}m\n**Staff:** {interaction.user.mention}"))
    await interaction.response.send_message(embed=success_embed("Success", f"{user.mention} has been temporarily banned for {duration_minutes} minutes."))

@bot_client.tree.command(name="unban_user", description="Unban a user using their unique ID")
@app_commands.checks.has_permissions(ban_members=True)
async def unban_user(interaction: discord.Interaction, user_id: str, reason: str = "No reason specified"):
    try:
        target_usr = await bot_client.fetch_user(int(user_id))
        await interaction.guild.unban(target_usr, reason=reason)
        await dispatch_audit_log(interaction.guild, success_embed("Mod Action: Unban", f"**Target:** {target_usr}\n**Staff:** {interaction.user.mention}"))
        await interaction.response.send_message(embed=success_embed("Success", f"Successfully unbanned user ID {user_id}."))
    except Exception as err:
        await interaction.response.send_message(embed=error_embed("Error", f"Could not execute unban: {err}"), ephemeral=True)

@bot_client.tree.command(name="timeout_member", description="Timeout a user for a set time duration")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout_member(interaction: discord.Interaction, user: discord.Member, minutes: int, reason: str = "No reason specified"):
    try:
        await user.send(embed=error_embed("Notice", f"You have been timed out in **{interaction.guild.name}** for {minutes}m."))
    except Exception:
        pass
    span = datetime.timedelta(minutes=minutes)
    await user.timeout(span, reason=reason)
    await dispatch_audit_log(interaction.guild, warning_embed("Mod Action: Timeout", f"**Target:** {user.mention}\n**Duration:** {minutes}m\n**Staff:** {interaction.user.mention}"))
    await interaction.response.send_message(embed=success_embed("Success", f"{user.mention} timed out for {minutes} minutes."))

@bot_client.tree.command(name="warn_member", description="Record a formal warning for a member")
@app_commands.checks.has_permissions(manage_messages=True)
async def warn_member(interaction: discord.Interaction, user: discord.Member, reason: str):
    try:
        await user.send(embed=warning_embed("Warning Logged", f"You received a warning in **{interaction.guild.name}**.\nReason: {reason}"))
    except Exception:
        pass
    await db_controller.execute(
        "INSERT INTO warning_records (guild_id, target_id, moderator_id, reason) VALUES (?, ?, ?, ?)",
        (interaction.guild_id, user.id, interaction.user.id, reason)
    )
    await dispatch_audit_log(interaction.guild, warning_embed("Mod Action: Warning", f"**Target:** {user.mention}\n**Staff:** {interaction.user.mention}\n**Reason:** {reason}"))
    await interaction.response.send_message(embed=warning_embed("Warning Issued", f"{user.mention} has been warned successfully."))

@bot_client.tree.command(name="check_warnings", description="View logged warnings for a member")
async def check_warnings(interaction: discord.Interaction, user: discord.Member):
    records = await db_controller.fetchall("SELECT id, reason FROM warning_records WHERE guild_id = ? AND target_id = ?", (interaction.guild_id, user.id))
    if not records:
        return await interaction.response.send_message(embed=success_embed("Warnings", f"{user.mention} has a clean record."), ephemeral=True)
    body_text = "\n".join([f"`#{item[0]}` - {item[1]}" for item in records])
    await interaction.response.send_message(embed=make_embed(f"Warnings for {user.name}", body_text))

@bot_client.tree.command(name="add_note", description="Add an internal staff note regarding a member")
@app_commands.checks.has_permissions(manage_messages=True)
async def add_note(interaction: discord.Interaction, user: discord.Member, note: str):
    await db_controller.execute(
        "INSERT INTO user_notes (guild_id, target_id, author_id, note_content) VALUES (?, ?, ?, ?)",
        (interaction.guild_id, user.id, interaction.user.id, note)
    )
    await interaction.response.send_message(embed=success_embed("Note Saved", f"Internal note recorded for {user.mention}."), ephemeral=True)

@bot_client.tree.command(name="view_notes", description="View internal staff notes for a member")
@app_commands.checks.has_permissions(manage_messages=True)
async def view_notes(interaction: discord.Interaction, user: discord.Member):
    notes = await db_controller.fetchall("SELECT id, author_id, note_content, timestamp FROM user_notes WHERE guild_id = ? AND target_id = ?", (interaction.guild_id, user.id))
    if not notes:
        return await interaction.response.send_message(embed=info_embed("Staff Notes", f"No notes found for {user.mention}."), ephemeral=True)
    
    packet = make_embed(f"Staff Notes — {user.name}", f"Internal moderation logs.")
    for n_id, a_id, content, ts in notes:
        packet.add_field(name=f"Note #{n_id} (Author: <@{a_id}>)", value=f"{content}\n*Timestamp: {ts}*", inline=False)
    await interaction.response.send_message(embed=packet, ephemeral=True)

# =====================================================================
# ECONOMY SUITE
# =====================================================================
economy_hub = app_commands.Group(name="economy", description="Community financial ecosystem commands")

@economy_hub.command(name="balance", description="Check your current wallet and bank balance")
async def eco_balance(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target = user or interaction.user
    res = await db_controller.fetchone("SELECT wallet, bank FROM economy_balances WHERE guild_id = ? AND user_id = ?", (interaction.guild_id, target.id))
    wallet = res[0] if res else 0
    bank = res[1] if res else 0

    packet = make_embed(f"Financial Balance — {target.name}", f"Overview of financial assets.")
    packet.add_field(name="Wallet", value=f"🪙 {wallet:,} credits", inline=True)
    packet.add_field(name="Bank", value=f"🏦 {bank:,} credits", inline=True)
    await interaction.response.send_message(embed=packet)

@economy_hub.command(name="deposit", description="Deposit cash from wallet into bank account")
async def eco_deposit(interaction: discord.Interaction, amount: int):
    if amount <= 0:
        return await interaction.response.send_message(embed=error_embed("Error", "Amount must be greater than zero."), ephemeral=True)
    
    res = await db_controller.fetchone("SELECT wallet, bank FROM economy_balances WHERE guild_id = ? AND user_id = ?", (interaction.guild_id, interaction.user.id))
    wallet = res[0] if res else 0
    bank = res[1] if res else 0

    if wallet < amount:
        return await interaction.response.send_message(embed=error_embed("Error", "You do not have enough funds in your wallet."), ephemeral=True)

    await db_controller.execute(
        "INSERT OR REPLACE INTO economy_balances (guild_id, user_id, wallet, bank) VALUES (?, ?, ?, ?)",
        (interaction.guild_id, interaction.user.id, wallet - amount, bank + amount)
    )
    await interaction.response.send_message(embed=success_embed("Deposit Successful", f"Deposited **{amount:,}** credits into your bank account."))

@economy_hub.command(name="withdraw", description="Withdraw currency from bank account to wallet")
async def eco_withdraw(interaction: discord.Interaction, amount: int):
    if amount <= 0:
        return await interaction.response.send_message(embed=error_embed("Error", "Amount must be greater than zero."), ephemeral=True)
    
    res = await db_controller.fetchone("SELECT wallet, bank FROM economy_balances WHERE guild_id = ? AND user_id = ?", (interaction.guild_id, interaction.user.id))
    wallet = res[0] if res else 0
    bank = res[1] if res else 0

    if bank < amount:
        return await interaction.response.send_message(embed=error_embed("Error", "You do not have enough funds in your bank account."), ephemeral=True)

    await db_controller.execute(
        "INSERT OR REPLACE INTO economy_balances (guild_id, user_id, wallet, bank) VALUES (?, ?, ?, ?)",
        (interaction.guild_id, interaction.user.id, wallet + amount, bank - amount)
    )
    await interaction.response.send_message(embed=success_embed("Withdrawal Successful", f"Withdrew **{amount:,}** credits into your wallet."))

@economy_hub.command(name="work", description="Perform professional labor to earn currency")
async def eco_work(interaction: discord.Interaction):
    earnings = random.randint(100, 350)
    res = await db_controller.fetchone("SELECT wallet, bank FROM economy_balances WHERE guild_id = ? AND user_id = ?", (interaction.guild_id, interaction.user.id))
    wallet = res[0] if res else 0
    bank = res[1] if res else 0

    await db_controller.execute(
        "INSERT OR REPLACE INTO economy_balances (guild_id, user_id, wallet, bank) VALUES (?, ?, ?, ?)",
        (interaction.guild_id, interaction.user.id, wallet + earnings, bank)
    )
    jobs = ["software developer", "community moderator", "graphic designer", "server administrator", "content creator"]
    job_title = random.choice(jobs)
    await interaction.response.send_message(embed=success_embed("Shift Completed", f"You worked as a **{job_title}** and earned **{earnings:,}** credits!"))

bot_client.tree.add_command(economy_hub)

# =====================================================================
# REPUTATION / VOUCH SYSTEM
# =====================================================================
vouch_hub = app_commands.Group(name="vouch", description="Community reputation and voucher framework")

@vouch_hub.command(name="give", description="Leave a reputation vouch for another user")
@app_commands.describe(user="The user you are vouching for", comment="Details regarding the interaction")
async def vouch_give(interaction: discord.Interaction, user: discord.Member, comment: Optional[str] = "No additional details"):
    if user.id == interaction.user.id:
        return await interaction.response.send_message(embed=error_embed("Error", "You cannot submit a vouch for yourself."), ephemeral=True)
    
    await db_controller.execute(
        "INSERT INTO vouch_records (guild_id, recipient_id, author_id, comment) VALUES (?, ?, ?, ?)",
        (interaction.guild_id, user.id, interaction.user.id, comment)
    )
    
    res = await db_controller.fetchone("SELECT COUNT(*) FROM vouch_records WHERE guild_id = ? AND recipient_id = ?", (interaction.guild_id, user.id))
    total_score = res[0] if res else 1

    packet = success_embed("Vouch Recorded", f"⭐ {interaction.user.mention} submitted a vouch for {user.mention}! Total vouches: **{total_score}**")
    if comment != "No additional details":
        packet.add_field(name="Comment", value=comment, inline=False)

    await interaction.response.send_message(embed=packet)
    await dispatch_audit_log(interaction.guild, make_embed("Reputation Log", f"**Author:** {interaction.user.mention}\n**Recipient:** {user.mention}\n**Total Vouches:** `{total_score}`\n**Note:** {comment}"))

@vouch_hub.command(name="leaderboard", description="Display community reputation leaderboard")
async def vouch_leaderboard(interaction: discord.Interaction):
    rows = await db_controller.fetchall("SELECT recipient_id, COUNT(*) as tally FROM vouch_records WHERE guild_id = ? GROUP BY recipient_id ORDER BY tally DESC LIMIT 10", (interaction.guild_id,))
    if not rows:
        return await interaction.response.send_message(embed=error_embed("Leaderboard", "No reputation points logged in this server yet."), ephemeral=True)
    
    lines = []
    medals = ["👑", "🥈", "🥉"]
    for idx, (usr_id, count) in enumerate(rows):
        rank_tag = medals[idx] if idx < 3 else f"`{idx + 1}.`"
        lines.append(f"{rank_tag} <@{usr_id}> — **{count}** vouches")

    await interaction.response.send_message(embed=make_embed("🏆 Vouch Leaderboard", "\n".join(lines)))

bot_client.tree.add_command(vouch_hub)

# =====================================================================
# UTILITY & FUN COMMANDS
# =====================================================================
@bot_client.tree.command(name="afk", description="Set your status to AFK with an optional reason")
async def afk_command(interaction: discord.Interaction, reason: str = "Away from keyboard"):
    afk_users[interaction.user.id] = reason
    await interaction.response.send_message(embed=success_embed("AFK Status Enabled", f"{interaction.user.mention}, you are now marked as AFK: `{reason}`"))

@bot_client.tree.command(name="poll", description="Create a simple interactive community poll")
async def poll_command(interaction: discord.Interaction, question: str):
    packet = make_embed("📊 Community Poll", f"**Question:** {question}\n\nReact with 👍 or 👎 below to cast your vote!")
    await interaction.response.send_message(embed=packet)
    msg = await interaction.original_response()
    await msg.add_reaction("👍")
    await msg.add_reaction("👎")

@bot_client.tree.command(name="purge_messages", description="Purge chat messages from channel")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge_messages(interaction: discord.Interaction, count: int):
    purged = await interaction.channel.purge(limit=count)
    await dispatch_audit_log(interaction.guild, make_embed("Mod Action: Purge", f"**Channel:** {interaction.channel.mention}\n**Count:** {len(purged)}\n**Staff:** {interaction.user.mention}", COLOR_WARNING))
    await interaction.response.send_message(embed=success_embed("Purge Complete", f"Successfully cleared {len(purged)} messages."), ephemeral=True)

@bot_client.tree.command(name="lock_channel", description="Lock text channel sending permissions")
@app_commands.checks.has_permissions(manage_channels=True)
async def lock_channel(interaction: discord.Interaction):
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
    await dispatch_audit_log(interaction.guild, warning_embed("Mod Action: Lock", f"**Channel:** {interaction.channel.mention}\n**Staff:** {interaction.user.mention}"))
    await interaction.response.send_message(embed=success_embed("Locked", "Channel message sending permissions locked."))

@bot_client.tree.command(name="unlock_channel", description="Restore text channel sending permissions")
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock_channel(interaction: discord.Interaction):
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=True)
    await dispatch_audit_log(interaction.guild, success_embed("Mod Action: Unlock", f"**Channel:** {interaction.channel.mention}\n**Staff:** {interaction.user.mention}"))
    await interaction.response.send_message(embed=success_embed("Unlocked", "Channel message sending permissions restored."))

# =====================================================================
# INFO SUITE
# =====================================================================
@bot_client.tree.command(name="user_profile", description="View user profile details and statistics")
async def user_profile(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    packet = make_embed(f"User Profile — {target.name}", f"Detailed identifier overview.")
    if target.display_avatar:
        packet.set_thumbnail(url=target.display_avatar.url)
    packet.add_field(name="Account Created", value=f"<t:{int(target.created_at.timestamp())}:D>", inline=True)
    packet.add_field(name="Joined Server", value=f"<t:{int(target.joined_at.timestamp())}:D>", inline=True)
    packet.add_field(name="Total Roles", value=f"{len(target.roles) - 1}", inline=True)
    await interaction.response.send_message(embed=packet)

@bot_client.tree.command(name="server_overview", description="View community server statistics")
async def server_overview(interaction: discord.Interaction):
    server = interaction.guild
    packet = make_embed(f"Community Overview — {server.name}", f"Infrastructure status report.")
    if server.icon:
        packet.set_thumbnail(url=server.icon.url)
    packet.add_field(name="Server Owner", value=f"<@{server.owner_id}>", inline=True)
    packet.add_field(name="Member Count", value=f"{server.member_count}", inline=True)
    packet.add_field(name="Channel Count", value=f"{len(server.channels)}", inline=True)
    await interaction.response.send_message(embed=packet)

# =====================================================================
# WEB SERVICE (RENDER KEEP-ALIVE)
# =====================================================================
async def handle_ping(request):
    return web.Response(text="Bot web service is live and fully responsive.")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
    await site.start()

# =====================================================================
# MAIN ROUTINE ENTRY POINT
# =====================================================================
async def main():
    if not BOT_TOKEN:
        raise ValueError("DISCORD_TOKEN environment variable is required!")

    await start_web_server()
    await bot_client.start(BOT_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
