import AppKit
import Foundation
import WebKit

/// `VoiceClone --self-test <phase>` tests the server rules of the app with no window.
/// It needs VOICECLONE_PORT (not 7860) and VOICECLONE_REPO, and is run by macapp/test-desktop.sh.
/// Phases: reuse, own, timeout, died, fail10, fail11, and secure-context (loads the page of a stand-in
/// server in a WKWebView that has no window and checks that WebKit allows the microphone API there).
enum SelfTest {
    static func run(_ args: [String]) -> Int32 {
        let env = ProcessInfo.processInfo.environment
        guard let phase = args.first,
              let port = env["VOICECLONE_PORT"], Int(port) != nil, port != "7860",
              let repo = env["VOICECLONE_REPO"] else {
            FileHandle.standardError.write(Data("usage: VOICECLONE_PORT=<not 7860> VOICECLONE_REPO=<folder> VoiceClone --self-test reuse|own|timeout|died|fail10|fail11|secure-context\n".utf8))
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
