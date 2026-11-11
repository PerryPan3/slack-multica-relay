import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aws_evidence as aws


class AwsEvidenceTests(unittest.TestCase):
    def test_no_write_or_endpoint_override_reaches_cli(self):
        with patch.object(aws.subprocess, "run") as run:
            for service, operation, args in [
                ("s3", "delete_object", {"Bucket": "example-bucket", "Key": "x"}),
                ("s3", "sync", {}),
                ("sts", "assume_role", {}),
                ("s3", "list_objects_v2", {"Bucket": "example-bucket", "endpoint_url": "https://elsewhere.test"}),
                ("s3", "list_objects_v2", {"Bucket": "--debug"}),
            ]:
                with self.assertRaises(ValueError):
                    aws.read(service, operation, args)
            run.assert_not_called()

    def test_host_credentials_are_used_without_copying_them_into_arguments(self):
        with tempfile.TemporaryDirectory() as home:
            result = SimpleNamespace(returncode=0, stdout=json.dumps({"Account": "123", "Arn": "arn:test", "UserId": "id"}), stderr="")
            with patch.object(aws.Path, "home", return_value=Path(home)), \
                 patch.object(aws, "executable", return_value="/usr/local/bin/aws"), \
                 patch.object(aws.subprocess, "run", return_value=result) as run, \
                 patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "do-not-expose"}):
                value = aws.read("sts", "get_caller_identity", {})
            command = run.call_args.args[0]
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["HOME"], home)
            self.assertEqual(env["AWS_EC2_METADATA_DISABLED"], "true")
            self.assertNotIn("do-not-expose", json.dumps(command))
            self.assertNotIn("do-not-expose", json.dumps(value))
            self.assertEqual(value["result"]["account"], "123")

    def test_s3_page_is_bounded_and_does_not_claim_a_complete_inventory(self):
        reply = {"IsTruncated": True, "NextContinuationToken": "next-page", "Contents": [{"Key": "prefix/a", "Size": 4, "ETag": "etag", "Owner": {"ID": "private-owner"}}]}
        with patch.object(aws, "executable", return_value="/usr/local/bin/aws"), \
             patch.object(aws.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(reply), stderr="")) as run:
            value = aws.read("s3", "list_objects_v2", {"Bucket": "example-bucket", "Prefix": "prefix/", "MaxKeys": 999})
        command = run.call_args.args[0]
        self.assertEqual(json.loads(command[command.index("--cli-input-json") + 1])["MaxKeys"], 200)
        self.assertIn("--no-paginate", command)
        self.assertFalse(value["result"]["complete"])
        self.assertEqual(value["result"]["next_cursor"], "next-page")
        self.assertNotIn("private-owner", json.dumps(value))

    def test_key_and_prefix_cannot_become_options_or_local_file_reads(self):
        response = SimpleNamespace(returncode=0, stdout='{}', stderr='')
        with patch.object(aws, "executable", return_value="/usr/local/bin/aws"), \
             patch.object(aws.subprocess, "run", return_value=response) as run:
            for operation, field in [("list_objects_v2", "Prefix"), ("head_object", "Key")]:
                for value in ["--endpoint-url=https://elsewhere.test", "file:///private/credential"]:
                    aws.read("s3", operation, {"Bucket": "example-bucket", field: value})
                    command = run.call_args.args[0]
                    self.assertNotIn(value, command)
                    self.assertEqual(json.loads(command[command.index("--cli-input-json") + 1])[field], value)

    def test_final_page_alone_is_not_a_complete_destination_inventory(self):
        with patch.object(aws, "executable", return_value="/usr/local/bin/aws"), \
             patch.object(aws.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout='{"IsTruncated":false}', stderr='')):
            value = aws.read("s3", "list_objects_v2", {"Bucket": "example-bucket", "ContinuationToken": "second-page"})
        self.assertFalse(value["result"]["complete"])
        self.assertFalse(value["result"]["has_more"])

    def test_missing_credentials_and_denied_permissions_are_distinct(self):
        for stderr, code in [("Unable to locate credentials", "credentials_missing"),
                             ("An error occurred (AccessDenied) when calling the ListObjectsV2 operation", "access_denied"),
                             ("An error occurred (ExpiredToken) when calling operation", "credentials_expired")]:
            with patch.object(aws, "executable", return_value="/usr/local/bin/aws"), \
                 patch.object(aws.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr=stderr)):
                value = aws.read("sts", "get_caller_identity", {})
            self.assertFalse(value["ok"])
            self.assertEqual(value["code"], code)

    def test_discovery_lists_configuration_not_secrets_or_claimed_access(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            (home / ".aws").mkdir()
            (home / ".aws/config").write_text("[default]\nregion=us-west-2\n[profile report]\nregion=us-east-1\nsso_start_url=https://example.test\n")
            (home / ".aws/credentials").write_text("[default]\naws_access_key_id=secret-id\naws_secret_access_key=secret-key\n")
            with patch.object(aws.Path, "home", return_value=home), patch.dict(os.environ, {}, clear=True):
                value = aws.discover()
            self.assertEqual([p["name"] for p in value["profiles"]], ["default", "report"])
            self.assertNotIn("secret-key", json.dumps(value))
            self.assertFalse(value["access_verified"])
            self.assertEqual(value["default_region"], "us-west-2")

    def test_manifest_comparison_is_not_a_sync_dry_run(self):
        page = {"complete": True, "objects": [{"key": "deploy/keep.sql", "size": 4}, {"key": "deploy/analyses/.gitkeep", "size": 0}]}
        value = aws.compare_manifest([{"path": "keep.sql", "size": 4}], page, "deploy/", "commit-sha")
        self.assertEqual(value["delete_candidates"], ["analyses/.gitkeep"])
        self.assertFalse(value["is_aws_sync_dry_run"])
        page["complete"] = False
        with self.assertRaises(ValueError):
            aws.compare_manifest([], page, "deploy/", "commit-sha")


if __name__ == "__main__":
    unittest.main()
