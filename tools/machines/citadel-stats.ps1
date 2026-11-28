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

# CPU % and the busiest processes over one quiet second, measured last: Win32_Processor.LoadPercentage read at the top
# counted this script's own start-up (PowerShell plus the hardware library pegs a 2-core Pentium for a moment), so
# Nasferatu showed 30-70 % while idle. The raw _Total counter counts idle time in 100 ns units (timer_100ns_inv).
$cores = [int]$cpu.NumberOfLogicalProcessors
$q = "SELECT PercentProcessorTime, Timestamp_Sys100NS FROM Win32_PerfRawData_PerfOS_Processor WHERE Name='_Total'"
$p1 = @{}; Get-Process | ForEach-Object { if ($_.CPU -ne $null) { $p1[$_.Id] = @($_.ProcessName, [double]$_.CPU) } }
$r1 = Get-CimInstance -Query $q
$sw = [Diagnostics.Stopwatch]::StartNew()
Start-Sleep -Milliseconds 1000
$r2 = Get-CimInstance -Query $q
$secs = $sw.Elapsed.TotalSeconds
$cpuPct = $null
$dt = [double]$r2.Timestamp_Sys100NS - [double]$r1.Timestamp_Sys100NS
if ($dt -gt 0) { $cpuPct = [math]::Round([math]::Max(0, [math]::Min(100, 100 * (1 - ([double]$r2.PercentProcessorTime - [double]$r1.PercentProcessorTime) / $dt))), 1) }
$byName = @{}
Get-Process | ForEach-Object {
    $a = $p1[$_.Id]
    if ($a -and $_.Id -ne $PID -and $_.CPU -ne $null -and $a[0] -eq $_.ProcessName) {
        $d = [double]$_.CPU - $a[1]
        if ($d -gt 0) { $byName[$_.ProcessName] = [double]$byName[$_.ProcessName] + $d }
    }
}
$top = @($byName.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 5 | ForEach-Object {
    $pct = [math]::Round(100 * $_.Value / ($secs * [math]::Max(1, $cores)), 1)
    if ($pct -ge 0.5) { [ordered]@{ name = $_.Key; percent = $pct } } })

# What the machine is, for the Fleet page's Specifications card (the same `spec` shape as the Linux script's; Citadel
# tidies the names). AdapterRAM is a 32-bit field, so it can't say more than 4 GB.
$cs = Get-CimInstance Win32_ComputerSystem
$board = Get-CimInstance Win32_BaseBoard
$gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object {
    [ordered]@{ vendor = [string]$_.AdapterCompatibility; device = [string]$_.Name; vram_mb = $(if ($_.AdapterRAM) { [math]::Round([double]$_.AdapterRAM / 1MB) } else { $null }) } })
# Memory sticks: SMBIOSMemoryType is the firmware's own code (24 DDR3, 26 DDR4, 34 DDR5…), named the way Linux's udev
# names them, so Citadel reads both the same way.
$memTypes = @{ 18 = 'DDR'; 19 = 'DDR2'; 20 = 'DDR'; 21 = 'DDR2'; 24 = 'DDR3'; 26 = 'DDR4'; 27 = 'LPDDR'; 28 = 'LPDDR2'; 29 = 'LPDDR3'; 30 = 'LPDDR4'; 34 = 'DDR5'; 35 = 'LPDDR5' }
$ram = @(Get-CimInstance Win32_PhysicalMemory | ForEach-Object {
    $t = [int]$_.SMBIOSMemoryType; if (-not $memTypes.ContainsKey($t)) { $t = [int]$_.MemoryType }
    $mts = $(if ($_.ConfiguredClockSpeed) { [int]$_.ConfiguredClockSpeed } elseif ($_.Speed) { [int]$_.Speed } else { $null })
    [ordered]@{ type = $(if ($memTypes.ContainsKey($t)) { $memTypes[$t] } else { 'Unknown' }); size = [int64]$_.Capacity; mts = $mts; form = $(if ($_.FormFactor -eq 12) { 'SODIMM' } else { 'DIMM' }) } })
$spec = [ordered]@{
    model = $null
    dmi = [ordered]@{ sys_vendor = [string]$cs.Manufacturer; product_name = [string]$cs.Model; board_vendor = [string]$board.Manufacturer; board_name = [string]$board.Product }
    cpu = ([string]$cpu.Name).Trim()
    cpu_part = $null
    cores = [int]$cpu.NumberOfCores
    threads = $cores
    max_mhz = [int]$cpu.MaxClockSpeed
    gpus = $gpus
    ram = $ram
    kernel = [string]$os.Version
    arch = [string]$os.OSArchitecture
}

$out = [ordered]@{
    v = 1
    host = $env:COMPUTERNAME
    os = $os.Caption -replace '^Microsoft ', ''
    uptime_s = [int64]((Get-Date) - $os.LastBootUpTime).TotalSeconds
    load = @()
    cpu = [ordered]@{ percent = $cpuPct; cores = $cores; temp = $cpuTemp }
    mem = [ordered]@{ total = $total; used = $total - $avail; available = $avail; swap_total = $swapTotal; swap_used = $swapUsed; buffers = 0; cached = 0 }
    disks = $disks
    drive_temps = $driveTemps
    gpu = $null
    battery = $null
    pi = $null
    raid = $raid
    top = $top
    spec = $spec
}
$out | ConvertTo-Json -Depth 4 -Compress
