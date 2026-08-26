#!/bin/bash
# macOS 源码开发兼容入口：显式打开本机 UI。
cd "$(dirname "$0")"
exec ./启动-Mac.command --local-ui
