"""Friendly name generator for tasks and jobs.

Produces memorable names like 'azure-deploy-alpha', 'repo-scan-bravo'.
"""
from __future__ import annotations

import hashlib
import random
from typing import Optional

ADJECTIVES = [
    "quick", "silent", "bright", "deep", "bold", "swift", "calm",
    "keen", "sharp", "smart", "cool", "warm", "fast", "iron",
    "blue", "red", "green", "gold", "dark", "light", "fresh",
    "prime", "grand", "core", "mega", "nova", "apex", "zero",
    "lunar", "solar", "cyber", "pixel", "turbo", "ultra", "hyper",
]

NOUNS = [
    "scan", "deploy", "build", "check", "sync", "fetch", "push",
    "pull", "merge", "patch", "task", "probe", "sweep", "audit",
    "relay", "spark", "forge", "pulse", "wave", "grid", "node",
    "link", "bolt", "dash", "flow", "beam", "core", "vault",
    "shard", "trace", "bloom", "drift", "flame", "orbit", "quest",
]

SUFFIXES = [
    "alpha", "bravo", "charlie", "delta", "echo", "fox",
    "gamma", "hawk", "india", "juliet", "kilo", "lima",
    "mike", "nova", "oscar", "papa", "romeo", "sierra",
    "tango", "ultra", "victor", "whiskey", "xray", "zulu",
]


def generate_name(seed: Optional[str] = None) -> str:
    """Generate a friendly name like 'swift-deploy-bravo'.

    If seed is provided, the name is deterministic for that seed.
    Otherwise, it's random.
    """
    if seed:
        h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
        adj = ADJECTIVES[h % len(ADJECTIVES)]
        noun = NOUNS[(h >> 8) % len(NOUNS)]
        suffix = SUFFIXES[(h >> 16) % len(SUFFIXES)]
    else:
        adj = random.choice(ADJECTIVES)
        noun = random.choice(NOUNS)
        suffix = random.choice(SUFFIXES)
    return f"{adj}-{noun}-{suffix}"