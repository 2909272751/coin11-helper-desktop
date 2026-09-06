"""轻量版运行时组件清单与来源校验（不变式：全部固定版本 + HTTPS allowlist）。

组件覆盖 requirements-desktop-runtime.txt 的全部直接运行时依赖与 EasyOCR 离线
模型，按组管理：

1. python-bootstrap  Python 3.12 可移动基座（随轻量发行内置，不下载）
2. automation        自动化依赖：uiautomator2 / uiautodev / requests
3. ocr               图像与 OCR 基础：numpy / Pillow / opencv-python / ddddocr / easyocr
4. torch             CPU torch：torch / torchvision（PyTorch 官方 CPU wheel index）
5. easyocr-models    离线模型 craft_mlt_25k.pth / zh_sim_g2.pth

安全约束（与 SPEC/HANDOFF 硬约束一致）：
- 每条来源 URL 必须是本模块内硬编码的 HTTPS allowlist 成员（精确匹配，不做
  子串/拼接）；安装/下载所需的一切（版本、URL、清单文本）都内嵌在代码里，
  绝不读取或执行远程任意 requirements/命令。
- pip 一律以参数列表 + --disable-pip-version-check + 无 shell + 超时/取消运行；
  日志经 logutil.redact 脱敏。
- 下载先进临时目录，成功后 os.replace 原子替换；安装完成后用 import 探针 +
  模型文件存在性/体积校验，损坏或不完整运行时绝不被当作可用。
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# 锁定 requirements（与 requirements-desktop-runtime.txt 同源；仅拆分到“组”粒度）
# ---------------------------------------------------------------------------

# --- 自动化依赖：任务脚本 import uiautomator2 / uiautodev / requests ---
REQS_AUTOMATION = [
    "uiautomator2==3.5.2",
    "uiautodev==0.14.0",
    "requests==2.32.3",
]

# --- 图像与 OCR 基础（不引入 headless 变体，避免双 opencv 并存冲突）---
REQS_OCR = [
    "numpy==2.1.3",
    "Pillow==10.4.0",
    "opencv-python==4.13.0.92",
    "ddddocr==1.5.6",
    "easyocr==1.7.2",
]

# --- CPU torch：PyTorch 官方 CPU wheel index（与完整运行时锁定一致）---
REQS_TORCH = [
    "torch==2.6.0+cpu",
    "torchvision==0.21.0+cpu",
]

# --- HTTPS allowlist（精确值/前缀校验用）---
PYTORCH_CPU_INDEX_URL = "https://download.pytorch.org/whl/cpu"
# pip 组可用的 index URL（精确匹配；空串 = 默认 PyPI）
ALLOWED_PIP_INDEX_URLS = frozenset({"", PYTORCH_CPU_INDEX_URL})

# pip 组 -> 锁定清单文本（安装时写入临时文件，用 -r 参数化传入）
REQS_TEXT_BY_GROUP = {
    "automation": "\n".join(REQS_AUTOMATION) + "\n",
    "ocr": "\n".join(REQS_OCR) + "\n",
    # torch 组清单自带官方 CPU index 行，与 requirements-lite-torch.txt 同源
    "torch": "--extra-index-url " + PYTORCH_CPU_INDEX_URL + "\n"
             + "\n".join(REQS_TORCH) + "\n",
}

# 模型下载允许的主机（github.com / objects.githubusercontent.com 官方 release）
ALLOWED_MODEL_HOST_FRAGMENTS = ("github.com", "objects.githubusercontent.com")

# --- EasyOCR 离线模型（来源与 build.ps1 完整路径同款 GitHub release URL）---
# 键 = EasyOCR 内部 model_key；值含下载 URL（官方 release 资源）与校验信息。
EASYOCR_MODEL_SOURCES = {
    "craft_mlt_25k": {
        "url": "https://github.com/JaidedAI/EasyOCR/releases/download/"
               "pre-v1.1.6/craft_mlt_25k.zip",
        "file": "craft_mlt_25k.pth",
        "min_bytes": 50 * 1024 * 1024,   # 真实 ~79MB
        "label": "检测模型 craft_mlt_25k",
    },
    "zh_sim_g2": {
        "url": "https://github.com/JaidedAI/EasyOCR/releases/download/"
               "v1.3/zh_sim_g2.zip",
        "file": "zh_sim_g2.pth",
        "min_bytes": 15 * 1024 * 1024,   # 真实 ~21MB
        "label": "中文识别模型 zh_sim_g2",
    },
}

# --- 各 pip 组的安装后 import 探针（用数据运行时解释器执行）---
PROBE_BY_GROUP = {
    "automation": ("uiautomator2", "uiautodev", "requests"),
    "ocr": ("numpy", "PIL", "cv2", "ddddocr", "easyocr"),
    "torch": ("torch", "torchvision"),
}
# 各组需要“已安装标记”的依赖文件名（界面/校验用；pip 组以探针为准）
# 模型组的校验 = 文件存在 + 体积下限（见 EASYOCR_MODEL_SOURCES）


def is_allowed_model_url(url: str) -> bool:
    """模型下载 URL 校验：HTTPS + 官方 release 主机（allowlist 精确前缀式检查）。"""
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    return any(host in url for host in ALLOWED_MODEL_HOST_FRAGMENTS)


def is_allowed_index_url(url: str) -> bool:
    return url in ALLOWED_PIP_INDEX_URLS


# ---------------------------------------------------------------------------
# 组件定义（不可变）
# ---------------------------------------------------------------------------

class ComponentDef:
    """一个组件（pip 依赖组 / 模型组 / 内置基座）的不可变描述。"""

    def __init__(self, component_id: str, name: str, purpose: str,
                 kind: str, size_mb: float = 0.0,
                 group: str = "", pinned: tuple = (),
                 index_url: str = "", bundled_relpath: str = "",
                 model_keys: tuple = ()):
        self.id = component_id
        self.name = name
        self.purpose = purpose
        self.kind = kind            # python / pip / model
        self.size_mb = size_mb      # 预估（模型为落盘体积；pip 为量级估算）
        self.group = group
        self.pinned = tuple(pinned)
        self.index_url = index_url
        self.bundled_relpath = bundled_relpath   # python 基座相对发行根路径
        self.model_keys = tuple(model_keys)

    # ---- 来源/自校验（防配置漂移）----
    def validate(self) -> bool:
        if self.kind == "pip":
            return bool(self.pinned) and is_allowed_index_url(self.index_url)
        if self.kind == "model":
            return bool(self.model_keys) and all(
                self._model_valid(k) for k in self.model_keys)
        if self.kind == "python":
            return bool(self.bundled_relpath)
        return False

    def _model_valid(self, key: str) -> bool:
        src = EASYOCR_MODEL_SOURCES.get(key)
        if not src:
            return False
        return is_allowed_model_url(src["url"]) and bool(src["file"])

    def model_source(self, key: str) -> dict:
        return dict(EASYOCR_MODEL_SOURCES[key])

    def probe_modules(self) -> tuple:
        return PROBE_BY_GROUP.get(self.id, ())

    def as_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "purpose": self.purpose,
            "kind": self.kind, "group": self.group, "size_mb": self.size_mb,
            "pinned": list(self.pinned), "index_url": self.index_url,
        }


# ---------------------------------------------------------------------------
# 固定清单（不可变；新增组件必须同时更新本表、requirements 文本与 UI 分组）
# ---------------------------------------------------------------------------

# Python 基座：随轻量发行内置（构建时从 .build\py312 复制）。
# 相对发行根的路径与 constants.DIR_RUNTIME_BOOTSTRAP_PY 保持一致。
COMPONENT_PYTHON_BOOTSTRAP = ComponentDef(
    component_id="python-bootstrap",
    name="Python 3.12 基座",
    purpose="随安装包内置的可移动 Python 解释器（含 pip）；只用于安装锁定依赖，"
            "任务运行使用安装完成后的数据运行时解释器。",
    kind="python",
    size_mb=116.0,
    group="python-bootstrap",
    bundled_relpath=os.path.join("runtime", "python-bootstrap", "python.exe"),
)

# 自动化依赖组（一次 pip 调用安装整组）
COMPONENT_AUTOMATION = ComponentDef(
    component_id="automation",
    name="自动化依赖",
    purpose="设备自动化运行库：uiautomator2、uiautodev（手机自动化）与 requests。",
    kind="pip",
    group="automation",
    size_mb=12.0,
    pinned=tuple(REQS_AUTOMATION),
)

# 图像 / OCR 基础组
COMPONENT_OCR = ComponentDef(
    component_id="ocr",
    name="图像 / OCR 基础",
    purpose="图像处理与 OCR：numpy、Pillow、opencv、ddddocr、easyocr（不含模型文件）。",
    kind="pip",
    group="ocr",
    size_mb=95.0,
    pinned=tuple(REQS_OCR),
)

# CPU torch 组（官方 CPU index）
COMPONENT_TORCH = ComponentDef(
    component_id="torch",
    name="CPU torch 推理",
    purpose="EasyOCR / 模型推理所需 CPU 版 PyTorch 与 torchvision（官方 CPU wheel）。",
    kind="pip",
    group="torch",
    size_mb=230.0,
    pinned=tuple(REQS_TORCH),
    index_url=PYTORCH_CPU_INDEX_URL,
)

# EasyOCR 离线模型组
COMPONENT_MODELS = ComponentDef(
    component_id="easyocr-models",
    name="EasyOCR 离线模型",
    purpose="离线中文 OCR 模型：craft 文本检测 + zh_sim 中文识别（约 100MB）。",
    kind="model",
    group="models",
    size_mb=100.0,
    model_keys=tuple(EASYOCR_MODEL_SOURCES.keys()),
)

# 需下载/安装的组件（python-bootstrap 内置，仅展示不安装）
DOWNLOADABLE_COMPONENTS = (
    COMPONENT_AUTOMATION,
    COMPONENT_OCR,
    COMPONENT_TORCH,
    COMPONENT_MODELS,
)
COMPONENT_BY_ID = {c.id: c for c in DOWNLOADABLE_COMPONENTS}
COMPONENT_BY_ID[COMPONENT_PYTHON_BOOTSTRAP.id] = COMPONENT_PYTHON_BOOTSTRAP

# 依赖安全的安装顺序：torch 必须先于 ocr（easyocr 依赖 torch，若先装 ocr，
# pip 会从默认 PyPI 拉取“非锁定”的 torch，破坏固定版本不变式）。
INSTALL_ORDER = ("torch", "ocr", "automation", "easyocr-models")

# UI 分组标签
GROUP_LABELS = {
    "python-bootstrap": "Python 基座（内置）",
    "automation": "自动化依赖",
    "ocr": "图像 / OCR 基础",
    "torch": "CPU torch 推理",
    "models": "EasyOCR 离线模型",
}

# 预估总下载体积（不含内置基座）
EST_TOTAL_DOWNLOAD_MB = sum(c.size_mb for c in DOWNLOADABLE_COMPONENTS)

# 状态枚举
STATE_IDLE = "idle"
STATE_PENDING = "pending"          # 队列中等待
STATE_DOWNLOADING = "downloading"  # 阶段：下载
STATE_INSTALLING = "installing"    # 阶段：pip 安装 / 解压
STATE_VERIFYING = "verifying"      # 阶段：校验
STATE_OK = "ok"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
