' WinClipboard silent launcher
' Auto-selects the entry point: prefers the Qt + Fluent version (winclipboard_qt.py);
' requires PySide6 + PySide6-Fluent-Widgets (single Qt UI).
'
' Interpreter resolution order:
'   1. python.txt next to this script: one line, full path of pythonw.exe (explicit override)
'   2. .venv next to this script - the project's OWN environment (preferred)
'   3. pythonw.exe found on PATH
' No hardcoded machine-specific fallbacks: this file must stay portable.
'
' NOTE: keep this file PURE ASCII! VBScript reads .vbs as ANSI/GBK on Chinese
' Windows; UTF-8 Chinese text next to a quote can swallow the closing quote
' and break the whole script ("Unterminated string constant").
Option Explicit
Dim fso, sh
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
Dim dir
dir = fso.GetParentFolderName(WScript.ScriptFullName)

Dim pyw, cfgPath, ts, line
pyw = ""

' 1) explicit override file
cfgPath = dir & "\python.txt"
If fso.FileExists(cfgPath) Then
  On Error Resume Next
  Set ts = fso.OpenTextFile(cfgPath, 1)
  If Err.Number = 0 Then
    Do Until ts.AtEndOfStream
      line = Trim(Replace(ts.ReadLine, vbCr, ""))
      If line <> "" Then
        pyw = line
        Exit Do
      End If
    Loop
    ts.Close
  End If
  On Error GoTo 0
End If
If pyw <> "" Then
  If Not fso.FileExists(pyw) Then pyw = ""
End If

' 2) project-local venv (relative path: no hardcoded user, no encoding issues)
If pyw = "" Then
  Dim localCandidates, c
  localCandidates = Array( _
    dir & "\.venv\Scripts\pythonw.exe", _
    dir & "\.venv\Scripts\python.exe")
  For Each c In localCandidates
    If fso.FileExists(c) Then
      pyw = c
      Exit For
    End If
  Next
End If

' 3) pythonw on PATH
If pyw = "" Then
  On Error Resume Next
  Dim eo
  Set eo = sh.Exec("cmd /c where pythonw.exe")
  If Not (eo Is Nothing) Then
    Do Until eo.StdOut.AtEndOfStream
      line = Trim(eo.StdOut.ReadLine)
      If line <> "" Then pyw = line : Exit Do
    Loop
  End If
  On Error GoTo 0
End If

If pyw = "" Or Not fso.FileExists(pyw) Then
  MsgBox "pythonw.exe not found." & vbCrLf & vbCrLf & _
         "Create python.txt next to the launcher containing the full path of a" & vbCrLf & _
         "pythonw.exe that has PySide6, PySide6-Fluent-Widgets, pywin32 and Pillow" & vbCrLf & _
         "installed, or put pythonw.exe on PATH.", _
         16, "WinClipboard launch failed"
  WScript.Quit 1
End If

Dim script
script = dir & "\winclipboard_qt.py"

' window style 0 hides the console when the resolved interpreter is python.exe
sh.Run Chr(34) & pyw & Chr(34) & " " & Chr(34) & script & Chr(34), 0, False
