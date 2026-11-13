# Transport validation — SCPI over TCP

Measured against a real HP Z3801A (S/N 3542A05653, firmware 3543-A) on 2026-11-13,
via a disposable serial↔TCP bridge on `127.0.0.1:4196`. The bridge was deliberately
the dumbest possible pipe — single client, no framing, no heartbeat — so that anything
observed is attributable to the device or the transport rather than the bridge.

ser2net itself was **not** tested; its installer requires elevation. What this
validates is the layering: 7-O-1 framing across a TCP bridge, and the
read-until-prompt loop.

## Result: 7 data bits / odd parity bridges cleanly

```text
serial open: COM7 19200 7O1
--- *IDN?   prompt='scpi >'  0.20s  tcp_chunks=1
    HEWLETT-PACKARD,Z3801A,3542A05653,3543-A
```

The identity string returns byte-exact over TCP. This was the critical unknown for the
whole TCP plan, since most cheap serial device servers assume 8-N-1 and these units
require 7-O-1.

## Result: the status screen arrives in multiple TCP segments

| Query | Elapsed | TCP chunks | Reply lines |
| --- | --- | --- | --- |
| `*IDN?` | 0.20 s | 1 | 1 |
| `:SYST:ERR?` | 0.20 s | 1 | 1 |
| `:DIAG:ROSC:EFC?` | 0.20 s | 1 | 1 |
| `:GPS:SAT:TRAC?` | 0.20 s | 1 | 1 |
| `:SYST:STAT?` | **2.84 s** | **6** | 23 |

**This is the empirical case for FR-2.** The status screen crossed the socket in six
separate segments. A single `recv()` would have returned a fragment, and any
fixed-delay read would be guessing. Scanning the accumulated buffer for the prompt is
the only correct approach, and it is what the probe did.

## Result: prompt scanning is faster than fixed delays

The prototype shell scripts used fixed sleeps — 700 ms for short queries, 4 s for the
status screen. Measured actuals are 200 ms and 2.84 s, so prompt scanning is roughly
**30% faster on the slow query and 3.5× faster on short ones**, while also being
correct rather than hopeful.

This matters for poll scheduling (C-1): a fast poll of four short queries costs ~0.8 s,
not ~2.8 s, so a 15 s fast cadence leaves ample headroom.

## Prompt grammar confirmed

```python
PROMPT = re.compile(rb"(scpi >|E-\d+>)\s*$")
```

Every reply terminated with `scpi >`. The `E-nnn>` form appears when an error is
queued, which gives free error detection — the transport knows a command failed
without a separate `:SYST:ERR?` round trip, though reading the queue is still needed
to learn *which* error.

## Not yet validated

- ser2net specifically, on any platform (needs elevation to install).
- The Waveshare device server, which does its own UART configuration in hardware
  rather than through a host serial driver.
- Behaviour across a real network rather than loopback: added latency, MTU-sized
  segmentation, and TCP retransmission. Loopback is the easy case; expect *more*
  fragmentation over Ethernet, which reinforces the conclusion above rather than
  weakening it.
- Reconnect behaviour (FR-6) on bridge or gateway restart.
