import os
import random
import datetime
import asyncio
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from aiohttp import web

# --- CONFIGURATION ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
DATABASE_PATH = "bot_database.db"
PORT = int(os.getenv("PORT", 8080))

PRIMARY_COLOR = 0x5865F2
SUCCESS_COLOR = 0x57F287
WARNING_COLOR = 0xFEE75C
DANGER_COLOR = 0xED4245

# --- KEEP-ALIVE WEB SERVER FOR RENDER ---
async def handle_ping(request):
    return web.Response(text="Bot is online and active!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Keep-alive web server started on port {PORT}")

def create_embed(title=None, description=None, color=PRIMARY_COLOR):
    return discord.Embed(title=title, description=description, color=color)

def success_embed(desc, title="Success"):
    return create_embed(title=f"✅ {title}", description=desc, color=SUCCESS_COLOR)

def error_embed(desc, title="Error"):
    return create_embed(title=f"❌ {title}", description=desc, color=DANGER_COLOR)

def warning_embed(desc, title="Warning"):
    return create_embed(title=f"⚠️ {title}", description=desc, color=WARNING_COLOR)

# --- DATABASE ENGINE ---
class Database:
    def __init__(self, db_path=DATABASE_PATH):
        self.db_path = db_path

    async def connect(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    ticket_category INTEGER
                )
            """)
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
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    user_id INTEGER,
                    unban_time TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS giveaways (
                    message_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    guild_id INTEGER,
                    prize TEXT,
                    end_time TIMESTAMP,
                    winner_count INTEGER,
                    ended INTEGER DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS giveaway_participants (
                    message_id INTEGER,
                    user_id INTEGER,
                    PRIMARY KEY (message_id, user_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    channel_id INTEGER PRIMARY KEY,
                    guild_id INTEGER,
                    user_id INTEGER,
                    status TEXT DEFAULT 'open'
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

# --- PERSISTENT BUTTON VIEWS ---
class TicketLaunchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary, custom_id="persistent_ticket_create", emoji="🎫")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        settings = await db.fetchone("SELECT ticket_category FROM guild_settings WHERE guild_id = ?", (guild.id,))
        
        category = guild.get_channel(settings[0]) if settings and settings[0] else None
        existing = await db.fetchone("SELECT channel_id FROM tickets WHERE guild_id = ? AND user_id = ? AND status = 'open'", (guild.id, interaction.user.id))
        
        if existing and guild.get_channel(existing[0]):
            return await interaction.followup.send("You already have an open ticket!", ephemeral=True)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }

        ticket_channel = await guild.create_text_channel(
            name=f"ticket-{interaction.user.name}",
            category=category,
            overwrites=overwrites
        )

        await db.execute("INSERT INTO tickets (channel_id, guild_id, user_id) VALUES (?, ?, ?)", (ticket_channel.id, guild.id, interaction.user.id))
        await ticket_channel.send(content=interaction.user.mention, embed=success_embed("Welcome! A moderator will assist you shortly.", title="Ticket Created"), view=TicketControlView())
        await interaction.followup.send(f"Ticket created: {ticket_channel.mention}", ephemeral=True)

class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="persistent_ticket_close", emoji="🔒")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Closing ticket in 5 seconds...")
        await db.execute("UPDATE tickets SET status = 'closed' WHERE channel_id = ?", (interaction.channel.id,))
        await asyncio.sleep(5)
        await interaction.channel.delete()

class GiveawayJoinView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enter Giveaway", style=discord.ButtonStyle.success, custom_id="persistent_giveaway_join", emoji="🎉")
    async def join_giveaway(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = interaction.message.id
        giveaway = await db.fetchone("SELECT ended FROM giveaways WHERE message_id = ?", (msg_id,))
        
        if not giveaway or giveaway[0] == 1:
            return await interaction.response.send_message("This giveaway has ended.", ephemeral=True)

        existing = await db.fetchone("SELECT user_id FROM giveaway_participants WHERE message_id = ? AND user_id = ?", (msg_id, interaction.user.id))
        if existing:
            await db.execute("DELETE FROM giveaway_participants WHERE message_id = ? AND user_id = ?", (msg_id, interaction.user.id))
            await interaction.response.send_message("You left the giveaway.", ephemeral=True)
        else:
            await db.execute("INSERT INTO giveaway_participants (message_id, user_id) VALUES (?, ?)", (msg_id, interaction.user.id))
            await interaction.response.send_message("You entered the giveaway!", ephemeral=True)

# --- BOT MAIN CLASS ---
class ModerationBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True

        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        await db.connect()
        await start_web_server()
        self.add_view(TicketLaunchView())
        self.add_view(TicketControlView())
        self.add_view(GiveawayJoinView())

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        try:
            synced = await self.tree.sync()
            print(f"Successfully synced {len(synced)} slash command(s).")
        except Exception as e:
            print(f"Error syncing commands: {e}")

        if not self.background_tasks.is_running():
            self.background_tasks.start()

    @tasks.loop(seconds=30)
    async def background_tasks(self):
        now = datetime.datetime.utcnow()
        # Tempban check
        records = await db.fetchall("SELECT id, guild_id, user_id FROM tempbans WHERE unban_time <= ?", (now,))
        for r in records:
            tb_id, guild_id, user_id = r
            guild = self.get_guild(guild_id)
            if guild:
                try:
                    user = await self.fetch_user(user_id)
                    await guild.unban(user, reason="Tempban expired.")
                except Exception:
                    pass
            await db.execute("DELETE FROM tempbans WHERE id = ?", (tb_id,))

        # Giveaway check
        ended_g = await db.fetchall("SELECT message_id, channel_id, prize, winner_count FROM giveaways WHERE ended = 0 AND end_time <= ?", (now,))
        for g in ended_g:
            msg_id, channel_id, prize, winner_count = g
            channel = self.get_channel(channel_id)
            if channel:
                try:
                    msg = await channel.fetch_message(msg_id)
                    participants = await db.fetchall("SELECT user_id FROM giveaway_participants WHERE message_id = ?", (msg_id,))
                    if not participants:
                        await channel.send(f"Giveaway for **{prize}** has ended! No valid participants.")
                    else:
                        winner_ids = [p[0] for p in participants]
                        selected = random.sample(winner_ids, min(winner_count, len(winner_ids)))
                        mentions = ", ".join([f"<@{uid}>" for uid in selected])
                        await channel.send(f"🎉 Congratulations {mentions}! You won **{prize}**!")
                    
                    embed = msg.embeds[0]
                    embed.title = "🎉 Giveaway Ended!"
                    embed.color = 0x747F8D
                    await msg.edit(embed=embed, view=None)
                except Exception:
                    pass
            await db.execute("UPDATE giveaways SET ended = 1 WHERE message_id = ?", (msg_id,))

bot = ModerationBot()

# --- AUTOMOD ---
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    
    # Block invite links for non-administrators
    if not message.author.guild_permissions.administrator:
        if "discord.gg/" in message.content.lower() or "discord.com/invite" in message.content.lower():
            await message.delete()
            return await message.channel.send(f"{message.author.mention}, posting invite links is prohibited!", delete_after=5)

    await bot.process_commands(message)

# --- SLASH COMMANDS ---
@bot.tree.command(name="ban", description="Ban a user from the server.")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    await member.ban(reason=f"{reason} (By {interaction.user})")
    await interaction.response.send_message(embed=success_embed(f"{member.mention} has been banned. | Reason: {reason}"))

@bot.tree.command(name="kick", description="Kick a user from the server.")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    await member.kick(reason=f"{reason} (By {interaction.user})")
    await interaction.response.send_message(embed=success_embed(f"{member.mention} has been kicked. | Reason: {reason}"))

@bot.tree.command(name="timeout", description="Timeout a user (duration in minutes).")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "No reason provided"):
    duration = datetime.timedelta(minutes=minutes)
    await member.timeout(duration, reason=reason)
    await interaction.response.send_message(embed=success_embed(f"{member.mention} has been timed out for {minutes} minutes."))

@bot.tree.command(name="warn", description="Issue an official warning to a user.")
@app_commands.checks.has_permissions(manage_messages=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    await db.execute("INSERT INTO warnings (guild_id, user_id, moderator_id, reason) VALUES (?, ?, ?, ?)",
                     (interaction.guild.id, member.id, interaction.user.id, reason))
    count = await db.fetchone("SELECT COUNT(*) FROM warnings WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, member.id))
    await interaction.response.send_message(embed=warning_embed(f"Warned {member.mention}! Reason: {reason}\nTotal warnings: {count[0]}"))

@bot.tree.command(name="purge", description="Purge a specified number of messages.")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: int):
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(embed=success_embed(f"Purged {len(deleted)} messages."), ephemeral=True)

@bot.tree.command(name="ticketpanel", description="Send the ticket creation panel to this channel.")
@app_commands.checks.has_permissions(administrator=True)
async def ticketpanel(interaction: discord.Interaction):
    embed = create_embed("Support Tickets", "Click the button below to open a support ticket.")
    await interaction.channel.send(embed=embed, view=TicketLaunchView())
    await interaction.response.send_message("Panel sent!", ephemeral=True)

@bot.tree.command(name="gstart", description="Start a giveaway.")
@app_commands.checks.has_permissions(manage_guild=True)
async def gstart(interaction: discord.Interaction, minutes: int, winners: int, prize: str):
    end_time = datetime.datetime.utcnow() + datetime.timedelta(minutes=minutes)
    embed = create_embed("🎉 Giveaway!", f"**Prize:** {prize}\n**Winners:** {winners}\n**Ends:** <t:{int(end_time.timestamp())}:R>")
    
    await interaction.response.send_message("Giveaway started!", ephemeral=True)
    msg = await interaction.channel.send(embed=embed, view=GiveawayJoinView())
    
    await db.execute("INSERT INTO giveaways (message_id, channel_id, guild_id, prize, end_time, winner_count) VALUES (?, ?, ?, ?, ?, ?)",
                     (msg.id, interaction.channel.id, interaction.guild.id, prize, end_time, winners))

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise ValueError("DISCORD_TOKEN environment variable is missing!")
    bot.run(DISCORD_TOKEN)
