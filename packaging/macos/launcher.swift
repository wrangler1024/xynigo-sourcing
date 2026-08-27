import AppKit
import Foundation

private let dataFolderName = "XynigoSourcing"

final class XynigoAppDelegate: NSObject, NSApplicationDelegate {
    private var handledURL = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.45) { [weak self] in
            guard let self = self, !self.handledURL else { return }
            self.launchExecutor()
            NSApplication.shared.terminate(nil)
        }
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        handledURL = true
        guard urls.count == 1, let raw = urls.first?.absoluteString,
              isAllowedProtocol(raw) else {
            showInvalidRequest()
            application.terminate(nil)
            return
        }
        do {
            try writePrivateRequest(raw, named: "protocol-request.txt")
            try openTerminalScript(named: "协议启动.command")
        } catch {
            showLaunchError()
        }
        application.terminate(nil)
    }

    private func launchExecutor() {
        do {
            let dataDirectory = try applicationDataDirectory()
            let marker = dataDirectory.appendingPathComponent(".standard-launched")
            let config = dataDirectory.appendingPathComponent("config.json")
            if !FileManager.default.fileExists(atPath: marker.path),
               !FileManager.default.fileExists(atPath: config.path) {
                NSApplication.shared.activate(ignoringOtherApps: true)
                let alert = NSAlert()
                alert.messageText = "首次启动 Xynigo 标准版"
                alert.informativeText = "以前使用过绿色包时，可先迁移配置、日志和运行数据；新用户可直接启动。"
                alert.addButton(withTitle: "直接启动")
                alert.addButton(withTitle: "迁移绿色包数据…")
                alert.addButton(withTitle: "取消")
                let response = alert.runModal()
                if response == .alertSecondButtonReturn {
                    try openTerminalScript(named: "迁移绿色包数据.command")
                    return
                }
                guard response == .alertFirstButtonReturn else { return }
            }
            try Data().write(to: marker, options: .atomic)
            try openTerminalScript(named: "启动本地执行器.command")
        } catch {
            showLaunchError()
        }
    }

    private func isAllowedProtocol(_ raw: String) -> Bool {
        guard raw.utf8.count <= 1024,
              !raw.contains("\n"), !raw.contains("\r"), !raw.contains("\0") else {
            return false
        }
        let pattern = #"^xynigo://(?:start/?|wake/?|pair\?code=[A-HJ-NP-Z2-9]{4}-?[A-HJ-NP-Z2-9]{4})$"#
        return raw.range(of: pattern, options: [.regularExpression, .caseInsensitive]) != nil
    }

    private func applicationDataDirectory() throws -> URL {
        let base = try FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let directory = base.appendingPathComponent(dataFolderName, isDirectory: true)
        try FileManager.default.createDirectory(
            at: directory,
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        return directory
    }

    private func writePrivateRequest(_ value: String, named name: String) throws {
        let target = try applicationDataDirectory().appendingPathComponent(name)
        try Data(value.utf8).write(to: target, options: .atomic)
        try FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: target.path)
    }

    private func openTerminalScript(named name: String) throws {
        guard let resourceURL = Bundle.main.resourceURL else {
            throw NSError(domain: "XynigoLauncher", code: 1)
        }
        let script = resourceURL.appendingPathComponent(name)
        guard FileManager.default.isExecutableFile(atPath: script.path) else {
            throw NSError(domain: "XynigoLauncher", code: 2)
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        process.arguments = ["-a", "Terminal", script.path]
        try process.run()
    }

    private func showInvalidRequest() {
        let alert = NSAlert()
        alert.messageText = "无效的 Xynigo 启动请求"
        alert.informativeText = "该请求未执行。请回到 Xynigo 云端工作台重试。"
        alert.runModal()
    }

    private func showLaunchError() {
        let alert = NSAlert()
        alert.messageText = "Xynigo 本地执行器未能启动"
        alert.informativeText = "请重新安装标准版，或联系管理员检查应用完整性。"
        alert.runModal()
    }
}

let application = NSApplication.shared
let delegate = XynigoAppDelegate()
application.delegate = delegate
application.setActivationPolicy(.accessory)
application.run()
