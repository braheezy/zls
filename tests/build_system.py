#!/usr/bin/env python3
"""Standalone stock-Zig build-system integration regressions (Python 3 stdlib).

Run: python3 tests/build_system.py --zls /absolute/path/to/zls --zig /absolute/path/to/zig
Each case starts a fresh ZLS and workspace. No external dependencies or network
are needed. --cache-dir /absolute/zig/cache reuses an existing global compiler
cache; ZLS configuration caches and workspace-local caches remain temporary.
Readiness is established by actual build-only named-module definition/hover
(or the running-build helper marker), not by sleeping after initialize. All
waits, including shutdown, are bounded.
"""

import argparse
import collections
import json
import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

BUILD = """const std = @import("std");
pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});
    const custom = b.option(bool, "custom", "Select custom module") orelse false;
    const root = b.createModule(.{
        .root_source_file = b.path("main.zig"),
        .target = target,
        .optimize = optimize,
    });
    root.addImport("named", b.createModule(.{
        .root_source_file = b.path(if (custom) "custom.zig" else "named.zig"),
    }));
    const options = b.addOptions();
    options.addOption(u32, "generated_value", 73);
    root.addOptions("generated_options", options);
    const obj = b.addObject(.{ .name = "fixture", .root_module = root });
    b.step("check", "Compile fixture").dependOn(&obj.step);
}
"""
MAIN = """const named = @import("named");
const options = @import("generated_options");
pub export fn fixture_value() u32 {
    return named.answer + options.generated_value;
}
"""


class Failure(Exception):
    pass


class FatalBuildError(Failure):
    pass


CASES = (
    "named_module",
    "generated_options",
    "relative_cache",
    "build_options",
    "local_dependency",
    "build_on_save",
    "build_on_save_default_true",
    "build_on_save_default_null",
    "configuration_recovery",
    "shutdown_running_build",
)


class Client:
    def __init__(
        self,
        executable,
        zig,
        workspace,
        cache,
        timeout,
        build_on_save,
        build_args=(),
        cwd=None,
        local_cache=None,
        global_cache=None,
    ):
        self.workspace = workspace
        self.timeout = timeout
        self.messages = queue.Queue()
        self.stderr = collections.deque(maxlen=200)
        self.notifications = []
        self.responses = {}
        self.sequence = 0
        self.configuration_replies = 0
        self.fatal_error = None
        self.closing = False
        self.expected_configuration_failure = False
        self.expected_failure_logs = []
        self.config = {
            "zig_exe_path": str(zig),
            "global_cache_path": str(cache / "zls"),
            "enable_build_on_save": build_on_save,
            "build_on_save_args": list(build_args),
        }
        env = os.environ.copy()
        env.pop("ZIG_LOCAL_CACHE_DIR", None)
        if local_cache is not None:
            env["ZIG_LOCAL_CACHE_DIR"] = local_cache
        env["ZIG_GLOBAL_CACHE_DIR"] = str(global_cache or cache / "zig")
        env["XDG_CACHE_HOME"] = str(cache / "xdg")
        self.env = env
        self.process = subprocess.Popen(
            [str(executable), "--enable-stderr-logs", "--log-level", "debug"],
            cwd=str(cwd or workspace),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.readers = [
            threading.Thread(target=self._stdout, daemon=True),
            threading.Thread(target=self._stderr, daemon=True),
        ]
        for reader in self.readers:
            reader.start()

    def _stdout(self):
        try:
            while True:
                headers = {}
                while True:
                    line = self.process.stdout.readline()
                    if not line:
                        raise EOFError("ZLS stdout closed")
                    if line in (b"\r\n", b"\n"):
                        break
                    key, value = line.decode("ascii").split(":", 1)
                    headers[key.lower()] = value.strip()
                remaining = int(headers["content-length"])
                chunks = []
                while remaining:
                    chunk = self.process.stdout.read(remaining)
                    if not chunk:
                        raise EOFError("ZLS stdout closed inside a message")
                    chunks.append(chunk)
                    remaining -= len(chunk)
                self.messages.put(json.loads(b"".join(chunks)))
        except Exception as exc:
            self.messages.put(exc)

    def _stderr(self):
        for raw in iter(self.process.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            self.stderr.append(line)
            if not self.closing and line.startswith("error") and "( bsp" in line:
                if self.is_expected_configuration_error(line):
                    self.expected_failure_logs.append(line)
                else:
                    self.fatal_error = FatalBuildError(
                        "fatal build-system log: " + line
                    )
                    self.messages.put(self.fatal_error)

    def is_expected_configuration_error(self, line):
        # Only configuration_recovery enables this, from the intentional bad
        # build.zig save until a restored build has published compiler diagnostics.
        # Do not forgive OOM, protocol errors, or unrelated startup failures.
        if not self.expected_configuration_failure:
            return False
        message = line.partition("): ")[2]
        patterns = (
            r"failed to receive message from build system server: EndOfStream",
            r"EndOfStream from build system server in .+",
            r"failed to send message to build system server in .+: BrokenPipe",
            r"build system server in .+ exited with code [12](?: with stderr:)?",
        )
        if " in " in message and str(self.workspace) not in message:
            return False
        return any(re.fullmatch(pattern, message) for pattern in patterns)

    def send(self, message):
        data = json.dumps(dict(jsonrpc="2.0", **message)).encode("utf-8")
        try:
            self.process.stdin.write(
                ("Content-Length: %d\r\n\r\n" % len(data)).encode("ascii") + data
            )
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise Failure("cannot write to ZLS: %s" % exc) from exc

    def notify(self, method, params):
        self.send({"method": method, "params": params})

    def pump(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Failure("deadline expired waiting for LSP message")
        try:
            message = self.messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise Failure("deadline expired waiting for LSP message") from exc
        if isinstance(message, FatalBuildError) and self.closing:
            return
        if isinstance(message, Exception):
            raise Failure(str(message))
        if "method" in message and "id" in message:
            method = message["method"]
            if method == "workspace/configuration":
                result = [self.config for _ in message["params"]["items"]]
                self.configuration_replies += 1
            elif method in (
                "client/registerCapability",
                "window/workDoneProgress/create",
            ):
                result = None
            else:
                self.send(
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "unsupported client request: " + method,
                        },
                    }
                )
                raise Failure("unexpected server request: " + method)
            self.send({"id": message["id"], "result": result})
        elif "id" in message:
            self.responses[message["id"]] = message
        else:
            self.notifications.append(message)

    def request(self, method, params, deadline=None):
        if self.fatal_error and not self.closing:
            raise self.fatal_error
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params})
        deadline = min(deadline or float("inf"), time.monotonic() + self.timeout)
        while request_id not in self.responses:
            self.pump(deadline)
        response = self.responses.pop(request_id)
        if "error" in response:
            raise Failure("%s: %s" % (method, response["error"]))
        return response.get("result")

    def initialize(self):
        result = self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self.workspace.as_uri(),
                "workspaceFolders": [
                    {"uri": self.workspace.as_uri(), "name": "fixture"}
                ],
                "capabilities": {
                    "workspace": {"configuration": True},
                    "textDocument": {"publishDiagnostics": {}},
                },
                "initializationOptions": self.config,
            },
        )
        self.notify("initialized", {})
        deadline = time.monotonic() + self.timeout
        while not self.configuration_replies:
            self.pump(deadline)
        return result.get("serverInfo", {})

    def open(self, path):
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": path.as_uri(),
                    "languageId": "zig",
                    "version": 1,
                    "text": path.read_text(),
                }
            },
        )

    def lookup(self, method, path, needle, deadline):
        text = path.read_text()
        offset = text.index(needle)
        before = text[:offset]
        return self.request(
            method,
            {
                "textDocument": {"uri": path.as_uri()},
                "position": {
                    "line": before.count("\n"),
                    "character": len(before.rsplit("\n", 1)[-1]),
                },
            },
            deadline,
        )

    def eventually(self, label, check, seconds):
        deadline = time.monotonic() + seconds
        attempts = 0
        last = None
        while time.monotonic() < deadline and attempts < int(seconds / 0.25) + 1:
            if self.fatal_error:
                raise self.fatal_error
            attempts += 1
            ok, last = check(deadline)
            if ok:
                return attempts
            # Backoff only between observed unsuccessful requests, never readiness by sleep.
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        raise Failure(
            "%s not ready after %d attempts; last result: %r" % (label, attempts, last)
        )

    def wait_diagnostics(self, uri, after, predicate, seconds, phase=None):
        if phase:
            print(
                "PHASE %s: waiting for diagnostics on %s (timeout=%gs)"
                % (phase, uri, seconds),
                flush=True,
            )
        deadline = time.monotonic() + seconds
        cursor = after
        seen = []
        while True:
            while cursor < len(self.notifications):
                message = self.notifications[cursor]
                cursor += 1
                if message.get("method") != "textDocument/publishDiagnostics":
                    continue
                params = message["params"]
                if params["uri"] == uri:
                    seen.append(params)
                    if predicate(params["diagnostics"]):
                        if phase:
                            print(
                                "PHASE %s: PASS %s" % (phase, json.dumps(params)),
                                flush=True,
                            )
                        return params
            try:
                self.pump(deadline)
            except Failure as exc:
                raise Failure(
                    "diagnostics wait failed [%s] on %s: %s; observed: %s"
                    % (phase or "unlabelled", uri, exc, json.dumps(seen))
                ) from exc

    def close(self, seconds=None, cleanup=True):
        self.closing = True
        deadline = time.monotonic() + (seconds or self.timeout)
        try:
            self.request("shutdown", None, deadline)
            self.notify("exit", None)
            self.process.stdin.close()
            try:
                code = self.process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise Failure("ZLS did not exit after shutdown/exit") from exc
            if code != 0:
                raise Failure("ZLS exited with status %d" % code)
        finally:
            if cleanup:
                self.cleanup()

    def cleanup(self):
        # Kept separate so shutdown_running_build can check for leaked children
        # BEFORE the harness kills anything. Cleanup must not manufacture a pass.
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait(timeout=5)
        for reader in self.readers:
            reader.join(timeout=2)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


def locations(result):
    if result is None:
        return []
    return result if isinstance(result, list) else [result]


def location_path(location):
    uri = location.get("uri", location.get("targetUri", ""))
    return Path(unquote(urlparse(uri).path))


def resolved(client, path, needle, expected, seconds):
    def check(deadline):
        definition = client.lookup("textDocument/definition", path, needle, deadline)
        hover = client.lookup("textDocument/hover", path, needle, deadline)
        targets = [location_path(item) for item in locations(definition)]
        return bool(hover) and any(expected(target) for target in targets), {
            "definition": definition,
            "hover": hover,
        }

    return client.eventually(needle + " definition and hover", check, seconds)


def compiler_proof(client, zig, seconds, build_args, main_source):
    """Check the fixture independently, without touching the watched source tree."""
    proof = client.workspace / "compiler-proof"
    proof.mkdir()
    for name in ("build.zig", "main.zig", "named.zig", "custom.zig"):
        (proof / name).write_text((client.workspace / name).read_text())

    def build():
        process = subprocess.Popen(
            [str(zig), "build"] + list(build_args),
            cwd=str(proof),
            env=client.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            try:
                stdout, stderr = process.communicate(timeout=seconds)
            except subprocess.TimeoutExpired as exc:
                raise Failure("fixture compiler preflight timed out") from exc
            return process.returncode, (stdout + stderr).decode(
                "utf-8", errors="replace"
            )
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

    code, output = build()
    if code != 0:
        raise Failure(
            "valid fixture failed zig build %s:\n%s" % (" ".join(build_args), output)
        )
    (proof / "main.zig").write_text(
        main_source.replace(
            "return named.answer + options.generated_value;",
            '@compileError("build-system-regression-sentinel");',
        )
    )
    code, output = build()
    if code == 0 or "build-system-regression-sentinel" not in output:
        raise Failure("invalid fixture did not produce compiler sentinel:\n" + output)
    print(
        "PROOF %s: zig build %s succeeds for valid fixture and fails with compiler sentinel"
        % (client.workspace.name, " ".join(build_args)),
        flush=True,
    )


def main_diagnostic_cycle(client, main, source, sentinel, seconds, published=None):
    marker = len(client.notifications)
    main.write_text(
        source.replace(
            "return named.answer + options.generated_value;",
            '@compileError("%s");' % sentinel,
        )
    )
    client.notify("textDocument/didSave", {"textDocument": {"uri": main.as_uri()}})
    diagnostic = client.wait_diagnostics(
        main.as_uri(),
        marker,
        lambda ds: any(d.get("severity") == 1 and sentinel in d["message"] for d in ds),
        seconds,
        phase=sentinel + ": publish main compiler sentinel",
    )
    if published:
        published()
    marker = len(client.notifications)
    main.write_text(source)
    client.notify("textDocument/didSave", {"textDocument": {"uri": main.as_uri()}})
    client.wait_diagnostics(
        main.as_uri(),
        marker,
        lambda ds: not ds,
        seconds,
        phase=sentinel + ": clear main compiler diagnostics",
    )
    return diagnostic


def configuration_recovery(client, main, source, build_source, seconds):
    build = client.workspace / "build.zig"
    marker = len(client.notifications)
    client.open(build)
    client.wait_diagnostics(
        build.as_uri(),
        marker,
        lambda ds: not ds,
        seconds,
        phase="configuration_recovery: initial build.zig diagnostics",
    )
    # Advance the old session's version counter beyond the new session's first
    # build, so resetting versions cannot quietly suppress recovered diagnostics.
    for cycle in range(3):
        main_diagnostic_cycle(
            client, main, source, "configuration-recovery-before-%d" % cycle, seconds
        )
    client.expected_configuration_failure = True
    marker = len(client.notifications)
    build.write_text(
        build_source
        + '\ncomptime { @compileError("configuration-recovery-build-sentinel"); }\n'
    )
    # The LSP buffer remains valid; the build.zig diagnostic must come from Zig.
    client.notify("textDocument/didSave", {"textDocument": {"uri": build.as_uri()}})
    client.wait_diagnostics(
        build.as_uri(),
        marker,
        lambda ds: any(
            d.get("severity") == 1
            and "configuration-recovery-build-sentinel" in d["message"]
            for d in ds
        ),
        seconds,
        phase="configuration_recovery: publish broken build.zig compiler sentinel",
    )
    marker = len(client.notifications)
    build.write_text(build_source)
    client.notify("textDocument/didSave", {"textDocument": {"uri": build.as_uri()}})
    client.wait_diagnostics(
        build.as_uri(),
        marker,
        lambda ds: not ds,
        seconds,
        phase="configuration_recovery: clear restored build.zig diagnostics",
    )

    def recovered():
        client.expected_configuration_failure = False

    for cycle in range(2):
        main_diagnostic_cycle(
            client,
            main,
            source,
            "configuration-recovery-after-%d" % cycle,
            seconds,
            published=recovered,
        )
    if client.fatal_error:
        raise client.fatal_error
    return (
        "3 main publish/clear cycles before restart, build.zig compiler error published/cleared, 2 main cycles after; %d expected startup logs"
        % len(client.expected_failure_logs)
    )


def helper_running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A dead, adopted zombie cannot execute and cannot be reaped by this script.
    result = subprocess.run(
        ["ps", "-o", "ppid=", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=2,
    )
    fields = result.stdout.split()
    if result.returncode == 1 and not fields:
        return False
    if result.returncode != 0 or len(fields) != 2:
        raise Failure("cannot inspect helper PID %d: %s" % (pid, result.stderr))
    return not (fields[0] == "1" and fields[1].startswith("Z"))


def shutdown_running_build(client, seconds):
    marker = client.workspace / "helper-started"
    record = client.workspace / "helper-pid.json"

    def started(deadline):
        if not marker.is_file():
            return False, "helper has not written its start marker"
        info = json.loads(record.read_text())
        return helper_running(info["pid"]), info

    client.eventually("running Python build helper marker", started, seconds)
    info = json.loads(record.read_text())
    started_at = time.monotonic()
    client.close(seconds=7.5, cleanup=False)
    elapsed = time.monotonic() - started_at
    if elapsed >= 8:
        raise Failure("shutdown took %.3fs, expected <8s" % elapsed)
    deadline = time.monotonic() + 3
    while helper_running(info["pid"]):
        if time.monotonic() >= deadline:
            raise Failure(
                "Python helper PID %d survived clean ZLS shutdown (pgid=%d)"
                % (info["pid"], info["pgid"])
            )
        time.sleep(0.05)
    return (
        "shutdown completed in %.3fs (<8s) during 60s build helper; PID %d absent or dead/adopted, checked before harness cleanup"
        % (elapsed, info["pid"])
    )


def run_case(name, args, root, cache):
    case_root = root / name
    case_root.mkdir()
    workspace = case_root
    cwd = workspace
    local_cache = None
    if name == "relative_cache":
        workspace = case_root / "workdir"
        workspace.mkdir()
        cwd = case_root / "server-cwd"
        cwd.mkdir()
        local_cache = "relative-zig-cache"
    build_on_save = name.startswith("build_on_save") or name == "configuration_recovery"
    enable = (
        None
        if name == "build_on_save_default_null"
        else (build_on_save or name == "shutdown_running_build")
    )
    build_args = (
        ["verify", "-Dcustom=true"]
        if name == "build_on_save"
        else (["check"] if name == "shutdown_running_build" else [])
    )
    use_custom = name in ("build_options", "build_on_save")
    build_source = BUILD
    main_source = MAIN
    if name == "build_on_save":
        # There is no check step and install does no compilation. Both explicit
        # step selection AND the -D option are necessary to pass this fixture.
        build_source = BUILD.replace('b.step("check",', 'b.step("verify",')
        main_source += '\ncomptime { if (named.answer != 99) @compileError("custom flag missing"); }\n'
    if name == "shutdown_running_build":
        helper = workspace / "sleeping-helper.py"
        helper.write_text("""import json
import os
from pathlib import Path
import sys
import time
Path(sys.argv[2]).write_text(json.dumps({"pid": os.getpid(), "pgid": os.getpgrp()}))
Path(sys.argv[1]).write_text("running")
time.sleep(60)
""")
        command = ", ".join(
            json.dumps(str(path))
            for path in (
                Path(sys.executable).resolve(),
                helper,
                workspace / "helper-started",
                workspace / "helper-pid.json",
            )
        )
        build_source = BUILD.replace(
            '    b.step("check", "Compile fixture").dependOn(&obj.step);',
            '    const check = b.step("check", "Compile fixture");\n'
            "    check.dependOn(&obj.step);\n"
            "    const sleeper = b.addSystemCommand(&.{%s});\n"
            "    check.dependOn(&sleeper.step);" % command,
        )
    if name == "local_dependency":
        build_source = BUILD.replace(
            "    const options = b.addOptions();",
            '    root.addImport("local_package", b.dependency("child", .{}).module("child_api"));\n'
            "    const options = b.addOptions();",
        )
        main_source = MAIN.replace(
            'const named = @import("named");',
            'const named = @import("named");\nconst local_package = @import("local_package");',
        ).replace(
            "return named.answer +",
            "return named.answer + local_package.child_answer +",
        )
        child = workspace / "child"
        child.mkdir()
        (workspace / "build.zig.zon").write_text(""".{
    .name = .integration_root,
    .version = "0.0.0",
    .fingerprint = 0x236ac440e93badbf,
    .dependencies = .{ .child = .{ .path = "child" } },
    .paths = .{""},
}
""")
        (child / "build.zig.zon").write_text(""".{
    .name = .integration_child,
    .version = "0.0.0",
    .fingerprint = 0xa8e303f883fe34d2,
    .paths = .{""},
}
""")
        (child / "build.zig").write_text("""const std = @import("std");
pub fn build(b: *std.Build) void {
    _ = b.addModule("child_api", .{ .root_source_file = b.path("api.zig") });
}
""")
        (child / "api.zig").write_text("pub const child_answer: u32 = 17;\n")
    for filename, content in {
        "build.zig": build_source,
        "main.zig": main_source,
        "named.zig": "pub const answer: u32 = 41;\n",
        "custom.zig": "pub const answer: u32 = 99;\n",
    }.items():
        (workspace / filename).write_text(content)
    if use_custom:
        (workspace / "zls.build.json").write_text(
            json.dumps(
                {
                    "build_options": [{"name": "custom", "value": "true"}],
                }
            )
        )
    client = Client(
        args.zls,
        args.zig,
        workspace,
        cache,
        args.request_timeout,
        enable,
        build_args=build_args,
        cwd=cwd,
        local_cache=local_cache,
        global_cache=args.cache_dir,
    )
    error = None
    details = ""
    try:
        info = client.initialize()
        main = workspace / "main.zig"
        client.open(main)
        # This import cannot resolve via relative-file fallback. Its successful
        # lookup proves asynchronous configuration AND build extraction completed.
        expected_module = workspace / ("custom.zig" if use_custom else "named.zig")
        attempts = (
            0
            if name == "shutdown_running_build"
            else resolved(
                client,
                main,
                "answer",
                lambda p: p == expected_module,
                args.ready_timeout,
            )
        )
        if build_on_save:
            compiler_proof(
                client,
                args.zig,
                args.ready_timeout,
                build_args or ["check"],
                main_source,
            )
        if name == "named_module":
            details = "named import definition + hover; ready in %d requests" % attempts
        elif name in ("generated_options", "relative_cache"):

            def generated(p):
                return (
                    p.is_relative_to(workspace / (local_cache or ".zig-cache"))
                    and p.is_file()
                    and "generated_value" in p.read_text()
                )

            count = resolved(
                client, main, "generated_value", generated, args.ready_timeout
            )
            details = (
                "generated options definition + hover in workspace %s (%d attempts); server cwd=%s"
                % (local_cache or ".zig-cache", count, cwd)
            )
        elif name == "build_options":
            count = attempts
            details = (
                "zls.build.json -Dcustom=true selects custom.zig (%d attempts)" % count
            )
        elif name == "local_dependency":
            count = resolved(
                client,
                main,
                "child_answer",
                lambda p: p == workspace / "child" / "api.zig",
                args.ready_timeout,
            )
            details = (
                "local build.zig.zon path dependency definition + hover (%d attempts)"
                % count
            )
        elif name == "configuration_recovery":
            details = configuration_recovery(
                client, main, main_source, build_source, args.ready_timeout
            )
        elif name == "shutdown_running_build":
            details = shutdown_running_build(client, args.ready_timeout)
        elif build_on_save:
            bad = main_source.replace(
                "return named.answer + options.generated_value;",
                '@compileError("build-system-regression-sentinel");',
            )
            marker = len(client.notifications)
            main.write_text(bad)
            # Leave the LSP buffer valid: only the compiler can see the disk-only
            # sentinel. Likewise, clearing cannot be an AST didChange side effect.
            client.notify(
                "textDocument/didSave", {"textDocument": {"uri": main.as_uri()}}
            )
            diagnostic = client.wait_diagnostics(
                main.as_uri(),
                marker,
                lambda ds: any(
                    d.get("severity") == 1
                    and "build-system-regression-sentinel" in d["message"]
                    for d in ds
                ),
                args.ready_timeout,
            )
            marker = len(client.notifications)
            main.write_text(main_source)

            client.notify(
                "textDocument/didSave", {"textDocument": {"uri": main.as_uri()}}
            )
            client.wait_diagnostics(
                main.as_uri(), marker, lambda ds: not ds, args.ready_timeout
            )
            details = (
                "args=%s enable=%s; compiler sentinel published then diagnostics cleared after disk edit/didSave: "
                % (json.dumps(build_args), json.dumps(enable))
                + json.dumps(diagnostic)
            )
        details += "; server=" + json.dumps(info)
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            if not client.closing:
                client.close()
            else:
                client.cleanup()
        except Exception as exc:
            error = (error + "; " if error else "") + "shutdown: " + str(exc)
        finally:
            # Core build runners may use a different group from ZLS. Kill the
            # recorded helper group on failure only AFTER the leak assertion.
            record = workspace / "helper-pid.json"
            if name == "shutdown_running_build" and record.is_file():
                try:
                    info = json.loads(record.read_text())
                    if helper_running(info["pid"]):
                        if info["pgid"] == os.getpgrp():
                            raise Failure(
                                "helper unexpectedly shares harness process group"
                            )
                        os.killpg(info["pgid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except Exception as exc:
                    error = (
                        (error + "; " if error else "") + "helper cleanup: " + str(exc)
                    )
    if error:
        print("FAIL %s: %s" % (name, error), flush=True)
        print(
            "--- %s ZLS stderr ---\n%s\n--- end stderr ---"
            % (name, "\n".join(client.stderr)),
            flush=True,
        )
        return False
    print("PASS %s: %s; clean shutdown" % (name, details), flush=True)
    return True


def absolute_executable(value):
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise argparse.ArgumentTypeError(
            "must be an absolute path to an executable file"
        )
    return path


def absolute_cache_directory(value):
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise argparse.ArgumentTypeError(
            "must be an absolute path to an existing cache directory"
        )
    return path


def positive_seconds(value):
    number = float(value)
    if not 0 < number < float("inf"):
        raise argparse.ArgumentTypeError("must be finite and positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zls", required=True, type=absolute_executable)
    parser.add_argument("--zig", required=True, type=absolute_executable)
    parser.add_argument("--request-timeout", type=positive_seconds, default=30)
    parser.add_argument("--ready-timeout", type=positive_seconds, default=90)
    parser.add_argument(
        "--cache-dir",
        type=absolute_cache_directory,
        help="reuse this Zig global cache; all other caches remain temporary",
    )
    parser.add_argument(
        "--case",
        choices=CASES,
        action="append",
        help="run only selected cases (repeatable)",
    )
    args = parser.parse_args()
    cases = args.case or CASES
    with tempfile.TemporaryDirectory(prefix="zls-build-system-") as directory:
        root = Path(directory)
        cache = root / "cache"
        cache.mkdir()
        print(
            "Temporary workspaces/caches: %s; Zig global cache: %s"
            % (root, args.cache_dir or cache / "zig"),
            flush=True,
        )
        passed = sum(run_case(name, args, root, cache) for name in dict.fromkeys(cases))
    total = len(set(cases))
    print(
        "RESULT: %d passed, %d failed, 0 skipped" % (passed, total - passed), flush=True
    )
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
