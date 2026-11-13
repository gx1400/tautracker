# SCPI command map — HP Z3801A, firmware 3543-A

Probed 2026-11-13 against S/N 3542A05653. Read-only: every candidate ended in `?`.
All timings are over direct serial at 19200 7-O-1.

**Why this matters:** `:SYST:STAT?` takes **2.84 s** to render and requires
column-offset parsing of a two-column fixed-width screen. Most of its scalar content
turns out to be available as discrete queries returning in **0.20 s** in structured
form — roughly 14× faster, with no parsing ambiguity.

## Supported (21)

| Query | Example reply | Notes |
| --- | --- | --- |
| `*IDN?` | `HEWLETT-PACKARD,Z3801A,3542A05653,3543-A` | identity; poll once per connection |
| `:SYST:ERR?` | `+0,"No error"` | error queue |
| `:SYNC:TFOM?` | `+3` | time figure of merit |
| `:SYNC:FFOM?` | `+2` | frequency figure of merit |
| `:SYNC:STAT?` | `WAIT` | SmartClock state as a compact enum. Vocabulary seen so far: `WAIT`, `REC`. Needs observation across a lock to enumerate fully |
| `:SYNC:HOLD:DUR?` | `+1.52000E+002,1` | holdover duration in seconds, plus a second field (flag) |
| `:GPS:SAT:TRAC?` | `+0` or `+1,+2` | tracked PRN list |
| `:GPS:SAT:TRAC:COUN?` | `+0` | tracked count |
| `:GPS:POS?` | `N,+38,+59,+5.64490E+001,W,+94,+40,+5.57540E+001,...` | position, already structured |
| `:GPS:REF:ADEL?` | `+0.00000E+000` | antenna delay |
| `:GPS:ADEL?` | `+0.00000E+000` | antenna delay (alias) |
| `:PTIM:DATE?` | `+2007,+3,+30` | date — **subject to the 1024-week rollover** |
| `:PTIM:TIME?` | `+14,+9,+41` | time of day |
| `:PTIM:TCODE?` | `T1#H3337DCA732000B4` | packed time code; format not yet decoded |
| `:PTIM:LEAP:STAT?` | `0` | leap second pending flag |
| `:PTIM:LEAP:ACC?` | `+18` | accumulated leap seconds |
| `:DIAG:ROSC:EFC?` | `+5.43933E+001` | EFC as percent of range |
| `:DIAG:ROSC:EFC:REL?` | `+5.43933E+001` | same value as above |
| `:DIAG:ROSC:EFC:ABS?` | `+809465` | **absolute DAC count** — finer resolution than the percent form, better for long-term aging trends |
| `:DIAG:LOG:COUN?` | `+184` | diagnostic log entry count |
| `:OUTP:STAT?` | `0` | output validity |

## Rejected — E-113 undefined header (14)

`:SYNC:HEAL?` `:SYNC:HOLD:STAT?` `:SYNC:HOLD:THR?` `:SYNC:HOLD:INIT?`
`:SYNC:SOUR:MODE?` `:SYNC:SOUR:STAT?` `:SYNC:LIM:HOLD?` `:PTIM:OUTP:STAT?`
`:GPS:SAT:VIS:COUN?` `:GPS:UTC?` `:GPS:DATE?` `:GPS:TIME?` `:SYST:VERS?` `:FREQ?`

These headers do not exist on this firmware.

## Unresolved — E-230, data stale or unavailable (2)

`:SYNC:TINT?` and `:PTIM:TINT?`

**This distinction matters.** E-113 means the header is undefined; **E-230 means the
command is recognised but has no valid value right now**. Both were probed while the
unit was in holdover, where the status screen shows `1PPS TI  --`.

A 150 s follow-up poll failed to settle it: the unit stayed in `WAIT` then `REC` with
0–2 satellites and never locked, so E-230 persisted throughout. **It remains unproven
whether these return 1PPS TI while locked.**

Worth retesting during a confirmed lock, because 1PPS TI is the primary accuracy metric
and a discrete query would remove the main reason to parse the status screen at a fast
cadence.

## Still only available from `:SYST:STAT?`

- Per-satellite El / Az / SS tables, and `Acq` state
- Synchronisation state text (`Outputs Valid` vs `Valid/Reduced Accuracy`)
- Holdover **prediction** (µs/24 h) and present uncertainty
- Health monitor's six individual flags
- Position survey progress and its suspend reason
- The `[?]` unconfirmed-time flag
- 1PPS TI, pending the E-230 retest
