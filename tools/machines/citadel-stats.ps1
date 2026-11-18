# Citadel's stats script for a Windows machine (Nasferatu): prints one JSON object in the same shape as the Linux
# citadel-stats, and exits. Installed as C:\ProgramData\Citadel\citadel-stats.ps1 and run only through Citadel's
# restricted key (forced command in C:\ProgramData\ssh\administrators_authorized_keys), so it takes no input.
#
# Temperatures come from LibreHardwareMonitorLib.dll if it's in C:\ProgramData\Citadel\lhm (loaded here and closed
# again: nothing stays running and nothing listens on a port). Without it, temperatures are simply null.
$ErrorActionPreference = 'SilentlyContinue'

$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$perfMem = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
$page = Get-CimInstance Win32_PageFileUsage | Measure-Object -Property AllocatedBaseSize, CurrentUsage -Sum

$total = [int64]$os.TotalVisibleMemorySize * 1024
$avail = [int64]$perfMem.AvailableBytes
$swapTotal = [int64]($page | Where-Object Property -eq 'AllocatedBaseSize').Sum * 1MB
$swapUsed = [int64]($page | Where-Object Property -eq 'CurrentUsage').Sum * 1MB

$disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
    [ordered]@{ mount = $_.DeviceID; total = [int64]$_.Size; free = [int64]$_.FreeSpace } })

# Intel RST RAID: Windows sees the array as one disk ("Intel Raid 5 Volume") and reports its health. RST also logs to the
# System log when a member drive drops out, so recent warnings and errors from its drivers are counted too.
$raid = $null
$vol = Get-PhysicalDisk | Where-Object FriendlyName -like '*Raid*' | Select-Object -First 1
if ($vol) {
    $events = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'iaStorA', 'iaStorAC', 'iaStorAVC', 'IAStorDataMgrSvc'; Level = 1, 2, 3; StartTime = (Get-Date).AddDays(-1) })
    $raid = [ordered]@{ name = $vol.FriendlyName; health = [string]$vol.HealthStatus; status = [string]($vol.OperationalStatus -join ', '); events_24h = $events.Count }
}

$cpuTemp = $null
$driveTemps = @()
$lhm = 'C:\ProgramData\Citadel\lhm\LibreHardwareMonitorLib.dll'
if (Test-Path $lhm) {
    try {
        Add-Type -Path $lhm
        $c = New-Object LibreHardwareMonitor.Hardware.Computer
        $c.IsCpuEnabled = $true
        $c.IsStorageEnabled = $true
        $c.Open()
        foreach ($hw in $c.Hardware) {
            $hw.Update()
            $temps = @($hw.Sensors | Where-Object { $_.SensorType -eq 'Temperature' -and $_.Value -ne $null })
            if ($hw.HardwareType -eq 'Cpu') {
                $pkg = $temps | Where-Object Name -like '*Package*' | Select-Object -First 1
                if (-not $pkg) { $pkg = $temps | Sort-Object Value -Descending | Select-Object -First 1 }
                if ($pkg) { $cpuTemp = [math]::Round([double]$pkg.Value, 1) }
            } elseif ($hw.HardwareType -eq 'Storage' -and $temps.Count) {
                $driveTemps += [ordered]@{ name = $hw.Name; temp = [math]::Round([double]$temps[0].Value, 1) }
            }
        }
        $c.Close()
    } catch { }
}

$out = [ordered]@{
    v = 1
    host = $env:COMPUTERNAME
    os = $os.Caption -replace '^Microsoft ', ''
    uptime_s = [int64]((Get-Date) - $os.LastBootUpTime).TotalSeconds
    load = @()
    cpu = [ordered]@{ percent = [double]$cpu.LoadPercentage; cores = [int]$cpu.NumberOfLogicalProcessors; temp = $cpuTemp }
    mem = [ordered]@{ total = $total; used = $total - $avail; available = $avail; swap_total = $swapTotal; swap_used = $swapUsed; buffers = 0; cached = 0 }
    disks = $disks
    drive_temps = $driveTemps
    gpu = $null
    battery = $null
    pi = $null
    raid = $raid
}
$out | ConvertTo-Json -Depth 4 -Compress
