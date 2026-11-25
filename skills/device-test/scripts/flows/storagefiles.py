"""Read app-container file metadata on physical Android and iOS without copying file contents."""

import json
import shlex
import tempfile
from pathlib import Path

from devtest_common import Blocked, run, short


def _android_files(x, root, *, optional=False, private=False):
    path = shlex.quote(root)
    command = "find %s ! -type d ! -type l -exec stat -c '%%s %%n' {} +" % path
    if optional:
        command = "if [ -e %s ]; then %s; fi" % (path, command)
    # Scoped storage denies run-as access to the external app folder; the adb shell reads that one directly.
    if private:
        command = "run-as %s sh -c %s" % (x.dev.app, shlex.quote(command))
    result = x.dev.adb("shell", "-T", command, binary=True)
    if result.returncode or result.stderr:
        raise Blocked("Android inventory failed for %s: %s" % (root, short(result.stderr.decode(errors="replace"))))
    result_files = {}
    for line in result.stdout.decode(errors="surrogateescape").split("\n"):
        if not line:
            continue
        size, separator, name = line.partition(" ")
        if not separator or not size.isdecimal() or not name.startswith(root + "/"):
            raise Blocked("unrecognized Android inventory row for " + root)
        result_files[name[len(root) + 1:]] = int(size)
    return result_files


def _ios_files(x, folder, recurse):
    with tempfile.TemporaryDirectory(prefix="devtest-file-metadata-") as directory:
        output = Path(directory) / "files.json"
        command = ["xcrun", "devicectl", "device", "info", "files", "--device", x.dev.udid,
                   "--domain-type", "appDataContainer", "--domain-identifier", x.dev.app,
                   "--recurse" if recurse else "--no-recurse", "--quiet", "--timeout", "45",
                   "--json-output", str(output)]
        if folder:
            command.extend(["--subdirectory", folder])
        result = run(command, timeout=55, text=True)
        if result.returncode or not output.exists():
            raise Blocked("iOS container inventory failed for %s: %s" % (folder or ".", short(result.stderr)))
        try:
            entries = json.loads(output.read_text())["result"]["files"]
            files = {entry["relativePath"]: entry["metadata"]["size"] for entry in entries
                     if not entry["resources"]["isDirectory"] and not entry["resources"]["isSymbolicLink"]}
        except (KeyError, TypeError, ValueError) as exc:
            raise Blocked("unrecognized devicectl file metadata") from exc
        if any(type(size) is not int or size < 0 for size in files.values()):
            raise Blocked("invalid size in devicectl file metadata")
        return files


def media_folder(x, folder: str) -> dict:
    """Return app media file names and exact logical bytes under one folder, Android/iOS."""
    root = x.dev.media_path(folder)
    if x.platform == "android":
        return _android_files(x, root, optional=True)
    try:
        files = _ios_files(x, root, recurse=False)
    except Blocked:
        # A removed directory also means its seeded files are absent.
        files = {name: size for name, size in _ios_files(x, "", recurse=True).items()
                 if str(Path(name).parent) == root}
    return {Path(name).name: size for name, size in files.items()}


def inventory(x) -> dict:
    """Return every app file as {root: {relative_path: bytes}}, skipping symbolic links."""
    if x.platform == "android":
        external = "/storage/emulated/0/Android/data/" + x.dev.app
        return {"internal": _android_files(x, ".", private=True), "external": _android_files(x, external)}
    return {"container": _ios_files(x, "", recurse=True)}
