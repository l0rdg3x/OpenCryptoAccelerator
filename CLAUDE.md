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
- Product in English (code, commits, docs).
- Git: work on branches, never commit/push without explicit go-ahead.
