"""Sign-in routes for the optional login."""
from .core import *  # noqa: F401,F403 - the shared app, templates and helpers


def _password_ok(password):
    env = _env_password()
    if env:
        return hmac.compare_digest(password.encode("utf-8"), env.encode("utf-8"))
    stored, _, _ = _login_settings()
    return bool(stored) and _check_password_hash(password, stored)


def _client_ip(request):
    return request.client.host if request.client else "?"


def _locked_out(ip):
    now = datetime.now().timestamp()
    recent = [t for t in _login_fails.get(ip, []) if now - t < LOGIN_LOCK_SECONDS]
    _login_fails[ip] = recent
    return len(recent) >= LOGIN_MAX_FAILS


def _safe_next(value, default="/"):
    target = safe_redirect(value, default)
    return default if target.startswith("/login") else target


@app.get("/login")
def login_page(request: Request, next: str = "/", error: str | None = None):
    if not login_enabled():
        return RedirectResponse(url=_safe_next(next), status_code=303)
    locked = _locked_out(_client_ip(request))
    return render("login.html", {"request": request, "next": _safe_next(next), "error": error, "locked": locked})


@app.post("/login")
def login_submit(request: Request, password: str = Form(""), remember: str = Form(""), next: str = Form("/")):
    ip = _client_ip(request)
    target = _safe_next(next)
    if _locked_out(ip):
        return RedirectResponse(url="/login?next=" + quote(target, safe="") + "&error=locked", status_code=303)
    if not _password_ok(password):
        _login_fails.setdefault(ip, []).append(datetime.now().timestamp())
        logger.warning("LOGIN: failed attempt from %s", ip)
        err = "locked" if _locked_out(ip) else "wrong"
        return RedirectResponse(url="/login?next=" + quote(target, safe="") + "&error=" + err, status_code=303)
    _login_fails.pop(ip, None)
    logger.info("LOGIN: signed in from %s (remember=%s)", ip, bool(remember))
    response = RedirectResponse(url=target, status_code=303)
    _set_session_cookie(response, bool(remember), request)
    return response


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(LOGIN_COOKIE, path="/")
    return response
