//go:build windows

package main

import (
	"strings"
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

func TestListenerPIDsOnlySelectsExactLoopbackPort(t *testing.T) {
	output := strings.Join([]string{
		"  Proto  Local Address          Foreign Address        State           PID",
		"  TCP    127.0.0.1:8765         0.0.0.0:0              LISTENING       5056",
		"  TCP    127.0.0.1:8766         0.0.0.0:0              LISTENING       6060",
		"  TCP    0.0.0.0:8765           0.0.0.0:0              LISTENING       7070",
		"  TCP    127.0.0.1:8765         127.0.0.1:51000        ESTABLISHED     5056",
	}, "\r\n")
	pids := listenerPIDs(output, 8765)
	if len(pids) != 1 || pids[0] != 5056 {
		t.Fatalf("listenerPIDs() = %v, want [5056]", pids)
	}
}
