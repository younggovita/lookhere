"""Builds ding.wav: a short, soft bell chime (synthesised, no third-party audio)."""
import os

import numpy as np
import soundfile as sf

SR = 48000
t = np.arange(int(SR * 0.9)) / SR
f0 = 1568.0  # G6
# Bell-like partials (slightly inharmonic), each decaying at its own rate.
partials = [(1.0, 1.0, 4.5), (2.0, 0.28, 7.0), (2.76, 0.12, 9.0), (5.4, 0.04, 14.0)]
x = sum(a * np.sin(2 * np.pi * f0 * k * t) * np.exp(-d * t) for k, a, d in partials)
x *= np.minimum(1.0, t / 0.004)          # 4 ms attack, no click
x[-int(SR * 0.05):] *= np.linspace(1, 0, int(SR * 0.05))  # fade out the tail
x = 0.288 * x / np.abs(x).max()          # gentle volume (0.32 turned down 10%)
sf.write(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ding.wav"), x.astype(np.float32), SR,
         subtype="PCM_16")
print("ding.wav written")
