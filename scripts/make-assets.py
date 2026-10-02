#!/usr/bin/env python3
"""生成 README 演示 GIF 与 GitHub 社交预览图（docs/feature/feature-distribution.md §5.1.3）。

前置条件：
  - 在 macOS 上运行：字体固定用 /System/Library/Fonts/Menlo.ttc 与 Helvetica.ttc，找不到就报错退出，
    不回落到其它字体——换字体会让图片尺寸和排版悄悄变化。
  - Python 3.8+，已安装 Pillow（pip install Pillow）；不需要网络。
  - 从仓库任意目录运行均可：用本仓库 src/ 下的 multi-claude，输出默认写到仓库的 docs/assets/。

做法：在临时目录里建 HOME、假 claude、返回 0 的假 security（让 LOGIN 显示 keychain）和两个示例账号
（work、personal）的示例用量缓存，实际运行 multi-claude 抓取真实输出，再按终端样式逐帧绘制。
输出里只把临时 HOME 的路径显示成 ~。临时目录结束时删除，不碰真实的 ~/.claude、~/.cc 与钥匙串。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MONO_FONT = "/System/Library/Fonts/Menlo.ttc"
SANS_FONT = "/System/Library/Fonts/Helvetica.ttc"

# 终端配色：深色背景，提示符绿色、注释灰色、输出浅色。
BG = (30, 30, 46)
FG = (205, 214, 244)
PROMPT = (166, 227, 161)
COMMENT = (127, 132, 156)
TITLE_BAR = (49, 50, 68)

EXAMPLES = """examples:
  make-assets.py                         write docs/assets/demo.gif and social-preview.png
  make-assets.py --out /tmp/assets       write the two images to another directory
  make-assets.py --print                 only print the captured terminal session
"""


def usage_cache(five, seven, now_ms):
    """示例用量：Claude Code 自己写在 .claude.json 里的缓存格式（multi-claude usage 读取它）。"""
    hour, day = 3600 * 1000, 24 * 3600 * 1000

    def iso(ms):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000))
    return {"cachedUsageUtilization": {"fetchedAtMs": now_ms, "utilization": {
        "five_hour": {"utilization": five, "resets_at": iso(now_ms + 2 * hour)},
        "seven_day": {"utilization": seven, "resets_at": iso(now_ms + 3 * day)}}}}


def capture_session():
    """在临时 HOME 中运行演示命令，返回 [(命令或注释, 输出行列表)]。"""
    tmp = tempfile.mkdtemp(prefix="mcl-assets-")
    try:
        home = os.path.join(tmp, "home")
        fakebin = os.path.join(tmp, "bin")
        os.makedirs(home)
        os.makedirs(fakebin)
        # 假 claude；假 security 总是返回 0，让 LOGIN 列显示 keychain（演示不碰真实钥匙串）。
        for name in ("claude", "security"):
            with open(os.path.join(fakebin, name), "w") as handle:
                handle.write("#!/bin/sh\nexit 0\n")
            os.chmod(os.path.join(fakebin, name), 0o755)
        # 启动命令目录放进 PATH：否则 add 会打印“不在 PATH 中”的提示，演示里不需要它。
        env = {"HOME": home, "PATH": os.pathsep.join([os.path.join(home, ".local", "bin"), fakebin, "/usr/bin", "/bin"]),
               "PYTHONPATH": os.path.join(ROOT, "src"), "LC_ALL": "en_US.UTF-8", "SHELL": "/bin/zsh"}

        def run(*args):
            proc = subprocess.run([sys.executable, "-m", "multi_claude"] + list(args), env=env, cwd=tmp,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                                  stdin=subprocess.DEVNULL, check=True)
            # 临时 HOME 的绝对路径对读者没有意义，显示成 ~；除此之外输出原样保留。
            return proc.stdout.rstrip("\n").replace(home, "~").splitlines()

        session = [("multi-claude add work --proxy 7901", run("add", "work", "--proxy", "7901")),
                   ("multi-claude add personal", run("add", "personal"))]
        # 示例账号的用量：真实使用时由 Claude Code 自己写入。
        now_ms = int(time.time() * 1000)
        for name, five, seven in (("work", 23, 41), ("personal", 5, 12)):
            with open(os.path.join(home, ".cc", name, ".claude.json"), "w") as handle:
                json.dump(usage_cache(five, seven, now_ms), handle)
        session.append(("# log in once per account: multi-claude login work", []))
        session.append(("multi-claude", run()))
        session.append(("# claude-work and claude-personal now run side by side", []))
        return session
    finally:
        shutil.rmtree(tmp)


def load_fonts():
    for path in (MONO_FONT, SANS_FONT):
        if not os.path.exists(path):
            sys.exit("font not found: {} (this script runs on macOS only, see -h)".format(path))
    from PIL import ImageFont
    return (ImageFont.truetype(MONO_FONT, 15), ImageFont.truetype(SANS_FONT, 64), ImageFont.truetype(SANS_FONT, 30),
            ImageFont.truetype(MONO_FONT, 21))


def make_gif(session, out_path, mono):
    from PIL import Image, ImageDraw

    width, line_h, pad, top = 940, 21, 18, 34
    rows = sum(1 + len(output) for _, output in session)
    height = top + pad * 2 + rows * line_h
    lines = []  # 已经显示的 (颜色, 文本)
    frames, durations = [], []

    def render(cursor_text=None):
        image = Image.new("RGB", (width, height), BG)
        draw = ImageDraw.Draw(image)
        draw.rectangle([0, 0, width, top - 8], fill=TITLE_BAR)
        for i, color in enumerate(((243, 139, 168), (249, 226, 175), (166, 227, 161))):
            draw.ellipse([14 + i * 20, 9, 26 + i * 20, 21], fill=color)
        y = top + pad
        shown = lines + ([cursor_text] if cursor_text else [])
        for color, text in shown:
            if color == PROMPT:
                draw.text((pad, y), "$", font=mono, fill=PROMPT)
                draw.text((pad + 18, y), text, font=mono, fill=FG)
            else:
                draw.text((pad, y), text, font=mono, fill=color)
            y += line_h
        return image

    def add(image, ms):
        frames.append(image)
        durations.append(ms)

    add(render(), 600)
    for command, output in session:
        is_comment = command.startswith("#")
        color = COMMENT if is_comment else PROMPT
        # 逐字出现，每 2 个字符一帧；注释整行出现。
        if not is_comment:
            for end in range(2, len(command) + 2, 2):
                add(render((color, command[:end] + "_")), 45)
        lines.append((color, command))
        add(render(), 500 if not is_comment else 1400)
        if output:
            lines.extend((FG, line) for line in output)
            add(render(), 1800)
    add(render(), 3500)
    # 64 色：32 色时窗口按钮的红黄绿会被量化成灰色。
    palette_frames = [frame.convert("P", palette=Image.ADAPTIVE, colors=64) for frame in frames]
    palette_frames[0].save(out_path, save_all=True, append_images=palette_frames[1:], duration=durations,
                           loop=0, optimize=True, disposal=1)


def make_social(session, out_path, mono, title_font, tagline_font):
    """社交预览图：链接被分享到 X、Reddit、Slack 时显示的缩略图。只放表头和账号行，字号要大到缩略图里也看得清。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1280, 640), BG)
    draw = ImageDraw.Draw(image)
    draw.text((80, 70), "multi-claude", font=title_font, fill=FG)
    draw.text((82, 155), "Run several Claude Code accounts side by side", font=tagline_font, fill=COMMENT)
    box = [80, 250, 1200, 500]
    draw.rounded_rectangle(box, radius=14, fill=(24, 24, 37), outline=TITLE_BAR, width=2)
    list_output = next(output for command, output in session if command == "multi-claude")
    table = [line for line in list_output if line.startswith(("NAME", "work", "personal"))]
    y = box[1] + 32
    draw.text((box[0] + 32, y), "$", font=mono, fill=PROMPT)
    draw.text((box[0] + 58, y), "multi-claude", font=mono, fill=FG)
    y += 16
    for line in table:
        y += 40
        draw.text((box[0] + 32, y), line, font=mono, fill=FG)
    draw.text((82, 545), "github.com/jakoes-wu/multi-claude", font=tagline_font, fill=PROMPT)
    image.save(out_path, optimize=True)


def main():
    parser = argparse.ArgumentParser(
        description="Generate docs/assets/demo.gif and docs/assets/social-preview.png from a real "
                    "multi-claude session in a temporary HOME (macOS, needs Pillow).",
        epilog=EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=os.path.join(ROOT, "docs", "assets"),
                        help="output directory (default: docs/assets in this repository)")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="print the captured session and exit without drawing")
    args = parser.parse_args()

    session = capture_session()
    if args.print_only:
        for command, output in session:
            print(command if command.startswith("#") else "$ " + command)
            for line in output:
                print(line)
        return 0
    try:
        import PIL  # noqa: F401
    except ImportError:
        sys.exit("Pillow is required: pip install Pillow")
    mono, title_font, tagline_font, social_mono = load_fonts()
    os.makedirs(args.out, exist_ok=True)
    gif = os.path.join(args.out, "demo.gif")
    png = os.path.join(args.out, "social-preview.png")
    make_gif(session, gif, mono)
    make_social(session, png, social_mono, title_font, tagline_font)
    print("wrote {} ({} KB)".format(gif, os.path.getsize(gif) // 1024))
    print("wrote {} ({} KB)".format(png, os.path.getsize(png) // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
