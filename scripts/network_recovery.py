"""Persist disconnected turns; readiness checks never invoke a model."""
import hashlib
import hmac
import json
import os
import re
from pathlib import Path
import subprocess
import time
import urllib.request
import fcntl

import review_evidence

requested_pause = None


class NetworkWait(Exception):
    def __init__(self, reason="model_network_wait"):
        self.code = reason
        super().__init__(reason)


def configuration():
    path = review_evidence.PRIVATE / "agent/recovery.json"
    return json.loads(path.read_text()) if path.exists() else {}


def enabled():
    return configuration().get("enabled") is True


def reachable():
    # Check the same ChatGPT transport used by this runtime, without generating tokens.
    try:
        result = subprocess.run(
            ["/usr/bin/curl", "--silent", "--head", "--connect-timeout", "3", "--max-time", "5",
             "--output", "/dev/null", "--write-out", "%{http_code}",
             "https://chatgpt.com/backend-api/codex/models"],
            capture_output=True, text=True, timeout=6,
            env={"PATH": "/usr/bin:/bin"})
        return result.returncode == 0 and result.stdout in ("200", "401", "403", "404", "405")
    except (OSError, subprocess.SubprocessError):
        return False


def path_for(identity):
    return review_evidence.PRIVATE / "runs/network-wait" / (identity + ".json")


def read(identity):
    path = path_for(identity)
    return json.loads(path.read_text()) if path.exists() else None


def save(identity, data):
    path = path_for(identity)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def defer(identity, args, code):
    previous = read(identity) or {}
    count = previous.get("resumes", 0)
    if count >= 3:
        return False
    save(identity, {"args": args, "created": previous.get("created", time.time()),
                    "resumes": count, "ready": False, "code": code,
                    "not_before": time.time() + 60, "healthy_since": None})
    return True


def consume(identity):
    pending = read(identity)
    if pending:
        if not pending.get("ready"):
            raise NetworkWait(pending["code"])
        pending.update(ready=False, resumes=pending.get("resumes", 0) + 1)
        save(identity, pending)


def clear(identity):
    path_for(identity).unlink(missing_ok=True)


class ProgressWatch:
    def __init__(self, now=None):
        self.progress = time.monotonic() if now is None else now
        self.probed = self.progress
        self.tools = set()
        self.network_since = None

    def event(self, event, now):
        item=event.get('item',{})
        if event.get('type')=='error' or item.get('type')=='error':
            message=str(event.get('message') or item.get('message') or '')
            if re.search(r'(?i)waiting for network|connection (?:failed|reset|closed)|error sending request|stream disconnected',message):
                if self.network_since is None: self.network_since=now
            return
        # Reconnection/error messages are not model or tool progress.
        if event.get("type") not in ("item.started", "item.completed", "turn.completed"):
            return
        self.progress = now
        self.network_since = None
        if item.get("type") == "mcp_tool_call":
            if event["type"] == "item.started": self.tools.add(item.get("id"))
            else: self.tools.discard(item.get("id"))

    def check(self, now, probe=reachable):
        # Let bounded tool calls settle before interrupting potentially unknown writes.
        idle = now - self.progress
        if self.tools:
            if idle >= 300: raise NetworkWait("model_stalled_wait")
            return
        if self.network_since is not None and now-self.network_since>=30:
            raise NetworkWait()
        if idle >= 60 and now - self.probed >= 30:
            self.probed = now
            if not probe(): raise NetworkWait()
        if idle >= 300: raise NetworkWait("model_stalled_wait")


def recovery_target(args, config, key, dispatch_unknown=None):
    event = args["event"]
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
    data={"thread": digest(":".join(event[k] for k in ("teamId", "channelId", "threadTs"))),
          "message": digest(":".join(event[k] for k in ("teamId", "channelId", "messageTs")))}
    if dispatch_unknown: data['dispatch_unknown']=dispatch_unknown
    body = json.dumps(data, separators=(",", ":"))
    stamp = str(int(time.time()))
    signature = hmac.new(key, ("network-recovery/1:" + stamp + ":" + body).encode(), hashlib.sha256).hexdigest()
    request = urllib.request.Request(config["worker_url"] + "/api/runtime/recovery", data=body.encode(),
        headers={"Content-Type": "application/json", "User-Agent": "PerryRelayRecovery/1.0",
                 "X-Relay-Time": stamp, "X-Relay-Signature": signature})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def tick():
    config = configuration()
    if config.get("enabled") is not True: return
    root = path_for("unused").parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "watcher.lock").open("a") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        pending = list(root.glob("*.json"))
        if not pending: return
        healthy = reachable()
        key = (review_evidence.PRIVATE / "review/key").read_text().strip().encode()
        for path in pending:
            identity = path.stem
            initial=read(identity)
            if not initial: continue
            event=initial['args'].get('event',{})
            binding=[event.get(k,'') for k in ('teamId','channelId','threadTs')]
            thread=hashlib.sha256(json.dumps(binding).encode()).hexdigest()
            folder=review_evidence.PRIVATE/'runs/native-agent'/thread
            folder.mkdir(parents=True,exist_ok=True,mode=0o700)
            with (folder/'lock').open('a') as turn_lock:
                try: fcntl.flock(turn_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError: continue
                record = read(identity)
                if not record or record.get('blocked') or time.time() < record["not_before"]: continue
                if not healthy:
                    record["healthy_since"] = None
                    save(identity, record)
                    continue
                if record.get("healthy_since") is None:
                    record["healthy_since"] = time.time()
                    save(identity, record)
                    continue
                if time.time() - record["healthy_since"] < 30: continue
                try:
                    target = recovery_target(record["args"], config, key)
                    if target.get("action") == "cancelled":
                        clear(identity)
                        continue
                    if target.get("action") != "resume": continue
                    source = target["runId"]
                    # Persist intent before writing. Unknown rerun results are never blindly repeated.
                    if record.get("source_run") == source:
                        if time.time()-record.get('dispatch_at',time.time())>=300:
                            notice=recovery_target(record['args'],config,key,dispatch_unknown=source)
                            if notice.get('action')=='notified':
                                record.update(blocked=True,ready=False)
                                save(identity,record)
                        continue
                    record.update(ready=True, source_run=source, dispatch_at=time.time())
                    save(identity, record)
                    command = [config["multica"], "--profile", config["profile"], "--server-url", config["server_url"],
                               "issue", "rerun-task", source, "--workspace-id", config["workspace_id"], "--output", "json"]
                    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
                    if result.returncode:
                        # Retain intent: the write may have reached the server despite a CLI error.
                        print(json.dumps({"event": "recovery_dispatch_unknown", "request": identity}))
                except Exception as error:
                    print(json.dumps({"event": "recovery_probe_failed", "request": identity, "type": type(error).__name__}))


if __name__ == "__main__":
    tick()
