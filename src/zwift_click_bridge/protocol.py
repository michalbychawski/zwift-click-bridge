"""
Minimal protobuf-"lite" varint decoder for Zwift Click button frames.
field 1 (tag 0x08) of a BUTTON_OPCODE frame is a bitmask, 0 = pressed per bit.
"""
from __future__ import annotations

from . import constants


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = data[offset]
        offset += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            break
        shift += 7
    return result, offset


def decode_button_bitmask(payload: bytes) -> int | None:
    """Returns the field-1 bitmask, or None if this isn't a button frame."""
    if not payload or payload[0] != constants.BUTTON_OPCODE:
        return None

    offset = 1
    while offset < len(payload):
        tag, offset = _read_varint(payload, offset)
        field_number, wire_type = tag >> 3, tag & 0x7
        if wire_type != 0:
            return None  # unexpected -- not the frame shape we've seen
        value, offset = _read_varint(payload, offset)
        if field_number == 1:
            return value
    return None
