from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Seconds may exceed a minute only when written as a bare count; every component
# of a clock form except the leading hour is bounded, so "1:70" is a typo rather
# than 130 seconds.
_SECONDS = re.compile(r"^\d+$")
_MINUTES_SECONDS = re.compile(r"^(\d{1,2}):(\d{1,2})$")
_HOURS_MINUTES_SECONDS = re.compile(r"^(\d+):(\d{1,2}):(\d{1,2})$")


@dataclass(frozen=True)
class SeekRequest:
    seconds: int
    relative: bool


def parse_seek_request(text: str) -> Optional[SeekRequest]:
    # The sign is the only thing separating a shift from a destination, so it is
    # stripped before the clock forms are read and reapplied afterwards.
    text = text.strip()
    relative = text.startswith(("+", "-"))
    seconds = _parse_clock(text[1:] if relative else text)
    if seconds is None:
        return None
    if relative and text.startswith("-"):
        seconds = -seconds
    return SeekRequest(seconds, relative=relative)


def _parse_clock(text: str) -> Optional[int]:
    if _SECONDS.match(text):
        return int(text)
    match = _MINUTES_SECONDS.match(text)
    if match:
        minutes, seconds = (int(part) for part in match.groups())
        return None if minutes > 59 or seconds > 59 else minutes * 60 + seconds
    match = _HOURS_MINUTES_SECONDS.match(text)
    if match:
        hours, minutes, seconds = (int(part) for part in match.groups())
        return None if minutes > 59 or seconds > 59 else hours * 3600 + minutes * 60 + seconds
    return None
