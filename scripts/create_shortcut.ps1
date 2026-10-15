# 在桌面创建 Amedown 快捷方式，实现「点击即用」。
#
# 用法（在项目根目录执行）：
#   powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1
#
# 可选参数：
#   -ExePath   <路径>   指定可执行文件（默认 dist\Amedown\Amedown.exe）
#   -Name      <名称>   快捷方式名称（默认 Amedown）
#   -Remove             删除已创建的快捷方式
param(
    [string]$ExePath = "",
    [string]$Name = "Amedown",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

function Get-AbsolutePath([string]$path) {
    if ([System.IO.Path]::IsPathRooted($path)) { return $path }
    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $path))
}

# 项目根 = 本脚本所在目录的上一级
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
if ([string]::IsNullOrWhiteSpace($ExePath)) {
    $ExePath = Join-Path $projectRoot "dist\Amedown\Amedown.exe"
}
$ExePath = Get-AbsolutePath $ExePath

$desktop = [Environment]::GetFolderPath("Desktop")
$linkPath = Join-Path $desktop "$Name.lnk"

if ($Remove) {
    if (Test-Path $linkPath) {
        Remove-Item $linkPath -Force
        Write-Host "已删除快捷方式: $linkPath"
    } else {
        Write-Host "快捷方式不存在: $linkPath"
    }
    exit 0
}

if (-not (Test-Path $ExePath)) {
    Write-Error "找不到可执行文件: $ExePath`n请先构建：pyinstaller run.spec --noconfirm"
    exit 1
}

$exeDir = Split-Path -Parent $ExePath
$exeName = Split-Path -Leaf $ExePath

# 图标来源：优先用 exe 旁边独立的 .ico 文件，而不是 exe 自身。
#
# 为什么要这样：Windows 会按「图标来源路径 + 索引」缓存图标。如果快捷方式指向
# exe 自身，重建 exe 后快捷方式常常仍显示**旧图标**（缓存未失效，实测遇到）。
# 改用一个独立文件作为来源，缓存键随之改变，Explorer 必须重新读取。
$iconSource = Join-Path $exeDir "Amedown.ico"
$repoIcon = Join-Path $projectRoot "src\bilidown\resources\Amedown.ico"
if (Test-Path $repoIcon) {
    try {
        Copy-Item $repoIcon $iconSource -Force
        Write-Host "已更新图标文件: $iconSource"
    } catch {
        Write-Host "复制图标失败，将回退使用 exe 内嵌图标: $_"
        $iconSource = ""
    }
} else {
    $iconSource = ""
}

if ([string]::IsNullOrWhiteSpace($iconSource) -or -not (Test-Path $iconSource)) {
    # 回退：用 exe 内嵌图标
    $iconSource = $ExePath
    Write-Host "未找到独立图标文件，使用 exe 内嵌图标"
}

# WScript.Shell 是创建 .lnk 的标准方式（无需额外依赖）
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($linkPath)
$shortcut.TargetPath = $ExePath
# 工作目录设为 exe 所在目录，保证相对路径行为一致
$shortcut.WorkingDirectory = $exeDir
$shortcut.Description = "Amedown - Bilibili 视频/音频下载器"
$shortcut.IconLocation = "$iconSource,0"
$shortcut.WindowStyle = 1
$shortcut.Save()

# 释放 COM 引用，避免文件被占用
[System.Runtime.InteropServices.Marshal]::ReleaseComObject($shortcut) | Out-Null
[System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell) | Out-Null
[GC]::Collect()

if (Test-Path $linkPath) {
    $size = (Get-Item $linkPath).Length
    Write-Host "已创建桌面快捷方式"
    Write-Host "  快捷方式: $linkPath"
    Write-Host "  目标    : $ExePath"
    Write-Host "  工作目录: $exeDir"
    Write-Host "  大小    : $size 字节"
    exit 0
}

Write-Error "快捷方式创建失败: $linkPath"
exit 1
