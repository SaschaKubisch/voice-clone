import Foundation

/// The rules for the web server of the desktop app. The rules themselves live in voiceclone.sh, the
/// helper the browser launcher uses too (copied into Contents/Resources by make-app.sh). This class
/// calls the helper, remembers the pid of a server this app started, and stops only that pid.
///
/// All methods are meant to run on `queue`, one at a time. `ownedPid` is set only by `begin()` when
/// it started a server. A server that already answered is never owned and never stopped.
final class ServerController {
    enum Begin: Equatable {
        case running                   // the URL already answered: use it, own nothing
        case started(pid: Int32)       // this app started the server and owns it
        case failed(code: Int32)       // helper exit code: 10 repo folder missing, 11 .venv missing, 12 other
    }

    enum Progress: Equatable {
        case up                        // the URL answers
        case waiting                   // the server we started is alive but does not answer yet
        case died                      // nothing answers and the server we started is gone
        case timedOut                  // still no answer after `startTimeout` seconds
    }

    let queue = DispatchQueue(label: "local.voiceclone.server")
    let helperPath: String
    let startTimeout: TimeInterval
    let url: URL
    let logPath: String
    let repo: String
    private(set) var ownedPid: Int32?
    private var startedAt: Date?

    init(startTimeout: TimeInterval = 90) {
        let env = ProcessInfo.processInfo.environment
        let helper = env["VOICECLONE_HELPER"] ?? ((Bundle.main.resourcePath ?? ".") + "/voiceclone.sh")
        self.helperPath = helper
        self.startTimeout = startTimeout
        let urlText = ServerController.run(helper, ["url"]).output
        self.url = URL(string: urlText) ?? URL(string: "http://127.0.0.1:7860/")!
        let log = ServerController.run(helper, ["logpath"]).output
        self.logPath = log.isEmpty ? NSString(string: "~/Library/Logs/VoiceClone/app.log").expandingTildeInPath : log
        self.repo = ServerController.run(helper, ["repo"]).output
    }

    /// Runs the helper once. Returns its exit status and its output without the final newline.
    static func run(_ helper: String, _ args: [String]) -> (status: Int32, output: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [helper] + args
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice
        do { try process.run() } catch { return (127, "") }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let text = String(data: data, encoding: .utf8) ?? ""
        return (process.terminationStatus, text.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    private func helper(_ args: String...) -> (status: Int32, output: String) {
        return ServerController.run(helperPath, args)
    }

    func isUp() -> Bool {
        return helper("check").status == 0
    }

    /// If the URL answers, use it. Otherwise start the server.
    func begin() -> Begin {
        // A server this app started earlier no longer answers: stop it so a new one can take the port.
        if ownedPid != nil && !isUp() { shutdown() }
        let reply = helper("begin")
        if reply.status == 0 {
            if reply.output == "running" { return .running }
            if reply.output.hasPrefix("started "), let pid = Int32(reply.output.dropFirst("started ".count)) {
                ownedPid = pid
                startedAt = Date()
                return .started(pid: pid)
            }
        }
        return .failed(code: reply.status == 0 ? 12 : reply.status)
    }

    /// One look at a server that is starting.
    func progress(now: Date = Date()) -> Progress {
        if isUp() { return .up }
        guard let pid = ownedPid, let started = startedAt else { return .died }
        if helper("alive", String(pid)).status != 0 { return .died }
        if now.timeIntervalSince(started) > startTimeout { return .timedOut }
        return .waiting
    }

    /// Stops the server this app started and nothing else. The helper checks the exact command line
    /// "<repo>/.venv/bin/python app.py" again before it sends a signal.
    func shutdown() {
        guard let pid = ownedPid else { return }
        _ = helper("stop", String(pid))
        ownedPid = nil
        startedAt = nil
    }
}
