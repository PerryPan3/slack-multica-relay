"""Exercise the SQL parser in the same interpreter that serves MCP queries."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

class SQLRuntimeTests(unittest.TestCase):
    def test_compiler_and_result_boundary(self):
        root=Path(__file__).resolve().parents[1]
        policy=root/".private/runtime/business/policy.json"
        deployed=json.loads(policy.read_text())["python"] if policy.exists() else sys.executable
        python=os.environ.get("BUSINESS_SQL_PYTHON",deployed)
        result=subprocess.run([python,str(root/"tests/business_sql_cases.py")],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr+"\nInstall scripts/requirements-business-sql.txt in the business MCP interpreter.")
