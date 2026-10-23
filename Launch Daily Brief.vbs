' Launch Daily Brief - opens today's brief, refreshing it first when it is stale.
' Windowless on purpose: no console flash, no terminal the user has to close.
Option Explicit

Dim sh, fso, home, pyw, scriptPath, htmlPath
Dim today, stamp, modified, cmd

Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

home       = fso.GetParentFolderName(WScript.ScriptFullName)
htmlPath   = fso.BuildPath(home, "index.html")
scriptPath = fso.BuildPath(home, "digest.py")

' Prefer a windowed interpreter, but do not assume a specific Python version:
' the py launcher is version-agnostic, then PATH, then the usual install dirs.
Function FindPythonw()
  Dim c, v, tries
  FindPythonw = ""

  ' 1) the py launcher's windowed twin, if installed
  For Each c In Array(sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Launcher\pyw.exe"), _
                      sh.ExpandEnvironmentStrings("%SystemRoot%\pyw.exe"))
    If fso.FileExists(c) Then FindPythonw = c : Exit Function
  Next

  ' 2) pythonw.exe on PATH
  For Each v In Split(sh.ExpandEnvironmentStrings("%PATH%"), ";")
    If Len(v) > 0 Then
      c = fso.BuildPath(v, "pythonw.exe")
      If fso.FileExists(c) Then FindPythonw = c : Exit Function
    End If
  Next

  ' 3) default per-user and machine install locations, newest first
  For Each v In Array("314", "313", "312", "311", "310", "39")
    c = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python" & v & "\pythonw.exe")
    If fso.FileExists(c) Then FindPythonw = c : Exit Function
    c = sh.ExpandEnvironmentStrings("%ProgramFiles%\Python" & v & "\pythonw.exe")
    If fso.FileExists(c) Then FindPythonw = c : Exit Function
  Next

  ' 4) give up and let the shell resolve it
  FindPythonw = "pythonw.exe"
End Function

pyw = FindPythonw()

Function DayStamp(d)
  DayStamp = Year(d) & "-" & Right("0" & Month(d), 2) & "-" & Right("0" & Day(d), 2)
End Function

today = DayStamp(Now)

' Case 1: no page at all - we have nothing to show, so build it first.
If Not fso.FileExists(htmlPath) Then
  cmd = """" & pyw & """ """ & scriptPath & """ --quiet --if-stale"
  sh.Run cmd, 0, True
  sh.Run """" & htmlPath & """", 1, False
  WScript.Quit
End If

' Case 2: the page is already today's - open it instantly.
modified = fso.GetFile(htmlPath).DateLastModified
If DayStamp(modified) = today Then
  sh.Run """" & htmlPath & """", 1, False
  WScript.Quit
End If

' Case 3: stale. Show what we have right away (instant, never a blank window)
' and regenerate in the background - the page reloads itself once the new
' file lands, so the wait never blocks the user.
sh.Run """" & htmlPath & """", 1, False
cmd = """" & pyw & """ """ & scriptPath & """ --quiet --if-stale"
sh.Run cmd, 0, False
