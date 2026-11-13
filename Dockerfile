# syntax=docker/dockerfile:1

# Docker is the primary deployment target (Q7). Two stages so build tooling does not
# ship in the runtime image.
FROM python:3.15-slim AS build

ARG VERSION=0.0.0-dev
WORKDIR /src

# Install dependencies before copying source, so a code change does not invalidate the
# dependency layer.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir --upgrade pip build \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheels .


FROM python:3.15-slim AS runtime

ARG VERSION=0.0.0-dev
LABEL org.opencontainers.image.title="tautrack" \
      org.opencontainers.image.description="Monitoring and trend analysis for GPS-disciplined oscillators" \
      org.opencontainers.image.source="https://github.com/gx1400/tautracker" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

# Run unprivileged. The serial device is passed in with --device and its group is
# supplied at runtime via --group-add, so no capability is needed in the image.
RUN useradd --create-home --uid 10001 tautrack

COPY --from=build /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# SQLite lives on a volume so history survives container replacement (NFR-5).
RUN mkdir -p /data && chown tautrack:tautrack /data
VOLUME ["/data"]

USER tautrack
WORKDIR /home/tautrack

ENV TAUTRACK_DATABASE=/data/tautrack.db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# A container is only reachable through a published port if the listener binds all
# interfaces, so override the loopback default here. The container network boundary
# is what limits exposure (NFR-6).
ENV TAUTRACK_WEB__HOST=0.0.0.0 \
    TAUTRACK_WEB__PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"

ENTRYPOINT ["tautrack"]
CMD ["serve"]
