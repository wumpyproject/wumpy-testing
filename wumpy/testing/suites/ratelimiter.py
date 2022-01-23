import sys
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Tuple

import anyio
import pytest
from wumpy.rest import Ratelimiter, Route

__all__ = ('RatelimiterSuite',)


class RatelimiterSuite:
    """Test suite for ensuring the ratelimiter works.

    All tests that test by time use the duration it took to enter the limiter
    as the unit multiplied to see if it takes longer than usual (a request
    is being limited).

    Simply subclass this class and override `get_impl()`. All tests are marked
    with `@pytest.mark.anyio` so define the `async_backend` fixture if your
    implementation only works with one backend.

    `DELTA_DURATION` can be set to change how many seconds in the future that
    the ratelimit resets in the tests.
    """

    def get_impl(self) -> Ratelimiter:
        raise NotImplementedError()

    DELTA_DURATION = 5

    @pytest.mark.anyio
    async def test_no_headers(self):
        impl = self.get_impl()

        # While it doesn't look like it tests much, it's a distinct test for
        # TypeErrors if the ratelimiter doesn't follow the typing correctly.
        async with impl as limiter:
            async with limiter(Route('GET', '/gateway')) as update:
                await update({})

    async def measure_ratelimiting(self, first: Route, second: Route) -> bool:
        """Make two ratelimited requests.

        Parameters:
            first: The first Route to make a request to.
            second: The second Route to make a request to make.

        Returns:
            Whether the second request was ratelimited.
        """
        impl = self.get_impl()

        async with impl as limiter:

            proxy = limiter(first)

            # This is the reference request, which we base the timing for.
            start = perf_counter()
            async with proxy as update:
                started = perf_counter() - start

                delta = timedelta(seconds=self.DELTA_DURATION)
                now = datetime.now(tz=timezone.utc)

                await update({
                    'X-RateLimit-Limit': '1',
                    'X-RateLimit-Remaining': '0',
                    'X-RateLimit-Reset': str((now + delta).timestamp()),
                    'X-RateLimit-Reset-After': str(delta.total_seconds()),
                })

            # With half the time it took an unratelimited access as a margin,
            # finally make the underlying test:
            proxy = limiter(second)
            with anyio.move_on_after(started * 1.5) as scope:
                update = await proxy.__aenter__()

            if scope.cancel_called:
                # If __aenter__() was cancelled, and an error was raised in it
                # then it failed and if this was used as 'async with' then the
                # body, or __aexit__() would not run. We need to exit early.
                return True

            await update({
                'X-RateLimit-Limit': '1',
                'X-RateLimit-Remaining': '0',
                'X-RateLimit-Reset': str((now + delta).timestamp()),
                'X-RateLimit-Reset-After': str(delta.total_seconds()),
                'X-RateLimit-Scope': 'user',
            })

            await proxy.__aexit__(*sys.exc_info())

            return scope.cancel_called

    @pytest.mark.anyio
    async def test_method_different_endpoint(self) -> None:
        result = await self.measure_ratelimiting(
            Route('GET', '/users/@me'),
            Route('PATCH', '/users/@me')
        )
        assert result is False

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ['first', 'second'],
        [
            # Same endpoint - Same major parameter
            (
                Route('GET', '/channels/{channel_id}', channel_id=41771983423143937),
                Route('GET', '/channels/{channel_id}', channel_id=41771983423143937),
            ),

            # Same endpoint - Different major parameter
            (
                Route('POST', '/channels/{channel_id}', channel_id=41771983423143937),
                Route('POST', '/channels/{channel_id}', channel_id=155101607195836416),
            ),

            # Different endpoint - Same major parameter
            (
                Route('GET', '/guilds/{guild_id}/bans', guild_id=197038439483310086),
                Route('GET', '/guilds/{guild_id}/roles', guild_id=197038439483310086),
            ),

            # Different endpoint - Different major parameter
            (
                Route('GET', '/webhooks/{webhook_id}', webhook_id=752831914402115456),
                Route('DELETE', '/webhooks/{webhook_id}', webhook_id=752831914402115456),
            ),
        ]
    )
    async def test_major_param(
        self,
        first: Route,
        second: Route,
    ) -> None:
        result = await self.measure_ratelimiting(first, second)

        expected = (
            first.endpoint == second.endpoint and
            first.major_params == second.major_params
        )
        assert result is expected

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ['first', 'second', 'third'],
        [
            # Same bucket - Same endpoint - Same major parameter
            # Same bucket - Same endpoint - Different major parameter
            # Same bucket - Different endpoint - Same major parameter
            # Same bucket - Different endpoint - Different major parameter
            # Different bucket - Same endpoint - Same major parameter
            # Different bucket - Same endpoint - Different major parameter
            # Different bucket - Different endpoint - Same major parameter
            # Different bucket - Different endpoint - Different major parameter
        ]
    )
    async def test_ratelimiter_bucket(
        self,
        first: Tuple[Route, str],
        second: Tuple[Route, str],
        third: Tuple[Route, str],
        result: bool
    ) -> None:
        """Make three ratelimited requests as a test for X-RateLimit-Bucket.

        Although similar, this is purposefully separated from the other
        ratelimiter test.

        Parameters:
            first: The first request to make.
            second: The second request to make.
            third: The third request to make.
            result: Whether the third request should have been ratelimited.

        Returns:
            Whether the third request took longer than it should have -
            indicating that it was ratelimited.
        """
        impl = self.get_impl()

        async with impl as limiter:

            proxy = limiter(first[0])

            # This is the reference request, which we base the timing for.
            start = perf_counter()
            async with proxy as update:
                started = perf_counter() - start

                delta = timedelta(seconds=self.DELTA_DURATION)
                now = datetime.now(tz=timezone.utc)

                await update({
                    'X-RateLimit-Limit': '2',
                    'X-RateLimit-Remaining': '1',
                    'X-RateLimit-Reset': str((now + delta).timestamp()),
                    'X-RateLimit-Reset-After': str(delta.total_seconds()),
                    'X-RateLimit-Bucket': first[1]
                })

            # If we're able to get more accurate timings then we might as well.
            start = perf_counter()
            async with limiter(second[0]) as update:
                started = (started + perf_counter() - start) / 2

                await update({
                    'X-RateLimit-Limit': '2',
                    'X-RateLimit-Remaining': '0',
                    'X-RateLimit-Reset': str((now + delta).timestamp()),
                    'X-RateLimit-Reset-After': str(delta.total_seconds()),
                    'X-RateLimit-Bucket': second[1]
                })

            # With half the time it took an unratelimited access as a margin,
            # finally make the underlying test:
            proxy = limiter(third[0])
            with anyio.move_on_after(started * 1.5) as scope:
                update = await proxy.__aenter__()

            if scope.cancel_called:
                # Similar to the non-bucket tests, we need to follow the
                # expected behavior of asynchronous context managers and exit
                # early if __aenter__() was cancelled.
                assert scope.cancel_called is result
                return

            await update({
                'X-RateLimit-Limit': '1',
                'X-RateLimit-Remaining': '0',
                'X-RateLimit-Reset': str((now + delta).timestamp()),
                'X-RateLimit-Reset-After': str(delta.total_seconds()),
                'X-RateLimit-Scope': 'user',
                'X-RateLimit-Bucket': third[1]
            })

            await proxy.__aexit__(*sys.exc_info())

            assert scope.cancel_called is result
