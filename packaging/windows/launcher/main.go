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
	"net/http"
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

	"github.com/lxn/walk"
	. "github.com/lxn/walk/declarative"
)

const (
	cloudWorkspaceURL = "https://xynigo.samforo.icu"
	launcherMutexName = "Local\\XynigoSourcing.Launcher"
	createNoWindow    = 0x08000000
)

var (
	pairCodePattern = regexp.MustCompile(`(?i)^[A-HJ-NP-Z2-9]{4}-?[A-HJ-NP-Z2-9]{4}$`)
	protocolPattern = regexp.MustCompile(`(?i)^xynigo://(?:start/?|wake/?|pair\?code=([A-HJ-NP-Z2-9]{4}-?[A-HJ-NP-Z2-9]{4}))$`)
	kernel32        = syscall.NewLazyDLL("kernel32.dll")
	createMutexW    = kernel32.NewProc("CreateMutexW")
)

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
}

type commandEnvelope struct {
	Command   string `json:"command"`
	CreatedAt string `json:"createdAt"`
}

type launcherApp struct {
	root string

	mw             *walk.MainWindow
	notify         *walk.NotifyIcon
	statusTitle    *walk.Label
	statusDetail   *walk.Label
	cloudValue     *walk.Label
	hubValue       *walk.Label
	taskValue      *walk.Label
	versionValue   *walk.Label
	deviceValue    *walk.Label
	heartbeatValue *walk.Label
	pairEdit       *walk.LineEdit
	pairButton     *walk.PushButton
	startButton    *walk.PushButton
	trayStatus     *walk.Action
	trayStartStop  *walk.Action

	mu            sync.Mutex
	child         *exec.Cmd
	childDone     chan error
	launcherToken string
	statusURL     string
	lastStatus    *localStatus
	pairInFlight  bool
	exiting       bool
}

func main() {
	root, err := executableDirectory()
	if err != nil {
		walk.MsgBox(nil, "Xynigo 启动失败", err.Error(), walk.MsgBoxIconError)
		return
	}
	command := startupCommand(os.Args[1:])
	mutex, alreadyRunning, err := acquireLauncherMutex(launcherMutexName)
	if err != nil {
		walk.MsgBox(nil, "Xynigo 启动失败", "无法创建本地单实例锁。", walk.MsgBoxIconError)
		return
	}
	if alreadyRunning {
		if err := writeCommand(root, command); err != nil {
			walk.MsgBox(nil, "Xynigo", "状态中心已运行，但启动请求未能转交。", walk.MsgBoxIconWarning)
		}
		return
	}
	defer syscall.CloseHandle(mutex)

	app := &launcherApp{root: root, launcherToken: randomToken()}
	if err := app.buildWindow(); err != nil {
		walk.MsgBox(nil, "Xynigo 启动失败", err.Error(), walk.MsgBoxIconError)
		return
	}
	if err := app.buildTray(); err != nil {
		walk.MsgBox(app.mw, "Xynigo 启动失败", err.Error(), walk.MsgBoxIconError)
		return
	}
	defer app.notify.Dispose()

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
	white := walk.RGB(255, 255, 255)
	navy := walk.RGB(8, 37, 72)
	muted := walk.RGB(83, 113, 139)
	teal := walk.RGB(17, 127, 134)
	line := walk.RGB(218, 230, 236)
	soft := walk.RGB(232, 248, 247)
	canvas := walk.RGB(244, 247, 250)

	// Declarative Walk treats string images as resource names and resolves
	// them relative to the executable directory. Passing an absolute path here
	// makes its resource manager prepend the executable directory a second
	// time (for example, C:\\...\\Xynigo Sourcing\\C:\\...), so the status
	// center fails before the executor can start.
	logo := "xynigo-logo.png"
	icon := "xynigo-x.ico"
	window := MainWindow{
		AssignTo:   &app.mw,
		Title:      "Xynigo 本地执行器状态中心",
		Icon:       icon,
		Size:       Size{Width: 780, Height: 640},
		MinSize:    Size{Width: 720, Height: 590},
		Background: SolidColorBrush{Color: canvas},
		Font:       Font{Family: "Microsoft YaHei UI", PointSize: 9},
		Layout:     VBox{Margins: Margins{Left: 18, Top: 18, Right: 18, Bottom: 16}, Spacing: 12},
		Children: []Widget{
			Composite{
				Background: SolidColorBrush{Color: soft},
				Layout:     HBox{Margins: Margins{Left: 18, Top: 12, Right: 18, Bottom: 12}, Spacing: 18},
				Children: []Widget{
					ImageView{Image: logo, Mode: ImageViewModeShrink, MinSize: Size{Width: 210, Height: 82}, MaxSize: Size{Width: 210, Height: 82}},
					Composite{Background: SolidColorBrush{Color: soft}, Layout: VBox{MarginsZero: true, Spacing: 3}, Children: []Widget{
						Label{Text: "LOCAL EXECUTOR STATUS CENTER", TextColor: teal, Font: Font{Family: "Microsoft YaHei UI", PointSize: 8, Bold: true}},
						Label{Text: "连接云端工作台与这台采购电脑", TextColor: navy, Font: Font{Family: "Microsoft YaHei UI", PointSize: 15, Bold: true}},
						Label{Text: "状态中心关闭后仍驻留托盘，不影响后台任务。", TextColor: muted},
					}},
				},
			},
			Composite{
				Border: true, Background: SolidColorBrush{Color: white},
				Layout: HBox{Margins: Margins{Left: 18, Top: 12, Right: 18, Bottom: 12}, Spacing: 12},
				Children: []Widget{
					Label{Text: "●", TextColor: teal, Font: Font{Family: "Segoe UI Symbol", PointSize: 20, Bold: true}, MinSize: Size{Width: 36}},
					Composite{Background: SolidColorBrush{Color: white}, Layout: VBox{MarginsZero: true, Spacing: 2}, Children: []Widget{
						Label{AssignTo: &app.statusTitle, Text: "正在启动本地执行器…", TextColor: navy, Font: Font{Family: "Microsoft YaHei UI", PointSize: 11, Bold: true}},
						Label{AssignTo: &app.statusDetail, Text: "正在读取云端、HubStudio 和本地任务状态。", TextColor: muted},
					}},
					HSpacer{},
					Label{AssignTo: &app.heartbeatValue, Text: "等待心跳", TextColor: muted, TextAlignment: AlignFar},
				},
			},
			Composite{Layout: Grid{Columns: 2, Spacing: 10}, Background: SolidColorBrush{Color: canvas}, Children: []Widget{
				statusCard("云端通道", &app.cloudValue, "正在连接", white, line, navy, muted),
				statusCard("HubStudio", &app.hubValue, "正在检查", white, line, navy, muted),
				statusCard("本机任务", &app.taskValue, "0 个运行中", white, line, navy, muted),
				statusCard("执行器版本", &app.versionValue, "—", white, line, navy, muted),
			}},
			Composite{
				Border: true, Background: SolidColorBrush{Color: white},
				Layout: VBox{Margins: Margins{Left: 16, Top: 12, Right: 16, Bottom: 12}, Spacing: 7},
				Children: []Widget{
					Label{Text: "设备配对", TextColor: navy, Font: Font{Family: "Microsoft YaHei UI", PointSize: 10, Bold: true}},
					Label{AssignTo: &app.deviceValue, Text: "尚未读取设备状态", TextColor: muted},
					Composite{Background: SolidColorBrush{Color: white}, Layout: HBox{MarginsZero: true, Spacing: 8}, Children: []Widget{
						LineEdit{AssignTo: &app.pairEdit, CueBanner: "输入云端显示的 8 位一次性配对码", MaxLength: 9},
						PushButton{AssignTo: &app.pairButton, Text: "配对这台电脑", MinSize: Size{Width: 122, Height: 34}, OnClicked: func() { app.startPair(app.pairEdit.Text()) }},
					}},
				},
			},
			Composite{Background: SolidColorBrush{Color: canvas}, Layout: HBox{MarginsZero: true, Spacing: 8}, Children: []Widget{
				PushButton{Text: "打开云端工作台", MinSize: Size{Width: 142, Height: 38}, OnClicked: func() { app.openCloudWorkspace() }},
				PushButton{AssignTo: &app.startButton, Text: "重新启动执行器", MinSize: Size{Width: 132, Height: 38}, OnClicked: func() { go app.restartExecutor() }},
				PushButton{Text: "打开日志目录", MinSize: Size{Width: 112, Height: 38}, OnClicked: func() { app.openLogs() }},
				HSpacer{},
				PushButton{Text: "刷新状态", MinSize: Size{Width: 96, Height: 38}, OnClicked: func() { go app.refreshStatus() }},
			}},
			Label{Text: "云端心跳是在线状态的最终依据；托盘不会静默执行采购、下单或删除动作。", TextColor: muted, TextAlignment: AlignCenter},
		},
	}
	if err := window.Create(); err != nil {
		return err
	}
	return nil
}

func statusCard(title string, target **walk.Label, initial string, background, line, navy, muted walk.Color) Widget {
	return Composite{
		Border: true, Background: SolidColorBrush{Color: background},
		Layout: VBox{Margins: Margins{Left: 15, Top: 11, Right: 15, Bottom: 11}, Spacing: 3},
		Children: []Widget{
			Label{Text: title, TextColor: muted, Font: Font{Family: "Microsoft YaHei UI", PointSize: 8, Bold: true}},
			Label{AssignTo: target, Text: initial, TextColor: navy, Font: Font{Family: "Microsoft YaHei UI", PointSize: 11, Bold: true}},
		},
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

	app.trayStatus = newTrayAction("状态中心 · 正在启动", app.showStatusCenter)
	openCloud := newTrayAction("打开云端工作台", app.openCloudWorkspace)
	app.trayStartStop = newTrayAction("重新启动执行器", func() { go app.restartExecutor() })
	pair := newTrayAction("配对这台电脑…", app.showStatusCenter)
	logs := newTrayAction("打开日志目录", app.openLogs)
	about := newTrayAction("关于 Xynigo", func() {
		walk.MsgBox(app.mw, "关于 Xynigo", "Xynigo Sourcing 本地执行器\n云端 Web 统一入口 · 本机安全执行", walk.MsgBoxIconInformation)
	})
	exit := newTrayAction("退出 Xynigo", app.exitApplication)
	for _, action := range []*walk.Action{
		app.trayStatus, openCloud, walk.NewSeparatorAction(), app.trayStartStop,
		pair, logs, walk.NewSeparatorAction(), about, exit,
	} {
		if err := notify.ContextMenu().Actions().Add(action); err != nil {
			return err
		}
	}
	return notify.SetVisible(true)
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
	})
}

func (app *launcherApp) openCloudWorkspace() {
	_ = exec.Command("rundll32.exe", "url.dll,FileProtocolHandler", cloudWorkspaceURL).Start()
}

func (app *launcherApp) openLogs() {
	directory := filepath.Join(app.root, "日志")
	if _, err := os.Stat(directory); errors.Is(err, os.ErrNotExist) {
		directory = filepath.Join(app.root, "查询日志")
	}
	_ = os.MkdirAll(directory, 0o700)
	_ = exec.Command("explorer.exe", directory).Start()
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
	app.mw.Synchronize(func() { app.renderStatus(status, err) })
}

func (app *launcherApp) fetchStatus() (*localStatus, error) {
	start := configuredServerPort(app.root)
	client := &http.Client{Timeout: 700 * time.Millisecond}
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
	if err != nil || status == nil {
		app.statusTitle.SetText("本地执行器未运行")
		app.statusDetail.SetText("可点击“启动执行器”恢复本机服务；云端工作台仍可独立使用。")
		app.cloudValue.SetText("未连接")
		app.hubValue.SetText("等待执行器")
		app.taskValue.SetText("—")
		app.versionValue.SetText("—")
		app.deviceValue.SetText("设备状态将在执行器启动后显示")
		app.heartbeatValue.SetText("无本机心跳")
		app.startButton.SetText("启动执行器")
		_ = app.trayStatus.SetText("状态中心 · 执行器未运行")
		_ = app.trayStartStop.SetText("启动执行器")
		_ = app.notify.SetToolTip("Xynigo 本地执行器 · 未运行")
		return
	}
	app.startButton.SetText("重新启动执行器")
	_ = app.trayStartStop.SetText("重新启动执行器")
	app.versionValue.SetText("v" + status.Version)
	if status.HubStudio.Connected {
		app.hubValue.SetText("已连接")
	} else {
		app.hubValue.SetText("未连接")
	}
	if status.Tasks.ActiveCount == 0 {
		app.taskValue.SetText("当前空闲")
	} else {
		app.taskValue.SetText(fmt.Sprintf("%d 个任务运行中", status.Tasks.ActiveCount))
	}
	if status.Executor.Paired {
		name := strings.TrimSpace(status.Executor.DisplayName)
		if name == "" {
			name = "这台采购电脑"
		}
		app.deviceValue.SetText(name + " · 已完成设备配对")
	} else {
		app.deviceValue.SetText("尚未配对 · 请在云端生成一次性配对码")
	}
	app.heartbeatValue.SetText(relativeTime(status.CloudChannel.LastPollAt))
	cloudText := cloudStatusText(status.CloudChannel.Status)
	app.cloudValue.SetText(cloudText)
	if status.CloudChannel.Status == "online" {
		app.statusTitle.SetText("本地执行器已连接云端")
		if status.HubStudio.Connected {
			app.statusDetail.SetText("云端通道与 HubStudio 均已就绪，可以接收本机任务。")
		} else {
			app.statusDetail.SetText("云端通道正常；HubStudio 尚未连接，请先启动并登录 HubStudio。")
		}
	} else if !status.Executor.Paired || status.CloudChannel.Status == "not_paired" {
		app.statusTitle.SetText("本地执行器正在运行，等待设备配对")
		app.statusDetail.SetText("在云端工作台生成 8 位一次性配对码后，可在下方完成绑定。")
	} else {
		app.statusTitle.SetText("本地执行器正在重连云端")
		app.statusDetail.SetText("本机服务保持运行；网络恢复后会自动重连，不会重复执行写任务。")
	}
	trayText := "状态中心 · " + cloudText
	_ = app.trayStatus.SetText(trayText)
	_ = app.notify.SetToolTip("Xynigo 本地执行器 · " + cloudText)
}

func cloudStatusText(status string) string {
	switch status {
	case "online":
		return "云端在线"
	case "connecting", "paired":
		return "正在连接"
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

func (app *launcherApp) ensureExecutor() {
	if _, err := app.fetchStatus(); err == nil {
		return
	}
	if err := app.startExecutor(); err != nil {
		app.mw.Synchronize(func() {
			app.statusTitle.SetText("本地执行器启动失败")
			app.statusDetail.SetText("请重新安装标准版，或联系管理员检查运行时完整性。")
		})
	}
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
	cmd.Env = append(os.Environ(),
		"XYNIGO_DATA_DIR="+app.root,
		"XYNIGO_INSTALL_DIR="+app.root,
		"XYNIGO_INSTALL_MODE="+installMode,
		"XYNIGO_LAUNCHER_TOKEN="+app.launcherToken,
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
