import os
import re
import json
import time
import random
import asyncio
import datetime
import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from aiohttp import web
from dotenv import load_dotenv
from groq import AsyncGroq

from database import db_controller

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_TOKEN = os.getenv("DISCORD_TOKEN")
WEB_PORT = int(os.getenv("PORT", 10000))
GROQ_API_SECRET = os.getenv("GROQ_API_KEY")

CHANNEL_ONLINE_ID = 1533920905258995905
CHANNEL_UPDATING_ID = 1533921928224702685
CHANNEL_OFFLINE_ID = 1533922000005894224

COLOR_NEUTRAL = 0x2B2D31
COLOR_SUCCESS = 0x2ECC71
COLOR_WARNING = 0xF1C40F
COLOR_DANGER = 0xE74C3C
COLOR_INFO = 0x3498DB
COLOR_PURPLE = 0x9B59B6

SPECIAL_USER_ID = 1242143149917212767

groq_api_client = None
if GROQ_API_SECRET:
    try:
        groq_api_client = AsyncGroq(api_key=GROQ_API_SECRET)
    except Exception as initialization_exception:
        print(f"Failed to initialize Groq client: {initialization_exception}")

user_cooldowns = {}

def is_owner_or_special(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    return interaction.user.id == SPECIAL_USER_ID or interaction.user == interaction.guild.owner

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
        embed = error_embed("Access Denied", "Je hebt geen toestemming om dit command te gebruiken.")
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    return True

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

async def send_dm_notification(member: discord.Member, action_title: str, reason: str, guild_name: str, extra_info: Optional[str] = None):
    """Hulpfunctie om een DM te sturen naar een gebruiker wanneer er een moderatie-actie plaatsvindt."""
    try:
        desc = f"You have received a **{action_title}** in **{guild_name}**.\n\n**Reason:** {reason}"
        if extra_info:
            desc += f"\n**Details:** {extra_info}"
        embed = make_embed(f"Notification: {action_title}", desc, COLOR_WARNING)
        await member.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        # Gebruiker heeft DM's uitgeschakeld of bot geblokkeerd
        pass

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
        
        self.invite_link_regex = re.compile(r"(discord\.gg|discord\.com/invite)/[a-zA-Z0-9]+")
        self.automod_cache = {}
        self.sticky_locks = {}

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
                except discord.HTTPException as e:
                    print(f"Failed to change visibility of channel {chan_id}: {e}")

    @tasks.loop(minutes=1)
    async def background_giveaway_loop(self):
        try:
            current_time = time.time()
            rows = await db_controller.fetchall("SELECT message_id, channel_id, guild_id, prize_name FROM giveaway_system WHERE ends_at <= ? AND is_ended = 0", (current_time,))
            for row in rows:
                msg_id, chan_id, guild_id, prize = row
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
                    
                    await db_controller.execute("UPDATE giveaway_system SET is_ended = 1 WHERE message_id = ?", (msg_id,))
                except (discord.NotFound, discord.HTTPException) as api_err:
                    print(f"Temporary API error while processing giveaway {msg_id}: {api_err}")
                except Exception as loop_err:
                    print(f"Error processing giveaway {msg_id}: {loop_err}")
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
            except Exception:
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
            print(f"App command error: {error}")
            traceback.print_exception(type(error), error, error.__traceback__)
            embed = error_embed("Command Error", "An unexpected error occurred while processing this command.")
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(name="ban", description="Ban a member from the server.")
        @app_commands.default_permissions(ban_members=True)
        async def ban_slash(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason provided"):
            if not await check_permission_and_respond(interaction, "ban_members"):
                return
            try:
                await send_dm_notification(member, "Ban", reason, interaction.guild.name)
                await member.ban(reason=reason)
                await interaction.response.send_message(embed=success_embed("Member Banned", f"Successfully banned {member.mention}.\nReason: {reason}"))
            except discord.HTTPException:
                await interaction.response.send_message(embed=error_embed("Error", "Failed to ban member due to an API error."), ephemeral=True)

        @self.tree.command(name="unban", description="Unban a user by their user ID.")
        @app_commands.default_permissions(ban_members=True)
        async def unban_slash(interaction: discord.Interaction, user_id: str, reason: Optional[str] = "No reason provided"):
            if not await check_permission_and_respond(interaction, "ban_members"):
                return
            try:
                user_obj = discord.Object(id=int(user_id))
                await interaction.guild.unban(user_obj, reason=reason)
                await interaction.response.send_message(embed=success_embed("User Unbanned", f"Successfully unbanned user ID `{user_id}`."))
            except ValueError:
                await interaction.response.send_message(embed=error_embed("Error", "Ongeldige user ID opgegeven."), ephemeral=True)
            except discord.HTTPException:
                await interaction.response.send_message(embed=error_embed("Error", "Failed to unban user due to an API error."), ephemeral=True)

        @self.tree.command(name="tempban", description="Temporary ban a member from the server.")
        @app_commands.default_permissions(ban_members=True)
        async def tempban_slash(interaction: discord.Interaction, member: discord.Member, duration_hours: float, reason: Optional[str] = "No reason provided"):
            if not await check_permission_and_respond(interaction, "ban_members"):
                return
            if duration_hours <= 0:
                return await interaction.response.send_message(embed=error_embed("Error", "Duur moet groter zijn dan 0 uur."), ephemeral=True)
            expiry = time.time() + (duration_hours * 3600)
            try:
                await send_dm_notification(member, "Temporary Ban", reason, interaction.guild.name, f"Duration: {duration_hours} hours")
                await member.ban(reason=reason)
                await db_controller.execute("INSERT OR REPLACE INTO temporary_bans (guild_id, target_id, expiry_timestamp) VALUES (?, ?, ?)", (interaction.guild.id, member.id, expiry))
                await interaction.response.send_message(embed=success_embed("Temporary Ban Applied", f"Successfully banned {member.mention} for {duration_hours} hours."))
            except discord.HTTPException:
                await interaction.response.send_message(embed=error_embed("Error", "Failed to temp-ban member due to an API error."), ephemeral=True)

        @self.tree.command(name="kick", description="Kick a member from the server.")
        @app_commands.default_permissions(kick_members=True)
        async def kick_slash(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason provided"):
            if not await check_permission_and_respond(interaction, "kick_members"):
                return
            try:
                await send_dm_notification(member, "Kick", reason, interaction.guild.name)
                await member.kick(reason=reason)
                await interaction.response.send_message(embed=success_embed("Member Kicked", f"Successfully kicked {member.mention}."))
            except discord.HTTPException:
                await interaction.response.send_message(embed=error_embed("Error", "Failed to kick member due to an API error."), ephemeral=True)

        @self.tree.command(name="timeout", description="Timeout a member for a specified duration in minutes.")
        @app_commands.default_permissions(moderate_members=True)
        async def timeout_slash(interaction: discord.Interaction, member: discord.Member, minutes: int, reason: Optional[str] = "No reason provided"):
            if not await check_permission_and_respond(interaction, "moderate_members"):
                return
            if minutes <= 0:
                return await interaction.response.send_message(embed=error_embed("Error", "Aantal minuten moet groter zijn dan 0."), ephemeral=True)
            try:
                await send_dm_notification(member, "Timeout", reason, interaction.guild.name, f"Duration: {minutes} minutes")
                until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
                await member.timeout(until, reason=reason)
                await interaction.response.send_message(embed=success_embed("Timeout Applied", f"Successfully timed out {member.mention} for {minutes} minutes."))
            except discord.HTTPException:
                await interaction.response.send_message(embed=error_embed("Error", "Failed to timeout member due to an API error."), ephemeral=True)

        @self.tree.command(name="untimeout", description="Remove timeout from a member.")
        @app_commands.default_permissions(moderate_members=True)
        async def untimeout_slash(interaction: discord.Interaction, member: discord.Member, reason: Optional[str] = "No reason provided"):
            if not await check_permission_and_respond(interaction, "moderate_members"):
                return
            try:
                await member.timeout(None, reason=reason)
                await interaction.response.send_message(embed=success_embed("Timeout Removed", f"Successfully removed timeout for {member.mention}."))
            except discord.HTTPException:
                await interaction.response.send_message(embed=error_embed("Error", "Failed to remove timeout due to an API error."), ephemeral=True)

        @self.tree.command(name="purge", description="Bulk delete messages in the channel.")
        @app_commands.default_permissions(manage_messages=True)
        async def purge_slash(interaction: discord.Interaction, amount: int):
            if not await check_permission_and_respond(interaction, "manage_messages"):
                return
            if amount <= 0:
                return await interaction.response.send_message(embed=error_embed("Error", "Aantal moet groter zijn dan 0."), ephemeral=True)
            await interaction.response.defer(ephemeral=True)
            deleted = await interaction.channel.purge(limit=amount)
            await interaction.followup.send(embed=success_embed("Purge Complete", f"Successfully deleted {len(deleted)} messages."), ephemeral=True)

        @self.tree.command(name="warn", description="Issue an official warning to a member.")
        @app_commands.default_permissions(moderate_members=True)
        async def warn_slash(interaction: discord.Interaction, member: discord.Member, reason: str):
            if not await check_permission_and_respond(interaction, "moderate_members"):
                return
            await send_dm_notification(member, "Warning", reason, interaction.guild.name)
            await db_controller.execute("INSERT INTO warning_records (guild_id, target_id, moderator_id, reason) VALUES (?, ?, ?, ?)", (interaction.guild.id, member.id, interaction.user.id, reason))
            await interaction.response.send_message(embed=success_embed("Warning Issued", f"Warned {member.mention} for: {reason}"))

        @self.tree.command(name="warnings", description="View all warnings for a specific member.")
        @app_commands.default_permissions(moderate_members=True)
        async def warnings_slash(interaction: discord.Interaction, member: discord.Member):
            if not await check_permission_and_respond(interaction, "moderate_members"):
                return
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
            if not await check_permission_and_respond(interaction, "manage_channels"):
                return
            if seconds < 0:
                return await interaction.response.send_message(embed=error_embed("Error", "Slowmode kan niet negatief zijn."), ephemeral=True)
            try:
                await interaction.channel.edit(slowmode_delay=seconds)
                await interaction.response.send_message(embed=success_embed("Slowmode Updated", f"Channel slowmode set to `{seconds}` seconds."))
            except discord.HTTPException:
                await interaction.response.send_message(embed=error_embed("Error", "Failed to update slowmode due to an API error."), ephemeral=True)

        @self.tree.command(name="lock", description="Lock the current channel to prevent members from sending messages.")
        @app_commands.default_permissions(manage_channels=True)
        async def lock_slash(interaction: discord.Interaction):
            if not await check_permission_and_respond(interaction, "manage_channels"):
                return
            try:
                await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
                await interaction.response.send_message(embed=success_embed("Channel Locked", "This channel has been locked."))
            except discord.HTTPException:
                await interaction.response.send_message(embed=error_embed("Error", "Failed to lock channel due to an API error."), ephemeral=True)

        @self.tree.command(name="unlock", description="Unlock the current channel.")
        @app_commands.default_permissions(manage_channels=True)
        async def unlock_slash(interaction: discord.Interaction):
            if not await check_permission_and_respond(interaction, "manage_channels"):
                return
            try:
                await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=True)
                await interaction.response.send_message(embed=success_embed("Channel Unlocked", "This channel has been unlocked."))
            except discord.HTTPException:
                await interaction.response.send_message(embed=error_embed("Error", "Failed to unlock channel due to an API error."), ephemeral=True)

        @self.tree.command(name="say", description="Make the bot say something in the channel.")
        @app_commands.default_permissions(manage_messages=True)
        async def say_slash(interaction: discord.Interaction, message: str):
            if not await check_permission_and_respond(interaction, "manage_messages"):
                return
            await interaction.response.send_message(embed=success_embed("Message Sent", "Done!"), ephemeral=True)
            await interaction.channel.send(message)

        @self.tree.command(name="embed", description="Send a custom text inside an embed.")
        @app_commands.default_permissions(manage_messages=True)
        async def embed_slash(interaction: discord.Interaction, title: str, description: str):
            if not await check_permission_and_respond(interaction, "manage_messages"):
                return
            await interaction.response.send_message(embed=success_embed("Embed Sent", "Done!"), ephemeral=True)
            await interaction.channel.send(embed=make_embed(title, description, COLOR_INFO))

        @self.tree.command(name="automod", description="Toggle or configure the AutoMod filter status.")
        @app_commands.default_permissions(administrator=True)
        async def automod_slash(interaction: discord.Interaction, status: bool):
            if not await check_permission_and_respond(interaction, "administrator"):
                return
            await db_controller.execute("INSERT OR REPLACE INTO server_settings(guild_id, automod_status) VALUES (?, ?)", (interaction.guild.id, status))
            self.automod_cache[interaction.guild.id] = status
            await interaction.response.send_message(embed=success_embed("AutoMod Updated", f"AutoMod system status is now set to: `{status}`"))

        @self.tree.command(name="giveaway", description="Start a new server giveaway.")
        @app_commands.default_permissions(manage_guild=True)
        async def giveaway_slash(interaction: discord.Interaction, prize: str, duration_minutes: float):
            if not await check_permission_and_respond(interaction, "manage_guild"):
                return
            if duration_minutes <= 0:
                return await interaction.response.send_message(embed=error_embed("Error", "Giveaway duur moet groter zijn dan 0 minuten."), ephemeral=True)
            ends_at = time.time() + (duration_minutes * 60)
            embed = make_embed("🎉 GIVEAWAY 🎉", f"Prize: **{prize}**\nReact with 🎉 to enter!\nEnds: <t:{int(ends_at)}:R>", COLOR_PURPLE)
            await interaction.response.send_message(embed=success_embed("Giveaway Started", "Giveaway message deployed!"), ephemeral=True)
            msg = await interaction.channel.send(embed=embed)
            await msg.add_reaction("🎉")
            await db_controller.execute("INSERT INTO giveaway_system (message_id, channel_id, guild_id, prize_name, ends_at) VALUES (?, ?, ?, ?, ?)", (msg.id, interaction.channel.id, interaction.guild.id, prize, ends_at))

        @self.tree.command(name="sticky", description="Create or clear a sticky message in the channel.")
        @app_commands.default_permissions(manage_messages=True)
        async def sticky_slash(interaction: discord.Interaction, text: Optional[str] = None):
            if not await check_permission_and_respond(interaction, "manage_messages"):
                return
            if not text:
                await db_controller.execute("DELETE FROM sticky_messages WHERE channel_id = ?", (interaction.channel.id,))
                return await interaction.response.send_message(embed=success_embed("Sticky Removed", "Sticky message cleared for this channel."), ephemeral=True)
            
            existing = await db_controller.fetchone("SELECT message_id FROM sticky_messages WHERE channel_id = ?", (interaction.channel.id,))
            if existing and existing[0]:
                try:
                    old_msg = await interaction.channel.fetch_message(existing[0])
                    await old_msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass

            msg = await interaction.channel.send(embed=make_embed("Pinned Operational Notice", text))
            await db_controller.execute("INSERT OR REPLACE INTO sticky_messages (channel_id, guild_id, text, message_id) VALUES (?, ?, ?, ?)", (interaction.channel.id, interaction.guild.id, text, msg.id))
            await interaction.response.send_message(embed=success_embed("Sticky Active", "Sticky message successfully deployed."), ephemeral=True)

        @self.tree.command(name="reactionrole", description="Bind a reaction emoji to a role on a message.")
        @app_commands.default_permissions(administrator=True)
        async def reactionrole_slash(interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
            if not await check_permission_and_respond(interaction, "administrator"):
                return
            try:
                m_id = int(message_id)
                msg = await interaction.channel.fetch_message(m_id)
                await msg.add_reaction(emoji)
                await db_controller.execute("INSERT OR REPLACE INTO reaction_role_bindings (message_id, emoji_icon, role_id) VALUES (?, ?, ?)", (m_id, emoji, role.id))
                await interaction.response.send_message(embed=success_embed("Reaction Role Bound", f"Successfully linked {emoji} to {role.mention} on message `{m_id}`."), ephemeral=True)
            except ValueError:
                await interaction.response.send_message(embed=error_embed("Error", "Ongeldige message ID opgegeven."), ephemeral=True)
            except discord.HTTPException:
                await interaction.response.send_message(embed=error_embed("Error", "Failed to bind reaction role due to an API error."), ephemeral=True)

        await self.tree.sync()
        print("Slash commands synced.")

    async def on_ready(self):
        print(f"Successfully logged in as {self.user} (ID: {self.user.id})")
        await self.set_status('online')
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="over server security | /help"))

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.user.id or not payload.guild_id:
            return
        res = await db_controller.fetchone(
            "SELECT role_id FROM reaction_role_bindings WHERE message_id = ? AND emoji_icon = ?",
            (payload.message_id, str(payload.emoji))
        )
        if res:
            guild = self.get_guild(payload.guild_id)
            if guild:
                role = guild.get_role(res[0])
                member = guild.get_member(payload.user_id)
                if not member:
                    try:
                        member = await guild.fetch_member(payload.user_id)
                    except discord.HTTPException:
                        member = None
                if role and member:
                    try:
                        await member.add_roles(role, reason="Reaction Role (Add)")
                    except discord.HTTPException:
                        pass

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.user.id or not payload.guild_id:
            return
        res = await db_controller.fetchone(
            "SELECT role_id FROM reaction_role_bindings WHERE message_id = ? AND emoji_icon = ?",
            (payload.message_id, str(payload.emoji))
        )
        if res:
            guild = self.get_guild(payload.guild_id)
            if guild:
                role = guild.get_role(res[0])
                member = guild.get_member(payload.user_id)
                if not member:
                    try:
                        member = await guild.fetch_member(payload.user_id)
                    except discord.HTTPException:
                        member = None
                if role and member:
                    try:
                        await member.remove_roles(role, reason="Reaction Role (Remove)")
                    except discord.HTTPException:
                        pass

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id
        if guild_id not in self.automod_cache:
            settings = await db_controller.fetchone("SELECT automod_status FROM server_settings WHERE guild_id = ?", (guild_id,))
            self.automod_cache[guild_id] = bool(settings and settings[0])

        if self.automod_cache[guild_id] and not message.author.guild_permissions.manage_messages:
            if self.invite_link_regex.search(message.content):
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                return await message.channel.send(embed=warning_embed("Automod Notice", f"{message.author.mention}, server invite links are strictly prohibited."), delete_after=5)

            letters = [c for c in message.content if c.isalpha()]
            if len(letters) > 10 and sum(1 for c in letters if c.isupper()) / len(letters) > 0.8:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                return await message.channel.send(embed=warning_embed("Automod Notice", f"{message.author.mention}, please avoid excessive capitalization."), delete_after=5)

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
                        chat_completion = await groq_api_client.chat.completions.create(
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
                            for i in range(0, len(reply_text), 1900):
                                chunk = reply_text[i:i+1900]
                                if i == 0:
                                    await message.reply(chunk)
                                else:
                                    await message.channel.send(chunk)
                    except Exception as err:
                        traceback.print_exc()
                        err_msg = str(err)
                        if "429" in err_msg or "rate_limit" in err_msg.lower():
                            await message.reply("API rate limit reached. Please try your request again shortly.")
                        else:
                            await message.reply("An error occurred while communicating with the AI model.")
                else:
                    await message.reply("Groq API key is not configured.")

        if message.channel.id not in self.sticky_locks:
            self.sticky_locks[message.channel.id] = asyncio.Lock()

        async with self.sticky_locks[message.channel.id]:
            sticky_row = await db_controller.fetchone("SELECT text, message_id FROM sticky_messages WHERE channel_id = ?", (message.channel.id,))
            if sticky_row:
                sticky_text, old_msg_id = sticky_row
                if old_msg_id:
                    try:
                        old_msg = await message.channel.fetch_message(old_msg_id)
                        await old_msg.delete()
                    except (discord.NotFound, discord.HTTPException):
                        pass
                new_sticky = await message.channel.send(embed=make_embed("Pinned Operational Notice", sticky_text))
                await db_controller.execute("UPDATE sticky_messages SET message_id = ? WHERE channel_id = ?", (new_sticky.id, message.channel.id))

        await self.process_commands(message)

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
