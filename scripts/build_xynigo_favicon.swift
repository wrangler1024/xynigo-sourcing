#!/usr/bin/env swift

import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

guard CommandLine.arguments.count == 3 else {
    fputs("Usage: build_xynigo_favicon.swift INPUT.png OUTPUT.ico\n", stderr)
    exit(64)
}

let inputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let outputURL = URL(fileURLWithPath: CommandLine.arguments[2])

guard
    let source = CGImageSourceCreateWithURL(inputURL as CFURL, nil),
    let sourceImage = CGImageSourceCreateImageAtIndex(source, 0, nil)
else {
    fputs("Unable to load PNG: \(inputURL.path)\n", stderr)
    exit(65)
}

func appendLE16(_ value: UInt16, to data: inout Data) {
    data.append(UInt8(truncatingIfNeeded: value))
    data.append(UInt8(truncatingIfNeeded: value >> 8))
}

func appendLE32(_ value: UInt32, to data: inout Data) {
    data.append(UInt8(truncatingIfNeeded: value))
    data.append(UInt8(truncatingIfNeeded: value >> 8))
    data.append(UInt8(truncatingIfNeeded: value >> 16))
    data.append(UInt8(truncatingIfNeeded: value >> 24))
}

func pngData(size: Int) -> Data? {
    let bytesPerRow = size * 4
    var pixels = [UInt8](repeating: 0, count: size * bytesPerRow)
    guard let context = CGContext(
        data: &pixels,
        width: size,
        height: size,
        bitsPerComponent: 8,
        bytesPerRow: bytesPerRow,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            | CGBitmapInfo.byteOrder32Big.rawValue
    ) else { return nil }

    context.interpolationQuality = .high
    context.draw(sourceImage, in: CGRect(x: 0, y: 0, width: size, height: size))
    guard let rendered = context.makeImage() else { return nil }

    let mutable = NSMutableData()
    guard let destination = CGImageDestinationCreateWithData(
        mutable,
        UTType.png.identifier as CFString,
        1,
        nil
    ) else { return nil }
    CGImageDestinationAddImage(destination, rendered, nil)
    guard CGImageDestinationFinalize(destination) else { return nil }
    return mutable as Data
}

let sizes = [16, 24, 32, 48, 64, 128, 256]
let images = sizes.compactMap { size -> (Int, Data)? in
    guard let data = pngData(size: size) else { return nil }
    return (size, data)
}
guard images.count == sizes.count else {
    fputs("Unable to render all favicon sizes\n", stderr)
    exit(70)
}

var ico = Data()
appendLE16(0, to: &ico)
appendLE16(1, to: &ico)
appendLE16(UInt16(images.count), to: &ico)

var offset = UInt32(6 + images.count * 16)
for (size, data) in images {
    ico.append(size == 256 ? 0 : UInt8(size))
    ico.append(size == 256 ? 0 : UInt8(size))
    ico.append(0)
    ico.append(0)
    appendLE16(1, to: &ico)
    appendLE16(32, to: &ico)
    appendLE32(UInt32(data.count), to: &ico)
    appendLE32(offset, to: &ico)
    offset += UInt32(data.count)
}
for (_, data) in images {
    ico.append(data)
}

do {
    try ico.write(to: outputURL, options: .atomic)
} catch {
    fputs("Unable to write ICO: \(error)\n", stderr)
    exit(74)
}

print("Wrote Xynigo multi-size favicon: \(sizes)")
