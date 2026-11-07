# makita_obi

Firmware for a Wemos D1 mini (`src/`, PlatformIO) and a Python tool (`python/makita_tool/`) that read and service Makita LXT 18 V batteries.

## Skills

Agent skills live in `.agents/skills/`. If your tool does not load them, read the file yourself.

- `.agents/skills/makita-battery-tool/SKILL.md`: how to use the board and the tool. Read it before talking to a battery.

## Battery safety

- Reads are safe. Anything that enters test mode or writes needs the user's approval for each run.
- Do not weaken the unlock refusals, clear a dead failure code, or edit the cycle count. The one exception is putting back a pack's own history with `restore --allow-history`, when the user asks for it.

## Checks

```bash
venv/bin/ruff check python
venv/bin/mypy python/makita_tool
venv/bin/pytest -q
```
