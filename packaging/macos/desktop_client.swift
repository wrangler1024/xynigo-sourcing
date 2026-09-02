import AppKit
import Foundation
import WebKit

private let desktopCloudURL = URL(string: "https://xynigo.samforo.icu")!
private let desktopDataFolder = "XynigoSourcing"
private let desktopSettingsQuery = "view=localsettings"
private let desktopPairPattern = try! NSRegularExpression(
    pattern: "^[A-HJ-NP-Z2-9]{4}-?[A-HJ-NP-Z2-9]{4}$",
    options: [.caseInsensitive]
)

private struct DesktopStatus: Decodable {
    let schemaVersion: Int
    let version: String
    let executor: DesktopExecutorSummary
    let cloudChannel: DesktopCloudSummary
    let hubStudio: DesktopHubSummary
    let tasks: DesktopTaskSummary
    let update: DesktopUpdateSummary
}

private struct DesktopExecutorSummary: Decodable {
    let paired: Bool
    let displayName: String
}

private struct DesktopCloudSummary: Decodable {
    let status: String
    let lastPollAt: String?
    let phase: String?
    let attempt: Int?
}

private struct DesktopHubSummary: Decodable {
    let connected: Bool
}

private struct DesktopTaskSummary: Decodable {
    let activeCount: Int
    let items: [DesktopTaskItem]
}

private struct DesktopTaskItem: Decodable {
    let label: String
    let elapsedSec: Int
}

private struct DesktopUpdateSummary: Decodable {
    let enabled: Bool
    let state: String
    let installMode: String
    let latestVersion: String?
    let message: String?
    let downloadPercent: Int?
}

private final class DesktopCard: NSBox {
    init(fill: NSColor, border: NSColor = .clear) {
        super.init(frame: .zero)
        boxType = .custom
        fillColor = fill
        borderColor = border
        borderWidth = border == .clear ? 0 : 1
        cornerRadius = 12
        contentViewMargins = NSSize(width: 14, height: 12)
        translatesAutoresizingMaskIntoConstraints = false
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }
}

final class XynigoDesktopDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate,
    WKNavigationDelegate, WKScriptMessageHandler {
    private var window: NSWindow?
    private var webView: WKWebView?
    private var statusItem: NSStatusItem?
    private var startMenuItem: NSMenuItem?
    private var settingsMenuItem: NSMenuItem?
    private var pairMenuItem: NSMenuItem?

    private let statusTitle = NSTextField(labelWithString: "正在启动 Xynigo 本地执行器")
    private let statusDetail = NSTextField(labelWithString: "正在检查本机运行时与云端连接。")
    private let heartbeatValue = NSTextField(labelWithString: "等待首次心跳")
    private let cloudValue = NSTextField(labelWithString: "正在连接")
    private let cloudNote = NSTextField(labelWithString: "等待本机执行器")
    private let hubValue = NSTextField(labelWithString: "正在检查")
    private let hubNote = NSTextField(labelWithString: "等待 HubStudio Local API")
    private let taskValue = NSTextField(labelWithString: "当前空闲")
    private let taskNote = NSTextField(labelWithString: "没有运行中的任务")
    private let versionValue = NSTextField(labelWithString: "—")
    private let versionNote = NSTextField(labelWithString: "等待读取版本")
    private let deviceValue = NSTextField(labelWithString: "正在读取设备状态")
    private let pairField = NSTextField(string: "")
    private let pairButton = NSButton(title: "配对这台电脑", target: nil, action: nil)
    private let pairingCard = DesktopCard(
        fill: .white,
        border: NSColor(calibratedWhite: 0.88, alpha: 1)
    )
    private let startButton = NSButton(title: "启动执行器", target: nil, action: nil)
    private let settingsButton = NSButton(title: "本机设置", target: nil, action: nil)
    private let updateButton = NSButton(title: "检查更新", target: nil, action: nil)

    private var pollTimer: Timer?
    private var statusProbeInFlight = false
    private var lastStatus: DesktopStatus?
    private var statusBaseURL: URL?
    private var childProcess: Process?
    private var childLogHandle: FileHandle?
    private var pairInFlight = false
    private var updateInFlight = false
    private var quitting = false
    private var pendingOpenSettings = false
    private let launcherToken = UUID().uuidString.replacingOccurrences(of: "-", with: "")
    private lazy var session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 0.7
        configuration.timeoutIntervalForResource = 1.0
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        return URLSession(configuration: configuration)
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMainMenu()
        buildWindow()
        buildStatusMenu()
        showDesktopClient()
        if prepareFirstLaunch() {
            ensureExecutor()
        } else {
            renderOffline("尚未启动；可在窗口中继续操作。")
        }
        pollTimer = Timer.scheduledTimer(
            timeInterval: 2,
            target: self,
            selector: #selector(refreshStatus),
            userInfo: nil,
            repeats: true
        )
        refreshStatus()
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        guard urls.count == 1 else {
            showAlert("无效的 Xynigo 启动请求", "该请求未执行。", .warning)
            return
        }
        handleProtocol(urls[0])
    }

    func applicationShouldHandleReopen(
        _ sender: NSApplication,
        hasVisibleWindows flag: Bool
    ) -> Bool {
        showDesktopClient()
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        quitting = true
        pollTimer?.invalidate()
        webView?.configuration.userContentController.removeScriptMessageHandler(
            forName: "xynigo"
        )
        stopManagedExecutor()
        return .terminateNow
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        statusItem?.button?.toolTip = "Xynigo 桌面客户端仍在运行"
        return false
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        guard message.name == "xynigo",
              message.frameInfo.isMainFrame,
              let sourceURL = message.frameInfo.request.url,
              sourceURL.scheme == "http",
              sourceURL.host == "127.0.0.1",
              let body = message.body as? [String: Any],
              let action = body["action"] as? String else {
            return
        }
        let payload = body["payload"] as? [String: Any] ?? [:]
        switch action {
        case "open-external":
            guard let raw = payload["url"] as? String,
                  let url = URL(string: raw),
                  url.scheme == "https" else { return }
            NSWorkspace.shared.open(url)
        case "open-logs":
            openLogs()
        case "restart-executor":
            restartExecutor()
        case "check-update":
            handleUpdate()
        case "pair-device":
            guard let code = payload["code"] as? String else { return }
            pairField.stringValue = code
            pairDevice()
        case "run-diagnostics":
            refreshStatus()
            notifyWeb("已刷新本机连接、任务与更新状态")
        case "export-diagnostics":
            exportDiagnosticSummary()
        case "backup-config":
            backupCurrentConfig()
        case "open-legacy-settings":
            openLegacySettings()
        default:
            break
        }
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if url.scheme == "about" ||
            (url.scheme == "http" && url.host == "127.0.0.1") {
            decisionHandler(.allow)
            return
        }
        if url.scheme == "https" {
            NSWorkspace.shared.open(url)
        }
        decisionHandler(.cancel)
    }

    private func loadDesktopUI(_ baseURL: URL, view: String? = nil) {
        guard var components = URLComponents(
            url: baseURL,
            resolvingAgainstBaseURL: false
        ) else { return }
        components.path = "/desktop/"
        var items = [URLQueryItem(name: "platform", value: "mac")]
        if let view { items.append(URLQueryItem(name: "view", value: view)) }
        components.queryItems = items
        guard let url = components.url else { return }
        if let current = webView?.url,
           current.host == url.host,
           current.port == url.port,
           current.path == url.path {
            if let view {
                webView?.evaluateJavaScript(
                    "window.xynigoDesktop && window.xynigoDesktop.navigate(\"\(view)\")"
                )
            }
            return
        }
        webView?.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))
    }

    private func notifyWeb(_ message: String) {
        guard let data = try? JSONSerialization.data(withJSONObject: message),
              let encoded = String(data: data, encoding: .utf8) else { return }
        webView?.evaluateJavaScript(
            "window.xynigoDesktop && window.xynigoDesktop.notify(\(encoded))"
        )
    }

    private func buildMainMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 Xynigo", action: #selector(showAbout), keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "显示 Xynigo", action: #selector(showDesktopClient), keyEquivalent: "0")
        appMenu.addItem(withTitle: "本机设置…", action: #selector(openLocalSettings), keyEquivalent: ",")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "退出 Xynigo", action: #selector(quitApplication), keyEquivalent: "q")
        for item in appMenu.items { item.target = self }
        appItem.submenu = appMenu
        NSApplication.shared.mainMenu = mainMenu
    }

    private func buildWindow() {
        let desktopWindow = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1360, height: 746),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        desktopWindow.title = "Xynigo 本地执行器"
        desktopWindow.minSize = NSSize(width: 1080, height: 650)
        desktopWindow.center()
        desktopWindow.delegate = self
        desktopWindow.isReleasedWhenClosed = false
        desktopWindow.tabbingMode = .disallowed
        desktopWindow.backgroundColor = .white

        let controller = WKUserContentController()
        controller.add(self, name: "xynigo")
        let configuration = WKWebViewConfiguration()
        configuration.userContentController = controller
        configuration.websiteDataStore = .default()
        let browser = WKWebView(frame: .zero, configuration: configuration)
        browser.navigationDelegate = self
        browser.allowsBackForwardNavigationGestures = false
        browser.allowsLinkPreview = false
        browser.translatesAutoresizingMaskIntoConstraints = false
        browser.loadHTMLString(
            """
            <!doctype html><meta charset="utf-8"><style>
            html,body{height:100%;margin:0;font-family:-apple-system;background:#fff;color:#123252}
            body{display:grid;place-items:center;text-align:center}.x{width:52px;height:52px;margin:auto;
            display:grid;place-items:center;border-radius:14px;color:white;font-size:22px;font-weight:800;
            background:linear-gradient(135deg,#31b8ae,#087c83);box-shadow:0 12px 32px #d5efed}
            h2{font-size:16px;margin:18px 0 6px}p{font-size:12px;color:#64748b}
            </style><div><div class="x">X</div><h2>正在启动 Xynigo 本地执行器</h2>
            <p>正在自动发现本机安全服务端口…</p></div>
            """,
            baseURL: nil
        )

        guard let contentView = desktopWindow.contentView else { return }
        contentView.addSubview(browser)
        NSLayoutConstraint.activate([
            browser.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            browser.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            browser.topAnchor.constraint(equalTo: contentView.topAnchor),
            browser.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
        ])
        webView = browser
        window = desktopWindow
    }

    private func makeHeader() -> NSView {
        let icon = NSImageView()
        icon.image = NSApplication.shared.applicationIconImage
        icon.imageScaling = .scaleProportionallyUpOrDown
        icon.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            icon.widthAnchor.constraint(equalToConstant: 58),
            icon.heightAnchor.constraint(equalToConstant: 58),
        ])
        let eyebrow = label(
            "XYNIGO DESKTOP",
            10,
            .bold,
            NSColor(calibratedRed: 0.086, green: 0.596, blue: 0.627, alpha: 1)
        )
        let title = label("连接云端工作台与这台采购电脑", 22, .bold)
        let subtitle = label(
            "Windows 与 macOS 共用同一配置契约、成员身份和本机执行器。",
            12,
            .regular,
            NSColor(calibratedWhite: 0.40, alpha: 1)
        )
        let copy = NSStackView(views: [eyebrow, title, subtitle])
        copy.orientation = .vertical
        copy.alignment = .leading
        copy.spacing = 3
        let header = NSStackView(views: [icon, copy])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 14
        return header
    }

    private func makeStatusBanner() -> NSView {
        let card = DesktopCard(
            fill: NSColor(calibratedRed: 0.910, green: 0.973, blue: 0.969, alpha: 1),
            border: NSColor(calibratedRed: 0.72, green: 0.88, blue: 0.88, alpha: 1)
        )
        statusTitle.font = .systemFont(ofSize: 15, weight: .semibold)
        statusDetail.font = .systemFont(ofSize: 11)
        statusDetail.textColor = NSColor(calibratedWhite: 0.36, alpha: 1)
        statusDetail.maximumNumberOfLines = 2
        heartbeatValue.font = .systemFont(ofSize: 11, weight: .medium)
        heartbeatValue.textColor = NSColor(calibratedWhite: 0.38, alpha: 1)
        heartbeatValue.alignment = .right
        let copy = NSStackView(views: [statusTitle, statusDetail])
        copy.orientation = .vertical
        copy.alignment = .leading
        copy.spacing = 3
        let spacer = NSView()
        let row = NSStackView(views: [copy, spacer, heartbeatValue])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 12
        card.contentView = row
        card.heightAnchor.constraint(equalToConstant: 90).isActive = true
        return card
    }

    private func statusCard(
        _ title: String,
        _ value: NSTextField,
        _ note: NSTextField
    ) -> NSView {
        let card = DesktopCard(fill: .white, border: NSColor(calibratedWhite: 0.88, alpha: 1))
        let heading = label(
            title.uppercased(),
            10,
            .semibold,
            NSColor(calibratedWhite: 0.43, alpha: 1)
        )
        value.font = .systemFont(ofSize: 15, weight: .semibold)
        note.font = .systemFont(ofSize: 10)
        note.textColor = NSColor(calibratedWhite: 0.43, alpha: 1)
        note.maximumNumberOfLines = 2
        let stack = NSStackView(views: [heading, value, note])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 4
        card.contentView = stack
        card.heightAnchor.constraint(equalToConstant: 82).isActive = true
        return card
    }

    private func configurePairingCard() {
        let heading = label("设备配对", 13, .semibold)
        deviceValue.font = .systemFont(ofSize: 11)
        deviceValue.textColor = NSColor(calibratedWhite: 0.40, alpha: 1)
        pairField.placeholderString = "输入云端生成的 8 位一次性配对码"
        pairField.font = .systemFont(ofSize: 12)
        pairField.translatesAutoresizingMaskIntoConstraints = false
        pairField.widthAnchor.constraint(greaterThanOrEqualToConstant: 360).isActive = true
        configure(pairButton, #selector(pairDevice), true)
        let inputRow = NSStackView(views: [pairField, pairButton])
        inputRow.orientation = .horizontal
        inputRow.alignment = .centerY
        inputRow.spacing = 8
        let stack = NSStackView(views: [heading, deviceValue, inputRow])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 6
        pairingCard.contentView = stack
        pairingCard.heightAnchor.constraint(equalToConstant: 104).isActive = true
    }

    private func makeActionBar() -> NSView {
        let cloudButton = NSButton(title: "打开云端工作台", target: nil, action: nil)
        let logsButton = NSButton(title: "日志", target: nil, action: nil)
        let refreshButton = NSButton(title: "刷新状态", target: nil, action: nil)
        configure(cloudButton, #selector(openCloudWorkspace), true)
        configure(settingsButton, #selector(openLocalSettings))
        configure(startButton, #selector(restartExecutor))
        configure(updateButton, #selector(handleUpdate))
        configure(logsButton, #selector(openLogs))
        configure(refreshButton, #selector(refreshStatus))
        let row = NSStackView(views: [
            cloudButton,
            settingsButton,
            startButton,
            updateButton,
            NSView(),
            logsButton,
            refreshButton,
        ])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 8
        return row
    }

    private func label(
        _ text: String,
        _ size: CGFloat,
        _ weight: NSFont.Weight = .regular,
        _ color: NSColor = NSColor(calibratedRed: 0.031, green: 0.145, blue: 0.282, alpha: 1)
    ) -> NSTextField {
        let result = NSTextField(labelWithString: text)
        result.font = .systemFont(ofSize: size, weight: weight)
        result.textColor = color
        return result
    }

    private func configure(
        _ button: NSButton,
        _ action: Selector,
        _ emphasized: Bool = false
    ) {
        button.target = self
        button.action = action
        button.bezelStyle = .rounded
        button.controlSize = .large
        if emphasized {
            button.contentTintColor = NSColor(
                calibratedRed: 0.086,
                green: 0.596,
                blue: 0.627,
                alpha: 1
            )
        }
    }

    private func buildStatusMenu() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.title = "X"
        item.button?.toolTip = "Xynigo 桌面客户端"
        let menu = NSMenu()
        menu.addItem(withTitle: "打开桌面客户端", action: #selector(showDesktopClient), keyEquivalent: "")
        menu.addItem(withTitle: "打开本机设置", action: #selector(openLocalSettings), keyEquivalent: "")
        menu.addItem(withTitle: "打开云端工作台", action: #selector(openCloudWorkspace), keyEquivalent: "")
        menu.addItem(NSMenuItem.separator())
        startMenuItem = menu.addItem(withTitle: "启动执行器", action: #selector(restartExecutor), keyEquivalent: "")
        pairMenuItem = menu.addItem(withTitle: "配对这台电脑…", action: #selector(focusPairing), keyEquivalent: "")
        settingsMenuItem = menu.item(withTitle: "打开本机设置")
        menu.addItem(withTitle: "打开日志目录", action: #selector(openLogs), keyEquivalent: "")
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle: "退出 Xynigo", action: #selector(quitApplication), keyEquivalent: "q")
        for menuItem in menu.items { menuItem.target = self }
        item.menu = menu
        statusItem = item
    }

    @objc private func showDesktopClient() {
        guard let window else { return }
        NSApplication.shared.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    @objc private func focusPairing() {
        showDesktopClient()
        webView?.evaluateJavaScript(
            "window.xynigoDesktop && window.xynigoDesktop.focusPairing()"
        )
    }

    @objc private func openCloudWorkspace() {
        NSWorkspace.shared.open(desktopCloudURL)
    }

    @objc private func openLocalSettings() {
        guard let baseURL = statusBaseURL else {
            pendingOpenSettings = true
            ensureExecutor()
            showAlert(
                "本机设置正在准备",
                "执行器启动并完成端口发现后，将自动打开设置页。",
                .informational
            )
            return
        }
        pendingOpenSettings = false
        showDesktopClient()
        loadDesktopUI(baseURL, view: "settings")
    }

    private func openLegacySettings() {
        guard let baseURL = statusBaseURL,
              var components = URLComponents(
                url: baseURL,
                resolvingAgainstBaseURL: false
              ) else { return }
        components.path = "/"
        components.percentEncodedQuery = desktopSettingsQuery
        if let url = components.url { NSWorkspace.shared.open(url) }
    }

    @objc private func openLogs() {
        do {
            let logs = try dataDirectory().appendingPathComponent("日志", isDirectory: true)
            try FileManager.default.createDirectory(
                at: logs,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            NSWorkspace.shared.open(logs)
        } catch {
            showAlert("日志目录不可用", "无法打开本机日志目录。", .warning)
        }
    }

    private func backupCurrentConfig() {
        do {
            let directory = try dataDirectory()
            let source = directory.appendingPathComponent("config.json")
            guard FileManager.default.fileExists(atPath: source.path) else {
                showAlert("暂无配置可备份", "本机尚未生成 config.json。", .informational)
                return
            }
            let backups = directory.appendingPathComponent("历史备份", isDirectory: true)
            try FileManager.default.createDirectory(
                at: backups,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let target = backups.appendingPathComponent(
                "config-\(desktopTimestamp()).json"
            )
            try FileManager.default.copyItem(at: source, to: target)
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: target.path
            )
            NSWorkspace.shared.activateFileViewerSelecting([target])
            notifyWeb("当前配置已备份到本机历史备份目录")
        } catch {
            showAlert("配置备份失败", "无法创建本机配置备份。", .warning)
        }
    }

    private func exportDiagnosticSummary() {
        do {
            let directory = try dataDirectory()
            let logs = directory.appendingPathComponent("日志", isDirectory: true)
            try FileManager.default.createDirectory(
                at: logs,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let target = logs.appendingPathComponent(
                "Xynigo-脱敏诊断-\(desktopTimestamp()).txt"
            )
            let status = lastStatus
            let lines = [
                "Xynigo 脱敏诊断摘要",
                "生成时间：\(ISO8601DateFormatter().string(from: Date()))",
                "运行时：\(runtimeID() ?? "不可用")",
                "云端通道：\(status.map { cloudStatus($0.cloudChannel.status) } ?? "未连接")",
                "HubStudio：\(status?.hubStudio.connected == true ? "已连接" : "未连接")",
                "活动任务数：\(status?.tasks.activeCount ?? 0)",
                "设备配对：\(status?.executor.paired == true ? "已完成" : "未完成")",
                "说明：本文件不包含凭证、飞书链接、业务明文或设备令牌。",
            ].joined(separator: "\n") + "\n"
            try lines.write(to: target, atomically: true, encoding: .utf8)
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: target.path
            )
            NSWorkspace.shared.activateFileViewerSelecting([target])
            notifyWeb("脱敏诊断摘要已生成，不包含凭证和业务明文")
        } catch {
            showAlert("诊断包导出失败", "无法生成脱敏诊断摘要。", .warning)
        }
    }

    private func desktopTimestamp() -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        return formatter.string(from: Date())
    }

    @objc private func showAbout() {
        showAlert(
            "Xynigo 桌面客户端",
            "Windows / macOS 双平台本地执行器\nRuntime：\(runtimeID() ?? "版本信息不可用")",
            .informational
        )
    }

    @objc private func quitApplication() {
        NSApplication.shared.terminate(nil)
    }

    private func prepareFirstLaunch() -> Bool {
        do {
            let directory = try dataDirectory()
            let marker = directory.appendingPathComponent(".standard-launched")
            let config = directory.appendingPathComponent("config.json")
            if !FileManager.default.fileExists(atPath: marker.path),
               !FileManager.default.fileExists(atPath: config.path) {
                let alert = NSAlert()
                alert.messageText = "首次启动 Xynigo 标准版"
                alert.informativeText = "以前使用过绿色包时，可先迁移配置、日志和运行数据；新用户可直接启动。"
                alert.addButton(withTitle: "直接启动")
                alert.addButton(withTitle: "迁移绿色包数据…")
                alert.addButton(withTitle: "暂不启动")
                let response = alert.runModal()
                if response == .alertSecondButtonReturn {
                    try openTerminalScript("迁移绿色包数据.command")
                    return false
                }
                if response != .alertFirstButtonReturn { return false }
            }
            try Data().write(to: marker, options: .atomic)
            return true
        } catch {
            showAlert("Xynigo 启动失败", "无法准备本机数据目录。", .critical)
            return false
        }
    }

    private func ensureExecutor() {
        discoverStatus { [weak self] status, baseURL in
            guard let self else { return }
            if let status, let baseURL {
                self.apply(status, baseURL)
            } else {
                self.startManagedExecutor()
            }
        }
    }

    private func startManagedExecutor() {
        guard !quitting else { return }
        if childProcess?.isRunning == true { return }
        do {
            let resources = try resourcesDirectory()
            let runtime = resources
                .appendingPathComponent("runtime", isDirectory: true)
                .appendingPathComponent("xynigo-sourcing")
            guard FileManager.default.isExecutableFile(atPath: runtime.path) else {
                throw NSError(domain: "XynigoLauncher", code: 2)
            }
            let directory = try dataDirectory()
            let logDirectory = directory.appendingPathComponent("日志", isDirectory: true)
            try FileManager.default.createDirectory(
                at: logDirectory,
                withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700]
            )
            let logURL = logDirectory.appendingPathComponent("本地执行器.log")
            if !FileManager.default.fileExists(atPath: logURL.path) {
                _ = FileManager.default.createFile(atPath: logURL.path, contents: nil)
            }
            try? FileManager.default.setAttributes(
                [.posixPermissions: 0o600],
                ofItemAtPath: logURL.path
            )
            let logHandle = try FileHandle(forWritingTo: logURL)
            try logHandle.seekToEnd()
            let process = Process()
            process.executableURL = runtime
            process.arguments = ["--no-browser"]
            process.currentDirectoryURL = directory
            process.environment = executorEnvironment(resources, directory)
            process.standardOutput = logHandle
            process.standardError = logHandle
            process.terminationHandler = { [weak self, weak process] _ in
                DispatchQueue.main.async {
                    guard let self, let process else { return }
                    if self.childProcess === process {
                        self.childProcess = nil
                        try? self.childLogHandle?.close()
                        self.childLogHandle = nil
                        if !self.quitting {
                            self.renderOffline("执行器已退出；可点击“启动执行器”重试。")
                        }
                    }
                }
            }
            try process.run()
            childProcess = process
            childLogHandle = logHandle
            statusTitle.stringValue = "本地执行器正在启动"
            statusDetail.stringValue = "正在自动发现可用端口；已占用端口会被安全跳过。"
            startButton.title = "重新启动执行器"
            startMenuItem?.title = "重新启动执行器"
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
                self?.refreshStatus()
            }
        } catch {
            renderOffline("运行时启动失败，请打开日志或重新安装候选包。")
            showAlert("Xynigo 本地执行器未能启动", "本机运行时不可用。", .critical)
        }
    }

    private func executorEnvironment(_ resources: URL, _ directory: URL) -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        environment["XYNIGO_DATA_DIR"] = directory.path
        environment["XYNIGO_INSTALL_DIR"] = resources.path
        environment["XYNIGO_INSTALL_MODE"] = "standard"
        environment["XYNIGO_RUNTIME_ID"] = runtimeID() ?? ""
        environment["XYNIGO_LAUNCHER_TOKEN"] = launcherToken
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        return environment
    }

    @objc private func restartExecutor() {
        if (lastStatus?.tasks.activeCount ?? 0) > 0 {
            showAlert(
                "本机任务正在执行",
                "请等待当前任务完成后再重启，客户端不会中断采购任务。",
                .warning
            )
            return
        }
        stopManagedExecutor()
        statusBaseURL = nil
        lastStatus = nil
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            self?.startManagedExecutor()
        }
    }

    private func stopManagedExecutor() {
        guard let process = childProcess, process.isRunning else { return }
        if quitting {
            process.terminate()
            return
        }
        if let baseURL = statusBaseURL {
            var request = URLRequest(
                url: baseURL.appendingPathComponent("executor-control/shutdown")
            )
            request.httpMethod = "POST"
            request.setValue(launcherToken, forHTTPHeaderField: "X-Xynigo-Launcher")
            session.dataTask(with: request).resume()
        }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 1.2) { [weak process] in
            if process?.isRunning == true { process?.terminate() }
        }
    }

    @objc private func refreshStatus() {
        discoverStatus { [weak self] status, baseURL in
            guard let self else { return }
            if let status, let baseURL {
                self.apply(status, baseURL)
            } else if self.childProcess?.isRunning == true {
                self.statusTitle.stringValue = "本地执行器正在启动"
                self.statusDetail.stringValue = "正在等待回环服务就绪并自动发现实际端口。"
            } else {
                self.renderOffline("可点击“启动执行器”恢复本机服务。")
            }
        }
    }

    private func discoverStatus(
        _ completion: @escaping (DesktopStatus?, URL?) -> Void
    ) {
        if statusProbeInFlight { return }
        statusProbeInFlight = true
        let start = configuredServerPort()
        probe(start, min(start + 9, 65535)) { [weak self] status, baseURL in
            DispatchQueue.main.async {
                self?.statusProbeInFlight = false
                completion(status, baseURL)
            }
        }
    }

    private func probe(
        _ port: Int,
        _ lastPort: Int,
        _ completion: @escaping (DesktopStatus?, URL?) -> Void
    ) {
        guard port <= lastPort,
              let baseURL = URL(string: "http://127.0.0.1:\(port)") else {
            completion(nil, nil)
            return
        }
        var request = URLRequest(url: baseURL.appendingPathComponent("executor-status.json"))
        request.timeoutInterval = 0.7
        session.dataTask(with: request) { [weak self] data, response, _ in
            if (response as? HTTPURLResponse)?.statusCode == 200,
               let data,
               let status = try? JSONDecoder().decode(DesktopStatus.self, from: data),
               status.schemaVersion == 1,
               !status.version.isEmpty {
                completion(status, baseURL)
                return
            }
            self?.probe(port + 1, lastPort, completion)
        }.resume()
    }

    private func apply(_ status: DesktopStatus, _ baseURL: URL) {
        lastStatus = status
        statusBaseURL = baseURL
        loadDesktopUI(baseURL)
        settingsButton.isEnabled = true
        settingsMenuItem?.isEnabled = true
        startButton.title = "重新启动执行器"
        startMenuItem?.title = "重新启动执行器"
        versionValue.stringValue = "v\(status.version)"
        versionNote.stringValue = clean(status.update.message) ?? "当前运行时已加载"
        hubValue.stringValue = status.hubStudio.connected ? "已连接" : "未连接"
        hubNote.stringValue = status.hubStudio.connected
            ? "HubStudio Local API 可用"
            : "请确认 HubStudio 已启动并登录"

        if status.tasks.activeCount > 0 {
            taskValue.stringValue = "\(status.tasks.activeCount) 个运行中"
            if let item = status.tasks.items.first {
                taskNote.stringValue = "\(item.label) · \(elapsed(item.elapsedSec))"
            } else {
                taskNote.stringValue = "任务正在本机安全执行"
            }
        } else {
            taskValue.stringValue = "当前空闲"
            taskNote.stringValue = "没有运行中的任务"
        }

        let paired = status.executor.paired && status.cloudChannel.status != "not_paired"
        pairingCard.isHidden = paired
        pairMenuItem?.isHidden = paired
        deviceValue.stringValue = paired
            ? "\(clean(status.executor.displayName) ?? "这台采购电脑") · 已完成设备配对"
            : "尚未配对 · 配对码 5 分钟内有效且只能使用一次"
        cloudValue.stringValue = cloudStatus(status.cloudChannel.status)
        cloudNote.stringValue = cloudPhase(status.cloudChannel)
        heartbeatValue.stringValue = heartbeat(status.cloudChannel.lastPollAt)

        switch status.cloudChannel.status {
        case "online":
            statusTitle.stringValue = status.tasks.activeCount > 0
                ? "本地执行器正在执行任务"
                : "Xynigo 桌面客户端在线"
            statusDetail.stringValue = status.hubStudio.connected
                ? "云端安全通道和 HubStudio 均已连接。"
                : "云端通道正常；HubStudio 尚未连接。"
        case "not_paired":
            statusTitle.stringValue = "执行器已启动，等待设备配对"
            statusDetail.stringValue = "在云端工作台生成一次性配对码，然后在下方完成绑定。"
        case "connecting", "reconnecting", "paired":
            statusTitle.stringValue = "正在建立云端安全通道"
            statusDetail.stringValue = "本机服务已经运行；网络恢复后会自动继续连接。"
        default:
            statusTitle.stringValue = "本机执行器已运行，云端暂时离线"
            statusDetail.stringValue = "本机设置仍可使用，云端连接会继续自动重试。"
        }
        renderUpdate(status.update, status.tasks.activeCount)
        statusItem?.button?.toolTip = "Xynigo 桌面客户端 · \(cloudValue.stringValue)"
        if pendingOpenSettings { openLocalSettings() }
    }

    private func renderOffline(_ message: String) {
        lastStatus = nil
        statusBaseURL = nil
        statusTitle.stringValue = "本地执行器未运行"
        statusDetail.stringValue = message
        cloudValue.stringValue = "未连接"
        cloudNote.stringValue = "等待本机执行器"
        hubValue.stringValue = "等待执行器"
        hubNote.stringValue = "尚未检查 Local API"
        taskValue.stringValue = "—"
        taskNote.stringValue = "执行器启动后显示"
        versionValue.stringValue = "v\((runtimeID() ?? "—").components(separatedBy: "-").first ?? "—")"
        versionNote.stringValue = "已安装，等待运行时启动"
        heartbeatValue.stringValue = "无本机心跳"
        settingsButton.isEnabled = false
        settingsMenuItem?.isEnabled = false
        updateButton.isEnabled = false
        startButton.title = "启动执行器"
        startMenuItem?.title = "启动执行器"
        pairingCard.isHidden = false
        pairMenuItem?.isHidden = false
        statusItem?.button?.toolTip = "Xynigo 桌面客户端 · 执行器未运行"
    }

    private func renderUpdate(_ update: DesktopUpdateSummary, _ activeTasks: Int) {
        var title = "检查更新"
        var enabled = update.enabled && update.installMode == "standard" && !updateInFlight
        switch update.state {
        case "checking": title = "正在检查…"; enabled = false
        case "available":
            title = clean(update.latestVersion).map { "更新到 v\($0)" } ?? "立即更新"
            if activeTasks > 0 { title = "任务结束后更新"; enabled = false }
        case "downloading": title = "下载 \(update.downloadPercent ?? 0)%"; enabled = false
        case "verifying": title = "正在校验…"; enabled = false
        case "extracting": title = "正在解压…"; enabled = false
        case "installing": title = "正在安装…"; enabled = false
        case "restarting": title = "正在重启…"; enabled = false
        case "disabled": title = "在线更新不可用"; enabled = false
        default: break
        }
        updateButton.title = title
        updateButton.isEnabled = enabled
    }

    @objc private func handleUpdate() {
        guard let status = lastStatus,
              status.update.enabled,
              status.update.installMode == "standard" else {
            showAlert("在线更新不可用", "请使用标准安装包覆盖升级。", .warning)
            return
        }
        if status.tasks.activeCount > 0 {
            showAlert("任务执行中", "请等待当前本机任务完成后再更新。", .warning)
            return
        }
        let install = status.update.state == "available"
        if install {
            let alert = NSAlert()
            alert.messageText = "确认更新 Xynigo"
            alert.informativeText = "将下载并校验新版本，随后由系统安装器完成更新。"
            alert.addButton(withTitle: "继续更新")
            alert.addButton(withTitle: "取消")
            if alert.runModal() != .alertFirstButtonReturn { return }
        }
        postControl(install ? "executor-control/update/install" : "executor-control/update/check")
    }

    private func postControl(_ path: String) {
        guard let baseURL = statusBaseURL else { return }
        updateInFlight = true
        updateButton.isEnabled = false
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = "POST"
        request.setValue(launcherToken, forHTTPHeaderField: "X-Xynigo-Launcher")
        session.dataTask(with: request) { [weak self] _, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                self.updateInFlight = false
                let statusCode = (response as? HTTPURLResponse)?.statusCode ?? 0
                if error != nil || ![200, 202].contains(statusCode) {
                    self.showAlert("更新请求失败", "本地执行器未接受更新请求。", .warning)
                }
                self.refreshStatus()
            }
        }.resume()
    }

    @objc private func pairDevice() {
        let raw = pairField.stringValue
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .uppercased()
        let range = NSRange(raw.startIndex..<raw.endIndex, in: raw)
        guard desktopPairPattern.firstMatch(in: raw, options: [], range: range) != nil else {
            showAlert("配对码无效", "请输入云端显示的 8 位一次性配对码。", .warning)
            return
        }
        guard !pairInFlight else { return }
        pairInFlight = true
        pairField.isEnabled = false
        pairButton.isEnabled = false
        pairButton.title = "正在配对…"
        let normalized = raw.replacingOccurrences(of: "-", with: "")
        let code = "\(normalized.prefix(4))-\(normalized.suffix(4))"
        do {
            let resources = try resourcesDirectory()
            let directory = try dataDirectory()
            let runtime = resources
                .appendingPathComponent("runtime", isDirectory: true)
                .appendingPathComponent("xynigo-sourcing")
            let process = Process()
            process.executableURL = runtime
            process.arguments = ["pair", code]
            process.currentDirectoryURL = directory
            process.environment = executorEnvironment(resources, directory)
            process.standardOutput = Pipe()
            process.standardError = process.standardOutput
            process.terminationHandler = { [weak self] process in
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.pairInFlight = false
                    self.pairField.isEnabled = true
                    self.pairButton.isEnabled = true
                    self.pairButton.title = "配对这台电脑"
                    if process.terminationStatus == 0 {
                        self.pairField.stringValue = ""
                        self.restartExecutor()
                        self.showAlert("设备配对完成", "正在等待云端心跳确认在线。", .informational)
                    } else {
                        self.showAlert("设备配对失败", "请确认配对码尚未过期并重试。", .warning)
                    }
                }
            }
            try process.run()
        } catch {
            pairInFlight = false
            pairField.isEnabled = true
            pairButton.isEnabled = true
            pairButton.title = "配对这台电脑"
            showAlert("设备配对失败", "本机运行时不可用。", .warning)
        }
    }

    private func handleProtocol(_ url: URL) {
        let raw = url.absoluteString
        guard allowedProtocol(raw) else {
            showAlert("无效的 Xynigo 启动请求", "该请求未执行。", .warning)
            return
        }
        showDesktopClient()
        if raw.range(
            of: #"^xynigo://settings/?$"#,
            options: [.regularExpression, .caseInsensitive]
        ) != nil {
            pendingOpenSettings = true
            ensureExecutor()
            return
        }
        if let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
           components.host?.caseInsensitiveCompare("pair") == .orderedSame,
           let code = components.queryItems?.first(where: { $0.name == "code" })?.value {
            pairField.stringValue = code
            pairDevice()
            return
        }
        ensureExecutor()
    }

    private func allowedProtocol(_ raw: String) -> Bool {
        guard raw.utf8.count <= 1024,
              !raw.contains("\n"),
              !raw.contains("\r"),
              !raw.contains("\0") else {
            return false
        }
        let pattern = #"^xynigo://(?:start/?|wake/?|settings/?|pair\?code=[A-HJ-NP-Z2-9]{4}-?[A-HJ-NP-Z2-9]{4})$"#
        return raw.range(
            of: pattern,
            options: [.regularExpression, .caseInsensitive]
        ) != nil
    }

    private func configuredServerPort() -> Int {
        guard let directory = try? dataDirectory(),
              let data = try? Data(contentsOf: directory.appendingPathComponent("config.json")),
              let object = try? JSONSerialization.jsonObject(with: data),
              let payload = object as? [String: Any],
              let port = payload["serverPort"] as? Int,
              (1...65526).contains(port) else {
            return 8765
        }
        return port
    }

    private func runtimeID() -> String? {
        guard let resources = try? resourcesDirectory(),
              let data = try? Data(contentsOf: resources.appendingPathComponent("VERSION.json")),
              let object = try? JSONSerialization.jsonObject(with: data),
              let payload = object as? [String: Any] else {
            return nil
        }
        return payload["runtimeId"] as? String ?? payload["version"] as? String
    }

    private func resourcesDirectory() throws -> URL {
        guard let url = Bundle.main.resourceURL else {
            throw NSError(domain: "XynigoLauncher", code: 1)
        }
        return url
    }

    private func dataDirectory() throws -> URL {
        let base = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let directory = base.appendingPathComponent(desktopDataFolder, isDirectory: true)
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        return directory
    }

    private func openTerminalScript(_ name: String) throws {
        let script = try resourcesDirectory().appendingPathComponent(name)
        guard FileManager.default.isExecutableFile(atPath: script.path) else {
            throw NSError(domain: "XynigoLauncher", code: 2)
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        process.arguments = ["-a", "Terminal", script.path]
        try process.run()
    }

    private func cloudStatus(_ status: String) -> String {
        switch status {
        case "online": return "云端在线"
        case "connecting", "paired": return "正在连接"
        case "reconnecting": return "正在重连"
        case "not_paired": return "等待配对"
        case "revoked": return "设备已撤销"
        case "credential_error": return "设备凭证异常"
        default: return "云端离线"
        }
    }

    private func cloudPhase(_ channel: DesktopCloudSummary) -> String {
        switch channel.phase ?? "" {
        case "authorizing": return "正在验证设备身份 · 第 \(max(channel.attempt ?? 1, 1)) 次"
        case "handshake": return "正在建立安全通道 · 第 \(max(channel.attempt ?? 1, 1)) 次"
        case "retry_wait": return "网络恢复后自动重试"
        case "listening": return "安全通道已建立"
        default: return channel.status == "not_paired" ? "需要一次性配对码" : "正在准备连接"
        }
    }

    private func heartbeat(_ value: String?) -> String {
        guard let value, let date = ISO8601DateFormatter().date(from: value) else {
            return "等待首次心跳"
        }
        let seconds = max(0, Int(Date().timeIntervalSince(date)))
        if seconds < 8 { return "心跳：刚刚" }
        if seconds < 60 { return "心跳：\(seconds) 秒前" }
        return "心跳：\(seconds / 60) 分钟前"
    }

    private func elapsed(_ seconds: Int) -> String {
        let safe = max(0, seconds)
        return String(format: "%02d:%02d", safe / 60, safe % 60)
    }

    private func clean(_ value: String?) -> String? {
        let result = String(value ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return result.isEmpty ? nil : result
    }

    private func showAlert(_ title: String, _ message: String, _ style: NSAlert.Style) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = style
        if let window, window.isVisible {
            alert.beginSheetModal(for: window)
        } else {
            alert.runModal()
        }
    }
}

@main
private enum XynigoDesktopMain {
    static func main() {
        let application = NSApplication.shared
        let delegate = XynigoDesktopDelegate()
        application.delegate = delegate
        application.setActivationPolicy(.regular)
        application.run()
    }
}
