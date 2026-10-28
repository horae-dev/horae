# SPDX-License-Identifier: Apache-2.0
"""Bounded capture of a plugin's standard error, redacted on bytes before decoding (#24).

A reader task drains the pipe continuously (a full pipe would block the plugin) into a
**head + tail** window: the first `HEAD_BYTES` (16 KiB) and the last `TAIL_BYTES` (48 KiB less
`ROOM`, 256 bytes left for the elision marker and redaction tokens, so that the text fits the
64 KiB record limit), plus a count of the bytes dropped in between. Memory stays bounded
however much is written.

**Cut points cannot leak a secret.** Each window keeps `margin` extra bytes at its cut edge
(the head its first `HEAD_BYTES + margin` bytes, the tail its last `TAIL_BYTES + margin`), where
`margin` is at least the longest redaction pattern minus one. Redaction runs on those bytes;
the head then keeps the bytes before its cut and the whole of any match that starts before it,
the tail the bytes after its cut and the whole of any match that ends after it. A secret
straddling a cut is therefore always seen whole, and never shows as a partial prefix or
suffix. When nothing was dropped, everything is redacted at once and cut afterwards.

The text is decoded with `errors="replace"` and is at most `MAX_TEXT` (65 536) characters, the
`StepRecord.stderr` limit.
"""

from collections.abc import Callable

from horae.plugins.host.redact import Redactor, token

HEAD_BYTES = 16 * 1024
ROOM = 256
TAIL_BYTES = 48 * 1024 - ROOM
MAX_TEXT = 65536
_MARKER = "\n…[{} bytes elided]…\n"


def _head(data: bytes, cut: int, redactor: Redactor) -> str:
    out: list[bytes] = []
    position = 0
    for start, end, name in redactor.spans(data):
        if start >= cut:
            break
        out.append(data[position:start])
        out.append(token(name).encode())
        position = end
    if position < cut:
        out.append(data[position:cut])
    return b"".join(out).decode("utf-8", errors="replace")


def _tail(data: bytes, cut: int, redactor: Redactor) -> str:
    out: list[bytes] = []
    position = cut
    for start, end, name in redactor.spans(data):
        if end <= cut:
            continue
        out.append(data[position:start] if start > position else b"")
        out.append(token(name).encode())
        position = max(position, end)
    out.append(data[position:])
    return b"".join(out).decode("utf-8", errors="replace")


def _fit(text: str, limit: int = MAX_TEXT) -> str:
    """`text` cut to `limit` characters, keeping a quarter of head and the rest of tail."""
    if len(text) <= limit:
        return text
    marker = _MARKER.format("some")
    keep = limit - len(marker)
    head = keep // 4
    return text[:head] + marker + text[len(text) - (keep - head) :]


class StderrCapture:
    """The head and tail of one window of a plugin's stderr (`take` starts a new window)."""

    def __init__(self, margin: int, *, head: int = HEAD_BYTES, tail: int = TAIL_BYTES) -> None:
        self.margin = max(0, margin)
        self._head_size = head
        self._tail_size = tail
        self._head = bytearray()
        self._tail = bytearray()
        self._dropped = 0
        self.listeners: list[Callable[[], None]] = []
        """Called after each `feed` (the host uses it to know when the pipe went quiet)."""

    def feed(self, data: bytes) -> None:
        room = self._head_size + self.margin - len(self._head)
        if room > 0:
            self._head += data[:room]
            data = data[room:]
        if data:
            self._tail += data
            excess = len(self._tail) - (self._tail_size + self.margin)
            if excess > 0:
                del self._tail[:excess]
                self._dropped += excess
        for listener in self.listeners:
            listener()

    def take(self, redactor: Redactor) -> str | None:
        """The redacted text of the window so far (`None` if empty); the window restarts."""
        head, tail, dropped = bytes(self._head), bytes(self._tail), self._dropped
        self._head, self._tail, self._dropped = bytearray(), bytearray(), 0
        if not head:
            return None
        if redactor.max_len - 1 > self.margin:  # pragma: no cover (the host sizes the margin)
            raise ValueError("the capture margin is smaller than the longest pattern")
        if not dropped:
            text = redactor.redact_bytes(head + tail).decode("utf-8", errors="replace")
            return _fit(text)
        first = _head(head, self._head_size, redactor)
        last = _tail(tail, len(tail) - self._tail_size, redactor)
        elided = dropped + (len(head) - self._head_size) + (len(tail) - self._tail_size)
        return _fit(first + _MARKER.format(elided) + last)


__all__ = ["HEAD_BYTES", "MAX_TEXT", "TAIL_BYTES", "StderrCapture"]
