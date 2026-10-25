import AppKit

/// How the app ends. Cmd-Q, closing the window, logout and the signals SIGTERM, SIGINT and SIGHUP all
/// end up in `shouldTerminate()`: the window goes away first, then the server this app started is
/// stopped (SIGTERM, SIGKILL after 10 s) and only then the app ends.
/// AppDelegate and the window-free self-test (SelfTest.swift, phases sigterm, sigint, sighup and
/// sigterm-twice) both use this class, so the test runs the code the app runs.
final class QuitPath {
    private let server: ServerController
    private let beforeStop: () -> Void
    private(set) var terminating = false
    private var signalSources: [DispatchSourceSignal] = []

    /// `beforeStop` runs once on the main thread when the quit starts, before the server is stopped.
    init(server: ServerController, beforeStop: @escaping () -> Void) {
        self.server = server
        self.beforeStop = beforeStop
    }

    /// The answer to applicationShouldTerminate. Always `.terminateLater`: the app ends when the
    /// server is stopped and the reply is sent. Asked again while a quit is running, it changes nothing.
    func shouldTerminate() -> NSApplication.TerminateReply {
        if terminating { return .terminateLater }
        terminating = true
        beforeStop()
        server.queue.async {
            self.server.shutdown()
            DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    func installSignalHandlers() {
        for sig in [SIGTERM, SIGINT, SIGHUP] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { [weak self] in self?.requestQuit() }
            source.resume()
            signalSources.append(source)
        }
    }

    /// Runs in the signal handler, which is a block on the main dispatch queue. `terminate:` must not
    /// run inside that block. With `.terminateLater` it waits in a nested run loop until the reply
    /// arrives, and the reply is a block on the same main queue. The main queue is serial: it does not
    /// start the next block while the current one has not returned, so the reply never ran and the
    /// app never ended. Cmd-Q works because it arrives as an event, outside the main queue.
    /// So the quit is handed to the run loop (`RunLoop.perform`, which is not a dispatch block), in all
    /// common modes so that it also runs while a menu is open or a sheet is shown. A signal that
    /// arrives while a quit is running does nothing.
    private func requestQuit() {
        RunLoop.main.perform(inModes: [.common]) { [weak self] in
            guard let self = self, !self.terminating else { return }
            NSApp.terminate(nil)
        }
    }
}
