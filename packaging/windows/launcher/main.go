//go:build windows

package main

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/bits"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"github.com/jchv/go-webview2/pkg/edge"
	"github.com/lxn/walk"
	. "github.com/lxn/walk/declarative"
)

const (
	cloudWorkspaceURL = "https://xynigo.samforo.icu"
	launcherMutexName = "Local\\XynigoSourcing.Launcher"
	createNoWindow    = 0x08000000
	updateCheckPath   = "/executor-control/update/check"
	updateInstallPath = "/executor-control/update/install"
)

var (
	pairCodePattern = regexp.MustCompile(`(?i)^[A-HJ-NP-Z2-9]{4}-?[A-HJ-NP-Z2-9]{4}$`)
	protocolPattern = regexp.MustCompile(`(?i)^xynigo://(?:start/?|wake/?|pair\?code=([A-HJ-NP-Z2-9]{4}-?[A-HJ-NP-Z2-9]{4}))$`)
	kernel32        = syscall.NewLazyDLL("kernel32.dll")
	createMutexW    = kernel32.NewProc("CreateMutexW")
	iphlpapi        = syscall.NewLazyDLL("iphlpapi.dll")
	getTCPTable     = iphlpapi.NewProc("GetExtendedTcpTable")
)

const (
	afINET                   = 2
	tcpTableOwnerPIDListener = 3
	mibTCPStateListen        = 2
	errorInsufficientBuffer  = 122
)

type mibTCPRowOwnerPID struct {
	State      uint32
	LocalAddr  uint32
	LocalPort  uint32
	RemoteAddr uint32
	RemotePort uint32
	OwningPID  uint32
}

type localStatus struct {
	SchemaVersion int    `json:"schemaVersion"`
	Version       string `json:"version"`
	LocalPort     int    `json:"localPort"`
	Executor      struct {
		Running      bool   `json:"running"`
		Paired       bool   `json:"paired"`
		DisplayName  string `json:"displayName"`
		Platform     string `json:"platform"`
		Architecture string `json:"architecture"`
	} `json:"executor"`
	CloudChannel struct {
		Status        string `json:"status"`
		LastPollAt    string `json:"lastPollAt"`
		LastErrorCode string `json:"lastErrorCode"`
		Phase         string `json:"phase"`
		Attempt       int    `json:"attempt"`
		NextRetryAt   string `json:"nextRetryAt"`
		ConnectedAt   string `json:"connectedAt"`
	} `json:"cloudChannel"`
	HubStudio struct {
		Connected bool   `json:"connected"`
		Status    string `json:"status"`
	} `json:"hubStudio"`
	Tasks struct {
		ActiveCount  int  `json:"activeCount"`
		SafeParallel bool `json:"safeParallel"`
		Items        []struct {
			Label      string `json:"label"`
			ElapsedSec int    `json:"elapsedSec"`
		} `json:"items"`
	} `json:"tasks"`
	Update struct {
		Enabled                     bool   `json:"enabled"`
		State                       string `json:"state"`
		Stage                       string `json:"stage"`
		InstallMode                 string `json:"installMode"`
		InstallFlow                 string `json:"installFlow"`
		CurrentVersion              string `json:"currentVersion"`
		CurrentRuntimeID            string `json:"currentRuntimeId"`
		LatestVersion               string `json:"latestVersion"`
		LatestRuntimeID             string `json:"latestRuntimeId"`
		Message                     string `json:"message"`
		DownloadReceivedBytes       int64  `json:"downloadReceivedBytes"`
		DownloadTotalBytes          int64  `json:"downloadTotalBytes"`
		DownloadPercent             int    `json:"downloadPercent"`
		DownloadSpeedBytesPerSecond int64  `json:"downloadSpeedBytesPerSecond"`
		DownloadEtaSeconds          *int   `json:"downloadEtaSeconds"`
	} `json:"update"`
}

type commandEnvelope struct {
	Command   string `json:"command"`
	CreatedAt string `json:"createdAt"`
}

type launcherApp struct {
	root string

	mw             *walk.MainWindow
	browser        *edge.Chromium
	notify         *walk.NotifyIcon
	statusSignal   *walk.Label
	statusTitle    *walk.Label
	statusDetail   *walk.Label
	cloudSignal    *walk.Label
	cloudValue     *walk.Label
	cloudNote      *walk.Label
	hubSignal      *walk.Label
	hubValue       *walk.Label
	hubNote        *walk.Label
	taskSignal     *walk.Label
	taskValue      *walk.Label
	taskNote       *walk.Label
	versionValue   *walk.Label
	versionNote    *walk.Label
	deviceValue    *walk.Label
	deviceState    *walk.Label
	heartbeatValue *walk.Label
	pairPanel      *walk.Composite
	pairEdit       *walk.LineEdit
	pairButton     *walk.PushButton
	startButton    *walk.PushButton
	updateButton   *walk.PushButton
	updateProgress *walk.ProgressBar
	trayStatus     *walk.Action
	trayCloud      *walk.Action
	trayHub        *walk.Action
	trayPair       *walk.Action
	trayStartStop  *walk.Action
	trayUpdate     *walk.Action

	mu             sync.Mutex
	child          *exec.Cmd
	childDone      chan error
	launcherToken  string
	statusURL      string
	desktopURL     string
	lastStatus     *localStatus
	lastStatusAt   time.Time
	statusFailures int
	pairInFlight   bool
	updateInFlight bool
	exiting        bool
}

func main() {
	root, err := executableDirectory()
	if err != nil {
		walk.MsgBox(nil, "Xynigo 启动失败", err.Error(), walk.MsgBoxIconError)
		return
	}
	command := startupCommand(os.Args[1:])
	appendStatusCenterLog(root, "launcher_process_started command="+command)
	mutex, alreadyRunning, err := acquireLauncherMutex(launcherMutexName)
	if err != nil {
		appendStatusCenterLog(root, "launcher_mutex_failed: "+err.Error())
		walk.MsgBox(nil, "Xynigo 启动失败", "无法创建本地单实例锁。", walk.MsgBoxIconError)
		return
	}
	if alreadyRunning {
		appendStatusCenterLog(root, "launcher_mutex_already_running")
		if err := writeCommand(root, command); err != nil {
			walk.MsgBox(nil, "Xynigo", "状态中心已运行，但启动请求未能转交。", walk.MsgBoxIconWarning)
		}
		return
	}
	defer syscall.CloseHandle(mutex)

	app := &launcherApp{root: root, launcherToken: randomToken()}
	if err := app.buildWindow(); err != nil {
		appendStatusCenterLog(root, "launcher_window_failed: "+err.Error())
		walk.MsgBox(nil, "Xynigo 启动失败", err.Error(), walk.MsgBoxIconError)
		return
	}
	if err := app.buildTray(); err != nil {
		appendStatusCenterLog(root, "launcher_tray_failed: "+err.Error())
		walk.MsgBox(app.mw, "Xynigo 启动失败", err.Error(), walk.MsgBoxIconError)
		return
	}
	defer app.notify.Dispose()
	appendStatusCenterLog(root, "launcher_ui_ready")

	app.mw.Closing().Attach(func(canceled *bool, reason walk.CloseReason) {
		if app.exiting {
			return
		}
		*canceled = true
		app.mw.SetVisible(false)
		_ = app.notify.ShowInfo("Xynigo 继续运行", "本地执行器已最小化到系统托盘。")
	})

	showAtStart := command != "background"
	if showAtStart {
		app.showStatusCenter()
	} else {
		app.mw.SetVisible(false)
	}
	go app.ensureExecutor()
	appendStatusCenterLog(root, "launcher_executor_start_dispatched")
	go app.statusLoop()
	go app.commandLoop()
	if command != "show" && command != "background" {
		go app.handleCommand(command)
	}
	app.mw.Run()
}

func executableDirectory() (string, error) {
	path, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("无法定位安装目录：%w", err)
	}
	return filepath.Dir(path), nil
}

func acquireLauncherMutex(name string) (syscall.Handle, bool, error) {
	pointer, err := syscall.UTF16PtrFromString(name)
	if err != nil {
		return 0, false, err
	}
	handle, _, callErr := createMutexW.Call(0, 0, uintptr(unsafe.Pointer(pointer)))
	if handle == 0 {
		return 0, false, callErr
	}
	already := callErr == syscall.ERROR_ALREADY_EXISTS
	return syscall.Handle(handle), already, nil
}

func startupCommand(args []string) string {
	if len(args) == 0 {
		return "show"
	}
	if args[0] == "--background" {
		return "background"
	}
	if args[0] == "--show" {
		return "show"
	}
	if args[0] == "--protocol" && len(args) == 2 {
		return args[1]
	}
	if args[0] == "--pair" && len(args) == 2 {
		return "pair:" + args[1]
	}
	return "show"
}

func randomToken() string {
	buffer := make([]byte, 32)
	if _, err := rand.Read(buffer); err != nil {
		return strconv.FormatInt(time.Now().UnixNano(), 16)
	}
	return hex.EncodeToString(buffer)
}

func commandPath(root string) string {
	return filepath.Join(root, "运行数据", "launcher-command.json")
}

func writeCommand(root, command string) error {
	directory := filepath.Join(root, "运行数据")
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return err
	}
	payload, _ := json.Marshal(commandEnvelope{
		Command: command, CreatedAt: time.Now().UTC().Format(time.RFC3339Nano),
	})
	temporary := filepath.Join(directory, fmt.Sprintf(".launcher-command-%d.tmp", os.Getpid()))
	if err := os.WriteFile(temporary, payload, 0o600); err != nil {
		return err
	}
	return os.Rename(temporary, commandPath(root))
}

func (app *launcherApp) buildWindow() error {
	if err := walk.Resources.SetRootDirPath(app.root); err != nil {
		return fmt.Errorf("无法定位桌面客户端资源目录：%w", err)
	}
	window := MainWindow{
		AssignTo:   &app.mw,
		Title:      "Xynigo 本地执行器",
		Icon:       "xynigo-x.ico",
		Size:       Size{Width: 1360, Height: 790},
		MinSize:    Size{Width: 1080, Height: 650},
		Background: SolidColorBrush{Color: walk.RGB(255, 255, 255)},
		Font:       Font{Family: "Microsoft YaHei UI", PointSize: 9},
		Layout:     VBox{MarginsZero: true, SpacingZero: true},
		Children: []Widget{
			Composite{
				AssignTo: &app.pairPanel,
				Visible:  false,
				Layout:   VBox{MarginsZero: true, SpacingZero: true},
				Children: []Widget{
					Label{AssignTo: &app.statusSignal},
					Label{AssignTo: &app.statusTitle},
					Label{AssignTo: &app.statusDetail},
					Label{AssignTo: &app.cloudSignal},
					Label{AssignTo: &app.cloudValue},
					Label{AssignTo: &app.cloudNote},
					Label{AssignTo: &app.hubSignal},
					Label{AssignTo: &app.hubValue},
					Label{AssignTo: &app.hubNote},
					Label{AssignTo: &app.taskSignal},
					Label{AssignTo: &app.taskValue},
					Label{AssignTo: &app.taskNote},
					Label{AssignTo: &app.versionValue},
					Label{AssignTo: &app.versionNote},
					Label{AssignTo: &app.deviceValue},
					Label{AssignTo: &app.deviceState},
					Label{AssignTo: &app.heartbeatValue},
					LineEdit{AssignTo: &app.pairEdit},
					PushButton{AssignTo: &app.pairButton},
					PushButton{AssignTo: &app.startButton},
					PushButton{AssignTo: &app.updateButton},
					ProgressBar{AssignTo: &app.updateProgress},
				},
			},
		},
	}
	if err := window.Create(); err != nil {
		return err
	}

	browser := edge.NewChromium()
	browser.DataPath = filepath.Join(app.root, "运行数据", "WebView2")
	browser.MessageCallback = app.handleWebMessage
	if !browser.Embed(uintptr(app.mw.Handle())) {
		return errors.New("WebView2 运行时不可用，请安装 Microsoft Edge WebView2 Runtime")
	}
	if settings, err := browser.GetSettings(); err == nil {
		_ = settings.PutAreDefaultContextMenusEnabled(false)
		_ = settings.PutAreDevToolsEnabled(false)
		_ = settings.PutIsStatusBarEnabled(false)
		_ = settings.PutIsZoomControlEnabled(false)
	}
	browser.NavigateToString(`<!doctype html><meta charset="utf-8"><style>
html,body{height:100%;margin:0;font-family:"Microsoft YaHei UI",sans-serif;color:#123252}
body{display:grid;place-items:center;text-align:center}.x{width:52px;height:52px;margin:auto;display:grid;
place-items:center;border-radius:14px;color:white;font-size:22px;font-weight:800;background:linear-gradient(135deg,#31b8ae,#087c83)}
h2{font-size:16px;margin:18px 0 6px}p{font-size:12px;color:#64748b}</style>
<div><div class="x">X</div><h2>正在启动 Xynigo 本地执行器</h2><p>正在准备 WebView2 与本机安全服务…</p></div>`)
	app.browser = browser
	app.mw.SizeChanged().Attach(browser.Resize)
	return nil
}

func statusCard(
	title string,
	signal, value, note **walk.Label,
	initialValue, initialNote string,
	background, navy, muted, signalColor walk.Color,
) Widget {
	header := []Widget{}
	if signal != nil {
		header = append(header, Label{
			AssignTo: signal, Text: "●", TextColor: signalColor,
			Font:    Font{Family: "Segoe UI Symbol", PointSize: 7, Bold: true},
			MinSize: Size{Width: 10}, TextAlignment: AlignCenter,
		})
	}
	header = append(header, Label{
		Text: title, TextColor: muted,
		Font: Font{Family: "Microsoft YaHei UI", PointSize: 8, Bold: true},
	})
	children := []Widget{
		Composite{Background: SolidColorBrush{Color: background}, Layout: HBox{MarginsZero: true, Spacing: 5}, Children: header},
		Label{AssignTo: value, Text: initialValue, TextColor: navy, Font: Font{Family: "Microsoft YaHei UI", PointSize: 11, Bold: true}},
	}
	if note != nil {
		children = append(children, Label{AssignTo: note, Text: initialNote, TextColor: muted, Font: Font{Family: "Microsoft YaHei UI", PointSize: 8}})
	} else {
		children = append(children, Label{Text: initialNote, TextColor: muted, Font: Font{Family: "Microsoft YaHei UI", PointSize: 8}})
	}
	return Composite{
		Border: true, Background: SolidColorBrush{Color: background}, StretchFactor: 1,
		MinSize:  Size{Height: 84},
		Layout:   VBox{Margins: Margins{Left: 12, Top: 10, Right: 12, Bottom: 10}, Spacing: 3},
		Children: children,
	}
}

func (app *launcherApp) buildTray() error {
	icon, err := walk.NewIconFromFile(filepath.Join(app.root, "xynigo-x.ico"))
	if err != nil {
		return fmt.Errorf("无法加载托盘图标：%w", err)
	}
	notify, err := walk.NewNotifyIcon(app.mw)
	if err != nil {
		return err
	}
	app.notify = notify
	if err := notify.SetIcon(icon); err != nil {
		return err
	}
	if err := notify.SetToolTip("Xynigo 本地执行器 · 正在启动"); err != nil {
		return err
	}
	notify.MouseDown().Attach(func(x, y int, button walk.MouseButton) {
		if button == walk.LeftButton {
			app.showStatusCenter()
		}
	})

	app.trayStatus = newTrayStatusAction("● 本地执行器正在启动")
	app.trayCloud = newTrayStatusAction("云端：正在连接")
	app.trayHub = newTrayStatusAction("HubStudio：正在检查")
	openStatus := newTrayAction("打开桌面客户端", app.showStatusCenter)
	openCloud := newTrayAction("打开云端工作台", app.openCloudWorkspace)
	openLocalSettings := newTrayAction("打开本机设置", app.openLocalSettings)
	app.trayStartStop = newTrayAction("重新启动执行器", func() { go app.restartExecutor() })
	app.trayUpdate = newTrayAction("检查更新", app.handleUpdateAction)
	app.trayPair = newTrayAction("配对这台电脑…", app.showPairing)
	logs := newTrayAction("打开日志目录", app.openLogs)
	about := newTrayAction("关于 Xynigo", func() {
		walk.MsgBox(app.mw, "关于 Xynigo", "Xynigo Sourcing 桌面客户端\n云端业务工作台 · 本机安全执行", walk.MsgBoxIconInformation)
	})
	exit := newTrayAction("退出 Xynigo", app.exitApplication)
	for _, action := range []*walk.Action{
		app.trayStatus, app.trayCloud, app.trayHub, walk.NewSeparatorAction(),
		openStatus, openCloud, openLocalSettings, app.trayPair, walk.NewSeparatorAction(),
		app.trayStartStop, app.trayUpdate, logs, walk.NewSeparatorAction(), about, exit,
	} {
		if err := notify.ContextMenu().Actions().Add(action); err != nil {
			return err
		}
	}
	return notify.SetVisible(true)
}

func newTrayStatusAction(text string) *walk.Action {
	action := walk.NewAction()
	_ = action.SetText(text)
	_ = action.SetEnabled(false)
	return action
}

func newTrayAction(text string, handler func()) *walk.Action {
	action := walk.NewAction()
	_ = action.SetText(text)
	action.Triggered().Attach(handler)
	return action
}

func (app *launcherApp) showStatusCenter() {
	app.mw.Synchronize(func() {
		app.mw.SetVisible(true)
		_ = app.mw.BringToTop()
		_ = app.mw.Activate()
		if app.browser != nil {
			app.browser.Focus()
		}
	})
}

func (app *launcherApp) showPairing() {
	app.showStatusCenter()
	if app.browser != nil {
		app.browser.Eval(`window.xynigoDesktop&&window.xynigoDesktop.focusPairing()`)
	}
}

func (app *launcherApp) openCloudWorkspace() {
	_ = exec.Command("rundll32.exe", "url.dll,FileProtocolHandler", cloudWorkspaceURL).Start()
}

func (app *launcherApp) openLocalSettings() {
	app.mu.Lock()
	statusURL := app.statusURL
	app.mu.Unlock()
	if statusPort(statusURL) == 0 {
		walk.MsgBox(
			app.mw,
			"本机设置暂不可用",
			"本地执行器尚未就绪，请稍后重试或先点击“重新启动执行器”。",
			walk.MsgBoxIconWarning,
		)
		return
	}
	app.showStatusCenter()
	if app.browser != nil {
		app.browser.Eval(`window.xynigoDesktop&&window.xynigoDesktop.navigate("settings")`)
	}
}

func (app *launcherApp) openLegacySettings() {
	app.mu.Lock()
	statusURL := app.statusURL
	app.mu.Unlock()
	settingsURL, err := localSettingsURL(statusURL)
	if err != nil {
		return
	}
	_ = exec.Command("rundll32.exe", "url.dll,FileProtocolHandler", settingsURL).Start()
}

func localSettingsURL(statusURL string) (string, error) {
	parsed, err := url.Parse(strings.TrimSpace(statusURL))
	if err != nil || parsed.Scheme != "http" || parsed.Port() == "" {
		return "", errors.New("本机服务地址无效")
	}
	host := strings.ToLower(parsed.Hostname())
	if host != "127.0.0.1" && host != "localhost" && host != "::1" {
		return "", errors.New("本机服务地址必须使用回环接口")
	}
	parsed.Path = "/"
	parsed.RawPath = ""
	parsed.RawQuery = "view=localsettings"
	parsed.Fragment = ""
	return parsed.String(), nil
}

func (app *launcherApp) navigateDesktop() {
	app.mu.Lock()
	statusURL := app.statusURL
	current := app.desktopURL
	app.mu.Unlock()
	parsed, err := url.Parse(strings.TrimSpace(statusURL))
	if err != nil || parsed.Scheme != "http" ||
		strings.ToLower(parsed.Hostname()) != "127.0.0.1" ||
		parsed.Port() == "" {
		return
	}
	parsed.Path = "/desktop/"
	parsed.RawPath = ""
	parsed.RawQuery = "platform=windows"
	parsed.Fragment = ""
	target := parsed.String()
	if target == current || app.browser == nil {
		return
	}
	app.browser.Navigate(target)
	app.mu.Lock()
	app.desktopURL = target
	app.mu.Unlock()
}

func (app *launcherApp) handleWebMessage(raw string) {
	var message map[string]any
	if len(raw) > 8192 || json.Unmarshal([]byte(raw), &message) != nil {
		return
	}
	action, _ := message["action"].(string)
	payload, _ := message["payload"].(map[string]any)
	switch action {
	case "open-external":
		rawURL, _ := payload["url"].(string)
		target, err := url.Parse(strings.TrimSpace(rawURL))
		if err != nil || target.Scheme != "https" || target.Hostname() == "" ||
			len(rawURL) > 4096 || strings.ContainsAny(rawURL, "\r\n\x00") {
			return
		}
		_ = exec.Command("rundll32.exe", "url.dll,FileProtocolHandler", rawURL).Start()
	case "open-logs":
		app.openLogs()
	case "restart-executor":
		go app.restartExecutor()
	case "check-update":
		app.handleUpdateAction()
	case "pair-device":
		code, _ := payload["code"].(string)
		app.startPair(code)
	case "run-diagnostics":
		go func() {
			app.refreshStatus()
			app.notifyWeb("已刷新本机连接、任务与更新状态")
		}()
	case "export-diagnostics":
		go app.exportDiagnosticSummary()
	case "backup-config":
		go app.backupCurrentConfig()
	case "open-legacy-settings":
		app.openLegacySettings()
	}
}

func (app *launcherApp) notifyWeb(message string) {
	if app.browser == nil {
		return
	}
	encoded, _ := json.Marshal(message)
	app.mw.Synchronize(func() {
		app.browser.Eval(
			"window.xynigoDesktop&&window.xynigoDesktop.notify(" +
				string(encoded) + ")")
	})
}

func (app *launcherApp) publishUpdateState(state string, message string, percent int) {
	if app.browser == nil {
		return
	}
	payload := map[string]any{
		"state":   state,
		"stage":   state,
		"message": message,
	}
	if percent >= 0 {
		payload["downloadPercent"] = percent
	}
	encoded, _ := json.Marshal(payload)
	app.mw.Synchronize(func() {
		app.browser.Eval(
			"window.xynigoDesktop&&window.xynigoDesktop.setUpdateStatus(" +
				string(encoded) + ")")
	})
}

func (app *launcherApp) openLogs() {
	directory := filepath.Join(app.root, "日志")
	if _, err := os.Stat(directory); errors.Is(err, os.ErrNotExist) {
		directory = filepath.Join(app.root, "查询日志")
	}
	_ = os.MkdirAll(directory, 0o700)
	_ = exec.Command("explorer.exe", directory).Start()
}

func desktopTimestamp() string {
	return time.Now().Format("20060102-150405")
}

func copyFileExclusive(source, target string) error {
	input, err := os.Open(source)
	if err != nil {
		return err
	}
	defer input.Close()
	output, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	_, copyErr := io.Copy(output, input)
	closeErr := output.Close()
	if copyErr != nil {
		_ = os.Remove(target)
		return copyErr
	}
	return closeErr
}

func (app *launcherApp) backupCurrentConfig() {
	source := filepath.Join(app.root, "config.json")
	if _, err := os.Stat(source); err != nil {
		app.mw.Synchronize(func() {
			walk.MsgBox(app.mw, "暂无配置可备份", "本机尚未生成 config.json。", walk.MsgBoxIconInformation)
		})
		return
	}
	directory := filepath.Join(app.root, "历史备份")
	if err := os.MkdirAll(directory, 0o700); err != nil {
		app.showDiagnosticWriteError("配置备份失败")
		return
	}
	target := filepath.Join(directory, "config-"+desktopTimestamp()+".json")
	if err := copyFileExclusive(source, target); err != nil {
		app.showDiagnosticWriteError("配置备份失败")
		return
	}
	_ = exec.Command("explorer.exe", "/select,", target).Start()
	app.notifyWeb("当前配置已备份到本机历史备份目录")
}

func (app *launcherApp) exportDiagnosticSummary() {
	app.mu.Lock()
	status := app.lastStatus
	runtime := ""
	if status != nil {
		runtime = status.Version
	}
	app.mu.Unlock()
	directory := filepath.Join(app.root, "日志")
	if err := os.MkdirAll(directory, 0o700); err != nil {
		app.showDiagnosticWriteError("诊断包导出失败")
		return
	}
	cloud, hub, paired, tasks := "未连接", "未连接", "未完成", 0
	if status != nil {
		cloud = cloudStatusText(status.CloudChannel.Status)
		if status.HubStudio.Connected {
			hub = "已连接"
		}
		if status.Executor.Paired {
			paired = "已完成"
		}
		tasks = status.Tasks.ActiveCount
	}
	lines := strings.Join([]string{
		"Xynigo 脱敏诊断摘要",
		"生成时间：" + time.Now().UTC().Format(time.RFC3339),
		"运行时：" + runtime,
		"云端通道：" + cloud,
		"HubStudio：" + hub,
		fmt.Sprintf("活动任务数：%d", tasks),
		"设备配对：" + paired,
		"说明：本文件不包含凭证、飞书链接、业务明文或设备令牌。",
	}, "\r\n") + "\r\n"
	target := filepath.Join(directory, "Xynigo-脱敏诊断-"+desktopTimestamp()+".txt")
	if err := os.WriteFile(target, []byte(lines), 0o600); err != nil {
		app.showDiagnosticWriteError("诊断包导出失败")
		return
	}
	_ = exec.Command("explorer.exe", "/select,", target).Start()
	app.notifyWeb("脱敏诊断摘要已生成，不包含凭证和业务明文")
}

func (app *launcherApp) showDiagnosticWriteError(title string) {
	app.mw.Synchronize(func() {
		walk.MsgBox(app.mw, title, "无法写入本机目标目录。", walk.MsgBoxIconError)
	})
}

func (app *launcherApp) handleUpdateAction() {
	app.mu.Lock()
	if app.updateInFlight || app.exiting {
		app.mu.Unlock()
		return
	}
	status := app.lastStatus
	if status == nil {
		app.mu.Unlock()
		walk.MsgBox(app.mw, "无法检查更新", "请先启动本地执行器。", walk.MsgBoxIconWarning)
		return
	}
	update := status.Update
	app.mu.Unlock()

	if !update.Enabled || update.InstallMode != "standard" {
		walk.MsgBox(
			app.mw,
			"在线更新不可用",
			"桌面在线更新仅支持标准安装版，请从云端工作台下载安装一次标准版。",
			walk.MsgBoxIconWarning,
		)
		return
	}
	action := "check"
	busyText := "正在检查…"
	if update.State == "available" {
		if status.Tasks.ActiveCount > 0 {
			walk.MsgBox(
				app.mw,
				"任务执行中",
				"请等待当前本机任务完成后再更新，状态中心不会中断正在执行的采购任务。",
				walk.MsgBoxIconWarning,
			)
			return
		}
		latest := strings.TrimSpace(update.LatestVersion)
		if latest == "" {
			latest = "最新版本"
		} else {
			latest = "v" + latest
		}
		answer := walk.MsgBox(
			app.mw,
			"确认更新 Xynigo",
			"将在线下载并校验 "+latest+"，随后自动重启本地执行器。\n\n更新前已确认当前没有正在执行的本机任务。",
			walk.MsgBoxYesNo|walk.MsgBoxIconQuestion,
		)
		if answer != walk.DlgCmdYes {
			return
		}
		action = "install"
		busyText = "准备更新…"
	}

	app.mu.Lock()
	if app.updateInFlight {
		app.mu.Unlock()
		return
	}
	app.updateInFlight = true
	app.mu.Unlock()
	app.updateButton.SetText(busyText)
	app.updateButton.SetEnabled(false)
	_ = app.trayUpdate.SetEnabled(false)
	if action == "install" {
		go app.publishUpdateState("downloading", "更新请求已确认，正在连接下载服务器…", 0)
	} else {
		go app.publishUpdateState("checking", "正在检查云端发布清单…", -1)
	}
	controlPath := updateCheckPath
	if action == "install" {
		controlPath = updateInstallPath
	}
	go func() {
		err := app.postLauncherControl(controlPath)
		app.mu.Lock()
		app.updateInFlight = false
		app.mu.Unlock()
		if err != nil {
			appendStatusCenterLog(app.root, "update_"+action+"_failed: "+err.Error())
			app.publishUpdateState("error", err.Error(), -1)
			app.mw.Synchronize(func() {
				walk.MsgBox(app.mw, "在线更新失败", err.Error(), walk.MsgBoxIconError)
			})
		} else {
			appendStatusCenterLog(app.root, "update_"+action+"_accepted")
			app.notifyWeb("更新请求已接受，页面将实时显示处理进度")
		}
		app.refreshStatus()
	}()
}

func (app *launcherApp) postLauncherControl(path string) error {
	app.mu.Lock()
	statusURL := app.statusURL
	token := app.launcherToken
	app.mu.Unlock()
	if statusURL == "" || statusPort(statusURL) == 0 {
		return errors.New("本地执行器尚未就绪，请稍后重试")
	}
	controlURL := strings.Replace(statusURL, "/executor-status.json", path, 1)
	request, err := http.NewRequest(http.MethodPost, controlURL, bytes.NewReader(nil))
	if err != nil {
		return errors.New("无法创建本机更新请求")
	}
	request.Header.Set("X-Xynigo-Launcher", token)
	client := &http.Client{Timeout: 3 * time.Second}
	response, err := client.Do(request)
	if err != nil {
		return errors.New("本地执行器未响应，请确认状态中心保持运行")
	}
	defer response.Body.Close()
	data, readErr := io.ReadAll(io.LimitReader(response.Body, 128*1024))
	if readErr != nil {
		return errors.New("无法读取本机更新响应")
	}
	if response.StatusCode == http.StatusOK || response.StatusCode == http.StatusAccepted {
		return nil
	}
	var payload struct {
		Error string `json:"error"`
	}
	if json.Unmarshal(data, &payload) == nil && strings.TrimSpace(payload.Error) != "" {
		return errors.New(strings.TrimSpace(payload.Error))
	}
	return fmt.Errorf("本机更新请求失败（HTTP %d）", response.StatusCode)
}

func (app *launcherApp) statusLoop() {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		app.refreshStatus()
		app.mu.Lock()
		exiting := app.exiting
		app.mu.Unlock()
		if exiting {
			return
		}
		<-ticker.C
	}
}

func (app *launcherApp) refreshStatus() {
	status, err := app.fetchStatus()
	if err != nil {
		app.mu.Lock()
		app.statusFailures++
		if app.statusFailures < 3 && app.lastStatus != nil && time.Since(app.lastStatusAt) < 15*time.Second {
			status = app.lastStatus
			err = nil
		}
		app.mu.Unlock()
	}
	app.mw.Synchronize(func() { app.renderStatus(status, err) })
}

func (app *launcherApp) fetchStatus() (*localStatus, error) {
	start := configuredServerPort(app.root)
	client := &http.Client{Timeout: 2 * time.Second}
	for port := start; port < start+10; port++ {
		url := fmt.Sprintf("http://127.0.0.1:%d/executor-status.json", port)
		response, err := client.Get(url)
		if err != nil {
			continue
		}
		data, readErr := io.ReadAll(io.LimitReader(response.Body, 128*1024))
		response.Body.Close()
		if readErr != nil || response.StatusCode != http.StatusOK {
			continue
		}
		var status localStatus
		if json.Unmarshal(data, &status) != nil || status.SchemaVersion != 1 || status.Version == "" {
			continue
		}
		app.mu.Lock()
		app.statusURL = url
		app.lastStatus = &status
		app.lastStatusAt = time.Now()
		app.statusFailures = 0
		app.mu.Unlock()
		return &status, nil
	}
	return nil, errors.New("executor_offline")
}

func configuredServerPort(root string) int {
	data, err := os.ReadFile(filepath.Join(root, "config.json"))
	if err == nil {
		var payload struct {
			ServerPort int `json:"serverPort"`
		}
		if json.Unmarshal(data, &payload) == nil && payload.ServerPort >= 1 && payload.ServerPort <= 65526 {
			return payload.ServerPort
		}
	}
	return 8765
}

func (app *launcherApp) renderStatus(status *localStatus, err error) {
	green := walk.RGB(20, 132, 93)
	amber := walk.RGB(162, 107, 11)
	red := walk.RGB(178, 74, 59)
	blue := walk.RGB(57, 119, 155)
	muted := walk.RGB(109, 131, 150)

	if err != nil || status == nil {
		app.statusTitle.SetText("本地执行器未运行")
		app.statusDetail.SetText("可点击“启动执行器”恢复本机服务；云端工作台仍可独立使用。")
		app.cloudValue.SetText("未连接")
		app.cloudNote.SetText("等待本机服务")
		app.hubValue.SetText("等待执行器")
		app.hubNote.SetText("尚未检查 Local API")
		app.taskValue.SetText("—")
		app.taskNote.SetText("执行器启动后显示")
		app.versionValue.SetText("—")
		app.versionNote.SetText("执行器启动后可检查")
		app.deviceValue.SetText("设备状态将在执行器启动后显示")
		app.deviceState.SetText("等待执行器")
		app.heartbeatValue.SetText("无本机心跳")
		app.statusSignal.SetTextColor(red)
		app.cloudSignal.SetTextColor(red)
		app.hubSignal.SetTextColor(muted)
		app.taskSignal.SetTextColor(muted)
		app.setPairingVisible(false)
		app.startButton.SetText("启动执行器")
		app.updateButton.SetText("检查更新")
		app.updateButton.SetEnabled(false)
		_ = app.trayStatus.SetText("● 本地执行器未运行")
		_ = app.trayCloud.SetText("云端：未连接")
		_ = app.trayHub.SetText("HubStudio：等待执行器")
		_ = app.trayStartStop.SetText("启动执行器")
		_ = app.trayUpdate.SetText("检查更新")
		_ = app.trayUpdate.SetEnabled(false)
		_ = app.notify.SetToolTip("Xynigo 本地执行器 · 未运行")
		return
	}
	app.navigateDesktop()
	app.startButton.SetText("重新启动执行器")
	_ = app.trayStartStop.SetText("重新启动执行器")
	app.versionValue.SetText("v" + status.Version)
	app.renderUpdateStatus(status)
	if status.HubStudio.Connected {
		app.hubValue.SetText("已连接")
		app.hubNote.SetText("Local API 可用")
		app.hubSignal.SetTextColor(green)
	} else {
		app.hubValue.SetText("未连接")
		app.hubNote.SetText("请确认 HubStudio 已启动")
		app.hubSignal.SetTextColor(red)
	}
	if status.Tasks.ActiveCount == 0 {
		app.taskValue.SetText("当前空闲")
		app.taskNote.SetText("没有运行中的任务")
		app.taskSignal.SetTextColor(green)
	} else {
		app.taskValue.SetText(fmt.Sprintf("%d 个运行中", status.Tasks.ActiveCount))
		app.taskNote.SetText(activeTaskNote(status))
		app.taskSignal.SetTextColor(blue)
	}
	unpaired := !status.Executor.Paired || status.CloudChannel.Status == "not_paired"
	if !unpaired {
		name := strings.TrimSpace(status.Executor.DisplayName)
		if name == "" {
			name = "这台采购电脑"
		}
		app.deviceValue.SetText(name + " · 已完成设备配对")
		app.deviceState.SetText("已配对")
		app.deviceState.SetTextColor(green)
		app.pairEdit.SetText("")
		app.setPairingVisible(false)
	} else {
		app.deviceValue.SetText("尚未配对 · 配对码 5 分钟内有效且只能使用一次")
		app.deviceState.SetText("待配对")
		app.deviceState.SetTextColor(amber)
		app.setPairingVisible(true)
	}
	if status.CloudChannel.Status == "online" {
		app.heartbeatValue.SetText(relativeTime(status.CloudChannel.LastPollAt))
	} else {
		app.heartbeatValue.SetText(lastOnlineText(status.CloudChannel.LastPollAt))
	}
	cloudText := cloudStatusText(status.CloudChannel.Status)
	app.cloudValue.SetText(cloudText)
	if unpaired {
		app.cloudNote.SetText("需要一次性配对码")
		app.cloudSignal.SetTextColor(amber)
		app.statusSignal.SetTextColor(amber)
		app.statusTitle.SetText("执行器已启动，等待设备配对")
		app.statusDetail.SetText("在云端工作台生成一次性配对码，然后在下方完成绑定。")
		_ = app.trayStatus.SetText("● 等待设备配对")
	} else if status.CloudChannel.Status == "online" && status.Tasks.ActiveCount > 0 {
		app.cloudNote.SetText("安全通道已建立")
		app.cloudSignal.SetTextColor(green)
		app.statusSignal.SetTextColor(blue)
		app.statusTitle.SetText("本地执行器正在执行任务")
		app.statusDetail.SetText("云端通道与 HubStudio 正常，当前任务将在本机安全执行。")
		_ = app.trayStatus.SetText(fmt.Sprintf("● 正在执行 %d 个任务", status.Tasks.ActiveCount))
	} else if status.CloudChannel.Status == "online" {
		app.cloudNote.SetText("安全通道已建立")
		app.cloudSignal.SetTextColor(green)
		app.statusSignal.SetTextColor(green)
		app.statusTitle.SetText("本地执行器在线")
		if status.HubStudio.Connected {
			app.statusDetail.SetText("云端通道和 HubStudio 均已连接，可以接收本机任务。")
		} else {
			app.statusDetail.SetText("云端通道正常；HubStudio 尚未连接，请先启动并登录 HubStudio。")
		}
		_ = app.trayStatus.SetText("● 本地执行器在线")
	} else if status.CloudChannel.Status == "connecting" || status.CloudChannel.Status == "reconnecting" || status.CloudChannel.Status == "paired" {
		app.cloudNote.SetText(connectionPhaseText(
			status.CloudChannel.Phase,
			status.CloudChannel.Attempt,
			status.CloudChannel.NextRetryAt))
		app.cloudSignal.SetTextColor(amber)
		app.statusSignal.SetTextColor(amber)
		if status.CloudChannel.Status == "reconnecting" {
			app.statusTitle.SetText("正在恢复云端连接")
		} else {
			app.statusTitle.SetText("正在连接云端工作台")
		}
		app.statusDetail.SetText(connectionPhaseDetail(
			status.CloudChannel.Phase,
			status.CloudChannel.NextRetryAt))
		_ = app.trayStatus.SetText("● 正在连接云端")
	} else {
		app.cloudNote.SetText(connectionPhaseText(
			status.CloudChannel.Phase,
			status.CloudChannel.Attempt,
			status.CloudChannel.NextRetryAt))
		app.cloudSignal.SetTextColor(red)
		app.statusSignal.SetTextColor(red)
		app.statusTitle.SetText("云端连接已中断")
		app.statusDetail.SetText(connectionPhaseDetail(
			status.CloudChannel.Phase,
			status.CloudChannel.NextRetryAt))
		_ = app.trayStatus.SetText("● 本地执行器离线")
	}
	_ = app.trayCloud.SetText("云端：" + strings.TrimPrefix(cloudText, "云端"))
	if status.HubStudio.Connected {
		_ = app.trayHub.SetText("HubStudio：已连接")
	} else {
		_ = app.trayHub.SetText("HubStudio：未连接")
	}
	_ = app.notify.SetToolTip("Xynigo 本地执行器 · " + cloudText)
}

func (app *launcherApp) renderUpdateStatus(status *localStatus) {
	update := status.Update
	message := strings.TrimSpace(update.Message)
	if message == "" {
		message = "等待检查更新"
	}
	app.versionNote.SetText(message)

	buttonText := "检查更新"
	trayText := "检查更新"
	enabled := update.Enabled && update.InstallMode == "standard"
	switch update.State {
	case "checking":
		buttonText = "正在检查…"
		trayText = "正在检查更新…"
		enabled = false
	case "available":
		latest := strings.TrimSpace(update.LatestVersion)
		if update.CurrentVersion != "" && update.CurrentVersion == latest &&
			update.CurrentRuntimeID != "" && update.LatestRuntimeID != "" &&
			update.CurrentRuntimeID != update.LatestRuntimeID {
			buttonText = "安装最新构建"
			trayText = "安装最新构建"
		} else if latest == "" {
			buttonText = "立即更新"
			trayText = "立即更新"
		} else {
			buttonText = "更新到 v" + latest
			trayText = "更新到 v" + latest
		}
		if status.Tasks.ActiveCount > 0 {
			buttonText = "任务结束后更新"
			trayText = "任务结束后可更新"
			enabled = false
		}
	case "downloading":
		if update.DownloadPercent > 0 {
			buttonText = fmt.Sprintf("下载 %d%%", update.DownloadPercent)
			trayText = fmt.Sprintf("正在下载更新 %d%%", update.DownloadPercent)
		} else {
			buttonText = "正在下载…"
			trayText = "正在下载更新…"
		}
		enabled = false
	case "verifying":
		buttonText = "正在校验…"
		trayText = "正在校验更新包…"
		enabled = false
	case "extracting":
		buttonText = "正在解压…"
		trayText = "正在解压更新包…"
		enabled = false
	case "installing":
		buttonText = "正在安装…"
		trayText = "正在启动安装…"
		enabled = false
	case "restarting":
		buttonText = "正在重启…"
		trayText = "正在安装更新…"
		enabled = false
	case "prompting":
		buttonText = "等待确认…"
		enabled = false
	case "disabled":
		buttonText = "暂不支持在线更新"
		enabled = false
	}
	if update.InstallMode != "standard" {
		buttonText = "请安装标准版"
		trayText = "在线更新需要标准版"
		enabled = false
	}
	app.mu.Lock()
	inFlight := app.updateInFlight
	app.mu.Unlock()
	if inFlight {
		enabled = false
	}
	app.updateButton.SetText(buttonText)
	app.updateButton.SetEnabled(enabled)
	progressVisible := update.State == "downloading" || update.State == "verifying" ||
		update.State == "extracting" || update.State == "installing" || update.State == "restarting"
	if app.updateProgress != nil {
		if update.State == "downloading" && update.DownloadPercent > 0 {
			_ = app.updateProgress.SetMarqueeMode(false)
			app.updateProgress.SetValue(update.DownloadPercent)
		} else if progressVisible {
			app.updateProgress.SetValue(0)
			_ = app.updateProgress.SetMarqueeMode(true)
		} else {
			_ = app.updateProgress.SetMarqueeMode(false)
			app.updateProgress.SetValue(0)
		}
		app.updateProgress.SetVisible(progressVisible)
	}
	_ = app.trayUpdate.SetText(trayText)
	_ = app.trayUpdate.SetEnabled(enabled)
}

func (app *launcherApp) setPairingVisible(visible bool) {
	if app.trayPair != nil {
		_ = app.trayPair.SetVisible(visible)
	}
}

func activeTaskNote(status *localStatus) string {
	if len(status.Tasks.Items) == 0 {
		return "任务正在本机安全执行"
	}
	item := status.Tasks.Items[0]
	label := strings.TrimSpace(item.Label)
	if label == "" {
		label = "任务正在本机安全执行"
	}
	if item.ElapsedSec <= 0 {
		return label
	}
	minutes := item.ElapsedSec / 60
	seconds := item.ElapsedSec % 60
	return fmt.Sprintf("%s · %02d:%02d", label, minutes, seconds)
}

func connectionPhaseText(phase string, attempt int, nextRetryAt string) string {
	if attempt < 1 {
		attempt = 1
	}
	switch phase {
	case "authorizing":
		return fmt.Sprintf("验证设备身份 · 第 %d 次", attempt)
	case "handshake":
		return fmt.Sprintf("建立安全通道 · 第 %d 次", attempt)
	case "retry_wait":
		if seconds, ok := retryCountdown(nextRetryAt); ok {
			return fmt.Sprintf("%d 秒后自动重试 · 已尝试 %d 次", seconds, attempt)
		}
		return fmt.Sprintf("准备自动重试 · 已尝试 %d 次", attempt)
	case "listening":
		return "安全通道已建立"
	default:
		return "正在准备连接"
	}
}

func connectionPhaseDetail(phase string, nextRetryAt string) string {
	switch phase {
	case "authorizing":
		return "连接进度 1/3：正在读取并验证这台设备的配对身份。"
	case "handshake":
		return "连接进度 2/3：正在快速握手；成功后会立即进入云端任务监听。"
	case "retry_wait":
		if seconds, ok := retryCountdown(nextRetryAt); ok {
			return fmt.Sprintf("本机服务和现有任务不受影响；将在 %d 秒后自动重试云端连接。", seconds)
		}
		return "本机服务和现有任务不受影响；正在准备自动重试云端连接。"
	default:
		return "本机服务保持运行；网络恢复后会自动重连，不会重复执行写任务。"
	}
}

func retryCountdown(value string) (int, bool) {
	parsed, err := time.Parse(time.RFC3339Nano, strings.TrimSpace(value))
	if err != nil {
		return 0, false
	}
	remaining := time.Until(parsed)
	if remaining <= 0 {
		return 0, true
	}
	seconds := int((remaining + time.Second - 1) / time.Second)
	return seconds, true
}

func cloudStatusText(status string) string {
	switch status {
	case "online":
		return "云端在线"
	case "connecting", "paired":
		return "正在连接"
	case "reconnecting":
		return "正在重连"
	case "not_paired":
		return "等待配对"
	case "revoked":
		return "设备已撤销"
	case "credential_error":
		return "设备凭证异常"
	case "offline", "error":
		return "云端离线"
	default:
		return "等待连接"
	}
}

func relativeTime(value string) string {
	parsed, err := time.Parse(time.RFC3339Nano, value)
	if err != nil {
		return "等待云端心跳"
	}
	delta := time.Since(parsed)
	if delta < 0 {
		delta = 0
	}
	if delta < 8*time.Second {
		return "心跳：刚刚"
	}
	if delta < time.Minute {
		return fmt.Sprintf("心跳：%d 秒前", int(delta.Seconds()))
	}
	return fmt.Sprintf("心跳：%d 分钟前", int(delta.Minutes()))
}

func lastOnlineText(value string) string {
	text := relativeTime(value)
	if text == "等待云端心跳" {
		return "等待首次握手"
	}
	return strings.Replace(text, "心跳：", "上次在线：", 1)
}

func (app *launcherApp) ensureExecutor() {
	if _, err := app.fetchStatus(); err == nil {
		app.mu.Lock()
		managedByCurrentLauncher := app.child != nil
		app.mu.Unlock()
		if managedByCurrentLauncher {
			appendStatusCenterLog(app.root, "status_detected_current_child")
			return
		}
		// A standard-package upgrade can replace the launcher while the old
		// Python child keeps running. Never adopt that process: it may advertise
		// an old capability set even though the UI itself is the new version.
		app.mu.Lock()
		port := statusPort(app.statusURL)
		app.mu.Unlock()
		appendStatusCenterLog(app.root, fmt.Sprintf("status_detected_orphan port=%d", port))
		if err := terminateStatusListener(port); err != nil {
			appendStatusCenterLog(app.root, "orphan_takeover_failed: "+err.Error())
			app.showExecutorStartFailure()
			return
		}
		appendStatusCenterLog(app.root, "orphan_takeover_succeeded")
		time.Sleep(800 * time.Millisecond)
	} else {
		appendStatusCenterLog(app.root, "status_unavailable_start_current")
	}
	if err := app.startExecutor(); err != nil {
		appendStatusCenterLog(app.root, "executor_start_failed: "+err.Error())
		app.showExecutorStartFailure()
	} else {
		appendStatusCenterLog(app.root, "executor_start_succeeded")
	}
}

func appendStatusCenterLog(root string, message string) {
	directory := filepath.Join(root, "日志")
	if os.MkdirAll(directory, 0o700) != nil {
		return
	}
	file, err := os.OpenFile(
		filepath.Join(directory, "状态中心.log"),
		os.O_CREATE|os.O_WRONLY|os.O_APPEND,
		0o600,
	)
	if err != nil {
		return
	}
	defer file.Close()
	_, _ = fmt.Fprintf(file, "%s %s\r\n", time.Now().UTC().Format(time.RFC3339), message)
}

func (app *launcherApp) showExecutorStartFailure() {
	app.mw.Synchronize(func() {
		app.statusTitle.SetText("本地执行器启动失败")
		app.statusDetail.SetText("请重新安装标准版，或联系管理员检查运行时完整性。")
	})
}

func statusPort(rawURL string) int {
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Scheme != "http" || (parsed.Hostname() != "127.0.0.1" && parsed.Hostname() != "localhost") {
		return 0
	}
	port, err := strconv.Atoi(parsed.Port())
	if err != nil || port < 1 || port > 65535 {
		return 0
	}
	return port
}

func tcpListenerPIDs(port int) ([]int, error) {
	if port < 1 || port > 65535 {
		return nil, errors.New("执行器监听端口无效")
	}
	var size uint32
	statusCode, _, _ := getTCPTable.Call(
		0,
		uintptr(unsafe.Pointer(&size)),
		0,
		afINET,
		tcpTableOwnerPIDListener,
		0,
	)
	if statusCode != errorInsufficientBuffer || size < 4 {
		return nil, fmt.Errorf("无法读取本机 TCP 监听表：%d", statusCode)
	}
	buffer := make([]byte, size)
	statusCode, _, _ = getTCPTable.Call(
		uintptr(unsafe.Pointer(&buffer[0])),
		uintptr(unsafe.Pointer(&size)),
		0,
		afINET,
		tcpTableOwnerPIDListener,
		0,
	)
	if statusCode != 0 {
		return nil, fmt.Errorf("无法读取本机 TCP 监听表：%d", statusCode)
	}
	count := *(*uint32)(unsafe.Pointer(&buffer[0]))
	rowSize := uintptr(unsafe.Sizeof(mibTCPRowOwnerPID{}))
	available := (uintptr(len(buffer)) - 4) / rowSize
	if uintptr(count) > available {
		return nil, errors.New("本机 TCP 监听表结构无效")
	}
	seen := make(map[int]bool)
	result := make([]int, 0, 1)
	for index := uintptr(0); index < uintptr(count); index++ {
		offset := uintptr(4) + index*rowSize
		row := (*mibTCPRowOwnerPID)(unsafe.Pointer(&buffer[offset]))
		rowPort := int(bits.ReverseBytes16(uint16(row.LocalPort)))
		if row.State != mibTCPStateListen || rowPort != port || row.OwningPID == 0 {
			continue
		}
		pid := int(row.OwningPID)
		if seen[pid] {
			continue
		}
		seen[pid] = true
		result = append(result, pid)
	}
	return result, nil
}

func terminateStatusListener(port int) error {
	pids, err := tcpListenerPIDs(port)
	if err != nil {
		return errors.New("无法定位旧版本本地执行器")
	}
	if len(pids) == 0 {
		return errors.New("旧版本本地执行器监听进程不存在")
	}
	for _, pid := range pids {
		process, findErr := os.FindProcess(pid)
		if findErr != nil || process.Kill() != nil {
			return errors.New("无法结束旧版本本地执行器")
		}
	}
	return nil
}

func (app *launcherApp) startExecutor() error {
	app.mu.Lock()
	defer app.mu.Unlock()
	if app.exiting {
		return errors.New("launcher_exiting")
	}
	if app.child != nil {
		return nil
	}
	runtimeRoot, installMode, err := resolveRuntime(app.root)
	if err != nil {
		return err
	}
	pythonw := filepath.Join(runtimeRoot, "python-embed", "pythonw.exe")
	runPy := filepath.Join(runtimeRoot, "run.py")
	if _, err := os.Stat(pythonw); err != nil {
		return fmt.Errorf("运行时缺少 pythonw.exe")
	}
	logs := filepath.Join(app.root, "日志")
	if err := os.MkdirAll(logs, 0o700); err != nil {
		return err
	}
	logFile, err := os.OpenFile(filepath.Join(logs, "本地执行器.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	cmd := exec.Command(pythonw, runPy, "--no-browser")
	cmd.Dir = app.root
	runtimeID := ""
	if installMode == "standard" {
		runtimeID = filepath.Base(runtimeRoot)
	}
	cmd.Env = append(os.Environ(),
		"XYNIGO_DATA_DIR="+app.root,
		"XYNIGO_INSTALL_DIR="+app.root,
		"XYNIGO_INSTALL_MODE="+installMode,
		"XYNIGO_RUNTIME_ID="+runtimeID,
		"XYNIGO_LAUNCHER_TOKEN="+app.launcherToken,
		"PYTHONUTF8=1",
		"PYTHONIOENCODING=utf-8",
	)
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: createNoWindow}
	if err := cmd.Start(); err != nil {
		logFile.Close()
		return err
	}
	app.child = cmd
	app.childDone = make(chan error, 1)
	done := app.childDone
	go func() {
		done <- cmd.Wait()
		logFile.Close()
		app.mu.Lock()
		if app.child == cmd {
			app.child = nil
		}
		app.mu.Unlock()
	}()
	return nil
}

func readCurrentVersion(root string) (string, error) {
	data, err := os.ReadFile(filepath.Join(root, "current-version.txt"))
	if err != nil {
		return "", errors.New("安装信息损坏：缺少当前版本")
	}
	version := strings.TrimSpace(string(data))
	if version == "" || strings.ContainsAny(version, `/\\`) {
		return "", errors.New("安装信息损坏：当前版本无效")
	}
	return version, nil
}

func resolveRuntime(root string) (string, string, error) {
	if version, err := readCurrentVersion(root); err == nil {
		runtimeRoot := filepath.Join(root, "versions", version)
		if _, statErr := os.Stat(filepath.Join(runtimeRoot, "run.py")); statErr == nil {
			return runtimeRoot, "standard", nil
		}
	}
	greenRunPy := filepath.Join(root, "run.py")
	greenPython := filepath.Join(root, "python-embed", "python.exe")
	if _, err := os.Stat(greenRunPy); err == nil {
		if _, pythonErr := os.Stat(greenPython); pythonErr == nil {
			return root, "green", nil
		}
	}
	return "", "", errors.New("运行时不完整：缺少标准版版本目录或绿色版 run.py")
}

func (app *launcherApp) restartExecutor() {
	app.stopExecutor()
	time.Sleep(600 * time.Millisecond)
	if err := app.startExecutor(); err != nil {
		app.mw.Synchronize(func() {
			walk.MsgBox(app.mw, "Xynigo 启动失败", err.Error(), walk.MsgBoxIconError)
		})
		return
	}
	app.mw.Synchronize(func() {
		_ = app.notify.ShowInfo("Xynigo", "本地执行器已重新启动。")
	})
}

func (app *launcherApp) stopExecutor() {
	app.mu.Lock()
	cmd := app.child
	done := app.childDone
	statusURL := app.statusURL
	token := app.launcherToken
	app.mu.Unlock()
	if cmd == nil {
		if port := statusPort(statusURL); port > 0 {
			_ = terminateStatusListener(port)
		}
		return
	}
	if statusURL != "" {
		controlURL := strings.Replace(statusURL, "/executor-status.json", "/executor-control/shutdown", 1)
		request, _ := http.NewRequest(http.MethodPost, controlURL, bytes.NewReader(nil))
		request.Header.Set("X-Xynigo-Launcher", token)
		client := &http.Client{Timeout: 900 * time.Millisecond}
		if response, err := client.Do(request); err == nil {
			io.Copy(io.Discard, response.Body)
			response.Body.Close()
		}
	}
	if done != nil {
		select {
		case <-done:
			return
		case <-time.After(5 * time.Second):
		}
	}
	_ = cmd.Process.Kill()
}

func (app *launcherApp) performPair(raw string) {
	code := strings.ToUpper(strings.TrimSpace(raw))
	if !pairCodePattern.MatchString(code) {
		app.mw.Synchronize(func() {
			walk.MsgBox(app.mw, "配对码无效", "请输入云端显示的 8 位一次性配对码。", walk.MsgBoxIconWarning)
		})
		return
	}
	code = strings.ReplaceAll(code, "-", "")
	code = code[:4] + "-" + code[4:]
	runtimeRoot, installMode, err := resolveRuntime(app.root)
	if err != nil {
		app.showPairResult(err)
		return
	}
	python := filepath.Join(runtimeRoot, "python-embed", "python.exe")
	runPy := filepath.Join(runtimeRoot, "run.py")
	cmd := exec.Command(python, runPy, "pair", code)
	cmd.Dir = app.root
	cmd.Env = append(os.Environ(),
		"PYTHONUTF8=1",
		"PYTHONIOENCODING=utf-8",
		"XYNIGO_DATA_DIR="+app.root,
		"XYNIGO_INSTALL_DIR="+app.root,
		"XYNIGO_INSTALL_MODE="+installMode,
	)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: createNoWindow}
	output, err := cmd.CombinedOutput()
	if err != nil {
		message := strings.TrimSpace(string(output))
		if message == "" {
			message = "配对失败，请确认配对码尚未过期。"
		}
		app.showPairResult(errors.New(message))
		return
	}
	app.mw.Synchronize(func() { app.pairEdit.SetText("") })
	app.restartExecutor()
	app.mw.Synchronize(func() {
		walk.MsgBox(app.mw, "设备配对完成", "这台电脑已绑定云端，正在等待真实心跳确认在线。", walk.MsgBoxIconInformation)
	})
}

func (app *launcherApp) startPair(raw string) {
	app.mu.Lock()
	if app.pairInFlight || app.exiting {
		app.mu.Unlock()
		return
	}
	app.pairInFlight = true
	app.mu.Unlock()
	app.pairEdit.SetEnabled(false)
	app.pairButton.SetEnabled(false)
	app.pairButton.SetText("正在配对…")
	go func() {
		defer app.finishPair()
		app.performPair(raw)
	}()
}

func (app *launcherApp) finishPair() {
	app.mu.Lock()
	app.pairInFlight = false
	app.mu.Unlock()
	app.mw.Synchronize(func() {
		app.pairEdit.SetEnabled(true)
		app.pairButton.SetEnabled(true)
		app.pairButton.SetText("配对这台电脑")
	})
}

func (app *launcherApp) showPairResult(err error) {
	app.mw.Synchronize(func() {
		walk.MsgBox(app.mw, "设备配对失败", err.Error(), walk.MsgBoxIconError)
	})
}

func (app *launcherApp) commandLoop() {
	ticker := time.NewTicker(800 * time.Millisecond)
	defer ticker.Stop()
	for range ticker.C {
		app.mu.Lock()
		exiting := app.exiting
		app.mu.Unlock()
		if exiting {
			return
		}
		path := commandPath(app.root)
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		_ = os.Remove(path)
		var envelope commandEnvelope
		if json.Unmarshal(data, &envelope) != nil {
			continue
		}
		created, err := time.Parse(time.RFC3339Nano, envelope.CreatedAt)
		if err != nil || time.Since(created) > 2*time.Minute {
			continue
		}
		go app.handleCommand(envelope.Command)
	}
}

func (app *launcherApp) handleCommand(command string) {
	if command == "show" || command == "background" || command == "" {
		app.showStatusCenter()
		go app.ensureExecutor()
		return
	}
	if strings.HasPrefix(command, "pair:") {
		app.showStatusCenter()
		app.performPair(strings.TrimPrefix(command, "pair:"))
		return
	}
	matches := protocolPattern.FindStringSubmatch(command)
	if matches == nil {
		return
	}
	app.showStatusCenter()
	if len(matches) > 1 && matches[1] != "" {
		app.performPair(matches[1])
		return
	}
	go app.ensureExecutor()
}

func (app *launcherApp) exitApplication() {
	app.mu.Lock()
	app.exiting = true
	app.mu.Unlock()
	app.stopExecutor()
	app.mw.Synchronize(func() {
		app.mw.SetVisible(true)
		app.mw.Close()
		walk.App().Exit(0)
	})
}
