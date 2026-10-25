import AppKit

// `VoiceClone --self-test <phase>` runs the server rules without a window (see SelfTest.swift).
if CommandLine.arguments.count >= 2 && CommandLine.arguments[1] == "--self-test" {
    exit(SelfTest.run(Array(CommandLine.arguments.dropFirst(2))))
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
