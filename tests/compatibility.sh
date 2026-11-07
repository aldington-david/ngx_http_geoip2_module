#!/usr/bin/env bash
set -euo pipefail
version=${1:-1.31.8}
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo 'Invalid Nginx version' >&2; exit 1; }
repo=$(cd "$(dirname "$0")/.." && pwd)
work="$repo/.compat-build"
mkdir -p "$work/tmp"
export TMPDIR="$work/tmp"
curl -fsSL --max-time 60 "https://nginx.org/download/nginx-$version.tar.gz" -o "$work/nginx.tar.gz"
tar -xzf "$work/nginx.tar.gz" -C "$work"
curl -fsSL --max-time 30 'https://raw.githubusercontent.com/maxmind/MaxMind-DB/d12e37856df12a62bb4647da66d20feddb8783a6/test-data/GeoIP2-Country-Test.mmdb' -o "$work/Country.mmdb"
cd "$work/nginx-$version"
./configure --prefix="$work/runtime" --with-compat --with-http_ssl_module --with-http_v2_module \
  --with-stream --with-stream_ssl_module --with-stream_ssl_preread_module --with-stream_realip_module \
  --add-dynamic-module="$repo" > "$work/configure.log" 2>&1 || { cat "$work/configure.log"; exit 1; }
make -j2 > "$work/build.log" 2>&1 || { tail -n 100 "$work/build.log"; exit 1; }
./objs/nginx -V
timeout --kill-after=2s 20s python3 "$repo/tests/smoke.py" \
  "$work/nginx-$version/objs/nginx" "$work/nginx-$version/objs" "$work/Country.mmdb" "$work/runtime"
