# PrusaSlicer sandbox image (Phase 5).
#
# The image is built once; each slicing job runs it as an ephemeral container
# with the sandbox flags applied at runtime (never baked into the image):
#
#   docker run --rm --network none --read-only --tmpfs /tmp \
#     --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 \
#     --memory 2g --cpus 2.0 \
#     -v <job-dir>/in:/work:ro -v <job-dir>/out:/out:rw \
#     printforge-prusaslicer --export-gcode --output /out/model.gcode \
#     --load /work/printer.ini --load /work/filament.ini --load /work/process.ini \
#     /work/model.stl
#
# See README.md in this directory for the full contract.
#
# Never expose this image with network access or a writable root filesystem.

FROM debian:bookworm-slim

LABEL org.opencontainers.image.title="printforge-prusaslicer"
LABEL org.opencontainers.image.description="Sandboxed headless PrusaSlicer CLI for PrintForge slicing jobs"

# prusa-slicer pulls the wxWidgets/OpenGL stack it needs to run headless. No X
# server is required: PrusaSlicer's CLI (--export-gcode) stays off-screen and
# LIBGL_ALWAYS_SOFTWARE covers hosts without a GPU.
RUN apt-get update \
    && apt-get install -y --no-install-recommends prusa-slicer ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The root filesystem is mounted read-only at runtime; HOME and /tmp are the
# writable tmpfs so PrusaSlicer can write preferences/scratch data.
ENV HOME=/tmp
ENV LIBGL_ALWAYS_SOFTWARE=1
ENV QT_QPA_PLATFORM=offscreen

# Mount points used by the worker: /work (mesh + generated ini, read-only) and
# /out (G-code output, read-write).
RUN mkdir -p /work /out

# Run as the SAME user as the worker that creates the job dirs (root in the app
# image). The bind mount replaces the image's /out with a host directory owned
# by that user, so any other UID (e.g. nobody) cannot write the exported G-code.
# Isolation comes from the runtime flags, not from the image user:
#   --network none --read-only --tmpfs /tmp --cap-drop ALL
#   --security-opt no-new-privileges --pids-limit 256 --memory ... --cpus ...
WORKDIR /work

ENTRYPOINT ["prusa-slicer"]
