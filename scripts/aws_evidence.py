"""Bounded AWS metadata reads with host credentials and no shell command surface."""
import configparser
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


OPERATIONS = {
    ("sts", "get_caller_identity"): ("sts", "get-caller-identity", {}),
    ("s3", "list_objects_v2"): ("s3api", "list-objects-v2", {
        "Bucket": "bucket", "Prefix": "prefix", "MaxKeys": "max-keys", "ContinuationToken": "continuation-token"}),
    ("s3", "head_object"): ("s3api", "head-object", {
        "Bucket": "bucket", "Key": "key", "VersionId": "version-id"}),
}


def executable():
    for path in (shutil.which("aws"), "/opt/homebrew/bin/aws", "/usr/local/bin/aws"):
        if path and Path(path).is_file() and os.access(path, os.X_OK):
            return path
    raise FileNotFoundError("AWS CLI is not installed in this host environment.")


def discover():
    profiles = {}
    for filename in ("config", "credentials"):
        config = configparser.RawConfigParser()
        variable = "AWS_CONFIG_FILE" if filename == "config" else "AWS_SHARED_CREDENTIALS_FILE"
        config.read(Path(os.environ.get(variable, Path.home() / ".aws" / filename)).expanduser())
        for section in config.sections():
            name = section.removeprefix("profile ") if filename == "config" else section
            if section.startswith(("sso-session ", "services ")):
                continue
            profile = profiles.setdefault(name, {"name": name})
            if filename == "config" and config.has_option(section, "region"):
                profile["region"] = config.get(section, "region")
    schemas = []
    for service, operation in OPERATIONS:
        properties = {"Bucket": {"type": "string"}}
        required = ["Bucket"]
        if service == "sts":
            properties, required = {}, []
        elif operation == "list_objects_v2":
            properties.update(Prefix={"type": "string"}, MaxKeys={"type": "integer", "minimum": 1, "maximum": 200}, ContinuationToken={"type": "string"})
        else:
            properties.update(Key={"type": "string"}, VersionId={"type": "string"})
            required.append("Key")
        schemas.append({"service": service, "operation": operation,
                        "arguments": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}})
    selected = os.environ.get("AWS_PROFILE", os.environ.get("AWS_DEFAULT_PROFILE", "default"))
    return {"profiles": sorted(profiles.values(), key=lambda p: p["name"]),
            "default_profile": selected,
            "default_region": os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION")) or profiles.get(selected, {}).get("region"),
            "access_verified": False, "operations": schemas,
            "instruction": "These are configured connections, not proof of permission. Check caller identity and then the needed read. No object content, sync command or AWS mutation is available."}


def environment():
    names = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE", "AWS_DEFAULT_PROFILE",
             "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_CA_BUNDLE",
             "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.update(HOME=str(Path.home()), PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
               AWS_EC2_METADATA_DISABLED="true", AWS_PAGER="", AWS_CLI_AUTO_PROMPT="off",
               AWS_RETRY_MODE="standard", AWS_MAX_ATTEMPTS="2")
    return env


def _error(stderr):
    # Do not return raw CLI diagnostics: configured helpers can include secrets.
    if "Unable to locate credentials" in stderr or "could not be found" in stderr and "profile" in stderr.lower():
        return {"ok": False, "code": "credentials_missing", "error": "No usable credentials were found for this host AWS profile. This is separate from owner-shell credential isolation."}
    if any(word in stderr for word in ("ExpiredToken", "InvalidClientTokenId", "UnrecognizedClient", "SSO session", "Token has expired")):
        return {"ok": False, "code": "credentials_expired", "error": "The configured AWS identity needs renewed authentication."}
    if any(word in stderr for word in ("AccessDenied", "Forbidden", "UnauthorizedOperation")):
        return {"ok": False, "code": "access_denied", "error": "AWS denied this specific read operation. No IAM permissions were changed."}
    if any(word in stderr for word in ("NoSuchBucket", "NoSuchKey", "Not Found", "(404)")):
        return {"ok": False, "code": "not_found", "error": "AWS did not return the requested object or bucket."}
    return {"ok": False, "code": "aws_read_failed", "error": "AWS did not provide a successful read receipt. Check the configured profile, region or service connection."}


def read(service, operation, arguments, profile=None, region=None):
    selected = OPERATIONS.get((service, operation))
    if not selected:
        raise ValueError("This AWS operation is not an available metadata read.")
    if not isinstance(arguments, dict) or set(arguments) - set(selected[2]):
        raise ValueError("Unexpected AWS arguments; endpoint overrides and arbitrary CLI flags are unavailable.")
    args = dict(arguments)
    if service == "s3":
        if not isinstance(args.get("Bucket"), str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", args["Bucket"]):
            raise ValueError("An S3 bucket name is required.")
        if operation == "head_object" and not args.get("Key"):
            raise ValueError("An exact object key is required.")
    for name, value in args.items():
        if name == "MaxKeys":
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("MaxKeys must be a positive integer.")
            args[name] = min(value, 200)
        elif not isinstance(value, str) or len(value.encode()) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError("AWS metadata arguments must be bounded text without control characters.")
    if profile is not None and (not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@+-]{0,127}", profile)):
        raise ValueError("Invalid AWS profile name.")
    if region is not None and (not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region)):
        raise ValueError("Invalid AWS region.")
    if operation == "list_objects_v2":
        args.setdefault("MaxKeys", 50)
    try:
        command = [executable(), selected[0], selected[1], "--output", "json", "--no-cli-pager", "--no-paginate",
                   "--cli-connect-timeout", "10", "--cli-read-timeout", "20"]
        # JSON prevents option parsing and CLI file:// expansion of object keys.
        command.extend(["--cli-input-json", json.dumps(args)])
        if profile is not None:
            command.append("--profile=" + profile)
        if region is not None:
            command.append("--region=" + region)
        result = subprocess.run(command, env=environment(), capture_output=True, text=True, timeout=50)
    except FileNotFoundError:
        return {"ok": False, "code": "cli_unavailable", "error": "The host AWS CLI is unavailable."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": "timeout", "error": "AWS metadata read timed out; no write was attempted."}
    if result.returncode:
        return _error(result.stderr)
    try:
        if len(result.stdout.encode()) > 1024 * 1024:
            raise ValueError("oversized response")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("invalid response")
    except ValueError:
        return {"ok": False, "code": "invalid_response", "error": "AWS returned an invalid or oversized metadata response."}
    if service == "sts":
        output = {"account": value.get("Account"), "arn": value.get("Arn"), "user_id": value.get("UserId")}
    elif operation == "list_objects_v2":
        output = {"bucket": args["Bucket"], "prefix": args.get("Prefix", ""),
                  "complete": not value.get("IsTruncated", False) and not args.get("ContinuationToken"),
                  "has_more": bool(value.get("IsTruncated", False)),
                  "next_cursor": value.get("NextContinuationToken"),
                  "objects": [{"key": item["Key"], "size": item["Size"], "etag": item.get("ETag"), "last_modified": item.get("LastModified")}
                              for item in value.get("Contents") or []]}
    else:
        output = {"bucket": args["Bucket"], "key": args["Key"], "size": value.get("ContentLength"),
                  "etag": value.get("ETag"), "last_modified": value.get("LastModified"), "version_id": value.get("VersionId")}
    return {"ok": True, "result": output}


def compare_manifest(source_files, remote_page, prefix, source_revision, excludes=()):
    """Compare a host-pinned file manifest to a complete bounded metadata listing."""
    if not source_revision or not remote_page.get("complete"):
        raise ValueError("A pinned source revision and complete destination listing are required.")
    if "prefix" in remote_page and remote_page["prefix"] != prefix:
        raise ValueError("The destination listing must cover the exact deployment prefix.")
    if not isinstance(prefix, str) or prefix and not prefix.endswith("/"):
        raise ValueError("The deployment prefix must end with a slash.")
    included = lambda path: not any(fnmatch.fnmatchcase(path, pattern) for pattern in excludes)
    source = {item["path"]: item["size"] for item in source_files if included(item["path"])}
    remote = {item["key"][len(prefix):]: item["size"] for item in remote_page["objects"]
              if item["key"].startswith(prefix) and included(item["key"][len(prefix):])}
    return {"source_revision": source_revision, "delete_candidates": sorted(set(remote) - set(source)),
            "upload_candidates_by_size": sorted(path for path in source if remote.get(path) != source[path]),
            "is_aws_sync_dry_run": False,
            "limitation": "Metadata-only comparison. It does not execute aws s3 sync, compare timestamps or file contents, or account for files generated later by a workflow."}
