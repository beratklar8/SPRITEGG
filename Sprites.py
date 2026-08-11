import discord

class EpicCollectionProvider:
    @staticmethod
    async def get_user_collection(db_controller, discord_id: int) -> list:
        rows = await db_controller.fetchall("SELECT sprite_id FROM user_sprites WHERE discord_id = ?", (discord_id,))
        if not rows:
            default_sprites = ["sprite_1", "sprite_2"]
            for s_id in default_sprites:
                await db_controller.execute(
                    "INSERT OR IGNORE INTO user_sprites (discord_id, sprite_id) VALUES (?, ?)",
                    (discord_id, s_id)
                )
            return default_sprites
        return [row[0] for row in rows]

async def calculate_completion(db_controller, discord_id: int) -> float:
    catalog = await db_controller.fetchall("SELECT id FROM sprite_catalog WHERE released = 1")
    total_released = len(catalog) if catalog else 3
    user_sprites = await EpicCollectionProvider.get_user_collection(db_controller, discord_id)
    catalog_ids = [c[0] for c in catalog] if catalog else ["sprite_1", "sprite_2", "sprite_3"]
    owned_released = [s for s in user_sprites if s in catalog_ids]
    return (len(owned_released) / total_released) * 100.0 if total_released > 0 else 0.0

async def update_sprite_roles(member: discord.Member, percentage: float):
    roles_config = [
        (10.0, "Sprite Scout"),
        (20.0, "Rare Scavenger"),
        (40.0, "Epic Gatherer"),
        (60.0, "Legendary Tracker"),
        (80.0, "Mythic Hoarder"),
        (100.0, "Master Collector")
    ]
    target_role_name = None
    for threshold, r_name in sorted(roles_config, reverse=True):
        if percentage >= threshold:
            target_role_name = r_name
            break
            
    guild = member.guild
    for _, r_name in roles_config:
        role = discord.utils.get(guild.roles, name=r_name)
        if not role:
            continue
        if r_name == target_role_name:
            if role not in member.roles:
                try:
                    await member.add_roles(role, reason="Sprite Collection Milestone")
                except Exception:
                    pass
        else:
            if role in member.roles:
                try:
                    await member.remove_roles(role, reason="Sprite Collection Update")
                except Exception:
                    pass

class SpritePanelView(discord.ui.View):
    def __init__(self, db_controller, web_base_url: str):
        super().__init__(timeout=None)
        self.db_controller = db_controller
        self.web_base_url = web_base_url

    @discord.ui.button(label="Manage Epic", style=discord.ButtonStyle.primary, emoji="🎮")
    async def manage_epic(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = await self.db_controller.fetchone("SELECT epic_display_name FROM epic_accounts WHERE discord_id = ?", (interaction.user.id,))
        embed = discord.Embed(title="Epic Account", color=0x2B2D31)
        if row:
            embed.description = f"Status — **Linked**\n\nEpic Name: `{row[0]}`"
            view = EpicLinkedView(self.db_controller)
        else:
            embed.description = "Status — **Not linked**\n\nLinking connects your Fortnite account so the bot can read your live sprite collection."
            view = EpicUnlinkedView(self.web_base_url)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Inventory", style=discord.ButtonStyle.secondary, emoji="🎒")
    async def inventory_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_inventory(interaction, self.db_controller)

    @discord.ui.button(label="Sprite Roles", style=discord.ButtonStyle.secondary, emoji="👑")
    async def roles_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_roles(interaction)

class EpicUnlinkedView(discord.ui.View):
    def __init__(self, web_base_url: str):
        super().__init__(timeout=180)
        self.web_base_url = web_base_url

    @discord.ui.button(label="Link Epic", style=discord.ButtonStyle.green, emoji="🔗")
    async def link_epic(self, interaction: discord.Interaction, button: discord.ui.Button):
        login_url = f"{self.web_base_url}/epic/login?discord_id={interaction.user.id}"
        embed = discord.Embed(
            title="Epic Games Sign-In",
            description=f"Use the button below and approve the sign-in on Epic. I'll confirm here as soon as you're done.\n\n[Open Signin Link]({login_url})",
            color=0x2B2D31
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

class EpicLinkedView(discord.ui.View):
    def __init__(self, db_controller):
        super().__init__(timeout=180)
        self.db_controller = db_controller

    @discord.ui.button(label="Refresh Collection", style=discord.ButtonStyle.primary, emoji="🔄")
    async def refresh_coll(self, interaction: discord.Interaction, button: discord.ui.Button):
        percentage = await calculate_completion(self.db_controller, interaction.user.id)
        if isinstance(interaction.user, discord.Member):
            await update_sprite_roles(interaction.user, percentage)
        await interaction.response.send_message(f"Collection refreshed! Current completion: **{percentage:.1f}%**", ephemeral=True)

    @discord.ui.button(label="Unlink Epic", style=discord.ButtonStyle.red, emoji="✖️")
    async def unlink_epic(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.db_controller.execute("DELETE FROM epic_accounts WHERE discord_id = ?", (interaction.user.id,))
        await interaction.response.send_message("Successfully unlinked your Epic account.", ephemeral=True)

async def show_inventory(interaction: discord.Interaction, db_controller):
    row = await db_controller.fetchone("SELECT epic_display_name FROM epic_accounts WHERE discord_id = ?", (interaction.user.id,))
    if not row:
        return await interaction.response.send_message("You must link your Epic account first using `/sprite panel`.", ephemeral=True)
    sprites = await EpicCollectionProvider.get_user_collection(db_controller, interaction.user.id)
    completion = await calculate_completion(db_controller, interaction.user.id)
    embed = discord.Embed(title="🎒 Sprite Inventory", color=0x2B2D31)
    embed.add_field(name="Epic Account", value=row[0], inline=False)
    embed.add_field(name="Progress", value=f"Collected: **{len(sprites)}** | Completion: **{completion:.1f}%**", inline=False)
    embed.add_field(name="Owned Sprites", value=", ".join(sprites) if sprites else "None", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

async def show_roles(interaction: discord.Interaction):
    desc = (
        "Your rank reflects how much of the released sprite catalog you have indexed. "
        "It updates automatically while your Epic account is linked, and you hold one rank at a time.\n\n"
        "100% — @Master Collector\n"
        "80% — @Mythic Finder\n"
        "60% — @Legendary Tracker\n"
        "40% — @Epic Gatherer\n"
        "20% — @Rare Scavenger\n"
        "10% — @Sprite Scout"
    )
    embed = discord.Embed(title="Sprite Roles", description=desc, color=0x2B2D31)
    await interaction.response.send_message(embed=embed, ephemeral=True)
