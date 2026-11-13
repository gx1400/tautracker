# tautrack

Monitoring, logging and trend analysis for GPS-disciplined oscillators (GPSDOs).

`tautrack` polls a GPSDO over serial or TCP, records its behaviour as time series,
and serves a read-only web UI for watching accuracy, stability and satellite
reception over spans from minutes to a year. Think of it as
[Smokeping](https://oss.oetiker.ch/smokeping/) for a frequency standard: the 1PPS
time interval behaves like latency, the lock/holdover state like packet loss.

The name is from **σ(τ)** — Allan deviation, the standard measure of oscillator
stability, where τ is the averaging interval.

> **Status: early development.** Nothing here is stable yet. The spec is still
> being written; see [`docs/`](docs/).

## Why

GPSDOs are long-horizon instruments. A unit can look healthy in a five-minute
glance and still be slowly losing its oscillator, drifting out of EFC range, or
sitting at a site that only achieves lock 70% of the time. The interesting
behaviour only shows up across hours, days and months:

- **Lock duty cycle** — what fraction of the time is it actually disciplined?
- **EFC trend** — the single best predictor of oven oscillator end-of-life.
- **Holdover prediction** — how well has the unit learned its own oscillator?
- **Satellite reception by elevation** — the metric that actually diagnoses
  antenna siting, as distinct from raw signal strength.

[Lady Heather](http://www.ke5fx.com/heather/readme.htm) is the established tool in
this space and remains the reference for deep oscillator analysis. `tautrack`
aims at a different shape of problem: a headless service that runs continuously,
retains history at sensible resolution, exports to an existing observability
stack, and is readable from a phone.

## Hardware support

Initial target is the **HP Z3801A** and SCPI-compatible siblings (Z3805A, HP
58503A, and the Lucent/Symmetricom units that share the SmartClock firmware).

Two transports:

- **Serial** — direct USB/RS-232 (19200 8-N-1 varies by unit; the Z3801A is
  19200 **7-O-1**).
- **TCP** — a serial device server such as the Waveshare RS232/485/422 TO POE
  ETH (B), which supports the 7 data bit / odd parity configuration these units
  require.

## Licence

[MIT](LICENSE).

Lady Heather and other existing tools inform *scope* here, via their published
documentation. No code is taken from them — tautrack is MIT and must stay free of
copyleft-licensed source.

## AI assistance

See [AI_DISCLOSURE.md](AI_DISCLOSURE.md).
