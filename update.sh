#!/usr/bin/env bash
docker run --rm \
 -v "${PWD}":/config \
 -v "${PWD}/.cache":/cache \
 -v "${PWD}/.build":/build \
 --workdir /config \
 -e PLATFORMIO_CORE_DIR=/cache/.plattformio \
 -e PLATFORMIO_GLOBALLIB_DIR=/cache/.plattformioLibs \
 -u $(id -u ${USER}):$(id -g ${USER}) \
 -it ghcr.io/esphome/esphome:latest update-all .

