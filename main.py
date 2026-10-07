import asyncio
import logging
import os
import secrets
import time

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from database import Database
from giveaways_worker import GiveawayWorker


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | "
        "%(levelname)s | "
        "%(name)s | "
        "%(message)s"
    ),
)

log = logging.getLogger(
    "giveaway-bot"
)


TOKEN = os.getenv(
    "DISCORD_TOKEN"
)

DATABASE_PATH = os.getenv(
    "GIVEAWAY_DB",
    "giveaways.db",
)


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


# ================================================================
# GIVEAWAY VIEW
# ================================================================

class GiveawayView(discord.ui.View):
    def __init__(
        self,
        bot,
        message_id: int | None = None,
        disabled: bool = False,
    ):
        super().__init__(
            timeout=None
        )

        self.bot = bot
        self.message_id = message_id

        button = discord.ui.Button(
            label="🎉 Meedoen",
            style=discord.ButtonStyle.success,
            custom_id="giveaway:join",
            disabled=disabled,
        )

        button.callback = (
            self.join_callback
        )

        self.add_item(button)

    async def join_callback(
        self,
        interaction: discord.Interaction,
    ):
        if interaction.message is None:
            await interaction.response.send_message(
                "Ongeldige giveaway.",
                ephemeral=True,
            )
            return

        message_id = (
            interaction.message.id
        )

        success, reason = (
            await self.bot.db.add_participant(
                message_id,
                interaction.user.id,
            )
        )

        responses = {
            "JOINED": (
                "Je doet mee! 🎉"
            ),
            "ALREADY_JOINED": (
                "Je doet al mee aan deze giveaway."
            ),
            "FULL": (
                "Deze giveaway zit vol."
            ),
            "CLOSED": (
                "Deze giveaway is al gesloten."
            ),
            "NOT_FOUND": (
                "Deze giveaway bestaat niet."
            ),
        }

        await interaction.response.send_message(
            responses.get(
                reason,
                "Er ging iets mis.",
            ),
            ephemeral=True,
        )


# ================================================================
# BOT
# ================================================================

class GiveawayBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()

        intents.guilds = True
        intents.members = True

        super().__init__(
            command_prefix="!",
            intents=intents,
        )

        self.db = Database(
            DATABASE_PATH
        )

        self.worker = GiveawayWorker(
            self,
            self.db,
            concurrency=4,
        )

        self.startup_complete = False
        self.shutdown_started = False

    # ============================================================
    # VIEWS
    # ============================================================

    def create_giveaway_view(
        self,
        message_id: int | None = None,
        disabled: bool = False,
    ):
        return GiveawayView(
            self,
            message_id,
            disabled,
        )

    # ============================================================
    # SETUP
    # ============================================================

    async def setup_hook(self):
        await self.db.connect()

        await self.db.integrity_check()

        # Persistent view.
        self.add_view(
            self.create_giveaway_view()
        )

        # Slash command registreren.
        self.tree.add_command(
            self.giveaway
        )

        await self.tree.sync()

        log.info(
            "Discord commands gesynchroniseerd."
        )

    # ============================================================
    # READY
    # ============================================================

    async def on_ready(self):
        if self.startup_complete:
            return

        self.startup_complete = True

        log.info(
            "Ingelogd als %s.",
            self.user,
        )

        await self.startup_reconciliation()

        await self.worker.start()

    # ============================================================
    # RECOVERY
    # ============================================================

    async def startup_reconciliation(self):
        cutoff = (
            time.time() - 120
        )

        processing = (
            await self.db.stale_processing(
                cutoff
            )
        )

        for row in processing:
            message_id = int(
                row["message_id"]
            )

            local_task = (
                self.worker.active_tasks.get(
                    message_id
                )
            )

            if local_task is not None:
                local_task.cancel()
                continue

            await self.db.recover_processing(
                message_id,
                row["processing_token"],
                row["result_winners"]
                is not None,
            )

        result_leases = (
            await self.db.stale_result_leases(
                cutoff
            )
        )

        for row in result_leases:
            await self.db.recover_result_lease(
                int(row["message_id"]),
                row[
                    "result_send_owner_token"
                ],
            )

        log.info(
            "Startup reconciliation voltooid."
        )

    # ============================================================
    # GIVEAWAY COMMAND
    # ============================================================

    @app_commands.command(
        name="giveaway",
        description="Maak een giveaway aan.",
    )
    @app_commands.describe(
        prize="De prijs van de giveaway.",
        minutes="Duur in minuten.",
        winners="Aantal winnaars.",
        max_participants="Maximum aantal deelnemers.",
    )
    async def giveaway(
        self,
        interaction: discord.Interaction,
        prize: str,
        minutes: app_commands.Range[
            int,
            1,
            10080,
        ],
        winners: app_commands.Range[
            int,
            1,
            50,
        ],
        max_participants: app_commands.Range[
            int,
            1,
            100000,
        ],
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Dit kan alleen in een server.",
                ephemeral=True,
            )
            return

        if interaction.channel is None:
            await interaction.response.send_message(
                "Dit kanaal kan niet worden gebruikt.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(
            ephemeral=True
        )

        creation_token = new_token(
            "create"
        )

        # Eenmalig berekenen.
        # Een recovery mag de eindtijd niet verlengen.
        expires_at = (
            time.time()
            + int(minutes) * 60
        )

        payload = {
            "prize": prize,
            "winner_count": int(
                winners
            ),
            "max_participants": int(
                max_participants
            ),
            "expires_at": expires_at,
        }

        # --------------------------------------------------------
        # Creation intent
        # --------------------------------------------------------

        await self.db.create_intent(
            creation_token,
            interaction.guild.id,
            interaction.channel.id,
            payload,
        )

        # --------------------------------------------------------
        # Discord bericht
        # --------------------------------------------------------

        embed = discord.Embed(
            title="🎉 Giveaway",
            description=(
                f"**Prijs:** {prize}\n"
                f"**Winnaars:** {winners}\n"
                f"**Maximum deelnemers:** "
                f"{max_participants}\n\n"
                "Klik hieronder op **Meedoen**."
            ),
        )

        embed.set_footer(
            text=(
                f"giveaway-create:"
                f"{creation_token}"
            )
        )

        # Disabled totdat DB-record bestaat.
        message = await interaction.channel.send(
            embed=embed,
            view=self.create_giveaway_view(
                disabled=True
            ),
        )

        # --------------------------------------------------------
        # Discord message opgeslagen
        # --------------------------------------------------------

        intent_saved = (
            await self.db.set_intent_message(
                creation_token,
                message.id,
            )
        )

        if not intent_saved:
            try:
                await message.delete()
            except discord.HTTPException:
                pass

            await self.db.fail_intent(
                creation_token,
                "Could not save Discord message ID.",
            )

            await interaction.followup.send(
                "De giveaway kon niet worden aangemaakt.",
                ephemeral=True,
            )

            return

        # --------------------------------------------------------
        # Giveaway DB-record
        # --------------------------------------------------------

        created = await self.db.create_giveaway(
            message_id=message.id,
            guild_id=interaction.guild.id,
            channel_id=interaction.channel.id,
            prize=prize,
            winner_count=int(
                winners
            ),
            max_participants=int(
                max_participants
            ),
            expires_at=expires_at,
            creation_token=creation_token,
        )

        if not created:
            await self.db.fail_intent(
                creation_token,
                "Could not create giveaway row.",
            )

            try:
                await message.edit(
                    view=self.create_giveaway_view(
                        message.id,
                        disabled=True,
                    )
                )
            except discord.HTTPException:
                pass

            await interaction.followup.send(
                "De giveaway kon niet worden opgeslagen.",
                ephemeral=True,
            )

            return

        # --------------------------------------------------------
        # Activation
        # --------------------------------------------------------

        await self.db.set_intent_activation_pending(
            creation_token
        )

        await message.edit(
            view=self.create_giveaway_view(
                message.id,
                disabled=False,
            )
        )

        await self.db.complete_intent(
            creation_token
        )

        await interaction.followup.send(
            "Giveaway succesvol aangemaakt! 🎉",
            ephemeral=True,
        )

    # ============================================================
    # SHUTDOWN
    # ============================================================

    async def close(self):
        if self.shutdown_started:
            return

        self.shutdown_started = True

        log.info(
            "Bot wordt afgesloten..."
        )

        await self.worker.stop()
        await self.db.close()

        await super().close()


# ================================================================
# START
# ================================================================

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN ontbreekt."
    )


bot = GiveawayBot()


if __name__ == "__main__":
    try:
        asyncio.run(
            bot.start(TOKEN)
        )
    except KeyboardInterrupt:
        pass
