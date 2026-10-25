-- Voice Clone launcher. A stay-open AppleScript applet, compiled by make-app.sh.
-- All process work is done by the helper "voiceclone.sh" in the bundle's Resources folder.
--
-- States: "idle" (nothing started by this applet), "starting" (server started, waiting for the URL),
-- "running" (server started by this applet answers). serverPid is set only for a server this
-- applet started, so Quit never stops a server it did not start.

property serverPid : ""
property appState : "idle"
property startedAt : missing value
property startTimeout : 90
property dialogTitle : "Voice Clone"

on run
	set serverPid to ""
	set appState to "idle"
	openOrStart()
end run

-- Clicking the app again in the Dock or in Launchpad.
on reopen
	openOrStart()
end reopen

-- Runs every second while the server starts, every 3 seconds otherwise.
on idle
	if appState is "starting" then
		if serverUp() then
			set appState to "running"
			openServerURL()
		else if not serverAlive() then
			set serverPid to ""
			set appState to "idle"
			failDialog("The server stopped before it was ready.")
		else if ((current date) - startedAt) > startTimeout then
			stopServer()
			failDialog("The server did not answer within " & startTimeout & " seconds. It was stopped.")
		end if
		return 1
	end if
	return 3
end idle

-- Cmd-Q and Dock > Quit.
on quit
	stopServer()
	continue quit
end quit

on openOrStart()
	if appState is "starting" then return
	-- A server this applet started earlier no longer answers: stop it so a new one can take the port.
	if serverPid is not "" and not serverUp() then stopServer()
	try
		set reply to runHelper("begin")
	on error errMsg number errNum
		startFailed(errNum)
		return
	end try
	if reply is "running" then
		openServerURL()
	else if reply starts with "started " then
		set serverPid to text 9 thru -1 of reply
		set startedAt to current date
		set appState to "starting"
	else
		failDialog("Unexpected reply from the helper: " & reply)
	end if
end openOrStart

on helperCommand(args)
	set helperPath to POSIX path of (path to resource "voiceclone.sh")
	return "/bin/bash " & quoted form of helperPath & " " & args
end helperCommand

on runHelper(args)
	return do shell script helperCommand(args)
end runHelper

on serverUp()
	try
		runHelper("check")
		return true
	on error
		return false
	end try
end serverUp

on serverAlive()
	if serverPid is "" then return false
	try
		runHelper("alive " & serverPid)
		return true
	on error
		return false
	end try
end serverAlive

on stopServer()
	if serverPid is not "" then
		try
			runHelper("stop " & serverPid)
		end try
	end if
	set serverPid to ""
	set appState to "idle"
end stopServer

on openServerURL()
	open location runHelper("url")
end openServerURL

on startFailed(errNum)
	set repo to runHelper("repo")
	if errNum is 10 then
		activate
		display dialog "The Voice Clone folder was not found:" & return & repo & return & return & "The app points to that folder. Put the folder back, or run" & return & "bash make-app.sh --install" & return & "from its new place." buttons {"OK"} default button "OK" with title dialogTitle with icon caution
	else if errNum is 11 then
		set setupCommands to "cd " & quoted form of repo & " && uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -r requirements.txt"
		activate
		set answer to display dialog "The Python environment is missing in:" & return & repo & return & return & "Set it up once in Terminal:" & return & "cd " & quoted form of repo & return & "uv venv --python 3.11 .venv" & return & "uv pip install --python .venv/bin/python -r requirements.txt" buttons {"Copy Commands", "OK"} default button "OK" with title dialogTitle with icon caution
		if button returned of answer is "Copy Commands" then set the clipboard to setupCommands
	else
		failDialog("The server could not be started (code " & errNum & ").")
	end if
end startFailed

on failDialog(message)
	set logFile to runHelper("logpath")
	activate
	set answer to display dialog message & return & return & "Log file:" & return & logFile buttons {"Open Log", "OK"} default button "OK" with title dialogTitle with icon caution
	if button returned of answer is "Open Log" then
		try
			do shell script "/usr/bin/open -t " & quoted form of logFile
		end try
	end if
end failDialog
