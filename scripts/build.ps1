<#
.SYNOPSIS
  Coin11 助手桌面版构建脚本（完整运行时，0.3.0，干净、可重复）。

.DESCRIPTION
  从干净工作树构建“完整运行时发行版”：
    1. 定位/校验 Python 3.12 x64（优先项目内 .build\py312，其次注册的 3.12）；
       明确拒绝使用系统 Python 3.14。
    2. 建立可移动自包含运行时 .build\runtime-src（Python 3.12 x64 base +
       site-packages），用锁定的 requirements-desktop-runtime.txt 安装全部任务依赖
       （uiautomator2/uiautodev/numpy/Pillow/opencv-python/ddddocr/easyocr/torch/
       torchvision/requests）。该运行时不含 venv 引用，可整体移动；不用用户的
       全局 site-packages。首次安装会下载 CPU PyTorch 与全部 wheel。
    3. 下载并校验 Android platform-tools（含 adb.exe）到 .build。
    4. 用 CPU 运行时预下载 EasyOCR 检测/识别模型（craft_mlt_25k.pth、
       zh_sim_g2.pth）到离线模型目录；下载失败则构建失败（不产出“可用”的假包）。
    5. 生成出厂种子 scripts/current（utils.py 兼容补丁、LICENSE、README_DESKTOP.md、
       COMPAT_PATCHES.md、.coin11-meta.json、.coin11-deps.json，
       heavy_deps_bundled=true）。
    6. PyInstaller onedir -> dist/Coin11助手/（轻量桌面壳 + 出厂种子）。
    7. 装配发行目录：platform-tools/、runtime/python/（内置解释器+全部依赖）、
       runtime/easyocr-models/、LICENSE、README_DESKTOP.md、COMPAT_PATCHES.md。
    8. 校验发行目录关键文件（EXE/内置解释器/私有 adb/离线模型/冻结合集）后，
       生成 dist/Coin11助手-0.3.0-windows-x64.zip。

  失败语义：任何一步（尤其 EasyOCR 模型或 PyTorch CPU wheel 下载）失败都给出
  明确可重复错误并保留现场，绝不输出“可用”的完整包。

.DESCRIPTION_LEGACY
  0.1.0 说明（历史）：任务的重型依赖不进入 CPU 发行版，任务以可见失败呈现
  （seed 内 .coin11-deps.json 标记 heavy_deps_bundled=false）。0.2.0 起改为
  完整运行时捆绑，见上。0.3.0 起新增品牌图标（PNG/ICO）、任务分组（日常 +
  限时活动）、实时日志与设备断线保护。

.NOTES
  要求已联网（python.org / download.pytorch.org / dl.google.com / jaist 镜像），
  git 在 PATH。构建体积明显大于 55 MB 属预期（完整运行时发行版）。
#>
[CmdletBinding()]
param(
    [string]$Version = "0.3.0",
    [switch]$SkipDownloads
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$build = Join-Path $root ".build"
$dist  = Join-Path $root "dist"
$distName = "Coin11助手"
$distDir = Join-Path $dist $distName
$distZip = Join-Path $dist ("Coin11助手-$Version-windows-x64.zip")

function Write-Step($msg) { Write-Host "[build] $msg" -ForegroundColor Cyan }
function Assert-True($cond, $msg) { if (-not $cond) { throw $msg } }

# --------------------------------------------------------------------- 0. 前置
Write-Step "构建根: $root"
Assert-True (Test-Path (Join-Path $root ".git")) "必须在 git 仓库内运行（干净工作树）"

# --------------------------------------------------------------------- 1. Python 3.12
$py312 = Join-Path $build "py312\python.exe"
if (Test-Path $py312) {
    Write-Step "使用项目内 Python 3.12: $py312"
} else {
    # 检查已注册的 Python 3.12（launcher）
    $found = $null
    try { $found = (& py -3.12 -c "import sys;print(sys.executable)" 2>$null) } catch {}
    if ($found -and (Test-Path $found)) {
        Write-Step "使用已注册 Python 3.12: $found"
        $py312 = $found.Trim()
    } else {
        if ($SkipDownloads) { throw "未找到 Python 3.12 且 -SkipDownloads 已指定：无法构建。" }
        Write-Step "下载 Python 3.12.10 x64 安装器 …"
        $dl = Join-Path $build "dl"
        New-Item -ItemType Directory -Force -Path $dl | Out-Null
        $installer = Join-Path $dl "python-3.12.10-amd64.exe"
        if (-not (Test-Path $installer)) {
            Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" `
                -OutFile $installer -UseBasicParsing
        }
        Assert-True ((Get-Item $installer).Length -gt 10MB) "Python 安装器下载不完整"
        Write-Step "静默安装 Python 3.12 到 $build\py312 …"
        $target = Join-Path $build "py312"
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        $p = Start-Process -FilePath $installer -ArgumentList @(
            "/quiet", "TargetDir=$target", "InstallAllUsers=0", "Include_pip=1",
            "Include_launcher=0", "AssociateFiles=0", "Shortcuts=0",
            "Include_test=0", "PrependPath=0") -Wait -PassThru
        Assert-True ($p.ExitCode -eq 0) "Python 3.12 安装失败 (exit $($p.ExitCode))"
        $py312 = Join-Path $target "python.exe"
    }
}
Assert-True (Test-Path $py312) "找不到 Python 3.12 可执行文件: $py312"
$pyVer = & $py312 -c "import sys;print('%d.%d'%(sys.version_info[0],sys.version_info[1]))"
Write-Step "Python 版本: $pyVer"
Assert-True ($pyVer -eq "3.12") "构建环境必须是 Python 3.12，当前: $pyVer（拒绝使用 3.14 做运行时）"

# --------------------------------------------------------------------- 2. venv（桌面壳构建）
$venv = Join-Path $build "venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Step "创建隔离 venv …"
    & $py312 -m venv $venv
}
$venvPy = Join-Path $venv "Scripts\python.exe"
Write-Step "安装桌面壳构建依赖（PySide6 / pyinstaller / dulwich）…"
& $venvPy -m pip install --disable-pip-version-check --no-warn-script-location `
    --no-cache-dir -q "PySide6==6.8.1" "pyinstaller==6.11.1" "dulwich==0.22.7"
if ($LASTEXITCODE -ne 0) { throw "桌面壳构建依赖安装失败" }

# --------------------------------------------------------------------- 2b. 完整运行时（可移动内置解释器 + 全部任务依赖）
$runtimeSrc = Join-Path $build "runtime-src"
$runtimeReq = Join-Path $root "requirements-desktop-runtime.txt"
Assert-True (Test-Path $runtimeReq) "缺少锁定依赖清单: requirements-desktop-runtime.txt"
if (-not (Test-Path (Join-Path $runtimeSrc "python.exe"))) {
    if ($SkipDownloads) { throw "缺少 runtime-src 且 -SkipDownloads 已指定：无法构建。" }
    Write-Step "复制可移动 Python base 到 runtime-src …"
    New-Item -ItemType Directory -Force -Path $runtimeSrc | Out-Null
    Copy-Item -Recurse (Join-Path $build "py312\*") $runtimeSrc
}
$runtimePy = Join-Path $runtimeSrc "python.exe"
Assert-True (Test-Path $runtimePy) "runtime-src 缺少 python.exe"
Assert-True (-not (Test-Path (Join-Path $runtimeSrc "pyvenv.cfg"))) `
    "runtime-src 必须是可移动 base（不允许 venv 布局，否则发行后解释器失效）"
Write-Step "安装完整运行时锁定依赖（首次含 CPU PyTorch 下载，体积大）…"
& $runtimePy -m pip install --disable-pip-version-check --no-warn-script-location `
    -r $runtimeReq
if ($LASTEXITCODE -ne 0) { throw "完整运行时依赖安装失败（请检查 download.pytorch.org 可达性）" }

# --------------------------------------------------------------------- 2c. EasyOCR 离线模型（CPU 预下载）
$modelsDir = Join-Path $build "easyocr-models"
New-Item -ItemType Directory -Force -Path $modelsDir | Out-Null
# craft_mlt_25k（文本检测）与 zh_sim_g2（中文识别）是 EasyOCR 1.7.2 默认必需模型；
# URL 取自 easyocr/config.py 的真实 release 地址（.zip 形式）
$modelUrls = @{
    "craft_mlt_25k" = "https://github.com/JaidedAI/EasyOCR/releases/download/pre-v1.1.6/craft_mlt_25k.zip"
    "zh_sim_g2"     = "https://github.com/JaidedAI/EasyOCR/releases/download/v1.3/zh_sim_g2.zip"
}
$modelZipDir = Join-Path $build "dl"
New-Item -ItemType Directory -Force -Path $modelZipDir | Out-Null
foreach ($name in @("craft_mlt_25k", "zh_sim_g2")) {
    $pthFile = Join-Path $modelsDir "$name.pth"
    if (-not (Test-Path $pthFile)) {
        $url = $modelUrls[$name]
        $zipFile = Join-Path $modelZipDir "$name.zip"
        if (-not (Test-Path $zipFile)) {
            if ($SkipDownloads) { throw "缺少 EasyOCR 模型 $name 且 -SkipDownloads 已指定：无法构建。" }
            Write-Step "下载 EasyOCR 模型 $name.zip …"
            Invoke-WebRequest -Uri $url -OutFile $zipFile -UseBasicParsing
        }
        Assert-True ((Get-Item $zipFile).Length -gt 10MB) "EasyOCR 模型 $name.zip 下载不完整"
        Write-Step "解压 $name.zip → $pthFile …"
        tar -xf $zipFile -C $modelsDir
        if (-not (Test-Path $pthFile)) {
            throw "解压 $name.zip 后未找到 $pthFile（构建失败，不产出可用假包）"
        }
    }
}
# 各模型真实体积下限（craft ~79MB，zh_sim_g2 ~21MB）
$modelMinBytes = @{
    "craft_mlt_25k.pth" = 50MB
    "zh_sim_g2.pth"     = 15MB
}
foreach ($pth in @("craft_mlt_25k.pth", "zh_sim_g2.pth")) {
    Assert-True ((Get-Item (Join-Path $modelsDir $pth)).Length -gt $modelMinBytes[$pth]) `
        "EasyOCR 模型 $pth 不完整（构建失败，不产出可用假包）"
}
Write-Step "EasyOCR 离线模型就绪: $modelsDir"
# 离线模型目录初始化验证：用运行时解释器创建 Reader(gpu=False, 离线目录) 且不联网
Write-Step "离线 EasyOCR 冒烟验证（不联网）…"
$env:COIN11_EASYOCR_MODEL_DIR = $modelsDir
& $runtimePy -c "import easyocr; r = easyocr.Reader(['ch_sim','en'], gpu=False, model_storage_directory=r'$modelsDir', download_enabled=False); print('EASYOCR_OFFLINE_OK')"
$smokeExit = $LASTEXITCODE
Remove-Item Env:COIN11_EASYOCR_MODEL_DIR -ErrorAction SilentlyContinue
if ($smokeExit -ne 0) { throw "EasyOCR 离线模型冒烟验证失败（exit $smokeExit）" }

# --------------------------------------------------------------------- 3. platform-tools
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
    Write-Step "解压 platform-tools …"
    if (Test-Path $ptDir) { Remove-Item -Recurse -Force $ptDir }
    New-Item -ItemType Directory -Force -Path $ptDir | Out-Null
    tar -xf $ptZip -C $ptDir
}
$ptExe = Join-Path $ptDir "platform-tools\adb.exe"
Assert-True (Test-Path $ptExe) "platform-tools 中缺少 adb.exe（构建中止，不产出残缺包）"
Write-Step "platform-tools 就绪: $ptExe"

# --------------------------------------------------------------------- 4. 出厂种子
Write-Step "生成出厂种子 scripts/current …"
$seedRoot = Join-Path $dist "seed"
if (Test-Path $seedRoot) { Remove-Item -Recurse -Force $seedRoot }
New-Item -ItemType Directory -Force -Path $seedRoot | Out-Null
$seedCurrent = Join-Path $seedRoot "scripts\current"
$head = (& git -C $root rev-parse HEAD 2>$null) | Select-Object -First 1
Push-Location $root
try {
    # 仓库根由 make_seed 模块位置自动推导；参数 = 目标目录 + commit
    & $venvPy -m desktop_app.make_seed $seedCurrent $head
    if ($LASTEXITCODE -ne 0) { throw "生成出厂种子失败" }
} finally { Pop-Location }

# --------------------------------------------------------------------- 5. PyInstaller
Write-Step "PyInstaller onedir 打包（轻量桌面壳）…"
if (Test-Path $distDir) { Remove-Item -Recurse -Force $distDir }
if (Test-Path (Join-Path $dist "build\Coin11Helper")) { Remove-Item -Recurse -Force (Join-Path $dist "build\Coin11Helper") }
$spec = Join-Path $root "packaging\Coin11Helper.spec"
& $venvPy -m PyInstaller --noconfirm --clean --distpath $dist --workpath (Join-Path $dist "build") $spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }
Assert-True (Test-Path (Join-Path $distDir "Coin11助手.exe")) "缺少 Coin11助手.exe"

# --------------------------------------------------------------------- 6. 装配发行目录
Write-Step "装配发行目录 …"
$ptOut = Join-Path $distDir "platform-tools"
if (Test-Path $ptOut) { Remove-Item -Recurse -Force $ptOut }
Copy-Item -Recurse (Join-Path $ptDir "platform-tools") $ptOut
Copy-Item (Join-Path $root "LICENSE") (Join-Path $distDir "LICENSE") -Force
Copy-Item (Join-Path $root "README_DESKTOP.md") (Join-Path $distDir "README_DESKTOP.md") -Force
Copy-Item (Join-Path $seedCurrent "COMPAT_PATCHES.md") (Join-Path $distDir "COMPAT_PATCHES.md") -Force

# 内置完整运行时（可移动解释器 + 全部依赖）
$rtPyOut = Join-Path $distDir "runtime\python"
if (Test-Path $rtPyOut) { Remove-Item -Recurse -Force $rtPyOut }
New-Item -ItemType Directory -Force -Path $rtPyOut | Out-Null
Write-Step "复制完整运行时到 runtime\python …（数分钟）"
Copy-Item -Recurse (Join-Path $runtimeSrc "*") $rtPyOut
Assert-True (Test-Path (Join-Path $rtPyOut "python.exe")) "发行目录缺少内置 python.exe"
Assert-True (-not (Test-Path (Join-Path $rtPyOut "pyvenv.cfg"))) "内置运行时不得为 venv 布局"

# 离线 EasyOCR 模型
$rtModelsOut = Join-Path $distDir "runtime\easyocr-models"
if (Test-Path $rtModelsOut) { Remove-Item -Recurse -Force $rtModelsOut }
New-Item -ItemType Directory -Force -Path $rtModelsOut | Out-Null
Copy-Item (Join-Path $modelsDir "craft_mlt_25k.pth") $rtModelsOut -Force
Copy-Item (Join-Path $modelsDir "zh_sim_g2.pth") $rtModelsOut -Force

# 品牌资源副本（PNG 运行时展示 + ICO 已由 PyInstaller 打进 EXE；发行根也放一份
# PNG 供读取，避免依赖 _internal 细节；ICO 不覆盖源文件）
$assetsOut = Join-Path $distDir "assets"
if (Test-Path $assetsOut) { Remove-Item -Recurse -Force $assetsOut }
New-Item -ItemType Directory -Force -Path $assetsOut | Out-Null
Copy-Item (Join-Path $root "assets\coin11-logo-v1.png") $assetsOut -Force
Copy-Item (Join-Path $root "assets\coin11-logo-v1.ico") $assetsOut -Force

# --------------------------------------------------------------------- 7. 校验 + zip
Write-Step "产物校验 …"
# 桌面壳冻结合集：PyInstaller 6 onedir 把 datas 落在 _internal/
$checkFiles = @(
    "Coin11助手.exe",
    "LICENSE",
    "README_DESKTOP.md",
    "assets\coin11-logo-v1.png",
    "assets\coin11-logo-v1.ico",
    "_internal\assets\coin11-logo-v1.png",
    "_internal\assets\coin11-logo-v1.ico",
    "platform-tools\adb.exe",
    "runtime\python\python.exe",
    "runtime\python\Lib\site-packages\torch\__init__.py",
    "runtime\python\Lib\site-packages\easyocr\__init__.py",
    "runtime\python\Lib\site-packages\uiautomator2\__init__.py",
    "runtime\python\Lib\site-packages\cv2\__init__.py",
    "runtime\python\Lib\site-packages\ddddocr\__init__.py",
    "runtime\python\Lib\site-packages\PIL\__init__.py",
    "runtime\python\Lib\site-packages\numpy\__init__.py",
    "runtime\python\Lib\site-packages\requests\__init__.py",
    "runtime\easyocr-models\craft_mlt_25k.pth",
    "runtime\easyocr-models\zh_sim_g2.pth",
    "_internal\scripts\current\utils.py",
    "_internal\scripts\current\.coin11-deps.json",
    "_internal\scripts\current\.coin11-meta.json"
)
foreach ($f in $checkFiles) {
    Assert-True (Test-Path (Join-Path $distDir $f)) "发行目录缺少: $f"
}
# seed 依赖标记必须为已捆绑
$depsJson = Join-Path $distDir "_internal\scripts\current\.coin11-deps.json"
$deps = Get-Content $depsJson -Raw -Encoding UTF8 | ConvertFrom-Json
Assert-True ($deps.heavy_deps_bundled -eq $true) "seed .coin11-deps.json 必须标记 heavy_deps_bundled=true"
# 内置解释器最小 PATH 导入探针（workerChecks: runtime 导入）
Write-Step "内置解释器最小 PATH 导入探针 …"
$probe = Join-Path $build "runtime_probe.py"
$probeSrc = @'
import sys
mods = ["torch", "torchvision", "cv2", "ddddocr", "easyocr",
        "uiautomator2", "uiautodev", "numpy", "PIL"]
missing = []
for m in mods:
    try:
        __import__(m)
    except Exception as exc:
        missing.append("%s:%s" % (m, exc))
if missing:
    print("MISSING:", "; ".join(missing)); sys.exit(2)
print("RUNTIME_PROBE_OK")
'@
# 以 UTF-8 无 BOM 写入（Python 源码不允许 BOM 前缀之外的编码问题）
[System.IO.File]::WriteAllText($probe, $probeSrc, (New-Object System.Text.UTF8Encoding($false)))
$probePy = Join-Path $distDir "runtime\python\python.exe"
# 最小 PATH：仅系统目录，直接启动发行内置解释器（不经 cmd 嵌套，避免转义/编码问题）
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $probePy
$psi.Arguments = "`"$probe`""
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.CreateNoWindow = $true
$sysRoot = $env:SystemRoot
$psi.EnvironmentVariables["PATH"] = "$sysRoot\system32;$sysRoot"
$psi.EnvironmentVariables.Remove("PYTHONPATH")
$proc = [System.Diagnostics.Process]::Start($psi)
$stdout = $proc.StandardOutput.ReadToEnd()
$stderr = $proc.StandardError.ReadToEnd()
$proc.WaitForExit()
if ($proc.ExitCode -ne 0) {
    Write-Host $stdout
    Write-Host $stderr
    throw "内置解释器最小 PATH 导入探针失败 (exit $($proc.ExitCode))"
}
Write-Host $stdout.Trim()
Remove-Item $probe -Force -ErrorAction SilentlyContinue

Write-Step "生成 zip: $distZip"
if (Test-Path $distZip) { Remove-Item -Force $distZip }
Compress-Archive -Path (Join-Path $distDir "*") -DestinationPath $distZip -CompressionLevel Optimal

Write-Step "构建完成 ✓"
Write-Host "发行目录: $distDir"
Write-Host "压缩包:   $distZip"
Write-Host "体积提示: 完整运行时发行版明显大于 55 MB 属预期（离线 CPU torch + easyocr 模型）"
