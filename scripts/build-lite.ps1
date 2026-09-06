<#
.SYNOPSIS
  Coin11 助手轻量版构建脚本（0.4.0，干净、可重复；不装配完整运行时/OCR 模型）。

.DESCRIPTION
  轻量发行 = 桌面壳 + 私有 ADB + 出厂种子 + Python 3.12 基座 + 下载组件：

    1. 复用 .build\py312 可移动 Python base（仅 pip，无任何任务依赖）。
    2. 下载并校验 Android platform-tools（含 adb.exe）到 .build。
    3. 生成出厂种子 scripts/current（标记 heavy_deps_bundled=false：运行组件
       未捆绑，首次使用到桌面端“下载中心”下载）。
    4. PyInstaller onedir -> dist\Coin11助手轻量版\（桌面壳 + 出厂种子）。
    5. 装配发行目录：platform-tools\、runtime\python-bootstrap\（内置 Python
       3.12 基座，仅含 pip）、assets、LICENSE、README_DESKTOP.md。
    6. 校验关键文件后生成 dist\Coin11助手轻量版-<version>-windows-x64.zip。

  *禁止*：本脚本绝不复制 .build\runtime-src 的 site-packages、绝不复制
  .build\easyocr-models 模型 —— 那些属于完整运行时发行（scripts/build.ps1）。

.NOTES
  仅打包桌面壳与基座；任务依赖（torch/easyocr 等）由用户在软件内“下载中心”
  在线下载安装到 %LOCALAPPDATA%\Coin11Helper\runtime。安装器（.iss）需
  Inno Setup 6（ISCC.exe）；若本机没有 ISCC，本脚本清晰报错并给出安装指引，
  绝不静默下载/安装构建工具。
#>
[CmdletBinding()]
param(
    [string]$Version = "0.4.2",
    [switch]$SkipDownloads,
    [switch]$MakeInstaller
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$build = Join-Path $root ".build"
$dist  = Join-Path $root "dist"
$liteName = "Coin11助手轻量版"
$liteDir = Join-Path $dist $liteName
$liteZip = Join-Path $dist ("Coin11助手轻量版-$Version-windows-x64.zip")
# 复用完整构建的编译环境
$venv = Join-Path $build "venv"
$venvPy = Join-Path $venv "Scripts\python.exe"
# 基座来源：项目内 Python 3.12 可移动 base（site-packages 仅 pip）
$py312 = Join-Path $build "py312\python.exe"
$bootstrapSrc = Join-Path $build "py312"

function Write-Step($msg) { Write-Host "[build-lite] $msg" -ForegroundColor Cyan }
function Assert-True($cond, $msg) { if (-not $cond) { throw $msg } }

Write-Step "轻量构建根: $root"

# ----------------------------------------------------------------- 0. 前置
Assert-True (Test-Path (Join-Path $root ".git")) "必须在 git 仓库内运行（干净工作树）"
Assert-True (Test-Path $venvPy) "缺少构建 venv（请先运行 scripts\build.ps1 一次以准备 PySide6/PyInstaller 环境）"
Assert-True (Test-Path $py312) "缺少 Python 3.12 基座来源 .build\py312（请先运行 scripts\build.ps1 一次）"

# ----------------------------------------------------------------- 1. 基座校验
# 基座必须是不含任务依赖的可移动 base：site-packages 只允许 pip*，且无 pyvenv.cfg
$spDir = Join-Path (Split-Path $py312) "Lib\site-packages"
if (Test-Path $spDir) {
    $heavy = Get-ChildItem $spDir -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notmatch '^(pip|pip-.*\.dist-info|__pycache__)$' }
    Assert-True (-not $heavy) "py312 基座 site-packages 混入了任务依赖（$($heavy.Name -join ', ')）；请使用干净 Python 3.12 base"
}
Assert-True (-not (Test-Path (Join-Path (Split-Path $py312) "pyvenv.cfg"))) `
    "基座必须是可移动 base（不允许 venv 布局）"
$pyVer = & $py312 -c "import sys;print('%d.%d'%(sys.version_info[0],sys.version_info[1]))"
Assert-True ($pyVer -eq "3.12") "基座必须是 Python 3.12，当前: $pyVer"

# ----------------------------------------------------------------- 2. platform-tools
$ptDir = Join-Path $build "platform-tools"
if (-not (Test-Path (Join-Path $ptDir "platform-tools\adb.exe"))) {
    if ($SkipDownloads) { throw "缺少 platform-tools 且 -SkipDownloads 已指定：无法构建。" }
    Write-Step "下载 Android platform-tools（官方链接）…"
    $dl = Join-Path $build "dl"
    New-Item -ItemType Directory -Force -Path $dl | Out-Null
    $ptZip = Join-Path $dl "platform-tools-latest-windows.zip"
    if (-not (Test-Path $ptZip)) {
        Invoke-WebRequest -Uri "https://dl.google.com/android/repository/platform-tools-latest-windows.zip" `
            -OutFile $ptZip -UseBasicParsing
    }
    Assert-True ((Get-Item $ptZip).Length -gt 1MB) "platform-tools 下载不完整"
    if (Test-Path $ptDir) { Remove-Item -Recurse -Force $ptDir }
    New-Item -ItemType Directory -Force -Path $ptDir | Out-Null
    tar -xf $ptZip -C $ptDir
}
Assert-True (Test-Path (Join-Path $ptDir "platform-tools\adb.exe")) "platform-tools 缺少 adb.exe"

# ----------------------------------------------------------------- 3. 出厂种子（轻量标记）
Write-Step "生成出厂种子（heavy_deps_bundled=false）…"
$seedLite = Join-Path $dist "seed-lite"
if (Test-Path $seedLite) { Remove-Item -Recurse -Force $seedLite }
New-Item -ItemType Directory -Force -Path $seedLite | Out-Null
$seedCurrent = Join-Path $seedLite "scripts\current"
$head = (& git -C $root rev-parse HEAD 2>$null) | Select-Object -First 1
$env:COIN11_SEED_HEAVY_DEPS = "0"
Push-Location $root
try {
    & $venvPy -m desktop_app.make_seed $seedCurrent $head
    if ($LASTEXITCODE -ne 0) { throw "生成轻量种子失败" }
} finally {
    Pop-Location
    Remove-Item Env:COIN11_SEED_HEAVY_DEPS -ErrorAction SilentlyContinue
}

# ----------------------------------------------------------------- 4. PyInstaller
Write-Step "PyInstaller onedir（轻量桌面壳，含独立轻量种子）…"
if (Test-Path $liteDir) { Remove-Item -Recurse -Force $liteDir }
if (Test-Path (Join-Path $dist "build\Coin11HelperLite")) {
    Remove-Item -Recurse -Force (Join-Path $dist "build\Coin11HelperLite")
}
$spec = Join-Path $root "packaging\Coin11Helper.spec"
# 独立 workpath 避免与完整构建互相清理
$env:COIN11_SEED_DIR = $seedCurrent
try {
    & $venvPy -m PyInstaller --noconfirm --clean `
        --distpath $dist --workpath (Join-Path $dist "build\Coin11HelperLite") $spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }
} finally {
    Remove-Item Env:COIN11_SEED_DIR -ErrorAction SilentlyContinue
}
# PyInstaller 按 spec 的 name 输出到 dist\Coin11助手\：改名为轻量版目录
$fullShell = Join-Path $dist "Coin11助手"
Assert-True (Test-Path (Join-Path $fullShell "Coin11助手.exe")) "缺少 Coin11助手.exe（PyInstaller 产物）"
if (Test-Path $liteDir) { Remove-Item -Recurse -Force $liteDir }
Rename-Item $fullShell $liteName

# ----------------------------------------------------------------- 5. 装配轻量发行
Write-Step "装配轻量发行目录 …"
# 私有 ADB
$ptOut = Join-Path $liteDir "platform-tools"
if (Test-Path $ptOut) { Remove-Item -Recurse -Force $ptOut }
Copy-Item -Recurse (Join-Path $ptDir "platform-tools") $ptOut
# 文档与许可证
Copy-Item (Join-Path $root "LICENSE") (Join-Path $liteDir "LICENSE") -Force
Copy-Item (Join-Path $root "README_DESKTOP.md") (Join-Path $liteDir "README_DESKTOP.md") -Force
Copy-Item (Join-Path $seedCurrent "COMPAT_PATCHES.md") (Join-Path $liteDir "COMPAT_PATCHES.md") -Force
# 内置 Python 3.12 基座（仅 pip；只用于 bootstrap 安装依赖）
$rtBootstrapOut = Join-Path $liteDir "runtime\python-bootstrap"
if (Test-Path $rtBootstrapOut) { Remove-Item -Recurse -Force $rtBootstrapOut }
New-Item -ItemType Directory -Force -Path $rtBootstrapOut | Out-Null
Write-Step "复制 Python 3.12 基座到 runtime\python-bootstrap …"
Copy-Item -Recurse (Join-Path $bootstrapSrc "*") $rtBootstrapOut
Assert-True (Test-Path (Join-Path $rtBootstrapOut "python.exe")) "轻量发行缺少内置基座 python.exe"
Assert-True (-not (Test-Path (Join-Path $rtBootstrapOut "pyvenv.cfg"))) "内置基座不得为 venv 布局"
# 品牌资源（PNG/ICO）
$assetsOut = Join-Path $liteDir "assets"
if (Test-Path $assetsOut) { Remove-Item -Recurse -Force $assetsOut }
New-Item -ItemType Directory -Force -Path $assetsOut | Out-Null
Copy-Item (Join-Path $root "assets\coin11-logo-v1.png") $assetsOut -Force
Copy-Item (Join-Path $root "assets\coin11-logo-v1.ico") $assetsOut -Force

# ----------------------------------------------------------------- 6. 校验
Write-Step "轻量产物校验 …"
$checkFiles = @(
    "Coin11助手.exe",
    "LICENSE",
    "README_DESKTOP.md",
    "platform-tools\adb.exe",
    "runtime\python-bootstrap\python.exe",
    "runtime\python-bootstrap\Lib\site-packages\pip\__init__.py",
    "assets\coin11-logo-v1.png",
    "_internal\scripts\current\utils.py",
    "_internal\scripts\current\.coin11-deps.json"
)
foreach ($f in $checkFiles) {
    Assert-True (Test-Path (Join-Path $liteDir $f)) "轻量发行缺少: $f"
}
# 关键负向断言：不得携带完整 site-packages 重型依赖与 OCR 模型
$forbidden = @(
    "runtime\python-bootstrap\Lib\site-packages\torch",
    "runtime\python-bootstrap\Lib\site-packages\easyocr",
    "runtime\python\Lib\site-packages\torch",
    "runtime\easyocr-models"
)
foreach ($f in $forbidden) {
    Assert-True (-not (Test-Path (Join-Path $liteDir $f))) "轻量发行禁止包含: $f"
}
# 轻量 seed 依赖标记必须为未捆绑
$depsJson = Join-Path $liteDir "_internal\scripts\current\.coin11-deps.json"
$deps = Get-Content $depsJson -Raw -Encoding UTF8 | ConvertFrom-Json
Assert-True ($deps.heavy_deps_bundled -eq $false) "轻量种子必须标记 heavy_deps_bundled=false"

# 体积上限 sanity：轻量版（含 116MB 基座 + ADB）不应超过 ~700MB
$sizeMB = [math]::Round(((Get-ChildItem $liteDir -Recurse -File |
    Measure-Object Length -Sum).Sum / 1MB), 0)
Write-Step "轻量发行体积: ${sizeMB} MB"
Assert-True ($sizeMB -lt 700) "轻量发行体积异常偏大（${sizeMB} MB），疑似混入完整运行时依赖"

Write-Step "生成 zip: $liteZip"
if (Test-Path $liteZip) { Remove-Item -Force $liteZip }
# .NET ZipFile 压缩（比 PowerShell Compress-Archive 对海量小文件快得多、稳定）
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    [System.IO.Path]::GetFullPath($liteDir), [System.IO.Path]::GetFullPath($liteZip),
    [System.IO.Compression.CompressionLevel]::Optimal, $false)

if ($MakeInstaller) {
    # ----------------------------------------------------------------- 7. Inno Setup 安装器
    Write-Step "生成 Inno Setup 安装器 …"
    $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $iscc)) {
        $iscc2 = "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
        if (Test-Path $iscc2) { $iscc = $iscc2 }
    }
    if (-not (Test-Path $iscc)) {
        # winget 在非管理员安装时会放到当前用户的 LocalAppData。
        $isccUser = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
        if (Test-Path $isccUser) { $iscc = $isccUser }
    }
    if (-not (Test-Path $iscc)) {
        throw @"
未找到 Inno Setup 6 编译器（ISCC.exe）。
轻量安装器（Coin11助手安装版-*.exe）需要 Inno Setup 6：
  1) 下载: https://jrsoftware.org/isdl.php （或 winget install JRSoftware.InnoSetup）
  2) 安装到默认位置后重新运行本脚本（-MakeInstaller）。
本构建脚本不会静默下载或安装构建工具。发行 zip 已生成，可先分发；
安装器仅在装好 ISCC 后产出。
"@
    }
    Write-Step "使用 ISCC: $iscc"
    $iss = Join-Path $root "packaging\Coin11Helper-lite.iss"
    & $iscc "/DAppVersion=$Version" "/DSourceDir=$liteDir" $iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup 编译失败（exit $LASTEXITCODE）" }
    $exe = Join-Path $dist "Coin11助手安装版-$Version-windows-x64.exe"
    Assert-True (Test-Path $exe) "未找到安装器产物: $exe"
    Write-Host "安装器: $exe"
}

Write-Step "轻量构建完成 ✓"
Write-Host "发行目录: $liteDir"
Write-Host "压缩包:   $liteZip"
Write-Host "体积提示: 轻量版仅含桌面壳 + ADB + Python 基座（约 300–500MB 解压），"
Write-Host "          任务依赖由用户在软件内首次联网下载（torch/easyocr 模型约 400MB）。"
