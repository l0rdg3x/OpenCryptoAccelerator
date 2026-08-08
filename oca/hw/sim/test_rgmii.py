# SPDX-License-Identifier: MIT
"""RGMII front end: two nibbles to a byte, and which edge carries which.

What this testbench can see is the nibble order, the control encoding and
the in-band status decode, which is also all the behavioural DDR branch
models. It runs at SIMULATION=1 for that reason: IDDRX1F and ODDRX1F
reach a simulator only as the blackboxes ecp5_prims.sv declares, so a
build on the ECP5 branch elaborates and captures nothing. The tap counts,
the primitive mapping and the PHY's own delay are outside any simulation
of this layer and stay outside until the board is on the bench.

Nibble order is the property worth the testbench. RGMII carries the low
nibble of a byte with the rising edge and the high nibble with the
falling one, the same way in both directions; exchanging the two is a
link that comes up, passes traffic and corrupts every byte of it, with
nothing upstream reading the halves separately to notice. So both order
tests drive bytes whose two nibbles always differ, and the two frame
tests drive 72 more of them.

The driver and the monitor are written here rather than taken from
cocotbext-eth. The protocol is four data lines, one control line and two
edges; the project pins what it depends on and this is not worth three
more packages.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge, Timer

# 125 MHz, the 1000BASE-T RGMII rate. The behavioural branch captures on
# edges and models no delay, so no assertion here depends on the number --
# what it has to be is a period whose quarters are whole nanoseconds,
# which is the resolution the runner leaves Verilator on.
CLK_PERIOD_NS = 8
HALF_NS = CLK_PERIOD_NS // 2

# A signal is driven SETUP_NS before the edge that captures it and read
# SAMPLE_NS after the edge that moved it, so neither the driver nor the
# monitor ever touches a value in the same time step as the edge it
# belongs to. Both have to fit in a half period with room left over:
# SETUP_NS + SAMPLE_NS < HALF_NS.
SETUP_NS = 1
SAMPLE_NS = 2

# The 72 bytes a minimum-size frame occupies on the wire: seven 0x55, the
# 0xD5 start delimiter, then 64 bytes of frame. Nothing in this layer
# parses a frame -- it moves nibbles -- so the body is a fixed pattern
# rather than a capture, built so the two nibbles of all 64 of its bytes
# differ and a nibble swap therefore changes every one of them.
#
# The preamble deliberately does not have that property and cannot be
# given it: 0x55 is symmetric, so a swapped link carries seven preamble
# bytes that still look exactly right. That is the first reason this
# defect survives a bring-up, and it is why the body is what the frame
# tests compare.
PREAMBLE = b"\x55" * 7 + b"\xd5"
FRAME_BODY = bytes(((i + 1) & 0x0F) | (((i + 9) & 0x0F) << 4) for i in range(64))
WIRE_FRAME = PREAMBLE + FRAME_BODY


class TxSymbol:
    """One transmitted byte-time, kept as its two halves.

    Which nibble sat on which edge is the whole question on this side, so
    the halves are recorded as they were read and the byte is derived from
    them. That is what lets an assertion name the edge that carried the
    wrong nibble, instead of reporting only that some byte came out wrong.
    """

    def __init__(self, rise_nib: int, fall_nib: int, rise_ctl: int, fall_ctl: int):
        self.rise_nib = rise_nib
        self.fall_nib = fall_nib
        self.rise_ctl = rise_ctl
        self.fall_ctl = fall_ctl

    @property
    def byte(self) -> int:
        return (self.fall_nib << 4) | self.rise_nib

    @property
    def tx_en(self) -> int:
        return self.rise_ctl

    @property
    def tx_er(self) -> int:
        return self.rise_ctl ^ self.fall_ctl

    def __repr__(self) -> str:
        return (f"TxSymbol(rise={self.rise_nib:#03x}/{self.rise_ctl}, "
                f"fall={self.fall_nib:#03x}/{self.fall_ctl})")


def status_nibble(link_up: int, speed: int, full_duplex: int) -> int:
    """In-band status as it rides the four data lines during the gap."""
    return (full_duplex << 3) | (speed << 1) | link_up


def status_symbol(link_up: int, speed: int, full_duplex: int):
    """A gap symbol carrying that status, its complement on the falling half.

    Only the rising nibble is in-band status. Putting the complement of it
    on the falling one is what makes a decoder that read the wrong half
    report the opposite of every field, instead of something that might
    coincide with the right answer for some of them.
    """
    nib = status_nibble(link_up, speed, full_duplex)
    return ((~nib & 0x0F) << 4) | nib, 0, 0


def status_of(dut):
    return (int(dut.link_up.value),
            int(dut.link_speed.value),
            int(dut.link_full_duplex.value))


def gmii_rx_sample(dut):
    """The receive side as a GMII sink would read it: byte, RX_DV, RX_ER."""
    return (int(dut.gmii_rxd.value),
            int(dut.gmii_rx_dv.value),
            int(dut.gmii_rx_er.value))


async def setup(dut):
    """Both clocks running, pads idle, reset released."""
    cocotb.start_soon(Clock(dut.rgmii_rx_clk, CLK_PERIOD_NS, unit="ns").start())
    cocotb.start_soon(Clock(dut.gmii_tx_clk, CLK_PERIOD_NS, unit="ns").start())
    dut.rgmii_rxd.value = 0
    dut.rgmii_rx_ctl.value = 0
    dut.gmii_txd.value = 0
    dut.gmii_tx_en.value = 0
    dut.gmii_tx_er.value = 0
    # No delay element to move in this branch; tied the way the top level
    # ties them to leave the taps where the parameter put them.
    dut.dly_loadn.value = 1
    dut.dly_move.value = 0
    dut.dly_direction.value = 0
    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.rgmii_rx_clk)
    dut.rst_n.value = 1
    await RisingEdge(dut.rgmii_rx_clk)


async def rx_drive(dut, symbols):
    """Drive (byte, dv, er) onto the receive pads, one per clock period.

    The low nibble and RX_DV go out ahead of the rising edge, the high
    nibble and RX_DV xor RX_ER ahead of the falling one. Returns what the
    DUT presented on GMII for each symbol, in order.

    Sampling runs one period behind the driving, in the window between the
    falling edge that completes a byte and the rising edge that starts the
    next -- which is where a GMII sink clocked on gmii_rx_clk reads it.

    The pads are parked afterwards on the last symbol's rising half rather
    than left holding its falling one. That is not tidiness: the in-band
    status register captures whenever both control halves are low, so a
    pad left holding an arbitrary nibble across the gap between two calls
    would be latched as a link status. Parking on the rising half makes
    those edges re-capture what the caller just drove, which is a no-op.
    """
    got = []
    await RisingEdge(dut.rgmii_rx_clk)
    await Timer(CLK_PERIOD_NS - SETUP_NS, unit="ns")
    for data, dv, er in symbols:
        # The byte for the symbol driven one period ago; on the first pass
        # it is whatever preceded this call, and it is dropped below.
        got.append(gmii_rx_sample(dut))
        dut.rgmii_rxd.value = data & 0x0F
        dut.rgmii_rx_ctl.value = dv
        await Timer(HALF_NS, unit="ns")
        dut.rgmii_rxd.value = (data >> 4) & 0x0F
        dut.rgmii_rx_ctl.value = dv ^ er
        await Timer(HALF_NS, unit="ns")
    got.append(gmii_rx_sample(dut))
    if symbols:
        data, dv, _ = symbols[-1]
        dut.rgmii_rxd.value = data & 0x0F
        dut.rgmii_rx_ctl.value = dv
    return got[1:]


async def tx_drive(dut, symbols):
    """Drive (byte, tx_en, tx_er) on GMII and read the pads back.

    A byte registered on the rising edge holds the pads for the whole
    period: the low nibble while rgmii_tx_clk is high, the high nibble
    while it is low. Both are read at the middle of their half, which is
    where the receiving PHY's delayed clock lands.
    """
    got = []
    await RisingEdge(dut.gmii_tx_clk)
    await Timer(CLK_PERIOD_NS - SETUP_NS, unit="ns")
    for data, en, er in symbols:
        dut.gmii_txd.value = data
        dut.gmii_tx_en.value = en
        dut.gmii_tx_er.value = er
        await Timer(SETUP_NS + SAMPLE_NS, unit="ns")
        rise_nib = int(dut.rgmii_txd.value)
        rise_ctl = int(dut.rgmii_tx_ctl.value)
        await Timer(HALF_NS, unit="ns")
        fall_nib = int(dut.rgmii_txd.value)
        fall_ctl = int(dut.rgmii_tx_ctl.value)
        got.append(TxSymbol(rise_nib, fall_nib, rise_ctl, fall_ctl))
        await Timer(HALF_NS - SETUP_NS - SAMPLE_NS, unit="ns")
    return got


@cocotb.test()
async def test_receive_assembles_the_byte_from_the_right_halves(dut):
    """gmii_rxd is {falling nibble, rising nibble}, in that order.

    Every byte below has two different nibbles, so a design that took the
    rising edge for the high half would deliver a different value for all
    ten of them rather than for a lucky subset.
    """
    await setup(dut)
    payload = bytes([0x12, 0x34, 0xAB, 0xCD, 0xF0, 0x0F, 0x5A, 0xA5, 0x01, 0x80])
    got = await rx_drive(dut, [(b, 1, 0) for b in payload])

    assert bytes(byte for byte, _, _ in got) == payload, \
        "received " + " ".join(f"{byte:02x}" for byte, _, _ in got) + \
        ", drove " + " ".join(f"{b:02x}" for b in payload)
    for i, (_, dv, er) in enumerate(got):
        assert (dv, er) == (1, 0), f"byte {i}: rx_dv={dv} rx_er={er}, want 1/0"


@cocotb.test()
async def test_receive_decodes_dv_and_er_from_the_two_control_halves(dut):
    """RX_DV is the rising half, RX_ER the two halves exclusive-ored.

    All four combinations, because either half on its own agrees with the
    right answer half the time and not on the same half: reading RX_DV off
    the falling line is right for (0,0) and (1,0), reading RX_ER off the
    falling line alone is right for (0,0) and (0,1).
    """
    await setup(dut)
    cases = [(0, 0), (1, 0), (0, 1), (1, 1)]
    data = [0x12, 0x34, 0x56, 0x78]
    got = await rx_drive(dut, [(d, dv, er) for d, (dv, er) in zip(data, cases)])

    for (dv, er), d, (byte, got_dv, got_er) in zip(cases, data, got):
        assert (got_dv, got_er) == (dv, er), \
            f"drove dv={dv} er={er}, read dv={got_dv} er={got_er}"
        assert byte == d, f"data disturbed by dv={dv} er={er}: {byte:#04x}"


@cocotb.test()
async def test_transmit_puts_the_low_nibble_on_the_rising_edge(dut):
    """The pads carry {3:0} while the clock is high and {7:4} while it is low.

    Asserted on the two halves rather than only on the byte they
    reassemble to, so a failure names the edge that carried the wrong
    nibble. All ten bytes have two different nibbles, which is what makes
    a swap visible in the first place.
    """
    await setup(dut)
    payload = bytes([0x12, 0x34, 0xAB, 0xCD, 0xF0, 0x0F, 0x5A, 0xA5, 0x01, 0x80])
    got = await tx_drive(dut, [(b, 1, 0) for b in payload])

    for i, (b, sym) in enumerate(zip(payload, got)):
        assert sym.rise_nib == b & 0x0F, \
            f"byte {i} ({b:#04x}): rising edge carries {sym.rise_nib:#03x}, " \
            f"want the low nibble {b & 0x0F:#03x}"
        assert sym.fall_nib == b >> 4, \
            f"byte {i} ({b:#04x}): falling edge carries {sym.fall_nib:#03x}, " \
            f"want the high nibble {b >> 4:#03x}"
        assert sym.byte == b, f"byte {i}: {sym} reassembles to {sym.byte:#04x}"


@cocotb.test()
async def test_transmit_encodes_dv_and_er_on_the_control_halves(dut):
    """tx_ctl is TX_EN rising, TX_EN xor TX_ER falling. All four combinations."""
    await setup(dut)
    cases = [(0, 0), (1, 0), (0, 1), (1, 1)]
    data = [0x12, 0x34, 0x56, 0x78]
    got = await tx_drive(dut, [(d, en, er) for d, (en, er) in zip(data, cases)])

    for (en, er), d, sym in zip(cases, data, got):
        assert sym.rise_ctl == en, \
            f"tx_en={en} tx_er={er}: rising tx_ctl is {sym.rise_ctl}, want {en}"
        assert sym.fall_ctl == (en ^ er), \
            f"tx_en={en} tx_er={er}: falling tx_ctl is {sym.fall_ctl}, " \
            f"want {en ^ er}"
        assert sym.byte == d, f"data disturbed by en={en} er={er}: {sym}"


@cocotb.test()
async def test_the_clocks_pass_through(dut):
    """rgmii_tx_clk follows gmii_tx_clk, gmii_rx_clk follows rgmii_rx_clk.

    Both are wires in this branch, and both are the kind of wire whose
    absence is invisible: a transmit clock stuck at a level clocks nothing
    at the PHY while every data assertion in this file still passes, since
    the data is read against the GMII clock the testbench drives.
    """
    await setup(dut)
    for _ in range(4):
        await RisingEdge(dut.gmii_tx_clk)
        await Timer(SAMPLE_NS, unit="ns")
        assert int(dut.rgmii_tx_clk.value) == 1, \
            "rgmii_tx_clk is low while gmii_tx_clk is high"
        await FallingEdge(dut.gmii_tx_clk)
        await Timer(SAMPLE_NS, unit="ns")
        assert int(dut.rgmii_tx_clk.value) == 0, \
            "rgmii_tx_clk is high while gmii_tx_clk is low"

    for _ in range(4):
        await RisingEdge(dut.rgmii_rx_clk)
        await Timer(SAMPLE_NS, unit="ns")
        assert int(dut.gmii_rx_clk.value) == 1, \
            "gmii_rx_clk is low while rgmii_rx_clk is high"
        await FallingEdge(dut.rgmii_rx_clk)
        await Timer(SAMPLE_NS, unit="ns")
        assert int(dut.gmii_rx_clk.value) == 0, \
            "gmii_rx_clk is high while rgmii_rx_clk is low"


@cocotb.test()
async def test_in_band_status_is_captured_in_the_gap(dut):
    """Link, speed and duplex off the data lines while both halves are low.

    Three speeds, both duplex values, link up and link down. The falling
    half of every symbol carries the complement of the status nibble, so a
    decode that read the wrong half would invert every field.
    """
    await setup(dut)
    cases = [
        (1, 0b00, 0),   # up, 10 Mbps, half duplex
        (1, 0b01, 1),   # up, 100 Mbps, full duplex
        (1, 0b10, 1),   # up, 1000 Mbps, full duplex
        (1, 0b10, 0),   # up, 1000 Mbps, half duplex
        (0, 0b00, 0),   # down
    ]
    for link_up, speed, full_duplex in cases:
        # Three gap symbols: the register samples one byte-time behind, so
        # two of the three land on the value under test.
        await rx_drive(dut, [status_symbol(link_up, speed, full_duplex)] * 3)
        assert status_of(dut) == (link_up, speed, full_duplex), \
            f"drove up={link_up} speed={speed:#04b} fd={full_duplex}, " \
            f"read {status_of(dut)}"


@cocotb.test()
async def test_in_band_status_is_not_captured_during_a_frame(dut):
    """A byte with data valid must leave the link registers alone.

    The negative half of the pair, and it is written so it can fail: the
    gap ahead of the frame establishes every status bit set, and every
    rising nibble in the frame is zero. Drop the both-halves-low guard and
    the registers read back all zeroes instead.
    """
    await setup(dut)
    live = (1, 0b11, 1)
    await rx_drive(dut, [status_symbol(*live)] * 3)
    assert status_of(dut) == live, \
        f"nothing was captured to defend: status is {status_of(dut)}"

    # tx_en high puts both control halves at 1, which is the gap condition
    # failing on both of them at once.
    await rx_drive(dut, [(0x00, 1, 0)] * 8)
    assert status_of(dut) == live, \
        f"a frame moved the link status to {status_of(dut)}, want {live}"

    # And an errored byte, where the two halves differ rather than both
    # being high -- the other way the guard can be got wrong.
    await rx_drive(dut, [(0x00, 0, 1)] * 8)
    assert status_of(dut) == live, \
        f"an rx_er byte moved the link status to {status_of(dut)}, want {live}"


@cocotb.test()
async def test_reset_clears_the_link_status(dut):
    """rst_n low zeroes all three registers, and holds them there.

    Asserted between two clock edges and read before the next one, so it
    is the asynchronous branch being measured and not a clear that a
    synchronous reset would have reached anyway. The pads stay parked on
    the status nibble with both control halves low, which makes every edge
    of the loop one the capture would otherwise have taken: what it
    asserts is that the reset wins each of them, not that the registers
    were cleared once.
    """
    await setup(dut)
    live = (1, 0b11, 1)
    await rx_drive(dut, [status_symbol(*live)] * 3)
    assert status_of(dut) == live, \
        f"nothing was captured to clear: status is {status_of(dut)}"

    await RisingEdge(dut.rgmii_rx_clk)
    await Timer(SETUP_NS, unit="ns")
    assert status_of(dut) == live, f"status is {status_of(dut)} before the reset"
    dut.rst_n.value = 0
    await Timer(SETUP_NS, unit="ns")
    assert status_of(dut) == (0, 0b00, 0), \
        f"reset left the status at {status_of(dut)} with no edge in between"
    for _ in range(3):
        await RisingEdge(dut.rgmii_rx_clk)
        await Timer(SAMPLE_NS, unit="ns")
        assert status_of(dut) == (0, 0b00, 0), \
            f"the status came back to {status_of(dut)} while reset was held"


@cocotb.test()
async def test_a_whole_frame_arrives_byte_for_byte(dut):
    """Seven preamble bytes, the delimiter and 64 more, with the gap either side.

    The bytes are the point and rx_dv either side of them is what pins
    where the frame starts and stops. A byte-time of extra pipeline
    anywhere in the receive path moves the whole captured window instead
    of corrupting anything in it, and only an assertion on the edges of
    that window can see it.
    """
    await setup(dut)
    gap = [status_symbol(1, 0b10, 1)] * 4
    got = await rx_drive(dut, gap + [(b, 1, 0) for b in WIRE_FRAME] + gap)

    dv = [d for _, d, _ in got]
    assert dv == [0] * 4 + [1] * len(WIRE_FRAME) + [0] * 4, \
        f"rx_dv window is {''.join(str(d) for d in dv)}"
    frame = bytes(byte for byte, _, _ in got[4:4 + len(WIRE_FRAME)])
    assert frame == WIRE_FRAME, "wire byte " + str(
        next(i for i in range(len(WIRE_FRAME)) if frame[i] != WIRE_FRAME[i])) + \
        " is the first to differ"
    assert all(er == 0 for _, _, er in got), "rx_er asserted on a clean frame"


@cocotb.test()
async def test_a_whole_frame_leaves_byte_for_byte(dut):
    """The same 72 bytes out through the transmit side, halves checked apart."""
    await setup(dut)
    gap = [(0x00, 0, 0)] * 4
    got = await tx_drive(dut, gap + [(b, 1, 0) for b in WIRE_FRAME] + gap)

    en = [sym.tx_en for sym in got]
    assert en == [0] * 4 + [1] * len(WIRE_FRAME) + [0] * 4, \
        f"tx_ctl window is {''.join(str(e) for e in en)}"
    for i, (b, sym) in enumerate(zip(WIRE_FRAME, got[4:])):
        assert (sym.rise_nib, sym.fall_nib) == (b & 0x0F, b >> 4), \
            f"wire byte {i} ({b:#04x}) went out as {sym}"
    assert all(sym.tx_er == 0 for sym in got), "tx_ctl encoded an error"
