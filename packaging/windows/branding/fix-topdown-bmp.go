// Command fix-topdown-bmp converts an uncompressed top-down Windows BMP into
// the bottom-up form accepted by NSIS Modern UI.
package main

import (
	"encoding/binary"
	"fmt"
	"os"
)

func main() {
	if len(os.Args) != 3 {
		fmt.Fprintln(os.Stderr, "usage: fix-topdown-bmp input.bmp output.bmp")
		os.Exit(2)
	}
	data, err := os.ReadFile(os.Args[1])
	if err != nil {
		panic(err)
	}
	if len(data) < 54 || string(data[:2]) != "BM" {
		panic("input is not a Windows BMP")
	}
	offset := int(binary.LittleEndian.Uint32(data[10:14]))
	width := int(int32(binary.LittleEndian.Uint32(data[18:22])))
	height := int32(binary.LittleEndian.Uint32(data[22:26]))
	bits := int(binary.LittleEndian.Uint16(data[28:30]))
	compression := binary.LittleEndian.Uint32(data[30:34])
	if width <= 0 || height >= 0 || bits != 24 || compression != 0 {
		panic("expected an uncompressed 24-bit top-down BMP")
	}
	rows := int(-height)
	stride := ((width*bits + 31) / 32) * 4
	if offset < 54 || offset+rows*stride > len(data) {
		panic("invalid BMP row layout")
	}
	output := append([]byte(nil), data...)
	binary.LittleEndian.PutUint32(output[22:26], uint32(rows))
	for row := 0; row < rows; row++ {
		source := offset + row*stride
		target := offset + (rows-1-row)*stride
		copy(output[target:target+stride], data[source:source+stride])
	}
	if err := os.WriteFile(os.Args[2], output, 0o644); err != nil {
		panic(err)
	}
}
