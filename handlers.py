"""
YadaCoin Open Source License (YOSL) v1.1

Copyright (c) 2017-2025 Matthew Vogel, Reynold Vogel, Inc.

This software is licensed under YOSL v1.1 – for personal and research use only.
NO commercial use, NO blockchain forks, and NO branding use without permission.

For commercial license inquiries, contact: info@yadacoin.io

Full license terms: see LICENSE.txt in this repository.
"""

import asyncio
import hmac
import inspect
import json
import os
import shutil
import sys
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tornado.ioloop import IOLoop

from node_dashboard.job import ReloadJob, job_state_path, read_job_state
from node_dashboard.restore import connect_client, restore_tool
from node_dashboard.settings import (
    BOOTSTRAP_URL,
    CONFIRM_PHRASE,
    redact_text,
    resolve_target,
    target_from_config,
    tcp_open,
)
from plugins.operator.plugins import (
    ESSENTIAL,
    PluginInstallError,
    catalog,
    install_from_url,
    list_installed,
    release_status,
    update_installed,
)
from plugins.operator.setup import (
    EDITABLE_CONFIG,
    apply_setup_gate,
    available_plugins,
    onboarding_pending,
    publish_second_factor_file,
    read_onboarding,
    read_second_factor,
    write_config_values,
    write_onboarding,
    write_second_factor,
)
from plugins.yadapasswordsignin.handlers import redirect_to_signin
from yadacoin.core.config import Config, SSLConfig
from yadacoin.enums.modes import MODES
from yadacoin.http.base import BaseHandler

SIGNIN_PURPOSE = "operator"

STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "static"))
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ASSETS = {
    "index.html": "text/html; charset=utf-8",
    "config.html": "text/html; charset=utf-8",
    "plugins.html": "text/html; charset=utf-8",
    "setup.html": "text/html; charset=utf-8",
    "workflow.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}


class SavedJob(object):
    def __init__(self, payload):
        self.status = payload.get("status")
        self._payload = payload

    def public(self):
        return self._payload


def client_job(payload):
    if not payload:
        return None
    result = dict(payload.get("result") or {})
    handoff = result.get("handoff")
    if isinstance(handoff, dict):
        handoff = dict(handoff)
        target = handoff.get("target")
        if isinstance(target, dict):
            target = dict(target)
            target.pop("password", None)
            handoff["target"] = target
        result["handoff"] = handoff
        payload = dict(payload)
        payload["result"] = result
    return payload


class OperatorState(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.job = None
        saved = read_job_state(job_state_path(REPO_ROOT))
        if saved and saved.get("status") in ("failed", "succeeded"):
            self.job = SavedJob(saved)

    def start(
        self, target, on_stop, node_host, node_port, on_quiesce=None, on_resume=None
    ):
        with self.lock:
            if self.job is not None and self.job.status == "running":
                return None
            job = ReloadJob(
                REPO_ROOT,
                "",
                lambda event: None,
                target=target,
                on_stop=on_stop,
                on_quiesce=on_quiesce,
                on_resume=on_resume,
                node_host=node_host,
                node_port=node_port,
            )
            self.job = job
        threading.Thread(target=job.run, name="bootstrap-reload", daemon=True).start()
        return job


STATE = OperatorState()


def live_target():
    config = Config()
    return resolve_target(
        target_from_config(
            {
                "mongodb_host": config.mongodb_host,
                "database": config.database,
                "mongodb_username": getattr(config, "mongodb_username", "") or "",
                "mongodb_password": getattr(config, "mongodb_password", "") or "",
            }
        )
    )


def status_payload():
    config = Config()
    payload = {
        "bootstrap_url": BOOTSTRAP_URL,
        "started_at": config.start_time,
        "tools": {
            "mongorestore": shutil.which("mongorestore"),
            "docker": shutil.which("docker"),
            "mode": restore_tool(),
        },
        "mongo": {"ok": False, "error": None},
    }
    try:
        target = live_target()
    except Exception as exc:
        payload["mongo"]["error"] = str(exc)
        return payload
    payload["mongo"].update(target.public())
    if not tcp_open(target.connect_host, target.port, timeout=1.5):
        payload["mongo"]["ok"] = False
        payload["mongo"]["error"] = (
            "MongoDB is not accepting connections at {}:{}"
        ).format(target.connect_host, target.port)
        return payload
    client = None
    try:
        client = connect_client(target, timeout_ms=2000)
        db = client[target.database]
        latest = db.blocks.find_one(sort=[("index", -1)], projection={"index": 1})
        payload["mongo"].update(
            {
                "ok": True,
                "blocks": db.blocks.estimated_document_count(),
                "consensus": db.consensus.estimated_document_count(),
                "height": None if not latest else latest.get("index"),
                "error": None,
            }
        )
    except Exception as exc:
        payload["mongo"]["ok"] = False
        payload["mongo"]["error"] = redact_text(str(exc), target.secrets())
    finally:
        if client is not None:
            client.close()
    return payload


def restart_process():
    os.execv(sys.executable, [sys.executable] + sys.argv)


def schedule_restart(loop=None):
    loop = loop or IOLoop.current()

    def later():
        loop.call_later(1.5, restart_process)

    loop.add_callback(later)


def call_on_loop(loop, fn):
    done = threading.Event()
    box = {}

    def finish(fut):
        try:
            box["result"] = fut.result()
        except Exception as exc:
            box["error"] = exc
        done.set()

    def run():
        try:
            result = fn()
            if inspect.iscoroutine(result):
                asyncio.ensure_future(result).add_done_callback(finish)
                return
            box["result"] = result
        except Exception as exc:
            box["error"] = exc
        done.set()

    loop.add_callback(run)
    done.wait()
    if "error" in box:
        raise box["error"]
    return box.get("result")


def maintenance_callbacks(loop, app):
    def quiesce():
        call_on_loop(loop, app.enter_maintenance)

    def resume():
        call_on_loop(loop, app.leave_maintenance)

    return quiesce, resume


def send_asset(handler, name):
    if name not in ASSETS:
        handler.set_status(404)
        handler.finish()
        return
    path = os.path.join(STATIC_DIR, name)
    with open(path, "rb") as handle:
        body = handle.read()
    handler.set_header("Content-Type", ASSETS[name])
    handler.set_header("Cache-Control", "no-cache")
    handler.write(body)


class OperatorHandler(BaseHandler):
    def initialize(self):
        self._redact_access_code()
        super().initialize()

    def _redact_access_code(self):
        uri = self.request.uri or ""
        if "code=" not in uri:
            return
        parts = urlsplit(uri)
        kept = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != "code"
        ]
        self.request.uri = urlunsplit(
            ("", "", parts.path, urlencode(kept), parts.fragment)
        )

    def _presented_access_code(self):
        code = self.get_query_argument("code", "") or ""
        if not code:
            code = self.request.headers.get("X-Operator-Code") or ""
        if not code:
            cookie = self.get_secure_cookie("operator_access")
            if cookie:
                try:
                    code = cookie.decode()
                except (AttributeError, UnicodeDecodeError):
                    code = ""
        return code

    def _access_code_ok(self):
        expected = getattr(self.config, "operator_access_code", "") or ""
        presented = self._presented_access_code()
        if not expected or not presented or len(presented) != len(expected):
            return False
        return hmac.compare_digest(presented, expected)

    def _issue_access_cookie(self):
        expected = getattr(self.config, "operator_access_code", "") or ""
        if not expected:
            return
        cookie_kwargs = {
            "httponly": True,
            "secure": self._operator_cookie_secure(),
        }
        try:
            self.set_secure_cookie(
                "operator_access",
                expected,
                samesite="Lax",
                **cookie_kwargs,
            )
        except TypeError:
            self.set_secure_cookie("operator_access", expected, **cookie_kwargs)

    def _deny_access_code(self):
        self.set_header("Cache-Control", "no-store")
        self.set_header("Referrer-Policy", "no-referrer")
        path = self.request.path or ""
        if path.startswith("/operator/api/"):
            self.set_status(401)
            self.render_as_json({"status": False, "error": "operator access code required"})
            return
        self.set_status(403)
        self.finish("operator access code required")

    def _strip_access_code(self):
        if self.request.method != "GET":
            return False
        if not (self.get_query_argument("code", "") or ""):
            return False
        if (self.request.path or "").startswith("/operator/api/"):
            return False
        self.redirect(self.request.path)
        return True

    async def prepare(self, exceptions=None):
        await super().prepare(exceptions=exceptions)
        if self._finished:
            return
        if self.request.method == "OPTIONS":
            return
        self.set_header("Referrer-Policy", "no-referrer")
        path = self.request.path or ""
        unlocked = await self.wallet_is_unlocked()
        coded = self._access_code_ok()
        open_setup = (
            getattr(self.config, "setup_required", False) or onboarding_pending(self.config)
        )
        if open_setup and not unlocked and not coded:
            self._deny_access_code()
            return
        if coded:
            self._issue_access_cookie()
            if self._strip_access_code():
                return
        if getattr(self.config, "setup_required", False):
            if (
                path.startswith("/operator/setup")
                or path.startswith("/operator/api/setup")
                or path in ("/operator/app.css", "/operator/app.js")
            ):
                return
            self.redirect("/operator/setup")
            return
        if onboarding_pending(self.config):
            allowed = (
                path.startswith("/operator/setup")
                or path.startswith("/operator/api/setup")
                or path in ("/operator/app.css", "/operator/app.js")
            )
            if allowed:
                return
            if path.startswith("/operator/api/"):
                self.set_status(401)
                self.render_as_json({"status": False, "error": "finish setup sign-in"})
                return
            self.redirect("/operator/setup/workflow")
            return
        if unlocked or coded:
            return
        path = self.request.path or ""
        if path.startswith("/operator/api/"):
            self.set_status(401)
            self.render_as_json({"status": False, "error": "not authorized"})
            return
        redirect_to_signin(self, SIGNIN_PURPOSE)


class OperatorPageHandler(OperatorHandler):
    async def get(self):
        send_asset(self, "index.html")


class OperatorAssetHandler(OperatorHandler):
    async def get(self, name):
        send_asset(self, name)


class OperatorHealthHandler(OperatorHandler):
    async def get(self):
        config = Config()
        self.set_header("Cache-Control", "no-store")
        self.write(
            {
                "ok": True,
                "started_at": config.start_time,
                "boot_id": str(config.operator_session_epoch),
            }
        )


class OperatorStatusHandler(OperatorHandler):
    async def get(self):
        payload = await IOLoop.current().run_in_executor(None, status_payload)
        self.set_header("Cache-Control", "no-store")
        self.write(payload)


class OperatorJobHandler(OperatorHandler):
    async def get(self):
        job = STATE.job.public() if STATE.job is not None else None
        self.set_header("Cache-Control", "no-store")
        self.write({"job": client_job(job)})


class OperatorReloadHandler(OperatorHandler):
    async def post(self):
        if "application/json" not in self.request.headers.get("Content-Type", ""):
            self.set_status(415)
            self.write({"error": "Content-Type must be application/json"})
            return
        try:
            payload = json.loads(self.request.body.decode("utf-8") or "{}")
        except ValueError:
            self.set_status(400)
            self.write({"error": "Invalid JSON"})
            return
        if not isinstance(payload, dict) or payload.get("confirm") != CONFIRM_PHRASE:
            self.set_status(400)
            self.write({"error": "Type reload to confirm the reload"})
            return
        try:
            target = await IOLoop.current().run_in_executor(None, live_target)
        except Exception as exc:
            self.set_status(400)
            self.write({"error": str(exc)})
            return
        config = Config()
        loop = IOLoop.current()
        quiesce, resume = maintenance_callbacks(loop, config.application)
        job = STATE.start(
            target,
            None,
            getattr(config, "serve_host", None) or "127.0.0.1",
            getattr(config, "serve_port", None),
            on_quiesce=quiesce,
            on_resume=resume,
        )
        if job is None:
            self.set_status(409)
            self.write({"error": "A reload is already running"})
            return
        self.set_status(202)
        self.write({"job": client_job(job.public())})


class OperatorSetupHandler(OperatorHandler):
    async def get(self):
        if not getattr(self.config, "setup_required", False):
            if onboarding_pending(self.config):
                self.redirect("/operator/setup/workflow")
                return
            self.redirect("/operator")
            return
        send_asset(self, "setup.html")


class OperatorWorkflowHandler(OperatorHandler):
    async def get(self):
        if getattr(self.config, "setup_required", False):
            self.redirect("/operator/setup")
            return
        if not onboarding_pending(self.config):
            self.redirect("/node-dashboard")
            return
        send_asset(self, "workflow.html")


class OperatorSetupApiHandler(OperatorHandler):
    async def get(self):
        self.set_header("Cache-Control", "no-store")
        self.write(
            {
                "setup_required": bool(getattr(self.config, "setup_required", False)),
                "missing": list(getattr(self.config, "setup_missing", []) or []),
                "username": getattr(self.config, "username", "") or "",
            }
        )

    async def post(self):
        if not getattr(self.config, "setup_required", False):
            self.set_status(409)
            self.write({"error": "setup is already complete"})
            return
        try:
            payload = json.loads(self.request.body.decode("utf-8") or "{}")
        except ValueError:
            self.set_status(400)
            self.write({"error": "Invalid JSON"})
            return
        if not isinstance(payload, dict):
            self.set_status(400)
            self.write({"error": "Invalid JSON"})
            return
        missing = set(getattr(self.config, "setup_missing", []) or [])
        values = {}
        if "username" in missing:
            username = (payload.get("username") or "").strip()
            if not username:
                self.set_status(400)
                self.write({"error": "username is required"})
                return
            values["username"] = username
        if "second_factor" in missing:
            second_factor = payload.get("second_factor") or ""
            if not str(second_factor).strip():
                self.set_status(400)
                self.write({"error": "second factor is required"})
                return
            try:
                path = write_second_factor(self.config, second_factor)
                publish_second_factor_file(self.config, path)
            except Exception as exc:
                self.set_status(400)
                self.write({"error": str(exc)})
                return
            os.environ["SECOND_FACTOR"] = str(second_factor).strip()
        if values:
            try:
                write_config_values(self.config, values)
            except Exception as exc:
                self.set_status(400)
                self.write({"error": str(exc)})
                return
            for key, value in values.items():
                setattr(self.config, key, value)
        apply_setup_gate(self.config)
        if not self.config.setup_required:
            write_onboarding(self.config, "announce")
            application = getattr(self.config, "application", None)
            if application is not None and hasattr(application, "rebind_after_setup"):
                await application.rebind_after_setup()
        self.write(
            {
                "ok": True,
                "setup_required": self.config.setup_required,
                "restart_required": False,
                "next": "/operator/setup/workflow"
                if not self.config.setup_required
                else "",
            }
        )


class OperatorIdentityHandler(OperatorHandler):
    async def get(self):
        from yadacoin.core.identityannouncement import IdentityAnnouncement

        username = (getattr(self.config, "username", "") or "").strip()
        found = None
        if username:
            found = await IdentityAnnouncement.get_by_username(
                username, include_mempool=True
            )
        self.set_header("Cache-Control", "no-store")
        self.write(
            {
                "username": username,
                "found": bool(found),
                "source": (found or {}).get("source") or "",
                "step": read_onboarding(self.config),
                "app_inception": bool(found)
                or bool(getattr(self.config, "password_inception_seen", False)),
            }
        )


class OperatorSetupPluginsHandler(OperatorHandler):
    async def get(self):
        installed = set(list_installed())
        self.write(
            {
                "ok": True,
                "plugins": [
                    dict(item, installed=item["name"] in installed)
                    for item in catalog()
                ],
            }
        )

    async def post(self):
        try:
            payload = json.loads(self.request.body.decode("utf-8") or "{}")
        except ValueError:
            self.set_status(400)
            self.write({"error": "Invalid JSON"})
            return
        known = {item["name"]: item["url"] for item in catalog()}
        selected = payload.get("names") if isinstance(payload, dict) else None
        if not isinstance(selected, list) or any(
            not isinstance(name, str) or name not in known for name in selected
        ):
            self.set_status(400)
            self.write({"error": "Choose plugins from the catalog."})
            return
        selected = list(dict.fromkeys(selected))
        for name in known:
            if name in ESSENTIAL and name not in selected:
                selected.append(name)
        disabled = [name for name in known if name not in selected and name not in ESSENTIAL]
        loop = IOLoop.current()
        installed = []
        try:
            for name in selected:
                if name in list_installed():
                    installed.append(name)
                    continue
                await loop.run_in_executor(None, install_from_url, known[name])
                installed.append(name)
            write_config_values(
                self.config, {"disabled_plugins": disabled}, internal=False
            )
            self.config.disabled_plugins = disabled
        except (PluginInstallError, ValueError) as exc:
            self.set_status(400)
            self.write({"error": str(exc), "installed": installed})
            return
        application = getattr(self.config, "application", None)
        if application is not None and hasattr(application, "enter_maintenance"):
            entered = not getattr(self.config, "maintenance", False)
            if entered:
                application.enter_maintenance()
            try:
                application.reload_http_routes()
            finally:
                if entered:
                    await application.leave_maintenance()
        self.write({"ok": True, "installed": installed, "disabled": disabled})


class OperatorSeedHandler(OperatorHandler):
    async def get(self):
        step = read_onboarding(self.config)
        if step not in ("seed", "signin"):
            self.set_status(409)
            self.write({"error": "seed is not available yet"})
            return
        self.set_header("Cache-Control", "no-store")
        self.write({"seed": getattr(self.config, "seed", "") or ""})


class OperatorOnboardingHandler(OperatorHandler):
    async def post(self):
        try:
            payload = json.loads(self.request.body.decode("utf-8") or "{}")
        except ValueError:
            self.set_status(400)
            self.write({"error": "Invalid JSON"})
            return
        step = (payload or {}).get("step") or ""
        order = ["announce", "apps", "plugins", "seed", "signin", "done"]
        current = read_onboarding(self.config) or "announce"
        if step not in order or order.index(step) != order.index(current) + 1:
            self.set_status(409)
            self.write({"error": "cannot skip setup steps", "step": current})
            return
        if step == "seed":
            self.config.password_inception_seen = False
        if step == "signin":
            from yadacoin.core.identityannouncement import IdentityAnnouncement

            username = (getattr(self.config, "username", "") or "").strip()
            found = None
            if username:
                found = await IdentityAnnouncement.get_by_username(
                    username, include_mempool=True
                )
            if not found and not getattr(self.config, "password_inception_seen", False):
                self.set_status(409)
                self.write({"error": "Broadcast inception has not been detected"})
                return
        if step == "done":
            if not await self.wallet_is_unlocked():
                self.set_status(401)
                self.write({"error": "sign in with Yada Password first"})
                return
        write_onboarding(self.config, step)
        self.write({"ok": True, "step": step})


class OperatorPluginsPageHandler(OperatorHandler):
    async def get(self):
        send_asset(self, "plugins.html")


class OperatorPluginsHandler(OperatorHandler):
    async def get(self):
        plugins = await IOLoop.current().run_in_executor(None, release_status)
        self.write({"ok": True, "plugins": plugins})

    async def post(self):
        try:
            body = json.loads(self.request.body.decode() or "{}")
        except (ValueError, UnicodeDecodeError):
            self.set_status(400)
            self.write({"error": "Send a repository URL."})
            return
        if not isinstance(body, dict):
            body = {}
        if body.get("action") == "update":
            await self._reload_plugin(update_installed, body.get("name"), "updated")
            return
        url = body.get("url")
        await self._reload_plugin(install_from_url, url, "installed")

    async def _reload_plugin(self, operation, argument, verb):
        application = getattr(self.config, "application", None)
        if application is None or not hasattr(application, "enter_maintenance"):
            self.set_status(500)
            self.write({"error": "Maintenance mode is not available."})
            return
        loop = IOLoop.current()
        try:
            installed = await loop.run_in_executor(None, operation, argument)
        except PluginInstallError as exc:
            self.set_status(400)
            self.write({"error": str(exc)})
            return
        entered = False
        reloaded = False
        try:
            if not getattr(self.config, "maintenance", False):
                application.enter_maintenance()
                entered = True
            application.reload_http_routes()
            reloaded = True
        except Exception as exc:
            self.set_status(500)
            self.write(
                {
                    "error": "{} was {}, but routes were not reloaded: {}".format(
                        installed["name"], verb, exc
                    )
                }
            )
        if entered:
            try:
                await application.leave_maintenance()
            except Exception as exc:
                self.set_status(500)
                self.write(
                    {
                        "error": "{} was {}, but the node did not leave maintenance: {}".format(
                            installed["name"], verb, exc
                        )
                    }
                )
                return
        if not reloaded:
            return
        plugins = await loop.run_in_executor(None, release_status)
        self.write(
            {
                "ok": True,
                "name": installed["name"],
                "plugins": plugins,
                "message": "{} is {} and its routes are loaded.".format(
                    installed["name"], verb
                ),
            }
        )


class OperatorConfigPageHandler(OperatorHandler):
    async def get(self):
        send_asset(self, "config.html")


def json_safe(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    return str(value)


class OperatorConfigHandler(OperatorHandler):
    async def get(self):
        config = Config()
        values = {}
        for key in EDITABLE_CONFIG:
            if key == "ssl":
                continue
            value = getattr(config, key, None)
            if key == "modes" and not isinstance(value, list):
                value = []
            if key == "disabled_plugins" and not isinstance(value, list):
                value = []
            values[key] = json_safe(value)
        ssl = {}
        try:
            if getattr(config, "ssl", None):
                ssl = json_safe(config.ssl.to_dict())
        except Exception:
            ssl = {}
        try:
            second_factor_set = bool(read_second_factor())
        except OSError:
            second_factor_set = False
        self.set_header("Cache-Control", "no-store")
        self.set_header("Content-Type", "application/json")
        self.write(
            json.dumps(
                {
                    "values": values,
                    "mode_options": [
                        mode.value
                        for mode in MODES
                        if mode.value not in ("dns", "proxy", "ssl")
                    ],
                    "available_plugins": available_plugins(),
                    "ssl": ssl,
                    "second_factor_set": second_factor_set,
                },
                default=str,
            )
        )

    async def post(self):
        try:
            payload = json.loads(self.request.body.decode("utf-8") or "{}")
        except ValueError:
            self.set_status(400)
            self.write({"error": "Invalid JSON"})
            return
        if not isinstance(payload, dict):
            self.set_status(400)
            self.write({"error": "Invalid JSON"})
            return
        values = payload.get("values")
        if not isinstance(values, dict):
            self.set_status(400)
            self.write({"error": "values object required"})
            return
        unknown = [key for key in values if key not in EDITABLE_CONFIG]
        if unknown:
            self.set_status(400)
            self.write({"error": "unsupported keys: {}".format(", ".join(unknown))})
            return
        try:
            saved = write_config_values(self.config, values)
        except Exception as exc:
            self.set_status(400)
            self.write({"error": str(exc)})
            return
        message = "Saved."
        if payload.get("restart"):
            previous_http = (
                getattr(self.config, "serve_host", None),
                getattr(self.config, "serve_port", None),
                getattr(getattr(self.config, "ssl", None), "port", None),
                "ssl" in (getattr(self.config, "modes", None) or []),
            )
            for key, value in saved.items():
                if key == "ssl":
                    self.config.ssl = SSLConfig.from_dict(value)
                    continue
                setattr(self.config, key, value)
            ssl_value = saved.get("ssl") or {}
            if ssl_value.get("common_name"):
                from plugins.operator.letsencrypt import (
                    issue_certificate,
                    start_renew_loop,
                )

                ok, detail = issue_certificate(
                    ssl_value["common_name"], ssl_value.get("email")
                )
                start_renew_loop(self.config)
                if not ok:
                    message = "Saved. Certificate was not issued: {}".format(detail)
            application = getattr(self.config, "application", None)
            if application is None or not hasattr(application, "apply_saved_config"):
                self.set_status(500)
                self.write({"error": "Saved, but maintenance mode is not available."})
                return
            try:
                await application.apply_saved_config(previous_http)
            except Exception as exc:
                self.set_status(500)
                self.write(
                    {
                        "error": "Saved, but the node could not apply the values: {}".format(
                            exc
                        )
                    }
                )
                return
            if message == "Saved.":
                message = "Saved. Services were restarted with these values."
        self.write(
            {"ok": True, "restart_required": False, "values": saved, "message": message}
        )


HANDLERS = [
    (r"/operator/api/status", OperatorStatusHandler),
    (r"/operator/api/job", OperatorJobHandler),
    (r"/operator/api/health", OperatorHealthHandler),
    (r"/operator/api/reload", OperatorReloadHandler),
    (r"/operator/api/setup/identity", OperatorIdentityHandler),
    (r"/operator/api/setup/seed", OperatorSeedHandler),
    (r"/operator/api/setup/onboarding", OperatorOnboardingHandler),
    (r"/operator/api/setup/plugins", OperatorSetupPluginsHandler),
    (r"/operator/api/setup", OperatorSetupApiHandler),
    (r"/operator/api/config", OperatorConfigHandler),
    (r"/operator/api/plugins", OperatorPluginsHandler),
    (r"/operator/(app\.(?:css|js))", OperatorAssetHandler),
    (r"/operator/setup/workflow/?", OperatorWorkflowHandler),
    (r"/operator/setup/?", OperatorSetupHandler),
    (r"/operator/config/?", OperatorConfigPageHandler),
    (r"/operator/plugins/?", OperatorPluginsPageHandler),
    (r"/operator/?", OperatorPageHandler),
]

OPERATOR_HANDLERS = HANDLERS
