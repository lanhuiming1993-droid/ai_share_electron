from __future__ import annotations

from pathlib import Path


LOGIN_TARGET = Path("/app/driver/wx.py")
LOGIN_OLD = """            # 等待页面完全加载
            print_info("正在加载登录页面...")
            await page.wait_for_load_state("networkidle")
"""
LOGIN_NEW = """            # 微信公众平台登录页会保留长连接；短暂等待后即可截取二维码。
            print_info("正在加载登录页面...")
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                print_warning("登录页保持网络活动，继续生成二维码")
"""
MAIN_TARGET = Path("/app/main.py")
ENV_DUMP_OLD = """    print("环境变量:")
    for k,v in os.environ.items():
        print(f"{k}={v}")
"""
ENV_DUMP_NEW = """    print("AlphaDesk suppressed the upstream environment dump.")
"""


def replace_once(target: Path, old: str, new: str, label: str) -> None:
    content = target.read_text(encoding="utf-8")
    if old not in content:
        raise RuntimeError(f"WeRSS {label} compatibility patch no longer matches {target}")
    target.write_text(content.replace(old, new, 1), encoding="utf-8")


def main() -> None:
    replace_once(LOGIN_TARGET, LOGIN_OLD, LOGIN_NEW, "QR login")
    replace_once(MAIN_TARGET, ENV_DUMP_OLD, ENV_DUMP_NEW, "environment log")
    print("AlphaDesk applied the WeRSS runtime compatibility patches.")


if __name__ == "__main__":
    main()
