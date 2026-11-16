# zwift-click-bridge

Pairs with one or more **Zwift Click v2** controllers over Bluetooth LE and
maps their paddle buttons to keyboard key presses for [MyWhoosh](https://mywhoosh.com)'s
MyShift virtual gearing keybinds (`I` = gear up, `K` = gear down). By default
the `b` button sends `I`, and `shift_up` and `shift_down` both send `K`. Works on macOS and Windows.

## How it works

A Click that has already been paired with the real Zwift app at least once
stays "unlocked" permanently on the hardware side. This tool takes advantage
of that: it skips the full ECDH handshake Zwift Play/Ride use, and instead:

1. Connects over BLE, subscribes to button notifications and to the device's
   SYNC_TX indications (its replies to activation and keepalive writes), and
   writes a short 3-message activation sequence.
2. Sends a `RideOn` keepalive every 3 seconds. A failed write is logged and
   retried on the next tick. This alone doesn't keep the pod awake: it sleeps
   about a minute after the last press whatever is written to its command
   channel.
3. Keeps the pod awake with the wake sequence BikeControl uses (`keep_awake`,
   see below), renewed every 5 minutes, and restarts the pod when the bridge
   disconnects so it can sleep.
4. Reads button state from unencrypted notifications (opcode `0x23`): a
   bitmask where bit `0x1000` is shift-up and bit `0x100` is shift-down, with
   `0` meaning pressed.

If your Click has *never* been paired with the real Zwift app, this won't
work -- pair it with Zwift once first to unlock it, then this tool can take
over.

## Requirements

- Python 3.10+
- A Zwift Click v2, powered on and previously unlocked by the Zwift app
- macOS 10.15+ or Windows 10 1809+ (both need BLE support, obviously)

## Setup

### Windows: prebuilt .exe (no Python needed)

A standalone `ZwiftClickBridge.exe` is built by
[the "Build Windows exe" GitHub Actions workflow](../../actions/workflows/build-windows.yml):
open it, click **Run workflow**, wait for it to finish, then download the
`ZwiftClickBridge-windows` artifact from the run's summary page. It's
unsigned, so Windows SmartScreen will likely warn on first launch -- click
"More info" -> "Run anyway".

### From source (macOS, Linux, or Windows)

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

This opens a window that scans continuously for the **right** Click v2 pod
(the left pod is ignored -- pressing the right pod also shows up on the
left pod's connection, so connecting both made every press count twice).
The window shows:

- Both Click v2 pods, drawn after the real ones. The pod in use is lit; each
  of its buttons lights up in its own colour on every press, and its LED shows
  the connection (gray = not connected, yellow = connecting, green = connected).
- A key chip beside each button with the key it sends.
- The pod's firmware, battery level and whether keep-awake is on.
- The last press and how many presses this session.
- A **Disconnect** button to deliberately drop the connection (e.g. to let
  it go to sleep). It reconnects automatically the next time you press one
  of its paddles.

**Remapping at runtime:** click a button on the drawing (or its key chip),
then press the key it should send (Esc cancels). Right-click to unmap it.
Changes take effect immediately and are saved to the config file.

The **Stop listening** / **Start listening** button at the top pauses or
resumes scanning entirely, disconnecting everything while stopped. Closing
the window quits the app.

## Configuration

A config file is created on first run at:

- macOS/Linux: `~/.config/zwift-click-bridge/config.json`
- Windows: `C:\Users\<you>\.config\zwift-click-bridge\config.json`

```json
{
  "controller_side": "right",
  "idle_disconnect_seconds": 600,
  "post_idle_cooldown_seconds": 90,
  "keep_awake": true,
  "buttons": {
    "0x1": {"name": "left", "key": null},
    "0x2": {"name": "up", "key": null},
    "0x4": {"name": "right", "key": null},
    "0x8": {"name": "down", "key": null},
    "0x10": {"name": "a", "key": null},
    "0x20": {"name": "b", "key": "i"},
    "0x40": {"name": "y", "key": null},
    "0x80": {"name": "z", "key": null},
    "0x100": {"name": "shift_down", "key": "k"},
    "0x1000": {"name": "shift_up", "key": "k"}
  }
}
```

`controller_side` picks which Click v2 pod to connect to (`"right"` or
`"left"`). The side is read from the pod's BLE advertisement, and the log
prints each pod's device type when it's first seen (`0x0a` = right, `0x0b` =
left).

All 10 bit positions from Zwift's shared accessory protocol are listed. On
a real Click v2 the two paddles -- `0x100` (shift down) and `0x1000` (shift
up) -- are the primary controls; `0x20` ("b") is mapped to the same key as
shift down by default as a second trigger for it. The rest
(`left`/`up`/`right`/`down`/`a`/`y`/`z`) belong to Zwift Play's d-pad and
face buttons and are here for config completeness (e.g. if you ever point
this at a Play controller instead). Set `"key"` to `null` to leave a button
unmapped, or to a key name to map it. Single characters (`"i"`, `"k"`) map
directly; for special keys use `pynput.keyboard.Key` names, e.g.
`"page_up"`, `"up"`, `"f1"`.

`idle_disconnect_seconds` (default 600 = 10 minutes) controls when the
bridge voluntarily disconnects a controller after no button activity on it,
letting the Click drop into its own battery-saving sleep instead of being
held awake indefinitely by `keep_awake`. It reconnects automatically the
next time you press that controller. Set it higher (or very large) if you'd
rather it always stay connected. The same cooldown applies if you click a
controller's **Disconnect** button yourself.

`post_idle_cooldown_seconds` (default 90) is how long the bridge waits
before it's willing to reconnect a controller after the above disconnect.
The Click keeps advertising for a while (tens of seconds) after any
disconnect before it actually sleeps; without this cooldown the bridge
would immediately reconnect to that lingering advertisement and the
controller would never actually get to sleep.

`keep_awake` (default `true`) sends the sequence BikeControl uses to keep a right
pod on without the Zwift app. Without it the pod sleeps about a minute after the
last press, whatever is written to its command channel (`RideOn`, requests,
released-button frames and the rest were all tried in 0.2.x), and a press then
has to wake it and wait a few seconds for the reconnect. Each run keeps it on for about
15 minutes (944 s) on the same connection, and the bridge renews it every 5
minutes. The sequence was captured from BikeControl's Bluetooth traffic by
[click2whoosh](https://github.com/NicolaM96/click2whoosh) (firmware 1.1.0): the
notifications on characteristics `…0100` and `…0102` are re-enabled, then
`…0102` ← `01`, `…0100` ← `3d b8 f3 96 00 00 00 00 00 01 00 00 00 b0 03 00 00 00`,
`…0102` ← `03`. The replies are logged as `reply: wake 0102 01f000` and so on.
**These characteristics belong to the pod's firmware-update mechanism.**
[swift-click](https://github.com/benoitzelsmann/swift-click) warns that this can
erase the secondary firmware slot (the spare slot an update is written to, not
the firmware that runs) and that long-term effects are unknown. BikeControl sends
it to every right pod it keeps awake. Set `false` to go back to the old
behaviour. When the bridge disconnects on its own (idle, **Disconnect**, closing
it normally) it writes `RESET` (`0x18`), so the pod sleeps about a minute later
instead of staying on for the rest of the 15 minutes. The pod's firmware version
is logged at every connect. Tested on a right pod with firmware 1.2.0: a 58-minute
ride with pauses of up to 4 minutes and no drops.

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

- The window has a log panel (**Show log**); **Copy log** puts the recent
  log (up to 3000 lines) on the clipboard for a bug report. The same log goes
  to `~/.config/zwift-click-bridge/bridge.log` (also on Windows, under your user
  folder; the `.exe` has no console). It records the app version and system,
  each connect (time, MTU, characteristics, firmware, battery), the Click's
  first replies, each keep-awake renewal, a status line every 5 minutes while
  connected, any non-button messages from the
  Click, failed keepalive writes, and every drop with how long it lasted and
  when the last reply and notification arrived.
- `zwift-click-spike` prints every decoded button bitmask -- use it to
  confirm the Click is reachable and behaving before blaming the bridge.
- `zwift-click-sniff` is a lower-level diagnostic that subscribes to every
  notify/indicate characteristic on the device and logs raw bytes verbatim,
  useful if a firmware update ever changes the protocol.
- If the Click drops mid-session, press a paddle to wake it; the bridge
  reconnects when it advertises again. With `keep_awake` on, a drop should
  only come from the radio link (range, interference).
- The first few paddle presses right after connecting can be silently
  dropped -- the button sensor itself seems to stay in a low-power state
  until a real press wakes it up, separately from the BLE connection being
  active. This is normal; just press a couple extra times after starting
  the bridge and it settles into reliably catching every press.
- Some individual units can stop reporting button presses after a while
  even though they're still showing as connected (other traffic, like the
  keepalive, keeps flowing fine -- it's specifically button-press reporting
  that goes quiet). This looks like a firmware-side issue on that specific
  controller rather than anything the bridge can fix from the host side.
  Click that controller's **Disconnect** button and press it again to force
  a fresh connection, which typically restores it for a while.
