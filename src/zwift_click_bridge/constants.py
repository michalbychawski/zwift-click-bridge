"""
GATT layout and session protocol for Zwift Click v2, confirmed empirically
against real hardware (see sniff.py). This is simpler than Zwift Play's
protocol: a Click that has already been unlocked by the real Zwift app at
least once persists that unlock on-device, so no ECDH handshake is needed --
just a short activation write followed by a periodic keepalive. An unlocked
device's SYNC_TX reply starts with 0x52 ('R' of "RideOn"); a never-unlocked
device would instead send a 0xFF-prefixed crypto challenge (not handled here).
"""

SERVICE_UUID = "00000001-19ca-4651-86e5-fa29dcdd09d1"
CHAR_ASYNC_NOTIFY = "00000002-19ca-4651-86e5-fa29dcdd09d1"      # device -> host, button bitmask notifications
CHAR_SYNC_RX_WRITE = "00000003-19ca-4651-86e5-fa29dcdd09d1"     # host -> device, activation + keepalive writes
CHAR_SYNC_TX_INDICATE = "00000004-19ca-4651-86e5-fa29dcdd09d1"  # device -> host, activation ack

RIDE_ON = b"RideOn"

# Sent once, in order, right after connecting (write-without-response).
ACTIVATION_WRITES = [
    RIDE_ON + bytes([0x02, 0x03]),
    bytes([0x00, 0x08, 0x00]),
    bytes([0x00, 0x08, 0x10]),
]

# Sent every KEEPALIVE_INTERVAL_SECONDS. The Click answers each RideOn (SYNC_TX echo), but a
# log from 2026-10-28 showed it still stops answering and drops ~60 s after the last button
# press: RideOn does not reset its own inactivity timer. The "get" requests below (the same
# ones activation sends: 0x00 = GET, field 1 = what to get; the Click answers 0x3c) are an
# experiment, `keepalive_mode` in the config, to see whether a request does reset it.
KEEPALIVE_WRITE = RIDE_ON
KEEPALIVE_INTERVAL_SECONDS = 3.0
KEEPALIVE_GETS = [
    bytes([0x00, 0x08, 0x00]),  # device info (answer: 3c 08 00 12 ... "Zwift Click2" ...)
    bytes([0x00, 0x08, 0x10]),  # object 0x10 (answer: 3c 08 10)
]
KEEPALIVE_MODES = ("rideon", "rideon+get", "get")

BUTTON_OPCODE = 0x23  # payload[0] for button-state notifications on CHAR_ASYNC_NOTIFY

# field-1 varint is a bitmask, 0 = pressed. This is the full bit layout for
# Zwift's shared accessory protocol (also used by Zwift Play's d-pad/A-B-Y-Z
# buttons); Click v2 only has two physical paddles, confirmed on real
# hardware to be 0x100 (shift down) and 0x1000 (shift up) -- the other
# bits are listed for config completeness (e.g. if used with a Play
# controller) but will never fire on a real Click.
BUTTON_BITS = {
    0x1: "left",
    0x2: "up",
    0x4: "right",
    0x8: "down",
    0x10: "a",
    0x20: "b",
    0x40: "y",
    0x80: "z",
    0x100: "shift_down",
    0x1000: "shift_up",
}

BUTTON_BIT_SHIFT_UP = 0x1000
BUTTON_BIT_SHIFT_DOWN = 0x100

DEVICE_NAME_HINTS = ("Zwift Click",)

# Each Click v2 pod advertises manufacturer data under Zwift's company ID whose
# first byte says which side it is (same values BikeControl uses). Pressing the
# right pod also shows up on the left pod's notify stream, so connecting both
# double-counts every press -- we only ever connect one side.
ZWIFT_MANUFACTURER_ID = 0x094A
DEVICE_TYPE_BY_SIDE = {
    "right": 0x0A,
    "left": 0x0B,
}
