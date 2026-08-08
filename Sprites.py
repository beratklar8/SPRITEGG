import json
import os
import discord
from discord import app_commands
from discord.ext import commands
import aiosqlite

DATABASE_NAME = os.getenv("DATABASE_PATH", "bot.db")
SPRITES_JSON_PATH = "sprites.json"

class SpritePagingView(discord.ui.View):
    def __init__(self, bot, user_id: int, sprites: list, index: int = 0):
        super().__init__(timeout=180)
        self.bot = bot
        self.user_id = user_id
        self.sprites = sprites
        self.index = index
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = self.index <= 0
        self.next_button.disabled = self.index >= len(self.sprites) - 1

    async def get_user_status(self, sprite_id: str):
        async with aiosqlite.connect(DATABASE_NAME) as db:
            async with db.execute(
                "SELECT owned, mastered FROM user_sprites WHERE user_id = ? AND sprite_id = ?",
                (self.user_id, sprite_id)
            ) as cursor:
                row = await cursor.fetchone()
                return (bool(row[0]), bool(row[1])) if row else (False, False)

    async def create_sprite_embed(self) -> discord.Embed:
        sprite = self.sprites[self.index]
        owned, mastered = await self.get_user_status(sprite["id"])

        rarity_colors = {
            "Common": 0x95A5A6,
            "Rare": 0x3498DB,
            "EPIC": 0x9B59B6,
            "LEGENDARY": 0xF1C40F,
            "MYTHIC": 0xE67E22,
            "SPECIAL": 0x1ABC9C
        }
        color = rarity_colors.get(sprite["rarity"].upper(), 0x2B2D31)

        embed = discord.Embed(
            title=f"🎨 {sprite['name']}",
            description=f"**Rarity:** `{sprite['rarity']}`\n**Category:** `{sprite['category']}`",
            color=color
        )
        embed.set_image(url=sprite["image_url"])
        embed.set_footer(text=f"Sprite {self.index + 1} of {len(self.sprites)} • ID: {sprite['id']}")
        
        status_text = f"✅ **Owned:** {'Yes' if owned else 'No'}\n⭐ **Mastered:** {'Yes' if mastered else 'No'}"
        embed.add_field(name="Collection Status", value=status_text, inline=False)
        return embed

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("You cannot control this menu.", ephemeral=True)
        if self.index > 0:
            self.index -= 1
            self.update_buttons()
            embed = await self.create_sprite_embed()
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji="✅", label="Owned", style=discord.ButtonStyle.success)
    async def owned_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("You cannot control this menu.", ephemeral=True)
        
        sprite = self.sprites[self.index]
        owned, mastered = await self.get_user_status(sprite["id"])
        new_owned = 0 if owned else 1

        async with aiosqlite.connect(DATABASE_NAME) as db:
            await db.execute(
                """
                INSERT INTO user_sprites (user_id, sprite_id, owned, mastered) 
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, sprite_id) DO UPDATE SET owned = ?
                """,
                (self.user_id, sprite["id"], new_owned, int(mastered), new_owned)
            )
            await db.commit()

        embed = await self.create_sprite_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji="⭐", label="Mastered", style=discord.ButtonStyle.primary)
    async def mastered_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("You cannot control this menu.", ephemeral=True)
        
        sprite = self.sprites[self.index]
        owned, mastered = await self.get_user_status(sprite["id"])
        new_mastered = 0 if mastered else 1
        
        # When mastered is True, owned automatically becomes True as well
        new_owned = 1 if new_mastered == 1 else int(owned)

        async with aiosqlite.connect(DATABASE_NAME) as db:
            await db.execute(
                """
                INSERT INTO user_sprites (user_id, sprite_id, owned, mastered) 
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, sprite_id) DO UPDATE SET owned = ?, mastered = ?
                """,
                (self.user_id, sprite["id"], new_owned, new_mastered, new_owned, new_mastered)
            )
            await db.commit()

        embed = await self.create_sprite_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("You cannot control this menu.", ephemeral=True)
        if self.index < len(self.sprites) - 1:
            self.index += 1
            self.update_buttons()
            embed = await self.create_sprite_embed()
            await interaction.response.edit_message(embed=embed, view=self)


class Sprites(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sprites_cache = []
        self.load_sprites_json()

    def load_sprites_json(self):
        if os.path.exists(SPRITES_JSON_PATH):
            with open(SPRITES_JSON_PATH, "r", encoding="utf-8") as f:
                self.sprites_cache = json.load(f)
        else:
            self.sprites_cache = []

    async def cog_load(self):
        async with aiosqlite.connect(DATABASE_NAME) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS sprites (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    rarity TEXT,
                    category TEXT,
                    image_url TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_sprites (
                    user_id INTEGER,
                    sprite_id TEXT,
                    owned BOOLEAN DEFAULT 0,
                    mastered BOOLEAN DEFAULT 0,
                    PRIMARY KEY (user_id, sprite_id)
                )
            """)
            await db.commit()

            # Synchronize JSON data into SQLite table
            for sprite in self.sprites_cache:
                await db.execute(
                    """
                    INSERT INTO sprites (id, name, rarity, category, image_url) 
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET name=excluded.name, rarity=excluded.rarity, category=excluded.category, image_url=excluded.image_url
                    """,
                    (sprite["id"], sprite["name"], sprite["rarity"], sprite["category"], sprite["image_url"])
                )
            await db.commit()

    @app_commands.command(name="sprites", description="Open the interactive Sprite collection viewer.")
    async def sprites_cmd(self, interaction: discord.Interaction):
        if not self.sprites_cache:
            return await interaction.response.send_message("No sprites available in the database.", ephemeral=True)
        
        view = SpritePagingView(self.bot, interaction.user.id, self.sprites_cache, index=0)
        embed = await view.create_sprite_embed()
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="sprite", description="Search and view a specific sprite by name.")
    @app_commands.describe(name="The exact or partial name of the sprite")
    async def sprite_cmd(self, interaction: discord.Interaction, name: str):
        found_index = -1
        for idx, s in enumerate(self.sprites_cache):
            if name.lower() in s["name"].lower():
                found_index = idx
                break

        if found_index == -1:
            return await interaction.response.send_message(embed=discord.Embed(title="Not Found", description=f"No sprite matching `{name}` was found.", color=0xE74C3C), ephemeral=True)

        view = SpritePagingView(self.bot, interaction.user.id, self.sprites_cache, index=found_index)
        embed = await view.create_sprite_embed()
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="collection", description="View your or another user's collection completion stats.")
    @app_commands.describe(member="The user whose collection you want to check")
    async def collection_cmd(self, interaction: discord.Interaction, member: discord.Member = None):
        target = member or interaction.user
        total_sprites = len(self.sprites_cache)
        if total_sprites == 0:
            return await interaction.response.send_message("No sprites configured.", ephemeral=True)

        async with aiosqlite.connect(DATABASE_NAME) as db:
            async with db.execute("SELECT COUNT(*) FROM user_sprites WHERE user_id = ? AND owned = 1", (target.id,)) as cursor:
                owned_count = (await cursor.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM user_sprites WHERE user_id = ? AND mastered = 1", (target.id,)) as cursor:
                mastered_count = (await cursor.fetchone())[0]

        owned_pct = (owned_count / total_sprites) * 100
        mastered_pct = (mastered_count / total_sprites) * 100

        embed = discord.Embed(
            title=f"📊 Collection Stats: {target.display_name}",
            color=0x3498DB
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="✅ Owned", value=f"`{owned_count} / {total_sprites}` ({owned_pct:.1f}%)", inline=False)
        embed.add_field(name="⭐ Mastered", value=f"`{mastered_count} / {total_sprites}` ({mastered_pct:.1f}%)", inline=False)
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="View the top sprite collectors.")
    async def leaderboard_cmd(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DATABASE_NAME) as db:
            async with db.execute("""
                SELECT user_id, SUM(owned) as total_owned 
                FROM user_sprites 
                WHERE owned = 1 
                GROUP BY user_id 
                ORDER BY total_owned DESC 
                LIMIT 10
            """) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            return await interaction.response.send_message("No collection data recorded yet.", ephemeral=True)

        medals = ["👑", "🥈", "🥉"]
        desc = ""
        for index, (user_id, count) in enumerate(rows):
            prefix = medals[index] if index < 3 else f"`{index + 1}.`"
            desc += f"{prefix} <@{user_id}> — **{count}** Sprites Owned\n"

        embed = discord.Embed(title="🏆 Sprite Collector Leaderboard", description=desc, color=0x9B59B6)
        await interaction.response.send_message(embed=embed)


async def setup(bot):
    await bot.add_cog(Sprites(bot))
