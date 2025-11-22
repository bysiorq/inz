#!/usr/bin/env python3
import time
from sensors import MCP3008

adc = MCP3008(0, 0)
CH = 2

print("Cisza / dmuchanie / krzyk – patrzymy na amp")
try:
    while True:
        vals = [adc.read_channel(CH) for _ in range(200)]
        vmin = min(vals)
        vmax = max(vals)
        amp = vmax - vmin
        avg = sum(vals) / len(vals)
        print(f"avg={avg:6.1f}  amp={amp:4d}")
        time.sleep(0.3)
except KeyboardInterrupt:
    print("Koniec")
