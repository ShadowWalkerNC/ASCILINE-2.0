"""
cli/profiles.py
===============
Named quality/speed preset bundles for ASCILINE.

Usage (CLI):
    python stream_server.py video.mp4 --profile cinematic
    python stream_server.py video.mp4 --profile terminal

A profile is a dict of CLI-equivalent defaults. Any explicitly provided
flag overrides the profile value — profiles are defaults, not locks.

Profile reference
-----------------
terminal  : Pure speed. Mode 1 B&W, narrow grid, no audio.
            Target: IoT devices, retro terminals, microcontrollers.

web       : Sensible default. Mode 3 (32K colors), balanced codec.
            Target: General browser playback, most users.

cinematic : Maximum fidelity. Mode 5 pixel, wide grid, high codec quality.
            Target: Creative/design use, showcase, CSS art.

bandwidth : Extreme compression. Mode 2, narrow grid, low codec quality, muted.
            Target: Very weak networks, remote sessions, embedded devices.

ai        : Structured text frames at max speed, no audio, narrow grid.
            Target: LLM pipelines, semantic video summarization, CV tools.
"""

from dataclasses import dataclass


@dataclass
class Profile:
    name:        str
    mode:        int
    cols:        int | None   # None → auto (450 pixel / 200 ascii)
    quality:     str          # lossless | high | balanced | low
    pixel:       bool
    vol:         int          # 0 = muted
    description: str


PROFILES: dict[str, Profile] = {
    "terminal": Profile(
        name        = "terminal",
        mode        = 1,
        cols        = 100,
        quality     = "lossless",
        pixel       = False,
        vol         = 0,
        description = "Pure speed — B&W, narrow grid, no audio. For IoT/retro terminals.",
    ),
    "web": Profile(
        name        = "web",
        mode        = 3,
        cols        = 200,
        quality     = "balanced",
        pixel       = False,
        vol         = 1,
        description = "Sensible default — 32K colors, balanced codec, audio on.",
    ),
    "cinematic": Profile(
        name        = "cinematic",
        mode        = 5,
        cols        = 480,
        quality     = "high",
        pixel       = True,
        vol         = 2,
        description = "Maximum fidelity — 16M color pixel mode, wide grid.",
    ),
    "bandwidth": Profile(
        name        = "bandwidth",
        mode        = 2,
        cols        = 120,
        quality     = "low",
        pixel       = False,
        vol         = 0,
        description = "Extreme compression — narrow grid, muted. For weak networks.",
    ),
    "ai": Profile(
        name        = "ai",
        mode        = 1,
        cols        = 80,
        quality     = "lossless",
        pixel       = False,
        vol         = 0,
        description = "Structured text frames at max speed. For LLM/CV pipelines.",
    ),
}

PROFILE_NAMES = list(PROFILES.keys())


def apply_profile(args, profile_name: str) -> None:
    """
    Apply a named profile to a parsed argparse Namespace.
    Explicit user-provided flags always take priority over profile defaults.
    """
    if profile_name not in PROFILES:
        raise ValueError(
            f"Unknown profile '{profile_name}'. "
            f"Available: {', '.join(PROFILE_NAMES)}"
        )
    p = PROFILES[profile_name]

    # Only set if the user did not explicitly provide the flag.
    # argparse stores None / False for unset optional args by convention.
    if args.mode == 1 and profile_name != "terminal":  # mode default is 1
        args.mode = p.mode
    if args.cols is None:
        args.cols = p.cols
    if args.quality == "lossless":  # quality default is lossless
        args.quality = p.quality
    if not args.pixel:
        args.pixel = p.pixel
    if args.vol == 1:  # vol default is 1
        args.vol = p.vol

    print(f"[PROFILE] {p.name}: {p.description}")
