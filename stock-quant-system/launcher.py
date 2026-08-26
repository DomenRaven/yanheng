"""
一键启动器（打包为 `研衡启动器.exe` 供双击运行）。

**打包方案说明（如实交代取舍，对齐vibe coding八荣八耻第7条"坦诚存疑"）**：
本项目依赖 torch/lightgbm/duckdb 等大体积、含C扩展的库，若用PyInstaller把整个
Streamlit应用连同这些依赖一起冻结进单个exe，体积会膨胀到数GB，且Streamlit的静态
资源在frozen模式下经常需要额外hook才能找到、torch的CUDA动态库也容易在冻结后找不到——
这是社区内已知的痛点，不是本项目工程能力不足。因此采用更稳妥的方案：
只把「启动器」这一小段逻辑打包成exe，它在运行时调用项目自带 `.venv` 里已经装好完整
依赖的 python 解释器去跑 `streamlit run app.py`，效果同样是"双击一下就能用"，
但不需要把几个GB的深度学习库塞进一个exe文件里，更稳定也更容易维护。

打包命令（已在 `scripts/build_exe.py` 里封装，见该脚本docstring）：
    pyinstaller --onefile --name 研衡启动器 --console launcher.py
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

_PORT = 8501
_APP_ENTRY = "app.py"


def _base_dir() -> Path:
    """开发时(`python launcher.py`)用脚本所在目录；打包成exe后(`sys.frozen=True`)
    用exe文件所在目录——约定"启动器与项目根目录放在一起"，不依赖当前工作目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def main() -> int:
    base = _base_dir()
    venv_python = base / ".venv" / "Scripts" / "python.exe"
    app_entry = base / _APP_ENTRY

    print("=" * 60)
    print("  研衡 YanHeng —— 个人量化研究系统 一键启动器")
    print("=" * 60)
    print(f"项目目录：{base}")

    if not venv_python.exists():
        print(f"\n[错误] 未找到虚拟环境：{venv_python}")
        print("请先按照《开发者使用说明书.docx》完成一次性环境搭建：")
        print("  1) 在项目根目录执行：python -m venv .venv")
        print("  2) 激活后执行：pip install -r requirements.txt")
        print("完成后再双击本启动器。")
        input("\n按回车键退出...")
        return 1

    if not app_entry.exists():
        print(f"\n[错误] 未找到 {_APP_ENTRY}，请确认启动器和项目文件在同一目录下。")
        input("按回车键退出...")
        return 1

    if _port_open(_PORT):
        print(f"\n检测到 {_PORT} 端口已在运行（可能已经启动过一次），直接打开浏览器...")
        webbrowser.open(f"http://localhost:{_PORT}")
        return 0

    print("\n正在启动本地服务（首次启动可能需要10~30秒，请勿关闭本窗口）...")
    subprocess.Popen(
        [str(venv_python), "-m", "streamlit", "run", str(app_entry),
         "--server.port", str(_PORT), "--server.headless", "true"],
        cwd=str(base),
    )

    for _ in range(60):
        if _port_open(_PORT):
            break
        time.sleep(1)
    else:
        print("\n[提示] 服务启动超时，请查看本窗口是否有报错信息；也可手动打开浏览器访问 "
              f"http://localhost:{_PORT}")
        input("按回车键关闭本窗口（不会关闭已启动的服务）...")
        return 0

    print(f"\n服务已就绪，正在打开浏览器：http://localhost:{_PORT}")
    webbrowser.open(f"http://localhost:{_PORT}")
    print("\n提示：关闭本窗口不会停止服务；如需彻底停止，请在任务管理器结束 streamlit 相关的 python 进程。")
    input("按回车键关闭本启动器窗口（应用会继续在后台运行）...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
