#!/bin/sh
# 重新编译 macOS 读取器。
#
# 改动 tools/mac/reader.cpp 之后跑一次；产物 colorpass/mac/reader.dylib
# 随包发布，用户不需要自己编译（也不需要装 Xcode）。
#
# 为什么是 universal：用户可能是 Apple Silicon 也可能是 Intel，
# 剪映本身是 arm64 原生，读取器必须能被同一个进程加载。
# 为什么 ad-hoc 签名：剪映的 entitlements 里有 disable-library-validation，
# 允许加载未经过期望签名的动态库；ad-hoc 签名足够，不需要开发者证书。
set -e
cd "$(dirname "$0")/../.."

clang++ -arch arm64 -arch x86_64 -std=c++17 -O2 -dynamiclib \
    -o colorpass/mac/reader.dylib tools/mac/reader.cpp
codesign -f -s - colorpass/mac/reader.dylib

echo "built: colorpass/mac/reader.dylib"
lipo -archs colorpass/mac/reader.dylib
