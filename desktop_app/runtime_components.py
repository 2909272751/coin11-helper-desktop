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

# --- PyTorch CPU wheel index（官方；禁止伪装为国内镜像，SPEC 4）---
PYTORCH_CPU_INDEX_URL = "https://download.pytorch.org/whl/cpu"

# --- pip 下载源 allowlist（SPEC 3：固定三项，无自定义 URL；精确匹配）---
# 注意：清华路径是 /simple，阿里云结尾带 /，官方为 /simple——按下方精确串判定。
PIP_SOURCE_TUNA = "https://pypi.tuna.tsinghua.edu.cn/simple"
PIP_SOURCE_ALIYUN = "https://mirrors.aliyun.com/pypi/simple/"
PIP_SOURCE_OFFICIAL = "https://pypi.org/simple"
# 空串保留作“默认 PyPI”（等价官方）；历史组件 index_url 校验兼容。
ALLOWED_PIP_INDEX_URLS = frozenset({
    "", PYTORCH_CPU_INDEX_URL,
    PIP_SOURCE_TUNA, PIP_SOURCE_ALIYUN, PIP_SOURCE_OFFICIAL,
})

# 下载源“模式”常量（下载中心下拉值与 settings 持久化键；SPEC 3）
SOURCE_SMART = "smart"        # 智能：清华 -> 阿里 -> 官方顺序重试
SOURCE_TUNA = "tuna"          # 清华 PyPI
SOURCE_ALIYUN = "aliyun"      # 阿里云 PyPI
SOURCE_OFFICIAL = "official"  # PyPI 官方
DEFAULT_PIP_SOURCE = SOURCE_SMART
# settings 里持久化下载源模式的键
SETTINGS_PIP_SOURCE_KEY = "pip_source"
# 下载中心下拉可选项（顺序 = 界面展示顺序；值 = 模式）
PIP_SOURCE_CHOICES = (
    (SOURCE_SMART, "智能（清华→阿里→官方）"),
    (SOURCE_TUNA, "清华 PyPI"),
    (SOURCE_ALIYUN, "阿里云 PyPI"),
    (SOURCE_OFFICIAL, "PyPI 官方"),
)
# 模式 -> 有序 index 链（SPEC 3）：
#   * 智能：清华 -> 阿里 -> 官方 顺序重试；
#   * 手动源：所选优先，失败再尝试其余 allowlist，官方始终最后；
#   * 手动官方：官方即终极回退（其余镜像为国内源，不做官方失败后的镜像尝试）。
PIP_SOURCE_FALLBACK_CHAIN = {
    SOURCE_SMART: (PIP_SOURCE_TUNA, PIP_SOURCE_ALIYUN, PIP_SOURCE_OFFICIAL),
    SOURCE_TUNA: (PIP_SOURCE_TUNA, PIP_SOURCE_ALIYUN, PIP_SOURCE_OFFICIAL),
    SOURCE_ALIYUN: (PIP_SOURCE_ALIYUN, PIP_SOURCE_TUNA, PIP_SOURCE_OFFICIAL),
    SOURCE_OFFICIAL: (PIP_SOURCE_OFFICIAL,),
}

# pip 组 -> 锁定清单文本（安装时写入临时文件，用 -r 参数化传入；
# 不含任何 index 行——index 由安装器按所选/智能源显式 --index-url 提供；
# torch 组 CPU wheel 由安装器附加官方 CPU index，见 torch_cpu_extra_index）。
REQS_TEXT_BY_GROUP = {
    "automation": "\n".join(REQS_AUTOMATION) + "\n",
    "ocr": "\n".join(REQS_OCR) + "\n",
    # torch 组不写 extra-index（安装器始终给官方 CPU index）
    "torch": "\n".join(REQS_TORCH) + "\n",
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
    """普通 pip 下载源 URL 校验：HTTPS allowlist 精确成员之一或空串。"""
    return url in ALLOWED_PIP_INDEX_URLS


def pip_source_label(source_url: str) -> str:
    """把源 URL 映射为中文标签（用于日志“来源/切换原因”可读）。"""
    return {
        PIP_SOURCE_TUNA: "清华 PyPI",
        PIP_SOURCE_ALIYUN: "阿里云 PyPI",
        PIP_SOURCE_OFFICIAL: "PyPI 官方",
        "": "PyPI 官方",
    }.get(source_url, source_url or "PyPI 官方")


def pip_source_mode_label(mode: str) -> str:
    """下载源模式 -> 中文标签（日志/界面用）；未知回“智能”。"""
    return {
        SOURCE_SMART: "智能（清华→阿里→官方）",
        SOURCE_TUNA: "清华 PyPI",
        SOURCE_ALIYUN: "阿里云 PyPI",
        SOURCE_OFFICIAL: "PyPI 官方",
    }.get(mode, "智能（清华→阿里→官方）")


def resolve_pip_source_chain(mode: str) -> tuple:
    """按下载源模式返回有序 index URL 链；未知模式回退默认链。

    只返回 allowlist 成员；返回链始终保证“官方源在链尾（若链中有官方）”。
    """
    chain = PIP_SOURCE_FALLBACK_CHAIN.get(mode)
    if not chain:
        chain = PIP_SOURCE_FALLBACK_CHAIN[DEFAULT_PIP_SOURCE]
    out = [u for u in chain if is_allowed_index_url(u)]
    return tuple(out)


def source_chain_for_pip_group(group: str, mode: str) -> tuple:
    """返回某 pip 组在所选模式下的普通 PyPI 下载源回退链（SPEC 3）。

    普通 pip 依赖（含 torch 的传递依赖）按所选/智能源链（清华→阿里→官方，
    官方始终最后）；torch 组的 CPU wheel 固定由安装器附加官方 CPU index
    （见 torch_cpu_extra_index），绝不伪装为国内镜像（SPEC 4）。
    """
    return resolve_pip_source_chain(mode)


def torch_cpu_extra_index() -> str:
    """torch 组安装必须附加的官方 CPU wheel index（download.pytorch.org/whl/cpu）。"""
    return PYTORCH_CPU_INDEX_URL


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
