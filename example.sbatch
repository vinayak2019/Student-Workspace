#!/bin/bash
#SBATCH --job-name=student-demo
#SBATCH --time=00:05:00
#SBATCH --ntasks=1
#SBATCH --output=slurm-%j.out
#SBATCH --error=slurm-%j.err
set -euo pipefail
hostname
date
python -c 'print("Hello from a Bridges-2 compute node")'
