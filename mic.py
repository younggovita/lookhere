"""The microphone in its own process (Mac only).

On a Mac, stopping a PortAudio stream can deadlock: Pa_StopStream waits inside Core Audio's
AudioOutputUnitStop while Core Audio's IO thread waits in PortAudio's startStopCallback, each on
the other's lock (seen 2026-10-30; it froze Lookhere mid-stop). Ending a process can't deadlock,
so Lookhere records the mic here and stops it by ending this process.

    mic.py <device index or -> <samplerate> <channels>

Writes "OK\\n" (or "ERR <message>\\n") on stdout, then raw float32 samples, interleaved, until
stdin closes or the parent goes away. The stream is never stopped, only abandoned on exit.
"""
import os
import sys
import threading

import sounddevice as sd


def main():
    dev = None if sys.argv[1] == "-" else int(sys.argv[1])
    samplerate, channels = int(sys.argv[2]), int(sys.argv[3])
    out = sys.stdout.buffer
    lock = threading.Lock()

    def cb(data, *_):
        try:
            with lock:
                out.write(data.tobytes())
                out.flush()
        except (BrokenPipeError, ValueError):
            os._exit(0)

    try:
        stream = sd.InputStream(device=dev, samplerate=samplerate, channels=channels, callback=cb)
    except Exception as e:
        out.write(("ERR %s\n" % e).replace("\n", " ").strip().encode() + b"\n")
        out.flush()
        return
    with lock:  # the header goes out before any samples
        out.write(b"OK\n")
        out.flush()
        stream.start()
    sys.stdin.read()  # until the parent closes stdin or exits
    os._exit(0)       # leave without Pa_StopStream


if __name__ == "__main__":
    main()
