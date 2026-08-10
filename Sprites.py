import os
import json
import discord
from discord.ext import commands
from discord import app_commands
from database import db_controller

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class Sprites(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.loop.create_task(self.seed_sprites())

    async def seed_sprites(self):
        await self.bot.wait_until_ready()
        try:
            json_path = os.path.join(BASE_DIR, "sprites.json")
            if os.path.exists(json_path):
                with open(json_path, "r", encoding="utf-8") as f:
                    sprites_data = json.load(f)
                    
                for sprite in sprites_data:
                    name = sprite.get("name")
                    rarity = sprite.get("rarity")
                    if name and rarity:
                        await db_controller.execute(
                            "INSERT OR REPLACE INTO sprites (name, rarity) VALUES (?, ?)",
                            (name, rarity)
                        )
                print("Sprites successfully loaded from sprites.json into the database!")
        except Exception as e:
            print(f"Error while auto-seeding sprites from JSON: {e}")

    @app_commands.command(name="listsprites", description="View all available sprites in the game catalog.")
    async def listsprites(self, interaction: discord.Interaction):
        try:
            rows = await db_controller.fetchall("SELECT name, rarity FROM sprites")
            
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
                description=desc,
                color=0xF1C40F
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            print(f"Error in listsprites: {e}")
            embed = discord.Embed(
                title="✖ Error",
                description="An unexpected error occurred while fetching the sprites.",
                color=0xE74C3C
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(Sprites(bot))
