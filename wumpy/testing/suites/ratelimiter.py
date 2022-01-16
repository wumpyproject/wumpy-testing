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
            start = perf_counter()

            # This is the reference request, which we base the timing for.
            update = await proxy.__aenter__()
            started = perf_counter() - start

            delta = timedelta(seconds=self.DELTA_DURATION)
            now = datetime.now(tz=timezone.utc)

            await update({
                'X-RateLimit-Limit': '1',
                'X-RateLimit-Remaining': '0',
                'X-RateLimit-Reset': str((now + delta).timestamp()),
                'X-RateLimit-Reset-After': str(delta.total_seconds()),
            })

            await proxy.__aexit__(*sys.exc_info())

            # With half the time it took an unratelimited access as a margin,
            # finally make the underlying test:
            proxy = limiter(second)
            with anyio.move_on_after(started * 1.5) as scope:
                update = await proxy.__aenter__()

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

    async def test_ratelimiter_bucket(
        self,
        first: Tuple[Route, dict],
        second: Tuple[Route, dict],
        third: Tuple[Route, dict],
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

            # This is the reference request, which we base the timing for.
            start = perf_counter()
            async with limiter(first[0]) as update:
                delta = timedelta(seconds=self.DELTA_DURATION)
                now = datetime.now(tz=timezone.utc)

                await update({
                    'X-RateLimit-Limit': '2',
                    'X-RateLimit-Remaining': '1',
                    'X-RateLimit-Reset': str((now + delta).timestamp()),
                    'X-RateLimit-Reset-After': str(delta.total_seconds()),
                    **first[1]
                })
            duration = perf_counter() - start

            # With half the time it took an unratelimited access as a margin,
            # make the underlying test. Because we don't know when the
            # ratelimiter decides to block and wait we need to make one request
            # that exhausts the ratelimit and one that will exceed it.
            with anyio.move_on_after(duration * 2.5) as scope:
                async with limiter(second[0]) as update:
                    await update({
                        'X-RateLimit-Limit': '2',
                        'X-RateLimit-Remaining': '0',
                        'X-RateLimit-Reset': str((now + delta).timestamp()),
                        'X-RateLimit-Reset-After': str(delta.total_seconds()),
                        **second[1]
                    })

                async with limiter(third[0]) as update:
                    await update({
                        'X-RateLimit-Limit': '1',
                        'X-RateLimit-Remaining': '0',
                        'X-RateLimit-Reset': str((now + delta).timestamp()),
                        'X-RateLimit-Reset-After': str(delta.total_seconds()),
                        'X-RateLimit-Scope': 'user',
                        **third[1]
                    })

            assert scope.cancel_called is result
