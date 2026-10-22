# syntax=docker/dockerfile:1
#
# deploytrain base image. It runs the web UI by default:
#   docker run -p 8080:8080 -e DEPLOYTRAIN_UI_TOKEN=... -v ./config:/config:ro -v state:/state IMAGE
# or a one-off train:
#   docker run -v ./config:/config:ro -v state:/state IMAGE depart --version v1.4.0
#
# Your deploy/inspection/rollback commands run inside the container, so build your own image
# FROM this one and add the tools they need (kubectl, helm, aws, ...). See examples/docker/.

ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY deploytrain ./deploytrain
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:${PYTHON_VERSION}-slim

LABEL org.opencontainers.image.title="deploytrain" \
      org.opencontainers.image.description="Deployment train: roll releases out stop by stop, within each platform's timetable" \
      org.opencontainers.image.source="https://github.com/VishnuT-Procore/traintraintrain" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEPLOYTRAIN_CONFIG=/config/train.yaml \
    DEPLOYTRAIN_STATE_DIR=/state \
    DEPLOYTRAIN_UI_HOST=0.0.0.0 \
    DEPLOYTRAIN_UI_PORT=8080

COPY --from=build /wheels /wheels
# tzdata: slim images may lack system timezone files; timetables need them.
RUN pip install --no-cache-dir /wheels/*.whl tzdata \
 && rm -rf /wheels \
 && useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin deploytrain \
 && mkdir -p /config /state \
 && chown deploytrain:deploytrain /state

WORKDIR /config
USER 10001:10001
VOLUME ["/state"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-m", "deploytrain.healthcheck"]

# `docker stop` sends SIGTERM: a `depart` job pulls its train's emergency brake, and the UI
# brakes the trains it dispatched, so journeys stop cleanly and can be resumed.
STOPSIGNAL SIGTERM
ENTRYPOINT ["deploytrain"]
CMD ["ui"]
