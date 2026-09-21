# OpenSCAD sandbox image (Phase 2).
#
# The image is built once; each CAD job runs it as an ephemeral container with
# the sandbox flags applied at runtime (never baked into the image itself):
#
#   docker run --rm --network none --read-only --tmpfs /tmp \
#     --cap-drop ALL --security-opt no-new-privileges \
#     --pids-limit 256 --memory 1g --cpus 1.0 \
#     -v <job-dir>/in:/work:ro -v <job-dir>/out:/out:rw \
#     printforge-openscad -o /out/model.stl /work/model.scad
#
# See README.md in this directory for the full contract.
#
# Never expose this image with network access or a writable root filesystem.

FROM openscad/openscad:latest

LABEL org.opencontainers.image.title="printforge-openscad"
LABEL org.opencontainers.image.description="Sandboxed OpenSCAD CLI for PrintForge CAD jobs"

# The root filesystem is mounted read-only at runtime. OpenSCAD may want to
# write its Qt/OpenSCAD preferences, so point HOME at the writable tmpfs.
ENV HOME=/tmp
ENV QT_QPA_PLATFORM=offscreen

# Mount points used by the worker: /work (SCAD input, read-only) and /out (STL
# output, read-write). Created here so they exist even without bind mounts.
RUN mkdir -p /work /out && chown -R nobody:nogroup /work /out

USER nobody:nogroup
WORKDIR /work

ENTRYPOINT ["openscad"]
