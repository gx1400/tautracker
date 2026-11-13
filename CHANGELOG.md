# Changelog

## [0.2.0](https://github.com/gx1400/tautracker/compare/v0.1.0...v0.2.0) (2026-11-13)


### Features

* add storage, poller, web UI, CI and container packaging ([6433caf](https://github.com/gx1400/tautracker/commit/6433cafe66e8810a18cb1a34c718680dc543797c))
* **tools:** add a ser2net work-alike, replacing the ad-hoc dev bridge ([f14d25f](https://github.com/gx1400/tautracker/commit/f14d25f50feb899c5b01c1093d4cae0903f8ebe0))
* **web:** explain every metric in the UI ([019eabc](https://github.com/gx1400/tautracker/commit/019eabc190597aa481fa892cc3f458fa3fc11077))
* **web:** show data gaps as gaps instead of interpolating across them ([52ae3c5](https://github.com/gx1400/tautracker/commit/52ae3c5a643d98f47b0bcc0197525f58eb6e3cff))


### Bug Fixes

* **ci:** declare the test dependencies explicitly ([5752982](https://github.com/gx1400/tautracker/commit/57529822e3fe977816fc9e3ff01af9934b703011))
* do not discard a status screen because an error is queued ([7bdb545](https://github.com/gx1400/tautracker/commit/7bdb5456a302604598b05cd263f83168112798fe))
* scale 1PPS TI reported in picoseconds, and add a history backfill tool ([f692318](https://github.com/gx1400/tautracker/commit/f692318ab8c7a2896e46e2bc6e34cf5b6a20de9c))
* serialise access to the shared SQLite connection ([b125eb3](https://github.com/gx1400/tautracker/commit/b125eb344507f2093edf318606b4fd4f86c66a5a))
* **web:** size the y-axis gutter to its labels, and clamp long tile values ([4b4645a](https://github.com/gx1400/tautracker/commit/4b4645a0405984633f455838c5bfacc740fef409))
* **web:** stop the sky view wasting horizontal space ([24e80b8](https://github.com/gx1400/tautracker/commit/24e80b882d6d27ae58a2ec39d434f5a2c406b60f))


### Documentation

* document installation, transports, configuration and Docker usage ([fd51357](https://github.com/gx1400/tautracker/commit/fd51357ecaed5078458c130d47dfd3ef34157fd7))
