# SPDX-License-Identifier: MIT
"""oca_crypto_console: drive the OCA host protocol over SLIP on the
board's serial console (/dev/ttyACM0, 115200 8N1), or against an
in-process fake for offline use with --fake.

Wire format: docs/design/2026-08-03-host-protocol.md.
RTL side of the framing: oca/hw/rtl/oca_slip_rx.sv, oca_slip_tx.sv.

The exit code is the contract: 0 only on a clean success, 1 on any link
or protocol failure (serial timeout, a malformed reply, a non-zero
status byte, a request id that was not echoed), 2 on a bad invocation
(argument parsing, before anything touches the wire).
"""

from __future__ import annotations

import argparse
import binascii
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SIM_DIR = _HERE.parent / "sim"
for _p in (_HERE, _SIM_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fake_device import FakeBoard  # noqa: E402
from link import OcaLink, OcaLinkError  # noqa: E402
from selftest import SelftestFailure, run_selftest  # noqa: E402
from transport import DEFAULT_BAUD, RawSerial  # noqa: E402

DEFAULT_PORT = "/dev/ttyACM0"
DEFAULT_TIMEOUT = 2.0


def _hex(s: str) -> bytes:
    try:
        return binascii.unhexlify(s)
    except (binascii.Error, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"not valid hex: {s!r}") from exc


def _fixed_hex(nbytes: int, label: str):
    def convert(s: str) -> bytes:
        data = _hex(s)
        if len(data) != nbytes:
            raise argparse.ArgumentTypeError(
                f"{label} must be {nbytes} bytes, got {len(data)}")
        return data
    return convert


def _cmd_load_key(link: OcaLink, args: argparse.Namespace) -> int:
    link.load_key(args.slot, args.key)
    print(f"loaded slot {args.slot}")
    return 0


def _cmd_seal(link: OcaLink, args: argparse.Namespace) -> int:
    tag, ct = link.seal(args.slot, args.nonce, args.aad, args.msg)
    print(f"tag={tag.hex()}")
    print(f"ct={ct.hex()}")
    return 0


def _cmd_open(link: OcaLink, args: argparse.Namespace) -> int:
    pt = link.open(args.slot, args.nonce, args.aad, args.ct, args.tag)
    print(f"pt={pt.hex()}")
    return 0


def _cmd_stats(link: OcaLink, args: argparse.Namespace) -> int:
    s = link.stats()
    print(f"received={s['received']} dropped_header={s['dropped_header']} "
          f"completed={s['completed']} auth_failures={s['auth_failures']}")
    return 0


def _cmd_selftest(link: OcaLink, args: argparse.Namespace) -> int:
    try:
        run_selftest(link, log=print)
    except SelftestFailure as exc:
        print(f"selftest: FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="oca_crypto_console",
        description="Host-side driver for the OCA host protocol over "
                     "SLIP on the board's serial console.")
    p.add_argument("--port", default=DEFAULT_PORT,
                    help=f"serial device (default {DEFAULT_PORT})")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD,
                    help=f"default {DEFAULT_BAUD}; the board only speaks this one")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help=f"seconds to wait for a reply (default {DEFAULT_TIMEOUT})")
    p.add_argument("--fake", action="store_true",
                    help="talk to an in-process fake board instead of a "
                         "serial port -- offline, no hardware required")

    sub = p.add_subparsers(dest="command", required=True)

    lk = sub.add_parser("load-key", help="load a 32-byte key into a slot")
    lk.add_argument("--slot", type=int, required=True)
    lk.add_argument("--key", type=_fixed_hex(32, "--key"), required=True,
                     help="32 bytes, hex")
    lk.set_defaults(func=_cmd_load_key)

    sl = sub.add_parser("seal", help="AEAD encrypt (ChaCha20-Poly1305)")
    sl.add_argument("--slot", type=int, required=True)
    sl.add_argument("--nonce", type=_fixed_hex(12, "--nonce"), required=True,
                     help="12 bytes, hex")
    sl.add_argument("--aad", type=_hex, default=b"", help="hex, default empty")
    sl.add_argument("--msg", type=_hex, required=True, help="hex")
    sl.set_defaults(func=_cmd_seal)

    op = sub.add_parser("open", help="AEAD decrypt and verify")
    op.add_argument("--slot", type=int, required=True)
    op.add_argument("--nonce", type=_fixed_hex(12, "--nonce"), required=True,
                     help="12 bytes, hex")
    op.add_argument("--aad", type=_hex, default=b"", help="hex, default empty")
    op.add_argument("--ct", type=_hex, required=True, help="hex")
    op.add_argument("--tag", type=_fixed_hex(16, "--tag"), required=True,
                     help="16 bytes, hex")
    op.set_defaults(func=_cmd_open)

    st = sub.add_parser("stats", help="read the four wire-protocol counters")
    st.set_defaults(func=_cmd_stats)

    se = sub.add_parser(
        "selftest",
        help="run official RFC 8439 vectors end to end and judge every "
             "byte against aead_model.py")
    se.set_defaults(func=_cmd_selftest)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        transport = FakeBoard() if args.fake else RawSerial(args.port, baudrate=args.baud)
    except OSError as exc:
        print(f"error: opening {args.port}: {exc}", file=sys.stderr)
        return 1

    link = OcaLink(transport, timeout=args.timeout)
    try:
        return args.func(link, args)
    except OcaLinkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: serial I/O: {exc}", file=sys.stderr)
        return 1
    finally:
        transport.close()


if __name__ == "__main__":
    sys.exit(main())
