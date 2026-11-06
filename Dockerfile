# syntax=docker/dockerfile:1.10

# Both stages must stay on the same Debian/Python generation: the uv image IS
# python:3.12-slim-bookworm plus uv, so the venv's interpreter path matches the
# runtime stage exactly. Mixing bases breaks the copied venv.
#
# The digests are spelled out on the FROM lines instead of being held in ARGs:
# Dependabot's Docker updater cannot rewrite an image behind `FROM ${ARG}`, so
# that form left the runtime base frozen until its CVEs failed the Trivy gate.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS builder
ARG TARGETARCH
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src

# Dependency layer on its own so editing src/ does not re-resolve the world.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv,id=uv-${TARGETARCH},sharing=locked \
    uv sync --frozen --no-dev --no-install-project

# README.md is the project's `readme`, so hatchling needs it to build the wheel.
COPY README.md ./
COPY src ./src
# --no-editable: an editable install would leave a .pth pointing at /src, which
# does not exist in the runtime stage.
RUN --mount=type=cache,target=/root/.cache/uv,id=uv-${TARGETARCH},sharing=locked \
    uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS runtime

# gphoto2 CLI: UsbTransport shells out to `gphoto2 --auto-detect`; the python
#   wheel does not ship it. Leaves a second libgphoto2 in the image on purpose.
# rsync + openssh-client: RemoteSync execs both directly.
# iputils-ping: PtpIpTransport.is_reachable() uses ICMP, never a TCP probe.
# Versions verified identical on linux/amd64 and linux/arm64; refresh with
#   apt-cache policy <pkg> in the matching base image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates=20250419~deb12u2 \
        gphoto2=2.5.28-2 \
        iputils-ping=3:20221126-1+deb12u1 \
        libusb-1.0-0=2:1.0.26-1 \
        openssh-client=1:9.2p1-2+deb12u10 \
        rsync=3.2.7-1+deb12u6 \
    && rm -rf /var/lib/apt/lists/*

# Debian ships ping with cap_net_raw+ep; runtimes that drop NET_RAW from the
# bounding set (podman's default) then refuse to even exec it. cp does not
# preserve xattrs, so this strips the file capability without extra packages.
RUN cp /usr/bin/ping /usr/bin/ping.nocap \
    && mv -f /usr/bin/ping.nocap /usr/bin/ping

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /captures

# Visualizer dashboard. Needs --visualizer-host=0.0.0.0 to be reachable.
EXPOSE 8000

LABEL org.opencontainers.image.title="solramp" \
      org.opencontainers.image.description="Sun-driven exposure ramping for 24/7 day-to-night timelapses on the Canon EOS 5D Mark IV over gphoto2" \
      org.opencontainers.image.licenses="MIT"

# The app's graceful shutdown path is SIGINT-driven.
STOPSIGNAL SIGINT

ENTRYPOINT ["solramp"]
CMD ["--help"]
