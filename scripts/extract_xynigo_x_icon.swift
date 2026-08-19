#!/usr/bin/env swift

import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

// Extracts the original Xynigo "X" artwork without redrawing it.
// The input should be a tight crop containing the X and the adjacent `y` edge.
// Only the largest non-transparent component is retained. In the master asset,
// the X body, navy rear stroke and coral accent form one anti-aliased component;
// the adjacent `y` edge is separate. Every retained pixel comes from the source.

guard CommandLine.arguments.count == 3 || CommandLine.arguments.count == 4 else {
    fputs("Usage: extract_xynigo_x_icon.swift INPUT.png OUTPUT.png [OUTPUT.ico]\n", stderr)
    exit(64)
}

let inputURL = URL(fileURLWithPath: CommandLine.arguments[1])
let outputURL = URL(fileURLWithPath: CommandLine.arguments[2])

guard
    let source = CGImageSourceCreateWithURL(inputURL as CFURL, nil),
    let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
else {
    fputs("Unable to load source image: \(inputURL.path)\n", stderr)
    exit(65)
}

let width = image.width
let height = image.height
let bytesPerPixel = 4
let bytesPerRow = width * bytesPerPixel
var pixels = [UInt8](repeating: 0, count: height * bytesPerRow)

guard let context = CGContext(
    data: &pixels,
    width: width,
    height: height,
    bitsPerComponent: 8,
    bytesPerRow: bytesPerRow,
    space: CGColorSpaceCreateDeviceRGB(),
    bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        | CGBitmapInfo.byteOrder32Big.rawValue
) else {
    fputs("Unable to create RGBA context\n", stderr)
    exit(70)
}

context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))

let pixelCount = width * height
var labels = [Int](repeating: -1, count: pixelCount)
var componentSizes: [Int] = []
var queue = [Int]()
queue.reserveCapacity(pixelCount)

func alpha(at index: Int) -> UInt8 {
    pixels[index * bytesPerPixel + 3]
}

for start in 0..<pixelCount where labels[start] == -1 && alpha(at: start) > 0 {
    let label = componentSizes.count
    var size = 0
    queue.removeAll(keepingCapacity: true)
    queue.append(start)
    labels[start] = label
    var cursor = 0

    while cursor < queue.count {
        let current = queue[cursor]
        cursor += 1
        size += 1
        let x = current % width
        let y = current / width

        for dy in -1...1 {
            for dx in -1...1 where dx != 0 || dy != 0 {
                let nx = x + dx
                let ny = y + dy
                guard nx >= 0, nx < width, ny >= 0, ny < height else { continue }
                let neighbor = ny * width + nx
                guard labels[neighbor] == -1, alpha(at: neighbor) > 0 else { continue }
                labels[neighbor] = label
                queue.append(neighbor)
            }
        }
    }

    componentSizes.append(size)
}

let keptLabels = Set(
    componentSizes.indices
        .sorted { componentSizes[$0] > componentSizes[$1] }
        .prefix(1)
)

guard keptLabels.count == 1 else {
    fputs("Expected at least one visible component, found \(componentSizes.count)\n", stderr)
    exit(65)
}

for index in 0..<pixelCount where !keptLabels.contains(labels[index]) {
    let offset = index * bytesPerPixel
    pixels[offset] = 0
    pixels[offset + 1] = 0
    pixels[offset + 2] = 0
    pixels[offset + 3] = 0
}

guard
    let provider = CGDataProvider(data: Data(pixels) as CFData),
    let outputImage = CGImage(
        width: width,
        height: height,
        bitsPerComponent: 8,
        bitsPerPixel: 32,
        bytesPerRow: bytesPerRow,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGBitmapInfo(rawValue:
            CGImageAlphaInfo.premultipliedLast.rawValue
                | CGBitmapInfo.byteOrder32Big.rawValue
        ),
        provider: provider,
        decode: nil,
        shouldInterpolate: true,
        intent: .defaultIntent
    ),
    let destination = CGImageDestinationCreateWithURL(
        outputURL as CFURL,
        UTType.png.identifier as CFString,
        1,
        nil
    )
else {
    fputs("Unable to create output image\n", stderr)
    exit(70)
}

CGImageDestinationAddImage(destination, outputImage, nil)
guard CGImageDestinationFinalize(destination) else {
    fputs("Unable to write output image: \(outputURL.path)\n", stderr)
    exit(74)
}

if CommandLine.arguments.count == 4 {
    let iconURL = URL(fileURLWithPath: CommandLine.arguments[3])
    let iconWidth = 256
    let iconHeight = 256
    let iconBytesPerRow = iconWidth * bytesPerPixel
    var iconPixels = [UInt8](repeating: 0, count: iconHeight * iconBytesPerRow)

    guard let iconContext = CGContext(
        data: &iconPixels,
        width: iconWidth,
        height: iconHeight,
        bitsPerComponent: 8,
        bytesPerRow: iconBytesPerRow,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            | CGBitmapInfo.byteOrder32Big.rawValue
    ) else {
        fputs("Unable to create ICO render context\n", stderr)
        exit(70)
    }

    iconContext.interpolationQuality = .high
    iconContext.draw(
        outputImage,
        in: CGRect(x: 0, y: 0, width: iconWidth, height: iconHeight)
    )

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

    let maskBytesPerRow = ((iconWidth + 31) / 32) * 4
    let bitmapBytes = 40 + iconWidth * iconHeight * 4 + maskBytesPerRow * iconHeight
    var ico = Data()

    // ICONDIR and ICONDIRENTRY.
    appendLE16(0, to: &ico)
    appendLE16(1, to: &ico)
    appendLE16(1, to: &ico)
    ico.append(0) // 0 represents 256 pixels.
    ico.append(0)
    ico.append(0)
    ico.append(0)
    appendLE16(1, to: &ico)
    appendLE16(32, to: &ico)
    appendLE32(UInt32(bitmapBytes), to: &ico)
    appendLE32(22, to: &ico)

    // BITMAPINFOHEADER. ICO stores twice the visible height (XOR + AND masks).
    appendLE32(40, to: &ico)
    appendLE32(UInt32(iconWidth), to: &ico)
    appendLE32(UInt32(iconHeight * 2), to: &ico)
    appendLE16(1, to: &ico)
    appendLE16(32, to: &ico)
    appendLE32(0, to: &ico)
    appendLE32(UInt32(iconWidth * iconHeight * 4), to: &ico)
    appendLE32(0, to: &ico)
    appendLE32(0, to: &ico)
    appendLE32(0, to: &ico)
    appendLE32(0, to: &ico)

    // The DIB pixel array is bottom-up BGRA. Convert premultiplied RGBA back to
    // straight color channels so translucent anti-aliased edges remain correct.
    for y in stride(from: iconHeight - 1, through: 0, by: -1) {
        for x in 0..<iconWidth {
            let offset = y * iconBytesPerRow + x * bytesPerPixel
            let alpha = Int(iconPixels[offset + 3])
            func straight(_ channel: UInt8) -> UInt8 {
                guard alpha > 0 else { return 0 }
                return UInt8(min(255, (Int(channel) * 255 + alpha / 2) / alpha))
            }
            ico.append(straight(iconPixels[offset + 2]))
            ico.append(straight(iconPixels[offset + 1]))
            ico.append(straight(iconPixels[offset]))
            ico.append(UInt8(alpha))
        }
    }

    // The 1-bit AND mask is kept for older ICO readers.
    for y in stride(from: iconHeight - 1, through: 0, by: -1) {
        var row = [UInt8](repeating: 0, count: maskBytesPerRow)
        for x in 0..<iconWidth {
            let alpha = iconPixels[y * iconBytesPerRow + x * bytesPerPixel + 3]
            if alpha == 0 {
                row[x / 8] |= UInt8(0x80 >> (x % 8))
            }
        }
        ico.append(contentsOf: row)
    }

    do {
        try ico.write(to: iconURL, options: .atomic)
    } catch {
        fputs("Unable to write ICO: \(error)\n", stderr)
        exit(74)
    }
}

let keptSizes = keptLabels.map { componentSizes[$0] }.sorted(by: >)
print("Kept original components \(keptSizes); removed \(componentSizes.count - keptLabels.count) adjacent/noise components")
