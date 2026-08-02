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

# ==========================================
# CONFIGURATION
# ==========================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot_data.db")
PORT = int(os.getenv("PORT", 8080))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

COLOR_PRIMARY = 0x5865F2    # Blurple
COLOR_SUCCESS = 0x57F287    # Green
COLOR_WARNING = 0xFEE75C    # Yellow
COLOR_ERROR = 0xED4245      # Red

# Initialize Gemini AI Client
ai_client = None
if GEMINI_API_KEY:
    try:
        ai_client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"Fout bij initialiseren Gemini Client: {e}")

# In-memory storage for sticky messages (channel_id: {"message": str, "last_msg_id": int})
sticky_messages = {}

# ==========================================
# EMBED HELPERS
# ==========================================
def create_embed(title: str, description: str, color: int = COLOR_PRIMARY) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)

def success_embed(title: str, description: str) -> discord.Embed:
    return create_embed(f"✅ {title}", description, COLOR_SUCCESS)

def error_embed(title: str, description: str) -> discord.Embed:
    return create_embed(f"❌ {title}", description, COLOR_ERROR)

def warning_embed(title: str, description: str) -> discord.Embed:
    return create_embed(f"⚠️ {title}", description, COLOR_WARNING)

# ==========================================
# DATABASE MANAGEMENT
# ==========================================
class Database:
    def __init__(self, db_path: str = DATABASE_PATH):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS warnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    user_id INTEGER,
                    moderator_id INTEGER,
                    reason TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tempbans (
                    guild_id INTEGER,
                    user_id INTEGER,
                    unban_time REAL,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ticket_configs (
                    guild_id INTEGER PRIMARY KEY,
                    category_id INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS active_tickets (
                    channel_id INTEGER PRIMARY KEY,
                    guild_id INTEGER,
                    user_id INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS giveaways (
                    message_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    guild_id INTEGER,
                    prize TEXT,
                    end_time REAL,
                    winners INTEGER,
                    ended BOOLEAN DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    automod_enabled BOOLEAN DEFAULT 0,
                    muted_role_id INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS reaction_roles (
                    message_id INTEGER,
                    emoji TEXT,
                    role_id INTEGER,
                    PRIMARY KEY (message_id, emoji)
                )
            """)
            await db.commit()

    async def execute(self, query: str, parameters: tuple = ()):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(query, parameters)
            await db.commit()
            return cursor

    async def fetchone(self, query: str, parameters: tuple = ()):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(query, parameters) as cursor:
                return await cursor.fetchone()

    async def fetchall(self, query: str, parameters: tuple = ()):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(query, parameters) as cursor:
                return await cursor.fetchall()

db = Database()

# ==========================================
# PERSISTENT VIEWS (TICKETS)
# ==========================================
class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📩 Open Ticket", style=discord.ButtonStyle.primary, custom_id="persistent_open_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        row = await db.fetchone("SELECT category_id FROM ticket_configs WHERE guild_id = ?", (guild.id,))
        category_id = row[0] if row else None
        category = guild.get_channel(category_id) if category_id else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        channel = await guild.create_text_channel(
            name=f"ticket-{interaction.user.name}",
            category=category,
            overwrites=overwrites
        )

        await db.execute("INSERT INTO active_tickets (channel_id, guild_id, user_id) VALUES (?, ?, ?)",
                         (channel.id, guild.id, interaction.user.id))

        close_view = discord.ui.View(timeout=None)
        close_btn = discord.ui.Button(label="🔒 Close Ticket", style=discord.ButtonStyle.danger, custom_id="persistent_close_ticket")
        close_view.add_item(close_btn)

        await channel.send(embed=create_embed("Ticket Created", f"Welcome {interaction.user.mention}! Please describe your issue or question."), view=close_view)
        await interaction.response.send_message(content=f"Ticket created in {channel.mention}!", ephemeral=True)

# ==========================================
# MAIN BOT CLASS
# ==========================================
class ModerationBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.reactions = True
        super().__init__(command_prefix="!", intents=intents)
        self.invite_regex = re.compile(r"(discord\.gg|discord\.com/invite)/[a-zA-Z0-9]+")

    async def setup_hook(self):
        await db.init()
        self.add_view(TicketView())
        self.check_giveaways.start()
        self.check_tempbans.start()
        await self.tree.sync()

    async def on_ready(self):
        print(f'Bot is online! Logged in as: {self.user.name} (ID: {self.user.id})')
        await self.change_presence(
            activity=discord.CustomActivity(name="Chat via mentions | /help")
        )

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        # 1. AI Chat Response when bot is mentioned
        if self.user.mentioned_in(message) and not message.mention_everyone:
            clean_content = message.content.replace(f'<@{self.user.id}>', '').replace(f'<@!{self.user.id}>', '').strip()
            
            if not clean_content:
                return await message.channel.send(f"Hey {message.author.mention}! How can I help you today?")

            async with message.channel.typing():
                if ai_client:
                    try:
                        response = await ai_client.aio.models.generate_content(
                            model='gemini-2.0-flash',
                            contents=clean_content
                        )
                        reply_text = response.text[:1900]
                        await message.reply(reply_text)
                    except Exception as e:
                        import traceback
                        traceback.print_exc()

                        error_str = str(e)
                        if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                            await message.reply("You have reached your message limit, please try again later.")
                        else:
                            await message.reply(
                                f"❌ Gemini Error\n"
                                f"Type: `{type(e).__name__}`\n"
                                f"Message:\n```{e}```"
                            )

        # 2. AutoMod Checks
        row = await db.fetchone("SELECT automod_enabled FROM guild_settings WHERE guild_id = ?", (message.guild.id,))
        if row and row[0] and not message.author.guild_permissions.manage_messages:
            if self.invite_regex.search(message.content):
                await message.delete()
                return await message.channel.send(embed=warning_embed("AutoMod", f"{message.author.mention}, posting invite links is not allowed!"), delete_after=5)

            if len(message.content) > 10 and sum(1 for c in message.content if c.isupper()) / len(message.content) > 0.7:
                await message.delete()
                return await message.channel.send(embed=warning_embed("AutoMod", f"{message.author.mention}, please avoid excessive caps!"), delete_after=5)

        # 3. Sticky Message Handler
        if message.channel.id in sticky_messages:
            sticky_data = sticky_messages[message.channel.id]
            if sticky_data.get("last_msg_id"):
                try:
                    old_msg = await message.channel.fetch_message(sticky_data["last_msg_id"])
                    await old_msg.delete()
                except Exception:
                    pass
            new_msg = await message.channel.send(embed=create_embed("📌 Sticky Message", sticky_data["message"]))
            sticky_messages[message.channel.id]["last_msg_id"] = new_msg.id

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.user.id:
            return
        emoji_str = str(payload.emoji)
        row = await db.fetchone("SELECT role_id FROM reaction_roles WHERE message_id = ? AND emoji = ?", (payload.message_id, emoji_str))
        if row:
            guild = self.get_guild(payload.guild_id)
            if guild:
                role = guild.get_role(row[0])
                member = guild.get_member(payload.user_id)
                if role and member:
                    await member.add_roles(role)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        emoji_str = str(payload.emoji)
        row = await db.fetchone("SELECT role_id FROM reaction_roles WHERE message_id = ? AND emoji = ?", (payload.message_id, emoji_str))
        if row:
            guild = self.get_guild(payload.guild_id)
            if guild:
                role = guild.get_role(row[0])
                member = guild.get_member(payload.user_id)
                if role and member:
                    await member.remove_roles(role)

    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.component:
            custom_id = interaction.data.get("custom_id")
            if custom_id == "persistent_close_ticket":
                row = await db.fetchone("SELECT user_id FROM active_tickets WHERE channel_id = ?", (interaction.channel_id,))
                if row:
                    await db.execute("DELETE FROM active_tickets WHERE channel_id = ?", (interaction.channel_id,))
                    await interaction.response.send_message("Closing ticket...")
                    await interaction.channel.delete()

    @tasks.loop(seconds=10)
    async def check_giveaways(self):
        now = time.time()
        ended_giveaways = await db.fetchall("SELECT message_id, channel_id, prize, winners FROM giveaways WHERE end_time <= ? AND ended = 0", (now,))
        
        for g in ended_giveaways:
            msg_id, chan_id, prize, winners_cnt = g
            await db.execute("UPDATE giveaways SET ended = 1 WHERE message_id = ?", (msg_id,))
            channel = self.get_channel(chan_id)
            if not channel:
                continue
            try:
                msg = await channel.fetch_message(msg_id)
                reaction = discord.utils.get(msg.reactions, emoji="🎉")
                users = [u async for u in reaction.users() if not u.bot] if reaction else []

                if not users:
                    await channel.send(f"🎉 Giveaway for **{prize}** ended, but there were no valid entries!")
                else:
                    win_list = random.sample(users, min(len(users), winners_cnt))
                    win_mentions = ", ".join([u.mention for u in win_list])
                    await channel.send(f"🎉 Congratulations {win_mentions}! You won **{prize}**!")
            except Exception:
                pass

    @tasks.loop(seconds=30)
    async def check_tempbans(self):
        now = time.time()
        expired = await db.fetchall("SELECT guild_id, user_id FROM tempbans WHERE unban_time <= ?", (now,))
        for g_id, u_id in expired:
            await db.execute("DELETE FROM tempbans WHERE guild_id = ? AND user_id = ?", (g_id, u_id))
            guild = self.get_guild(g_id)
            if guild:
                try:
                    user = await self.fetch_user(u_id)
                    await guild.unban(user, reason="Tempban expired")
                except Exception:
                    pass

bot = ModerationBot()

# ==========================================
# MODERATION COMMANDS (MET DM NOTIFICATIES)
# ==========================================
@bot.tree.command(name="kick", description="Kicks a member from the server")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    try:
        await user.send(embed=error_embed("Kicked", f"You have been kicked from **{interaction.guild.name}**.\n**Reason:** {reason}"))
    except Exception:
        pass
    await user.kick(reason=reason)
    await interaction.response.send_message(embed=success_embed("Kicked", f"{user.mention} has been kicked.\n**Reason:** {reason}"))

@bot.tree.command(name="ban", description="Bans a member from the server")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    try:
        await user.send(embed=error_embed("Banned", f"You have been banned from **{interaction.guild.name}**.\n**Reason:** {reason}"))
    except Exception:
        pass
    await user.ban(reason=reason)
    await interaction.response.send_message(embed=success_embed("Banned", f"{user.mention} has been banned.\n**Reason:** {reason}"))

@bot.tree.command(name="tempban", description="Temporarily ban a member for a duration in minutes")
@app_commands.checks.has_permissions(ban_members=True)
async def tempban(interaction: discord.Interaction, user: discord.Member, minutes: int, reason: str = "No reason provided"):
    try:
        await user.send(embed=error_embed("Tempbanned", f"You have been temporarily banned from **{interaction.guild.name}** for **{minutes} minutes**.\n**Reason:** {reason}"))
    except Exception:
        pass
    unban_time = time.time() + (minutes * 60)
    await user.ban(reason=f"Tempban for {minutes}m: {reason}")
    await db.execute("INSERT OR REPLACE INTO tempbans (guild_id, user_id, unban_time) VALUES (?, ?, ?)",
                     (interaction.guild_id, user.id, unban_time))
    await interaction.response.send_message(embed=success_embed("Tempbanned", f"{user.mention} banned for {minutes} minutes.\n**Reason:** {reason}"))

@bot.tree.command(name="unban", description="Unban a user by User ID")
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
    try:
        user = await bot.fetch_user(int(user_id))
        await interaction.guild.unban(user, reason=reason)
        await interaction.response.send_message(embed=success_embed("Unbanned", f"{user.name} has been unbanned."))
    except Exception as e:
        await interaction.response.send_message(embed=error_embed("Error", f"Failed to unban user: {e}"), ephemeral=True)

@bot.tree.command(name="timeout", description="Timeout a member for a specified duration in minutes")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, user: discord.Member, minutes: int, reason: str = "No reason provided"):
    try:
        await user.send(embed=error_embed("Timeout", f"You have been timed out in **{interaction.guild.name}** for **{minutes} minutes**.\n**Reason:** {reason}"))
    except Exception:
        pass
    duration = datetime.timedelta(minutes=minutes)
    await user.timeout(duration, reason=reason)
    await interaction.response.send_message(embed=success_embed("Timeout", f"{user.mention} has been timed out for {minutes} minutes."))

@bot.tree.command(name="untimeout", description="Remove timeout from a member")
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, user: discord.Member):
    await user.timeout(None)
    await interaction.response.send_message(embed=success_embed("Timeout Removed", f"Timeout for {user.mention} has been removed."))

@bot.tree.command(name="mute", description="Mute a member using a Muted role")
@app_commands.checks.has_permissions(manage_roles=True)
async def mute(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    try:
        await user.send(embed=error_embed("Muted", f"You have been muted in **{interaction.guild.name}**.\n**Reason:** {reason}"))
    except Exception:
        pass
    guild = interaction.guild
    row = await db.fetchone("SELECT muted_role_id FROM guild_settings WHERE guild_id = ?", (guild.id,))
    role = guild.get_role(row[0]) if row and row[0] else None

    if not role:
        role = discord.utils.get(guild.roles, name="Muted")
        if not role:
            role = await guild.create_role(name="Muted")
            for channel in guild.channels:
                await channel.set_permissions(role, send_messages=False, speak=False)
            await db.execute("INSERT INTO guild_settings (guild_id, muted_role_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET muted_role_id = ?",
                             (guild.id, role.id, role.id))

    await user.add_roles(role, reason=reason)
    await interaction.response.send_message(embed=success_embed("Muted", f"{user.mention} has been muted.\n**Reason:** {reason}"))

@bot.tree.command(name="unmute", description="Unmute a member")
@app_commands.checks.has_permissions(manage_roles=True)
async def unmute(interaction: discord.Interaction, user: discord.Member):
    guild = interaction.guild
    row = await db.fetchone("SELECT muted_role_id FROM guild_settings WHERE guild_id = ?", (guild.id,))
    role = guild.get_role(row[0]) if row and row[0] else discord.utils.get(guild.roles, name="Muted")

    if role in user.roles:
        await user.remove_roles(role)
        await interaction.response.send_message(embed=success_embed("Unmuted", f"{user.mention} has been unmuted."))
    else:
        await interaction.response.send_message(embed=error_embed("Error", f"{user.mention} is not muted!"), ephemeral=True)

@bot.tree.command(name="warn", description="Issue a warning to a member")
@app_commands.checks.has_permissions(manage_messages=True)
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str):
    try:
        await user.send(embed=warning_embed("Warning", f"You have received a warning in **{interaction.guild.name}**.\n**Reason:** {reason}"))
    except Exception:
        pass
    await db.execute("INSERT INTO warnings (guild_id, user_id, moderator_id, reason) VALUES (?, ?, ?, ?)",
                     (interaction.guild_id, user.id, interaction.user.id, reason))
    await interaction.response.send_message(embed=warning_embed("Warning Issued", f"{user.mention} has been WARNED.\n**Reason:** {reason}"))

@bot.tree.command(name="warnings", description="View warnings history for a member")
async def warnings(interaction: discord.Interaction, user: discord.Member):
    rows = await db.fetchall("SELECT id, reason, timestamp FROM warnings WHERE guild_id = ? AND user_id = ?", (interaction.guild_id, user.id))
    if not rows:
        return await interaction.response.send_message(embed=success_embed("Warnings", f"{user.mention} has no warnings."), ephemeral=True)
    desc = "\n".join([f"`#{r[0]}` | **Reason:** {r[1]}" for r in rows])
    await interaction.response.send_message(embed=create_embed(f"Warnings for {user.name}", desc))

@bot.tree.command(name="purge", description="Purge a specified number of messages")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: int):
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.response.send_message(embed=success_embed("Purge", f"Deleted {len(deleted)} messages."), ephemeral=True)

@bot.tree.command(name="lock", description="Lock the current channel")
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction):
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
    await interaction.response.send_message(embed=success_embed("Channel Locked", "Members can no longer send messages in this channel."))

@bot.tree.command(name="unlock", description="Unlock the current channel")
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction):
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=True)
    await interaction.response.send_message(embed=success_embed("Channel Unlocked", "Members can send messages again."))

@bot.tree.command(name="lockdown", description="Lockdown all channels in the server")
@app_commands.checks.has_permissions(administrator=True)
async def lockdown(interaction: discord.Interaction):
    await interaction.response.defer()
    for channel in interaction.guild.text_channels:
        await channel.set_permissions(interaction.guild.default_role, send_messages=False)
    await interaction.followup.send(embed=success_embed("Server Lockdown", "All text channels have been locked!"))

@bot.tree.command(name="slowmode", description="Set channel slowmode in seconds (0 to disable)")
@app_commands.checks.has_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, seconds: int):
    await interaction.channel.edit(slowmode_delay=seconds)
    await interaction.response.send_message(embed=success_embed("Slowmode", f"Slowmode set to {seconds} seconds."))

@bot.tree.command(name="nickname", description="Change a member's nickname")
@app_commands.checks.has_permissions(manage_nicknames=True)
async def nickname(interaction: discord.Interaction, user: discord.Member, new_nickname: Optional[str] = None):
    await user.edit(nick=new_nickname)
    await interaction.response.send_message(embed=success_embed("Nickname Changed", f"Updated nickname for {user.mention}."))

@bot.tree.command(name="role_add", description="Add a role to a member")
@app_commands.checks.has_permissions(manage_roles=True)
async def role_add(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    await user.add_roles(role)
    await interaction.response.send_message(embed=success_embed("Role Added", f"Added {role.mention} to {user.mention}."))

@bot.tree.command(name="role_remove", description="Remove a role from a member")
@app_commands.checks.has_permissions(manage_roles=True)
async def role_remove(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    await user.remove_roles(role)
    await interaction.response.send_message(embed=success_embed("Role Removed", f"Removed {role.mention} from {user.mention}."))

# ==========================================
# INFO COMMANDS
# ==========================================
@bot.tree.command(name="userinfo", description="Get information about a user")
async def userinfo(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    embed = create_embed(f"User Info - {user.name}", f"ID: `{user.id}`")
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Account Created", value=f"<t:{int(user.created_at.timestamp())}:D>", inline=True)
    embed.add_field(name="Joined Server", value=f"<t:{int(user.joined_at.timestamp())}:D>", inline=True)
    embed.add_field(name="Roles", value=f"{len(user.roles) - 1}", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="serverinfo", description="Get information about this server")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    embed = create_embed(f"Server Info - {guild.name}", f"ID: `{guild.id}`")
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
    embed.add_field(name="Members", value=f"{guild.member_count}", inline=True)
    embed.add_field(name="Channels", value=f"{len(guild.channels)}", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="channelinfo", description="Get information about a channel")
async def channelinfo(interaction: discord.Interaction, channel: discord.TextChannel = None):
    channel = channel or interaction.channel
    embed = create_embed(f"Channel Info - #{channel.name}", f"ID: `{channel.id}`")
    embed.add_field(name="Category", value=channel.category.name if channel.category else "None", inline=True)
    embed.add_field(name="Created At", value=f"<t:{int(channel.created_at.timestamp())}:D>", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="roleinfo", description="Get information about a role")
async def roleinfo(interaction: discord.Interaction, role: discord.Role):
    embed = create_embed(f"Role Info - {role.name}", f"ID: `{role.id}`", color=role.color.value or COLOR_PRIMARY)
    embed.add_field(name="Color", value=str(role.color), inline=True)
    embed.add_field(name="Members Count", value=str(len(role.members)), inline=True)
    embed.add_field(name="Hoisted", value=str(role.hoist), inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="avatar", description="Display a user's avatar")
async def avatar(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    embed = create_embed(f"{user.name}'s Avatar", "")
    embed.set_image(url=user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="banner", description="Display a user's banner")
async def banner(interaction: discord.Interaction, user: discord.User = None):
    user = user or interaction.user
    fetched_user = await bot.fetch_user(user.id)
    if fetched_user.banner:
        embed = create_embed(f"{fetched_user.name}'s Banner", "")
        embed.set_image(url=fetched_user.banner.url)
        await interaction.response.send_message(embed=embed)
    else:
        await interaction.response.send_message(embed=error_embed("No Banner", f"{fetched_user.name} does not have a banner set."), ephemeral=True)

@bot.tree.command(name="botinfo", description="Display information about the bot")
async def botinfo(interaction: discord.Interaction):
    embed = create_embed("Bot Information", f"Logged in as **{bot.user.name}**\nServing **{len(bot.guilds)}** server(s).")
    embed.add_field(name="Library", value="discord.py 2.x", inline=True)
    embed.add_field(name="Developer", value="SPRITEGG", inline=True)
    await interaction.response.send_message(embed=embed)

# ==========================================
# GIVEAWAYS, TICKETS & EXTRA UTILITIES
# ==========================================
@bot.tree.command(name="giveaway", description="Start a giveaway")
@app_commands.checks.has_permissions(manage_events=True)
async def start_giveaway(interaction: discord.Interaction, duration_minutes: int, winners: int, prize: str):
    end_time = time.time() + (duration_minutes * 60)
    embed = create_embed(f"🎉 GIVEAWAY: {prize}", f"React with 🎉 to enter!\n**Winners:** {winners}\n**Ends:** <t:{int(end_time)}:R>")
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    await msg.add_reaction("🎉")
    await db.execute("INSERT INTO giveaways (message_id, channel_id, guild_id, prize, end_time, winners) VALUES (?, ?, ?, ?, ?, ?)",
                     (msg.id, interaction.channel_id, interaction.guild_id, prize, end_time, winners))

@bot.tree.command(name="giveaway_reroll", description="Reroll a giveaway winner")
@app_commands.checks.has_permissions(manage_events=True)
async def giveaway_reroll(interaction: discord.Interaction, message_id: str):
    try:
        msg = await interaction.channel.fetch_message(int(message_id))
        reaction = discord.utils.get(msg.reactions, emoji="🎉")
        users = [u async for u in reaction.users() if not u.bot] if reaction else []
        if users:
            winner = random.choice(users)
            await interaction.response.send_message(f"🎉 New Winner: {winner.mention}!")
        else:
            await interaction.response.send_message(embed=error_embed("Reroll Failed", "No valid entries found."), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(embed=error_embed("Error", f"Could not fetch message: {e}"), ephemeral=True)

@bot.tree.command(name="giveaway_edit", description="Edit an ongoing giveaway duration")
@app_commands.checks.has_permissions(manage_events=True)
async def giveaway_edit(interaction: discord.Interaction, message_id: str, new_duration_minutes: int):
    new_end = time.time() + (new_duration_minutes * 60)
    await db.execute("UPDATE giveaways SET end_time = ? WHERE message_id = ?", (new_end, int(message_id)))
    await interaction.response.send_message(embed=success_embed("Giveaway Updated", f"New end time set for giveaway `{message_id}`."))

@bot.tree.command(name="setup_tickets", description="Create the ticket panel in this channel")
@app_commands.checks.has_permissions(administrator=True)
async def setup_tickets(interaction: discord.Interaction, category: discord.CategoryChannel = None):
    if category:
        await db.execute("INSERT INTO ticket_configs (guild_id, category_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET category_id = ?",
                         (interaction.guild_id, category.id, category.id))
    await interaction.channel.send(embed=create_embed("Support Tickets", "Click the button below to open a ticket."), view=TicketView())
    await interaction.response.send_message(embed=success_embed("Tickets", "Ticket panel successfully created."), ephemeral=True)

@bot.tree.command(name="add_reaction_role", description="Add a reaction role to a message")
@app_commands.checks.has_permissions(manage_roles=True)
async def add_reaction_role(interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
    try:
        msg = await interaction.channel.fetch_message(int(message_id))
        await msg.add_reaction(emoji)
        await db.execute("INSERT OR REPLACE INTO reaction_roles (message_id, emoji, role_id) VALUES (?, ?, ?)",
                         (msg.id, emoji, role.id))
        await interaction.response.send_message(embed=success_embed("Reaction Role", f"Bound {emoji} to {role.mention} on message `{msg.id}`."), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(embed=error_embed("Error", f"Failed: {e}"), ephemeral=True)

@bot.tree.command(name="sticky", description="Set a sticky message for this channel")
@app_commands.checks.has_permissions(manage_messages=True)
async def sticky(interaction: discord.Interaction, message: str):
    sticky_messages[interaction.channel_id] = {"message": message, "last_msg_id": None}
    await interaction.response.send_message(embed=success_embed("Sticky Message", "Sticky message set for this channel."))

@bot.tree.command(name="poll", description="Create a poll")
async def poll(interaction: discord.Interaction, question: str):
    embed = create_embed("📊 Poll", question)
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()
    await msg.add_reaction("👍")
    await msg.add_reaction("👎")

@bot.tree.command(name="embed_builder", description="Create a custom embed message")
@app_commands.checks.has_permissions(manage_messages=True)
async def embed_builder(interaction: discord.Interaction, title: str, description: str, color_hex: Optional[str] = "5865F2"):
    try:
        color_val = int(color_hex.lstrip('#'), 16)
    except ValueError:
        color_val = COLOR_PRIMARY
    embed = create_embed(title, description, color=color_val)
    await interaction.channel.send(embed=embed)
    await interaction.response.send_message(embed=success_embed("Embed Created", "Custom embed posted successfully!"), ephemeral=True)

@bot.tree.command(name="automod", description="Enable or disable AutoMod")
@app_commands.checks.has_permissions(administrator=True)
async def automod_toggle(interaction: discord.Interaction, enabled: bool):
    await db.execute("INSERT INTO guild_settings (guild_id, automod_enabled) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET automod_enabled = ?",
                     (interaction.guild_id, enabled, enabled))
    status = "enabled" if enabled else "disabled"
    await interaction.response.send_message(embed=success_embed("AutoMod", f"AutoMod is now **{status}**."))

# ==========================================
# WEB SERVER (KEEP-ALIVE FOR RENDER)
# ==========================================
async def handle_ping(request):
    return web.Response(text="Bot is online and active!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

# ==========================================
# MAIN EXECUTION
# ==========================================
async def main():
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN environment variable is missing!")

    await start_web_server()
    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
