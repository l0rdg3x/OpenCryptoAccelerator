# SPDX-License-Identifier: MIT
"""Incremental SLIP decoder for a live byte stream.

oca/hw/sim/slip_model.py already has an encode()/decode() pair. Its
encode() is reused as-is by link.py: it is RFC 1055 and matches what
oca_slip_tx.sv emits (one END, at the end, both framing bytes escaped),
so a second encoder would only be a second place for the same bug.

Its decode() is not reused, because it solves a different problem: it
takes one fully-buffered byte string and returns every frame in it, all
at once, and raises on the first framing error in the whole buffer. A
serial port hands over a handful of bytes at a time -- a frame can span
several read()s, and several frames can arrive in one read() -- so the
host needs a decoder that can be fed one byte at a time and asked "is a
frame done yet", which decode() cannot answer.

The state machine below is written against oca_slip_rx.sv's, not
against slip_model.decode()'s, because it is the RTL's recovery
behaviour a live link needs: a bad or dangling escape is STICKY for the
rest of the frame ("the position in the stream is no longer known, so
nothing after it can be trusted either") and is only reported once the
terminating END is seen, at which point the frame is discarded and
decoding resumes cleanly on the next byte -- exactly the two refusals
oca_slip_rx.sv counts as cnt_esc. slip_model.decode() instead raises
immediately at the bad byte and abandons the whole buffer, which is
right for a testbench that gets to see the mistake as a hard failure
and wrong for a link that has to keep resynchronising after one.

An empty frame (two ENDs with nothing between them) is absorbed without
raising and without being reported, matching oca_slip_rx.sv's "AN EMPTY
FRAME IS NOT AN ERROR" -- RFC 1055 has senders emit a leading END to
flush line noise, and a decoder that treated it as a zero-length reply
would hand the caller something indistinguishable from a real answer.
"""

from __future__ import annotations

from slip_model import END, ESC, ESC_END, ESC_ESC

__all__ = ["SlipDecodeError", "SlipReader"]


class SlipDecodeError(ValueError):
    """A byte stream violated RFC 1055 framing: a dangling ESC right
    before END, or an ESC followed by anything but ESC_END/ESC_ESC.
    Both are what oca_slip_rx.sv counts as cnt_esc; the RTL never
    answers about either, so on a live board this is indistinguishable
    from a plain timeout. It is not indistinguishable here: this
    exception is for corruption discovered on the *reply* stream, which
    the host reads directly, and fake_device.py raises it internally
    (then stays silent on the wire) to decide whether to answer at all.
    """


class SlipReader:
    """Byte-at-a-time SLIP decoder. One instance per direction of a link;
    it is not safe to share one between concurrent frames."""

    def __init__(self) -> None:
        self._cur = bytearray()
        self._esc_pending = False
        self._err = False  # sticky until the END that closes this frame

    def feed(self, byte: int) -> bytes | None:
        """Feed one byte off the wire.

        Returns the completed frame's payload once END closes it, or
        None otherwise -- which covers both "no frame has ended yet"
        and "one just did, and it was empty", because the caller reacts
        to both the same way: keep reading.

        Raises SlipDecodeError exactly when the frame just closed by
        END was invalid; the reader is clean again immediately
        afterwards; and END always closes the frame, a pending escape
        included, so one corrupt frame costs exactly that frame and
        never desynchronises the ones after it.
        """
        if byte == END:
            if self._esc_pending:
                self._err = True
                self._esc_pending = False
            frame, bad = bytes(self._cur), self._err
            self._cur = bytearray()
            self._err = False
            if bad:
                raise SlipDecodeError("bad or dangling escape in frame")
            return frame if frame else None

        if self._esc_pending:
            self._esc_pending = False
            if byte == ESC_END:
                self._cur.append(END)
            elif byte == ESC_ESC:
                self._cur.append(ESC)
            else:
                self._err = True  # sticky: nothing after this can be trusted
        elif byte == ESC:
            self._esc_pending = True
        else:
            self._cur.append(byte)
        return None
