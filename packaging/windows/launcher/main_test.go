//go:build windows

package main

import (
	"bytes"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func TestLauncherControlIdentitySurvivesRestartAndIsEncrypted(t *testing.T) {
	root := t.TempDir()
	token := loadOrCreateLauncherToken(root)
	if len(token) < 32 || loadOrCreateLauncherToken(root) != token {
		t.Fatal("control identity did not survive reopening")
	}
	stored, err := os.ReadFile(filepath.Join(root, "运行数据", "launcher-token.dpapi"))
	if err != nil || bytes.Contains(stored, []byte(token)) {
		t.Fatal("control identity must be stored with Windows user encryption")
	}
}

func TestShutdownRequiresExplicitIdleApproval(t *testing.T) {
	for _, tc := range []struct {
		status   int
		body     string
		accepted bool
	}{
		{409, `{"stopping":false}`, false},
		{403, `{}`, false},
		{200, `{"stopping":false}`, false},
		{200, `invalid`, false},
		{200, `{"stopping":true}`, true},
	} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Method != "POST" || r.Header.Get("X-Xynigo-Launcher") != "test-control" {
				t.Error("shutdown must be authenticated")
			}
			w.WriteHeader(tc.status)
			io.WriteString(w, tc.body)
		}))
		app := &launcherApp{root: t.TempDir(), launcherToken: "test-control", statusURL: server.URL + "/executor-status.json"}
		if app.stopExecutor() != tc.accepted {
			t.Errorf("unexpected shutdown result: %d %s", tc.status, tc.body)
		}
		server.Close()
	}
}

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
		"online":       "云端在线",
		"reconnecting": "正在重连",
		"not_paired":   "等待配对",
		"offline":      "云端离线",
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

func TestLocalSettingsURLUsesDiscoveredLoopbackPort(t *testing.T) {
	got, err := localSettingsURL(
		"http://127.0.0.1:8767/executor-status.json")
	if err != nil {
		t.Fatal(err)
	}
	if want := "http://127.0.0.1:8767/?view=localsettings"; got != want {
		t.Fatalf("localSettingsURL() = %q, want %q", got, want)
	}
	for _, raw := range []string{
		"https://127.0.0.1:8767/executor-status.json",
		"http://example.test:8767/executor-status.json",
		"http://127.0.0.1/executor-status.json",
	} {
		if _, err := localSettingsURL(raw); err == nil {
			t.Fatalf("localSettingsURL(%q) unexpectedly succeeded", raw)
		}
	}
}

func TestDesktopPageURLUsesDiscoveredLoopbackPort(t *testing.T) {
	got, err := desktopPageURL(
		"http://127.0.0.1:8767/executor-status.json")
	if err != nil {
		t.Fatal(err)
	}
	if want := "http://127.0.0.1:8767/desktop/?platform=windows"; got != want {
		t.Fatalf("desktopPageURL() = %q, want %q", got, want)
	}
	for _, raw := range []string{
		"https://127.0.0.1:8767/executor-status.json",
		"http://example.test:8767/executor-status.json",
		"http://127.0.0.1/executor-status.json",
	} {
		if _, err := desktopPageURL(raw); err == nil {
			t.Fatalf("desktopPageURL(%q) unexpectedly succeeded", raw)
		}
	}
}

func TestDesktopAttemptURLAddsCacheBuster(t *testing.T) {
	got := desktopAttemptURL(
		"http://127.0.0.1:8767/desktop/?platform=windows", 2)
	want := "http://127.0.0.1:8767/desktop/?platform=windows&reload=2"
	if got != want {
		t.Fatalf("desktopAttemptURL() = %q, want %q", got, want)
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
