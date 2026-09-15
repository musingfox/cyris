#!/usr/bin/env bash
# Render a deployable copy of wrangler.toml that points at a published registry
# image instead of building the Dockerfile.
#
# The tracked file cannot carry the registry path: docs/spec/wrangler-toml-stays-
# fork-neutral.md requires it to stay deployable by a stranger who has only
# cloned this repository, which rules out an account id anywhere in it. So the
# deployer identity is supplied here, at deploy time, and the result is a build
# artifact — generated, used, never committed.
#
# Usage: derive-wrangler-config.sh <account_id> <image_name> <tag> [out_path]
set -euo pipefail

account_id=${1:?account id required}
image_name=${2:?image name required}
tag=${3:?image tag or digest required}
out=${4:-wrangler.deploy.toml}

src=$(dirname "$0")/../wrangler.toml

# A tag names something mutable; a digest names one image forever. Both are
# accepted because they answer different questions — ":release" is what a
# routine deploy asks for, "@sha256:…" is what pinning to a known-good image
# asks for, and the reference is written the way the registry expects each.
case "$tag" in
  sha256:*) reference="registry.cloudflare.com/$account_id/$image_name@$tag" ;;
  *) reference="registry.cloudflare.com/$account_id/$image_name:$tag" ;;
esac

# Anchored on the exact line the tracked file carries. A near-miss must fail
# loudly rather than deploy a config that still builds locally: that would
# publish an image with no build sha and look like a successful deploy.
matches=$(grep -c '^image = "\./Dockerfile"$' "$src")
if [ "$matches" != "1" ]; then
  echo "wrangler.toml has $matches lines reading 'image = \"./Dockerfile\"', expected exactly 1" >&2
  exit 1
fi

awk -v ref="$reference" '
  $0 == "image = \"./Dockerfile\"" { print "image = \"" ref "\""; next }
  { print }
' "$src" >"$out"

echo "$out"
