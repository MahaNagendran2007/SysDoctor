#!/usr/bin/env bash
# Runs every stress scenario one after another for a quick demo (Linux VM recommended).
cd "$(dirname "$0")"
echo "1) CPU hog (60 s)";      timeout 60 python3 cpu_hog.py
echo "2) Memory leak (90 s)";  timeout 90 python3 memory_leak.py --rate 15 --max 1200
echo "3) Zombies (60 s)";      timeout 60 python3 zombie.py --count 5
echo "4) Disk I/O (60 s)";     timeout 60 python3 disk_io.py --mb 200
echo "5) Thrashing (90 s)";    timeout 90 python3 thrashing.py --fraction 1.15 --seconds 80
