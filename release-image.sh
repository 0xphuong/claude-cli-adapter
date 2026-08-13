#!/usr/bin/env bash
# Build and publish the container image.
#
#   ./release-image.sh v0.2.0                 # build linux/amd64 and push
#   ./release-image.sh v0.2.0 --latest        # also push the :latest tag
#   ./release-image.sh v0.2.0 --no-push       # build and load locally, no push
#   PLATFORM=linux/amd64,linux/arm64 ./release-image.sh v0.2.0
#
# Override the repository without editing this file:
#   IMAGE=docker.io/you/other-name ./release-image.sh v0.2.0

set -euo pipefail

IMAGE="${IMAGE:-docker.io/binhphuong/claude-cli-adapter}"
PLATFORM="${PLATFORM:-linux/amd64}"

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
note() { printf '\033[36m==>\033[0m %s\n' "$*"; }

# --- arguments ---------------------------------------------------------------
TAG=""; PUSH=1; ALSO_LATEST=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-push) PUSH=0 ;;
    --latest)  ALSO_LATEST=1 ;;
    --platform) shift; PLATFORM="${1:?--platform needs a value}" ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option: $1" ;;
    *) [ -z "$TAG" ] || die "tag given twice: '$TAG' and '$1'"; TAG="$1" ;;
  esac
  shift
done
[ -n "$TAG" ] || die "usage: $0 <tag> [--latest] [--no-push] [--platform p]"

cd "$(dirname "$0")"

# --- provenance --------------------------------------------------------------
# Stamp the commit into the image so a running container can be traced back to
# source. A dirty tree gets a "-dirty" suffix rather than a silent mismatch.
REVISION="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
if ! git diff --quiet HEAD 2>/dev/null; then
  REVISION="${REVISION}-dirty"
  note "working tree is dirty — image will be labelled ${REVISION}"
fi
if git rev-parse "$TAG" >/dev/null 2>&1; then
  [ "$(git rev-parse "$TAG^{commit}")" = "$(git rev-parse HEAD)" ] \
    || note "warning: git tag $TAG does not point at HEAD"
else
  note "warning: no git tag named $TAG (building anyway)"
fi

# Keep the CLI version identical to what .env pins, so an image built here
# matches what compose runs locally.
CLAUDE_CODE_VERSION="$(sed -n 's/^CLAUDE_CODE_VERSION=//p' .env 2>/dev/null | tail -1)"
CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION:-$(sed -n 's/^ARG CLAUDE_CODE_VERSION=//p' Dockerfile | tail -1)}"

# --- build -------------------------------------------------------------------
args=(
  --platform "$PLATFORM"
  --build-arg "CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}"
  --label "org.opencontainers.image.revision=${REVISION}"
  --label "org.opencontainers.image.version=${TAG}"
  --label "org.opencontainers.image.source=https://github.com/0xphuong/claude-cli-adapter"
  -t "${IMAGE}:${TAG}"
)
[ "$ALSO_LATEST" -eq 1 ] && args+=(-t "${IMAGE}:latest")

if [ "$PUSH" -eq 1 ]; then
  # Push straight from the builder: a multi-platform result cannot be --load'ed
  # into the local docker store at all, and the active builder may use the
  # docker-container driver, where a plain build leaves nothing behind locally.
  args+=(--push)
else
  case "$PLATFORM" in
    *,*) die "--no-push cannot load a multi-platform build; build one platform at a time" ;;
  esac
  args+=(--load)
fi

note "image      ${IMAGE}:${TAG}$([ "$ALSO_LATEST" -eq 1 ] && echo ' + :latest')"
note "platform   ${PLATFORM}"
note "CLI pinned ${CLAUDE_CODE_VERSION}"
note "revision   ${REVISION}"
note "$([ "$PUSH" -eq 1 ] && echo 'building and PUSHING' || echo 'building locally (no push)')"

docker buildx build "${args[@]}" .

# --- report ------------------------------------------------------------------
if [ "$PUSH" -eq 1 ]; then
  # Confirm every tag actually resolves in the registry, not just the build
  # exiting 0 — and print each digest so ":latest" can be checked against the
  # version tag it is supposed to alias.
  pushed=("${TAG}")
  [ "$ALSO_LATEST" -eq 1 ] && pushed+=("latest")
  note "pushed ${#pushed[@]} tag(s):"
  for t in "${pushed[@]}"; do
    d=$(docker buildx imagetools inspect "${IMAGE}:${t}" 2>/dev/null \
          | sed -n 's/^Digest: *//p' | head -1)
    printf '    %-40s %s\n' "${IMAGE}:${t}" "${d:-<not resolvable>}"
  done
  echo
  note "pull with:  docker pull ${IMAGE}:${TAG}"
else
  note "loaded locally:"
  # docker strips the docker.io/ prefix in the local store, so a lookup by the
  # fully qualified name finds nothing.
  docker images "${IMAGE#docker.io/}" \
    --format '    {{.Repository}}:{{.Tag}}  {{.Size}}  {{.ID}}' | head -5
  docker image inspect "${IMAGE#docker.io/}:${TAG}" \
    --format '    platform: {{.Os}}/{{.Architecture}}' 2>/dev/null || true
fi
