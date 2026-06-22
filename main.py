#!/usr/bin/env python
"""Entry point for the Resume-Aware Internship Finder.

Usage:
    python main.py --resume path/to/resume.pdf [options]

See `python main.py --help` for all options, and config.yaml for tuning.
"""

from internfinder.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
