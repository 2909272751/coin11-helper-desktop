"""全局常量：路径规则、固定同步源、环境变量名。"""
from __future__ import annotations

import os
import sys

APP_DISPLAY_NAME = "Coin11助手"
APP_NAME = "Coin11Helper"  # %LOCALAPPDATA% 下的数据目录名
APP_VERSION = "0.4.1"

# 唯一允许的上游同步源（HTTPS）。校验时使用精确字符串比较，不做子串匹配。
UPSTREAM_REPO_URL = "https://github.com/czl0325/coin11-tb.git"
# 同步走 GitHub 官方 API / codeload，仅允许这两个精确前缀；用于读取默认分支与
# HEAD commit、下载该 commit 的源码包。
GITHUB_API_REPO = "https://api.github.com/repos/czl0325/coin11-tb"
GITHUB_CODELOAD_PREFIX = "https://codeload.github.com/czl0325/coin11-tb/tar.gz/"

# 桌面应用向任务脚本注入的设备序列号环境变量
ENV_DEVICE_SERIAL = "COIN11_DEVICE_SERIAL"
# 可选：开发/测试时可把私有 adb 指向自定义路径（正式发行不使用）
ENV_ADB_EXE = "COIN11_ADB_EXE"
# 测试时可覆盖数据目录（默认 %LOCALAPPDATA%\\Coin11Helper）
ENV_DATA_DIR = "COIN11_HELPER_DATA_DIR"
# EasyOCR 离线模型目录（发行目录/用户数据内已打包模型；未设置时保持上游可运行默认）
ENV_EASYOCR_MODEL_DIR = "COIN11_EASYOCR_MODEL_DIR"

# 目录布局
DIR_PLATFORM_TOOLS = "platform-tools"
DIR_SCRIPTS = "scripts"
DIR_CURRENT = "current"
DIR_PREVIOUS = "previous"
DIR_STAGING = ".staging"
DIR_LOGS = "logs"

# 完整运行时布局（发行根下）
DIR_RUNTIME = "runtime"
DIR_RUNTIME_PY = os.path.join(DIR_RUNTIME, "python")
DIR_RUNTIME_MODELS = os.path.join(DIR_RUNTIME, "easyocr-models")

# 轻量版运行时布局
#  - 发行根 runtime\\python-bootstrap：随安装包内置的可移动 Python 3.12 基座
#    （仅 pip，无任何任务依赖/模型）。只用于 bootstrap：复制到用户数据运行时后
#    由下载中心在其上安装锁定依赖；绝不直接用于运行任务。
#  - 用户数据运行时（默认 %LOCALAPPDATA%\\Coin11Helper\\runtime）：
#        python\\python.exe        —— 已验证的数据运行时解释器（任务只接受它）
#        easyocr-models\\*.pth     —— 下载的离线模型
#        state.json                —— 组件验证状态
#        .downloads\\              —— 临时下载（成功后原子替换）
DIR_RUNTIME_BOOTSTRAP_PY = os.path.join(DIR_RUNTIME, "python-bootstrap")
# 用户数据目录下默认运行时子目录（可被设置覆盖 runtime_dir）
DIR_USER_RUNTIME = "runtime"

# 品牌图标（PNG 源文件只读；.ico 由构建/脚本生成，二者都不被覆盖改写）
ASSET_LOGO_PNG = "coin11-logo-v1.png"
ASSET_LOGO_ICO = "coin11-logo-v1.ico"

# 默认任务列表（可日常运行的日常任务；按 UI 分组展示）。
# 每个条目: task id / 标题 / 上游脚本文件名 / 说明 / 分组。
# group=daily 为日常任务（默认展示并允许勾选）；group=limited 为限时活动脚本
# （页面可能已改版/过期，需单独展开区域并运行前提示）。
GROUP_DAILY = "daily"
GROUP_LIMITED = "limited"

# 日常任务：原有 6 项 + 仓库内可直接入口的签到/现金任务
DEFAULT_TASKS = [
    {
        "id": "taobao_achievement",
        "title": "淘宝成就中心签到",
        "script": "淘宝成就中心签到.py",
        "group": GROUP_DAILY,
        "description": "启动淘宝并进入成就中心完成可做任务与每日签到（不处理邀请/下单类任务）。",
    },
    {
        "id": "xianyu_dice",
        "title": "闲鱼扔骰子",
        "script": "闲鱼扔骰子.py",
        "group": GROUP_DAILY,
        "description": "在闲鱼完成每日扔骰子攒币任务。",
    },
    {
        "id": "taobao_baba_farm",
        "title": "淘宝芭芭农场",
        "script": "淘宝芭芭农场.py",
        "group": GROUP_DAILY,
        "description": "进入淘宝芭芭农场完成浇灌与可做任务。",
    },
    {
        "id": "taobao_coin",
        "title": "淘金币任务",
        "script": "淘金币任务.py",
        "group": GROUP_DAILY,
        "description": "完成淘金币频道的可做任务以领取金币。",
    },
    {
        "id": "tmall_tree",
        "title": "天猫摇钱树",
        "script": "天猫摇钱树.py",
        "group": GROUP_DAILY,
        "description": "完成天猫摇钱树每日任务。",
    },
    {
        "id": "alipay_farm",
        "title": "支付宝农场",
        "script": "支付宝农场.py",
        "group": GROUP_DAILY,
        "description": "完成支付宝蚂蚁庄园/农场相关可做任务。",
    },
    {
        "id": "taobao_cash_checkin",
        "title": "淘宝现金签到",
        "script": "淘宝现金签到.py",
        "group": GROUP_DAILY,
        "description": "淘宝红包签到页每日签到与领现金/元宝。",
    },
    {
        "id": "xianyu_cash_checkin",
        "title": "闲鱼现金签到",
        "script": "闲鱼现金签到.py",
        "group": GROUP_DAILY,
        "description": "闲鱼天天红包每日签到领现金。",
    },
    {
        "id": "alipay_checkin",
        "title": "支付宝打卡",
        "script": "支付宝打卡.py",
        "group": GROUP_DAILY,
        "description": "支付宝每日打卡任务页面完成可做打卡任务。",
    },
    {
        "id": "wanzhuan_alipay",
        "title": "玩赚支付宝",
        "script": "玩赚支付宝.py",
        "group": GROUP_DAILY,
        "description": "进入支付宝“玩赚支付宝”小程序完成每日签到。",
    },
    {
        "id": "boxian_checkin",
        "title": "玻弦打卡",
        "script": "玻弦打卡.py",
        "group": GROUP_DAILY,
        "description": "进入支付宝“玻弦打卡”小程序完成每日可做打卡任务。",
    },
]

# 限时活动（可能已过期/页面已改版）：单独区域展示，运行前提示，不默认勾选。
# 严格走与日常任务相同的 ID/文件白名单、更新与回退流程（validate_snapshot 校验存在）。
LIMITED_TASKS = [
    {
        "id": "limited_2024_double11",
        "title": "2024 淘宝双11",
        "script": "2024淘宝双11.py",
        "group": GROUP_LIMITED,
        "limited": True,
        "description": "限时活动脚本（2024 双11）。页面可能已变更或活动已结束，运行失败属预期。",
    },
    {
        "id": "limited_2025_618",
        "title": "2025 淘宝 618",
        "script": "2025淘宝618活动.py",
        "group": GROUP_LIMITED,
        "limited": True,
        "description": "限时活动脚本（2025 618）。页面可能已变更或活动已结束，运行失败属预期。",
    },
    {
        "id": "limited_2025_double11",
        "title": "2025 淘宝双11",
        "script": "2025淘宝双11.py",
        "group": GROUP_LIMITED,
        "limited": True,
        "description": "限时活动脚本（2025 双11）。页面可能已变更或活动已结束，运行失败属预期。",
    },
    {
        "id": "limited_2026_618",
        "title": "2026 淘宝 618",
        "script": "2026淘宝618活动.py",
        "group": GROUP_LIMITED,
        "limited": True,
        "description": "限时活动脚本（2026 618）。页面可能已变更或活动已结束，运行失败属预期。",
    },
]

# 全部受支持任务（UI 默认任务 + 限时活动）。顺序即运行顺序：日常在前、限时在后。
ALL_TASKS = DEFAULT_TASKS + LIMITED_TASKS

# 任务脚本运行需要、完整运行时发行版已捆绑的重型依赖说明
HEAVY_DEPS_NOTE = (
    "任务脚本依赖 torch / easyocr / ddddocr / opencv / uiautomator2 等运行时依赖；"
    "当前发行版已捆绑完整 CPU 运行时（内置 Python 3.12 与全部依赖、离线 EasyOCR 模型），"
    "任务可直接运行，不再显示“依赖待就绪”。桌面壳、ADB、同步、回退与任务编排不受影响。"
)

# 限时活动运行前提示（页面可能已改版/过期）
LIMITED_TASK_RUN_WARNING = (
    "“{title}”是限时活动脚本（可能已过期，页面可能已变更）。"
    "仍要继续运行吗？\n\n提示：若活动已结束，脚本会如实失败或无法完成真实任务，"
    "请以手机界面为准。"
)


def app_base_dir() -> str:
    """程序私有根目录。

    - 冻结(onedir)：exe 所在目录 = 发行根，含 platform-tools/。
    - 源码：仓库根目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    # desktop_app/constants.py -> 仓库根
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bundle_dir() -> str:
    """PyInstaller 数据资源根目录。

    - 冻结：sys._MEIPASS（onedir 下即 _internal/，内含打包进 datas 的 scripts/current）。
    - 源码：app_base_dir()（仓库根，seed 结构即仓库顶层）。
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and os.path.isdir(meipass):
            return meipass
    return app_base_dir()


def seed_current_dir() -> str:
    """出厂种子脚本所在目录（只读）。

    - 冻结：_internal/scripts/current（PyInstaller datas 打包的目标目录）。
    - 源码：仓库根本身（utils.py、任务脚本、img 就在顶层）。
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return os.path.join(meipass, DIR_SCRIPTS, DIR_CURRENT)
    return app_base_dir()


def default_data_dir() -> str:
    """数据目录：%LOCALAPPDATA%\\Coin11Helper（测试可覆盖）。程序目录只读也可运行。"""
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return os.path.abspath(override)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    return os.path.join(base, APP_NAME)


def asset_path(name: str) -> str:
    """品牌资源（assets/*）绝对路径。源码 = 仓库根/assets；冻结 = bundle_dir/assets。"""
    return os.path.join(bundle_dir(), "assets", name)


def logo_png_path() -> str:
    """标题区品牌 PNG（存在才返回路径，否则空串由调用方降级）。"""
    path = asset_path(ASSET_LOGO_PNG)
    return path if os.path.isfile(path) else ""


def logo_ico_path() -> str:
    """窗口/EXE 图标 .ico（生成物）。存在才返回路径，否则空串。"""
    path = asset_path(ASSET_LOGO_ICO)
    return path if os.path.isfile(path) else ""
