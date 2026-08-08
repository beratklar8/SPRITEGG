import discord
from discord.ext import commands
from discord import app_commands

class Sprites(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="addsprite", description="Voeg een nieuwe sprite toe aan de database.")
    @app_commands.default_permissions(administrator=True)
    async def addsprite(self, interaction: discord.Interaction, name: str, description: str):
        # Haal de database controller op uit main (of gebruik je eigen db-methode)
        try:
            from main import db_controller
            await db_controller.execute(
                "INSERT OR REPLACE INTO sprites_data (name, description) VALUES (?, ?)",
                (name, description)
            )
            await interaction.response.send_message(f"Sprite **{name}** is succesvol opgeslagen in de database!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Fout bij opslaan: {e}", ephemeral=True)

    @app_commands.command(name="listsprites", description="Bekijk alle beschikbare sprites in de database.")
    async def listsprites(self, interaction: discord.Interaction):
        try:
            from main import db_controller
            rows = await db_controller.fetchall("SELECT name, description FROM sprites_data")
            
            if not rows:
                return await interaction.response.send_message("No sprites available in the database.", ephemeral=True)
            
            desc = ""
            for name, description in rows:
                desc += f"**{name}**: {description}\n"
                
            embed = discord.Embed(title="🎮 Beschikbare Sprites", description=desc, color=0x2ECC71)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Fout bij ophalen: {e}", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Sprites(bot))
