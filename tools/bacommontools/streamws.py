# Released under the MIT License. See LICENSE for details.
#
"""WebSocket-based stream consumer for bacloud (Phase 2).

A stream-mode bacloud kickoff lands at a basn node, which injects a
``StreamWS`` into the response pointing at its own
``/streamcall/<call_id>`` WebSocket endpoint. We open that WS, print
``StreamOutput`` frames live as they arrive, and return the terminal
``StreamFinal`` so the caller can splice it back into bacloud's
existing response-handling flow.

WS failures (handshake reject, expired token, mid-stream drop,
unparseable frame) surface as :class:`~efro.error.CleanError`. There
is no HTTP-polling fallback — once a kickoff response carries
``stream_ws`` we commit to the WS path. Bamaster's HTTP-polling
path stays in place for kickoffs that *don't* get a ``stream_ws``
injected (i.e. requests bypassing basn or hitting older basn nodes).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from efro.error import CleanError
from efro.dataclassio import dataclass_from_json
from bacommon.bacloud import (
    BACLOUD_VERSION,
    ResponseData,
    StreamFinal,
    StreamFrame,
    StreamOutput,
)

if TYPE_CHECKING:
    from bacommon.bacloud import StreamWS


def consume_via_ws(
    response: ResponseData, *, bearer: str | None
) -> ResponseData:
    """Drain a stream over WebSocket and return a terminal-only response.

    The returned ``ResponseData`` carries the terminal ``StreamFinal``
    in ``stream_frames`` so bacloud's existing ``stream_frames`` loop
    falls through to the usual terminal handling
    (message/error/end_command).

    Caller must check ``response.stream_ws is not None`` first.
    Raises :class:`~efro.error.CleanError` on any WS failure.
    """
    assert response.stream_ws is not None
    terminal = asyncio.run(_consume(response.stream_ws, bearer))
    return ResponseData(stream_frames=[terminal])


async def _consume(sw: StreamWS, bearer: str | None) -> StreamFinal:
    """Open the WS, print outputs, return the terminal frame."""
    import websockets
    from websockets.exceptions import (
        ConnectionClosed,
        InvalidStatus,
        WebSocketException,
    )

    headers: list[tuple[str, str]] = [('X-WS-Token', sw.ws_token)]
    if bearer is not None:
        headers.append(('Authorization', f'Bearer {bearer}'))
    headers.append(('User-Agent', f'bacloud/{BACLOUD_VERSION}'))

    try:
        async with websockets.connect(
            sw.basn_url, additional_headers=headers
        ) as ws:
            async for raw in ws:
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8')
                frame = dataclass_from_json(StreamFrame, raw)
                if isinstance(frame, StreamOutput):
                    print(frame.text, end='', flush=True)
                elif isinstance(frame, StreamFinal):
                    return frame
            raise CleanError('Stream WS closed without terminal frame.')
    except InvalidStatus as exc:
        raise CleanError(f'Stream WS handshake rejected: {exc}') from exc
    except ConnectionClosed as exc:
        # 4001 = token expired (refresh endpoint is a future Phase 2
        # extension; for now we surface a CleanError). 4002/4003/4004
        # are token problems; treat as fatal.
        raise CleanError(
            f'Stream WS closed mid-stream: '
            f'code={exc.code} reason={exc.reason!r}'
        ) from exc
    except WebSocketException as exc:
        raise CleanError(f'Stream WS error: {exc}') from exc
    except OSError as exc:
        raise CleanError(f'Stream WS connect failed: {exc}') from exc
