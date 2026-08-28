//go:build windows

package main

import (
	"net"
	"os"
	"testing"
)

func TestActiveTaskNoteUsesFirstTaskAndElapsedTime(t *testing.T) {
	status := &localStatus{}
	status.Tasks.Items = append(status.Tasks.Items, struct {
		Label      string `json:"label"`
		ElapsedSec int    `json:"elapsedSec"`
	}{Label: "查询订单物流", ElapsedSec: 96})

	if got, want := activeTaskNote(status), "查询订单物流 · 01:36"; got != want {
		t.Fatalf("activeTaskNote() = %q, want %q", got, want)
	}
}

func TestActiveTaskNoteFallsBackWithoutItems(t *testing.T) {
	if got, want := activeTaskNote(&localStatus{}), "任务正在本机安全执行"; got != want {
		t.Fatalf("activeTaskNote() = %q, want %q", got, want)
	}
}

func TestCloudStatusTextCoversPairingAndOfflineStates(t *testing.T) {
	tests := map[string]string{
		"online":     "云端在线",
		"not_paired": "等待配对",
		"offline":    "云端离线",
	}
	for input, want := range tests {
		if got := cloudStatusText(input); got != want {
			t.Fatalf("cloudStatusText(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestStatusPortOnlyAcceptsLoopbackHTTP(t *testing.T) {
	if got, want := statusPort("http://127.0.0.1:8765/executor-status.json"), 8765; got != want {
		t.Fatalf("statusPort(loopback) = %d, want %d", got, want)
	}
	for _, raw := range []string{
		"https://127.0.0.1:8765/executor-status.json",
		"http://example.test:8765/executor-status.json",
		"http://127.0.0.1:not-a-port/executor-status.json",
	} {
		if got := statusPort(raw); got != 0 {
			t.Fatalf("statusPort(%q) = %d, want 0", raw, got)
		}
	}
}

func TestTCPListenerPIDsFindsCurrentProcess(t *testing.T) {
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	port := listener.Addr().(*net.TCPAddr).Port
	pids, err := tcpListenerPIDs(port)
	if err != nil {
		t.Fatal(err)
	}
	for _, pid := range pids {
		if pid == os.Getpid() {
			return
		}
	}
	t.Fatalf("tcpListenerPIDs(%d) = %v, want current PID %d", port, pids, os.Getpid())
}
