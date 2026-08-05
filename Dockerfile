# syntax=docker/dockerfile:1.7
#
# Two stages: the builder resolves dependencies into a virtualenv, the runtime
# copies that venv into a clean image. Nothing from the build (compilers, pip
# caches, source tree) survives into the shipped layer.
#
# rasterio, numpy and pyproj all publish manylinux wheels for CPython 3.14 on
# both amd64 and arm64, and rasterio's wheels bundle GDAL and PROJ. That is why
# no system GDAL is installed anywhere here -- and why the build pins those
# three to binary-only, so a missing wheel fails loudly instead of silently
# starting a multi-hour source build of GDAL.

ARG PYTHON_VERSION=3.14

# ---------------------------------------------------------------- builder ---
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy only what the build backend needs before the source, so dependency
# resolution stays cached when application code changes.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --only-binary=rasterio,numpy,pyproj ".[web]"

# ---------------------------------------------------------------- runtime ---
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="reliefkit" \
      org.opencontainers.image.description="Printable 3D terrain models from public-domain elevation data" \
      org.opencontainers.image.source="https://github.com/zfouts/reliefkit" \
      org.opencontainers.image.licenses="MIT"

# Two runtime libraries, both load-bearing:
#
#   ca-certificates -- every elevation source is fetched over HTTPS, and TLS
#                      verification fails outright without a trust store.
#   libexpat1       -- rasterio's wheel bundles GDAL but links libexpat.so.1
#                      dynamically. python:slim does not ship it, so the image
#                      builds cleanly and then dies on `import rasterio`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# Fixed high UID/GID so host bind-mount ownership is predictable. No home
# directory and no login shell -- this account exists only to own the process.
RUN groupadd --system --gid 10001 reliefkit \
    && useradd --system --uid 10001 --gid 10001 \
       --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin reliefkit

# The venv stays root-owned while the process runs as uid 10001, so the
# application cannot rewrite its own code or dependencies at runtime.
COPY --from=builder --chown=root:root --chmod=755 /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1

USER 10001:10001
WORKDIR /tmp
EXPOSE 8000

# Uses the stdlib rather than curl, which slim images do not ship. Hits the
# liveness endpoint, which deliberately does not touch upstream services.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["reliefkit-serve"]
CMD ["--host", "0.0.0.0", "--port", "8000"]
