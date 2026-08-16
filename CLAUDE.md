# CLAUDE.md — OpenCrypto Accelerator

The canonical project document for agents is **`AGENTS.md`**: it is
part of these instructions. Read it by section rather than whole:

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
`docs/STATUS.md`** — one page, done / not established / next, updated
at every merge and every design gate.

Critical points in short:

- No system-wide installs: everything lives in `tools/` and `oca/.venv/`.
- Test vectors only from official sources in
  `oca/tests/vectors/sources/` — never hand-typed expected values.
- Git: work on branches; never commit or push without explicit go-ahead.
- Product in English (code, commits, docs).
