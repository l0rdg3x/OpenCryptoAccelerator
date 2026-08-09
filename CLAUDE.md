# CLAUDE.md — OpenCrypto Accelerator

The canonical project document for agents is **`AGENTS.md`**: read it
first and treat it as part of these instructions (layout, environment
rules, build/test commands, hard rules, status).

The specification driving all work is `SPEC.md`; the MVP bill of
materials is `BOM-MVP.md`.

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
- Product in English (code, commits, docs).
- Git: work on branches, never commit/push without explicit go-ahead.
