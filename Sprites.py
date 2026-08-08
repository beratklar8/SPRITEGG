import json
import os
import discord
from discord.ext import commands
from discord import app_commands

class SpriteSelectView(discord.ui.View):
    def __init__(self, sprites, user_id, already_owned=None):
        super().__init__(timeout=180)
        self.sprites = sprites  # List of (name, rarity)
        self.user_id = user_id
        self.current_page = 0
        self.per_page = 5
        self.selected_sprites = set(already_owned) if already_owned else set()
        self.update_components()

    def update_components(self):
        self.clear_items()
        
        start = self.current_page * self.per_page
        end = start + self.per_page
        page_items = self.sprites[start:end]

        # Add toggle buttons for current page items
        for name, rarity in page_items:
            is_selected = name in self.selected_sprites
            emoji = "✅" if is_selected else "⬛"
            button = discord.ui.Button(
                label=f"{name} ({rarity})",
                style=discord.ButtonStyle.success if is_selected else discord.ButtonStyle.secondary,
                custom_id=f"sprite_{name}",
                emoji=emoji,
                row=len(self.children) // 5 if len(self.children) < 20 else 0
            )
            button.callback = self.make_toggle_callback(name)
            self.add_item(button)

        # Pagination & Done buttons
        total_pages = (len(self.sprites) - 1) // self.per_page + 1

        prev_button = discord.ui.Button(label="◀ Previous", style=discord.ButtonStyle.primary, disabled=(self.current_page == 0), row=4)
        prev_button.callback = self.prev_page_callback
        self.add_item(prev_button)

        page_indicator = discord.ui.Button(label=f"Page {self.current_page + 1}/{total_pages}", style=discord.ButtonStyle.grey, disabled=True, row=4)
        self.add_item(page_indicator)

        next_button = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.primary, disabled=(self.current_page >= total_pages - 1), row=4)
        next_button.callback = self.next_page_callback
        self.add_item(next_button)

        done_button = discord.ui.Button(label="Done / Save", style=discord.ButtonStyle.danger, emoji="💾", row=4)
        done_button.callback = self.done_callback
        self.add_item(done_button)

    def make_toggle_callback(self, sprite_name):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("This menu is not for you!", ephemeral=True)
            
            if sprite_name in self.selected_sprites:
                self.selected_sprites.remove(sprite_name)
            else:
                self.selected_sprites.add(sprite_name)
                
            self.update_components()
            await interaction.response.edit_message(view=self)
        return callback

    async def prev_page_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This menu is not for you!", ephemeral=True)
        if self.current_page > 0:
            self.current_page -= 1
            self.update_components()
            await interaction.response.edit_message(view=self)

    async def next_page_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This menu is not for you!", ephemeral=True)
        total_pages = (len(self.sprites) - 1) // self.per_page + 1
        if self.current_page < total_pages - 1:
            self.current_page += 1
            self.update_components()
            await interaction.response.edit_message(view=self)

    async def done_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This menu is not for you!", ephemeral=True)
        
        try:
            from main import db_controller
            
            # Fetch existing items already in the database for this user
            existing_rows = await db_controller.fetchall("SELECT sprite_name FROM user_inventory WHERE user_id = ?", (self.user_id,))
            existing_sprites = {row[0] for row in existing_rows}
            
            # Items selected now but not yet in the DB -> Add them
            added_count = 0
            for name in self.selected_sprites:
                if name not in existing_sprites:
                    catalog_item = await db_controller.fetchone("SELECT description FROM sprites_data WHERE name = ?", (name,))
                    if catalog_item:
                        rarity = catalog_item[0]
                        await db_controller.execute(
                            "INSERT OR IGNORE INTO user_inventory (user_id, sprite_name, rarity) VALUES (?, ?, ?)",
                            (self.user_id, name, rarity)
                        )
                        added_count += 1
            
            # Items that were in the DB but deselected by the user -> Remove them
            removed_count = 0
            for name in existing_sprites:
                if name not in self.selected_sprites:
                    await db_controller.execute(
                        "DELETE FROM user_inventory WHERE user_id = ? AND sprite_name = ?",
                        (self.user_id, name)
                    )
                    removed_count += 1

            embed = discord.Embed(
                title="✔ Selection Saved!",
                description=f"Inventory updated! Added **{added_count}** and removed **{removed_count}** sprites.",
                color=0x2ECC71
            )
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            await interaction.response.send_message(f"An error occurred while saving: {e}", ephemeral=True)


class Sprites(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.loop.create_task(self.seed_sprites())

    async def seed_sprites(self):
        await self.bot.wait_until_ready()
        try:
            from main import db_controller
            if os.path.exists("sprites.json"):
                with open("sprites.json", "r", encoding="utf-8") as f:
                    sprites_data = json.load(f)
                    
                for sprite in sprites_data:
                    name = sprite.get("name")
                    rarity = sprite.get("rarity")
                    if name and rarity:
                        description = f"Rarity: {rarity}"
                        await db_controller.execute(
                            "INSERT OR REPLACE INTO sprites_data (name, description) VALUES (?, ?)",
                            (name, description)
                        )
                print("Sprites successfully loaded from sprites.json into the database!")
        except Exception as e:
            print(f"Error while auto-seeding sprites from JSON: {e}")

    @app_commands.command(name="addsprites", description="Open the interactive menu to select and add sprites to your inventory.")
    async def addsprites(self, interaction: discord.Interaction):
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

            # Fetch already saved sprites for this user
            user_rows = await db_controller.fetchall("SELECT sprite_name FROM user_inventory WHERE user_id = ?", (interaction.user.id,))
            already_owned = [r[0] for r in user_rows]

            formatted_sprites = [(name, desc.replace("Rarity: ", "")) for name, desc in rows]
            
            view = SpriteSelectView(formatted_sprites, interaction.user.id, already_owned=already_owned)
            embed = discord.Embed(
                title="🎮 Sprite Selection Menu",
                description="Click on the sprites below to select or deselect them. Use the pages to navigate and click **Done / Save** when you are finished!",
                color=0x3498DB
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            
        except Exception as e:
            embed = discord.Embed(
                title="✖ Error",
                description=f"Failed to open sprite menu: {e}",
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
                description=desc + "\n*(Use /addsprites to open the interactive selection menu)*",
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
                    description=f"{target.mention} does not have any sprites in their inventory yet.",
                    color=0xF1C40F
                )
                return await interaction.response.send_message(embed=embed)
            
            # Split items into chunks to stay safely under Discord's 4096 character limit
            embeds = []
            current_desc = ""
            
            for name, rarity in rows:
                line = f"🌟 **{name}** `[{rarity}]`\n"
                if len(current_desc) + len(line) > 4000:
                    embeds.append(discord.Embed(
                        title=f"🎒 Game Inventory — {target.name}",
                        description=current_desc,
                        color=0xF1C40F
                    ))
                    current_desc = line
                else:
                    current_desc += line
                    
            if current_desc:
                embeds.append(discord.Embed(
                    title=f"🎒 Game Inventory — {target.name}",
                    description=current_desc,
                    color=0xF1C40F
                ))
            
            # Send the first page safely to prevent character length errors
            await interaction.response.send_message(embed=embeds[0])
            
        except Exception as e:
            embed = discord.Embed(
                title="✖ Error",
                description=f"Failed to load inventory: {e}",
                color=0xE74C3C
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(Sprites(bot))
