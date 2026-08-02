import os
import asyncio
import random
import datetime
import discord
from discord import app_commands
from discord.ext import commands

# Set up intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class Client(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # Sync slash commands with Discord
        await self.tree.sync()
        print("Slash commands synced successfully!")

bot = Client()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} (ID: {bot.user.id})')

# --- MODERATION COMMANDS ---

# 1. /ban
@bot.tree.command(name="ban", description="Bans a member from the server.")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    await member.ban(reason=reason)
    await interaction.response.send_message(f"🚫 **{member.mention}** has been banned. Reason: *{reason}*")

# 2. /kick
@bot.tree.command(name="kick", description="Kicks a member from the server.")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    await member.kick(reason=reason)
    await interaction.response.send_message(f"👢 **{member.mention}** has been kicked. Reason: *{reason}*")

# 3. /timeout
@bot.tree.command(name="timeout", description="Puts a member in timeout for a specified duration in minutes.")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: str = "No reason provided"):
    duration = datetime.timedelta(minutes=minutes)
    await member.timeout(duration, reason=reason)
    await interaction.response.send_message(f"⏳ **{member.mention}** has been put in timeout for {minutes} minute(s). Reason: *{reason}*")

# 4. /mute (Removes messaging permissions)
@bot.tree.command(name="mute", description="Mutes a member by applying a timeout.")
@app_commands.checks.has_permissions(moderate_members=True)
async def mute(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    duration = datetime.timedelta(hours=24) # Default 24 hours
    await member.timeout(duration, reason=reason)
    await interaction.response.send_message(f"🤐 **{member.mention}** has been muted. Reason: *{reason}*")

# 5. /warn
@bot.tree.command(name="warn", description="Warns a member.")
@app_commands.checks.has_permissions(manage_messages=True)
async def warn(interaction: discord.Interaction, member: discord.Member, reason: str):
    await interaction.response.send_message(f"⚠️ **{member.mention}** has been warned! Reason: *{reason}*")
    try:
        await member.send(f"You have been warned in **{interaction.guild.name}**. Reason: *{reason}*")
    except discord.Forbidden:
        pass # Member has DMs closed

# --- GIVEAWAY COMMAND ---

# 6. /giveaway
@bot.tree.command(name="giveaway", description="Starts a giveaway.")
@app_commands.checks.has_permissions(manage_events=True)
async def giveaway(interaction: discord.Interaction, duration_seconds: int, prize: str):
    embed = discord.Embed(
        title="🎉 GIVEAWAY STARTED! 🎉",
        description=f"**Prize:** {prize}\n**Hosted by:** {interaction.user.mention}\n**React with 🎉 to enter!**",
        color=discord.Color.gold()
    )
    embed.set_footer(text=f"Ends in {duration_seconds} seconds!")
    
    await interaction.response.send_message(embed=embed)
    message = await interaction.original_response()
    await message.add_reaction("🎉")

    await asyncio.sleep(duration_seconds)

    # Fetch updated message with reactions
    message = await interaction.channel.fetch_message(message.id)
    reaction = discord.utils.get(message.reactions, emoji="🎉")
    users = [user async for user in reaction.users() if not user.bot]

    if not users:
        await interaction.followup.send(f"🎉 The giveaway for **{prize}** ended, but nobody entered!")
    else:
        winner = random.choice(users)
        await interaction.followup.send(f"🎉 Congratulations {winner.mention}! You won **{prize}**!")

# Error handler for missing permissions
@ban.error
@kick.error
@timeout.error
@mute.error
@warn.error
@giveaway.error
async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)

# Run the bot
token = os.getenv('DISCORD_TOKEN')
if token:
    bot.run(token)
else:
    print("Error: No DISCORD_TOKEN found!")
