# OrcaSlicer sandbox image (Phase 5, optional second slicing backend).
#
# The image is built once; each slicing job runs it as an ephemeral container
# with the sandbox flags applied at runtime (never baked into the image):
#
#   docker run --rm --network none --read-only --tmpfs /tmp \
#     --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 \
#     --memory 2g --cpus 2.0 \
#     -v <job-dir>/in:/work:ro -v <job-dir>/out:/out:rw \
#     printforge-orcaslicer \
#     --slice 1 --load-settings "/work/printer.json;/work/process.json" \
#     --load-filaments /work/filament.json \
#     --outputdir /out /work/model.stl
#
# OrcaSlicer is wxWidgets/OpenGL and opens an X display even in CLI mode, so the
# entrypoint wraps the real binary in `xvfb-run -a`; the display is discarded
# with the container.
#
# See README.md in this directory for the full contract.
#
# Never expose this image with network access or a writable root filesystem.

FROM ubuntu:24.04

LABEL org.opencontainers.image.title="printforge-orcaslicer"
LABEL org.opencontainers.image.description="Sandboxed headless OrcaSlicer CLI (Xvfb) for PrintForge slicing jobs"

# Pinned upstream release. Upstream only builds Linux AppImages on Ubuntu 24.04
# (glibc >= 2.38), which is why this image uses ubuntu:24.04 instead of the
# debian:bookworm-slim base of the PrusaSlicer image.
ARG ORCASLICER_VERSION=2.4.2
ARG ORCA_APPIMAGE_SHA256=d12fb8c8eac1aecd2dfb6377acd48f994f8fa439ed5292fa532dd82880f029fd

# Runtime packages:
#   xvfb + xauth        virtual X display; OrcaSlicer's CLI still creates a GUI
#                       toolkit/GL context, so `xvfb-run -a` wraps every run.
#   libopengl0/EGL/GL/GLU  host GL stack. AppRun refuses to start without
#                       libOpenGL.so.0; libgl1-mesa-dri adds the software
#                       rasterizer and LIBGL_ALWAYS_SOFTWARE covers GPU-less hosts.
#   libwebkit2gtk-4.1-0 + libsoup-3.0-0   linked by the AppImage but NOT bundled;
#                       AppRun explicitly checks the host for WebKitGTK 4.1.
#   libgstreamer-plugins-base1.0-0, libsecret-1-0, libwayland-server0
#                       transitive WebKit/GTK runtime deps missing from a minimal rootfs.
#   libmspack0          bundled by the AppImage; installed for robustness.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        xvfb \
        xauth \
        libegl1 \
        libgl1 \
        libgl1-mesa-dri \
        libglu1-mesa \
        libopengl0 \
        libgstreamer-plugins-base1.0-0 \
        libsoup-3.0-0 \
        libwebkit2gtk-4.1-0 \
        libmspack0 \
        libwayland-server0 \
        libsecret-1-0 \
    && rm -rf /var/lib/apt/lists/*

# Download the pinned AppImage, verify its checksum, then extract it. Extracting
# (instead of mounting through FUSE) keeps the runtime free of /dev/fuse,
# --cap-add and --privileged, matching the other sandbox images.
RUN set -eux; \
    url="https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v${ORCASLICER_VERSION}/OrcaSlicer_Linux_AppImage_Ubuntu2404_V${ORCASLICER_VERSION}.AppImage"; \
    curl -fL --retry 3 --retry-delay 2 -o /tmp/orcaslicer.AppImage "$url"; \
    echo "${ORCA_APPIMAGE_SHA256}  /tmp/orcaslicer.AppImage" | sha256sum -c -; \
    chmod +x /tmp/orcaslicer.AppImage; \
    cd /tmp; \
    /tmp/orcaslicer.AppImage --appimage-extract >/dev/null; \
    mv /tmp/squashfs-root /opt/orcaslicer; \
    rm -f /tmp/orcaslicer.AppImage; \
    test -x /opt/orcaslicer/AppRun

# The root filesystem is mounted read-only at runtime; HOME (and everything
# OrcaSlicer writes under it) must land on the /tmp tmpfs. LIBGL_ALWAYS_SOFTWARE
# keeps GL working on hosts without a GPU.
ENV HOME=/tmp
ENV LIBGL_ALWAYS_SOFTWARE=1

# Mount points used by the worker: /work (mesh + generated JSON profiles,
# read-only) and /out (sliced output, read-write).
RUN mkdir -p /work /out

# Run as the SAME user as the worker that creates the job dirs (root in the app
# image). The bind mount replaces the image's /out with a host directory owned
# by that user, so any other UID (e.g. nobody) cannot write the output.
# Isolation comes from the runtime flags, not from the image user:
#   --network none --read-only --tmpfs /tmp --cap-drop ALL
#   --security-opt no-new-privileges --pids-limit 256 --memory ... --cpus ...
WORKDIR /work

# CLI wrapper: runs the extracted AppRun under `xvfb-run -a` so callers just pass
# OrcaSlicer arguments (mirrors ENTRYPOINT ["prusa-slicer"] of the other image).
COPY entrypoint.sh /usr/local/bin/orca-slicer
RUN chmod +x /usr/local/bin/orca-slicer

ENTRYPOINT ["/usr/local/bin/orca-slicer"]
