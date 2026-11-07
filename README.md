# makita_obi

Read and service Makita LXT 18 V batteries with a Wemos D1 mini (ESP8266) and a Python command line tool.

The firmware is based on ArduinoOBI from [Open Battery Information](https://github.com/mnh-jansson/open-battery-information)
and stays compatible with its web tool. It adds a session mode that holds Enable high across several commands,
which frame writes need.

## Wiring

See [docs/wiring.svg](docs/wiring.svg).

![Finished adapter: D1 mini wired to the battery connector, with a thick copper foil tab that fits the battery GND slot](docs/hardware.jpg)

| Battery | D1 mini | Note |
|---|---|---|
| Connector position 6, Enable | D6 (GPIO12) | 4.7 kΩ pull-up to 3V3 |
| Connector position 2, 1-Wire | D7 (GPIO13) | 4.7 kΩ pull-up to 3V3 |
| Main `−` terminal | GND | Shared ground, a thick copper foil tab fits the slot |

Position 1 of the yellow connector is the end closest to the negative battery terminal. Never pull these lines up
to 5 V and never connect the main `+` terminal to the D1 mini.

## Install

```bash
uv venv venv
uv pip install --python venv/bin/python -e '.[dev]'
uvx --from platformio pio run -t upload --upload-port /dev/ttyUSB0
```

## Use

```bash
venv/bin/makita-tool info                  # read everything, changes nothing
venv/bin/makita-tool info --json
venv/bin/makita-tool monitor --interval 10
venv/bin/makita-tool unlock --dry-run      # show what an unlock would do
venv/bin/makita-tool unlock
venv/bin/makita-tool reset-errors
venv/bin/makita-tool led-test
venv/bin/makita-tool restore FILE          # write a backup frame back to the same pack
venv/bin/makita-tool exit-test-mode        # recover a pack left in test mode
venv/bin/makita-tool raw cc d7 0e 00 02 --read 3
```

Close the OBI web tool first, because only one program can hold the serial port.

## Safety

- Reading sends documented read commands only. Test mode needs an explicit `--probe-test-mode`.
- Writes ask for confirmation, and `unlock` saves the frame to `~/.local/state/makita_tool/backups` first.
- `unlock` clears a false lockout on a healthy pack. It refuses a pack that reports itself dead, has a cell failure
  flag, has a cell below 3.0 V, has more than 0.3 V imbalance, is above 60 °C, or has an unknown controller type.
- `unlock` tries the error reset first and only writes the frame if the pack is still locked.
- `unlock` never clears the dead failure code or changes history such as the cycle count.
- `restore` only undoes an unlock: it refuses a backup that differs from the pack outside the lock fields.
- A write stops if test mode is refused, the pack does not arm, or the ROM id changes. The pack arms once per
  insertion, so reinsert it before retrying.
- Public notes say a pack in test mode cannot power tools or charge. The tool always leaves test mode, and on the
  tested BL1850 holding Enable low for 2 s also ended it.
- `reset-errors` wipes battery history, and `unlock` runs the same `DA 04` step first. On a real BL1850 (type 2) `DA 04` also reset the charge cycle count from 35 to 0 and the damage rating from 1 to 0, and changed frame bytes 15, 21, 23, 27, 29 and 31, even though the failure code was already OK. The change survived a power cycle. The pack stayed unlocked. Whether other counters are cleared is unknown.
- F0513 (type 5) packs are read only. They have no LED test, error reset or working frame write.
- `raw` refuses `99` and `10 21`, even with `--yes`. On F0513 packs `99` opens a command tree that includes a fuse
  trigger and a massive reset, and `10 21` overwrites the ROM id. Do not send other unknown commands to a battery.
- Lithium-ion packs can be dangerous. A lockout is often real protection, so do not charge a pack you do not trust.

## Status

Read commands and entering and leaving test mode are tested on a real 2014 BL1850 that reports no model and has no
LEDs, so the LED commands ran but their effect was not seen: they replied `07`, not the usual `06`. Frame writes,
`reset-errors` has been run once, on a real BL1850. `unlock` and `restore` are covered by simulated tests only and have not been run on a real battery. Support for controller types other than this pack is implemented
from public notes and is untested.

## Credits

Protocol facts come from [makita-lxt-protocol](https://github.com/rosvall/makita-lxt-protocol) by rosvall, Open
Battery Information by Martin Jansson, and the public notes of PocketOBI, makita-battery-tools and
Makita-LXT-Battery-Monitor-Unlocker. No code from those last three projects is included.
