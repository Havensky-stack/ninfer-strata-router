#!/usr/bin/env python3
"""One OpenAI/Anthropic endpoint in front of two local models that cannot share the GPUs.

Strata (IQ3_S, layer split over both V100s) and NInfer-DFlash (coder390, TP2 over both
V100s) each want the whole pair of cards, so only one can be resident.  This sends each
request to the model it names, starts that model when it is not up, and stops the other
one first, which is what frees the VRAM the new one needs.

    GET  /v1/models                both models, with the state of each
    GET  /router/status            the same, plus ports, PIDs and a log tail
    GET  /router/metrics           tokens, requests and output speed          (admin only)
    POST /router/load   {"model": "coder390"}    start one without a request  (admin only)
    POST /router/unload {"model": "all"}         free the GPUs               (admin only)
    POST /v1/chat/completions      forwarded to the model named in the body
    (also /v1/completions, /v1/responses, /v1/messages, /v1/embeddings, ...)
    anything else                  forwarded to the model already loaded

The model is the body's "model" field; anything the aliases in router.json do not know
goes to default_model.  The field is rewritten to the backend's own model id, so a client
can call the router with any of the aliases.
models.<name>.effort_map additionally rewrites a reasoning effort that backend refuses -
NInfer's template exposes low, medium and xhigh, so "high" and "max" are mapped onto them.
With models.<name>.strip_rejected (NInfer), a field the backend refuses by name is dropped
and the request retried, so a client written for the fuller OpenAI API still works there.
"tools" is filtered rather than dropped - function tools with strict off - because a
request without its tool definitions makes the model write the call as plain text.
A path whose /v1 is missing (/responses, /messages, ...) or doubled (/v1/v1/messages) is
forwarded as /v1/... , so a client whose base URL carries the prefix and one whose base URL
does not both reach the backend.

Every request must carry one of auth.keys, as "Authorization: Bearer <key>" or as
"x-api-key: <key>" - including requests from this machine, because the tunnel that
publishes the router connects from 127.0.0.1.  Only the health paths are exempt.  A key
that is also in auth.admin_keys skips the rate limit, so it can never be throttled; its
requests are marked "(admin)" in the log.  The paths in auth.admin_only_paths (by default
/router/load and /router/unload, which start and stop models) answer 403 to a normal key.

Concurrency and priority: requests run on threads, at most limits.max_inflight at once
(more than that is a 503), with a per-key token bucket on top (more than that is a 429).
An admin key (auth.admin_keys) skips both, so it is never throttled or refused for load.
Each backend still serves one request at a time, so requests past the first queue inside
the model.  A switch stops the other model, which would kill its answer mid-reply, so the
router waits for that answer first: a normal caller waits switch.drain_seconds and is then
refused with a 503 and a Retry-After, while an admin caller waits switch.admin_drain_seconds
and then switches anyway, cutting that answer off.  A request that names a model being
taken down is refused the same way.  A request that has to load one waits start_timeout_s.
While an admin request waits for a model, that model - and any model that has to drain for
it - is kept clear: a new normal-key request for it is refused with a 503 and a Retry-After
instead of joining the queue, so the admin never waits behind a growing line.  It still
waits for what the backend had already accepted.

Only the standard library is used, so any Python 3.9+ runs this.
"""
import ctypes
import hashlib
import hmac
import http.client
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "router.json")
LOG_PATH = os.path.join(HERE, "router.log")

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailer", "trailers", "transfer-encoding", "upgrade"}

LOAD_LOCK = threading.Lock()      # one load or unload at a time
ST_LOCK = threading.Lock()        # short: STATE only


def log(message):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    for name, model in cfg["models"].items():
        model.setdefault("aliases", [])
        model.setdefault("send_model", name)
        model.setdefault("ready_path", "/health")
        model.setdefault("ready_json", None)
        model.setdefault("start_timeout_s", 900)
        model.setdefault("stop_timeout_s", 120)
        parsed = urlparse(model["base"])
        model["_host"] = parsed.hostname or "127.0.0.1"
        model["_port"] = parsed.port or 80
        model["_aliases"] = {a.lower() for a in
                             [name, model["send_model"], *model["aliases"]]}
    return cfg


CFG = load_config()
MODELS = CFG["models"]
DEFAULT = CFG.get("default_model") or next(iter(MODELS))
STATE = {name: {"state": "stopped", "detail": "", "since": None, "pids": []}
         for name in MODELS}

# Every request needs one of these keys, wherever it comes from.  There is deliberately no
# "localhost is trusted" shortcut: this router is published through a tunnel (frp) that
# connects from 127.0.0.1, so a local shortcut would be no authentication at all.
AUTH = CFG.get("auth") or {}
AUTH_KEYS = [k for k in (AUTH.get("keys") or []) if k]
AUTH_ADMIN_KEYS = [k for k in (AUTH.get("admin_keys") or []) if k]
AUTH_EXEMPT = set(AUTH.get("exempt_paths") or ["/health", "/router/health"])
# Control endpoints that start and stop models: an admin key only.
ADMIN_ONLY_PATHS = set(AUTH.get("admin_only_paths") or ["/router/load", "/router/unload"])

LIMITS = CFG.get("limits") or {}
MAX_BODY = int(LIMITS.get("max_body_bytes") or (32 << 20))
MAX_INFLIGHT = int(LIMITS.get("max_inflight") or 16)
INFLIGHT = threading.BoundedSemaphore(MAX_INFLIGHT)

# Rate limit: one token bucket per presented API key.  0 requests/minute turns it off.
_RATE = LIMITS.get("rate_limit") or {}
RATE_PER_MIN = float(_RATE.get("requests_per_minute") or 0)
RATE_BURST = float(_RATE.get("burst") or 10)

# A switch stops the other model, which would cut its answer off mid-reply.  The router
# waits this long for those answers to finish and then refuses the new request instead.
SWITCH = CFG.get("switch") or {}
SWITCH_DRAIN_S = float(SWITCH.get("drain_seconds") or 45)
# An admin key waits longer and then takes the GPUs anyway: "everything gives way to the
# admin, but a reply that is being written still gets to finish first" (300 s by default).
ADMIN_DRAIN_S = float(SWITCH.get("admin_drain_seconds") or SWITCH_DRAIN_S)


class TooLarge(Exception):
    """A request body over limits.max_body_bytes."""


class Busy(Exception):
    """Another model is mid-answer; refusing is better than cutting it off."""

    def __init__(self, model, retry_after):
        super().__init__("the %s model is answering a request right now" % model)
        self.model = model
        self.retry_after = retry_after


COUNT_LOCK = threading.Lock()
INFLIGHT_BY_MODEL = {name: 0 for name in MODELS}


def busy_models(exclude=None):
    """The models with a request in flight right now."""
    with COUNT_LOCK:
        return sorted(m for m, n in INFLIGHT_BY_MODEL.items() if n > 0 and m != exclude)


def reserve(name):
    with COUNT_LOCK:
        INFLIGHT_BY_MODEL[name] += 1


def release_model(name):
    with COUNT_LOCK:
        INFLIGHT_BY_MODEL[name] = max(0, INFLIGHT_BY_MODEL[name] - 1)


# An admin request must not end up behind a queue that keeps growing, so the models it
# needs (the one it is asking for, and the ones that have to drain first) are marked here
# while it waits.  New normal-key requests for a marked model are refused instead of
# joining the queue.
ADMIN_LOCK = threading.Lock()
ADMIN_WANTS = {name: 0 for name in MODELS}


def admin_wants(name, on=True):
    with ADMIN_LOCK:
        ADMIN_WANTS[name] = max(0, ADMIN_WANTS.get(name, 0) + (1 if on else -1))


def admin_wanted():
    """The models an admin request is waiting on right now."""
    with ADMIN_LOCK:
        return sorted(m for m, n in ADMIN_WANTS.items() if n > 0)


# Two admin requests for different models would otherwise wait for each other: each one
# counts as in flight, and each one waits for the other to drain.  Only the first admin
# request may enter the load/switch path; a request for the other model gets a clear 503
# and retries after that model is up.  Requests for the same model may still queue.
ADMIN_SWITCH_LOCK = threading.Lock()
ADMIN_SWITCH = {"model": None, "count": 0, "since": None}


def claim_admin_switch(name):
    """The model another admin request is already bringing up, or None if this may go."""
    with ADMIN_SWITCH_LOCK:
        owner = ADMIN_SWITCH["model"]
        if owner and owner != name:
            return owner
        if not owner:
            ADMIN_SWITCH["model"] = name
            ADMIN_SWITCH["since"] = time.time()
        ADMIN_SWITCH["count"] += 1
        return None


def release_admin_switch(name):
    with ADMIN_SWITCH_LOCK:
        if ADMIN_SWITCH["model"] == name:
            ADMIN_SWITCH["count"] = max(0, ADMIN_SWITCH["count"] - 1)
            if not ADMIN_SWITCH["count"]:
                ADMIN_SWITCH["model"] = None
                ADMIN_SWITCH["since"] = None


def admin_switch_owner():
    with ADMIN_SWITCH_LOCK:
        return ADMIN_SWITCH["model"]


class RateLimiter:
    """A token bucket per API key.  requests_per_minute <= 0 disables the limiter."""

    def __init__(self, per_minute, burst):
        self.rate = per_minute / 60.0
        self.burst = max(float(burst), 1.0)
        self.buckets = {}
        self.lock = threading.Lock()

    def take(self, key):
        """(True, 0.0) when allowed, (False, seconds_to_wait) when the bucket is empty."""
        if self.rate <= 0:
            return True, 0.0
        now = time.monotonic()
        with self.lock:
            tokens, last = self.buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < 1.0:
                self.buckets[key] = (tokens, now)
                return False, max(1.0, (1.0 - tokens) / self.rate)
            self.buckets[key] = (tokens - 1.0, now)
            return True, 0.0


RATE = RateLimiter(RATE_PER_MIN, RATE_BURST)


# ------------------------------------------------------------------ usage metrics
# What went through the router, with the token counts the backend reported and the speed the
# router measured itself.  The counts are read out of the response (a JSON body or the SSE
# frames), so they need no cooperation from the client.
USAGE_LOCK = threading.Lock()
USAGE = {
    "since": time.strftime("%Y-%m-%d %H:%M:%S"),
    "requests": 0,
    "failed": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "timed_completion_tokens": 0,
    "generation_ms": 0,
    "by_model": {},
    "by_key": {},
    "recent": [],
}
RECENT_MAX = 20
USAGE_BUFFER_MAX = 8 << 20


def show(message):
    """Print to the router's own console window, apart from the log file."""
    try:
        print(message, flush=True)
    except OSError:
        pass


def usage_line(entry):
    """One finished request, compactly, for the window."""
    def count(value):
        return "-" if value is None else str(value)

    return ("[usage] %-9s %-14s %s  %s in / %s out  %s  ttft %s  total %dms"
            % (entry["model"], entry["path"], entry["status"],
               count(entry["prompt_tokens"]), count(entry["completion_tokens"]),
               "%.1f tok/s" % entry["output_tok_per_s"]
               if entry["output_tok_per_s"] is not None else "     - tok/s",
               "%dms" % entry["ttft_ms"] if entry["ttft_ms"] is not None else "-",
               entry["duration_ms"]))


def usage_beat(interval=60.0):
    """A summary line a minute in the window, only when something happened since the last."""
    seen = 0
    while True:
        time.sleep(interval)
        with USAGE_LOCK:
            requests = USAGE["requests"]
            prompt = USAGE["prompt_tokens"]
            completion = USAGE["completion_tokens"]
            average = speed(USAGE["timed_completion_tokens"], USAGE["generation_ms"])
            since = USAGE["since"]
        if requests == seen:
            continue
        seen = requests
        show("[usage] %d request(s) since %s:  %s in / %s out,  average %s tok/s"
             % (requests, since, format(prompt, ","), format(completion, ","),
                "-" if average is None else "%.1f" % average))


def key_label(key):
    """A short stable name for a key, so /router/metrics never prints the key itself."""
    if not key or key == "-":
        return "anonymous"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def find_usage(node):
    """(prompt, completion) from the first usage object in a response, whatever wrapped it.

    Chat Completions says prompt_tokens/completion_tokens; the Responses and Anthropic APIs
    say input_tokens/output_tokens, under "usage", "response.usage" or "message.usage"."""
    if isinstance(node, dict):
        if any(k in node for k in ("prompt_tokens", "input_tokens",
                                   "completion_tokens", "output_tokens")):
            return (node.get("prompt_tokens", node.get("input_tokens")),
                    node.get("completion_tokens", node.get("output_tokens")))
        for value in node.values():
            found = find_usage(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find_usage(value)
            if found:
                return found
    return None


def speed(tokens, ms):
    return round(tokens / (ms / 1000.0), 1) if tokens and ms else None


def record_usage(model, key, path, status, prompt, completion, ttft_ms, duration_ms, ok=True):
    """One finished request, counted in total, per model and per key."""
    gen_ms = None
    if ttft_ms is not None and duration_ms is not None:
        gen_ms = max(1, duration_ms - ttft_ms)
    entry = {"time": time.strftime("%H:%M:%S"), "model": model, "key": key_label(key),
             "path": path, "status": status, "prompt_tokens": prompt,
             "completion_tokens": completion, "ttft_ms": ttft_ms,
             "duration_ms": duration_ms,
             "output_tok_per_s": speed(completion, gen_ms),
             "wall_tok_per_s": speed(completion, duration_ms)}
    with USAGE_LOCK:
        USAGE["requests"] += 1
        if not ok:
            USAGE["failed"] += 1
        USAGE["prompt_tokens"] += prompt or 0
        USAGE["completion_tokens"] += completion or 0
        if gen_ms:                             # only requests whose decode time we measured
            USAGE["timed_completion_tokens"] += completion or 0
            USAGE["generation_ms"] += gen_ms
        for bucket, label in ((USAGE["by_model"], model), (USAGE["by_key"], entry["key"])):
            row = bucket.setdefault(label, {"requests": 0, "prompt_tokens": 0,
                                            "completion_tokens": 0,
                                            "timed_completion_tokens": 0,
                                            "generation_ms": 0,
                                            "last_output_tok_per_s": None})
            row["requests"] += 1
            row["prompt_tokens"] += prompt or 0
            row["completion_tokens"] += completion or 0
            if gen_ms:
                row["timed_completion_tokens"] += completion or 0
                row["generation_ms"] += gen_ms
            if entry["output_tok_per_s"] is not None:
                row["last_output_tok_per_s"] = entry["output_tok_per_s"]
        USAGE["recent"].append(entry)
        while len(USAGE["recent"]) > RECENT_MAX:
            USAGE["recent"].pop(0)
    show(usage_line(entry))
    return entry


class UsageSniffer:
    """Watches one response go past and keeps its token counts and its first token time."""

    def __init__(self):
        self.first_token = None
        self.buffer = b""

    def feed(self, chunk):
        if self.first_token is None and chunk:
            self.first_token = time.time()
        if len(self.buffer) < USAGE_BUFFER_MAX:
            self.buffer += chunk[:USAGE_BUFFER_MAX - len(self.buffer)]

    def counts(self):
        """(prompt_tokens, completion_tokens) as the backend reported them; None where absent."""
        if not self.buffer:
            return None, None
        try:
            found = find_usage(json.loads(self.buffer))
            if found:
                return found
        except ValueError:
            pass                                   # not one JSON body, so it is SSE
        prompt = completion = None
        for line in self.buffer.splitlines():
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                found = find_usage(json.loads(payload))
            except ValueError:
                continue
            if found:
                if found[0] is not None:
                    prompt = found[0]
                if found[1] is not None:
                    completion = found[1]
        return prompt, completion


def http_get(url, timeout=5.0):
    parsed = urlparse(url)
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    try:
        conn.request("GET", parsed.path or "/")
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def ready(name, timeout=5.0):
    """(True, '') when the backend answers and, if a ready_json is set, matches it."""
    model = MODELS[name]
    try:
        status, body = http_get(model["base"] + model["ready_path"], timeout=timeout)
    except OSError as exc:
        return False, "not answering (%s)" % exc
    if status != 200:
        return False, "HTTP %d from %s" % (status, model["ready_path"])
    want = model.get("ready_json")
    if want:
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return False, "not JSON from %s" % model["ready_path"]
        for key, value in want.items():
            if data.get(key) != value:
                return False, "%s is %r, wanted %r" % (key, data.get(key), value)
    return True, ""


def port_pids(port):
    """Every PID listening on TCP `port`, whoever started it."""
    pids = set()
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=30,
                             creationflags=CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return pids
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].rsplit(":", 1)[-1] == str(port):
                try:
                    pids.add(int(parts[4]))
                except ValueError:
                    pass
    return pids


def vram_used_mib():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                              "--format=csv,noheader,nounits"], capture_output=True,
                             text=True, timeout=30,
                             creationflags=CREATE_NO_WINDOW).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def gpu_pids(needles):
    """PIDs of GPU processes whose executable path contains one of these names."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name",
                              "--format=csv,noheader"], capture_output=True, text=True,
                             timeout=30, creationflags=CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        pid, _, path = line.partition(",")
        pid = pid.strip()
        if pid.isdigit() and any(n.lower() in path.lower() for n in needles):
            found.append(int(pid))
    return found


def kill_stragglers(name):
    """Kill what is left of a model on the GPU after the process holding its port is gone.

    A model's engine can outlive that process - Strata's server detaches its engine, so the
    job object does not reach it - and then the router is "stopped" while both cards are
    still taken.  models.<name>.stragglers names the executables to look for."""
    needles = MODELS[name].get("stragglers") or []
    if not needles:
        return
    for pid in sorted(set(gpu_pids(needles))):
        log("%s: PID %d still holds a GPU, stopping it" % (name, pid))
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       timeout=60, creationflags=CREATE_NO_WINDOW)


# ------------------------------------------------------------------- job object
# The service the router starts holds both V100s in its own process, so it must not outlive
# the router: closing the router's window is meant to be the whole stop, the way every other
# service on this PC is run.  A job object with KILL_ON_JOB_CLOSE does that even when the
# router is killed outright and gets no chance to clean up.
_kernel32 = None
JOB = None


def make_job():
    global _kernel32, JOB
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes

        class JobBasic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class JobExtended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", JobBasic),
                        ("IoInfo", ctypes.c_byte * 48),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JobExtended()
        info.BasicLimitInformation.LimitFlags = 0x00002000   # KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(job, 9, ctypes.byref(info),
                                                 ctypes.sizeof(info)):
            _kernel32.CloseHandle(job)
            return None
        JOB = job
        return job
    except Exception:                                  # noqa: BLE001 - the router works without it
        return None


def join_job(proc):
    """Put a freshly started backend under JOB, so it dies with the router."""
    if JOB is None:
        return
    if not _kernel32.AssignProcessToJobObject(JOB, int(proc._handle)):
        log("could not put %s (PID %d) in the router's job object (error %d): closing the "
            "router window may leave it running" % (proc.args, proc.pid,
                                                   ctypes.get_last_error()))


def set_state(name, state, detail=""):
    with ST_LOCK:
        entry = STATE[name]
        if entry["state"] != state:
            entry["since"] = time.strftime("%Y-%m-%d %H:%M:%S")
        entry["state"] = state
        entry["detail"] = detail


def set_pids(name, pids):
    with ST_LOCK:
        STATE[name]["pids"] = sorted(pids)


# --------------------------------------------------------------- start and stop
def stop(name, why=""):
    model = MODELS[name]
    pids = port_pids(model["_port"])
    with ST_LOCK:
        pids |= set(STATE[name].get("pids") or ())
    if pids:
        log("stopping %s (PIDs %s)%s" % (name, ", ".join(map(str, sorted(pids))),
                                         " - %s" % why if why else ""))
        set_state(name, "stopping", "PIDs %s" % ", ".join(map(str, sorted(pids))))
        for pid in sorted(pids):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           timeout=60, creationflags=CREATE_NO_WINDOW)
        deadline = time.time() + model["stop_timeout_s"]
        while time.time() < deadline and port_pids(model["_port"]):
            time.sleep(1.0)
        left = port_pids(model["_port"])
        set_pids(name, set())
        if left:
            set_state(name, "stopped", "port %d is still held by %s" % (model["_port"], left))
            log("%s: port %d is still held by %s" % (name, model["_port"], left))
        else:
            log("%s stopped" % name)
    kill_stragglers(name)          # the engine can outlive the process that held the port
    set_state(name, "stopped", "")


def wait_vram_free(limit_mib=1500, timeout=120):
    """Both models want both cards, so wait for the previous one to really let go."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        used = vram_used_mib()
        if not used or max(used) <= limit_mib:
            return True
        time.sleep(2.0)
    log("VRAM is still %s MiB per card after %ds; starting anyway"
        % (vram_used_mib(), timeout))
    return False


def start(name):
    model = MODELS[name]
    out = open(os.path.join(HERE, "backend-%s.log" % name), "ab", buffering=0)
    log("starting %s: %s  (cwd %s)" % (name, " ".join(model["start"]),
                                       model.get("cwd") or HERE))
    proc = subprocess.Popen(model["start"], cwd=model.get("cwd") or HERE,
                            stdout=out, stderr=subprocess.STDOUT,
                            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
    join_job(proc)
    return proc


def ensure(name, why, admin=False):
    """Make `name` the loaded model.  (True, '') on success, (False, reason) otherwise.

    A normal caller whose switch would cut off another model's in-flight answer waits
    switch.drain_seconds and is then refused with Busy.  An admin caller waits
    switch.admin_drain_seconds and then switches anyway: that answer is cut off."""
    ok, _ = ready(name)
    if ok:
        set_state(name, "ready", "already up - %s" % why)
        return True, ""

    # Switching means stopping the other model, and that kills whatever it is answering.
    # Give those answers a bounded chance to finish; the check is repeated under the lock.
    window = ADMIN_DRAIN_S if admin else SWITCH_DRAIN_S
    draining = [m for m in MODELS if m != name] if admin else []
    try:
        for other in draining:          # keep them clear, or they never finish draining
            admin_wants(other, True)
        deadline = time.time() + window
        while time.time() < deadline:
            if not busy_models(exclude=name):
                break
            time.sleep(1.0)
    finally:
        for other in draining:
            admin_wants(other, False)

    with LOAD_LOCK:
        ok, _ = ready(name)
        if ok:
            set_state(name, "ready", "already up - %s" % why)
            return True, ""

        busy = busy_models(exclude=name)
        if busy and not admin:
            log("not switching to %s: %s is answering a request, the caller retries"
                % (name, busy[0]))
            raise Busy(busy[0], 10)
        if busy:
            log("admin priority: switching to %s after %ds; %s was still answering, that "
                "request is cut off" % (name, int(window), busy[0]))

        for other in MODELS:
            if other != name and port_pids(MODELS[other]["_port"]):
                set_state(other, "stopping", "the router is switching to %s" % name)
                stop(other, "the router is switching to %s (%s)" % (name, why))

        if port_pids(MODELS[name]["_port"]):
            stop(name, "it holds port %d without answering" % MODELS[name]["_port"])

        wait_vram_free()
        set_state(name, "starting", "for %s" % why)
        proc = start(name)
        deadline = time.time() + MODELS[name]["start_timeout_s"]
        while time.time() < deadline:
            ok, _ = ready(name)
            if ok:
                set_pids(name, port_pids(MODELS[name]["_port"]))
                set_state(name, "ready", "loaded for %s" % why)
                log("%s is ready (%s)" % (name, why))
                return True, ""
            if proc.poll() is not None:
                message = ("%s exited with code %s before answering; see %s"
                           % (name, proc.returncode,
                              os.path.join(HERE, "backend-%s.log" % name)))
                set_state(name, "failed", message)
                log(message)
                return False, message
            time.sleep(3.0)
        message = ("%s did not answer within %ds; see %s"
                   % (name, MODELS[name]["start_timeout_s"],
                      os.path.join(HERE, "backend-%s.log" % name)))
        set_state(name, "failed", message)
        log(message)
        return False, message


def resolve_model(name):
    if not name:
        return None
    key = str(name).strip().lower()
    for candidate, model in MODELS.items():
        if key in model["_aliases"]:
            return candidate
    return None


def loaded_model():
    for candidate in MODELS:
        if ready(candidate, timeout=2.0)[0]:
            return candidate
    return None


def flatten_call_outputs(parsed):
    """Turn a function_call_output result given as content parts into the string NInfer wants.

    The Responses API allows `output` to be an array of parts; NInfer answers 400
    "function_call_output output must be a string".  Joining the text parts keeps the result
    exactly as the model would have read it.  Returns how many items were rewritten.
    """
    items = parsed.get("input")
    if not isinstance(items, list):
        return 0
    flattened = 0
    for item in items:
        if not (isinstance(item, dict) and item.get("type") == "function_call_output"):
            continue
        output = item.get("output")
        if not isinstance(output, list):
            continue
        texts = []
        for part in output:
            if isinstance(part, str):
                texts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
        item["output"] = "\n".join(texts)
        flattened += 1
    return flattened


def rewrite_body(body, model):
    """The body a backend gets: its own model id, and a reasoning effort it can accept.

    A backend's chat template exposes a fixed set of efforts and answers 400 for anything
    else - NInfer exposes low, medium and xhigh, so the OpenAI names "high" and "max" are
    refused.  models.<name>.effort_map maps those onto what that backend does accept.

    The effort can sit in one of three places, one per protocol:
        Chat Completions   "reasoning_effort": "high"
        Responses          "reasoning": {"effort": "high"}
        Anthropic Messages "output_config": {"effort": "high"}
    """
    if not body:
        return body
    try:
        parsed = json.loads(body)
    except ValueError:
        return body
    if not isinstance(parsed, dict):
        return body

    changed = False
    if model.get("send_model") and "model" in parsed:
        parsed["model"] = model["send_model"]
        changed = True

    effort_map = model.get("effort_map") or {}
    if effort_map:
        spots = []
        if isinstance(parsed.get("reasoning_effort"), str):
            spots.append((parsed, "reasoning_effort"))
        for parent in ("reasoning", "output_config"):
            node = parsed.get(parent)
            if isinstance(node, dict) and isinstance(node.get("effort"), str):
                spots.append((node, "effort"))
        for node, key in spots:
            if node[key] in effort_map:
                log("%s: reasoning effort %r -> %r"
                    % (model.get("send_model") or "?", node[key], effort_map[node[key]]))
                node[key] = effort_map[node[key]]
                changed = True

    if model.get("strip_rejected"):            # this backend takes the plain, stricter shape
        flattened = flatten_call_outputs(parsed)
        if flattened:
            changed = True
            log("%s: flattened %d function_call_output result(s) into strings"
                % (model.get("send_model") or "?", flattened))

    return json.dumps(parsed, ensure_ascii=False).encode("utf-8") if changed else body


MAX_STRIPS = 8


def backend_path(path):
    """The path the backend serves, whichever way the client wrote its API root.

    Clients disagree about where /v1 goes: Codex appends /responses to its base_url, so its
    base URL carries the /v1, while Claude Code appends /v1/messages, so its base URL must
    not.  Either mistake lands here - /responses gets the prefix, /v1/v1/messages loses the
    duplicate - so both reach the backend's own /v1/... path.  The router's own paths and
    the health endpoints are left alone.
    """
    while path.startswith("/v1/v1"):        # the base URL had /v1 and the client added another
        path = path[3:]
    if path == "/v1" or path.startswith(("/v1/", "/router", "/health", "/models")):
        return path
    return "/v1" + path


def drop_rejected(body, error_body):
    """(smaller body, what was dropped) for a 400 that names something the backend refuses.

    A strict backend rejects any field it does not implement and says which one, the way
    NInfer's Responses parser does:
        {"error": {"code": "text_option_not_supported", "param": "text",
                   "message": "text.verbosity is not supported"}}
    Removing just that field and retrying lets a client written against the fuller OpenAI
    API work unchanged.  Only models with "strip_rejected": true get this, so every other
    backend keeps the strict pass-through and a real client mistake is still reported.
    """
    try:
        error = json.loads(error_body or b"{}").get("error")
    except (ValueError, AttributeError):
        return None
    if not isinstance(error, dict):
        return None
    code = str(error.get("code") or "")
    if code != "unknown_parameter" and not code.endswith("_not_supported"):
        return None

    # a nested refusal says "text.verbosity is not supported"; otherwise error.param names it
    first = str(error.get("message") or "").split(" ")[0]
    path = first.split(".") if "." in first else []
    if not path:
        param = error.get("param")
        path = [param] if isinstance(param, str) and param else []
    if not path:
        return None

    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None

    # "tools" must never be dropped whole: that leaves the model with no tools at all and it
    # then writes a tool call as plain text.  NInfer takes function tools with strict off and
    # nothing else, so keep those and retry.
    if path == ["tools"] and isinstance(parsed.get("tools"), list):
        kept, changed = [], False
        for tool in parsed["tools"]:
            if not (isinstance(tool, dict) and tool.get("type") == "function"):
                changed = True                  # custom / hosted tools are not supported
                continue
            if tool.get("strict") is not False:
                tool["strict"] = False          # strict schema enforcement is refused too
                changed = True
            kept.append(tool)
        if not changed:
            return None                         # nothing we can fix: pass the 400 through
        parsed["tools"] = kept
        return (json.dumps(parsed, ensure_ascii=False).encode("utf-8"),
                "tools (kept %d function tools; this backend takes nothing else)" % len(kept))

    node = parsed
    for key in path[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return None
    if path[-1] not in node:
        return None
    del node[path[-1]]
    if len(path) > 1 and not node:                 # an emptied parent goes with it
        parent = parsed
        for key in path[:-2]:
            parent = parent[key]
        del parent[path[-2]]
        path = path[:-1]
    return json.dumps(parsed, ensure_ascii=False).encode("utf-8"), ".".join(path)


# -------------------------------------------------------------------- the proxy
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "model-router/1.1"

    def log_message(self, fmt, *args):        # the router logs its own lines
        pass

    def _body(self):
        length = self.headers.get("Content-Length")
        if length:
            if int(length) > MAX_BODY:
                raise TooLarge("%s bytes, the limit is %s" % (length, MAX_BODY))
            return self.rfile.read(int(length))
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks = []
            while True:
                try:
                    size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        return b""

    def _authorized(self, path):
        """Whether this request carries one of auth.keys (constant-time comparison)."""
        sent = ""
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            sent = header[7:].strip()
        if not sent:
            sent = (self.headers.get("x-api-key") or "").strip()
        self.presented_key = sent or "-"      # the rate limit is per key
        self.is_admin = bool(sent) and any(hmac.compare_digest(sent, k)
                                           for k in AUTH_ADMIN_KEYS)
        if not AUTH_KEYS or path in AUTH_EXEMPT:
            return True
        if not sent:
            return False
        matched = False
        for key in AUTH_KEYS:
            matched |= hmac.compare_digest(sent, key)
        return matched

    def _unauthorized(self):
        body = json.dumps({"error": {
            "message": "missing or wrong API key: send it as "
                       "\"Authorization: Bearer <key>\" or \"x-api-key: <key>\"",
            "type": "invalid_request_error", "code": "invalid_api_key"}}).encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Bearer realm="model-router"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _failed(self, name, reason):
        self._json(503, {"error": {"message": "the %s model is not available: %s"
                                              % (name, reason),
                                   "type": "router_unavailable",
                                   "code": "model_not_loaded"}})

    def _busy(self, model, message, retry_after):
        """503 with Retry-After: the router refuses rather than cut off a running answer."""
        payload = {"error": {
            "message": "%s: %s. Retry in about %d s - the router will not cut off the "
                       "answer that is running." % (model, message, int(retry_after)),
            "type": "router_busy", "code": "model_busy_retry"}}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(503)
        self.send_header("Retry-After", str(int(max(1, retry_after))))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _too_many(self, retry_after):
        payload = {"error": {
            "message": "rate limit reached (%g requests per minute per key); retry in "
                       "about %d s" % (RATE_PER_MIN, int(retry_after)),
            "type": "rate_limit_error", "code": "rate_limited"}}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(429)
        self.send_header("Retry-After", str(int(max(1, retry_after))))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _admin_priority(self, model, retry_after):
        """503: the model is being kept clear for an admin request, so load is shed."""
        payload = {"error": {
            "message": "the %s model is being kept clear for an admin request; retry in "
                       "about %d s" % (model, int(retry_after)),
            "type": "router_busy", "code": "admin_priority"}}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(503)
        self.send_header("Retry-After", str(int(max(1, retry_after))))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _admin_contention(self, model, owner, retry_after):
        """503: another admin is already loading a different model."""
        payload = {"error": {
            "message": "another admin request is already bringing up %s; the %s request "
                       "was not queued. Retry in about %d s."
                       % (owner, model, int(retry_after)),
            "type": "router_busy", "code": "admin_contention"}}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(503)
        self.send_header("Retry-After", str(int(max(1, retry_after))))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forbidden(self, path):
        """403: the key is valid but this control endpoint needs an admin key."""
        payload = {"error": {
            "message": "%s is an admin endpoint: send one of auth.admin_keys" % path,
            "type": "permission_error", "code": "admin_key_required"}}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle(self, method):
        try:
            body = self._body()
            path = self.path.split("?")[0]
            if not self._authorized(path):
                log("401 %s %s from %s (no or wrong API key)"
                    % (method, path, self.client_address[0]))
                return self._unauthorized()
            if AUTH_ADMIN_KEYS and path in ADMIN_ONLY_PATHS and not self.is_admin:
                log("403 %s %s from %s (an admin key is required)"
                    % (method, path, self.client_address[0]))
                return self._forbidden(path)
            if path == "/router/status":
                return self._status()
            if path == "/router/metrics":
                return self._metrics()
            if path == "/router/load":
                return self._load(body)
            if path == "/router/unload":
                return self._unload(body)
            if path in ("/v1/models", "/models") and method == "GET":
                return self._models()
            if path in ("/health", "/router/health") and method == "GET":
                # public and unauthenticated: say that we are alive, nothing about the inside
                return self._json(200, {"status": "ok", "service": "model-router"})
            name = self._target(method, path, body)
            if name is None:
                return self._json(404, {"error": {
                    "message": "no model to answer %s %s" % (method, path),
                    "type": "router_error"}})
            if not self.is_admin:                 # auth.admin_keys skip the rate limit
                allowed, wait = RATE.take(self.presented_key)
                if not allowed:
                    log("429 %s %s from %s (rate limit, retry in %.0fs)"
                        % (method, path, self.client_address[0], wait))
                    return self._too_many(wait)
                if name in admin_wanted():        # keep the queue clear for the admin
                    log("503 %s %s from %s (an admin request needs %s, load shed)"
                        % (method, path, self.client_address[0], name))
                    return self._admin_priority(name, 5)
            with ST_LOCK:
                state = STATE[name]["state"]
            if state == "stopping":
                return self._busy(name, "it is being taken down to make room for the other "
                                        "model", 5)
            counted = False
            claimed = False
            if self.is_admin:
                owner = claim_admin_switch(name)
                if owner:
                    log("503 %s %s from %s (another admin request is bringing up %s)"
                        % (method, path, self.client_address[0], owner))
                    return self._admin_contention(name, owner, 10)
                claimed = True
            try:
                if not self.is_admin:      # an admin request is never refused for load
                    if not INFLIGHT.acquire(blocking=False):
                        return self._json(503, {"error": {
                            "message": "too many requests in flight (%d); try again shortly"
                                       % MAX_INFLIGHT,
                            "type": "router_busy", "code": "too_many_requests"}})
                    counted = True
                reserve(name)
                if self.is_admin:
                    admin_wants(name, True)   # nothing normal joins this model's queue
                log("%s %s -> %s from %s%s" % (method, path, name, self.client_address[0],
                                               " (admin)" if self.is_admin else ""))
                try:
                    ok, detail = ensure(name, "%s %s" % (method, path),
                                        admin=self.is_admin)
                finally:
                    if claimed:           # the switch is over; another admin may try now
                        release_admin_switch(name)
                        claimed = False
                if not ok:
                    return self._failed(name, detail)
                self._forward(method, path, body, name)
            except Busy as exc:
                return self._busy(exc.model, str(exc), exc.retry_after)
            finally:
                release_model(name)
                if self.is_admin:
                    admin_wants(name, False)
                if counted:
                    INFLIGHT.release()
                if claimed:
                    release_admin_switch(name)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except TooLarge as exc:
            self._json(413, {"error": {"message": "request body too large: %s" % exc,
                                       "type": "invalid_request_error"}})
        except Exception as exc:                     # noqa: BLE001 - reported to the client
            log("error on %s %s: %r" % (method, self.path, exc))
            try:
                self._json(500, {"error": {"message": str(exc), "type": "router_error"}})
            except OSError:
                pass

    def _target(self, method, path, body):
        wanted = None
        if body:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                wanted = parsed.get("model")
        name = resolve_model(wanted)
        if name:
            return name
        if wanted:
            log("unknown model %r -> %s" % (wanted, DEFAULT))
        return loaded_model() or DEFAULT

    def _state_map(self):
        with ST_LOCK:
            return {name: {"state": STATE[name]["state"], "detail": STATE[name]["detail"],
                           "since": STATE[name]["since"], "port": MODELS[name]["_port"]}
                    for name in MODELS}

    def _models(self):
        with ST_LOCK:
            data = [{"id": name, "object": "model", "created": 0, "owned_by": "local",
                     "state": STATE[name]["state"],
                     "aliases": sorted(MODELS[name]["_aliases"])}
                    for name in MODELS]
        self._json(200, {"object": "list", "data": data})

    def _status(self):
        tail = ""
        try:
            with open(LOG_PATH, encoding="utf-8", errors="replace") as fh:
                tail = "".join(fh.readlines()[-25:])
        except OSError:
            pass
        self._json(200, {"service": "model-router", "default_model": DEFAULT,
                         "listen": CFG["listen"], "models": self._state_map(),
                         "in_flight": dict(INFLIGHT_BY_MODEL),
                         "admin_priority": admin_wanted(),
                         "admin_switch": admin_switch_owner(),
                         "limits": {"max_inflight": MAX_INFLIGHT,
                                    "max_body_bytes": MAX_BODY,
                                    "requests_per_minute": RATE_PER_MIN,
                                    "burst": RATE_BURST,
                                    "admin_keys": len(AUTH_ADMIN_KEYS),
                                    "switch_drain_seconds": SWITCH_DRAIN_S,
                                    "admin_drain_seconds": ADMIN_DRAIN_S},
                         "vram_used_mib": vram_used_mib(), "log_tail": tail})

    def _metrics(self):
        """Tokens, requests and output speed, counted by the router itself."""
        with USAGE_LOCK:
            snapshot = json.loads(json.dumps(USAGE))       # a copy, freed before summing
        snapshot["output_tok_per_s"] = speed(snapshot["timed_completion_tokens"],
                                            snapshot["generation_ms"])
        for bucket in ("by_model", "by_key"):
            for row in snapshot[bucket].values():
                row["output_tok_per_s"] = speed(row["timed_completion_tokens"],
                                               row["generation_ms"])
        self._json(200, snapshot)

    def _load(self, body):
        parsed = json.loads(body or b"{}")
        raw = parsed.get("model") or DEFAULT
        name = resolve_model(raw)
        if not name:
            return self._json(400, {"error": {"message": "unknown model %r" % raw}})
        ok, detail = ensure(name, "POST /router/load", admin=self.is_admin)
        self._json(200 if ok else 503, {"model": name, "state": STATE[name]["state"],
                                        "detail": detail or "ready"})

    def _unload(self, body):
        parsed = json.loads(body or b"{}")
        wanted = parsed.get("model") or "all"
        if str(wanted).lower() in ("all", "*"):
            names = list(MODELS)
        else:
            resolved = resolve_model(wanted)
            if not resolved:
                return self._json(400, {"error": {"message": "unknown model %r" % wanted}})
            names = [resolved]
        for name in names:
            stop(name, "POST /router/unload")
        self._json(200, {"unloaded": names, "models": self._state_map(),
                         "vram_used_mib": vram_used_mib()})

    def _relay(self, response, raw):
        """Forward a response already read in full - a 400 the router cannot fix."""
        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _forward(self, method, path, body, name):
        model = MODELS[name]
        body = rewrite_body(body, model)
        target = backend_path(path)
        if target != path:
            log("%s: forwarding %s as %s (the API root's /v1 was missing or doubled)"
                % (name, path, target))

        headers = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP or key.lower() in (
                    "host", "content-length", "authorization", "x-api-key"):
                continue
            headers[key] = value
        if body:
            headers["Content-Length"] = str(len(body))

        started = time.time()
        sniffer = UsageSniffer()
        strips = 0
        while True:
            conn = http.client.HTTPConnection(model["_host"], model["_port"], timeout=7200)
            try:
                conn.request(method, target, body=body if body else None, headers=headers)
                response = conn.getresponse()
            except OSError as exc:
                conn.close()
                log("%s: %s %s failed after the model was ready: %r"
                    % (name, method, path, exc))
                return self._failed(name, "the backend stopped answering (%s)" % exc)

            if not (model.get("strip_rejected") and response.status == 400 and body
                    and strips < MAX_STRIPS):
                break
            raw = response.read()
            conn.close()
            reduced = drop_rejected(body, raw)
            if reduced is None:                    # nothing we can fix: pass the 400 through
                record_usage(name, self.presented_key, path, response.status, None, None,
                             None, round((time.time() - started) * 1000), ok=False)
                return self._relay(response, raw)
            body, dropped = reduced
            headers["Content-Length"] = str(len(body))
            strips += 1
            log("%s: dropped %r, the backend refuses it (retrying, %d)"
                % (name, dropped, strips))

        length = response.getheader("Content-Length")
        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.lower() in HOP_BY_HOP or key.lower() == "content-length":
                continue
            self.send_header(key, value)
        if length is not None:
            self.send_header("Content-Length", length)
        else:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        try:
            if length is not None:
                left = int(length)
                while left > 0:
                    chunk = response.read(min(262144, left))
                    if not chunk:
                        break
                    sniffer.feed(chunk)
                    self.wfile.write(chunk)
                    left -= len(chunk)
            else:
                while True:
                    chunk = response.read1(262144)
                    if not chunk:
                        break
                    sniffer.feed(chunk)
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log("%s: the client went away mid-answer" % name)
        finally:
            conn.close()
            prompt, completion = sniffer.counts()
            duration_ms = round((time.time() - started) * 1000)
            # Only a streamed answer has a first token to time generation from: a
            # non-streaming body arrives in one piece after the model has finished, so its
            # "time to first token" would be the whole generation and the speed nonsense.
            streamed = length is None
            record_usage(name, self.presented_key, path, response.status, prompt, completion,
                         round((sniffer.first_token - started) * 1000)
                         if streamed and sniffer.first_token else None,
                         duration_ms)


def main():
    listen = CFG["listen"]
    log("model router listening on http://%s:%d/v1" % (listen["host"], listen["port"]))
    for name, model in MODELS.items():
        log("  %-10s aliases %s -> %s (port %d)"
            % (name, sorted(model["_aliases"]), model["base"], model["_port"]))
    log("  auth: %s%s" % ("one of %d API key(s)" % len(AUTH_KEYS) if AUTH_KEYS else "OFF",
                          " - exempt: %s" % ", ".join(sorted(AUTH_EXEMPT))
                          if AUTH_KEYS else ""))
    log("  closing this window stops the router and every model it started")
    if make_job() is None:
        log("  WARNING: no job object - a model started here would survive the router")
    for name in MODELS:                    # whoever is already up keeps that state across a restart
        if ready(name, timeout=3.0)[0]:
            set_pids(name, port_pids(MODELS[name]["_port"]))
            set_state(name, "ready", "already up when the router started")
            log("  %s is already up on port %d" % (name, MODELS[name]["_port"]))
        else:
            kill_stragglers(name)          # an engine left over from a killed router
    threading.Thread(target=usage_beat, daemon=True).start()   # the minute summary line
    server = ThreadingHTTPServer((listen["host"], listen["port"]), Handler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
