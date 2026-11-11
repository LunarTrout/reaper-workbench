"""Local file-mailbox client for the resident ReaScript bridge.

The mailbox is serialized across Python callers and uses atomic file replacement.
It is local IPC only: no network listener, process injection, or memory scanning.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from ..errors import (ApiUnavailable, BridgeTimeout, BridgeUnavailable,
                      ReaperAudioError, StaleContext, TransactionFailed,
                      ValidationError)
from .api_catalog import ApiCallResult, get_api_catalog
from .handles import ReaperHandle

_REMOTE_ERRORS = {
    "api_unavailable": ApiUnavailable,
    "bridge_timeout": BridgeTimeout,
    "stale_context": StaleContext,
    "transaction_failed": TransactionFailed,
    "validation_error": ValidationError,
}
_INSTANCE_STALE_AFTER_S = 12.0


def _pid_alive(pid: int) -> bool:
    """Check a PID without depending on psutil (Windows and POSIX)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
        if not handle:
            return False
        try:
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def default_runtime_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        raise ValidationError("LOCALAPPDATA is not set; pass runtime_dir explicitly")
    return Path(root) / "REAPER-Audio-Library" / "runtime"


class ReaperBridge:
    """Synchronous client for a single resident ReaScript service.

    Each call writes a request to ``request.json`` and waits for a response file
    named by the request ID. The Lua service consumes and removes the mailbox
    before invoking REAPER, so a timed-out call cannot overwrite an in-flight
    request. Stale calls include an expiry and must be discarded by the service.
    """

    def __init__(self, runtime_dir: str | Path | None = None, *,
                 bridge_session_id: str | None = None, poll_s: float = 0.02,
                 lock_stale_s: float = 300.0):
        self.runtime_root = Path(runtime_dir) if runtime_dir else default_runtime_dir()
        self.bridge_session_id = bridge_session_id
        if bridge_session_id:
            self.runtime_dir: Path | None = None
        elif runtime_dir is not None:
            # Explicit directories retain the direct mailbox mode used for
            # isolated tests and custom single-instance installations.
            self.runtime_dir = Path(runtime_dir)
        else:
            self.runtime_dir = None
        self.responses: Path | None = None
        self.request_path: Path | None = None
        self.lock_path: Path | None = None
        self.poll_s = max(0.005, float(poll_s))
        self.lock_stale_s = max(30.0, float(lock_stale_s))

    def discover_instances(self) -> list[dict[str, Any]]:
        """Return fresh resident ReaScript endpoints under this runtime root."""
        registry = self.runtime_root / "instances"
        now = time.time()
        instances: list[dict[str, Any]] = []
        try:
            descriptors = list(registry.glob("*.json"))
        except OSError:
            return instances
        for descriptor_path in descriptors:
            try:
                descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            session_id = descriptor.get("bridge_session_id")
            pulse_path = self.runtime_root / "sessions" / str(session_id) / "heartbeat"
            try:
                last_seen = pulse_path.stat().st_mtime
            except OSError:
                continue
            if (not isinstance(session_id, str) or session_id != descriptor_path.stem or
                    not re.fullmatch(r"[0-9a-fA-F]+-[0-9a-fA-F]+", session_id) or
                    descriptor.get("protocol") != 1 or
                    not math.isfinite(last_seen) or now - last_seen > _INSTANCE_STALE_AFTER_S or
                    last_seen - now > 60):
                continue
            descriptor["last_seen_epoch"] = last_seen
            descriptor["runtime_dir"] = str(
                self.runtime_root / "sessions" / session_id)
            instances.append(descriptor)
        return sorted(instances, key=lambda value: value["bridge_session_id"])

    def _resolve_runtime_dir(self) -> Path:
        if self.runtime_dir is not None:
            return self.runtime_dir
        instances = self.discover_instances()
        if self.bridge_session_id:
            selected = next((instance for instance in instances
                             if instance["bridge_session_id"] == self.bridge_session_id), None)
            if selected is None:
                raise BridgeUnavailable(
                    "The selected REAPER instance is not advertising a live ReaScript bridge",
                    details={"bridge_session_id": self.bridge_session_id,
                             "available_instances": [item["bridge_session_id"]
                                                     for item in instances]},
                )
        elif len(instances) == 1:
            selected = instances[0]
            self.bridge_session_id = selected["bridge_session_id"]
        elif not instances:
            raise BridgeUnavailable(
                "No live REAPER bridge was discovered. Start the resident ReaScript in REAPER.",
                details={"runtime_root": str(self.runtime_root)},
            )
        else:
            raise ValidationError(
                "Multiple REAPER instances are active; select bridge_session_id explicitly",
                details={"available_instances": [
                    {"bridge_session_id": item["bridge_session_id"],
                     "reaper_version": item.get("reaper_version"),
                     "active_project": item.get("active_project")}
                    for item in instances
                ]},
            )
        self.runtime_dir = Path(selected["runtime_dir"])
        return self.runtime_dir

    def _prepare(self) -> None:
        self.runtime_dir = self._resolve_runtime_dir()
        self.responses = self.runtime_dir / "responses"
        self.request_path = self.runtime_dir / "request.json"
        self.lock_path = self.runtime_dir / "client.lock"
        self.responses.mkdir(parents=True, exist_ok=True)

    def _acquire_lock(self, deadline: float, owner: str) -> None:
        assert self.lock_path is not None
        while time.monotonic() < deadline:
            try:
                descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age_s = time.time() - self.lock_path.stat().st_mtime
                    if age_s > self.lock_stale_s:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                    # A killed client leaves the file behind; its PID no longer
                    # exists, so the lock is free immediately.
                    owner_text = self.lock_path.read_text(encoding="ascii").strip()
                    pid_text = owner_text.split(":", 1)[0]
                    if pid_text.isdigit() and not _pid_alive(int(pid_text)):
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except (FileNotFoundError, OSError):
                    continue
                time.sleep(self.poll_s)
                continue
            with os.fdopen(descriptor, "w", encoding="ascii") as lock:
                lock.write(owner)
                lock.flush()
                os.fsync(lock.fileno())
            return
        raise BridgeTimeout("Timed out waiting for the local REAPER bridge mailbox",
                            details={"timeout": "client_lock"})

    def _release_lock(self, owner: str) -> None:
        assert self.lock_path is not None
        try:
            if self.lock_path.read_text(encoding="ascii") == owner:
                self.lock_path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass

    def _remove_pending_request(self, request_id: str) -> None:
        assert self.request_path is not None
        try:
            value = json.loads(self.request_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if value.get("id") == request_id:
            self.request_path.unlink(missing_ok=True)

    def call(self, operation: str, arguments: dict[str, Any] | None = None, *,
             timeout_s: float = 10.0) -> Any:
        if not isinstance(operation, str) or not operation.strip():
            raise ValidationError("operation must be a non-empty string")
        if timeout_s <= 0:
            raise ValidationError("timeout_s must be positive")
        if arguments is not None and not isinstance(arguments, dict):
            raise ValidationError("arguments must be a JSON object")

        self._prepare()
        assert self.responses is not None and self.request_path is not None
        assert self.runtime_dir is not None
        request_id = uuid.uuid4().hex
        response_path = self.responses / f"{request_id}.json"
        owner = f"{os.getpid()}:{request_id}"
        started = time.monotonic()
        deadline = started + timeout_s
        self._acquire_lock(deadline, owner)
        temp_path = self.runtime_dir / f"request.{request_id}.tmp"
        payload = {
            "protocol": 1,
            "id": request_id,
            "operation": operation,
            "arguments": arguments or {},
            "deadline_epoch": time.time() + max(0.0, deadline - time.monotonic()),
        }
        try:
            try:
                encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                                     allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValidationError("Request arguments are not valid JSON",
                                      details={"reason": str(exc)}) from exc
            temp_path.write_text(encoded, encoding="utf-8")
            os.replace(temp_path, self.request_path)

            while time.monotonic() < deadline:
                if response_path.is_file():
                    return self._read_response(response_path, request_id)
                time.sleep(self.poll_s)

            # Resolve a response that appeared at the deadline before returning timeout.
            if response_path.is_file():
                return self._read_response(response_path, request_id)
            self._remove_pending_request(request_id)
            raise BridgeTimeout(
                "REAPER did not answer before the request expired",
                details={"operation": operation, "timeout_s": timeout_s,
                         "request_id": request_id},
            )
        finally:
            temp_path.unlink(missing_ok=True)
            self._release_lock(owner)

    @staticmethod
    def _read_response(response_path: Path, request_id: str) -> Any:
        # REAPER writes the JSON in place. On Windows a concurrent read can
        # hit a sharing/permission error or a half-written file, so retry
        # briefly instead of deleting a valid response and reporting failure.
        last_error: Exception | None = None
        response: dict | None = None
        for _ in range(200):
            try:
                raw = response_path.read_bytes()
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    # The resident ReaScript writes JSON in the local code page;
                    # non-ASCII track/plugin names corrupt it for a UTF-8 reader.
                    text = raw.decode("cp1251", errors="replace")
                response = json.loads(text)
                break
            except (PermissionError, FileNotFoundError, json.JSONDecodeError,
                    OSError) as exc:
                last_error = exc
                time.sleep(0.01)
        if response is None:
            raise BridgeUnavailable("REAPER returned an unreadable bridge response",
                                    details={"request_id": request_id,
                                             "reason": str(last_error)})
        response_path.unlink(missing_ok=True)

        if response.get("id") != request_id:
            raise BridgeUnavailable("Bridge returned a mismatched response ID",
                                    details={"expected": request_id,
                                             "received": response.get("id")})
        if not response.get("ok", False):
            code = str(response.get("error", "bridge_error"))
            error_type = _REMOTE_ERRORS.get(code, ReaperAudioError)
            raise error_type(response.get("message", "REAPER operation failed"),
                             details=response.get("details", {}))
        return response.get("result")

    def health(self, *, timeout_s: float = 1.0) -> dict:
        try:
            result = self.call("health", timeout_s=timeout_s)
        except BridgeTimeout as exc:
            raise BridgeUnavailable(
                "No resident ReaScript is answering. Start the library bridge in REAPER.",
                details=exc.details,
            ) from exc
        if not isinstance(result, dict):
            raise BridgeUnavailable("REAPER returned an invalid health response",
                                    details={"result_type": type(result).__name__})
        return result

    def list_projects(self, *, timeout_s: float = 5.0) -> dict:
        """List open project tabs for this REAPER bridge instance."""
        result = self.call("projects.list", timeout_s=timeout_s)
        if (not isinstance(result, dict) or not isinstance(result.get("projects"), list)
                or not isinstance(result.get("count"), int)):
            raise BridgeUnavailable("REAPER returned an invalid project list")
        return result

    def project_context(self, project_id: str | None = None, *, include_items: bool = False,
                        include_fx_params: bool = False, include_midi_notes: bool = False,
                        timeout_s: float = 10.0) -> dict:
        """Read one explicitly identified open project, or the active project."""
        if project_id is not None and (not isinstance(project_id, str) or not project_id):
            raise ValidationError("project_id must be a non-empty string when provided")
        if not isinstance(include_items, bool) or not isinstance(include_fx_params, bool) or \
                not isinstance(include_midi_notes, bool):
            raise ValidationError("Project context options must be booleans")
        result = self.call("project.context", {
            "project_id": project_id,
            "include_items": include_items or include_midi_notes,
            "include_fx_params": include_fx_params,
            "include_midi_notes": include_midi_notes,
        }, timeout_s=timeout_s)
        if not isinstance(result, dict) or not isinstance(result.get("revision"), dict):
            raise BridgeUnavailable("REAPER returned an invalid project context")
        return result

    def capture_render(self, *, expected_revision: dict[str, Any],
                       output_dir: str | os.PathLike[str], job_id: str,
                       expected_track_count: int,
                       timeout_s: float = 600.0,
                       section_start_s: float | None = None,
                       section_end_s: float | None = None,
                       master_only: bool = False) -> dict:
        """Render processed track stems plus the master through REAPER itself.

        The output directory must already exist and be owned by the caller's
        task workspace. This method never adds renders to the project. It
        verifies the returned mapping and direct-child paths before handing the
        files back to the caller for manifest registration and analysis.
        """
        if not isinstance(expected_revision, dict):
            raise ValidationError("expected_revision must be a project revision object")
        revision = {
            "project_id": expected_revision.get("project_id"),
            "change_count": expected_revision.get("change_count"),
            "path": expected_revision.get("path"),
        }
        if (not isinstance(revision["project_id"], str) or not revision["project_id"] or
                isinstance(revision["change_count"], bool) or
                not isinstance(revision["change_count"], int) or
                revision["change_count"] < 0 or
                not isinstance(revision["path"], str)):
            raise ValidationError("expected_revision is missing valid project identity fields")
        if (isinstance(expected_track_count, bool) or
                not isinstance(expected_track_count, int) or expected_track_count < 0):
            raise ValidationError("expected_track_count must be a non-negative integer")
        if (not isinstance(job_id, str) or
                re.fullmatch(r"[A-Za-z0-9_-]{1,64}", job_id) is None):
            raise ValidationError("job_id must contain 1 to 64 safe filename characters")
        directory = Path(output_dir).expanduser()
        if not directory.is_absolute():
            raise ValidationError("output_dir must be an absolute directory path")
        try:
            directory_real = directory.resolve(strict=True)
        except OSError as exc:
            raise ValidationError("output_dir must already exist",
                                  details={"path": str(directory),
                                           "reason": str(exc)}) from exc
        if not directory_real.is_dir():
            raise ValidationError("output_dir must be a directory",
                                  details={"path": str(directory_real)})

        result = self.call("project.capture_render", {
            "project_id": revision["project_id"],
            "expected_revision": revision,
            "output_dir": str(directory_real),
            "job_id": job_id,
            "section_start_s": section_start_s,
            "section_end_s": section_end_s,
            "master_only": master_only,
        }, timeout_s=timeout_s)
        returned_before = result.get("revision") if isinstance(result, dict) else None
        returned_after = result.get("revision_after") if isinstance(result, dict) else None
        revision_matches = (
            isinstance(returned_before, dict) and
            all(returned_before.get(key) == value for key, value in revision.items())
        )
        after_matches_project = (
            isinstance(returned_after, dict) and
            returned_after.get("project_id") == revision["project_id"] and
            returned_after.get("path") == revision["path"] and
            isinstance(returned_after.get("change_count"), int) and
            not isinstance(returned_after.get("change_count"), bool) and
            returned_after["change_count"] >= revision["change_count"]
        )
        expected_point = ("native_render_master_only" if master_only
                          else "native_render_track_stems_plus_master")
        if (not isinstance(result, dict) or
                result.get("project_id") != revision["project_id"] or
                not revision_matches or not after_matches_project or
                result.get("capture_point") != expected_point or
                not isinstance(result.get("files"), list)):
            raise BridgeUnavailable("REAPER returned an invalid analysis render response")

        files = result["files"]
        expected_files = 1 if master_only else expected_track_count + 1
        if len(files) != expected_files:
            raise BridgeUnavailable(
                "REAPER returned an incomplete master render set" if master_only
                else "REAPER returned an incomplete track and master render set",
                details={"expected_files": expected_files,
                         "received_files": len(files)})
        seen_guids: set[str] = set()
        seen_indices: set[int] = set()
        masters = 0
        for item in files:
            if not isinstance(item, dict):
                raise BridgeUnavailable("REAPER returned an invalid rendered file entry")
            guid, name = item.get("source_guid"), item.get("name")
            index, is_master = item.get("track_index"), item.get("is_master")
            bytes_count, target = item.get("bytes"), item.get("path")
            if (not isinstance(guid, str) or not guid or guid in seen_guids or
                    not isinstance(name, str) or not name or
                    not isinstance(is_master, bool) or
                    isinstance(index, bool) or not isinstance(index, int) or
                    isinstance(bytes_count, bool) or not isinstance(bytes_count, int) or
                    bytes_count <= 44 or not isinstance(target, str)):
                raise BridgeUnavailable("REAPER returned invalid rendered file metadata")
            seen_guids.add(guid)
            target_path = Path(target)
            try:
                target_real = target_path.resolve(strict=True)
                actual_bytes = target_real.stat().st_size
            except OSError as exc:
                raise BridgeUnavailable("A REAPER render file is missing",
                                        details={"path": target,
                                                 "reason": str(exc)}) from exc
            if (target_real.parent != directory_real or
                    target_real.suffix.lower() != ".wav" or
                    actual_bytes != bytes_count):
                raise BridgeUnavailable("A REAPER render escaped its workspace or changed",
                                        details={"path": target,
                                                 "output_dir": str(directory_real),
                                                 "reported_bytes": bytes_count,
                                                 "actual_bytes": actual_bytes})
            if is_master:
                masters += 1
                if index != -1:
                    raise BridgeUnavailable("REAPER returned an invalid master track index")
            else:
                if index < 0 or index >= expected_track_count or index in seen_indices:
                    raise BridgeUnavailable("REAPER returned an invalid or duplicate track index",
                                            details={"track_index": index})
                seen_indices.add(index)
        required_tracks = 0 if master_only else expected_track_count
        if masters != 1 or len(seen_indices) != required_tracks:
            raise BridgeUnavailable("REAPER did not return each track and one master exactly once",
                                    details={"master_count": masters,
                                             "track_indices": sorted(seen_indices)})
        return result

    def sample_tracks(self, *, project_id: str,
                      output_dir: str | os.PathLike[str], job_id: str,
                      start_s: float, end_s: float,
                      sample_rate: int = 48_000,
                      timeout_s: float = 300.0) -> dict:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        directory_real = directory.resolve(strict=True)
        result = self.call("project.sample_tracks", {
            "project_id": project_id,
            "output_dir": str(directory_real),
            "job_id": job_id,
            "start_s": start_s,
            "end_s": end_s,
            "sample_rate": sample_rate,
        }, timeout_s=timeout_s)
        for entry in result.get("files", []):
            path = Path(entry["path"]).resolve(strict=True)
            if path.parent != directory_real:
                raise ValidationError(
                    "Bridge returned an audio file outside the requested directory",
                    details={"path": str(path)})
            entry["path"] = str(path)
        return result

    def list_installed_plugins(self, *, timeout_s: float = 60.0) -> dict:
        return self.call("fx.list_installed", {}, timeout_s=timeout_s)

    def list_fx_parameters(self, *, track_guid: str, fx_guid: str,
                           timeout_s: float = 30.0) -> dict:
        return self.call("fx.list_params", {
            "track_guid": track_guid,
            "fx_guid": fx_guid,
        }, timeout_s=timeout_s)

    def save_project(self, mode: str, *, project_id: str | None = None,
                     path: str | os.PathLike[str] | None = None,
                     overwrite_existing: bool = False,
                     timeout_s: float = 30.0) -> dict:
        """Save a project using REAPER's native save APIs.

        ``current`` writes the project's current file. ``save_as`` requires an
        explicit absolute .RPP path and refuses an existing target by default.
        """
        if mode not in {"current", "save_as"}:
            raise ValidationError("mode must be 'current' or 'save_as'")
        if project_id is not None and (not isinstance(project_id, str) or not project_id):
            raise ValidationError("project_id must be a non-empty string when provided")
        arguments: dict[str, Any] = {
            "mode": mode,
            "project_id": project_id,
        }
        if mode == "save_as":
            if path is None:
                raise ValidationError("save_as requires an explicit .RPP path")
            target = Path(path).expanduser()
            if not target.is_absolute():
                raise ValidationError("save_as path must be absolute",
                                      details={"path": str(target)})
            if target.suffix.lower() != ".rpp":
                raise ValidationError("save_as path must end in .RPP",
                                      details={"path": str(target)})
            target = target.resolve(strict=False)
            arguments["path"] = str(target)
            arguments["overwrite_existing"] = bool(overwrite_existing)
        elif path is not None or overwrite_existing:
            raise ValidationError("path and overwrite_existing are only valid for save_as")

        result = self.call("project.save", arguments, timeout_s=timeout_s)
        if not isinstance(result, dict) or result.get("saved") is not True:
            raise BridgeUnavailable("REAPER did not confirm the project save",
                                    details={"result": result})
        return result

    def call_api(self, name: str, *values: Any, timeout_s: float = 10.0) -> ApiCallResult:
        """Call a documented ReaScript function with typed, project-bound handles.

        The official signature catalog validates host-object arguments before
        they cross the local bridge. ``reaper.array`` arguments use ordinary
        Python sequences and are returned in ``ApiCallResult.arrays`` after the
        host call mutates them.
        """
        signature = get_api_catalog().get(name)
        if len(values) != len(signature.parameters):
            raise ValidationError("Wrong number of arguments for REAPER API function",
                                  details={"name": name,
                                           "expected": len(signature.parameters),
                                           "received": len(values),
                                           "signature": signature.raw_lua_signature})

        encoded_values: list[Any] = []
        contexts: set[tuple[str, int]] = set()
        for parameter, value in zip(signature.parameters, values):
            expected = parameter.value_type
            if value is None:
                if expected.optional:
                    encoded_values.append(None)
                    continue
                raise ValidationError("Required REAPER API argument cannot be null",
                                      details={"name": name,
                                               "parameter": parameter.name,
                                               "type": expected.name})
            if expected.is_host_handle:
                if not isinstance(value, ReaperHandle):
                    raise ValidationError("REAPER API argument requires a typed host handle",
                                          details={"name": name,
                                                   "parameter": parameter.name,
                                                   "expected_type": expected.name})
                if value.kind != expected.name:
                    raise ValidationError("REAPER API handle type does not match its signature",
                                          details={"name": name,
                                                   "parameter": parameter.name,
                                                   "expected_type": expected.name,
                                                   "received_type": value.kind})
                contexts.add((value.project_id, value.change_count))
                encoded_values.append(value.to_wire())
                continue
            if isinstance(value, ReaperHandle):
                raise ValidationError("A REAPER host handle was passed to a scalar parameter",
                                      details={"name": name,
                                               "parameter": parameter.name,
                                               "expected_type": expected.name,
                                               "received_type": value.kind})
            if expected.name == "reaper.array":
                if not isinstance(value, (list, tuple)):
                    raise ValidationError("reaper.array parameters require a numeric sequence",
                                          details={"name": name,
                                                   "parameter": parameter.name})
                if any(isinstance(item, bool) or not isinstance(item, (int, float))
                       for item in value):
                    raise ValidationError("reaper.array values must be numeric")
                encoded_values.append({"$array": list(value)})
                continue
            if isinstance(value, (list, tuple, dict)):
                raise ValidationError("Only reaper.array parameters accept sequences or objects",
                                      details={"name": name,
                                               "parameter": parameter.name,
                                               "type": expected.name})
            if expected.name == "integer" and (
                isinstance(value, bool) or not isinstance(value, int)
            ):
                raise ValidationError("REAPER API integer argument requires an integer",
                                      details={"name": name, "parameter": parameter.name})
            if expected.name == "number" and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            ):
                raise ValidationError("REAPER API number argument requires a number",
                                      details={"name": name, "parameter": parameter.name})
            if expected.name == "boolean" and not isinstance(value, bool):
                raise ValidationError("REAPER API boolean argument requires a boolean",
                                      details={"name": name, "parameter": parameter.name})
            if expected.name == "string" and not isinstance(value, str):
                raise ValidationError("REAPER API string argument requires a string",
                                      details={"name": name, "parameter": parameter.name})
            encoded_values.append(value)

        if len(contexts) > 1:
            raise ValidationError("One REAPER API call cannot mix project revisions",
                                  details={"contexts": sorted(contexts)})
        argument_types = [
            {"type": parameter.value_type.name,
             "host_handle": parameter.value_type.is_host_handle,
             "optional": parameter.value_type.optional}
            for parameter in signature.parameters
        ]
        return_types = [
            {"type": value_type.name,
             "host_handle": value_type.is_host_handle,
             "optional": value_type.optional}
            for value_type in signature.returns
        ]
        result = self.call("api.call", {
            "name": name,
            "args": encoded_values,
            "parameter_types": argument_types,
            "return_types": return_types,
            "catalog_version": get_api_catalog().documentation_version,
        }, timeout_s=timeout_s)
        if not isinstance(result, dict) or not isinstance(result.get("returns"), list):
            raise BridgeUnavailable("REAPER returned an invalid generic API result",
                                    details={"name": name})

        def decode(item: Any) -> Any:
            if isinstance(item, dict) and "$reaper_handle" in item:
                return ReaperHandle.from_wire(item)
            if isinstance(item, list):
                return tuple(decode(value) for value in item)
            if isinstance(item, dict):
                return {key: decode(value) for key, value in item.items()}
            return item

        arrays = {int(index): tuple(float(value) for value in values)
                  for index, values in result.get("arrays", {}).items()}
        return ApiCallResult(name=name,
                             returns=tuple(decode(item) for item in result["returns"]),
                             arrays=arrays,
                             project_before=result.get("project_before"),
                             project_after=result.get("project_after"))
