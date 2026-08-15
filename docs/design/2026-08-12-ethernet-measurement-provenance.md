# Where the Ethernet numbers came from, after the code went

**Status: provenance record. Written 2026-08-12, on the branch that
deleted the Ethernet route.**

`SPEC.md` still argues, in the present tense, that one Ethernet port
costs 8422 LUTs and that this is why two cores with two ports do not fit
on an LFE5U-45F. That argument survives the removal of the code: it is
about what a port would cost if this project ever built one on different
hardware, not about anything in the tree. This document exists so the
figure stays falsifiable after the sources it was taken from left the
working tree.

It also corrects a mistake this branch made in draft. The two synthesis
probes under `oca/hw/rtl/vendor/` were described, in the plan for this
removal, as "the measurement apparatus for the 8422-LUT figure". **They
are not, and re-running them is what showed it.**

## What the 8422 actually is

It is a **`TRELLIS_COMB` count from a nextpnr out-of-context run**, not a
yosys cell count. Three things establish that and none of them is a
guess:

- 8422 / 43848 = 19.21%, and 43848 is the device's `TRELLIS_COMB`
  capacity as nextpnr reports it. Every other LUT percentage in this
  project divides by the same number: 13043/43848 = 29.7% for
  `oca_uart_crypto`, 24602/43848 = 56.1% for `oca_dual` (both on yosys
  `41a4b5a03`; on `f77ddfb87` they read 13030 and 24621 and round the
  same), 41446/43848 = 94.5% for the two-cores-two-ports row.
  `oca/hw/syn/README.md:957` heads the column
  `TRELLIS_COMB | of 43848` outright.
- `docs/design/2026-08-05-ethernet-integration.md:183-186` quotes an
  **Fmax** beside the variant figure — "Fmax falls from 81 to 72.9 MHz"
  for `UDP_CHECKSUM_GEN_ENABLE=0`. yosys `stat` produces no Fmax.
  nextpnr does.
- Commit `d4ee09f` (2026-08-05), where 7147 first appears, says
  "measured **out-of-context** on this toolchain" — which is nextpnr's
  `--out-of-context`, the same flag `run_synth.py` passes for every
  design without an `.lpf`.

## The provenance gap, stated rather than papered over

**The exact invocation that produced 7147 and 1214 is not recorded
anywhere in this repository.** `oca_udp_complete_64_probe` was never a
`DESIGNS` entry in `run_synth.py` — checked with
`git log -S` over the whole history — so the figure did not come through
the project's own synthesis flow, and no script in the tree reproduces
it. What is recorded is the result, in `docs/RECORD.md` and in the
2026-08-05 design document, and the sources it was taken from.

So the honest position is: **the sources are preserved and the method is
only partially recorded.** Anyone re-deriving 8422 will have to
reconstruct the nextpnr out-of-context run themselves. That was already
true before this branch; deleting the code did not cause it, and this
document is where the project stops pretending otherwise.

## What the probes are, and what they measured

The probes exist so each wrapper can be pushed through the exact
frontend combination a real build uses — vendor Verilog through
`read_verilog`, our SystemVerilog through `read_slang` — because a
wrapper that only passes `read_verilog` on its own has not been tested.
Their headers say "not part of any build". They report area as a side
effect, **before** nextpnr packs anything, so their numbers are not
comparable to a `TRELLIS_COMB` figure.

Both were re-run at `fd3059c` before deleting them, on yosys 0.67+
(git sha1 `41a4b5a03`), with the commands transcribed below.

| cell | `oca_eth_mac_1g_fifo_64_probe` | `oca_udp_complete_64_probe` |
|---|---|---|
| LUT4 | 1061 | 3801 |
| PFUMX | 236 | 514 |
| L6MUX21 | 53 | 43 |
| CCU2C | 56 | 302 |
| TRELLIS_FF | 741 | 4284 |
| TRELLIS_DPR16X4 | — | 121 |
| DP16KD | 6 | 3 |
| `$scopeinfo` | 13 | 25 |
| **total cells** | **2166** | **9093** |
| exit, wall time | 0, 111.65 s | 0, 3578.16 s |

Both columns add up to their totals. `$scopeinfo` is not hardware; it is
listed because yosys counts it in the total it prints. The UDP stack's
121 `TRELLIS_DPR16X4` are LUT fabric spent as distributed RAM, which is
the same effect `docs/design/2026-08-05-ethernet-integration.md:184-186`
notes for the checksum-off variant: area that a block-RAM column does
not show.

**Neither number is the published one, and that is expected**: 1214 is
`eth_mac_1g_rgmii_fifo` while this probe measures
`oca_eth_mac_1g_fifo_64`, the same wrapper without the `rgmii_phy_if`
the ~61 accounts for; and both figures are pre-pack yosys cells against
a post-pack `TRELLIS_COMB` published figure. **7147 and 1214 are not
reproduced here and nothing in this document should be read as
confirming them.**

**Budget properly if you re-run these.** The `timeout 900` written into
each probe's own header is wrong for the UDP one on this toolchain. At
900 s it exits 124 inside the standalone `hierarchy -check` pass while
deriving `lfsr`, never reaching `synth_ecp5` at all — its log contains
no `SYNTH_LATTICE` pass, where the MAC probe's log reaches it as step
12. At a 5400 s ceiling it completes in 3578 s, and 3502 s of that —
98% of the run — is `hierarchy`. `lfsr.v` is the parameterised CRC and
scrambler generator the checksum path pulls in, and deriving it is where
the time goes. Peak memory was 2.5 GB.

## Provenance of the sources

| what | value |
|---|---|
| repository commit | `fd3059ca7784acd5b3f3c72ed5083cd68de4df6e` |
| `verilog-ethernet` pin | `77320a9471d19c7dd383914bc049e02d9f4f1ffb` |
| yosys | 0.67+, git sha1 `41a4b5a03` |

**Upstream is deprecated, not archived.** The repository is public and
still serves `77320a94` as the head of master; what its author did was
deprecate it in favour of `taxi`, and the pinned commit is literally its
"Add deprecation notice". Nothing has moved there since 2025-02-27. That
is not a copy gone, but it is a copy this project does not control, so
the submodule's object store under
`.git/modules/oca/hw/vendor/verilog-ethernet` (6.3 MB) was deliberately
**not** purged when the submodule was deregistered: 6.3 MB is the price
of not depending on a deprecated third-party repository staying up.

To get the sources back:

```sh
git checkout fd3059ca7784acd5b3f3c72ed5083cd68de4df6e
git submodule update --init oca/hw/vendor/verilog-ethernet
```

## The commands

Both are transcribed from the headers of the deleted probe files,
`oca/hw/rtl/vendor/oca_eth_mac_1g_fifo_64_probe.sv` and
`oca_udp_complete_64_probe.sv`, and are run from the repository root.
Note that they read the **pinned** submodule, not the patched tree under
`oca/hw/vendor/build/` that `run_synth.py` redirected builds to.

The MAC:

```sh
tools/yosys/bin/yosys -p "read_verilog \
  -Ioca/hw/vendor/verilog-ethernet/rtl \
  -Ioca/hw/vendor/verilog-ethernet/lib/axis/rtl \
  oca/hw/vendor/verilog-ethernet/rtl/eth_mac_1g_fifo.v \
  oca/hw/vendor/verilog-ethernet/rtl/eth_mac_1g.v \
  oca/hw/vendor/verilog-ethernet/rtl/axis_gmii_rx.v \
  oca/hw/vendor/verilog-ethernet/rtl/axis_gmii_tx.v \
  oca/hw/vendor/verilog-ethernet/rtl/lfsr.v \
  oca/hw/vendor/verilog-ethernet/lib/axis/rtl/axis_async_fifo_adapter.v \
  oca/hw/vendor/verilog-ethernet/lib/axis/rtl/axis_async_fifo.v \
  oca/hw/vendor/verilog-ethernet/lib/axis/rtl/axis_adapter.v \
  oca/hw/rtl/vendor/oca_eth_mac_1g_fifo_64.v; \
  read_slang --top oca_eth_mac_1g_fifo_64_probe \
  oca/hw/rtl/vendor/oca_eth_mac_1g_fifo_64_probe.sv; \
  hierarchy -check -top oca_eth_mac_1g_fifo_64_probe; \
  synth_ecp5 -top oca_eth_mac_1g_fifo_64_probe; stat"
```

The UDP stack is the same shape over `udp_complete_64.v`,
`ip_complete_64.v`, `ip_64.v`, `ip_eth_rx_64.v`, `ip_eth_tx_64.v`,
`ip_arb_mux.v`, `arp.v`, `arp_cache.v`, `arp_eth_rx.v`, `arp_eth_tx.v`,
`eth_arb_mux.v`, `udp_64.v`, `udp_ip_rx_64.v`, `udp_ip_tx_64.v`,
`udp_checksum_gen_64.v`, `lfsr.v`, `axis_fifo.v`, `arbiter.v`,
`priority_encoder.v` and `oca_udp_complete_64.v`, with
`oca_udp_complete_64_probe` as the top.

## The honest residue

Two pieces of the Ethernet work stayed in the tree on purpose, and both
are recorded in `docs/STATUS.md`:

- `oca_clkrst.sv` keeps its B50612D PHY reset sequencer, with the tests
  that hold it to the datasheet's Table 86 minimums. It sits inside
  `oca_pll`, a design already measured on silicon; reopening it would
  risk a validated design for no functional gain. Its comments still
  name `oca_rgmii.sv`, `oca_top`, `verilog-ethernet`,
  `eth_mac_1g_fifo` and `udp_complete_64`, all deleted;
  `docs/STATUS.md` lists the lines and declares this a bounded,
  recorded limitation rather than an oversight.
- `oca/hw/syn/colorlight_i9.lpf` is kept verbatim, RGMII and PHY pinout
  included. 270 of its 327 lines are ECP5 analysis that surviving code
  cites, and `run_synth.py` cites it by line number. No surviving design
  uses it as a constraint file.
