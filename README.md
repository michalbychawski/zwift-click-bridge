# zwift-click-bridge

Pairs with a **Zwift Click v2** controller over Bluetooth LE and maps its two
paddle buttons to keyboard key presses -- by default `I` (shift up) and `K`
(shift down), matching [MyWhoosh](https://mywhoosh.com)'s MyShift virtual
gearing keybinds. Works on macOS and Windows.

## How it works

A Click that has already been paired with the real Zwift app at least once
stays "unlocked" permanently on the hardware side. This tool takes advantage
of that: it skips the full ECDH handshake Zwift Play/Ride use, and instead:

1. Connects over BLE and writes a short 3-message activation sequence.
2. Sends a bare `RideOn` keepalive write every 3 seconds (skipping this makes
   the device go idle and disconnect after about a minute).
3. Reads button state from unencrypted notifications (opcode `0x23`): a
   bitmask where bit `0x1000` is shift-up and bit `0x4` is shift-down, with
   `0` meaning pressed.

If your Click has *never* been paired with the real Zwift app, this won't
work -- pair it with Zwift once first to unlock it, then this tool can take
over.

## Requirements

- Python 3.10+
- A Zwift Click v2, powered on and previously unlocked by the Zwift app
- macOS 10.15+ or Windows 10 1809+ (both need BLE support, obviously)

## Setup

```
git clone git@github.com:michalbychawski/zwift-click-bridge.git
cd zwift-click-bridge
python3 -m venv .venv
```

macOS/Linux:
```
.venv/bin/pip install -e .
```

Windows (PowerShell):
```
.venv\Scripts\activate
pip install -e .
```

## Usage

Run the diagnostic first to confirm the Click connects and buttons decode
correctly on your machine before trusting the real bridge:

```
.venv/bin/zwift-click-spike      # macOS/Linux
zwift-click-spike                # Windows, venv activated
```

Press both paddles -- you should see `shift_up=pressed` / `shift_down=pressed`
toggle. Then run the real bridge, which actually types the mapped keys:

```
.venv/bin/zwift-click-bridge      # macOS/Linux
zwift-click-bridge                # Windows, venv activated
```

A tray icon shows connection status (gray = disconnected, green = connected)
with a Quit option. It auto-reconnects if the Click disconnects.

## Configuration

A config file is created on first run at:

- macOS/Linux: `~/.config/zwift-click-bridge/config.json`
- Windows: `C:\Users\<you>\.config\zwift-click-bridge\config.json`

```json
{
  "reconnect_delay_seconds": 3,
  "buttons": {
    "0x1000": {"name": "shift_up", "key": "i"},
    "0x4": {"name": "shift_down", "key": "k"}
  }
}
```

Change `"key"` to remap either button. Single characters (`"i"`, `"k"`) map
directly; for special keys use `pynput.keyboard.Key` names, e.g. `"page_up"`,
`"up"`, `"f1"`.

## Platform notes

**macOS**: the first time it tries to inject a keystroke, you may need to
grant Accessibility permission to your terminal app in System Settings ->
Privacy & Security -> Accessibility. If keys still don't land after granting
it, toggle the permission off and back on, then relaunch the bridge.

**Windows**: should work out of the box via `SendInput`. If MyWhoosh (or
whatever you're controlling) ever runs elevated as Administrator while the
bridge runs as a normal user, Windows' UIPI will silently block injected
keys -- run the bridge as Administrator too in that case. If Windows prompts
to "pair" the Click when it's first seen, you can usually dismiss it; this
tool connects directly over GATT without needing OS-level Bluetooth pairing.

**Both platforms**: close the real Zwift app if it's running on the same
machine -- two hosts fighting over the same BLE connection causes the Click
to get stuck in a non-responsive state (it recovers after being left idle
for ~20 seconds and reconnected cleanly).

## Troubleshooting

- `zwift-click-spike` prints every decoded button bitmask -- use it to
  confirm the Click is reachable and behaving before blaming the bridge.
- `zwift-click-sniff` is a lower-level diagnostic that subscribes to every
  notify/indicate characteristic on the device and logs raw bytes verbatim,
  useful if a firmware update ever changes the protocol.
- If the Click stops responding mid-session, it's almost always the device
  itself going idle, not the bridge -- leave it untouched for ~20s, press a
  paddle to wake it, and the bridge's auto-reconnect should pick it back up.
