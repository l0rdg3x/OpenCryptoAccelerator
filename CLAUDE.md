# CLAUDE.md — OpenCrypto Accelerator

The canonical project document for agents is **`AGENTS.md`**: it carries
the rules and the how-to, and it is part of these instructions. Read it
by section rather than whole:

- **`## Hard rules` and `## Environment rules`, always, before any
  work.** Nothing elsewhere overrides them.
- `## How to build and test` — before running, adding or changing a
  suite.
- `## Repository layout` — when a path is not obvious from `ls`.
- `## Project shape` — when the question is what OCA is for.

**The measurements live in `docs/RECORD.md`** — the long-form record:
every measurement, how it was arrived at, and what it does NOT
establish. Read it before quoting any figure or claiming a result, and
write every new bench number into it.

The specification driving all work is `SPEC.md`; the MVP bill of
materials is `BOM-MVP.md`. **Where the project stands is
`docs/STATUS.md`** — one page, done / not established / next, updated at
every merge and every design gate; `docs/RECORD.md` is its long sibling.

Critical points in short:

- No system-wide installs without explicit permission: everything lives
  in `tools/` and `oca/.venv/` (cocotb installed from git master,
  Python 3.14).
- Test vectors only from official sources in
  `oca/tests/vectors/sources/` — never hand-typed expected values.
- A test suite's exit code is the contract: a runner that always exits
  0 is a runner that never fails. Prove a new check by mutation — make
  it fail once on purpose — before trusting its green.
- A correction edits the figure in place, everywhere it appears, in the
  same commit — never stack a dated amendment on top of text that still
  says the old thing.
- An Fmax belongs to the commit it was measured on. Equal cell counts
  are not the same netlist — two builds can match on every per-type
  total and still place differently — so matching area proves nothing
  about clock. Re-measure before publishing, and find what changed
  before blaming the toolchain.
- Two frontends, and the split is not symmetric: our own SystemVerilog
  goes through `read_slang`, **third-party Verilog through
  `read_verilog`, never slang**. Slang did not infer the memories of the
  one vendored design this was measured on and spilled them into logic —
  10x the area on one module, 38x on another, and no block RAM at all —
  so a vendored design read through slang measures far too large and
  gets abandoned for the wrong reason. **Which counter produced those
  figures was never recorded**, so quote the ratio and never the
  absolute numbers.
- Linux/WireGuard integration goes through the kernel crypto API:
  **no kernel patches.** FreeBSD `if_wg` needs a minimal patch or is
  out of scope for v1.
- Product in English (code, commits, docs).
- Git: work on branches, never commit/push without explicit go-ahead.
