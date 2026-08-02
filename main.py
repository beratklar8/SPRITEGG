import os
import re
import time
import random
import asyncio
import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiosqlite
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# CONFIGURATION
# ==========================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot_data.db")
PORT = int(os.getenv("PORT", 8080))

COLOR_PRIMARY = 0x5865F2    # Blurple
COLOR_SUCCESS = 0x57F287    # Green
COLOR_WARNING = 0xFEE75C    # Yellow
COLOR_ERROR = 0xED4245      # Red

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
                    automod_enabled BOOLEAN DEFAULT 0
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
        super().__init__(command_prefix="!", intents=intents)
        self.invite_regex = re.compile(r"(discord\.gg|discord\.com/invite)/[a-zA-Z0-9]+")

    async def setup_hook(self):
        await db.init()
        self.add_view(TicketView())
        self.check_giveaways.start()
        await self.tree.sync()

    async def on_ready(self):
        print(f'Bot is online! Logged in as: {self.user.name} (ID: {self.user.id})')
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="over the server"))

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        # AutoMod Check
        row = await db.fetchone("SELECT automod_enabled FROM guild_settings WHERE guild_id = ?", (message.guild.id,))
        if row and row[0] and not message.author.guild_permissions.manage_messages:
            if self.invite_regex.search(message.content):
                await message.delete()
                return await message.channel.send(embed=warning_embed("AutoMod", f"{message.author.mention}, posting invite links is not allowed!"), delete_after=5)

            if len(message.content) > 10 and sum(1 for c in message.content if c.isupper()) / len(message.content) > 0.7:
                await message.delete()
                return await message.channel.send(embed=warning_embed("AutoMod", f"{message.author.mention}, please avoid excessive caps!"), delete_after=5)

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

bot = ModerationBot()

# ==========================================
# SLASH COMMANDS
# ==========================================
@bot.tree.command(name="kick", description="Kicks a member from the server")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    await user.kick(reason=reason)
    await interaction.response.send_message(embed=success_embed("Kicked", f"{user.mention} has been kicked.\n**Reason:** {reason}"))

@bot.tree.command(name="ban", description="Bans a member from the server")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    await user.ban(reason=reason)
    await interaction.response.send_message(embed=success_embed("Banned", f"{user.mention} has been banned.\n**Reason:** {reason}"))

@bot.tree.command(name="timeout", description="Timeout a member for a specified duration")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, user: discord.Member, minutes: int, reason: str = "No reason provided"):
    duration = datetime.timedelta(minutes=minutes)
    await user.timeout(duration, reason=reason)
    await interaction.response.send_message(embed=success_embed("Timeout", f"{user.mention} has been timed out for {minutes} minutes."))

@bot.tree.command(name="untimeout", description="Remove timeout from a member")
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, user: discord.Member):
    await user.timeout(None)
    await interaction.response.send_message(embed=success_embed("Timeout Removed", f"Timeout for {user.mention} has been removed."))

@bot.tree.command(name="warn", description="Issue a warning to a member")
@app_commands.checks.has_permissions(manage_messages=True)
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str):
    await db.execute("INSERT INTO warnings (guild_id, user_id, moderator_id, reason) VALUES (?, ?, ?, ?)",
                     (interaction.guild_id, user.id, interaction.user.id, reason))
    await interaction.response.send_message(embed=warning_embed("Warning Issued", f"{user.mention} has been WARNED.\n**Reason:** {reason}"))

@bot.tree.command(name="warnings", description="View warnings for a member")
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

@bot.tree.command(name="automod", description="Enable or disable AutoMod")
@app_commands.checks.has_permissions(administrator=True)
async def automod_toggle(interaction: discord.Interaction, enabled: bool):
    await db.execute("INSERT INTO guild_settings (guild_id, automod_enabled) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET automod_enabled = ?",
                     (interaction.guild_id, enabled, enabled))
    status = "enabled" if enabled else "disabled"
    await interaction.response.send_message(embed=success_embed("AutoMod", f"AutoMod is now **{status}**."))

@bot.tree.command(name="setup_tickets", description="Create the ticket panel in this channel")
@app_commands.checks.has_permissions(administrator=True)
async def setup_tickets(interaction: discord.Interaction, category: discord.CategoryChannel = None):
    if category:
        await db.execute("INSERT INTO ticket_configs (guild_id, category_id) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET category_id = ?",
                         (interaction.guild_id, category.id, category.id))
    await interaction.channel.send(embed=create_embed("Support Tickets", "Click the button below to open a ticket."), view=TicketView())
    await interaction.response.send_message(embed=success_embed("Tickets", "Ticket panel successfully created."), ephemeral=True)

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

@bot.tree.command(name="userinfo", description="Get information about a user")
async def userinfo(interaction: discord.Interaction, user: discord.Member = None):
    user = user or interaction.user
    embed = create_embed(f"User Info - {user.name}", f"ID: `{user.id}`")
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Account Created", value=f"<t:{int(user.created_at.timestamp())}:D>", inline=True)
    embed.add_field(name="Joined Server", value=f"<t:{int(user.joined_at.timestamp())}:D>", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="serverinfo", description="Get information about this server")
async def serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    embed = create_embed(f"Server Info - {guild.name}", f"ID: `{guild.id}`")
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
    embed.add_field(name="Members", value=f"{guild.member_count}", inline=True)
    await interaction.response.send_message(embed=embed)

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
