# SPDX-License-Identifier: MIT
"""The properties worth paying gate-level simulation for.

Run by run_proto_gate.py against an oca_core whose oca_proto is a
synthesised ECP5 netlist. Everything here decides whether plaintext
leaves the device; nothing here is white-box, because synth_ecp5
flattens oca_proto and its registers do not exist under those names in
the netlist, so the monitors in test_oca_core.py and test_attack.py
cannot be armed.

The plumbing is imported from test_oca_core.py, so the packets on the
wire are the ones the RTL suites send.
"""

import cocotb

from aead_model import aead_encrypt
from proto_model import (ST_AUTH_FAIL, ST_OK, build_load_key, build_open,
                         build_seal)
from test_oca_core import KEY, NONCE, command, setup


@cocotb.test()
async def test_gate_seal_and_open_round_trip(dut):
    """A mapped protocol engine that answers correctly at all.

    Before any security property: the netlist has to parse a header,
    reach the key store, drive the engine and build a response. A
    mapper defect anywhere in that path shows up here as a wrong status
    or a wrong body.
    """
    await setup(dut, monitor=False)
    rsp = await command(dut, build_load_key(0x01, 0, KEY))
    assert rsp["status"] == ST_OK, f"load_key status {rsp['status']}"

    aad, msg = b"gate", b"the netlist is the design that ships"
    want_ct, want_tag = aead_encrypt(KEY, NONCE, aad, msg)
    sealed = await command(dut, build_seal(0x02, 0, NONCE, aad, msg))
    assert sealed["status"] == ST_OK, f"seal status {sealed['status']}"
    assert sealed["body"] == want_tag + want_ct, "netlist ciphertext mismatch"

    opened = await command(dut, build_open(0x03, 0, NONCE, aad, want_ct,
                                           want_tag))
    assert opened["status"] == ST_OK, f"open status {opened['status']}"
    assert opened["body"] == msg, "netlist plaintext mismatch"


@cocotb.test()
async def test_gate_every_tag_byte_is_compared(dut):
    """The tag comparison, at its full width, in cells.

    This is the check no other stage of the flow can make. The
    comparison is combinational, so the flip-flop floors in
    hw/syn/run_synth.py cannot see it, and Verilator never runs yosys, so
    the RTL suites cannot either. A mapper turning part of that equality
    into a constant is precisely what stock yosys did to the key store's
    bounds check.

    Sixteen forgeries, one flipped bit per tag byte with the bit
    position rotating along, then the intact tag so that a comparison
    stuck at false is not mistaken for a working one.
    """
    await setup(dut, monitor=False)
    await command(dut, build_load_key(0x10, 3, KEY))
    msg = b"sixteen bytes, all of them in the netlist"
    sealed = await command(dut, build_seal(0x11, 3, NONCE, b"", msg))
    assert sealed["status"] == ST_OK, f"seal status {sealed['status']}"
    tag, ct = sealed["body"][:16], sealed["body"][16:]

    for i in range(16):
        forged = bytearray(tag)
        forged[i] ^= 1 << (i % 8)
        rsp = await command(
            dut, build_open(0x20 + i, 3, NONCE, b"", ct, bytes(forged)))
        assert rsp["body"] == b"", \
            f"tag byte {i}, bit {i % 8}: plaintext leaked from the netlist"
        assert rsp["status"] == ST_AUTH_FAIL, \
            (f"tag byte {i}, bit {i % 8} flipped and the netlist answered "
             f"{rsp['status']}: that byte is not compared in cells")

    opened = await command(dut, build_open(0x30, 3, NONCE, b"", ct, tag))
    assert opened["status"] == ST_OK, "the intact tag stopped verifying"
    assert opened["body"] == msg, "the intact tag returned the wrong plaintext"
