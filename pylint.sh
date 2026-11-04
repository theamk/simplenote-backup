#!/bin/bash
set -e
uv run --group dev pylint simplenote-backup.py simperium-test.py
echo 'No errors!'
