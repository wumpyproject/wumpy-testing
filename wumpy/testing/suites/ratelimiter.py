from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Tuple

import anyio
import pytest
from wumpy.rest import Ratelimiter, Route, ServerException


class RatelimiterSuite:
    """Test suite for ensuring the ratelimiter works.

    All tests that test by time use the duration it took to enter the limiter
    as the unit multiplied to see if it takes longer than usual (a request
    is being limited).

    Simply subclass this class and override `get_impl()`. All tests are marked
    with `@pytest.mark.anyio` so define the `async_backend` fixture if your
    implementation only works with one backend.
    """
    def get_impl(self) -> Ratelimiter:
        raise NotImplementedError()

    @pytest.mark.anyio
    async def test_no_headers(self):
        impl = self.get_impl()

        # While it doesn't look like it tests much, it's a distinct test for
        # TypeErrors if the ratelimiter doesn't follow the typing correctly.
        async with impl as limiter:
            async with limiter(Route('GET', '/gateway')) as update:
                await update({})

    async def run_two_requests(
        self,
        first: Tuple[Route, dict],
        second: Tuple[Route, dict]
    ) -> bool:
        """Make two ratelimited requests as a test.

        The two parameters are tuples of (route, response headers). The second
        request will be called with `move_on_after()` as a way to detect
        whether it was limited by the ratelimiter.

        Parameters:
            first: The first request to make.
            second: The second request to make.

        Returns:
            Whether the second request was limited.
        """
        impl = self.get_impl()

        async with impl as limiter:

            # We use the time it takes to enter the limiter as the unit of time
            # later on. That way tests only really take as much time as they
            # need to. This implementation somewhat depends on the fact that
            # exciting the limiter takes the same (if not less) amount of time.
            start = perf_counter()
            async with limiter(first[0]) as update:
                duration = perf_counter() - start

                delta = timedelta(seconds=duration * 6)
                now = datetime.now(tz=timezone.utc)

                await update({
                    'X-RateLimit-Limit': '1',
                    'X-RateLimit-Remaining': '0',
                    'X-RateLimit-Reset': str(now + delta),
                    'X-RateLimit-Reset-After': str(delta.total_seconds()),
                    **first[1]
                })

            with anyio.move_on_after(duration * 3) as scope:
                async with limiter(second[0]) as update:
                    await update({
                        'X-RateLimit-Limit': '1',
                        'X-RateLimit-Remaining': '0',
                        'X-RateLimit-Reset': str(now + delta),
                        'X-RateLimit-Reset-After': str(delta.total_seconds()),
                        'X-RateLimit-Scope': 'user',
                        **second[1]
                    })

            return scope.cancel_called

    @pytest.mark.anyio
    async def test_no_more(self):
        impl = self.get_impl()

        async with impl as limiter:

            start = perf_counter()
            async with limiter(Route('GET', '/gateway')) as update:
                duration = perf_counter() - start

                delta = timedelta(seconds=duration * 6)
                now = datetime.now(tz=timezone.utc)
                await update({
                    'X-RateLimit-Limit': '1',
                    'X-RateLimit-Remaining': '0',
                    'X-RateLimit-Reset': str((now + delta).timestamp()),
                    'X-RateLimit-Reset-After': str(delta.total_seconds()),
                })

            # It is also possible to assert how long it took inside of the
            # context manager but that slows down tests considerably.
            with anyio.move_on_after(duration * 2) as scope:
                async with limiter(Route('GET', '/gateway')) as update:
                    await update({
                        'X-RateLimit-Limit': '1',
                        'X-RateLimit-Remaining': '0',
                        'X-RateLimit-Reset': str((now + delta).timestamp()),
                        'X-RateLimit-Reset-After': str(delta.total_seconds()),
                        'X-RateLimit-Scope': 'user',  # Mimic a 429 response
                    })

            # The ratelimiter should have limited the request, which would take
            # more time than usually - meaning it got cancelled.
            assert scope.cancel_called, (
                "Ratelimiter did not limit request to same endpoint with no remaining"
                " requests left"
            )

    @pytest.mark.anyio
    async def test_same_endpoint_major_params(self):
        impl = self.get_impl()

        async with impl as limiter:
            route = Route(
                'POST', '/channels/{channel_id}/messages', channel_id=41771983423143937
            )
            start = perf_counter()
            async with limiter(route) as update:
                duration = perf_counter() - start

                delta = timedelta(seconds=duration * 6)
                now = datetime.now(tz=timezone.utc)
                await update({
                    'X-RateLimit-Limit': '1',
                    'X-RateLimit-Remaining': '0',
                    'X-RateLimit-Reset': str((now + delta).timestamp()),
                    'X-RateLimit-Reset-After': str(delta.total_seconds()),
                })

            route = Route(
                'POST', '/channels/{channel_id}/messages', channel_id=155101607195836416
            )
            with anyio.move_on_after(duration * 2) as scope:
                async with limiter(route) as update:

                    await update({
                        'X-RateLimit-Limit': '1',
                        'X-RateLimit-Remaining': '0',
                        'X-RateLimit-Reset': str((now + delta).timestamp()),
                        'X-RateLimit-Reset-After': str(delta.total_seconds()),
                        'X-RateLimit-Scope': 'user',  # Mimic a 429 response
                    })

            # These two requests share the same bucket and same endpoint
            # but have different major parameters - meaning this request
            # should've not been ratelimited.
            assert not scope.cancel_called, (
                "Ratelimiter limited request to the same endpoint but with differing"
                " major parameters (channel_id)"
            )

    @pytest.mark.anyio
    async def test_same_bucket_major_params(self):
        impl = self.get_impl()

        CHNL_ID = 41771983423143937
        async with impl as limiter:
            route = Route(
                'GET', '/channels/{channel_id}', channel_id=CHNL_ID
            )
            start = perf_counter()
            async with limiter(route) as update:
                duration = perf_counter() - start

                delta = timedelta(seconds=duration * 8)
                now = datetime.now(tz=timezone.utc)

                await update({
                    'X-RateLimit-Remaining': '1',
                    'X-RateLimit-Bucket': 'abc123xyz789abc123xyz789',
                    'X-RateLimit-Reset': str((now + delta).timestamp()),
                    'X-RateLimit-Reset-After': str(delta.total_seconds()),
                })

            route = Route(
                'POST', '/channels/{channel_id}/invites', channel_id=CHNL_ID
            )
            async with limiter(route) as update:
                await update({
                    'X-RateLimit-Remaining': '0',
                    'X-RateLimit-Bucket': 'abc123xyz789abc123xyz789',
                    'X-RateLimit-Reset': str((now + delta).timestamp()),
                    'X-RateLimit-Reset-After': str(delta.total_seconds()),
                })

            route = Route(
                'GET', '/channels/{channel_id}', channel_id=CHNL_ID
            )
            with anyio.move_on_after(duration * 2) as scope:
                async with limiter(route) as update:
                    await update({
                        'X-RateLimit-Remaining': '0',
                        'X-RateLimit-Bucket': 'abc123xyz789abc123xyz789',
                        'X-RateLimit-Reset': str((now + delta).timestamp()),
                        'X-RateLimit-Reset-After': str(delta.total_seconds()),
                        'X-RateLimit-Scope': 'user',  # Mimic a 429 response
                    })

            assert scope.cancel_called, (
                "Ratelimiter did not limit request to different endpoint but same bucket with"
                " same major parameters (channel_id)"
            )

# 11 tests:
# SB, SE, SM
# DB, SE, SM - SB, DE, SM - SB, SE, DM
# DB, DE, SM - SB, DE, DM - DB, SE, DM
# DB, DE, DM

# SE, SM
# DE, SM - SE, DM
