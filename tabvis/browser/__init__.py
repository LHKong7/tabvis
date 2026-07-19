"""Browser-agent service package.

Owns a single long-lived Playwright ``launch_persistent_context`` (a persistent Chromium
profile) shared across all ``Browser*`` tool calls. See :mod:`tabvis.browser.manager`
for the singleton getter and :mod:`tabvis.browser.browser_service` for the driver.
"""
