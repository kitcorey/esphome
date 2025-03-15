#!/usr/bin/env bash
# .cache is necessary for platformio to work properly with esp-idf
# https://github.com/esphome/issues/issues/6520
docker run --rm \
 -v "${PWD}":/config \
 -v "${PWD}/.cache":/.cache \
 -v "${PWD}/.build":/.build \
 -v "${HOME}/repos/airgradient_esphome":"${HOME}/repos/airgradient_esphome" \
 --pull always \
 --workdir /config \
 -e PLATFORMIO_CORE_DIR=/.cache/.plattformio \
 -e PLATFORMIO_GLOBALLIB_DIR=/.cache/.plattformioLibs \
 -u $(id -u ${USER}):$(id -g ${USER}) \
 -it ghcr.io/esphome/esphome:latest update-all .

