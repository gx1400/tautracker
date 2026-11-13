# Open questions

Decisions are recorded here as they are made, newest status first. Questions are put
to the maintainer one at a time.

| # | Question | Status |
| --- | --- | --- |
| Q1 | Where does the time-series data live? | **decided** — own store, OTel additive |
| Q2 | Implementation language and runtime | **decided** — Python |
| Q3 | Local storage engine and retention tiering | **decided** — SQLite, app-side rollups |
| Q4 | How much ADEV analysis to implement | **decided** — long-tau ADEV only |
| Q5 | UI stack — server-rendered vs SPA | **open** |
| Q6 | Charting library | pending |
| Q7 | Packaging — single container vs compose stack | pending |
| Q8 | Config file format | pending |
| Q9 | Satellite history depth for the polar chart | pending |
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

Server-rendered with progressive enhancement, or a single-page app. Affects the
responsive implementation and how live updates arrive.

**Decided:** *pending*

## Q6 — Charting library

Needs: many sparklines, large time spans, shaded state bands, a polar chart, and good
behaviour on a phone.

**Decided:** *pending*

## Q7 — Packaging

Single container with embedded storage, or a compose stack with an external database.

**Decided:** *pending*

## Q8 — Config file format

TOML / YAML / env-only, and the precedence rules for environment overrides.

**Decided:** *pending*

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
