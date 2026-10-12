# -*- coding: utf-8 -*-
"""WinClipboard application identity and release metadata.

Keep version and repository identity in this dependency-free module so the
runtime, updater, installer, diagnostics, and build tooling all consume the
same source of truth.
"""

APP_NAME = "WinClipboard"
APP_VERSION = "1.0.0-rc1"

GITHUB_OWNER = "20212661"
GITHUB_REPOSITORY = "WinClipboard"
GITHUB_SLUG = "%s/%s" % (GITHUB_OWNER, GITHUB_REPOSITORY)

REPO_URL = "https://github.com/%s" % GITHUB_SLUG
RELEASE_API_HOST = "api.github.com"
RELEASE_API_PATH = "/repos/%s/releases/latest" % GITHUB_SLUG
RELEASE_API = "https://%s%s" % (RELEASE_API_HOST, RELEASE_API_PATH)
