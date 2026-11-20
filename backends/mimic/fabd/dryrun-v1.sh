#!/bin/bash
# Dry run of the northbound API v1 on two idle copies of fabd (no device access; the live agent on :7480 is untouched).
cd "$(dirname "$0")/.." && exec python3 fabd/dryrun_v1.py
