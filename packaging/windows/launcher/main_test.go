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

func TestManagedExecutorStopScriptIsScopedToInstallRoot(t *testing.T) {
	script := managedExecutorStopScript()
	for _, required := range []string{
		"pythonw.exe",
		"ExecutablePath",
		"StartsWith",
		`run\.py\s+--no-browser`,
	} {
		if !strings.Contains(script, required) {
			t.Fatalf("managedExecutorStopScript() must contain %q", required)
		}
	}
	for _, forbidden := range []string{"taskkill", "/IM pythonw.exe"} {
		if strings.Contains(strings.ToLower(script), strings.ToLower(forbidden)) {
			t.Fatalf("managedExecutorStopScript() must not contain broad kill %q", forbidden)
		}
	}
}
