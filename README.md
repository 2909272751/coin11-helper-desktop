# coin11-tb
使用uiautomator2自动化完成淘宝双11的金币任务，淘金币任务，芭芭农场任务，闲鱼任务，支付宝打卡任务。

需要安装adb，自行百度教程。
使用教程请看抖音
```
3.53 04/14 X@z.TY bnD:/ 自动化完成淘宝任务教程  https://v.douyin.com/i5xNsWVx/ 复制此链接，打开Dou音搜索，直接观看视频！
```
或者快手
```
https://v.kuaishou.com/nGmUFX 自动化完成淘宝任务教程 该作品在快手被播放过1次，点击链接，打开【快手极速版】直接观看！
```

先执行`pip install -r requirements.txt`安装依赖，然后执行对应的python文件即可。

## 桌面版（Coin11 助手）

本仓库还包含 Windows x64 桌面封装（非官方）：0.2.0 起为**完整运行时发行版**，
发行目录内置 Python 3.12、ADB、uiautomator2、OpenCV、ddddocr、EasyOCR、
CPU 版 PyTorch 及离线模型，用户无需另装 Python/Git/ADB/pip 包即可运行日常任务。

0.3.0 起新增：

- **实时日志**：脚本 stdout/stderr 逐行低延迟显示（不等任务退出）；日志区支持
  “自动滚动”与“仅看错误”过滤；
- **断线保护**：任务运行期间每约 2 秒检查所选设备；连续两次缺失/离线/未授权时
  自动停止当前任务与整个队列，并如实标注结果；
- **任务分组**：日常任务按需展示，另有可展开的**限时活动**分组（2024 双11、
  2025 618、2025 双11、2026 618 等，可能已过期，运行前会提示）；
- **界面与图标**：现代浅色卡片布局 + 品牌图标（`assets/coin11-logo-v1.png` 为源，
  构建生成 `.ico`，均不覆盖源文件）。

- 使用说明：见 `README_DESKTOP.md`；
- 构建完整运行时发行版：`powershell -ExecutionPolicy Bypass -File scripts\build.ps1`
  （需要联网下载 CPU PyTorch 与 EasyOCR 模型；产出
  `dist\Coin11助手-0.3.0-windows-x64.zip`，体积明显大于 55 MB 属预期）；
- 桌面运行依赖锁定：`requirements-desktop-runtime.txt`（仅构建用，不安装到全局）。

## 点赞历史

[![Star History Chart](https://star-history.dera.page/svg?repos=czl0325/coin11-tb&type=Date)](https://star-history.dera.page/#czl0325/coin11-tbt&Date)
<br><br>
