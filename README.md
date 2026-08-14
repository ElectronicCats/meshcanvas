# MeshCanvas

Draws a shape on a map as synthetic Meshtastic nodes, transmitted over LoRa with
a CatSniffer v3. Built to test how a NodeDB and the position layer hold up under
injected nodes, on a mesh you own.

The Meshtastic protocol is implemented from scratch here: the 16-byte on-air
header, AES-CTR payload encryption, channel hashing, frequency slot selection and
time-on-air. Nothing depends on a Meshtastic device being present. Every constant
was read out of `meshtastic/firmware` at `origin/master` rather than recalled, and
checked against known-good values in the tests.

## Scope

This transmits on real spectrum. Point it at hardware you own, on a channel you
control. Injecting synthetic nodes into a shared or public mesh writes fake
entries into every receiving device's NodeDB and consumes airtime other people
depend on. The defaults here are built for a bench, not for a live network.

## Install

Runs on Linux, macOS and Windows. Requires Python 3.11 or newer.

Create the environment and install:

```bash
uv venv --python 3.13
uv pip install -e ".[dev]"
```

Activate it if you want to run scripts directly (`python -m meshcanvas` works
without activation once the venv interpreter is on PATH):

- Linux and macOS: `source .venv/bin/activate`
- Windows PowerShell: `.venv\Scripts\Activate.ps1`
- Windows cmd: `.venv\Scripts\activate.bat`

Run the server. It binds to localhost by default, and it must stay there: there
is no authentication and `rf` mode keys a transmitter.

```bash
python -m meshcanvas
```

Open http://127.0.0.1:8000. The same command and URL work on all three
platforms.

## Connecting the CatSniffer

The board presents three USB CDC serial ports. MeshCanvas finds the right two
(Cat-LoRa and Cat-Shell) by USB vendor and product id, then identifies each one
by the interface label the firmware exposes (which Linux surfaces), falling back
to USB interface number and then device-name order. This mirrors how Electronic
Cats' own catnip tool selects ports, so it does not matter what the operating
system named the devices:

- Linux: `/dev/ttyACM0`, `/dev/ttyACM1`, `/dev/ttyACM2`. You may need to join the
  `dialout` group to open them: `sudo usermod -aG dialout $USER`, then log out
  and back in.
- macOS: `/dev/cu.usbmodem<serial>1`, `...3`, `...5`. Serial ports are hidden
  from a sandboxed shell, so if discovery reports no board when one is plugged
  in, that is usually why.
- Windows: `COMx`. Windows assigns COM numbers by enumeration history, not by
  interface, so they can look scrambled. MeshCanvas still picks the right ones
  because it sorts by the interface number in the device's hardware id, not by
  the COM number.

Ports are grouped by the board's serial number, so a second CatSniffer used as a
receive sniffer does not get mixed with the transmitter. With two boards attached
auto-discovery cannot know which is which, so it refuses; pick one by serial
(`device_serial=...`, or `--device-serial` on the sniffer tool) or name the ports
outright (`lora_port=...`, `shell_port=...`, or `--lora-port`/`--shell-port`). To
tell the ports apart by hand, the board firmware has an `identify` command on the
Cat-Shell port that blinks the LEDs.

## Modes

| Mode | What it does |
| --- | --- |
| `dry-run` | Builds and encrypts every frame, transmits nothing. Reports real airtime. The default. |
| `mqtt` | Publishes a ServiceEnvelope to a broker. No RF. |
| `rf` | Transmits over LoRa through the CatSniffer. |

Start in `dry-run`. It exercises the whole pipeline, so anything that is going to
fail fails there, without keying the radio.

## Home bench setup

Two pieces of hardware: the CatSniffer transmits, and a Meshtastic node receives
and shows you the result.

**1. Use the Private test profile.** It is the default: standard LongFast radio
settings a stock node understands, but a channel name and a freshly generated key
that only your node holds. That is the whole isolation. Do not use the Meshtastic
public default channel (`LongFast` with the default key) for this unless you are
in RF isolation. That channel is what every stock device powers up on, so any
Meshtastic node in radio range decodes your synthetic nodes, adds them to its
NodeDB, and rebroadcasts them. The private key means a neighbour's node cannot
decrypt the frames even if it hears them, and the private channel name also lands
you on a different frequency slot.

**2. Configure the receiving node** with that same channel name and PSK, the same
region, and the same modem preset. Pair it to the Meshtastic phone or web client
so you can watch its map.

**3. Turn the power down.** `tx_power_dbm` of 0 or lower is plenty across a
bench. The CatSniffer goes down to -9 dBm. Power is clamped to the region limit
automatically, but the limit is much higher than a bench needs.

**4. Dry-run first**, confirm the point count, frequency and airtime look right,
then switch to `rf`.

## Airtime

`/api/budget` reports per-packet time on air, total airtime, projected duty cycle
and ETA before anything is sent, and a run that would exceed the region limit is
refused unless you deliberately override it.

Two frames go out per node, NodeInfo then Position, so 50 nodes is 100 packets.
On US LongFast that is about 600 ms per packet and roughly 2 minutes at the
default pacing.

Pacing targets 50 percent of airtime by default, or the region limit if it is
lower. US and ANZ legally permit 100 percent, but pacing to that keys the
transmitter continuously for the whole run and starves every other node in range.
`airtime_target_percent` lowers it further; the region limit is a hard ceiling
that it cannot raise.

Preset choice dominates everything else. A 58-byte frame is 681 ms on LongFast
and 30 ms on ShortTurbo, a factor of 23.

## Profiles and the frequency slot

The Profile selector presets region, modem preset, frequency slot, channel and
hop limit together.

- **Private test (default).** Standard LongFast radio settings so a regular
  Meshtastic node can demodulate, but the channel name is `meshcanvas` and the
  key is generated fresh with `crypto.getRandomValues` when the profile loads.
  The resulting channel hash is not `0x08`, so only a node you configure with the
  same name and key decodes your synthetic nodes. This is the one to use for
  testing against a normal, stock-firmware node.
- **Meshtastic public default.** The real public channel every stock device uses:
  LongFast, the well-known default key, channel hash `0x08`. Selecting it shows a
  warning, because a run on it in a populated area lands synthetic nodes in every
  Meshtastic device in range and is rebroadcast by their radios. Only use it in
  RF isolation (a shielded enclosure, or somewhere with no other nodes in range)
  or against a node you own.

The public profile targets a channel you do not own. The tool provides it because
the exact parameters are needed for a genuine "does stock firmware resist this"
test, but that test is only clean when no other Meshtastic device is in range. In
a populated area, stay on the Private test profile.

The slot field matters more than it looks. Meshtastic derives the frequency slot
from djb2 of the channel name unless `loraConfig.channel_num` pins it. If your
receiving node's config pins a slot, you must set the same `channel_num` here or
you land on a different frequency: the radio configures happily, transmits
happily, and the node never hears a thing, with nothing reporting the miss. Leave
the field empty to behave like a stock node whose slot comes from the name.

## Choosing a node count

Node count is what decides whether a shape is readable, and text needs far more
points than a solid outline. "MESH" at 50 nodes samples to scattered dots; at
about 300 the letterforms are unmistakable. Solid shapes such as a circle or a
polygon read fine at 50.

That trade runs straight into airtime. 300 nodes is 600 packets, which is about
12 minutes on US LongFast at the default pacing. Render the preview first and
look at the map before committing to a transmit: the preview costs nothing.

If a shape looks wrong on screen, raise the node count before suspecting the
geometry. Zoom out too, since a 1000 m shape over a dense basemap is easy to lose
among the map's own detail.

## Identifying and cleaning up synthetic nodes

Every synthetic node number carries a fixed high byte, `0x7F` by default and
settable per run. Node numbers are derived from a seed with BLAKE2b, so a run is
reproducible, and each run writes `sessions/session-<timestamp>.csv` mapping
every shape point to its node number, node ID and coordinates. That file is what
you use to find and remove the injected entries afterwards.

## Verifying a real node decodes a synthetic packet

This has been confirmed end to end: on a private channel, a stock Meshtastic node
received the transmitted frames, decrypted them, parsed the protobufs, and drew
the synthetic nodes on its own map. A real receiver is the strictest check there
is, so that result validates the whole chain at once: frame assembly, the AES-CTR
crypto, the channel hash, the frequency slot, and the CatSniffer transmit path.

The point of the check is to confirm a receiver treats the frames as genuine, not
merely that something was transmitted. To reproduce it:

1. Run `dry-run` first. `test_a_receiver_can_decode_our_frame` in
   `tests/test_packet.py` already reverses the full pipeline the way a receiving
   node does: split the cleartext header, match the channel by hash, decrypt with
   that channel's key, and parse the protobuf. A successful protobuf decode is
   how the firmware itself validates that it used the right key.

2. Confirm the two radios agree before blaming the payload. On the CatSniffer
   shell (`Cat-Shell`, the third CDC port):

   ```
   lora_config
   ```

   Frequency, SF, bandwidth, coding rate, preamble and sync word must match the
   receiving node's channel settings exactly. Sync word must read
   `0x2B (reg 0x24B4)`.

3. Transmit in `rf` mode with the receiving node's client open. A synthetic node
   appearing in the node list, with its position on the map, is a confirmed
   decode: the receiver could only place it by decrypting the payload and parsing
   the Position protobuf.

4. If nothing arrives, check in this order: sync word, frequency slot, channel
   name and PSK, then power and distance. A mismatch in any of the first three is
   silent, and the radio reports success regardless.

### The sync word trap

The CatSniffer firmware's own help text says `0xNN Custom, e.g. 0x2D for
Meshtastic (reg 0x24D4)`. That is wrong. Meshtastic uses `0x2B`, which expands to
register `0x24B4`; `0x2D` is the LoRaWAN public-network value. A board configured
from that help text transmits and receives nothing, with no error anywhere. This
driver always sends `0x2B`.

## Layout

```
meshcanvas/protocol/   header, AES-CTR crypto, channel hashing, frequency, ToA
meshcanvas/radio/      null (dry-run), catsniffer (serial), mqtt
meshcanvas/geometry/   rasterization, projection, k-means sampling
meshcanvas/api/        FastAPI, WebSocket progress
web/                   Leaflet map and control panel
```

Layering runs one way. The protocol layer never imports the radio layer, and
geometry imports neither. A backend takes finished frame bytes, which is what
lets the null backend stand in as a complete test rig for everything above it.

## Tests

```bash
python -m pytest -q
```

248 tests. The protocol layer is checked against known-good external values
rather than against itself:

- The LongFast channel hash `0x08`, the byte every stock Meshtastic node puts in
  its header.
- Published frequency slots: US LongFast at 906.875 MHz, EU_868 at 869.525 MHz,
  EU_433 at 433.875 MHz.
- Time on air replicates RadioLib's integer arithmetic including its truncation,
  because a float model drifts from what the chip actually schedules.
- Geometry: a square specified in metres must come out square in metres at
  latitude 45, measured with haversine. That test fails if the `cos(latitude)`
  projection correction is missing or applied to the wrong axis.

The CatSniffer configuration path has been run against real hardware (firmware
v3.1.0.0) with every parameter read back. The transmit path is covered by a fake
serial port that replays the firmware's own response strings.

## Known limits

- The stream-mode transmit path has not been confirmed on air.
- Stream mode has no framing: the firmware transmits whatever its ring buffer
  holds when the LoRa thread polls. Each frame is written in one call, and frames
  at or under 64 bytes fit a single USB bulk packet, so they arrive intact.
  Larger frames could in principle split across two transmissions.
- `next_hop` and `relay_node` are written as zero. Valid on both 2.5.x and 2.6+,
  but not confirmed against a live 2.6 receiver.
