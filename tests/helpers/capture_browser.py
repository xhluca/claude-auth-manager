#!/usr/bin/env python3
"""Offline native-CLI test opener: capture its URL without opening any browser."""

import os
import sys
from pathlib import Path

with Path(os.environ["CAM_TEST_BROWSER_URL"]).open("a") as stream:
    stream.write(sys.argv[1] + "\n")
