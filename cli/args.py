"""
cli/args.py
===========
Argparse setup, startup banner, and interactive command loop for ASCILINE.

The ASCII_LOGO, HELP_TEXT, command_loop(), and print_status() functions
are moved here from stream_server.py to keep the server file focused on
FastAPI + WebSocket logic only.
"""

import os
import sys
import threading
from cli.profiles import PROFILE_NAMES, apply_profile

os.system("")  # ANSI on Windows

ASCII_LOGO = "\033[36m" + r"""
    _    ____   ____ ___ _     ___ _   _ _____ 
   / \  / ___| / ___|_ _| |   |_ _| \ | | ____|
  / _ \ \___ \| |    | || |    | ||  \| |  _|  
 / ___ \ ___) | |___ | || |___ | || |\  | |___ 
/_/   \_\____/ \____|___|_____|___|_| \_|_____|
""" + "\033[0m"

HELP_TEXT = "\033[1;37m" + """
╔═══════════════════════════════════════════════════╗
║               ASCILINE  —  COMMANDS               ║
╠═══════════════════════════════════════════════════╣
║                                                   ║
║  \033[36m/help\033[1;37m      Show this help message               ║
║  \033[36m/status\033[1;37m    Show current server & playback info  ║
║  \033[36m/quit\033[1;37m      Stop the server and exit             ║
║                                                   ║
╠═══════════════════════════════════════════════════╣
║             CLI LAUNCH OPTIONS                    ║
╠═══════════════════════════════════════════════════╣
║                                                   ║
║  \033[33m─── Profile (quick start) ───\033[1;37m                   ║
║  \033[32m--profile\033[1;37m   terminal|web|cinematic|bandwidth|ai  ║
║                                                   ║
║  \033[33m─── Source ───\033[1;37m                                  ║
║  \033[32mvideo\033[1;37m          Path or URL to a video           ║
║  \033[32m--playlist\033[1;37m     JSON playlist file               ║
║  \033[32m--folder\033[1;37m       Play all videos in a folder      ║
║                                                   ║
║  \033[33m─── Render ───\033[1;37m                                  ║
║  \033[32m--mode\033[1;37m  \033[35m1-5\033[1;37m    Color quality                    ║
║     1=B&W  2=512c  3=32Kc  4=262Kc  5=16M        ║
║  \033[32m--pixel\033[1;37m        Pixel block mode (with mode 2-5) ║
║  \033[32m--cols\033[1;37m  \033[35mN\033[1;37m      Grid columns  (default: 200)     ║
║  \033[32m--rows\033[1;37m  \033[35mN\033[1;37m      Grid rows     (default: auto)    ║
║  \033[32m--char-ratio\033[1;37m   Terminal char aspect ratio        ║
║                                                   ║
║  \033[33m─── Playback ───\033[1;37m                                ║
║  \033[32m--vol\033[1;37m   \033[35m0-5\033[1;37m    Volume (0=mute, 1=normal, 5=2x)  ║
║  \033[32m--loop\033[1;37m         Loop the playlist infinitely     ║
║  \033[32m--quality\033[1;37m \033[35mlvl\033[1;37m  Codec quality (lossless,low,etc) ║
║                                                   ║
║  \033[33m─── Server ───\033[1;37m                                  ║
║  \033[32m--port\033[1;37m  \033[35mN\033[1;37m      Server port    (default: 8000)    ║
║  \033[32m--debug\033[1;37m        Show bandwidth stats (RAW/WIRE)  ║
║                                                   ║
╚═══════════════════════════════════════════════════╝
""" + "\033[0m"


def print_status(app_state) -> None:
    queue = getattr(app_state, "queue", [])
    idx   = getattr(app_state, "current_index", 0)
    loop  = getattr(app_state, "loop", False)
    print(f"\n\033[1;37m{'═'*55}\033[0m")
    print(f" \033[32m►\033[0m \033[1mQueue\033[0m      : {len(queue)} video(s)")
    print(f" \033[32m►\033[0m \033[1mNow Playing\033[0m: {idx + 1}/{len(queue)}")
    if queue and idx < len(queue):
        entry = queue[idx]
        px = ' \033[35m[PIXEL]\033[0m' if entry.get('pixel') else ''
        print(f" \033[32m►\033[0m \033[1mVideo\033[0m      : \033[36m{entry['video'][:80]}\033[0m")
        print(f" \033[32m►\033[0m \033[1mSettings\033[0m   : mode={entry['mode']}{px} vol={entry['vol']}")
    print(f" \033[32m►\033[0m \033[1mLoop\033[0m       : {'ON' if loop else 'OFF'}")
    print(f"\033[1;37m{'═'*55}\033[0m\n")


def command_loop(app_state) -> None:
    """Interactive REPL running on a daemon thread alongside uvicorn."""
    print(f" \033[90mType \033[36m/help\033[90m for available commands.\033[0m\n")
    while True:
        try:
            cmd = input().strip().lower()
            if cmd in ('/help', 'help'):
                print(HELP_TEXT)
            elif cmd in ('/status', 'status'):
                print_status(app_state)
            elif cmd in ('/quit', 'quit', 'exit'):
                print("\n \033[33m⏹  Shutting down ASCILINE...\033[0m\n")
                os._exit(0)
            elif cmd:
                print(f" \033[90mUnknown command: '{cmd}'. Type \033[36m/help\033[90m for options.\033[0m")
        except (EOFError, KeyboardInterrupt):
            print("\n \033[33m⏹  Shutting down ASCILINE...\033[0m\n")
            os._exit(0)


def build_arg_parser():
    """
    Build and return the ArgumentParser for stream_server.py.
    Extracted here so it can be tested independently.
    """
    import argparse
    parser = argparse.ArgumentParser(
        description=f"{ASCII_LOGO}\nReal-Time ASCII Web Server\n"
                    "Stream local videos or URLs to your browser.",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    src = parser.add_argument_group('\033[33mSource\033[0m')
    src.add_argument("video", nargs="?", default="video.mp4")
    src.add_argument("--playlist", metavar="FILE", default=None)
    src.add_argument("--folder",   metavar="DIR",  default=None)

    render = parser.add_argument_group('\033[33mRender\033[0m')
    render.add_argument("--mode",       type=int,   choices=[1,2,3,4,5], default=1)
    render.add_argument("--pixel",      action="store_true", default=False)
    render.add_argument("--cols",       type=int,   default=None)
    render.add_argument("--rows",       type=int,   default=0)
    render.add_argument("--char-ratio", type=float, default=0.45,
                        help="Terminal character aspect ratio (default 0.45). "
                             "Tune per font: Consolas≈0.42, Courier≈0.50")

    playback = parser.add_argument_group('\033[33mPlayback\033[0m')
    playback.add_argument("--vol",     type=int, default=1)
    playback.add_argument("--loop",    action="store_true", default=False)
    playback.add_argument("--quality", choices=["lossless","high","balanced","low"],
                          default="lossless")
    playback.add_argument("--no-thumbnails", action="store_true", default=False)

    preset = parser.add_argument_group('\033[33mProfile\033[0m')
    preset.add_argument("--profile", choices=PROFILE_NAMES, default=None,
                        help="Named preset (overrides mode/cols/quality/pixel/vol defaults).")

    srv = parser.add_argument_group('\033[33mServer\033[0m')
    srv.add_argument("--host",  default="127.0.0.1")
    srv.add_argument("--port",  type=int, default=8000)
    srv.add_argument("--debug", action="store_true", default=False)

    return parser
