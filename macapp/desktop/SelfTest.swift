import AppKit
import Foundation
import WebKit

/// `VoiceClone --self-test <phase>` tests the server rules of the app with no window.
/// It needs VOICECLONE_PORT (not 7860) and VOICECLONE_REPO, and is run by macapp/test-desktop.sh.
/// Phases: reuse, own, timeout, died, fail10, fail11, secure-context (loads the page of a stand-in
/// server in a WKWebView that has no window and checks that WebKit allows the microphone API there),
/// and sigterm, sigint, sighup and sigterm-twice (start a server, run the quit path of the app on a real
/// NSApplication run loop, send signals to this process, expect that it ends and stops the server).
enum SelfTest {
    static func run(_ args: [String]) -> Int32 {
        let env = ProcessInfo.processInfo.environment
        guard let phase = args.first,
              let port = env["VOICECLONE_PORT"], Int(port) != nil, port != "7860",
              let repo = env["VOICECLONE_REPO"] else {
            FileHandle.standardError.write(Data("usage: VOICECLONE_PORT=<not 7860> VOICECLONE_REPO=<folder> VoiceClone --self-test reuse|own|timeout|died|fail10|fail11|secure-context|sigterm|sigint|sighup|sigterm-twice\n".utf8))
            return 2
        }
        var fails: Int32 = 0
        func check(_ ok: Bool, _ what: String) {
            print(ok ? "ok    \(phase): \(what)" : "FAIL  \(phase): \(what)")
            if !ok { fails += 1 }
        }
        func wait(_ s: ServerController, while keep: (ServerController.Progress) -> Bool) -> ServerController.Progress {
            var state = s.progress()
            while keep(state) { Thread.sleep(forTimeInterval: 0.25); state = s.progress() }
            return state
        }
        func gone(_ pid: Int32) -> Bool { return kill(pid, 0) != 0 }

        switch phase {
        case "reuse":
            // A server that is not ours answers on the port.
            let s = ServerController(startTimeout: 5)
            check(s.isUp(), "the stand-in answers before begin")
            check(s.begin() == .running, "begin() reports running and starts nothing")
            check(s.ownedPid == nil, "the app owns no server")
            s.shutdown()
            check(s.isUp(), "shutdown() leaves the server it did not start running")

        case "own":
            let s = ServerController(startTimeout: 30)
            check(!s.isUp(), "the port is free before begin")
            let first = s.begin()
            guard case .started(let pid) = first else { check(false, "begin() starts the server, got \(first)"); return fails }
            check(s.ownedPid == pid, "the app owns pid \(pid)")
            check(commandLine(pid) == "\(repo)/.venv/bin/python app.py", "the process is '<repo>/.venv/bin/python app.py'")
            let state = wait(s) { $0 == .waiting }
            check(state == .up, "the server answers (\(state))")
            check(s.begin() == .running, "a second begin() starts nothing")
            check(s.ownedPid == pid, "the app still owns the same pid")
            s.shutdown()
            check(gone(pid), "shutdown() stops the server it started")
            check(!s.isUp(), "the port is free after shutdown")
            check(s.ownedPid == nil, "the app owns nothing after shutdown")

        case "timeout":
            // The server runs but never answers.
            let s = ServerController(startTimeout: 2)
            guard case .started(let pid) = s.begin() else { check(false, "begin() starts the server"); return fails }
            let state = wait(s) { $0 == .waiting }
            check(state == .timedOut, "no answer after the timeout gives timedOut (\(state))")
            s.shutdown()
            check(gone(pid), "shutdown() stops the server that never answered")

        case "died":
            // The server exits at once.
            let s = ServerController(startTimeout: 30)
            guard case .started(let pid) = s.begin() else { check(false, "begin() starts the server"); return fails }
            let state = wait(s) { $0 == .waiting }
            check(state == .died, "a server that exits gives died (\(state))")
            check(gone(pid), "the process is gone")
            s.shutdown()
            check(s.ownedPid == nil, "shutdown() clears the pid of a dead server")

        case "fail10", "fail11":
            let s = ServerController(startTimeout: 5)
            let code: Int32 = phase == "fail10" ? 10 : 11
            check(s.begin() == .failed(code: code), "begin() fails with code \(code)")
            check(s.ownedPid == nil, "the app owns no server")

        case "secure-context":
            // getUserMedia (the Record voice tab) only exists in a secure context. Is http://127.0.0.1 one?
            let s = ServerController(startTimeout: 5)
            let result = WebProbe.run(url: s.url)
            check(result.loaded, "the page of the stand-in loads in a WKWebView")
            check(result.secure, "window.isSecureContext is true at \(s.url.absoluteString)")
            check(result.hasMediaDevices, "navigator.mediaDevices.getUserMedia exists")

        case "sigterm":
            return QuitProbe.run(phase: phase, signals: [(0.3, SIGTERM)])
        case "sigint":
            return QuitProbe.run(phase: phase, signals: [(0.3, SIGINT)])
        case "sighup":
            return QuitProbe.run(phase: phase, signals: [(0.3, SIGHUP)])
        case "sigterm-twice":
            // The server needs about 1.5 s to stop, so the later signals arrive while the quit is running.
            // SIGINT follows SIGTERM at once, before the run loop can have started the quit.
            return QuitProbe.run(phase: phase, signals: [(0.3, SIGTERM), (0.0, SIGINT), (0.4, SIGTERM), (0.4, SIGHUP)])

        default:
            FileHandle.standardError.write(Data("unknown phase \(phase)\n".utf8))
            return 2
        }
        return min(fails, 255)
    }

    private static func commandLine(_ pid: Int32) -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/ps")
        process.arguments = ["-o", "command=", "-p", String(pid)]
        let pipe = Pipe()
        process.standardOutput = pipe
        do { try process.run() } catch { return "" }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (String(data: data, encoding: .utf8) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

/// A WKWebView with no window and no Dock icon: loads a URL and asks the page three questions.
private final class WebProbe: NSObject, WKNavigationDelegate {
    var loaded = false
    var finished = false
    var secure = false
    var hasMediaDevices = false

    static func run(url: URL) -> WebProbe {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        let probe = WebProbe()
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = WKWebsiteDataStore.nonPersistent()
        let view = WKWebView(frame: NSRect(x: 0, y: 0, width: 400, height: 300), configuration: configuration)
        view.navigationDelegate = probe
        view.load(URLRequest(url: url))
        let deadline = Date().addingTimeInterval(20)
        while !probe.finished && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.05)) }
        return probe
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loaded = true
        webView.evaluateJavaScript("[window.isSecureContext, !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)]") { value, _ in
            if let flags = value as? [Bool], flags.count == 2 { self.secure = flags[0]; self.hasMediaDevices = flags[1] }
            self.finished = true
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { finished = true }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { finished = true }
}

/// The quit path of the app without a window, on a real NSApplication run loop. It starts the server
/// (VOICECLONE_REPO/.venv/bin/python app.py on VOICECLONE_PORT), sends signals to its own process and
/// expects that the app ends by itself with the server stopped. The quit path is the QuitPath class
/// that AppDelegate uses. A signal handler that deadlocks shows up as the watchdog below: it fails the
/// phase when the process is still running `exitLimit` seconds after the last signal.
private final class QuitProbe: NSObject, NSApplicationDelegate {
    static let exitLimit = 6.0

    let phase: String
    let signals: [(delay: Double, signal: Int32)]   // delay in seconds after the previous signal (the first: after the server answers)
    let server = ServerController(startTimeout: 30)
    var pid: Int32 = 0
    var fails: Int32 = 0
    var firstSignalAt: Date?
    lazy var quitPath = QuitPath(server: server) {}

    init(phase: String, signals: [(delay: Double, signal: Int32)]) {
        self.phase = phase
        self.signals = signals
    }

    func check(_ ok: Bool, _ what: String) {
        print(ok ? "ok    \(phase): \(what)" : "FAIL  \(phase): \(what)")
        if !ok { fails += 1 }
    }

    static func run(phase: String, signals: [(delay: Double, signal: Int32)]) -> Int32 {
        setvbuf(stdout, nil, _IONBF, 0)   // the lines must survive a SIGKILL of this process
        let probe = QuitProbe(phase: phase, signals: signals)
        let server = probe.server
        probe.check(!server.isUp(), "the port is free before begin")
        guard case .started(let pid) = server.begin() else { probe.check(false, "begin() starts the server"); return probe.fails }
        probe.pid = pid
        print("server pid \(pid)")   // test-desktop.sh reads this line
        var state = server.progress()
        while state == .waiting { Thread.sleep(forTimeInterval: 0.25); state = server.progress() }
        probe.check(state == .up, "the server answers (\(state))")
        guard state == .up else { server.shutdown(); return probe.fails }

        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)   // no Dock icon, no window
        app.delegate = probe
        probe.quitPath.installSignalHandlers()
        probe.sendSignals()
        app.run()   // does not return: terminate ends the process in applicationWillTerminate
        return probe.fails
    }

    private func sendSignals() {
        let queue = DispatchQueue.global()
        var elapsed = 0.0
        for (delay, signal) in signals {
            elapsed += delay
            queue.asyncAfter(deadline: .now() + elapsed) {
                let name = [SIGTERM: "SIGTERM", SIGINT: "SIGINT", SIGHUP: "SIGHUP"][signal] ?? String(signal)
                print("      \(self.phase): sending \(name) to this process")
                if self.firstSignalAt == nil { self.firstSignalAt = Date() }
                kill(getpid(), signal)
            }
        }
        queue.asyncAfter(deadline: .now() + elapsed + QuitProbe.exitLimit) {
            self.check(false, "the process still runs \(Int(QuitProbe.exitLimit)) s after the last signal: the quit path is stuck")
            self.server.queue.sync { self.server.shutdown() }
            _exit(1)
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        return quitPath.shouldTerminate()
    }

    /// The last call before the process ends: the server must be stopped by now.
    func applicationWillTerminate(_ notification: Notification) {
        check(server.ownedPid == nil, "the app owns no server when it ends")
        check(kill(pid, 0) != 0, "the server it started (pid \(pid)) is gone when it ends")
        check(!server.isUp(), "the port is free when it ends")
        if let start = firstSignalAt { print(String(format: "      %@: the app ended %.1f s after the first signal", phase, Date().timeIntervalSince(start))) }
        exit(min(fails, 255))
    }
}
