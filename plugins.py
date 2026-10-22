"""Install a YadaCoin plugin from a git repository without importing it."""

import ast
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HTTPS_RE = re.compile(
    r"^https://[A-Za-z0-9._-]+(?::\d+)?(?:/[A-Za-z0-9._~!$&'()*+,;=:@%-]+)+/?$"
)
SSH_RE = re.compile(r"^git@[A-Za-z0-9._-]+:[A-Za-z0-9._/-]+(?:\.git)?$")
PROTECTED = {"operator"}
ESSENTIAL = {
    "operator",
    "app",
    "nodeapi",
    "graphapi",
    "explorerapi",
    "kel",
    "apidocs",
    "product",
    "yadacoinwallet",
    "yadacoinpool",
    "yadapasswordsignin",
    "passwordrotation",
}


class PluginInstallError(Exception):
    pass


def deps_dir():
    path = os.path.join(install_dir(), ".pydeps")
    os.makedirs(path, exist_ok=True)
    if path not in sys.path:
        sys.path.insert(0, path)
    return path


def install_dir():
    env_dir = os.environ.get("YADA_PLUGINS_DIR")
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        return os.path.abspath(env_dir)
    docker_dir = "/plugins"
    if os.path.isdir(docker_dir) or os.path.exists("/.dockerenv"):
        os.makedirs(docker_dir, exist_ok=True)
        return docker_dir
    repo_dir = os.path.join(REPO_ROOT, "plugins")
    os.makedirs(repo_dir, exist_ok=True)
    return repo_dir


def parse_repo_url(raw):
    url = (raw or "").strip()
    if not url or any(ch.isspace() for ch in url):
        raise PluginInstallError("Paste a repository URL.")
    if not (HTTPS_RE.match(url) or SSH_RE.match(url)):
        raise PluginInstallError("Use an https or git@ repository URL.")
    if url.startswith("https://") and not url.endswith(".git"):
        url = url.rstrip("/") + ".git"
    leaf = url.rstrip("/").rsplit("/", 1)[-1]
    if ":" in leaf:
        leaf = leaf.rsplit(":", 1)[-1]
    name = leaf[:-4] if leaf.endswith(".git") else leaf
    name = name.replace("-", "_")
    if not NAME_RE.match(name) or name in PROTECTED:
        raise PluginInstallError("That repository name is not a valid plugin name.")
    return url, name


def plugin_root(checkout):
    if _has_handlers(checkout):
        return checkout
    children = []
    for name in os.listdir(checkout):
        if name.startswith("."):
            continue
        path = os.path.join(checkout, name)
        if os.path.isdir(path) and _has_handlers(path):
            children.append(path)
    if len(children) == 1:
        return children[0]
    raise PluginInstallError(
        "Not a YadaCoin plugin. Expected handlers.py that sets HANDLERS."
    )


def _has_handlers(directory):
    path = os.path.join(directory, "handlers.py")
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
    except (OSError, SyntaxError):
        return False
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "HANDLERS":
                return True
    return False


def catalog():
    path = os.path.join(REPO_ROOT, "plugins.txt")
    entries = []
    if not os.path.isfile(path):
        return entries
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "\t" not in line:
                continue
            name, url = line.split("\t", 1)
            name = name.strip()
            if not name or name in PROTECTED:
                continue
            entries.append(
                {
                    "name": name,
                    "url": url.strip(),
                    "essential": name in ESSENTIAL,
                }
            )
    return entries


def list_installed():
    root = install_dir()
    names = []
    if not os.path.isdir(root):
        return names
    for name in sorted(os.listdir(root)):
        if name.startswith(".") or name.startswith("__"):
            continue
        path = os.path.join(root, name)
        if os.path.isdir(path) and _has_handlers(path):
            names.append(name)
    return names


def _https_url(url):
    prefix = "git@github.com:"
    if url.startswith(prefix):
        return "https://github.com/" + url[len(prefix) :]
    return url


def _install_requirements(directory):
    requirements = os.path.join(directory, "requirements.txt")
    if os.path.isfile(requirements):
        _run(["pip", "install", "-r", requirements, "--target", deps_dir()], timeout=300)


def _remember_source(directory, url):
    with open(os.path.join(directory, ".yada-source"), "w", encoding="utf-8") as handle:
        handle.write(url.strip() + "\n")


def _stored_url(directory):
    path = os.path.join(directory, ".yada-source")
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def _catalog_url(name):
    for entry in catalog():
        if entry["name"] == name:
            return _https_url(entry["url"])
    return ""


def _use_https_remote(directory):
    completed = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=directory,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        return
    url = _https_url((completed.stdout or "").strip())
    if url.startswith("https://"):
        _run(["git", "remote", "set-url", "origin", url], cwd=directory)


def _tag_key(name):
    if not name or not re.match(r"^v?\d+(?:\.\d+)*$", name):
        return None
    text = name[1:] if name.startswith("v") else name
    return tuple(int(part) for part in text.split("."))


def _is_newer(latest, current):
    latest_key = _tag_key(latest)
    if latest_key is None:
        return False
    current_key = _tag_key(current)
    if current_key is None:
        return True
    return latest_key > current_key


def _git_error(detail):
    lines = [line.strip() for line in (detail or "").splitlines() if line.strip()]
    if not lines:
        return "command failed"
    for line in lines:
        if line.lower().startswith(("fatal:", "error:", "remote:")):
            return line[:500]
    return lines[0][:500]


def _latest_tag(url):
    url = _https_url(url)
    try:
        completed = subprocess.run(
            ["git", "ls-remote", "--tags", url],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PluginInstallError(str(exc)) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "could not read tags").strip()
        raise PluginInstallError(_git_error(detail))
    found = {}
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].startswith("refs/tags/"):
            continue
        ref = parts[1][:-3] if parts[1].endswith("^{}") else parts[1]
        name = ref.rsplit("/", 1)[-1]
        key = _tag_key(name)
        if key is not None:
            found[name] = key
    if not found:
        return ""
    return max(found, key=found.get)


def _git_checkout(directory):
    git_meta = os.path.join(directory, ".git")
    return os.path.isdir(git_meta) or os.path.isfile(git_meta)


def _current_tag(directory):
    if _git_checkout(directory):
        try:
            completed = subprocess.run(
                ["git", "describe", "--tags", "--exact-match", "HEAD"],
                cwd=directory,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if completed.returncode == 0:
            return completed.stdout.strip()
        return ""
    path = os.path.join(directory, ".yada-release")
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def _remember_release(directory, tag):
    with open(os.path.join(directory, ".yada-release"), "w", encoding="utf-8") as handle:
        handle.write(tag + "\n")


def _plugin_url(directory, name):
    if _git_checkout(directory):
        try:
            completed = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=directory,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is not None and completed.returncode == 0:
            return _https_url(completed.stdout.strip())
    return _https_url(_stored_url(directory) or _catalog_url(name))


def _checkout_tag(directory, tag):
    _use_https_remote(directory)
    _run(
        ["git", "fetch", "--depth", "1", "origin", "refs/tags/{}:refs/tags/{}".format(tag, tag)],
        cwd=directory,
    )
    _run(["git", "checkout", "--detach", tag], cwd=directory)
    _remember_release(directory, tag)


def release_status():
    rows = []
    root = install_dir()
    for name in list_installed():
        dest = os.path.join(root, name)
        current = _current_tag(dest)
        latest = ""
        error = ""
        url = _plugin_url(dest, name)
        if url:
            try:
                latest = _latest_tag(url)
            except PluginInstallError as exc:
                error = str(exc)
        rows.append(
            {
                "name": name,
                "current": current,
                "latest": latest,
                "update": _is_newer(latest, current),
                "error": error,
            }
        )
    return rows


def update_installed(name):
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise PluginInstallError("That plugin name is not valid.")
    dest = os.path.join(install_dir(), name)
    if not (os.path.isdir(dest) and _has_handlers(dest)):
        raise PluginInstallError("That plugin is not installed.")
    url = _plugin_url(dest, name)
    if not url:
        raise PluginInstallError("No repository URL is recorded for this plugin.")
    tag = _latest_tag(url)
    if not tag:
        raise PluginInstallError("No tagged release.")
    if not _is_newer(tag, _current_tag(dest)):
        raise PluginInstallError("Already on the latest release.")
    if _git_checkout(dest):
        _checkout_tag(dest, tag)
        _install_requirements(dest)
        return {"name": name, "path": dest, "tag": tag}
    if name in PROTECTED:
        raise PluginInstallError("operator can only be updated from its git checkout.")
    installed = install_from_url(url, tag=tag)
    installed["tag"] = tag
    return installed


def _run(args, cwd=None, timeout=180):
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PluginInstallError(str(exc)) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "command failed").strip()
        raise PluginInstallError(_git_error(detail))


def install_from_url(raw, tag=None):
    url, name = parse_repo_url(raw)
    url = _https_url(url)
    parent = tempfile.mkdtemp(prefix="yada-plugin-")
    checkout = os.path.join(parent, "src")
    try:
        if tag is None:
            tag = _latest_tag(url)
        command = ["git", "clone", "--depth", "1"]
        if tag:
            command.extend(["--branch", tag])
        command.extend([url, checkout])
        _run(command)
        source = plugin_root(checkout)
        _install_requirements(source)
        dest_root = install_dir()
        dest = os.path.join(dest_root, name)
        backup = dest + ".bak"
        if os.path.isdir(backup):
            shutil.rmtree(backup)
        if os.path.isdir(dest):
            os.rename(dest, backup)
        try:
            shutil.copytree(source, dest)
        except Exception:
            if os.path.isdir(backup) and not os.path.isdir(dest):
                os.rename(backup, dest)
            raise
        if os.path.isdir(backup):
            shutil.rmtree(backup)
        _remember_source(dest, url)
        if tag:
            _remember_release(dest, tag)
        return {"name": name, "url": url, "path": dest, "tag": tag or ""}
    finally:
        shutil.rmtree(parent, ignore_errors=True)
