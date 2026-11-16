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

# Sent every KEEPALIVE_INTERVAL_SECONDS. The Click answers each one, but it doesn't reset the pod's
# own ~60 s sleep timer: nothing written to SYNC_RX does (RideOn, GET requests, a released
# controller frame, game state, the pairing page were all tried; see the git history up to 0.2.11).
# The wake sequence below does.
KEEPALIVE_WRITE = RIDE_ON
KEEPALIVE_INTERVAL_SECONDS = 3.0

# Keeping the right pod awake: this sequence keeps it on for ~944 s on the same connection. It's
# what BikeControl sends:
# click2whoosh (github.com/NicolaM96/click2whoosh, docs/HOW-IT-WORKS.md) captured it from
# BikeControl's Bluetooth traffic, firmware 1.1.0. Notifications on both characteristics are
# re-enabled, then CHAR_WAKE_CONTROL <- 01 (reply 01 f0 00), CHAR_WAKE <- WAKE_MESSAGE (reply 00),
# CHAR_WAKE_CONTROL <- 03 (reply 12 00 ...). These characteristics belong to the firmware-update
# mechanism: swift-click warns this "can erase the secondary firmware slot" (the spare slot an
# update is written to, not the running firmware). `keep_awake` in the config turns it off.
CHAR_WAKE = "00000100-19ca-4651-86e5-fa29dcdd09d1"
CHAR_WAKE_CONTROL = "00000102-19ca-4651-86e5-fa29dcdd09d1"
WAKE_MESSAGE = bytes.fromhex("3d b8 f3 96 00 00 00 00 00 01 00 00 00 b0 03 00 00 00")
WAKE_RENEW_SECONDS = 300.0  # click2whoosh's interval, well inside the ~944 s each run grants
# Restarts the pod, which cancels the rest of the ~944 s: it then sleeps about a minute later.
# Sent when the bridge disconnects on its own, so the pod doesn't stay on for 15 minutes.
# (POWER_OFF, 0x0f, is answered NOT_SUPPORTED.)
OPCODE_RESET = 0x18
CHAR_FIRMWARE_REVISION = "00002a26-0000-1000-8000-00805f9b34fb"
CHAR_BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"  # standard; the pod also notifies changes

# A connection attempt that works takes 0.2-6 s. One that hangs does so reading services, and
# the Click may stop advertising during a long wait, so give up early and catch the next packet.
CONNECT_TIMEOUT_SECONDS = 10.0

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
