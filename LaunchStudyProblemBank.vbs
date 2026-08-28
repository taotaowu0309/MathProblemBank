Option Explicit

Dim fso, shell, env, root, scriptPath, pythonw

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
Set env = shell.Environment("PROCESS")

root = fso.GetParentFolderName(WScript.ScriptFullName)
scriptPath = fso.BuildPath(root, "shared\scripts\launch_study_problem_bank.pyw")

If Not fso.FileExists(scriptPath) Then
    MsgBox "Cannot find launch_study_problem_bank.pyw." & vbCrLf & vbCrLf & _
        "Expected under:" & vbCrLf & root & "\shared\scripts", _
        vbExclamation, "Study Problem Bank"
    WScript.Quit
End If

shell.CurrentDirectory = root

pythonw = fso.BuildPath(root, ".venv\Scripts\pythonw.exe")
If RunIfExists(pythonw, scriptPath) Then WScript.Quit

pythonw = fso.BuildPath(env("LOCALAPPDATA"), "Programs\Python\Python312\pythonw.exe")
If RunIfExists(pythonw, scriptPath) Then WScript.Quit

If TryRun("pythonw " & Chr(34) & scriptPath & Chr(34)) Then WScript.Quit
If TryRun("pyw -3.12 " & Chr(34) & scriptPath & Chr(34)) Then WScript.Quit
If TryRun("pyw -3 " & Chr(34) & scriptPath & Chr(34)) Then WScript.Quit

MsgBox "Cannot find pyw or pythonw.", vbExclamation, "Study Problem Bank"

Function RunIfExists(exePath, targetPath)
    If fso.FileExists(exePath) Then
        RunIfExists = TryRun(Chr(34) & exePath & Chr(34) & " " & Chr(34) & targetPath & Chr(34))
    Else
        RunIfExists = False
    End If
End Function

Function TryRun(command)
    On Error Resume Next
    shell.Run command, 0, False
    TryRun = (Err.Number = 0)
    Err.Clear
    On Error GoTo 0
End Function
