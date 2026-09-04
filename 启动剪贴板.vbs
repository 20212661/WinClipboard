' WinClipboard silent launcher
' Auto-selects the entry point: prefers the Qt + Fluent version (winclipboard_qt.py);
' falls back to the tkinter version (clipboard_manager.py) when PySide6 is missing.
'
' Interpreter resolution order:
'   1. python.txt next to this script: one line, full path of pythonw.exe (explicit override)
'   2. hermes venv interpreter (known-good on this machine)
'   3. pythonw.exe found on PATH
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

' 2) hermes venv (known-good on this machine)
If pyw = "" Then
  Dim candidates, c
  candidates = Array( _
    "C:\Users\haoshangyang\AppData\Local\hermes\hermes-agent\venv\Scripts\pythonw.exe", _
    "C:\Users\haoshangyang\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe")
  For Each c In candidates
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
         "pythonw.exe that has pywin32 and Pillow installed, or put pythonw.exe on PATH.", _
         16, "WinClipboard launch failed"
  WScript.Quit 1
End If

' Probe Qt dependencies without a console window; exit code 0 means available.
Dim probe, rc, script
probe = Chr(34) & pyw & Chr(34) & " -c " & Chr(34) & "import PySide6, qfluentwidgets" & Chr(34)
rc = sh.Run(probe, 0, True)

If rc = 0 Then
  script = dir & "\winclipboard_qt.py"
Else
  script = dir & "\clipboard_manager.py"
End If

' window style 0 hides the console when the resolved interpreter is python.exe
sh.Run Chr(34) & pyw & Chr(34) & " " & Chr(34) & script & Chr(34), 0, False
