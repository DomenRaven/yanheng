"""
一键打包 `launcher.py` 为 `研衡启动器.exe`（放在项目根目录，双击即可启动全部服务）。

用法（需先在 `.venv` 里 `pip install pyinstaller`）：
    .venv\\Scripts\\python.exe scripts\\build_exe.py

**为什么单独提取成脚本而不是让用户直接背 pyinstaller 命令**：可复用、可重复执行
（模型/依赖升级后重新打包一次即可），也把"打包完清理build目录"这类收尾工作固化下来，
不用每次手动清理（对齐quant-dev-loop"任务做完立刻清理"的纪律）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_EXE_NAME = "研衡启动器"


def main() -> int:
    launcher = _ROOT / "launcher.py"
    if not launcher.exists():
        print(f"[错误] 未找到 {launcher}")
        return 1

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--console", "--noconfirm",
        "--name", _EXE_NAME,
        "--distpath", str(_ROOT / "_dist_tmp"),
        "--workpath", str(_ROOT / "_build_tmp"),
        "--specpath", str(_ROOT / "_build_tmp"),
        str(launcher),
    ]
    print("执行：", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(_ROOT))
    if result.returncode != 0:
        print("[错误] PyInstaller 打包失败，见上方日志。")
        return result.returncode

    built_exe = _ROOT / "_dist_tmp" / f"{_EXE_NAME}.exe"
    target_exe = _ROOT / f"{_EXE_NAME}.exe"
    if not built_exe.exists():
        print(f"[错误] 打包产物未找到：{built_exe}")
        return 1
    shutil.copy2(built_exe, target_exe)
    print(f"\n打包完成：{target_exe}")

    # 清理PyInstaller的中间产物，只保留最终exe，不在仓库里堆积build缓存
    for tmp_dir in (_ROOT / "_dist_tmp", _ROOT / "_build_tmp"):
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
    print("已清理打包中间文件（_dist_tmp / _build_tmp）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
