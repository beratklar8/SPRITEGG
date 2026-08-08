import json
import os
import discord
from discord.ext import commands
from discord import app_commands

class Sprites(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.loop.create_task(self.seed_sprites())

    async def seed_sprites(self):
        await self.bot.wait_until_ready()
        try:
            from main import db_controller
            
            # Maak de user_inventory tabel aan als deze nog niet bestaat
            await db_controller.execute("""
                CREATE TABLE IF NOT EXISTS user_inventory (
                    user_id INTEGER,
                    sprite_name TEXT,
                    rarity TEXT,
                    PRIMARY KEY (user_id, sprite_name)
                )
            """)
            
            # Laad sprites.json in de catalogus
            if os.path.exists("sprites.json"):
                with open("sprites.json", "r", encoding="utf-8") as f:
                    sprites_data = json.load(f)
                    
                for sprite in sprites_data:
                    name = sprite.get("name")
                    rarity = sprite.get("rarity")
                    if name and rarity:
                        await db_controller.execute(
                            "INSERT OR IGNORE INTO sprites_data (name, description) VALUES (?, ?)",
                            (name, rarity)
                        )
                print("Sprites successfully seeded from sprites.json!")
        except Exception as e:
            print(f"Error while auto-seeding sprites from JSON/Inventory: {e}")

    @app_commands.command(name="addsprite", description="Add a sprite from the catalog to your own inventory.")
    async def addsprite(self, interaction: discord.Interaction, sprite_name: str):
        try:
            from main import db_controller
            
            # 1. Controleer of de sprite überhaupt bestaat in de catalogus (sprites_data)
            catalog_item = await db_controller.fetchone(
                "SELECT description FROM sprites_data WHERE name = ?", (sprite_name,)
            )
            
            if not catalog_item:
                embed = discord.Embed(
                    title="✖ Sprite Not Found",
                    description=f"De sprite **'{sprite_name}'** bestaat niet in de catalogus! Bekijk alle beschikbare sprites met `/listsprites`.",
                    color=0xE74C3C
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            rarity = catalog_item[0] # description in de tabel wordt gebruikt als rarity
            
            # 2. Controleer of de gebruiker deze sprite al heeft
            existing = await db_controller.fetchone(
                "SELECT 1 FROM user_inventory WHERE user_id = ? AND sprite_name = ?", 
                (interaction.user.id, sprite_name)
            )
            
            if existing:
                embed = discord.Embed(
                    title="⚠️ Al in bezit",
                    description=f"Je hebt **{sprite_name}** (`{rarity}`) al in je inventaris!",
                    color=0xF1C40F
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            # 3. Voeg toe aan de inventaris van de gebruiker
            await db_controller.execute(
                "INSERT INTO user_inventory (user_id, sprite_name, rarity) VALUES (?, ?, ?)",
                (interaction.user.id, sprite_name, rarity)
            )
            
            embed = discord.Embed(
                title="✔ Sprite Toegevoegd!",
                description=f"Je hebt **{sprite_name}** (`{rarity}`) succesvol toegevoegd aan je inventaris!",
                color=0x2ECC71
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except Exception as e:
            embed = discord.Embed(
                title="✖ Error",
                description=f"Er ging iets mis bij het toevoegen van de sprite: {e}",
                color=0xE74C3C
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="listsprites", description="View all available sprites in the game catalog.")
    async def listsprites(self, interaction: discord.Interaction):
        try:
            from main import db_controller
            rows = await db_controller.fetchall("SELECT name, description FROM sprites_data")
            
            if not rows:
                embed = discord.Embed(
                    title="⚠ Warning",
                    description="No sprites available in the database.",
                    color=0xF1C40F
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            desc = ""
            for name, rarity in rows[:30]:
                desc += f"• **{name}** — *{rarity}*\n"
                
            embed = discord.Embed(
                title="🎮 All Available Sprites & Items",
                description=desc + "\n*(Gebruik /addsprite [naam] om er eentje te claimen)*",
                color=0xF1C40F
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            embed = discord.Embed(
                title="✖ Error",
                description=f"Failed to fetch sprites: {e}",
                color=0xE74C3C
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="inventory", description="View someone's inventory of owned sprites.")
    async def inventory(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        try:
            from main import db_controller
            rows = await db_controller.fetchall("SELECT sprite_name, rarity FROM user_inventory WHERE user_id = ?", (target.id,))
            
            if not rows:
                embed = discord.Embed(
                    title="🎒 Inventory Empty",
                    description=f"{target.mention} heeft nog geen sprites in hun inventaris.",
                    color=0xF1C40F
                )
                return await interaction.response.send_message(embed=embed)
            
            desc = ""
            for name, rarity in rows:
                desc += f"🌟 **{name}** `[{rarity}]`\n"
                
            embed = discord.Embed(
                title=f"🎒 Game Inventory — {target.name}",
                description=desc,
                color=0xF1C40F
            )
            await interaction.response.send_message(embed=embed)
        except Exception as e:
            embed = discord.Embed(
                title="✖ Error",
                description=f"Failed to load inventory: {e}",
                color=0xE74C3C
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(Sprites(bot))
