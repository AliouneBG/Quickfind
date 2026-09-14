' Launch QuickFind with no console window.
' Double-click this, or drop a shortcut to it in shell:startup to run at login.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run "pythonw quickfind.py", 0, False
