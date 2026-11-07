---
name: makita-battery-tool
description: Read, diagnose and service Makita LXT 18V batteries with the makita_obi project (Wemos D1 mini on /dev/ttyUSB0 plus the makita-tool Python CLI) - cell voltages, temperatures, charge cycles, lock state, failure codes, false lockout unlock, error reset, LED test, frame backup and restore, raw commands, firmware flashing and wiring. Use when the user mentions a Makita battery, BMS, OBI, BL18xx pack, or a battery that is locked or will not charge.
---

# Makita Battery Tool

Run everything from the project root. Read `README.md` there for wiring and safety rules. Wiring diagram: `docs/wiring.svg`.

## Before using the board

1. Check that the port is free with `fuser /dev/ttyUSB0`. If a browser holds it, ask the user to close the OBI web tool. Do not kill the browser.
2. If `venv` is missing, install it with `uv venv venv` and `uv pip install --python venv/bin/python -e '.[dev]'`.
3. Run commands from the project folder. Every run takes about 3 seconds to start, because the tool waits for the board.

## Read commands (safe to run)

```bash
venv/bin/makita-tool info
venv/bin/makita-tool info --json
venv/bin/makita-tool monitor --interval 10
venv/bin/makita-tool unlock --dry-run
venv/bin/makita-tool raw cc d7 0e 00 02 --read 3
```

`raw` only skips its confirmation for known read commands. It refuses `99` and `10 21` outright: on F0513 packs `99` opens a command tree with a fuse trigger (`94`, "will release smoke"), a massive reset (`11`) and ROM id writes, and `10 21` overwrites the ROM id. Do not work around this with scripts, and do not send undocumented commands to a battery.

## Commands that change the battery

Get the user's explicit approval for each run. Do not pass `--yes` unless the user approved that exact operation.

- `unlock`: clears a false lockout and backs up the frame first. It tries the error reset, and writes the frame only if the pack is still locked.
- `reset-errors`: sends `DA 04`, which clears the error register and wipes battery history. `unlock` runs the same step first. On a real BL1850 (type 2) `DA 04` also reset the charge cycle count from 35 to 0 and the damage rating from 1 to 0, and changed frame bytes 15, 21, 23, 27, 29 and 31, even though the failure code was already OK. The change survived a power cycle. The pack stayed unlocked. Whether other counters are cleared is unknown. Tell the user this before asking for approval. Refused on type 5 packs, which are read only: sources report no LED control, no error reset, and frame writes that do nothing.
- `led-test`, `--probe-test-mode`: enter test mode briefly. `led-test` is not supported on type 5: a real BL1860B refused `d9 96 a5` in both `cc` and `33` forms, and `cc 99` followed by `33 da 31` lit nothing.
- `restore FILE`: undoes an unlock on the same pack. It refuses a backup that differs outside the lock fields, so old backups stop working once the cycle count changes. Backups are in `~/.local/state/makita_tool/backups`.
- `exit-test-mode`: leaves test mode. On a real BL1850, Enable low for 2 s ended test mode too, and removing the pack should do the same.

Run `info` and `unlock --dry-run` first and show the user the result. Do not weaken the unlock refusals, clear a dead failure code, or edit the cycle count: a lockout can be real protection, and the cycle count is the pack's true history.

## Reading the results

- All `FF` from every command: check Enable, 1-Wire, ground and that the battery is seated.
- All `FF` from some commands only: normal, because packs support different commands. Compare with `info`.
- Model marked "estimated from capacity": the pack did not report a model. Capacity cannot separate a BL1850 from a BL1850B, and packs without LEDs are the non-B models, so check the label.
- `DA 31` and `DA 34` (LED commands) replied `07` on a BL1850 with no LEDs. The meaning of `07` is unknown.
- Controller type unknown: the pack may be type 2, which only test mode can confirm. Type 2 health and event counters also answer only in test mode, so `info --probe-test-mode` is needed to see them.
- Checking test mode: on a real BL1850 the frame stayed valid in test mode, and `exit-test-mode` acknowledged even outside it, so neither proves the state. `raw cc d6 04 05 02 --read 3` ends in `06` only in test mode on a type 2 pack, but a new run drops Enable first, so check within one session.
- Temperatures use 0.1 K units. The OBI web tool shows them about 3 °C too high because it treats the value as 0.01 °C.
- `status code` in the web tool is byte 19 of the frame. It is a factory value, not a fault code. Use the failure code instead.
- The web tool stops with "Unsupported Battery" when a pack reports no model, even though live data works. Use `makita-tool` for those packs.

## Firmware

```bash
uvx --from platformio pio run -t upload --upload-port /dev/ttyUSB0
```

Pins are set in `platformio.ini`: `OBI_OW_PIN=13` (D7), `OBI_EN_PIN=12` (D6). Do not use D8 for a line with a pull-up: GPIO15 must be low at boot. The web tool may show a random firmware version when it connects, because it catches ESP8266 boot output.

## Status

Reads and entering and leaving test mode are tested on a real BL1850. `reset-errors` has been run once on a real BL1850, see its effect above. Frame writes, `unlock` and `restore` are tested in simulation only, so treat the first real write as an experiment and tell the user this before running it.
