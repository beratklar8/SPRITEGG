import discord
from discord.ext import commands
from discord import app_commands

# List of all your items and variants to load automatically
INITIAL_SPRITES = [
    ("John Wick", "MYTHIC"),
    ("Batman", "MYTHIC"),
    ("Cube Batman", "SPECIAL"),
    ("Gold Batman", "SPECIAL"),
    ("Gummy Batman", "SPECIAL"),
    ("Galaxy Batman", "SPECIAL"),
    ("Holofoil Batman", "SPECIAL"),
    ("Water", "SPECIAL"),
    ("Gold Water", "SPECIAL"),
    ("Quack Water", "SPECIAL"),
    ("Gummy Water", "SPECIAL"),
    ("Galaxy Water", "SPECIAL"),
    ("Gem Water", "SPECIAL"),
    ("Holofoil Water", "SPECIAL"),
    ("Earth", "SPECIAL"),
    ("Cube Earth", "SPECIAL"),
    ("Gold Earth", "SPECIAL"),
    ("Quack Earth", "SPECIAL"),
    ("Gummy Earth", "SPECIAL"),
    ("Galaxy Earth", "SPECIAL"),
    ("Gem Earth", "SPECIAL"),
    ("Fire", "RARE"),
    ("Cube Fire", "SPECIAL"),
    ("Gold Fire", "SPECIAL"),
    ("Quack Fire", "SPECIAL"),
    ("Gummy Fire", "SPECIAL"),
    ("Galaxy Fire", "SPECIAL"),
    ("Holofoil Fire", "SPECIAL"),
    ("Duck", "EPIC"),
    ("Gold Duck", "SPECIAL"),
    ("Gummy Duck", "SPECIAL"),
    ("Galaxy Duck", "SPECIAL"),
    ("Gem Duck", "SPECIAL"),
    ("Ghost", "EPIC"),
    ("Gold Ghost", "SPECIAL"),
    ("Gummy Ghost", "SPECIAL"),
    ("Galaxy Ghost", "SPECIAL"),
    ("Holofoil Ghost", "SPECIAL"),
    ("Dream", "LEGENDARY"),
    ("Cube Dream", "SPECIAL"),
    ("Gold Dream", "SPECIAL"),
    ("Gummy Dream", "SPECIAL"),
    ("Galaxy Dream", "SPECIAL"),
    ("Demon", "EPIC"),
    ("Punk", "LEGENDARY"),
    ("Cube Punk", "SPECIAL"),
    ("Gold Punk", "SPECIAL"),
    ("Gummy Punk", "SPECIAL"),
    ("Galaxy Punk", "SPECIAL"),
    ("King", "EPIC"),
    ("Gold King", "SPECIAL"),
    ("Gummy King", "SPECIAL"),
    ("Galaxy King", "SPECIAL"),
    ("Holofoil King", "SPECIAL"),
    ("Vini Jr.", "MYTHIC"),
    ("Burnt Peanut", "MYTHIC"),
    ("Zero Point", "MYTHIC"),
    ("Cube Zero Point", "SPECIAL"),
    ("Gold Zero Point", "SPECIAL"),
    ("Quack Zero Point", "SPECIAL"),
    ("Gummy Zero Point", "SPECIAL"),
    ("Galaxy Zero Point", "SPECIAL"),
    ("Gem Zero Point", "SPECIAL"),
    ("Holofoil Zero Point", "SPECIAL"),
    ("Fishy", "RARE"),
    ("Cube Fishy", "SPECIAL"),
    ("Gold Fishy", "SPECIAL"),
    ("Gummy Fishy", "SPECIAL"),
    ("Galaxy Fishy", "SPECIAL"),
    ("Striker", "EPIC"),
    ("Gold Striker", "SPECIAL"),
    ("Gummy Striker", "SPECIAL"),
    ("Galaxy Striker", "SPECIAL"),
    ("Holofoil Striker", "SPECIAL"),
    ("Aura", "EPIC"),
    ("Gold Aura", "SPECIAL"),
    ("Gummy Aura", "SPECIAL"),
    ("Galaxy Aura", "SPECIAL"),
    ("Gem Aura", "SPECIAL"),
    ("Boss", "LEGENDARY"),
    ("Cube Boss", "SPECIAL"),
    ("Gold Boss", "SPECIAL"),
    ("Gummy Boss", "SPECIAL"),
    ("Galaxy Boss", "SPECIAL"),
    ("Grim", "MYTHIC"),
    ("Cube Grim", "SPECIAL"),
    ("Gold Grim", "SPECIAL"),
    ("Gummy Grim", "SPECIAL"),
    ("Galaxy Grim", "SPECIAL"),
    ("Gem Grim", "SPECIAL"),
    ("Holofoil Grim", "SPECIAL"),
    ("Air", "RARE"),
    ("Gold Air", "SPECIAL"),
    ("Gummy Air", "SPECIAL"),
    ("Galaxy Air", "SPECIAL"),
    ("Holofoil Air", "SPECIAL"),
    ("Seven", "LEGENDARY"),
    ("Gold Seven", "SPECIAL"),
    ("Gummy Seven", "SPECIAL"),
    ("Galaxy Seven", "SPECIAL"),
    ("Holofoil Seven", "SPECIAL"),
    ("Ironmouse", "MYTHIC"),
    ("Pollo", "MYTHIC"),
    ("Llama", "LEGENDARY"),
    ("Gold Llama", "SPECIAL"),
    ("Gummy Llama", "SPECIAL"),
    ("Galaxy Llama", "SPECIAL"),
    ("Gem Llama", "SPECIAL"),
    ("Peely", "LEGENDARY"),
    ("Gold Peely", "SPECIAL"),
    ("Gummy Peely", "SPECIAL"),
    ("Galaxy Peely", "SPECIAL"),
    ("Holofoil Peely", "SPECIAL")
]

class Sprites(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.loop.create_task(self.seed_sprites())

    async def seed_sprites(self):
        await self.bot.wait_until_ready()
        try:
            from main import db_controller
            for name, rarity in INITIAL_SPRITES:
                await db_controller.execute(
                    "INSERT OR IGNORE INTO sprites_data (name, description) VALUES (?, ?)",
                    (name, rarity)
                )
        except Exception as e:
            print(f"Error while auto-seeding sprites: {e}")

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
                description=desc + "\n*(Displayed in yellow style)*",
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
