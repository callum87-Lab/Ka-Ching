"""Ka-Ching! web app entry point (uvicorn app.main:app).

The code is split by area:
  core.py            the app, templates, middleware, and all shared helpers
  pages.py           Dashboard, Orders, Calendar, Search, Insights, Alerts
  items.py           logging, importing and editing items
  settings_pages.py  the Settings page and everything it saves
  auth.py            the optional login
  api.py             phone sync API, summary JSON, calendar feed, service worker
  welcome.py         the first-run setup guide
"""
from .core import *  # noqa: F401,F403 - keeps app.main.<name> working
from .core import app  # noqa: F401
from . import pages, items, settings_pages, auth, api, welcome  # noqa: F401,E402 - registers the routes
