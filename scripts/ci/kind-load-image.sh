#!/usr/bin/env bash
# Load ONE image into a kind cluster, linux/amd64 only, and fail closed.
#
# Usage: scripts/ci/kind-load-image.sh IMAGE CLUSTER
#
# Why not `docker pull` + `kind load docker-image`: with Docker's
# containerd image store, `docker save` of a multi-platform image keeps
# the index but not the other platforms' blobs, and it adds the image's
# attestation manifest as a second top-level entry. kind imports with
# `ctr images import --all-platforms`, which then fails with
# "content digest ... not found" (redis-exporter) or "mismatched image
# rootfs and manifest layers" (chroma). skopeo copies the amd64 image
# from the registry into a plain docker-archive with one image and no
# attestation, which kind loads cleanly.
#
# Any failure exits non-zero, so the calling step fails at once and
# names the image instead of a helm --wait timeout much later.
set -euo pipefail

img="${1:?usage: kind-load-image.sh IMAGE CLUSTER}"
cluster="${2:?usage: kind-load-image.sh IMAGE CLUSTER}"

auth_args=()
authfile="${DOCKER_CONFIG:-${HOME}/.docker}/config.json"
if [[ -f "${authfile}" ]]; then
  auth_args=(--authfile "${authfile}")
fi

workdir="$(mktemp -d)"
trap 'rm -rf -- "${workdir:?}"' EXIT
archive="${workdir}/image.tar"

skopeo --override-os linux --override-arch amd64 copy --retry-times 3 \
  "${auth_args[@]}" "docker://${img}" "docker-archive:${archive}:${img}"
kind load image-archive "${archive}" --name "${cluster}"
echo "loaded ${img} into kind cluster ${cluster} (linux/amd64)"
