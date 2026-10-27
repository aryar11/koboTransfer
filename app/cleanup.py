import asyncio
import logging

from .rooms import RoomStore

log = logging.getLogger("kobosend.cleanup")


async def sweep_forever(store: RoomStore, interval_seconds: int) -> None:
    while True:
        try:
            removed = await asyncio.to_thread(store.sweep_expired)
            if removed:
                log.info("Removed %d expired room(s)", removed)
        except Exception:
            log.exception("Cleanup sweep failed")
        await asyncio.sleep(interval_seconds)
