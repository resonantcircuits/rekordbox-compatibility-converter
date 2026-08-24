#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/versions.env"

if [[ "$(uname -s)" == "Darwin" ]]; then
    export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-11.0}"
fi

OUTPUT_DIR="${1:-$PROJECT_ROOT/build-vendor/minimal-ffmpeg}"
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
if [[ "$OUTPUT_DIR" == "/" || "$OUTPUT_DIR" == "$PROJECT_ROOT" ]]; then
    echo "Refusing unsafe FFmpeg build output directory: $OUTPUT_DIR" >&2
    exit 2
fi
DOWNLOAD_DIR="$OUTPUT_DIR/downloads"
SOURCE_DIR="$OUTPUT_DIR/source-tree"
PREFIX_DIR="$OUTPUT_DIR/prefix"
BIN_DIR="$OUTPUT_DIR/bin"
LEGAL_DIR="$OUTPUT_DIR/legal"
CORRESPONDING_SOURCE_DIR="$OUTPUT_DIR/corresponding-source"

FFMPEG_ARCHIVE="ffmpeg-$FFMPEG_VERSION.tar.xz"
LAME_ARCHIVE="lame-$LAME_VERSION.tar.gz"
FFMPEG_URL="https://ffmpeg.org/releases/$FFMPEG_ARCHIVE"
LAME_URL="https://downloads.sourceforge.net/project/lame/lame/$LAME_VERSION/$LAME_ARCHIVE"

mkdir -p "$DOWNLOAD_DIR" "$SOURCE_DIR" "$PREFIX_DIR" "$BIN_DIR"

verify_sha256() {
    local expected="$1"
    local path="$2"
    if command -v shasum >/dev/null 2>&1; then
        printf '%s  %s\n' "$expected" "$path" | shasum -a 256 -c -
    else
        printf '%s  %s\n' "$expected" "$path" | sha256sum -c -
    fi
}

download_source() {
    local url="$1"
    local expected="$2"
    local destination="$3"
    if [[ ! -f "$destination" ]]; then
        curl --fail --location --retry 3 --output "$destination" "$url"
    fi
    verify_sha256 "$expected" "$destination"
}

download_source "$FFMPEG_URL" "$FFMPEG_SHA256" "$DOWNLOAD_DIR/$FFMPEG_ARCHIVE"
download_source "$LAME_URL" "$LAME_SHA256" "$DOWNLOAD_DIR/$LAME_ARCHIVE"

rm -rf "$SOURCE_DIR/ffmpeg-$FFMPEG_VERSION" "$SOURCE_DIR/lame-$LAME_VERSION"
tar -xf "$DOWNLOAD_DIR/$FFMPEG_ARCHIVE" -C "$SOURCE_DIR"
tar -xf "$DOWNLOAD_DIR/$LAME_ARCHIVE" -C "$SOURCE_DIR"

PROCESSORS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 2)"

pushd "$SOURCE_DIR/lame-$LAME_VERSION" >/dev/null
./configure \
    --prefix="$PREFIX_DIR" \
    --disable-shared \
    --enable-static \
    --disable-frontend
make -j"$PROCESSORS"
make install
popd >/dev/null

export PKG_CONFIG_PATH="$PREFIX_DIR/lib/pkgconfig"
pushd "$SOURCE_DIR/ffmpeg-$FFMPEG_VERSION" >/dev/null
./configure \
    --prefix="$PREFIX_DIR" \
    --pkg-config-flags=--static \
    --extra-cflags="-I$PREFIX_DIR/include" \
    --extra-ldflags="-L$PREFIX_DIR/lib" \
    --disable-autodetect \
    --disable-everything \
    --disable-doc \
    --disable-debug \
    --disable-network \
    --disable-shared \
    --enable-static \
    --enable-pic \
    --enable-small \
    --enable-ffmpeg \
    --enable-ffprobe \
    --enable-avcodec \
    --enable-avformat \
    --enable-avfilter \
    --disable-avdevice \
    --enable-swresample \
    --enable-swscale \
    --enable-libmp3lame \
    --enable-zlib \
    --enable-protocol=file,pipe \
    --enable-demuxer=aiff,flac,mov,mp3,wav \
    --enable-muxer=aiff,hash,mp3,wav \
    --enable-parser=aac,flac,mpegaudio \
    --enable-decoder=aac,alac,flac,mjpeg,mp3,mp3float,png \
    --enable-decoder=pcm_f32be,pcm_f32le,pcm_f64be,pcm_f64le \
    --enable-decoder=pcm_s8,pcm_s16be,pcm_s16le,pcm_s24be,pcm_s24le,pcm_s32be,pcm_s32le,pcm_u8 \
    --enable-encoder=libmp3lame,mjpeg,pcm_s16be,pcm_s16le,pcm_s24be,pcm_s24le \
    --enable-filter=aresample,format,scale
make -j"$PROCESSORS"
make install
popd >/dev/null

EXECUTABLE_SUFFIX=""
if [[ -f "$PREFIX_DIR/bin/ffmpeg.exe" ]]; then
    EXECUTABLE_SUFFIX=".exe"
fi
cp "$PREFIX_DIR/bin/ffmpeg$EXECUTABLE_SUFFIX" "$BIN_DIR/ffmpeg$EXECUTABLE_SUFFIX"
cp "$PREFIX_DIR/bin/ffprobe$EXECUTABLE_SUFFIX" "$BIN_DIR/ffprobe$EXECUTABLE_SUFFIX"

rm -rf "$LEGAL_DIR" "$CORRESPONDING_SOURCE_DIR"
mkdir -p "$LEGAL_DIR/ffmpeg" "$LEGAL_DIR/lame" "$CORRESPONDING_SOURCE_DIR"
cp "$SOURCE_DIR/ffmpeg-$FFMPEG_VERSION/COPYING.LGPLv2.1" "$LEGAL_DIR/ffmpeg/"
cp "$SOURCE_DIR/ffmpeg-$FFMPEG_VERSION/LICENSE.md" "$LEGAL_DIR/ffmpeg/"
cp "$SOURCE_DIR/lame-$LAME_VERSION/COPYING" "$LEGAL_DIR/lame/"
cp "$SOURCE_DIR/lame-$LAME_VERSION/LICENSE" "$LEGAL_DIR/lame/"

cp "$DOWNLOAD_DIR/$FFMPEG_ARCHIVE" "$CORRESPONDING_SOURCE_DIR/"
cp "$DOWNLOAD_DIR/$LAME_ARCHIVE" "$CORRESPONDING_SOURCE_DIR/"
cp "$SCRIPT_DIR/build_minimal_ffmpeg.sh" "$CORRESPONDING_SOURCE_DIR/"
cp "$SCRIPT_DIR/versions.env" "$CORRESPONDING_SOURCE_DIR/"
cp "$SCRIPT_DIR/CORRESPONDING_SOURCE_README.md" "$CORRESPONDING_SOURCE_DIR/README.md"
tar -czf "$OUTPUT_DIR/corresponding-source.tar.gz" \
    -C "$OUTPUT_DIR" corresponding-source

"$BIN_DIR/ffmpeg$EXECUTABLE_SUFFIX" -version
"$BIN_DIR/ffmpeg$EXECUTABLE_SUFFIX" -L
