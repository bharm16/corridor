# Linux x86-64 runner for the PDF-engine bakeoff.
#
# The experiment lock pins manylinux_2_28 x86-64 wheels by direct URL (#720), so
# the engines cannot install on macOS or on arm64 Linux. This image is the only
# place the three adapters can run together outside CI.
#
# Bookworm ships glibc 2.36, satisfying manylinux_2_28.
FROM --platform=linux/amd64 python:3.12-slim-bookworm

# tesseract-ocr is required, not optional: the downstream OCR comparison fails
# rather than skips when it is absent (#722).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /repo
ENV UV_NO_SYNC=0
