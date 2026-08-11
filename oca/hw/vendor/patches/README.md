# verilog-ethernet patches — RETIRED 2026-08-12

**The Ethernet route is closed and this file is history.** The
Colorlight i9 v7.2 carries both B50612D PHYs on the module and routes
their MDI pairs to the SO-DIMM edge, but the RJ45 sockets and the
magnetics are on a carrier no kit sold with the module includes: there
is nothing to plug a cable into. See `SPEC.md`, PHASE 2. Nothing below
is work to do. It is kept because both patches were measured, because
the code they patch is still in the tree, and because the second one
carries a before/after table of what moving a comparison off a critical
path bought.

Local patches to the pinned `verilog-ethernet` submodule. The submodule
itself is never edited: `../vendor_patches.py` extracts the pinned commit
with `git archive`, applies these on top, and leaves the result in
`../build/verilog-ethernet/`, which is what `hw/syn/run_synth.py` and
`hw/sim/run_eth_mac.py` read. `git status` on the submodule stays clean,
and the tree the tools see is reproducible from the pin plus these files
and nothing else.

    .venv/bin/python hw/vendor/vendor_patches.py     # build or refresh it

Both runners refuse to work without it, and say which patch is missing.
That is the point of the mechanism rather than a nicety: both patches
change what the hardware does, so a build that quietly used the pinned
tree would produce a bitstream that cannot receive a frame and a timing
figure belonging to a different design.

Upstream is archived — the pin `77320a94` is "Add deprecation notice" —
so there is nowhere to send these. They are written to be reviewable
against upstream anyway: minimal, in the vendor's own style, and each
confined to the one construct at fault.

## verilog-ethernet-axis-adapter-upsize-tkeep.patch

`axis_adapter`'s upsize branch ignored `S_KEEP_ENABLE`. The parameter is
documented "if disabled, tkeep assumed to be 1'b1", and the bypass branch
honours the matching `M_KEEP_ENABLE` that way, but the upsize branch
copied `s_axis_tkeep` into the wide keep register whatever the parameter
said. `eth_mac_1g_fifo` sets `S_KEEP_ENABLE(0)` and ties that port to a
literal `0` (`eth_mac_1g_fifo.v:304`), so every beat of every received
frame arrived with `tkeep = 8'h00`.

It is fatal downstream and not cosmetic: `eth_axis_rx` computes its byte
count from `tkeep`, so with all zeros no byte is valid anywhere in the
receive path, and `oca_top` wires `mac_rx_tkeep` straight into it.

The patch adds one wire — `s_axis_tkeep_int`, all ones when the parameter
is disabled — and uses it in the three places the branch reads
`s_axis_tkeep`. A 60-byte frame now arrives as
`ff ff ff ff ff ff ff 0f`.

`test_rx_axis_tkeep_marks_the_valid_bytes_of_every_beat` is where that is
asserted, beat by beat, on four frame lengths.

## verilog-ethernet-axis-gmii-rx-fcs-off-crc-path.patch

`oca_top` places and routes at 102.59 MHz on `rgmii_rx_clk` against the
125 MHz a gigabit link requires. The critical path is 9.75 ns and it
splits in two: `crc_state` through the LFSR to `crc_next` is 4.71 ns and
has to close in one cycle, and the 32-bit FCS comparison after it is
5.04 ns and does not. Place and route cannot reach a 21.8% shortfall —
measured, the placer levers give 0.7% and `router2` diverges — so the
comparison has to come off that path.

The patch moves it one cycle later, where it reads the registered
`crc_state` instead of the combinational `crc_next`. That needs the last
payload byte held back by one cycle so the verdict still arrives with
`tlast`, which is `STATE_LAST` and the one new register, `gmii_rxd_d5`.
The four FCS bytes and the four `gmii_rx_er` stages have shifted by one
in that cycle too, so the comparison reads `d1..d4` where it read
`d0..d3`.

What must not move, and how each is checked in
`hw/sim/test_eth_mac.py`:

  * the verdict, for every frame good or bad — the whole suite is green
    with this patch absent and with it present, and four deliberately
    wrong versions of it are caught;
  * the frame on `m_axis`, byte for byte and **not one byte longer** —
    `delivered()` refuses a frame followed by any prefix of its own FCS,
    which is the failure this shape produces if the payload beat is not
    held back;
  * `error_bad_frame` against `error_bad_fcs` — a receive error over the
    FCS bytes is still a bad frame and not a bad FCS, on each of the four
    bytes separately.

The MII path is held to the same rule: `gmii_rxd_d5` shifts inside
`if (mii_odd)` with the rest of the delay line, so it stays in step with
a state machine that only advances on odd cycles. That path is not
exercised by any test here — this project ran the port at gigabit and
tied `mii_select` low — and it is reasoned, not measured.

### What it measured, on `oca_top`, seed 1, LFE5U-45F-6BG381C

                     before      after
    rgmii_rx_clk   102.59 MHz  115.77 MHz   still FAILS 125 MHz
    clk_tx         137.25 MHz  124.69 MHz   now FAILS 125 MHz
    clk_sys         49.99 MHz   52.16 MHz   ok
    clk25          496.03 MHz  486.85 MHz   ok
    TRELLIS_FF         16840       16849
    TRELLIS_COMB       17782       17802

**The FCS comparison is off the critical path, and the receive clock
still misses.** 9.75 ns became 8.64 ns, and what is in it now is not the
CRC at all: it is the receive frame FIFO's commit loop, from
`wr_ptr_commit_reg[3]` through the `drop_frame` logic and the adapter's
`m_axis_tlast` into `wr_ptr_sync_commit_reg` — 2.43 ns of logic and
6.20 ns of routing, spread over three placement columns. 125 MHz needs
8.00 ns, so what is left is a 7.4% shortfall in `axis_async_fifo`'s
frame-drop path, routing-dominated. No further lever was tried on it:
the route was retired on 2026-08-12 for want of a socket, not for want
of that 7.4%.

**`clk_tx` regressed and now fails by 0.25%.** Its critical path is
entirely inside `axis_gmii_tx` — `frame_ptr_reg` through `frame_reg` to
`gmii_txd_reg`, 8.02 ns, 2.41 ns logic and 5.61 ns routing — and neither
patch touches that module or the transmit width adapter, which is the
downsize branch. The nine flip-flops these patches add are all in the
receive path, so what moved `clk_tx` is where seed 1 put an unchanged
netlist once the receive path stopped dominating the placer's attention.
On a design already known to be congestion-limited rather than
area-limited, that is the expected shape of the result and not a change
in the transmit logic.
