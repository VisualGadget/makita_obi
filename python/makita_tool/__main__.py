"""Command line interface."""

import argparse
import json
import sys
import time
from pathlib import Path

import serial

from makita_tool import __version__
from makita_tool.battery import Battery, BatteryError, Report, report_to_dict
from makita_tool.frame import Frame
from makita_tool.link import Link, LinkError

PROBE_HELP = 'identify the controller with a short visit to test mode'
READ_ONLY_PREFIXES = (b'\xaa\x00', b'\xd4', b'\xd6', b'\xd7', b'\xdc\x0b', b'\xdc\x0c')


def print_report(report: Report) -> None:
    """Print a report for people.

    :param report: Battery report.
    """
    model = report.model or 'unknown'
    source = 'reported by the pack' if report.model_reported else 'estimated from capacity'
    lock = 'LOCKED: ' + ', '.join(report.lock_causes) if report.locked else 'unlocked'
    rows: list[tuple[str, object]] = [
        ('Model', f'{model} ({source})' if report.model else model),
        ('Manufactured', report.manufactured),
        ('Capacity', f'{report.capacity_ah:.1f} Ah'),
        ('Controller type', report.controller_type if report.controller_type is not None else 'unknown'),
        ('Charge cycles', report.cycle_count),
        ('Lock state', lock),
        ('Failure code', report.failure),
        ('Cell failure flag', 'set' if report.cell_failure else 'clear'),
        ('Damage rating', f'{report.damage_rating} of 7'),
    ]
    live = report.live
    if live.pack_voltage is not None:
        rows.append(('Pack voltage', f'{live.pack_voltage:.3f} V'))

    for index, voltage in enumerate(live.cell_voltages, start=1):
        rows.append((f'Cell {index}', f'{voltage:.3f} V'))

    if live.imbalance is not None:
        rows.append(('Cell imbalance', f'{live.imbalance:.3f} V'))

    for label, temperature in (('Temperature 1', live.temperature_c), ('Temperature 2', live.temperature2_c)):
        if temperature is not None:
            rows.append((label, f'{temperature:.1f} °C'))

    if report.health_rating is not None:
        rows.append(('Health', f'{report.health_rating} of 4'))

    if report.overdischarge_percent is not None:
        rows.append(('Overdischarge', f'{report.overdischarge_percent} %'))

    if report.overload_percent is not None:
        rows.append(('Overload', f'{report.overload_percent} %'))

    if report.overdischarge_events is not None:
        rows.append(('Overdischarge events', report.overdischarge_events))

    if report.overload_events is not None:
        rows.append(('Overload events', report.overload_events))

    if report.charge_level_raw is not None:
        rows.append(('Charge level (raw)', report.charge_level_raw))

    rows.extend((('ROM id', report.rom), ('Frame', report.frame)))
    for name, value in rows:
        print(f'{name + ":":22}{value}')


def confirm(message: str, assume_yes: bool) -> None:
    """Ask before an operation that changes the battery.

    :param message: What is about to happen.
    :param assume_yes: Skip the question.
    """
    print(message)
    if not assume_yes and input('Type yes to continue: ').strip().lower() != 'yes':
        raise BatteryError('cancelled, nothing was changed')


def run_info(battery: Battery, args: argparse.Namespace) -> None:
    """Print the battery report.

    :param battery: Battery.
    :param args: Parsed arguments.
    """
    report = battery.report(probe_test_mode=args.probe_test_mode)
    if args.json:
        print(json.dumps(report_to_dict(report), indent=2))
    else:
        print_report(report)


def run_monitor(battery: Battery, args: argparse.Namespace) -> None:
    """Print live measurements repeatedly.

    :param battery: Battery.
    :param args: Parsed arguments.
    """
    while True:
        live = battery.report().live
        cells = ' '.join(f'{voltage:.3f}' for voltage in live.cell_voltages)
        print(f'{time.strftime("%H:%M:%S")} {live.pack_voltage} V  cells {cells}  {live.temperature_c} °C', flush=True)
        time.sleep(args.interval)


def run_raw(link: Link, args: argparse.Namespace) -> None:
    """Send a raw command.

    :param link: Link to the firmware.
    :param args: Parsed arguments.
    """
    data = bytes.fromhex(''.join(args.bytes))
    if args.mode != 'cc' or not data.startswith(READ_ONLY_PREFIXES):
        confirm(message=f'Raw {args.mode} command {data.hex(" ")} may change the battery.', assume_yes=args.yes)

    handlers = {'cc': link.skip, '33': link.rom, 'none': link.raw}
    print(handlers[args.mode](data=data, reply_length=args.read).hex(' ').upper())


def run_unlock(battery: Battery, args: argparse.Namespace) -> None:
    """Clear a false lockout.

    :param battery: Battery.
    :param args: Parsed arguments.
    """
    _, frame, target, clear_errors, blockers = battery.plan_unlock(probe_test_mode=args.probe_test_mode)
    if target is None and not clear_errors:
        print(f'Nothing to do: the frame is valid and the failure code is {frame.failure_name}.')

        return

    if blockers:
        raise BatteryError('unlock refused: ' + '; '.join(blockers))

    print(f'Current frame: {frame.data.hex(" ").upper()}')
    print(f'The error register is cleared first, failure code: {frame.failure_name}.')
    if target is not None:
        print(f'If still locked: {target.data.hex(" ").upper()}')

    if args.dry_run:
        print('Dry run: nothing was written.')

        return

    confirm(message='This writes to the battery. A backup of the frame is saved first.', assume_yes=args.yes)
    print_result(frame=battery.unlock(probe_test_mode=args.probe_test_mode))


def print_result(frame: Frame) -> None:
    """Print the state of a frame after a write.

    :param frame: Frame read back from the battery.
    """
    print(f'Read back:     {frame.data.hex(" ").upper()}')
    print(f'Lock causes:   {", ".join(frame.lock_causes) or "none"}')
    print(f'Failure code:  {frame.failure_name}')


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    :return: Argument parser.
    """
    parser = argparse.ArgumentParser(prog='makita-tool', description=__doc__)
    parser.add_argument('--port', default='/dev/ttyUSB0', help='serial port of the D1 mini')
    parser.add_argument('--version', action='version', version=__version__)
    commands = parser.add_subparsers(dest='command', required=True)

    info = commands.add_parser('info', help='read everything without changing the battery')
    info.add_argument('--json', action='store_true', help='print JSON')
    info.add_argument('--probe-test-mode', action='store_true', help=PROBE_HELP)

    monitor = commands.add_parser('monitor', help='print live measurements repeatedly')
    monitor.add_argument('--interval', type=float, default=5.0, help='seconds between reads')

    raw = commands.add_parser('raw', help='send raw bytes, for example: raw cc d7 0e 00 02 --read 3')
    raw.add_argument('mode', choices=('cc', '33', 'none'), help='ROM command sent first')
    raw.add_argument('bytes', nargs='+', help='hex bytes')
    raw.add_argument('--read', type=int, default=0, help='number of bytes to read')
    raw.add_argument('--yes', action='store_true', help='do not ask for confirmation')

    leds = commands.add_parser('led-test', help='light all LEDs: briefly enters test mode')
    leds.add_argument('--seconds', type=float, default=5.0, help='time the LEDs stay on')
    leds.add_argument('--yes', action='store_true', help='do not ask for confirmation')

    reset = commands.add_parser('reset-errors', help='clear the error register')
    reset.add_argument('--yes', action='store_true', help='do not ask for confirmation')

    unlock = commands.add_parser('unlock', help='clear a false lockout on a healthy pack')
    unlock.add_argument('--dry-run', action='store_true', help='show the plan without writing')
    unlock.add_argument('--probe-test-mode', action='store_true', help=PROBE_HELP)
    unlock.add_argument('--yes', action='store_true', help='do not ask for confirmation')

    restore = commands.add_parser('restore', help='write a backup frame back to the same battery')
    restore.add_argument('file', type=Path, help='backup file')
    restore.add_argument('--yes', action='store_true', help='do not ask for confirmation')

    commands.add_parser('exit-test-mode', help='recover a pack left in test mode')

    return parser


def dispatch(link: Link, args: argparse.Namespace) -> None:
    """Run the selected command.

    :param link: Link to the firmware.
    :param args: Parsed arguments.
    """
    battery = Battery(link=link)
    if args.command == 'info':
        run_info(battery=battery, args=args)
    elif args.command == 'monitor':
        run_monitor(battery=battery, args=args)
    elif args.command == 'raw':
        run_raw(link=link, args=args)
    elif args.command == 'led-test':
        confirm(message='The pack enters test mode for a few seconds.', assume_yes=args.yes)
        print(f'LED command replies: {battery.led_test(seconds=args.seconds).hex(" ").upper()}')
    elif args.command == 'reset-errors':
        confirm(message='This clears the error register of the battery.', assume_yes=args.yes)
        battery.reset_errors()
        with link.session():
            print_result(frame=battery.read_frame()[1])
    elif args.command == 'unlock':
        run_unlock(battery=battery, args=args)
    elif args.command == 'restore':
        confirm(message=f'This writes the frame from {args.file} to the battery.', assume_yes=args.yes)
        print_result(frame=battery.restore(path=args.file))
    elif args.command == 'exit-test-mode':
        with link.session():
            battery.exit_test_mode()


def main() -> int:
    """Run the tool.

    :return: Process exit code.
    """
    args = build_parser().parse_args()
    try:
        with Link(port=args.port) as link:
            dispatch(link=link, args=args)
    except (BatteryError, LinkError, serial.SerialException) as error:
        print(f'Error: {error}', file=sys.stderr)

        return 1

    except KeyboardInterrupt:
        return 130

    return 0


if __name__ == '__main__':
    sys.exit(main())
