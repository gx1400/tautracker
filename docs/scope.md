# Scope

## Purpose

A headless service that continuously polls a GPS-disciplined oscillator, retains its
behaviour as time series at sensible resolution, and presents it through a read-only
responsive web UI — plus exports to an existing OpenTelemetry/Grafana stack.

## In scope (v1)

| Area | Included |
| --- | --- |
| Devices | HP Z3801A and SCPI-compatible siblings (Z3805A, 58503A, Lucent/Symmetricom SmartClock units) |
| Transport | Serial (USB/RS-232) **and** TCP (serial device server, e.g. Waveshare PoE ETH (B)) |
| Polling | Configurable cadence, split fast/slow (see below) |
| Storage | Tiered: high-resolution recent data, downsampled long-term trends |
| UI | Read-only, responsive (desktop / tablet / phone) |
| Charts | Sparklines with selectable spans: 5m, 15m, 30m, 1h, 6h, 12h, 24h, 3d, 1w, 2w, 1m, 6m, 1y |
| Satellite view | Azimuth/elevation polar chart |
| Clocks | Unit time (as reported), adjusted UTC, adjusted local — timezone configurable |
| Telemetry | OpenTelemetry metrics, logs, and traces where meaningful |
| Config | File-based, with environment overrides |

## Out of scope (v1)

- **Device control.** The UI is view-only. The poller necessarily *sends* SCPI queries,
  but never configuration or state-changing commands (`:GPS:POS`, `:SYST:PRES`,
  `:GPS:INIT:*`). Those stay manual.
- **Authentication / multi-user.** Assumed to run on a trusted LAN.
- **Multi-device.** One GPSDO per instance for v1; the data model should not preclude
  it later.
- **Alerting.** Delegated to the Grafana stack via exported metrics.

## Scope question: how much Lady Heather to take on

[Lady Heather](http://www.ke5fx.com/heather/readme.htm) is the reference tool. Its
headline analysis features are the **ADEV family** — ADEV, MDEV, HDEV, TDEV — plus an
az/el satellite map, oscillator and temperature characterisation, and direct device
control.

**A hard constraint shapes this decision.** Allan deviation at averaging interval τ
requires phase or frequency samples at intervals ≤ τ/2. Polling SCPI gives one sample
per poll — realistically 10–60 s, because `:SYST:STAT?` alone takes ~4 s to render.
That means:

- τ ≥ ~2 minutes is computable from polled data.
- **τ < 1 minute is not reachable by polling at all.** Short-τ ADEV needs a time
  interval counter or a 1 Hz phase feed, which this architecture cannot provide.

So tautrack can produce *medium- and long-τ* stability figures — which is where
oscillator aging and temperature effects live — but cannot replace a TIC-based
measurement for short-τ noise characterisation. Any ADEV display must state its τ
floor honestly rather than implying full coverage.

See `open-questions.md` Q4 for the decision.

## Metric catalogue

Derived from two days of captured Z3801A telemetry.

### From `:SYST:STAT?` (slow poll, ~4 s)

| Metric | Type | Notes |
| --- | --- | --- |
| Synchronisation state | enum | `Outputs Valid` / `Valid/Reduced Accuracy` / `Outputs Invalid` |
| SmartClock mode | enum | `Locked to GPS` (± `stabilizing frequency`), `Recovery`, `Holdover`, `Power-up` (+ substate) |
| TFOM / FFOM | int 0–9 | Time / frequency figure of merit; lower is better, FFOM 0 = settled |
| 1PPS TI | ns, signed | **Primary accuracy metric.** Offset of unit 1PPS vs GPS |
| Holdover duration | seconds | Non-zero only while in holdover |
| Holdover uncertainty (present) | µs | Current estimate |
| Holdover prediction | µs/24 h | Unit's self-assessment; falls as SmartClock learns the OCXO |
| Hold threshold | µs | Configured limit |
| 1PPS CLK status | enum | `Synchronized to GPS Time` / `Invalid: not tracking` / `inacc position` / `Assessing stability` |
| Satellites tracked / not tracked | int | |
| Per-satellite (tracked) | PRN, El, Az, SS | SS is an arbitrary unit, not dBm |
| Per-satellite (not tracked) | PRN, El, Az, acq state | `Acq .` = reading orbit data |
| GPS time / date | timestamp | **Subject to 1024-week rollover on 1996 firmware**; `[?]` suffix = unconfirmed |
| Position mode | enum | `Survey: N%` (+ suspend reason) / `Hold` |
| Position | lat / lon / height MSL | `INIT` (seed), `AVG` (surveying), or plain (held) |
| Antenna delay | ns | |
| Elevation mask | degrees | |
| Health | 6 booleans | Self Test, Int Pwr, Oven Pwr, OCXO, EFC, GPS Rcv |

### From direct queries (fast poll, ~0.7 s each)

| Query | Metric |
| --- | --- |
| `:DIAG:ROSC:EFC?` | **EFC %** — oscillator steering, the long-term aging indicator |
| `:GPS:SAT:TRAC?` | Tracked PRN list |
| `:SYST:ERR?` | Error queue |
| `*IDN?` | Identity (model, serial, firmware) — poll once at connect |
| `:DIAG:LOG:COUN?` / `:DIAG:LOG:READ? n` | Diagnostic log (event history) |

### Derived metrics

| Metric | Derivation |
| --- | --- |
| Lock duty cycle | Rolling fraction of samples in a `Locked` mode |
| Satellite count (true) | **Caveat:** polled counts undersample badly. Observed: survey advanced 8 %/h during an hour where *zero* 60 s samples caught 4+ satellites. Treat polled counts as indicative; prefer derived state |
| Lowest elevation acquired | The antenna-siting diagnostic — more informative than signal strength |
| EFC band width | Spread between consecutive samples; narrows as the loop settles |
| ADEV family | See scope question above |

## Protocol notes

- Commands terminate CR LF. Responses end with a prompt: `scpi > ` on success,
  `E-nnn> ` when an error is queued.
- **Read until the prompt**, never a fixed sleep. Fixed delays are fragile over TCP
  where the device server packetises unpredictably, and the prompt gives free error
  detection.
- Only one client may hold a session. A device server may accept many TCP connections
  (the Waveshare allows 30) but SCPI is request/response against shared state —
  concurrent readers corrupt each other. The poller must be the sole connection.
- On a device server, **disable heartbeat and registration packets.** They inject
  bytes into the transparent serial stream and will intermittently corrupt responses.
