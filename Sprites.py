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
            
            # Controleer of sprites.json bestaat
            if os.path.exists("sprites.json"):
                with open("sprites.json", "r", encoding="utf-8") as f:
                    sprites_data = json.load(f)
                    
                for sprite in sprites_data:
                    name = sprite.get("name")
                    rarity = sprite.get("rarity")
                    # Je kunt hier eventueel ook category of image_url opslaan als je database dat ondersteunt
                    if name and rarity:
                        await db_controller.execute(
                            "INSERT OR IGNORE INTO sprites_data (name, description) VALUES (?, ?)",
                            (name, rarity)
                        )
                print("Sprites successfully seeded from sprites.json!")
        except Exception as e:
            print(f"Error while auto-seeding sprites from JSON: {e}")

    @app_commands.command(name="addsprite", description="Add a new sprite to the database.")
    @app_commands.default_permissions(administrator=True)
    async def addsprite(self, interaction: discord.Interaction, name: str, rarity: str):
        try:
            from main import db_controller
            await db_controller.execute(
                "INSERT OR REPLACE INTO sprites_data (name, description) VALUES (?, ?)",
                (name, rarity)
            )
            embed = discord.Embed(
                title="✔ Sprite Saved",
                description=f"Sprite **{name}** (Rarity: {rarity}) has been successfully saved!",
                color=0x2ECC71
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            embed = discord.Embed(
                title="✖ Error",
                description=f"Failed to save sprite: {e}",
                color=0xE74C3C
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="listsprites", description="View all available sprites in the database.")
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
                description=desc + "\n*(Loaded via sprites.json)*",
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

    @app_commands.command(name="inventory", description="View someone's inventory or items.")
    async def inventory(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        try:
            from main import db_controller
            rows = await db_controller.fetchall("SELECT name, description FROM sprites_data LIMIT 15")
            
            if not rows:
                embed = discord.Embed(
                    title="⚠ Inventory Empty",
                    description="No sprites available in the database.",
                    color=0xF1C40F
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            
            desc = f"Inventory for {target.mention}:\n\n"
            for name, rarity in rows:
                desc += f"🌟 **{name}** [{rarity}] - `Owned / Mastered`\n"
                
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
