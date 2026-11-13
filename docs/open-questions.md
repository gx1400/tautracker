# Open questions

Decisions are recorded here as they are made, newest status first. Questions are put
to the maintainer one at a time.

| # | Question | Status |
|---|---|---|
| Q1 | Where does the time-series data live? | **open** |
| Q2 | Implementation language and runtime | pending |
| Q3 | Retention tiering mechanism | pending |
| Q4 | How much ADEV analysis to implement | pending |
| Q5 | UI stack — server-rendered vs SPA | pending |
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

**Decided:** _pending_

## Q2 — Implementation language and runtime

Gates everything downstream. Candidates: Python (fast to build, mature OTel SDK,
matches existing tooling in other repos) vs Go (single static binary, trivial
container).

**Decided:** _pending_

## Q3 — Retention tiering mechanism

The Zabbix history/trends analogue. Options differ by where the downsampling runs:
database-side (Influx tasks, Timescale continuous aggregates) or application-side.
Must preserve min/max (FR-10).

**Decided:** _pending_

## Q4 — How much ADEV analysis to implement

Constrained by C-3: τ < ~2 min is unreachable. Options range from none, through
medium/long-τ ADEV only with an honest τ floor, to the full ADEV/MDEV/HDEV/TDEV family.

**Decided:** _pending_

## Q5 — UI stack

Server-rendered with progressive enhancement, or a single-page app. Affects the
responsive implementation and how live updates arrive.

**Decided:** _pending_

## Q6 — Charting library

Needs: many sparklines, large time spans, shaded state bands, a polar chart, and good
behaviour on a phone.

**Decided:** _pending_

## Q7 — Packaging

Single container with embedded storage, or a compose stack with an external database.

**Decided:** _pending_

## Q8 — Config file format

TOML / YAML / env-only, and the precedence rules for environment overrides.

**Decided:** _pending_

## Q9 — Satellite history depth

Whether the polar chart shows only the current fix or trails over a configurable
window, and how much per-PRN history is retained (FR-11 cost).

**Decided:** _pending_

## Q10 — Parser strategy

The `:SYST:STAT?` screen is fixed-width, two-column, and varies by state (`INIT` vs
`AVG` vs held position; tracked table present only when tracking > 0). Column-position
parsing proved necessary in the prototype scripts — a naive field split reads the
right-hand table's PRN as the left-hand table's signal strength.

**Decided:** _pending_
