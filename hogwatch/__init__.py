"""HogWatch -- find out who (and what) is slowing the home internet to a crawl.

Pieces:
  ping.py       latency to the eero, the ISP gateway, and the internet
  etw.py        low-level Windows event feed of every network send/receive
  pcnet.py      turns that feed into "which program on this PC used how much"
  eero.py       reads live per-device usage from the eero cloud (the same data
                the eero app shows)
  incidents.py  notices slowdowns and writes a plain-English explanation
  collector.py  runs all of the above in background threads
  web.py        serves the local dashboard at http://127.0.0.1:8765
"""

__version__ = "1.0.0"
