"""Use the deployed gateway interpreter for process lifecycle regressions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

class RuntimeRecoveryBridgeTests(unittest.TestCase):
    def test_runtime_recovery(self):
        root=Path(__file__).resolve().parents[1]
        policy=root/'.private/runtime/review/evidence.json'
        python=os.environ.get('BUSINESS_SQL_PYTHON',json.loads(policy.read_text())['python'] if policy.exists() else sys.executable)
        result=subprocess.run([python,str(root/'tests/runtime_recovery_cases.py')],capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
