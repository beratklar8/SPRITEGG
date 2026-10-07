import asyncio
import json
import logging
import random
import secrets
import time

import discord

from database import Database

log = logging.getLogger(__name__)

PROCESSING_TIMEOUT = 120
RESULT_LEASE_TIMEOUT = 120
WATCHDOG_INTERVAL = 30
MAX_RESULT_RETRIES = 5


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


class GiveawayWorker:
    def __init__(
        self,
        bot,
        database: Database,
        concurrency: int = 4,
    ):
        self.bot = bot
        self.db = database

        self.semaphore = asyncio.Semaphore(
            concurrency
        )

        self.concurrency = concurrency

        self.stop_event = asyncio.Event()

        self.processor_task = None
        self.watchdog_task = None

        self.active_tasks: dict[
            int,
            asyncio.Task,
        ] = {}

    # ============================================================
    # LIFECYCLE
    # ============================================================

    async def start(self):
        self.stop_event.clear()

        self.processor_task = asyncio.create_task(
            self.processor_loop(),
            name="giveaway-processor",
        )

        self.watchdog_task = asyncio.create_task(
            self.watchdog_loop(),
            name="giveaway-watchdog",
        )

        log.info(
            "Giveaway worker gestart."
        )

    async def stop(self):
        self.stop_event.set()

        tasks = []

        if self.processor_task is not None:
            self.processor_task.cancel()
            tasks.append(self.processor_task)

        if self.watchdog_task is not None:
            self.watchdog_task.cancel()
            tasks.append(self.watchdog_task)

        if tasks:
            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

        active = list(
            self.active_tasks.values()
        )

        for task in active:
            task.cancel()

        if active:
            await asyncio.gather(
                *active,
                return_exceptions=True,
            )

        self.active_tasks.clear()

    # ============================================================
    # PROCESSOR
    # ============================================================

    async def processor_loop(self):
        while not self.stop_event.is_set():
            try:
                expired = (
                    await self.db.get_expired_giveaways(
                        self.concurrency
                    )
                )

                retries = (
                    await self.db.get_result_retries(
                        self.concurrency
                    )
                )

                message_ids = {
                    int(row["message_id"])
                    for row in expired
                }

                message_ids.update(
                    int(row["message_id"])
                    for row in retries
                )

                for message_id in message_ids:
                    if (
                        message_id
                        in self.active_tasks
                    ):
                        continue

                    task = asyncio.create_task(
                        self.process_one(
                            message_id
                        ),
                        name=(
                            f"giveaway-{message_id}"
                        ),
                    )

                    self.active_tasks[
                        message_id
                    ] = task

                    task.add_done_callback(
                        lambda finished,
                        mid=message_id:
                        self.task_done(
                            mid,
                            finished,
                        )
                    )

                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(),
                        timeout=2,
                    )
                except asyncio.TimeoutError:
                    pass

            except asyncio.CancelledError:
                raise

            except Exception:
                log.exception(
                    "Fout in processor_loop."
                )

                await asyncio.sleep(2)

    def task_done(
        self,
        message_id: int,
        task: asyncio.Task,
    ):
        if (
            self.active_tasks.get(
                message_id
            )
            is task
        ):
            self.active_tasks.pop(
                message_id,
                None,
            )

        if task.cancelled():
            return

        try:
            exception = task.exception()
        except asyncio.CancelledError:
            return

        if exception:
            log.error(
                "Giveaway task %s crashte: %r",
                message_id,
                exception,
            )

    async def process_one(
        self,
        message_id: int,
    ):
        async with self.semaphore:
            row = await self.db.get_giveaway(
                message_id
            )

            if row is None:
                return

            if row["status"] == "ACTIVE":
                await self.process_expired(
                    message_id
                )

            elif row["status"] == "PROCESSING_RESULT":
                await self.send_result(
                    message_id
                )

    # ============================================================
    # WINNAARS
    # ============================================================

    async def process_expired(
        self,
        message_id: int,
    ):
        processing_token = new_token(
            "processing"
        )

        claimed = (
            await self.db.claim_processing(
                message_id,
                processing_token,
            )
        )

        if not claimed:
            return

        try:
            async with asyncio.timeout(
                PROCESSING_TIMEOUT
            ):
                participants = (
                    await self.db.get_participants(
                        message_id
                    )
                )

                row = await self.db.get_giveaway(
                    message_id
                )

                if row is None:
                    return

                participant_count = len(
                    participants
                )

                await self.db.reconcile_participant_count(
                    message_id
                )

                winner_count = min(
                    int(row["winner_count"]),
                    participant_count,
                )

                if winner_count > 0:
                    winners = (
                        secrets.SystemRandom().sample(
                            participants,
                            winner_count,
                        )
                    )
                else:
                    winners = []

                saved = (
                    await self.db.save_processing_result(
                        message_id,
                        processing_token,
                        winners,
                        participant_count,
                    )
                )

                if not saved:
                    log.warning(
                        "Processing lease verloren voor %s.",
                        message_id,
                    )

                    return

        except asyncio.CancelledError:
            raise

        except asyncio.TimeoutError:
            await self.db.fail_processing(
                message_id,
                processing_token,
                "PROCESSING_TIMEOUT",
                "Winner selection timed out.",
            )

            return

        except Exception as exc:
            await self.db.fail_processing(
                message_id,
                processing_token,
                "PROCESSING_ERROR",
                repr(exc),
            )

            log.exception(
                "Winner processing failed voor %s.",
                message_id,
            )

            return

        await self.send_result(
            message_id
        )

    # ============================================================
    # RESULTAAT
    # ============================================================

    async def send_result(
        self,
        message_id: int,
    ):
        owner_token = new_token(
            "result"
        )

        claimed = (
            await self.db.claim_result(
                message_id,
                owner_token,
                RESULT_LEASE_TIMEOUT,
            )
        )

        if not claimed:
            return

        row = await self.db.get_giveaway(
            message_id
        )

        if row is None:
            return

        try:
            winners = json.loads(
                row["result_winners"]
                or "[]"
            )

            self.validate_winners(
                winners,
                int(row["winner_count"]),
            )

            channel = self.bot.get_channel(
                int(row["channel_id"])
            )

            if channel is None:
                channel = await self.bot.fetch_channel(
                    int(row["channel_id"])
                )

            existing = (
                await self.find_existing_result(
                    channel,
                    message_id,
                )
            )

            if existing is not None:
                result_message_id = (
                    existing.id
                )

            else:
                if winners:
                    mentions = ", ".join(
                        f"<@{user_id}>"
                        for user_id in winners
                    )

                    description = (
                        f"🎉 Gefeliciteerd {mentions}!\n\n"
                        f"Jullie hebben **{row['prize']}** "
                        "gewonnen!"
                    )
                else:
                    description = (
                        f"😢 De giveaway voor "
                        f"**{row['prize']}** is geëindigd, "
                        "maar er waren geen geldige deelnemers."
                    )

                embed = discord.Embed(
                    title="🎉 Giveaway afgelopen",
                    description=description,
                )

                embed.set_footer(
                    text=(
                        f"giveaway-result:"
                        f"{message_id}"
                    )
                )

                message = await channel.send(
                    embed=embed
                )

                result_message_id = message.id

            completed = (
                await self.db.complete_result(
                    message_id,
                    owner_token,
                    result_message_id,
                )
            )

            if not completed:
                log.warning(
                    "Resultaat %s verzonden, "
                    "maar DB completion-fence faalde.",
                    message_id,
                )

                return

            await self.disable_giveaway(
                message_id
            )

        except asyncio.CancelledError:
            raise

        except discord.NotFound as exc:
            await self.handle_error(
                message_id,
                owner_token,
                "NOT_FOUND",
                str(exc),
                permanent=True,
            )

        except discord.Forbidden as exc:
            await self.handle_error(
                message_id,
                owner_token,
                "FORBIDDEN",
                str(exc),
                permanent=True,
            )

        except discord.HTTPException as exc:
            retry_after = getattr(
                exc,
                "retry_after",
                None,
            )

            status = getattr(
                exc,
                "status",
                0,
            )

            if retry_after is not None:
                await self.handle_error(
                    message_id,
                    owner_token,
                    "RATE_LIMIT",
                    str(exc),
                    permanent=False,
                    delay=float(
                        retry_after
                    ),
                )

            elif 500 <= status < 600:
                await self.handle_error(
                    message_id,
                    owner_token,
                    "DISCORD_5XX",
                    str(exc),
                    permanent=False,
                )

            else:
                await self.handle_error(
                    message_id,
                    owner_token,
                    "DISCORD_HTTP",
                    str(exc),
                    permanent=True,
                )

        except Exception as exc:
            await self.handle_error(
                message_id,
                owner_token,
                "RESULT_ERROR",
                repr(exc),
                permanent=False,
            )

            log.exception(
                "Resultaat verzenden mislukt voor %s.",
                message_id,
            )

    @staticmethod
    def validate_winners(
        winners,
        winner_count: int,
    ):
        if not isinstance(
            winners,
            list,
        ):
            raise ValueError(
                "Winners must be a list."
            )

        if len(winners) > winner_count:
            raise ValueError(
                "Too many winners."
            )

        if len(winners) != len(
            set(winners)
        ):
            raise ValueError(
                "Duplicate winners."
            )

        for user_id in winners:
            if (
                isinstance(user_id, bool)
                or not isinstance(
                    user_id,
                    int,
                )
            ):
                raise ValueError(
                    "Invalid winner ID."
                )

    async def handle_error(
        self,
        message_id: int,
        owner_token: str,
        error_code: str,
        error_message: str,
        *,
        permanent: bool,
        delay: float | None = None,
    ):
        row = await self.db.get_giveaway(
            message_id
        )

        if row is None:
            return

        retry_count = (
            int(row["retry_count"])
            + 1
        )

        if retry_count >= MAX_RESULT_RETRIES:
            permanent = True

        if delay is None:
            delay = (
                min(
                    300,
                    2 ** min(
                        retry_count,
                        8,
                    ),
                )
                + random.uniform(
                    0,
                    2,
                )
            )

        await self.db.result_failed(
            message_id,
            owner_token,
            retry_count,
            time.time() + delay,
            error_code,
            error_message,
            permanent,
        )

    # ============================================================
    # IDEMPOTENCY
    # ============================================================

    async def find_existing_result(
        self,
        channel,
        message_id: int,
    ):
        marker = (
            f"giveaway-result:{message_id}"
        )

        async for message in channel.history(
            limit=100
        ):
            if (
                self.bot.user is not None
                and message.author.id
                != self.bot.user.id
            ):
                continue

            for embed in message.embeds:
                if (
                    embed.footer
                    and embed.footer.text
                    == marker
                ):
                    return message

        return None

    # ============================================================
    # UI
    # ============================================================

    async def disable_giveaway(
        self,
        message_id: int,
    ):
        row = await self.db.get_giveaway(
            message_id
        )

        if row is None:
            return

        try:
            channel = self.bot.get_channel(
                int(row["channel_id"])
            )

            if channel is None:
                channel = await self.bot.fetch_channel(
                    int(row["channel_id"])
                )

            message = await channel.fetch_message(
                message_id
            )

            await message.edit(
                view=self.bot.create_giveaway_view(
                    message_id,
                    disabled=True,
                )
            )

        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
        ):
            log.warning(
                "Kon UI van giveaway %s niet uitschakelen.",
                message_id,
            )

    # ============================================================
    # WATCHDOG
    # ============================================================

    async def watchdog_loop(self):
        while not self.stop_event.is_set():
            try:
                cutoff = (
                    time.time()
                    - PROCESSING_TIMEOUT
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
                        self.active_tasks.get(
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

                result_cutoff = (
                    time.time()
                    - RESULT_LEASE_TIMEOUT
                )

                results = (
                    await self.db.stale_result_leases(
                        result_cutoff
                    )
                )

                for row in results:
                    await self.db.recover_result_lease(
                        int(row["message_id"]),
                        row[
                            "result_send_owner_token"
                        ],
                    )

                await asyncio.sleep(
                    WATCHDOG_INTERVAL
                )

            except asyncio.CancelledError:
                raise

            except Exception:
                log.exception(
                    "Fout in giveaway watchdog."
                )

                await asyncio.sleep(5)
