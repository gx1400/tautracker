# Requirements

## Functional

### Acquisition

- **FR-1** Connect to a GPSDO over serial (configurable port, baud, data bits, parity,
  stop bits) or TCP (host, port). Transport selected by config; identical behaviour
  above the transport boundary.
- **FR-2** Read responses by scanning for the SCPI prompt (`scpi > ` / `E-nnn> `), with
  a timeout — never a fixed delay.
- **FR-3** Detect and record error-queue prompts (`E-nnn>`) as events, not silent
  failures.
- **FR-4** Split poll cadences, independently configurable:
  - *fast* — short queries (EFC, tracked PRNs). Default 15 s.
  - *slow* — full `:SYST:STAT?` screen. Default 60 s.
- **FR-5** Identify the device once per connection (`*IDN?`) and record model, serial
  and firmware as resource attributes.
- **FR-6** Reconnect automatically with backoff on transport loss, and record the
  outage as a gap rather than interpolating across it.
- **FR-7** Never transmit state-changing commands. The query allowlist is explicit.

### Storage

- **FR-8** Persist all parsed metrics as time series with device identity attached.
- **FR-9** Tiered retention, Zabbix-style: full-resolution recent data, downsampled
  aggregates (min/max/avg/count per bucket) for long spans. See Q3.
- **FR-10** Aggregates must preserve **min and max**, not just mean — a 10-minute
  holdover must stay visible in a one-year view.
- **FR-11** Satellite data is per-PRN and sparse; the schema must handle PRNs
  appearing and disappearing without producing dense empty series.

### Web UI

- **FR-12** Read-only. No control affordances.
- **FR-13** Responsive across phone, tablet and desktop.
- **FR-14** Sparkline charts with a shared time-span selector:
  5 m, 15 m, 30 m, 1 h, 6 h, 12 h, 24 h, 3 d, 1 w, 2 w, 1 mo, 6 mo, 1 y.
- **FR-15** Charted metrics: 1PPS TI (accuracy), TI jitter/spread, EFC, satellite
  count, per-satellite signal strength, TFOM/FFOM, holdover prediction, lock state.
- **FR-16** Lock/holdover state rendered as shaded bands beneath or behind the
  continuous series (the Smokeping loss-shading analogue).
- **FR-17** Satellite azimuth/elevation polar chart of the current constellation.
- **FR-18** Three clocks: unit time as reported, adjusted UTC, adjusted local.
  - The Z3801A's 1996 firmware reports a date **1024 weeks behind** actual. The
    adjustment is a configured constant, applied for display only; raw values are
    stored unmodified.
- **FR-19** Timezone configurable by file and environment variable.

### Telemetry

- **FR-20** Export OpenTelemetry metrics, logs and traces to a configurable OTLP
  endpoint.
- **FR-21** Traces cover poll cycles — connect, query, parse — so a slow or failing
  device is diagnosable from the trace.
- **FR-22** Instrument the application's own health (poll duration, parse failures,
  reconnects) distinctly from the GPSDO's telemetry.

## Non-functional

- **NFR-1** Single GPSDO per instance; the data model must not preclude multiple.
- **NFR-2** **Docker deployment first.** The container is the primary deployment
  target; running directly from a checkout remains supported for development.
- **NFR-2a** Storage is embedded, so no sidecar database. A single volume holds the
  SQLite file.
- **NFR-2b** For initial testing the host serial device is passed into the container.
  On Linux hosts this is `--device /dev/ttyUSB0`. **On Windows hosts, Docker Desktop
  cannot pass a COM port into a Linux container** — the USB device must first be
  attached to WSL2 with `usbipd-win`, which detaches it from Windows. See
  `open-questions.md` Q11.
- **NFR-2c** Once a TCP serial gateway is available, the TCP transport removes the
  need for device passthrough entirely. This is the intended long-term configuration.
- **NFR-3** Configuration from a file, with environment-variable overrides.
- **NFR-4** Modest footprint — target a Raspberry Pi class host.
- **NFR-5** Survive restarts without data loss beyond the in-flight poll.
- **NFR-6** No authentication in v1; assumed trusted LAN. Must not be *hostile* to
  adding it later (no state mutation via GET, etc.).
- **NFR-7** MIT licensed; no copyleft dependencies in the distributed artefact.

## Constraints

- **C-1** `:SYST:STAT?` takes ~4 s to render on a Z3801A. Fast and slow polls must not
  overlap on a single connection.
- **C-2** Exactly one client may hold the SCPI session.
- **C-3** Polling cannot sample faster than a few seconds; ADEV below τ ≈ 2 min is
  unreachable by design (see `scope.md`).
- **C-4** Serial device servers must have heartbeat and registration packets disabled.
- **C-5** Satellite counts from periodic polls systematically undersample. Derived
  values must not present them as exact.
