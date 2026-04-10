$TaskName = "fanc_vm"
$PythonPath = "C:\fanc_vm\venv\Scripts\pythonw.exe"
$ScriptPath = "C:\fanc_vm\fanc_client.py"
$WorkDir = "C:\fanc_vm"

# Удалить существующую задачу если есть
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task: $TaskName"
}

# Создать задачу
$Action = New-ScheduledTaskAction -Execute $PythonPath -Argument $ScriptPath -WorkingDirectory $WorkDir
$Trigger = New-ScheduledTaskTrigger -AtStartup

# Настройки: скрытое окно, не останавливать при питании от батареи
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -Hidden `
    -Priority 0

# Запуск от SYSTEM с максимальными правами
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

# Регистрация
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal -Description "Fan control (for VM)" | Out-Null

Write-Host "Task created: $TaskName"
Write-Host "Run mode: Hidden (no console window)"
Write-Host "Trigger: At logon"
Write-Host "Priority: Real-time (0)"

# Запустить сразу
Start-ScheduledTask -TaskName $TaskName
Write-Host "Task started"
