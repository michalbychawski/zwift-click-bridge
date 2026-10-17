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

# Sent repeatedly or the device goes idle and the connection drops after ~60s.
KEEPALIVE_WRITE = RIDE_ON
KEEPALIVE_INTERVAL_SECONDS = 3.0

BUTTON_OPCODE = 0x23  # payload[0] for button-state notifications on CHAR_ASYNC_NOTIFY

# Confirmed on real Click v2 hardware: field-1 varint is a bitmask, 0 = pressed.
BUTTON_BIT_SHIFT_UP = 0x1000
BUTTON_BIT_SHIFT_DOWN = 0x4

DEVICE_NAME_HINTS = ("Zwift Click",)
