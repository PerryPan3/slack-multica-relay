"""Run native MCP tool tests with the interpreter used by the Slack gateway."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

class NativeToolRuntimeTests(unittest.TestCase):
    def test_native_tools_in_configured_python(self):
        root=Path(__file__).resolve().parents[1]
        policy=root/".private/runtime/review/evidence.json"
        deployed=json.loads(policy.read_text())["python"] if policy.exists() else sys.executable
        python=os.environ.get("BUSINESS_SQL_PYTHON",deployed)
        result=subprocess.run([python,str(root/"tests/agent_tools_cases.py")],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
