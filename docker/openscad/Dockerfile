# OpenSCAD sandbox image (Phase 2).
#
# The image is built once; each CAD job runs it as an ephemeral container with
# the sandbox flags applied at runtime:
#
#   docker run --rm --network none --read-only --tmpfs /tmp \
#     --cap-drop ALL --security-opt no-new-privileges \
#     --pids-limit 256 --memory 1g --cpus 1.0 \
#     -v <job-dir>:/work:ro -v <out-dir>:/out:rw \
#     printforge-openscad -o /out/model.stl /work/model.scad
#
# Never expose this image with network access or a writable root filesystem.

FROM openscad/openscad:latest

USER nobody:nogroup
WORKDIR /work

ENTRYPOINT ["openscad"]
