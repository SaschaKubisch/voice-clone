import AppKit
import WebKit

/// The window of the desktop app: one WKWebView that shows the Gradio page of the local server.
/// Server start, polling and stop go through ServerController, always on its queue.
final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    private enum Phase { case starting, ready, failed }

    private struct Failure {
        let message: String
        var setupCommands: String? = nil   // offered by "Copy Commands"
    }

    private let server = ServerController()
    private var window: NSWindow!
    private var webView: WKWebView!
    private var phase = Phase.starting
    private var pollTimer: Timer?
    private var polling = false
    private var terminating = false
    private var signalSources: [DispatchSourceSignal] = []
    private var downloadTargets: [ObjectIdentifier: URL] = [:]

    // MARK: Launch and quit

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        installSignalHandlers()
        NSApp.activate(ignoringOtherApps: true)
        startServer()
    }

    /// The app quits when its window closes.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    /// Cmd-Q, closing the window, SIGTERM and logout end up here. The window goes away first, then
    /// the server this app started is stopped (SIGTERM, SIGKILL after 10 s) and only then the app ends.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if terminating { return .terminateLater }
        terminating = true
        pollTimer?.invalidate()
        window?.orderOut(nil)
        server.queue.async {
            self.server.shutdown()
            DispatchQueue.main.async { NSApp.reply(toApplicationShouldTerminate: true) }
        }
        return .terminateLater
    }

    private func installSignalHandlers() {
        for sig in [SIGTERM, SIGINT, SIGHUP] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { NSApp.terminate(nil) }
            source.resume()
            signalSources.append(source)
        }
    }

    // MARK: Window

    private func buildWindow() {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = WKWebsiteDataStore.nonPersistent()   // nothing is kept between runs
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self

        var size = NSSize(width: 1200, height: 900)
        if let visible = NSScreen.main?.visibleFrame {
            size = NSSize(width: min(size.width, visible.width - 40), height: min(size.height, visible.height - 80))
        }
        window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Voice Clone"
        window.isReleasedWhenClosed = false
        window.contentMinSize = NSSize(width: 640, height: 480)
        window.delegate = self
        window.contentView = webView
        window.center()
        window.makeKeyAndOrderFront(nil)
    }

    // MARK: Menu

    private func buildMenu() {
        let mainMenu = NSMenu()
        func submenu(_ title: String) -> NSMenu {
            let item = NSMenuItem()
            mainMenu.addItem(item)
            let menu = NSMenu(title: title)
            item.submenu = menu
            return menu
        }
        func add(_ menu: NSMenu, _ title: String, _ action: Selector?, _ key: String = "",
                 _ modifiers: NSEvent.ModifierFlags = .command, target: AnyObject? = nil) {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
            item.keyEquivalentModifierMask = modifiers
            item.target = target
            menu.addItem(item)
        }

        let app = submenu("Voice Clone")
        add(app, "About Voice Clone", #selector(NSApplication.orderFrontStandardAboutPanel(_:)))
        app.addItem(.separator())
        add(app, "Hide Voice Clone", #selector(NSApplication.hide(_:)), "h")
        add(app, "Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option])
        add(app, "Show All", #selector(NSApplication.unhideAllApplications(_:)))
        app.addItem(.separator())
        add(app, "Quit Voice Clone", #selector(NSApplication.terminate(_:)), "q")

        let file = submenu("File")
        add(file, "Close", #selector(NSWindow.performClose(_:)), "w")

        let edit = submenu("Edit")
        add(edit, "Undo", Selector(("undo:")), "z")
        add(edit, "Redo", Selector(("redo:")), "z", [.command, .shift])
        edit.addItem(.separator())
        add(edit, "Cut", #selector(NSText.cut(_:)), "x")
        add(edit, "Copy", #selector(NSText.copy(_:)), "c")
        add(edit, "Paste", #selector(NSText.paste(_:)), "v")
        add(edit, "Select All", #selector(NSText.selectAll(_:)), "a")

        let view = submenu("View")
        add(view, "Reload", #selector(reload(_:)), "r", target: self)
        add(view, "Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control])

        let windowMenu = submenu("Window")
        add(windowMenu, "Minimize", #selector(NSWindow.performMiniaturize(_:)), "m")
        add(windowMenu, "Zoom", #selector(NSWindow.performZoom(_:)))

        NSApp.mainMenu = mainMenu
        NSApp.windowsMenu = windowMenu
    }

    /// Cmd-R: reload the page, or try to start the server again after a failure.
    @objc private func reload(_ sender: Any?) {
        switch phase {
        case .ready: loadServerPage()
        case .failed: startServer()
        case .starting: break
        }
    }

    // MARK: Server start

    private func startServer() {
        phase = .starting
        server.queue.async {
            let result = self.server.begin()
            DispatchQueue.main.async { self.didBegin(result) }
        }
    }

    private func didBegin(_ result: ServerController.Begin) {
        switch result {
        case .running:
            loadServerPage()
        case .started:
            showPage(title: "Starting Voice Clone...", detail: "The window shows the page as soon as the server answers.", spinner: true)
            pollTimer?.invalidate()
            pollTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in self?.pollOnce() }
        case .failed(let code):
            fail(failure(forCode: code))
        }
    }

    private func pollOnce() {
        if polling || phase != .starting { return }
        polling = true
        server.queue.async {
            let progress = self.server.progress()
            DispatchQueue.main.async {
                self.polling = false
                self.didPoll(progress)
            }
        }
    }

    private func didPoll(_ progress: ServerController.Progress) {
        guard phase == .starting else { return }
        switch progress {
        case .waiting:
            break
        case .up:
            pollTimer?.invalidate()
            loadServerPage()
        case .died:
            pollTimer?.invalidate()
            server.queue.async { self.server.shutdown() }
            fail(Failure(message: "The server stopped before it was ready."))
        case .timedOut:
            pollTimer?.invalidate()
            server.queue.async { self.server.shutdown() }
            fail(Failure(message: "The server did not answer within \(Int(server.startTimeout)) seconds. It was stopped."))
        }
    }

    private func loadServerPage() {
        phase = .ready
        webView.load(URLRequest(url: server.url))
    }

    private func failure(forCode code: Int32) -> Failure {
        let repo = server.repo
        switch code {
        case 10:
            return Failure(message: "The Voice Clone folder was not found:\n\(repo)\n\nThe app points to that folder. Put the folder back, or run\nbash make-app.sh --install\nfrom its new place.")
        case 11:
            let quoted = "'" + repo.replacingOccurrences(of: "'", with: "'\\''") + "'"
            let commands = "cd \(quoted) && uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt"
            return Failure(message: "The Python environment is missing in:\n\(repo)\n\nSet it up once in Terminal:\ncd \(quoted)\nuv venv --python 3.11 .venv\nuv pip install --python .venv/bin/python -r requirements.txt",
                           setupCommands: commands)
        default:
            return Failure(message: "The server could not be started (code \(code)).")
        }
    }

    /// Shows the failure in the window and in an alert that names the log file. Cmd-R tries again.
    private func fail(_ failure: Failure) {
        phase = .failed
        let log = NSString(string: server.logPath).abbreviatingWithTildeInPath
        showPage(title: "Voice Clone did not start",
                 detail: failure.message + "\n\nLog file:\n" + log + "\n\nPress Cmd-R to try again.", spinner: false)
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Voice Clone did not start"
        alert.informativeText = failure.message + "\n\nLog file:\n" + log
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: failure.setupCommands == nil ? "Open Log" : "Copy Commands")
        alert.beginSheetModal(for: window) { response in
            guard response == .alertSecondButtonReturn else { return }
            if let commands = failure.setupCommands {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(commands, forType: .string)
            } else {
                let open = Process()
                open.executableURL = URL(fileURLWithPath: "/usr/bin/open")
                open.arguments = ["-t", self.server.logPath]
                try? open.run()
            }
        }
    }

    private func showPage(title: String, detail: String, spinner: Bool) {
        func escape(_ text: String) -> String {
            return text.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
                .replacingOccurrences(of: ">", with: "&gt;").replacingOccurrences(of: "\n", with: "<br>")
        }
        let html = """
        <!doctype html><html><head><meta charset="utf-8"><style>
        :root { color-scheme: light dark; }
        body { margin: 0; height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center;
               font: 15px -apple-system, sans-serif; text-align: center; }
        h1 { font-size: 22px; font-weight: 600; margin: 18px 0 8px; }
        p { max-width: 560px; margin: 0; line-height: 1.5; opacity: 0.7; }
        .spin { width: 28px; height: 28px; border: 3px solid rgba(128,128,128,0.3); border-top-color: #ff7a59;
                border-radius: 50%; animation: s 0.9s linear infinite; }
        @keyframes s { to { transform: rotate(360deg); } }
        </style></head><body>
        \(spinner ? "<div class=\"spin\"></div>" : "")
        <h1>\(escape(title))</h1><p>\(escape(detail))</p></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // MARK: Which addresses stay in the window

    private func isServerURL(_ url: URL) -> Bool {
        return url.scheme == "http" && url.host == server.url.host && url.port == server.url.port
    }

    private func openInBrowser(_ url: URL) {
        if ["http", "https", "mailto"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
    }

    // MARK: WKNavigationDelegate

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.shouldPerformDownload { decisionHandler(.download); return }
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        let scheme = url.scheme ?? ""
        if ["about", "blob", "data"].contains(scheme) && navigationAction.targetFrame != nil {
            decisionHandler(.allow)
        } else if navigationAction.targetFrame == nil {
            openInBrowser(url)                         // a link that wants a new window
            decisionHandler(.cancel)
        } else if isServerURL(url) || navigationAction.targetFrame?.isMainFrame == false {
            decisionHandler(.allow)
        } else {
            openInBrowser(url)                         // the main frame would leave 127.0.0.1
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let disposition = (navigationResponse.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        if navigationResponse.isForMainFrame && (disposition.lowercased().hasPrefix("attachment") || !navigationResponse.canShowMIMEType) {
            decisionHandler(.download)
        } else {
            decisionHandler(.allow)
        }
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        navigationFailed(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        navigationFailed(error)
    }

    /// The server went away after it had answered: say so in the window. Cmd-R starts it again.
    private func navigationFailed(_ error: Error) {
        let nsError = error as NSError
        let unreachable = [NSURLErrorCannotConnectToHost, NSURLErrorNetworkConnectionLost, NSURLErrorTimedOut, NSURLErrorCannotFindHost]
        guard phase == .ready, nsError.domain == NSURLErrorDomain, unreachable.contains(nsError.code) else { return }
        fail(Failure(message: "The server does not answer."))
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        if phase == .ready { loadServerPage() }
    }

    // MARK: WKUIDelegate

    /// A page that asks for a new window (target="_blank", window.open): open it in the default browser.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { openInBrowser(url) }
        return nil
    }

    /// The microphone for the page of this server (the Record voice tab). Nothing else is granted.
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        let ours = origin.protocol == "http" && origin.host == server.url.host && origin.port == (server.url.port ?? 80)
        decisionHandler(ours && type == .microphone ? .grant : .deny)
    }

    /// File inputs (the upload buttons).
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.canChooseFiles = true
        panel.beginSheetModal(for: window) { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { response in completionHandler(response == .alertFirstButtonReturn) }
    }

    // MARK: WKDownloadDelegate

    /// Downloads (the download button of the audio player) go to ~/Downloads and are shown in Finder.
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let folder = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory() + "/Downloads")
        var name = (suggestedFilename as NSString).lastPathComponent
        if name.isEmpty || name == "." || name == ".." { name = "download" }
        let base = (name as NSString).deletingPathExtension
        let ext = (name as NSString).pathExtension
        var target = folder.appendingPathComponent(name)
        var n = 2
        while FileManager.default.fileExists(atPath: target.path) {
            target = folder.appendingPathComponent(ext.isEmpty ? "\(base) \(n)" : "\(base) \(n).\(ext)")
            n += 1
        }
        downloadTargets[ObjectIdentifier(download)] = target
        completionHandler(target)
    }

    func downloadDidFinish(_ download: WKDownload) {
        if let target = downloadTargets.removeValue(forKey: ObjectIdentifier(download)) {
            NSWorkspace.shared.activateFileViewerSelecting([target])
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        downloadTargets.removeValue(forKey: ObjectIdentifier(download))
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "The download failed"
        alert.informativeText = error.localizedDescription
        alert.beginSheetModal(for: window, completionHandler: nil)
    }
}
