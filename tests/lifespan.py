"""Keep AnyIO transport lifespan entry/exit in one task across pytest fixtures."""

import asyncio
from contextlib import asynccontextmanager


@asynccontextmanager
async def running_app(app):
    ready = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    async def serve():
        try:
            async with app.router.lifespan_context(app):
                ready.set_result(None)
                await stop.wait()
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
            raise

    task = asyncio.create_task(serve())
    try:
        await ready
        yield app
    finally:
        stop.set()
        await task
