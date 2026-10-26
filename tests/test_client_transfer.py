import shutil
import subprocess
from pathlib import Path

import pytest


def test_client_streaming_and_range_fallbacks():
    node = shutil.which('node')
    if not node:
        pytest.skip('Client transfer tests require Node.js; included in Docker image')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, '--test',
                             str(root / 'tests' / 'transfer-part.test.mjs'),
                             str(root / 'tests' / 'queue-policy.test.mjs'),
                             str(root / 'tests' / 'language.test.mjs')],
                            cwd=root, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
