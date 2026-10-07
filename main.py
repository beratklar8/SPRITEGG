import asyncio
import logging
import os
import secrets
import time

from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from database import Database
from giveaways_worker import GiveawayWorker


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

log = logging.getLogger("giveaway-bot")

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_PATH = os.getenv(
    "GIVEAWAY_DB",
    "giveaways.db",
)


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


# ============================================================
# RENDER WEB SERVER
# ============================================================

async def health_handler(request):
    return web.Response(
        text="Giveaway bot is online."
    )


async def health_json_handler(request):
    return web.json_response(
        {
            "status": "online",
            "bot": "giveaway",
        }
    )


async def start_web_server():
    app = web.Application()

    app.router.add_get(
        "/",
        health_handler,
    )

    app.router.add_get(
        "/health",
        health_handler,
    )

    app.router.add_get(
        "/health.json",
        health_json_handler,
    )

    runner = web.AppRunner(app)

    await runner.setup()

    # Render geeft deze variabele automatisch.
    port = int(
        os.environ.get(
            "PORT",
            "10000",
        )
    )

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=port,
    )

    await site.start()

    log.info(
        "HTTP server gestart op 0.0.0.0:%s",
        port,
    )

    return runner


# ============================================================
# GIVEAWAY VIEW
# ============================================================

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

        button.callback = self.join_callback

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

        message_id = interaction.message.id

        try:
            success, reason = (
                await self.bot.db.add_participant(
                    message_id,
                    interaction.user.id,
                )
            )
        except Exception:
            log.exception(
                "Fout bij deelnemen aan giveaway %s",
                message_id,
            )

            await interaction.response.send_message(
                "Er ging iets mis. Probeer het opnieuw.",
                ephemeral=True,
            )

            return

        responses = {
            "JOINED": (
                "Je doet mee aan de giveaway! 🎉"
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


# ============================================================
# BOT
# ============================================================

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

    # ========================================================
    # GIVEAWAY VIEW
    # ========================================================

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

    # ========================================================
    # SETUP HOOK
    # ========================================================

    async def setup_hook(self):

        log.info(
            "Database verbinden..."
        )

        await self.db.connect()

        log.info(
            "Database verbonden."
        )

        await self.db.integrity_check()

        log.info(
            "Database integrity check OK."
        )

        # Persistente giveaway buttons
        self.add_view(
            self.create_giveaway_view()
        )

        # Slash command registreren
        try:
            self.tree.add_command(
                self.giveaway
            )
        except discord.app_commands.errors.CommandAlreadyRegistered:
            pass

        await self.tree.sync()

        log.info(
            "Discord slash commands gesynchroniseerd."
        )

    # ========================================================
    # READY
    # ========================================================

    async def on_ready(self):

        if self.startup_complete:
            return

        self.startup_complete = True

        log.info(
            "========================================"
        )

        log.info(
            "Discord bot online: %s",
            self.user,
        )

        log.info(
            "Bot ID: %s",
            self.user.id if self.user else "unknown",
        )

        log.info(
            "Servers: %s",
            len(self.guilds),
        )

        log.info(
            "========================================"
        )

        await self.startup_reconciliation()

        await self.worker.start()

        log.info(
            "Giveaway worker gestart."
        )

    # ========================================================
    # STARTUP RECOVERY
    # ========================================================

    async def startup_reconciliation(self):

        cutoff = (
            time.time()
            - 120
        )

        # ----------------------------------------------------
        # Stale PROCESSING recovery
        # ----------------------------------------------------

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

                try:
                    await local_task
                except (
                    asyncio.CancelledError,
                    Exception,
                ):
                    pass

                continue

            await self.db.recover_processing(
                message_id,
                row["processing_token"],
                row["result_winners"] is not None,
            )

        # ----------------------------------------------------
        # Stale result leases
        # ----------------------------------------------------

        result_leases = (
            await self.db.stale_result_leases(
                cutoff
            )
        )

        for row in result_leases:

            await self.db.recover_result_lease(
                int(row["message_id"]),
                row["result_send_owner_token"],
            )

        log.info(
            "Startup reconciliation voltooid."
        )

    # ========================================================
    # GIVEAWAY COMMAND
    # ========================================================

    @app_commands.command(
        name="giveaway",
        description="Maak een giveaway aan.",
    )
    @app_commands.describe(
        prize="De prijs van de giveaway.",
        minutes="Duur van de giveaway in minuten.",
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

        # Eindtijd één keer bepalen.
        expires_at = (
            time.time()
            + (
                int(minutes)
                * 60
            )
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

        # ====================================================
        # CREATION INTENT
        # ====================================================

        await self.db.create_intent(
            creation_token,
            interaction.guild.id,
            interaction.channel.id,
            payload,
        )

        # ====================================================
        # GIVEAWAY EMBED
        # ====================================================

        embed = discord.Embed(
            title="🎉 Giveaway",
            description=(
                f"**Prijs:** {prize}\n"
                f"**Winnaars:** {winners}\n"
                f"**Maximum deelnemers:** "
                f"{max_participants}\n\n"
                "Klik hieronder op "
                "**🎉 Meedoen** om deel te nemen."
            ),
        )

        embed.set_footer(
            text=(
                "giveaway-create:"
                f"{creation_token}"
            )
        )

        # Eerst disabled.
        message = await interaction.channel.send(
            embed=embed,
            view=self.create_giveaway_view(
                disabled=True
            ),
        )

        # ====================================================
        # MESSAGE ID OPSLAAN
        # ====================================================

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

        # ====================================================
        # DATABASE RECORD
        # ====================================================

        created = (
            await self.db.create_giveaway(
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

        # ====================================================
        # ACTIVATION
        # ====================================================

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

    # ========================================================
    # SHUTDOWN
    # ========================================================

    async def close(self):

        if self.shutdown_started:
            return

        self.shutdown_started = True

        log.info(
            "Bot wordt afgesloten..."
        )

        try:
            await self.worker.stop()
        except Exception:
            log.exception(
                "Fout tijdens stoppen worker."
            )

        try:
            await self.db.close()
        except Exception:
            log.exception(
                "Fout tijdens sluiten database."
            )

        await super().close()


# ============================================================
# TOKEN CHECK
# ============================================================

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN ontbreekt in de Render Environment Variables."
    )


# ============================================================
# BOT INSTANCE
# ============================================================

bot = GiveawayBot()


# ============================================================
# MAIN
# ============================================================

async def main():

    # Eerst HTTP server starten.
    # Render moet direct een open poort kunnen zien.
    web_runner = await start_web_server()

    try:

        log.info(
            "Discord bot wordt gestart..."
        )

        await bot.start(
            TOKEN
        )

    except Exception:
        log.exception(
            "Discord bot is gestopt door een fout."
        )

        raise

    finally:

        log.info(
            "HTTP server wordt afgesloten..."
        )

        await web_runner.cleanup()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    try:
        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        log.info(
            "Bot handmatig gestopt."
        )
