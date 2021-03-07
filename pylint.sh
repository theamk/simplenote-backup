#!/bin/bash
set -e
PYTHONPATH=simperium-python/ pylint simplenote-backup.py 
echo 'No errors!'
