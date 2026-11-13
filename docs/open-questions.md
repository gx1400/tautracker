# Open questions

Decisions are recorded here as they are made, newest status first. Questions are put
to the maintainer one at a time.

| # | Question | Status |
| --- | --- | --- |
| Q1 | Where does the time-series data live? | **decided** — own store, OTel additive |
| Q2 | Implementation language and runtime | **decided** — Python |
| Q3 | Local storage engine and retention tiering | **decided** — SQLite, app-side rollups |
| Q4 | How much ADEV analysis to implement | **decided** — long-tau ADEV only |
| Q5 | UI stack — server-rendered vs SPA | **decided** — Jinja + htmx |
| Q11 | Development data source, given Docker serial passthrough limits | **decided** — replay transport |
| Q12 | Platform serial strategy — ship a bridge or document one? | **decided** — document ser2net |
| Q6 | Charting library | **decided** — uPlot + hand-rolled SVG polar |
| Q7 | Packaging — single container vs compose stack | **decided** — Docker-first, single container |
| Q8 | Config file format | **decided** — TOML + env overrides |
| Q9 | Satellite history depth for the polar chart | **open** |
| Q10 | Parser strategy for the status screen | pending |

---

## Q1 — Where does the time-series data live?

tautrack has its own UI with year-long history, *and* exports OpenTelemetry to an
existing Grafana stack. Those two facts can be satisfied in more than one way, and the
choice determines the storage tiering, the UI's query path, and whether the app is
usable standalone.

**Decided: tautrack owns its own store. OTel export is additive.**

The application writes to a datastore it controls and serves its UI from that store.
It separately exports OpenTelemetry metrics, logs and traces to a configurable OTLP
endpoint for the maintainer's Grafana stack.

Rationale:

- The UI's history and the observability stack have different lifetimes. Buckets get
  reorganised, retention policies change, stacks get migrated — a tool monitoring an
  instrument owned for years should not lose its history to that.
- tautrack stays self-contained: it runs with no external dependencies, which keeps it
  useful to anyone cloning the repo who does not run Influx.
- Retention and tiering are then ours to specify precisely (see Q3) rather than
  inherited from whatever policy the external stack happens to have.

Accepted costs: data is stored twice, and the tiering mechanism is ours to build.

## Q2 — Implementation language and runtime

Gates the storage engine, the web framework and the packaging.

**Decided: Python.** Async poller, FastAPI for the HTTP/API layer.

Rationale:

- Maintainability by the maintainer outweighs a tidier container. This is a tool to be
  owned for years alongside the instrument, and Python is already in use across the
  maintainer's other repositories (`ruff.toml` in `dfm17_hamradio`, `python-s1p-plot`,
  `python-trx-cloudlog`, `pyweaving_warptracker`).
- The OpenTelemetry Python SDK is mature across all three signals — metrics, logs and
  traces — which FR-20 requires.
- The real complexity here is parsing a fixed-width, state-dependent status screen
  (Q10). Python suits that work.
- Performance is irrelevant at one poll per 15 s (C-1, C-2).

Accepted costs: a ~100–150 MB container rather than ~20 MB, and a runtime to install.
Long-lived async tasks need deliberate handling of unhandled exceptions so a dead
poller task cannot go unnoticed.

If footprint ever matters, the poller is the only component that would need porting.

## Q3 — Local storage engine and retention tiering

Storage engine and tiering mechanism are coupled, so they were decided together.

**Decided: SQLite with application-side rollups**, following the Zabbix
history/trends model.

- `samples` — raw observations, retained short-term (default 14 days).
- `trends` — per-bucket `min`/`avg`/`max`/`count`, hourly and daily, retained for
  years.
- A periodic housekeeping task rolls raw into trends, then prunes raw.

Rationale:

- Preserves the self-containment that decided Q1: clone, run, see data, with no
  database to provision. Adding a Postgres dependency would give back much of the
  independence Q1 was chosen for.
- This is exactly Zabbix's own model, so the maintainer's requested analogy maps
  directly rather than approximately.
- Volume does not justify a real TSDB. A 15 s fast poll plus 60 s slow poll yields on
  the order of **58k rows/day** stored narrow (~21 M/year raw); at 14 days raw that is
  ~800k live rows, plus hourly trends of ~280k rows/year.
- WAL mode handles a single writer plus the UI's readers at this volume.
- Writing the rollup ourselves gives exact control over FR-10's min/max preservation.

Accepted costs: the rollup logic is ours to own and test; bucket queries are
hand-written SQL; and external concurrent access is limited.

Migration path if outgrown: the schema ports to TimescaleDB with the rollup task
replaced by continuous aggregates. The rollup semantics transfer.

## Q4 — How much ADEV analysis to implement

The Lady Heather scope question, bounded by C-3.

**Decided: overlapping ADEV for long τ only, with the τ floor shown explicitly.**

- Computed from the stored 1PPS TI series, for τ from ~2 min to ~1 day.
- The τ floor is rendered **on the chart**, not buried in documentation, together with
  a note that short-τ characterisation requires a time interval counter.
- MDEV/HDEV/TDEV are not implemented in v1. The stored series supports adding them
  later without a schema change.

Rationale:

- Long τ is where the questions this tool exists to answer actually live: oscillator
  aging, temperature response, and holdover behaviour. On the reference unit the
  holdover prediction improved 8.7× (432 → 49.5 µs/24 h) over two days — a long-τ
  phenomenon.
- Honesty about the τ floor matters in this community. Publishing a τ = 1 s figure
  derived from 60 s samples would be immediately and correctly criticised.
- Overlapping ADEV over a stored series is well-defined and testable against published
  reference datasets.

Caveats to carry into the implementation:

- The 1PPS TI reading is the **unit's own internal phase comparison**, not an
  independent measurement against an external reference. It is a legitimate phase
  series for long τ, but it is not equivalent to a counter-based measurement and the
  UI should not imply that it is.
- τ below ~2 min is unreachable by polling and must never be offered.

Accepted cost: tautrack does not replace Lady Heather for short-term phase noise
characterisation.

## Q5 — UI stack

Server-rendered with progressive enhancement, or a single-page app.

**Decided: server-rendered Jinja + htmx, with charts hydrated client-side.**

FastAPI renders pages; htmx swaps fragments on span change and for live refresh; a
small chart library draws into canvas from JSON endpoints.

Rationale:

- No build step and no `node_modules`. Clone, `pip install`, run. This continues the
  self-containment thread from Q1 and Q3 and keeps the prototype immediate.
- Keeps the stack Python plus a little JavaScript, supporting the maintainability
  argument that decided Q2.
- This is a **read-only** dashboard. The interaction budget is "pick a span and look",
  which htmx covers.
- Server-side span handling is the natural place for raw-vs-trends tier selection
  (Q3): the server picks the tier, the client only draws.

Accepted costs: rich cross-chart interaction (linked cursors, drag-zoom across panels)
would be awkward; live updates are polled rather than pushed unless SSE is added
later. The chart layer is already client-side JavaScript, so it can absorb richer
interaction without changing the server.

## Q6 — Charting library

Needs: many sparklines, spans up to a year, shaded state bands, a polar sky chart, good
behaviour on a phone, no build step (Q5) and a permissive licence (NFR-7).

**Decided: uPlot for time series; hand-rolled SVG for the polar chart.**

- uPlot (MIT, ~45-50 KB minified), vendored as a plain script, draws every sparkline.
  State bands are drawn through its draw hooks.
- The satellite sky plot is generated SVG, roughly 100 lines.

Rationale:

- Sparklines are the bulk of this UI and uPlot is the specialist: built for many series
  with large point counts, canvas-based, and far lighter than the alternatives. Weight
  matters on a phone.
- Hand-rolling the polar chart is deliberate, not a concession. The sky plot needs
  semantics no generic polar scatter provides: per-PRN labels, dot colour by signal
  strength, a visual distinction between *tracked* and `Acq .`, the elevation-mask ring
  at 10 degrees, and optionally trails (Q9). Configuring a generic library around those
  would cost more than drawing it.
- ECharts was the alternative, attractive for its native polar coordinates. Rejected
  because that attraction is worth little given the custom sky-plot semantics, and
  because trimming its ~1 MB dist requires a build step — precisely what Q5 ruled out,
  so the no-build path forces the heavy bundle rather than allowing a tuned one.

Accepted cost: two rendering approaches in the codebase.

## Q7 — Packaging

**Decided (maintainer directive): Docker deployment first, single container.**

- The container is the primary deployment target, not an afterthought. Running
  directly from a checkout stays supported for development.
- Storage is embedded (Q3), so no sidecar database. A volume holds the SQLite file.
- For initial testing the host serial port is passed into the container.
- Once the Waveshare TCP gateway is in place, the TCP transport removes the need for
  device passthrough entirely — which is the longer-term intent.

## Q8 — Config file format

Config carries: transport selection and parameters, poll cadences, retention tiers,
timezone, the GPS week-rollover offset, and the OTLP endpoint.

**Decided: TOML, with `TAUTRACK_`-prefixed environment overrides.**

- `config.toml` is the file. Sections: `[transport]`, `[poll]`, `[retention]`,
  `[otel]`, `[display]`.
- Any value is overridable by environment variable using `__` for nesting, the
  established Pydantic Settings convention — e.g.
  `TAUTRACK_TRANSPORT__PORT=/dev/ttyUSB0`.
- Precedence: environment > file > built-in defaults.

Rationale:

- **`tomllib` is in the standard library** from Python 3.11, so parsing costs zero
  dependencies. Every dependency added to the base runtime is one more thing that can
  break a container build or need a security update across the years this will run —
  and protecting clone-and-run has driven Q1, Q3 and Q5.
- TOML is unambiguous about types: `baudrate = 19200` is an integer, `parity = "odd"`
  a string. YAML's implicit coercion is a real hazard in a config containing values
  like `parity: o` or a bare `COM7`.
- The `__` nesting convention gives env overrides without bespoke merge logic.

Accepted cost: TOML is less familiar than YAML to people arriving from Compose,
Kubernetes or Home Assistant, and nests less readably. Judged minor here because the
config is flat — a handful of short sections with no deep structure, which is where
YAML's readability advantage would otherwise show.

## Q9 — Satellite history depth

Whether the polar chart shows only the current fix or trails over a configurable
window, and how much per-PRN history is retained (FR-11 cost).

**Decided:** *pending*

## Q10 — Parser strategy

The `:SYST:STAT?` screen is fixed-width, two-column, and varies by state (`INIT` vs
`AVG` vs held position; tracked table present only when tracking > 0). Column-position
parsing proved necessary in the prototype scripts — a naive field split reads the
right-hand table's PRN as the left-hand table's signal strength.

**Decided:** *pending*

## Q11 — Development data source

Docker Desktop (Windows **and** macOS) runs a Linux VM and cannot pass a host serial
device through. On Windows a COM port must first be attached to WSL2 with
`usbipd-win`, which detaches it from Windows — and the host-side logger currently
holds the port for a long-running capture.

**Decided: build a replay transport as a first-class third transport.**

Replays recorded sessions alongside `serial` and `tcp`. Seeded from real captured
data: ~34,000 lines of `:SYST:STAT?` screens and 1,600+ parsed rows spanning the
outdoor run, a relocation, lock acquisition, survey completion and a reception
degradation.

Rationale:

- Development and CI need no hardware, so long-running captures are undisturbed.
- It makes the parser testable against real pathological states, all of which broke
  the prototype scripts at least once:
  - the two-column screen layout (a naive field split reads the right-hand table's PRN
    as the left-hand table's signal strength)
  - `INIT` vs `AVG` vs plain held position
  - the tracked-satellite table being absent entirely when tracking is 0
  - `Acq .` / `Acq ..` acquisition states
  - the `[?]` unconfirmed-time flag
  - `Suspended:` reasons printed on the line *below* the `MODE` line
- Fixture-driven tests pin these permanently.

Accepted cost: replay is not the real transport, so `serial` and `tcp` still need a
hardware test before release.

## Q12 — Platform serial strategy

See the transport matrix in `requirements.md`. Native `--device` passthrough works only
where the Docker daemon runs on Linux. Elsewhere the `tcp` transport plus a host-side
serial bridge is the answer, and ser2net now ships Windows and macOS builds.

The question is whether tautrack also ships its own bridge helper.

**Decided: document ser2net, ship nothing.** tautrack speaks only `serial`, `tcp` and
`replay`.

Rationale:

- ser2net is mature, actively maintained (4.6.6, November 2025) and now ships Windows
  and macOS builds, so the problem is solved by someone else.
- The need is **transitional**: Linux hosts never needed a bridge, and a hardware TCP
  gateway makes it moot.
- A naive bridge is deceptively easy to get subtly wrong — client disconnects, dropped
  bytes mid-reply, corrupted prompt framing. Those failures masquerade as device
  faults, the same trap as the device-server heartbeat packets already documented in
  `scope.md`.

Accepted cost: first-run friction on Windows and macOS, where a second component must
be installed before any data appears.

A disposable bridge *was* written to validate SCPI over TCP (see
`transport-validation.md`). It is a throwaway test tool and is not part of the
project.
