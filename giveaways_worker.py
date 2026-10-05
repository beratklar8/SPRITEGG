                try:
                    winners = await select_giveaway_winners(db_controller, giveaway_id, giveaway["winner_count"])

                    # Idempotentie check: als het resultaat er al is, niet opnieuw sturen
                    if existing_result_msg_id > 0:
                        logger.info(f"Resultaatbericht voor giveaway {giveaway_id} was al verzonden (ID: {existing_result_msg_id}). Afronden...")
                    else:
                        if winners:
                            winner_mentions = ", ".join([f"<@{w}>" for w in winners])
                            result_text = f"🎉 Gefeliciteerd {winner_mentions}! Jij hebt **{giveaway['prize']}** gewonnen!"
                        else:
                            result_text = f"😢 De giveaway voor **{giveaway['prize']}** is geëindigd, maar er waren geen geldige deelnemers."

                        sent_result_msg = await channel.send(result_text)
                        
                        # Sla ID direct op in de DB *voordat* we op COMPLETED zetten
                        await db_controller.set_result_pending(giveaway_id, worker_token, sent_result_msg.id)

                    # Zet definitief op COMPLETED
                    success = await db_controller.finalize_giveaway(giveaway_id, worker_token)
                    if not success:
                        logger.critical(f"Kon giveaway {giveaway_id} niet voltooien (token conflict)! Resultaat staat wel op Discord.")

                finally:
                    # Netjes annuleren en wachten tot de heartbeat taak is gestopt
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass
