"""
ascii_video_player2.py
======================
Standalone terminal ASCII video player — entry point.

This file is intentionally thin. All rendering logic lives in:
  core/decoder.py  — VideoDecoder, AsciiMapper

Usage:
  python ascii_video_player2.py video.mp4 --cols 120
  python ascii_video_player2.py video.mp4 -q 2 --cols 160

Quality flag (-q / --quality):
  0 = max quality  (full 24-bit color, default)
  1 = 128 levels/channel
  2 = 64 levels/channel  (fast, some banding)
  3 = 32 levels/channel  (max speed)
"""

import sys
import time
import shutil
import argparse
import os

os.system("")  # Enable ANSI on Windows

# ── Import from canonical core module ────────────────────────────────
from core.decoder import VideoDecoder, AsciiMapper


class TerminalRenderer:
    """
    Orchestrates: VideoDecoder → AsciiMapper → stdout.

    Auto-fits grid to terminal size unless --cols is specified.
    Centers output both horizontally and vertically.
    Uses CHAR_RATIO to correct for terminal font aspect ratio —
    tune with --char-ratio per font (Consolas≈0.42, Courier≈0.50).
    """

    _CURSOR_HOME  = "\033[H"
    _HIDE_CURSOR  = "\033[?25l"
    _SHOW_CURSOR  = "\033[?25h"
    _DISABLE_WRAP = "\033[?7l"
    _ENABLE_WRAP  = "\033[?7h"
    _BLACK_BG     = "\033[40m"
    _RESET_ALL    = "\033[0m"
    _CLEAR_SCREEN = "\033[2J"

    DEFAULT_CHAR_RATIO = 0.45

    def __init__(
        self,
        path         : str,
        palette      : list[str] | None = None,
        quantize_bits: int = 0,
        cols         : int = 0,
        char_ratio   : float = DEFAULT_CHAR_RATIO,
    ) -> None:
        _probe  = VideoDecoder(path, 2, 2)
        vid_w   = _probe.vid_w
        vid_h   = _probe.vid_h
        src_fps = _probe.fps
        _probe.release()

        term    = shutil.get_terminal_size(fallback=(220, 50))
        t_cols  = term.columns
        t_lines = term.lines - 2
        aspect  = vid_h / vid_w
        orient  = "portrait" if vid_h > vid_w else "landscape"

        if cols > 0:
            rows = max(1, int(cols * aspect * char_ratio))
        else:
            safe_cols = min(t_cols, 160)
            if orient == "landscape":
                cols = safe_cols
                rows = max(1, int(cols * aspect * char_ratio))
                if rows > t_lines:
                    rows  = t_lines
                    cols  = max(1, int(rows / (aspect * char_ratio)))
            else:
                rows = t_lines
                cols = max(1, int(rows / (aspect * char_ratio)))
                if cols > safe_cols:
                    cols = safe_cols
                    rows = max(1, int(cols * aspect * char_ratio))

        self._pad_y = max(0, (t_lines - rows) // 2)
        self._pad_x = " " * max(0, (t_cols  - cols) // 2)

        print(self._CLEAR_SCREEN)
        print(
            f"\033[1m[ASCILINE — Terminal Player]\033[0m\n"
            f"  Orientation : {orient.upper()}\n"
            f"  Video       : {vid_w}x{vid_h}\n"
            f"  ASCII Grid  : {cols}x{rows} characters\n"
            f"  FPS         : {src_fps:.1f}\n"
            f"  Color depth : {2**(8 - quantize_bits)} levels/channel\n"
            f"  Char ratio  : {char_ratio}\n"
            f"  Exit        : Ctrl+C\n"
        )
        time.sleep(2.0)

        self._decoder = VideoDecoder(path, cols, rows)
        self._mapper  = AsciiMapper(palette, quantize_bits)
        self._frame_t = 1.0 / self._decoder.fps

    def play(self) -> None:
        stdout = sys.stdout
        stdout.write(self._DISABLE_WRAP + self._HIDE_CURSOR + self._BLACK_BG)
        stdout.flush()
        try:
            for gray_frame, bgr_frame in self._decoder:
                t0          = time.perf_counter()
                ascii_frame = self._mapper.convert(gray_frame, bgr_frame)
                if self._pad_x:
                    ascii_frame = self._pad_x + ascii_frame.replace("\n", "\n" + self._pad_x)
                if self._pad_y > 0:
                    ascii_frame = ("\n" * self._pad_y) + ascii_frame
                stdout.write(self._CURSOR_HOME + ascii_frame)
                stdout.flush()
                wait = self._frame_t - (time.perf_counter() - t0)
                if wait > 0:
                    time.sleep(wait)
        except KeyboardInterrupt:
            pass
        finally:
            stdout.write(self._ENABLE_WRAP + self._SHOW_CURSOR + self._RESET_ALL + "\n")
            stdout.flush()
            self._decoder.release()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="ASCILINE Terminal Player — True Color ANSI, zero flicker"
    )
    parser.add_argument("video", help="Path to video file (MP4, AVI, MKV ...)")
    parser.add_argument("--palette", default=None, help="Custom character palette, space-separated")
    parser.add_argument("-q", "--quality", type=int, choices=[0, 1, 2, 3], default=0,
                        help="Color depth: 0=max (default), 3=max speed")
    parser.add_argument("-c", "--cols", type=int, default=0,
                        help="Fixed grid width (0 = auto-fit to terminal)")
    parser.add_argument("--char-ratio", type=float, default=0.45,
                        help="Terminal character aspect ratio (default 0.45). "
                             "Tune per font: Consolas≈0.42, Courier≈0.50")
    args = parser.parse_args()

    palette = args.palette.split() if args.palette else None
    try:
        renderer = TerminalRenderer(
            path          = args.video,
            palette       = palette,
            quantize_bits = args.quality,
            cols          = args.cols,
            char_ratio    = args.char_ratio,
        )
        renderer.play()
    except FileNotFoundError as e:
        print(f"\n[Error] {e}")
        sys.exit(1)
