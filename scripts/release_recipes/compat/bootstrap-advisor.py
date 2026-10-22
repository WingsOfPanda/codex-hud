"""Run the unchanged seven-check script with its targeted temporary-path adapter."""
from recipe_context import CONFIG, DATA, RECIPE
from pathlib import Path
import runpy
import sys
from advisor_tmp import install

BASE = DATA
assert Path(sys.argv[1]).resolve(strict=True) == RECIPE / 'check-advisor-mixes.py'
sys.argv = sys.argv[1:]
with install(BASE):
    runpy.run_path(str(RECIPE / 'check-advisor-mixes.py'), run_name='__main__')
