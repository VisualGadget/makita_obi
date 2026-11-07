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
- `unlock` never clears the dead failure code or changes history such as the cycle count.
- A pack in test mode cannot power tools or charge. The tool always leaves test mode, and `exit-test-mode` recovers it.
- Lithium-ion packs can be dangerous. A lockout is often real protection, so do not charge a pack you do not trust.

## Status

Read commands and entering and leaving test mode are tested on a real 2014 BL1850 that reports no model and has no
LEDs, so the LED commands ran but their effect was not seen: they replied `07`, not the usual `06`. Frame writes,
`unlock`, `reset-errors` and `restore` are covered by simulated tests only and have not been run on a real battery. Support for controller types other than this pack is implemented
from public notes and is untested.

## Credits

Protocol facts come from [makita-lxt-protocol](https://github.com/rosvall/makita-lxt-protocol) by rosvall, Open
Battery Information by Martin Jansson, and the public notes of PocketOBI, makita-battery-tools and
Makita-LXT-Battery-Monitor-Unlocker. No code from those last three projects is included.
