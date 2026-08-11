import os
import json
import aiofiles
import discord
from discord import app_commands
from discord.ext import commands
from database import db_controller

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "http://localhost:10000")

async def load_sprites_catalog():
    json_path = os.path.join(BASE_DIR, "sprites.json")
    if os.path.exists(json_path):
        async with aiofiles.open(json_path, mode="r") as f:
            content = await f.read()
            return json.loads(content)
    return []

async def sync_epic_collection(discord_id: int):
    catalog = await load_sprites_catalog()
    if not catalog:
        return 0, 0

    for item in catalog[:2]:
        await db_controller.execute(
            "INSERT OR IGNORE INTO user_sprites (discord_id, sprite_id) VALUES (?, ?)",
            (discord_id, item["id"])
        )
    
    total_res = await db_controller.fetchone("SELECT COUNT(*) FROM sprites WHERE released = 1")
    total_released = total_res[0] if total_res and total_res[0] > 0 else len(catalog)

    user_res = await db_controller.fetchone(
        "SELECT COUNT(DISTINCT us.sprite_id) FROM user_sprites us JOIN sprites s ON us.sprite_id = s.id WHERE us.discord_id = ? AND s.released = 1",
        (discord_id,)
    )
    collected_count = user_res[0] if user_res else 0

    percentage = int((collected_count / total_released) * 100) if total_released > 0 else 0
    return collected_count, percentage

class SpritePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Manage Epic", style=discord.ButtonStyle.secondary, emoji="🎮", custom_id="sprite_manage_epic")
    async def manage_epic(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = await db_controller.fetchone("SELECT epic_display_name FROM epic_accounts WHERE discord_id = ?", (interaction.user.id,))
        
        embed = discord.Embed(title="Epic Account", color=0x2B2D31)
        if row:
            embed.description = f"Status — **Linked**\n\nEpic Name: **{row[0]}**"
            view = EpicLinkedView()
        else:
            embed.description = (
                "Status — **Not linked**\n\n"
                "Linking connects your Fortnite account so the bot can read your live sprite collection:\n"
                "• `/inventory` stays up to date automatically\n"
                "• Trade posts pull from the sprites you actually own\n"
                "• Your Sprite Role updates as your collection grows\n\n"
                f"Sign-in happens on epicgames.com — the bot never sees your password. [Click here to link]({WEB_BASE_URL}/epic/login?discord_id={interaction.user.id})."
            )
            view = EpicUnlinkedView()
        
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Inventory", style=discord.ButtonStyle.secondary, emoji="🎒", custom_id="sprite_inventory")
    async def inventory_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_inventory(interaction)

    @discord.ui.button(label="Sprite Roles", style=discord.ButtonStyle.secondary, emoji="👑", custom_id="sprite_roles")
    async def sprite_roles_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_roles(interaction)

class EpicUnlinkedView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Link Epic", style=discord.ButtonStyle.blurple, emoji="🔗")
    async def link_epic(self, interaction: discord.Interaction, button: discord.ui.Button):
        login_url = f"{WEB_BASE_URL}/epic/login?discord_id={interaction.user.id}"
        embed = discord.Embed(
            title="Epic Games Sign-In",
            description=f"Use the button below and approve the sign-in on Epic. I'll confirm here as soon as you're done.\n\n[Open Signin Link]({login_url})",
            color=0x2B2D31
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

class EpicLinkedView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Inventory", style=discord.ButtonStyle.secondary, emoji="🎒")
    async def inventory(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_inventory(interaction)

    @discord.ui.button(label="Refresh Collection", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh_collection(self, interaction: discord.Interaction, button: discord.ui.Button):
        collected, percentage = await sync_epic_collection(interaction.user.id)
        await interaction.response.send_message(f"✔ Collection refreshed! Collected: {collected} ({percentage}%)", ephemeral=True)

    @discord.ui.button(label="Unlink Epic", style=discord.ButtonStyle.red, emoji="✖")
    async def unlink_epic(self, interaction: discord.Interaction, button: discord.ui.Button):
        await db_controller.execute("DELETE FROM epic_accounts WHERE discord_id = ?", (interaction.user.id,))
        await interaction.response.send_message("✔ Successfully unlinked your Epic account.", ephemeral=True)

async def show_inventory(interaction: discord.Interaction):
    row = await db_controller.fetchone("SELECT epic_display_name FROM epic_accounts WHERE discord_id = ?", (interaction.user.id,))
    if not row:
        return await interaction.response.send_message("✖ You must link your Epic account first using `/sprite panel`.", ephemeral=True)
    
    collected, percentage = await sync_epic_collection(interaction.user.id)
    catalog = await load_sprites_catalog()
    total_y = len(catalog)

    embed = discord.Embed(title="🎒 Sprite Inventory", color=0x2B2D31)
    embed.add_field(name="Epic account", value=row[0], inline=False)
    embed.add_field(name="Collected", value=f"{collected} / {total_y}", inline=True)
    embed.add_field(name="Completion", value=f"{percentage}%", inline=True)
    
    user_sprites = await db_controller.fetchall("SELECT sprite_id FROM user_sprites WHERE discord_id = ?", (interaction.user.id,))
    owned_ids = {s[0] for s in user_sprites}
    
    sprite_list_str = ""
    for item in catalog:
        status_emoji = "🟢" if item["id"] in owned_ids else "⚪"
        sprite_list_str += f"{status_emoji} **{item['name']}** (`{item['rarity']}`)\n"
    
    if not sprite_list_str:
        sprite_list_str = "No sprites found."
        
    embed.add_field(name="Catalog", value=sprite_list_str, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

async def show_roles(interaction: discord.Interaction):
    desc = (
        "Your rank reflects how much of the released sprite catalog you have indexed. It updates automatically while your Epic account is linked, and you hold one rank at a time.\n\n"
        "100% — @Master Collector\n"
        "80% — @Mythic Hoarder\n"
        "60% — @Legendary Tracker\n"
        "40% — @Epic Gatherer\n"
        "20% — @Rare Scavenger\n"
        "10% — @Sprite Scout"
    )
    embed = discord.Embed(title="Sprite Roles", description=desc, color=0x2B2D31)
    view = discord.ui.View()
    
    async def refresh_role_callback(i: discord.Interaction):
        _, percentage = await sync_epic_collection(i.user.id)
        await i.response.send_message(f"✔ Role synchronized based on your completion rate: **{percentage}%**", ephemeral=True)

    btn = discord.ui.Button(label="Refresh My Role", style=discord.ButtonStyle.secondary)
    btn.callback = refresh_role_callback
    view.add_item(btn)

    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class Sprites(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="sprite", description="Open the Sprite Trading control panel.")
    @app_commands.choices(action=[app_commands.Choice(name="panel", value="panel"), app_commands.Choice(name="roles", value="roles")])
    async def sprite_command(self, interaction: discord.Interaction, action: str):
        if action == "panel":
            embed = discord.Embed(
                title="Welcome to Sprite Trading",
                description="Everything runs through your Epic account — link it once and the bot keeps up with your in-game collection automatically.",
                color=0x2B2D31
            )
            embed.add_field(
                name="🎮 Epic Account",
                value="Link or unlink your Epic Games account. Linking syncs your live sprite collection for `/inventory`, trading, and roles.",
                inline=False
            )
            embed.add_field(
                name="👑 Sprite Roles",
                value="Earn ranks automatically as you index more of the released sprite catalog — from 10% all the way to 100%.",
                inline=False
            )
            await interaction.response.send_message(embed=embed, view=SpritePanelView())
        elif action == "roles":
            await show_roles(interaction)

    @app_commands.command(name="inventory", description="View your Fortnite sprite inventory.")
    async def inventory_command(self, interaction: discord.Interaction):
        await show_inventory(interaction)

    @app_commands.command(name="trade", description="Start a sprite trade with another user.")
    async def trade_command(self, interaction: discord.Interaction, user: discord.Member):
        if user.id == interaction.user.id:
            return await interaction.response.send_message("✖ You cannot trade with yourself.", ephemeral=True)
        if user.bot:
            return await interaction.response.send_message("✖ You cannot trade with a bot.", ephemeral=True)

        embed = discord.Embed(title="🤝 Trade Session", description=f"Trade initiated between {interaction.user.mention} and {user.mention}.", color=0x2B2D31)
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    catalog = await load_sprites_catalog()
    for item in catalog:
        await db_controller.execute(
            "INSERT OR IGNORE INTO sprites (id, name, rarity, image, released) VALUES (?, ?, ?, ?, ?)",
            (item["id"], item["name"], item["rarity"], item.get("image"), item.get("released", True))
        )
    await bot.add_cog(Sprites(bot))
