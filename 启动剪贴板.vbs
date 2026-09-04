' WinClipboard silent launcher
' Auto-selects the entry point: prefers the Qt + Fluent version (winclipboard_qt.py);
' falls back to the tkinter version (clipboard_manager.py) when PySide6 is missing.
' Launches with pythonw.exe (no console window).
'
' NOTE: keep this file PURE ASCII! VBScript reads .vbs as ANSI/GBK on Chinese
' Windows; UTF-8 Chinese text next to a quote can swallow the closing quote
' and break the whole script ("Unterminated string constant").
Option Explicit
Dim fso, sh, pyw, script
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
Dim dir
dir = fso.GetParentFolderName(WScript.ScriptFullName)

Dim candidates
candidates = Array( _
  "C:\Users\haoshangyang\AppData\Local\hermes\hermes-agent\venv\Scripts\pythonw.exe", _
  "C:\Users\haoshangyang\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe" _
)

pyw = ""
Dim c
For Each c In candidates
  If fso.FileExists(c) Then
    pyw = c
    Exit For
  End If
Next

' Fallback: try pythonw on PATH
If pyw = "" Then
  On Error Resume Next
  Dim eo, line
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
  MsgBox "pythonw.exe not found." & vbCrLf & _
         "Please make sure Python with pywin32 and Pillow is installed (hermes venv).", _
         16, "WinClipboard launch failed"
  WScript.Quit 1
End If

' Probe Qt dependencies without a console window; exit code 0 means available.
Dim probe, rc
probe = Chr(34) & pyw & Chr(34) & " -c " & Chr(34) & "import PySide6, qfluentwidgets" & Chr(34)
rc = sh.Run(probe, 0, True)

If rc = 0 Then
  script = dir & "\winclipboard_qt.py"
Else
  script = dir & "\clipboard_manager.py"
End If

sh.Run Chr(34) & pyw & Chr(34) & " " & Chr(34) & script & Chr(34), 0, False
