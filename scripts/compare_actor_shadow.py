#!/usr/bin/env python3
"""Run deterministic migration parity tests without touching Supabase."""
import subprocess, sys

raise SystemExit(subprocess.call([sys.executable,"-m","pytest","-q","tests/test_actor_math.py"]))
