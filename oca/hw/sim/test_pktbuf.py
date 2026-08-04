# SPDX-License-Identifier: MIT
"""Packet buffer: words come back at the word address they went in, the
counter tracks the write position in bytes, and the full flag fires at
BYTES.

The two banks are the reason the tests below name one: a clear, a count
and an out-of-range read must each stay inside the bank they were aimed
at. A clear that reaches the other bank truncates a packet in flight in
silence, and an out-of-range read that falls back to absolute word zero
hands back a neighbour's header."""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge, Timer

BYTES = 2048
WORDS = BYTES // 8

# The memory is zeroed out of reset one word per cycle, over both banks,
# and clr_busy is high for exactly that long.
CLEAR_CYCLES = 2 * WORDS


async def setup(dut, wait_clear: bool = True):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.wr_en.value = 0
    dut.wr_data.value = 0
    dut.wr_bytes.value = 0
    dut.wr_clear.value = 0
    dut.wr_bank.value = 0
    dut.rd_bank.value = 0
    dut.rd_addr.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    if not wait_clear:
        return
    # The clear owns the single write port while it runs, so a test that
    # started writing here would have its first words dropped.
    for _ in range(CLEAR_CYCLES + 1):
        await RisingEdge(dut.clk)


async def wait_clear_done(dut, limit: int = 4 * WORDS) -> int:
    """Cycles spent with clr_busy high, sampled before each edge.

    Returns once it has read low, one edge past that cycle so the caller
    is free to drive again.
    """
    for n in range(limit):
        await ReadOnly()
        done = int(dut.clr_busy.value) == 0
        await RisingEdge(dut.clk)
        if done:
            return n
    raise AssertionError(f"clr_busy still high {limit} cycles after reset")


def secret_word(bank: int, addr: int) -> int:
    """A non-zero word, distinct for every (bank, address) pair."""
    return 0x5EC5E700_00000000 | (bank << 20) | (addr + 1)


async def write_words(dut, words, bank: int = 0):
    """Write back to back, one (value, valid bytes) pair per cycle."""
    dut.wr_bank.value = bank
    for value, nbytes in words:
        dut.wr_data.value = value
        dut.wr_bytes.value = nbytes
        dut.wr_en.value = 1
        await RisingEdge(dut.clk)
    dut.wr_en.value = 0
    await RisingEdge(dut.clk)


async def write_word(dut, value: int, nbytes: int = 8, bank: int = 0):
    await write_words(dut, [(value, nbytes)], bank=bank)


async def write_bytes(dut, data: bytes, bank: int = 0):
    """Split into little-endian words; only the last one may be partial."""
    words = []
    for off in range(0, len(data), 8):
        chunk = data[off:off + 8]
        words.append((int.from_bytes(chunk.ljust(8, b"\x00"), "little"), len(chunk)))
    await write_words(dut, words, bank=bank)


async def read_word(dut, addr: int, bank: int = 0) -> int:
    dut.rd_bank.value = bank
    dut.rd_addr.value = addr
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    return int(dut.rd_data.value)


@cocotb.test()
async def test_write_then_read_back(dut):
    await setup(dut)
    rng = random.Random(0xB0FF)
    payload = bytes(rng.getrandbits(8) for _ in range(304))
    await write_bytes(dut, payload)
    assert int(dut.wr_count.value) == len(payload)
    for addr in (0, 1, 7, 8, 9, 37):
        got = await read_word(dut, addr)
        want = payload[addr * 8:addr * 8 + 8]
        assert got.to_bytes(8, "little") == want, f"word {addr}: {got:#018x}"


@cocotb.test()
async def test_clear_restarts_at_zero(dut):
    await setup(dut)
    await write_bytes(dut, b"first")
    dut.wr_clear.value = 1
    await RisingEdge(dut.clk)
    dut.wr_clear.value = 0
    await RisingEdge(dut.clk)
    assert int(dut.wr_count.value) == 0
    await write_bytes(dut, b"second")
    assert int(dut.wr_count.value) == 6
    w0 = await read_word(dut, 0)
    assert w0.to_bytes(8, "little")[:6] == b"second"


@cocotb.test()
async def test_full_flag_and_no_overrun(dut):
    await setup(dut)
    await write_bytes(dut, bytes(BYTES))
    assert int(dut.rd_full.value) == 1, "full not asserted at capacity"
    before = int(dut.wr_count.value)
    await write_word(dut, 0xFFFFFFFFFFFFFFFF, 8)
    assert int(dut.wr_count.value) == before, "counter moved past capacity"
    assert await read_word(dut, 0) == 0, "overrun wrapped and corrupted word 0"


@cocotb.test()
async def test_partial_final_word(dut):
    """A final word with 3 valid bytes advances wr_count by 3, not 8."""
    await setup(dut)
    await write_word(dut, int.from_bytes(b"abcdefgh", "little"), 8)
    await write_word(dut, int.from_bytes(b"xyz00000", "little"), 3)
    assert int(dut.wr_count.value) == 11, f"count {int(dut.wr_count.value)}"
    w0 = await read_word(dut, 0)
    assert w0.to_bytes(8, "little") == b"abcdefgh"
    w1 = await read_word(dut, 1)
    assert w1.to_bytes(8, "little")[:3] == b"xyz"


@cocotb.test()
async def test_read_past_capacity_is_clamped(dut):
    """An out-of-range word address reads word 0, never wraps into data.

    WORDS + 1 truncates to word 1 if the range check is dropped, which is
    what makes this assertion able to fail."""
    await setup(dut)
    await write_bytes(dut, b"headword" + b"\x11" * 8)
    got = await read_word(dut, WORDS + 1)
    assert got == int.from_bytes(b"headword", "little"), f"{got:#018x}"


@cocotb.test()
async def test_banks_are_independent(dut):
    """Two packets resident at once, neither visible to the other."""
    await setup(dut)
    a = b"bank-zero-payload" + bytes(range(40))
    b = b"bank-one-payload!" + bytes(range(40, 80))
    await write_bytes(dut, a, bank=0)
    assert int(dut.wr_count.value) == len(a)
    await write_bytes(dut, b, bank=1)
    assert int(dut.wr_count.value) == len(b), "bank 1 inherited bank 0's count"

    dut.wr_bank.value = 0
    await RisingEdge(dut.clk)
    assert int(dut.wr_count.value) == len(a), "bank 0's count was disturbed"

    for addr in range(0, len(a) // 8):
        got = await read_word(dut, addr, bank=0)
        assert got.to_bytes(8, "little") == a[addr * 8:addr * 8 + 8], \
            f"bank 0 word {addr}"
    for addr in range(0, len(b) // 8):
        got = await read_word(dut, addr, bank=1)
        assert got.to_bytes(8, "little") == b[addr * 8:addr * 8 + 8], \
            f"bank 1 word {addr}"


@cocotb.test()
async def test_clear_touches_only_its_own_bank(dut):
    """A clear aimed at one bank must not truncate the packet in the other.

    This is the failure with no symptom: the neighbour's byte count drops
    to zero mid-packet, its later writes land back over its own head, and
    its response comes back short under a tag that verifies.
    """
    await setup(dut)
    await write_bytes(dut, b"keep-me-whole-please", bank=0)
    await write_bytes(dut, b"discard", bank=1)

    dut.wr_bank.value = 1
    dut.wr_clear.value = 1
    await RisingEdge(dut.clk)
    dut.wr_clear.value = 0
    await RisingEdge(dut.clk)
    assert int(dut.wr_count.value) == 0, "bank 1 was not cleared"

    dut.wr_bank.value = 0
    dut.rd_bank.value = 0
    await RisingEdge(dut.clk)
    assert int(dut.wr_count.value) == 20, \
        f"bank 0 count is {int(dut.wr_count.value)}, want 20"
    w0 = await read_word(dut, 0, bank=0)
    assert w0.to_bytes(8, "little") == b"keep-me-", "bank 0 data disturbed"


@cocotb.test()
async def test_read_clamp_stays_inside_its_bank(dut):
    """An out-of-range address reads word 0 of the bank it was aimed at.

    Clamping to absolute word zero would answer with bank 0's first word,
    which is another packet's header: a bounds failure has to degrade to
    the reader's own bytes, never to its neighbour's.
    """
    await setup(dut)
    await write_bytes(dut, b"BANKZERO" + b"\x11" * 8, bank=0)
    await write_bytes(dut, b"BANKONE!" + b"\x22" * 8, bank=1)

    got = await read_word(dut, WORDS + 1, bank=1)
    assert got == int.from_bytes(b"BANKONE!", "little"), \
        f"clamped out of its own bank: {got:#018x}"
    got = await read_word(dut, WORDS + 1, bank=0)
    assert got == int.from_bytes(b"BANKZERO", "little"), f"{got:#018x}"


@cocotb.test()
async def test_counts_are_reported_per_side(dut):
    """wr_count follows the write bank and rd_count the read bank.

    Exposing one for both would either drop good writes or validate a
    length against the wrong packet.
    """
    await setup(dut)
    await write_bytes(dut, bytes(24), bank=0)
    await write_bytes(dut, bytes(56), bank=1)

    dut.wr_bank.value = 1
    dut.rd_bank.value = 0
    await RisingEdge(dut.clk)
    assert int(dut.wr_count.value) == 56, f"wr_count {int(dut.wr_count.value)}"
    assert int(dut.rd_count.value) == 24, f"rd_count {int(dut.rd_count.value)}"


@cocotb.test()
async def test_reset_zeroises_both_banks(dut):
    """Reset must wipe the stored bytes, not only the byte counts.

    The array holds the request and the response of whatever ran last:
    plaintext, ciphertext, and the 32 raw key bytes of a load-key
    command. Resetting the counters hides those bytes from the protocol
    and leaves every one of them in the block RAM.

    Every word is read back before the reset, so the check afterwards
    cannot pass on a buffer that was never written.
    """
    await setup(dut)
    for bank in (0, 1):
        await write_words(dut, [(secret_word(bank, a), 8)
                                for a in range(WORDS)], bank=bank)
        assert int(dut.wr_count.value) == BYTES, \
            f"bank {bank} stored {int(dut.wr_count.value)} bytes, want {BYTES}"
    for bank in (0, 1):
        for addr in range(WORDS):
            got = await read_word(dut, addr, bank=bank)
            assert got == secret_word(bank, addr), \
                f"bank {bank} word {addr} reads {got:#018x} before the reset"

    dut.rst_n.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst_n.value = 1
    for _ in range(CLEAR_CYCLES + 1):
        await RisingEdge(dut.clk)

    for bank in (0, 1):
        for addr in range(WORDS):
            got = await read_word(dut, addr, bank=bank)
            assert got == 0, \
                f"bank {bank} word {addr} still holds {got:#018x} after reset"


@cocotb.test()
async def test_clear_busy_spans_one_cycle_per_word(dut):
    """clr_busy is high out of reset and falls when the last word is done.

    One write port and one word per cycle, over both banks: any other
    number means either the clear stops short of the array or something
    downstream is held off longer than it has to be.
    """
    await setup(dut, wait_clear=False)
    await Timer(1, unit="ns")
    assert int(dut.clr_busy.value) == 1, "clr_busy is low out of reset"
    n = await wait_clear_done(dut)
    assert n == CLEAR_CYCLES, \
        f"clr_busy fell after {n} cycles, want {CLEAR_CYCLES} (2 * {WORDS})"


@cocotb.test()
async def test_writes_during_the_clear_do_not_land(dut):
    """A write offered while clr_busy is high must leave no trace.

    The clear owns the write port, so the data cannot land; the byte
    count must not move either. A count that advanced over dropped data
    is the failure with no symptom -- the packet is answered at its full
    length out of words the buffer never wrote.
    """
    await setup(dut, wait_clear=False)
    await write_words(dut, [(secret_word(0, a), 8) for a in range(16)])
    left = await wait_clear_done(dut)
    assert left > 0, "the clear was already over: nothing was offered against it"
    assert int(dut.wr_count.value) == 0, \
        f"wr_count is {int(dut.wr_count.value)}: a write moved it during the clear"
    for addr in range(16):
        got = await read_word(dut, addr)
        assert got == 0, \
            f"word {addr} holds {got:#018x}: a write landed during the clear"
