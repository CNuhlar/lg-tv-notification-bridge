import AppKit
import Foundation

let appName = "LG TV Bridge"
let workspace = Bundle.main.bundleURL.deletingLastPathComponent()
let bridgeScript = workspace.appendingPathComponent("mac_notifications_to_tv.py")
let logFile = workspace.appendingPathComponent("mac-notification-bridge.log")
let launchAgentLabel = "local.lgtv.notificationbridge.menu"
let launchAgentURL = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/LaunchAgents/\(launchAgentLabel).plist")

final class BridgeController: NSObject, NSApplicationDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let menu = NSMenu()
    private let statusMenuItem = NSMenuItem(title: "Forwarding: Stopped", action: nil, keyEquivalent: "")
    private let toggleMenuItem = NSMenuItem(title: "Enable Notification Forwarding", action: #selector(toggleForwarding), keyEquivalent: "")
    private let launchMenuItem = NSMenuItem(title: "Start at Login: Enabled", action: #selector(toggleLaunchAtLogin), keyEquivalent: "")
    private var process: Process?
    private var shouldRun = true
    private var lastError: String?
    private var restartTimer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupStatusItem()
        setupMenu()
        shouldRun = UserDefaults.standard.object(forKey: "forwardingEnabled") as? Bool ?? true
        updateLaunchMenu()
        if shouldRun {
            startBridge()
        }
        restartTimer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
            guard let self else { return }
            if self.shouldRun && self.process?.isRunning != true {
                self.startBridge()
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopBridge()
    }

    private func setupStatusItem() {
        if let button = statusItem.button {
            if let image = NSImage(systemSymbolName: "tv", accessibilityDescription: appName) {
                image.isTemplate = true
                button.image = image
            } else {
                button.title = "TV"
            }
        }
        statusItem.menu = menu
    }

    private func setupMenu() {
        toggleMenuItem.target = self
        launchMenuItem.target = self

        let restartItem = NSMenuItem(title: "Restart Bridge", action: #selector(restartBridge), keyEquivalent: "r")
        restartItem.target = self

        let openLogItem = NSMenuItem(title: "Open Log", action: #selector(openLog), keyEquivalent: "l")
        openLogItem.target = self

        let openFolderItem = NSMenuItem(title: "Open Project Folder", action: #selector(openFolder), keyEquivalent: "")
        openFolderItem.target = self

        let quitItem = NSMenuItem(title: "Quit", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self

        menu.addItem(statusMenuItem)
        menu.addItem(toggleMenuItem)
        menu.addItem(restartItem)
        menu.addItem(NSMenuItem.separator())
        menu.addItem(launchMenuItem)
        menu.addItem(NSMenuItem.separator())
        menu.addItem(openLogItem)
        menu.addItem(openFolderItem)
        menu.addItem(NSMenuItem.separator())
        menu.addItem(quitItem)
        updateMenu()
    }

    @objc private func toggleForwarding() {
        shouldRun.toggle()
        UserDefaults.standard.set(shouldRun, forKey: "forwardingEnabled")
        if shouldRun {
            startBridge()
        } else {
            stopBridge()
        }
        updateMenu()
    }

    @objc private func restartBridge() {
        stopBridge()
        if shouldRun {
            startBridge()
        }
        updateMenu()
    }

    @objc private func openLog() {
        NSWorkspace.shared.open(logFile)
    }

    @objc private func openFolder() {
        NSWorkspace.shared.open(workspace)
    }

    @objc private func toggleLaunchAtLogin() {
        if FileManager.default.fileExists(atPath: launchAgentURL.path) {
            try? FileManager.default.removeItem(at: launchAgentURL)
        } else {
            installLaunchAgent()
        }
        updateLaunchMenu()
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    private func startBridge() {
        stopBridge()
        guard FileManager.default.fileExists(atPath: bridgeScript.path) else {
            statusMenuItem.title = "Forwarding: Script missing"
            return
        }

        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        task.arguments = ["python3", bridgeScript.path]
        task.currentDirectoryURL = workspace

        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        task.environment = env

        let logHandle = try? FileHandle(forWritingTo: logFile)
        logHandle?.seekToEndOfFile()
        task.standardOutput = logHandle
        task.standardError = logHandle
        task.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                if task.terminationStatus == 13 {
                    self?.lastError = "Needs Full Disk Access"
                    self?.shouldRun = false
                    UserDefaults.standard.set(false, forKey: "forwardingEnabled")
                }
                self?.updateMenu()
            }
        }

        do {
            lastError = nil
            try task.run()
            process = task
        } catch {
            appendLog("menu app failed to start bridge: \(error)")
        }
        updateMenu()
    }

    private func stopBridge() {
        if let process, process.isRunning {
            process.terminate()
            DispatchQueue.global().async {
                Thread.sleep(forTimeInterval: 1)
                if process.isRunning {
                    process.interrupt()
                }
            }
        }
        process = nil
        updateMenu()
    }

    private func updateMenu() {
        let running = process?.isRunning == true
        if let lastError, !shouldRun {
            statusMenuItem.title = "Forwarding: \(lastError)"
            toggleMenuItem.title = "Enable Notification Forwarding"
            return
        }
        if shouldRun {
            statusMenuItem.title = running ? "Forwarding: Enabled" : "Forwarding: Starting..."
            toggleMenuItem.title = "Disable Notification Forwarding"
        } else {
            statusMenuItem.title = "Forwarding: Disabled"
            toggleMenuItem.title = "Enable Notification Forwarding"
        }
    }

    private func updateLaunchMenu() {
        let enabled = FileManager.default.fileExists(atPath: launchAgentURL.path)
        launchMenuItem.title = enabled ? "Start at Login: Enabled" : "Start at Login: Disabled"
    }

    private func installLaunchAgent() {
        let appPath = Bundle.main.bundlePath
        let plist = """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
          <key>Label</key>
          <string>\(launchAgentLabel)</string>
          <key>ProgramArguments</key>
          <array>
            <string>/usr/bin/open</string>
            <string>\(appPath)</string>
          </array>
          <key>RunAtLoad</key>
          <true/>
        </dict>
        </plist>
        """
        do {
            try FileManager.default.createDirectory(at: launchAgentURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            try plist.write(to: launchAgentURL, atomically: true, encoding: .utf8)
        } catch {
            appendLog("failed to install launch agent: \(error)")
        }
    }

    private func appendLog(_ message: String) {
        let line = "[menu] \(Date()) \(message)\n"
        if let data = line.data(using: .utf8) {
            if FileManager.default.fileExists(atPath: logFile.path),
               let handle = try? FileHandle(forWritingTo: logFile) {
                handle.seekToEndOfFile()
                try? handle.write(contentsOf: data)
                try? handle.close()
            } else {
                try? data.write(to: logFile)
            }
        }
    }
}

let app = NSApplication.shared
let delegate = BridgeController()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
