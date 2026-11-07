"""Assemble the web page, the Python package it runs and the firmware into one folder for hosting."""

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_FILES = ('__init__', 'battery', 'frame', 'transport', 'web')
FIRMWARE = ROOT / '.pio' / 'build' / 'd1_mini' / 'firmware.bin'
ESP_IMAGE_MAGIC = 0xE9


def firmware_version() -> str:
    """Read the firmware version from the build flags.

    :return: Version text.
    """
    config = (ROOT / 'platformio.ini').read_text()

    return '.'.join(re.findall(r'FIRMWARE_VERSION_(?:MAJOR|MINOR|PATCH)=(\d+)', config))


def add_firmware(target: Path) -> None:
    """Copy the firmware and write the manifest of the install button.

    :param target: Firmware folder, created here.
    """
    if not FIRMWARE.exists():
        raise SystemExit(f'{FIRMWARE} is missing: build it first with pio run')

    if FIRMWARE.read_bytes()[0] != ESP_IMAGE_MAGIC:
        raise SystemExit(f'{FIRMWARE} is not an ESP image')

    target.mkdir()
    shutil.copy(FIRMWARE, target)
    manifest = {
        'name': 'makita_obi',
        'version': firmware_version(),
        'builds': [{'chipFamily': 'ESP8266', 'parts': [{'path': 'firmware.bin', 'offset': 0}]}],
    }
    (target / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


def build(target: Path) -> None:
    """Copy the site files.

    :param target: Output folder, replaced if it exists.
    """
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(ROOT / 'web', target)
    package = target / 'py' / 'makita_tool'
    package.mkdir(parents=True)
    for name in PACKAGE_FILES:
        shutil.copy(ROOT / 'python' / 'makita_tool' / f'{name}.py', package)

    add_firmware(target=target / 'firmware')


if __name__ == '__main__':
    build(target=Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / '_site')
