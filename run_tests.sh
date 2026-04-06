#!/bin/bash
export PATH=/home/sarathy/.local/bin:/home/sarathy/.cargo/bin:/usr/local/bin:/usr/bin:/bin
cd /home/sarathy/projects/charu.ai
uv run python -m pytest tests/ -x --tb=short -q 2>&1 | tail -100
