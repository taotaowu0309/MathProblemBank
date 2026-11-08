Option Explicit

Dim fso, shell, env, root, scriptPath, updaterPath, pythonw

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
Set env = shell.Environment("PROCESS")

root = fso.GetParentFolderName(WScript.ScriptFullName)
scriptPath = fso.BuildPath(root, "shared\scripts\launch_study_problem_bank.pyw")
updaterPath = fso.BuildPath(root, "shared\scripts\code_update.py")

If Not fso.FileExists(scriptPath) Then
    MsgBox "Cannot find launch_study_problem_bank.pyw." & vbCrLf & vbCrLf & _
        "Expected under:" & vbCrLf & root & "\shared\scripts", _
        vbExclamation, "Study Problem Bank"
    WScript.Quit
End If

shell.CurrentDirectory = root

pythonw = fso.BuildPath(root, ".venv\Scripts\pythonw.exe")
If RunIfExists(pythonw) Then WScript.Quit

pythonw = fso.BuildPath(env("LOCALAPPDATA"), "Programs\Python\Python312\pythonw.exe")
If RunIfExists(pythonw) Then WScript.Quit

If Launch("pythonw") Then WScript.Quit
If Launch("pyw -3.12") Then WScript.Quit
If Launch("pyw -3") Then WScript.Quit

MsgBox "Cannot find pyw or pythonw.", vbExclamation, "Study Problem Bank"

Function RunIfExists(exePath)
    If fso.FileExists(exePath) Then
        RunIfExists = Launch(Chr(34) & exePath & Chr(34))
    Else
        RunIfExists = False
    End If
End Function

' Fast-forward the program code from Git first (never blocks; see logs\code_update.log),
' then start the control center from the possibly updated scripts.
Function Launch(python)
    If fso.FileExists(updaterPath) Then
        If Not TryRunWait(python & " " & Chr(34) & updaterPath & Chr(34)) Then
            Launch = False
            Exit Function
        End If
        ' The app process would otherwise fetch again. Mac launches do not set this.
        env("MPB_CODE_UPDATE_DONE") = "1"
    End If
    Launch = TryRun(python & " " & Chr(34) & scriptPath & Chr(34))
End Function

Function TryRunWait(command)
    On Error Resume Next
    shell.Run command, 0, True
    TryRunWait = (Err.Number = 0)
    Err.Clear
    On Error GoTo 0
End Function

Function TryRun(command)
    On Error Resume Next
    shell.Run command, 0, False
    TryRun = (Err.Number = 0)
    Err.Clear
    On Error GoTo 0
End Function
