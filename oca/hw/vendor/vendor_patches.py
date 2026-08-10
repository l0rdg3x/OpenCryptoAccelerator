# SPDX-License-Identifier: MIT
"""The patched verilog-ethernet tree: how it is built and how it is checked.

The pinned submodule at `verilog-ethernet/` is never edited. Everything
that reads the vendor RTL -- `hw/syn/run_synth.py` and
`hw/sim/run_eth_mac.py` -- reads `build/verilog-ethernet/` instead: the
pin's own content, extracted with `git archive`, with the patches in
`patches/` applied on top. That directory is a build product, ignored by
git, and reproducible from the pin plus the patch files, which is the
only claim this project makes about it.

Build or refresh it with:

    .venv/bin/python hw/vendor/vendor_patches.py

The check is the point. Both patches change what the hardware does --
one makes the receive path's tkeep mean anything at all, the other moves
the FCS comparison off the critical path -- so a build that quietly used
an unpatched tree would produce a bitstream that cannot receive and a
timing figure measured on the wrong design. `require()` refuses to
proceed rather than let that happen, and it names what is missing.

Applying is idempotent by exit code only: git's messages are localised,
so the text of a failure says nothing portable. A tree that is neither
pristine nor already patched fails here rather than being forced.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

VENDOR_DIR = Path(__file__).resolve().parent
# The repository the submodule is a submodule of, which is what holds the
# gitlink pin() checks the checkout against.
ROOT = VENDOR_DIR.parents[2]

# The pinned submodule, read-only. Nothing in this project writes here.
PINNED = VENDOR_DIR / "verilog-ethernet"
PATCH_DIR = VENDOR_DIR / "patches"
# The build product every reader of the vendor RTL actually opens.
PATCHED = VENDOR_DIR / "build" / "verilog-ethernet"
STAMP = PATCHED.parent / "verilog-ethernet.stamp"

# Applied in this order. They touch different files, so the order is only
# for a reproducible stamp.
PATCHES = [
    "verilog-ethernet-axis-adapter-upsize-tkeep.patch",
    "verilog-ethernet-axis-gmii-rx-fcs-off-crc-path.patch",
]

BUILD_HINT = ("build it with `.venv/bin/python hw/vendor/vendor_patches.py` "
              "from oca/")


def _detached_env(tree: Path) -> dict:
    """An environment in which `git apply` treats `tree` as the top level.

    This is the whole reason this function exists, and it was measured
    the wrong way round first. `git apply` run from inside a repository
    resolves the paths in a patch against that repository's top level,
    not against the working directory, and *silently ignores* the ones
    that fall outside the directory it was started in. The patched tree
    lives at oca/hw/vendor/build/, inside this repository, so every
    `git apply` there -- the real one and the --check that vouches for
    it -- matched nothing, changed nothing and exited 0. The applier
    reported two patches applied over a pristine tree and the check
    agreed with it.

    A ceiling above the tree stops git looking for that enclosing
    repository. `_git_apply` proves the ceiling took, rather than
    trusting it, because this failure mode is invisible in the exit code.
    """
    env = dict(os.environ)
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env["GIT_CEILING_DIRECTORIES"] = str(tree.resolve().parent)
    return env


def _git_apply(tree: Path, patch: Path, *flags: str) -> int:
    env = _detached_env(tree)
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                         cwd=tree, env=env, capture_output=True, text=True)
    # Either git finds no repository, or it finds one whose top level is
    # the tree itself. Anything else and the patch paths do not mean what
    # this module thinks they mean.
    if top.returncode == 0 and Path(top.stdout.strip()) != tree.resolve():
        sys.exit(f"refusing to patch {tree}: git resolves it inside the "
                 f"repository at {top.stdout.strip()}, where patch paths are "
                 "read against that repository's top level and silently "
                 "ignored. Nothing was applied and nothing was checked.")
    return subprocess.run(
        ["git", "apply", *flags, "-p1", str(patch)],
        cwd=tree, env=env, capture_output=True).returncode


def is_applied(tree: Path, patch: Path) -> bool:
    """True when every hunk of `patch` is already present in `tree`.

    A tree that is not there has nothing applied to it, which is a
    finding and not a crash: describe() reports it and require() acts on
    it.
    """
    if not tree.is_dir() or not patch.is_file():
        return False
    return _git_apply(tree, patch, "--check", "--reverse") == 0


def apply_all(tree: Path) -> None:
    if not tree.is_dir():
        sys.exit(f"nothing to patch: {tree} does not exist")
    for name in PATCHES:
        patch = PATCH_DIR / name
        if not patch.is_file():
            sys.exit(f"patch not found: {patch}")
        if is_applied(tree, patch):
            continue
        if _git_apply(tree, patch) != 0:
            sys.exit(f"{name} does not apply to {tree} — that tree is "
                     f"neither pristine at the pin nor already patched")


def pin() -> str:
    """The commit the submodule is checked out at, once it is known to be
    the commit the superproject asks for.

    Reading the submodule's own HEAD alone is not enough: a submodule
    left at some other commit would be extracted from, stamped with that
    commit, and every check here would agree with itself. What makes the
    pin a pin is the gitlink in the parent repository, so the two are
    compared and a disagreement stops the build rather than being
    recorded as if it were intended.
    """
    out = subprocess.run(["git", "-C", str(PINNED), "rev-parse", "HEAD"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"cannot read the submodule pin at {PINNED}: "
                 "is the submodule checked out?")
    head = out.stdout.strip()

    rel = PINNED.relative_to(ROOT)
    link = subprocess.run(["git", "-C", str(ROOT), "ls-tree", "HEAD", str(rel)],
                          capture_output=True, text=True)
    if link.returncode != 0 or not link.stdout.strip():
        sys.exit(f"cannot read the gitlink for {rel} in {ROOT}")
    fields = link.stdout.split()
    if len(fields) < 3 or fields[1] != "commit":
        sys.exit(f"{rel} is not a submodule in this commit: {link.stdout!r}")
    want = fields[2]

    if head != want:
        sys.exit(
            f"the submodule at {PINNED} is checked out at {head[:9]} but "
            f"this commit pins it at {want[:9]}. Building from it would "
            f"measure a tree nobody asked for.\n"
            f"Fix with: git -C {ROOT} submodule update --checkout {rel}")
    return head


def tree_digest(tree: Path) -> str:
    """One hash over every file in `tree`, path and content.

    The patches reverse-applying only proves the lines they touch. It
    does not prove the other 97 files are the pinned ones: appending a
    line to lfsr.v -- the CRC-32 the whole FCS verdict rests on -- left
    every check here reporting a healthy tree until this was added.
    """
    h = hashlib.sha256()
    for path in sorted(p for p in tree.rglob("*") if p.is_file()):
        h.update(str(path.relative_to(tree)).encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def stamp_now(tree: Path | None = None) -> dict:
    s = {
        "pin": pin(),
        "patches": {
            name: hashlib.sha256((PATCH_DIR / name).read_bytes()).hexdigest()
            for name in PATCHES
        },
    }
    if tree is not None:
        s["tree"] = tree_digest(tree)
    return s


def build() -> None:
    """Extract the pin into PATCHED and patch it. Destructive and total.

    `git archive` and not a copy of the working tree: it is the pinned
    commit's content by definition, so a submodule someone had edited by
    hand cannot leak into a measurement.
    """
    if PATCHED.exists():
        shutil.rmtree(PATCHED)
    PATCHED.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(PINNED), "archive", "HEAD"],
                             capture_output=True)
    if archive.returncode != 0:
        sys.exit(f"git archive failed in {PINNED}:\n"
                 f"{archive.stderr.decode(errors='replace')}")
    extract = subprocess.run(["tar", "-x", "-C", str(PATCHED)],
                             input=archive.stdout, capture_output=True)
    if extract.returncode != 0:
        sys.exit(f"tar failed:\n{extract.stderr.decode(errors='replace')}")
    apply_all(PATCHED)
    # The stamp is written with the tree's own digest in it, so a later
    # run can tell this tree from one that has been edited since.
    stamp = stamp_now(PATCHED)
    STAMP.write_text(json.dumps(stamp, indent=2) + "\n")
    print(f"{PATCHED}: pin {stamp['pin'][:9]}, "
          f"{len(PATCHES)} patch(es) applied")


def problems(tree: Path, *, stamped: bool) -> list:
    """Every reason `tree` is not a trustworthy patched vendor tree.

    `stamped` also demands that it was built from this pin and these
    patch files. Only PATCHED carries a stamp; a tree named by
    OCA_ETH_MAC_VENDOR is whatever the caller made it, which is the
    point of that override.
    """
    if not tree.is_dir():
        return [f"{tree} does not exist"]
    found = []
    for name in PATCHES:
        patch = PATCH_DIR / name
        if not patch.is_file():
            found.append(f"patch file missing: {patch}")
        elif not is_applied(tree, patch):
            found.append(f"not applied: {name}")
    if stamped:
        want = stamp_now()
        try:
            have = json.loads(STAMP.read_text())
        except (OSError, ValueError):
            found.append(f"no readable stamp at {STAMP}")
        else:
            if have.get("pin") != want["pin"]:
                found.append(
                    f"built from pin {str(have.get('pin'))[:9]}, "
                    f"the submodule is now at {want['pin'][:9]}")
            for name, digest in want["patches"].items():
                if have.get("patches", {}).get(name) != digest:
                    found.append(f"patch file changed since the build: {name}")
            # And the tree itself. The two checks above cover the pin and
            # the patch files; this covers every other file in the tree,
            # which nothing else here looks at.
            stored = have.get("tree")
            if stored is None:
                found.append(
                    "the stamp predates the tree digest: rebuild with "
                    "vendor_patches.py build")
            elif stored != tree_digest(tree):
                found.append(
                    f"{tree} has been edited since it was built: rebuild "
                    "with vendor_patches.py build, and if you meant the "
                    "edit, it belongs in a patch file")
    return found


def require(tree: Path = PATCHED) -> None:
    """Refuse to go on unless `tree` is this pin with these patches in it.

    One exception, and it is narrow: a tree named explicitly by the caller
    through OCA_ETH_MAC_VENDOR, missing a patch, is warned about loudly
    and allowed. That is the shape of a mutation experiment -- prove the
    suite goes red without the patch -- and refusing it would mean the
    patches could never be shown to be load-bearing. A missing tree, or
    the default tree in any state but correct, is still fatal.
    """
    found = problems(tree, stamped=tree == PATCHED)
    if found and tree != PATCHED and tree.is_dir() and all(
            f.startswith("not applied: ") for f in found):
        print(f"WARNING: {tree} is missing " + ", ".join(
                  f.removeprefix("not applied: ") for f in found)
              + ".\n  Going on because it was named explicitly. Anything "
                "measured here describes a design the board will not carry.",
              file=sys.stderr)
        return
    if found:
        sys.exit(f"vendor tree {tree} is not usable:\n  "
                 + "\n  ".join(found)
                 + f"\n{BUILD_HINT}\nThe patches are not cosmetic: without "
                 "them the receive path delivers tkeep=0 on every beat and "
                 "the FCS comparison sits on the 125 MHz critical path, so "
                 "anything measured here would be measured on a different "
                 "design.")


def describe(tree: Path) -> str:
    """Two lines naming the tree and the state of each patch in it."""
    if not tree.is_dir():
        return f"vendor tree: {tree}\n  MISSING"
    state = ", ".join(
        f"{name.removeprefix('verilog-ethernet-').removesuffix('.patch')}"
        f"={'in' if is_applied(tree, PATCH_DIR / name) else 'ABSENT'}"
        for name in PATCHES)
    return f"vendor tree: {tree}\n  patches: {state}"


if __name__ == "__main__":
    # `build` is destructive and `check` must not be: running this file
    # with no argument used to build, which made "check the tree" and
    # "replace the tree" the same command and quietly repaired what a
    # check was meant to catch.
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "build":
        build()
    elif cmd == "check":
        found = problems(PATCHED, stamped=True)
        print(describe(PATCHED))
        if found:
            sys.exit("  " + "\n  ".join(found) + f"\n{BUILD_HINT}")
    else:
        sys.exit(f"usage: {sys.argv[0]} [build|check]")
