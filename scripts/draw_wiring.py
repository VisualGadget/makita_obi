"""Draw the wiring between a Wemos D1 mini and the 7-position connector of a Makita LXT battery."""

from itertools import pairwise
from pathlib import Path

import schemdraw
from schemdraw import elements as elm

OUTPUT_FILE = Path(__file__).resolve().parents[1] / 'docs' / 'wiring.svg'
BOX_HEIGHT = 15
WIRE_LENGTH = 11
CELL_SIZE = 1.5
POSITION_NOTES = {7: 'no wire', 6: 'Enable', 2: '1-Wire'}


def add_numbering_panel(drawing: schemdraw.Drawing, left: float, top: float) -> None:
    """Add a panel that explains how the connector positions are numbered.

    :param drawing: Drawing to add the panel to.
    :param left: X coordinate of the left edge of the panel.
    :param top: Y coordinate of the top edge of the panel.
    """
    title = 'Connector position numbering (used in this drawing, not an official Makita numbering)'
    drawing.add(elm.Label().at((left, top)).label(title, halign='left'))

    cell_top = top - 1
    cell_bottom = cell_top - CELL_SIZE
    for index, position in enumerate(range(7, 0, -1)):
        cell_left = left + index * CELL_SIZE
        cell_right = cell_left + CELL_SIZE
        corners = [
            (cell_left, cell_top),
            (cell_right, cell_top),
            (cell_right, cell_bottom),
            (cell_left, cell_bottom),
            (cell_left, cell_top),
        ]
        for start, end in pairwise(corners):
            drawing.add(elm.Line().at(start).to(end))

        center_x = cell_left + CELL_SIZE / 2
        drawing.add(elm.Label().at((center_x, (cell_top + cell_bottom) / 2)).label(str(position), halign='center'))
        if position in POSITION_NOTES:
            note_y = cell_bottom - 0.6
            drawing.add(elm.Label().at((center_x, note_y)).label(POSITION_NOTES[position], halign='center'))

    notes = (
        'Cable plug with its contact opening up and wires pointing down: position 1 is the right end,\n'
        'position 7 is the left end. Position 7 has a contact but no wire in the Makita cable.\n'
        'Enable and 1-Wire are each second from an end, so turning the plug around swaps them.\n'
        'On the battery, position 1 is the end closest to the negative terminal.'
    )
    notes_left = left + 7 * CELL_SIZE + 1
    drawing.add(elm.Label().at((notes_left, cell_top - 1)).label(notes, halign='left'))


def build_drawing() -> schemdraw.Drawing:
    """Build the wiring drawing.

    :return: The finished drawing.
    """
    drawing = schemdraw.Drawing(show=False)
    drawing.config(unit=3, fontsize=12)

    battery = elm.Ic(
        pins=[
            elm.IcPin(name='Position 7: no wire', side='right', slot='9/9', anchorname='p7'),
            elm.IcPin(name='Position 6: Enable', side='right', slot='8/9', anchorname='enable'),
            elm.IcPin(name='Position 5: not used', side='right', slot='7/9', anchorname='p5'),
            elm.IcPin(name='Position 4: not used', side='right', slot='6/9', anchorname='p4'),
            elm.IcPin(name='Position 3: not used', side='right', slot='5/9', anchorname='p3'),
            elm.IcPin(name='Position 2: 1-Wire', side='right', slot='4/9', anchorname='data'),
            elm.IcPin(name='Position 1: not used', side='right', slot='3/9', anchorname='p1'),
            elm.IcPin(name='Main − terminal', side='right', slot='2/9', anchorname='negative'),
            elm.IcPin(name='Main + terminal', side='right', slot='1/9', anchorname='positive'),
        ],
        size=(8, BOX_HEIGHT),
        edgepadW=0.5,
        leadlen=0.5,
    )
    drawing.add(battery.right().at((0, 0)))

    enable = battery.absanchors['enable']
    board = elm.Ic(
        pins=[
            elm.IcPin(name='D6 (GPIO12)', side='left', slot='8/9', anchorname='d6'),
            elm.IcPin(name='3V3', side='left', slot='6/9', anchorname='v33'),
            elm.IcPin(name='D7 (GPIO13)', side='left', slot='4/9', anchorname='d7'),
            elm.IcPin(name='GND', side='left', slot='2/9', anchorname='gnd'),
        ],
        size=(6, BOX_HEIGHT),
        edgepadW=0.5,
        leadlen=0.5,
    )
    drawing.add(board.right().anchor('d6').at((enable[0] + WIRE_LENGTH, enable[1])))

    data = battery.absanchors['data']
    negative = battery.absanchors['negative']
    positive = battery.absanchors['positive']
    rail = board.absanchors['v33']
    pull_x = enable[0] + WIRE_LENGTH / 2

    drawing.add(elm.Line().at(enable).to(board.absanchors['d6']))
    drawing.add(elm.Line().at(data).to(board.absanchors['d7']))
    drawing.add(elm.Line().at(negative).to(board.absanchors['gnd']))
    drawing.add(elm.Line().at(rail).to((pull_x, rail[1])))
    drawing.add(elm.Dot().at((pull_x, rail[1])))
    drawing.add(elm.Resistor().at((pull_x, rail[1])).to((pull_x, enable[1])).label('R1\n4.7 kΩ', loc='bottom'))
    drawing.add(elm.Dot().at((pull_x, enable[1])))
    drawing.add(elm.Resistor().at((pull_x, rail[1])).to((pull_x, data[1])).label('R2\n4.7 kΩ', loc='top'))
    drawing.add(elm.Dot().at((pull_x, data[1])))

    load_x = enable[0] + 8.5
    drawing.add(elm.Line().at(positive).to((enable[0] + 1.5, positive[1])))
    drawing.add(elm.Button().right().length(2.5).label('SW1\nWake (optional)', loc='bottom'))
    drawing.add(elm.Resistor().right().length(3).label('R3 1 kΩ, 1 W', loc='bottom'))
    drawing.add(elm.Line().to((load_x, positive[1])))
    drawing.add(elm.Line().to((load_x, negative[1])))
    drawing.add(elm.Dot().at((load_x, negative[1])))

    middle_y = (enable[1] + positive[1]) / 2
    drawing.add(elm.Label().at((enable[0] - 6.8, middle_y)).label('Makita\nLXT 18 V\nbattery', halign='center'))
    board_x = board.absanchors['d6'][0] + 4.5
    drawing.add(elm.Label().at((board_x, middle_y)).label('Wemos\nD1 mini\n\nUSB to PC', halign='center'))

    add_numbering_panel(drawing=drawing, left=enable[0] - 8, top=positive[1] - 3.2)

    return drawing


def main() -> None:
    """Render the wiring drawing to an SVG file."""
    build_drawing().save(str(OUTPUT_FILE))


if __name__ == '__main__':
    main()
