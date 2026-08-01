"""Process-entry behavior."""

from __future__ import annotations

from tabvis import bootstrap_entry
from tabvis.utils import dotenv_loader


def test_bootstrap_treats_keyboard_interrupt_as_clean_shutdown(monkeypatch) -> None:
    monkeypatch.setattr(dotenv_loader, "load_env_files", lambda: None)
    monkeypatch.setattr(bootstrap_entry, "ensure_bootstrap_macro", lambda: None)

    def interrupted(coroutine):
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(bootstrap_entry.asyncio, "run", interrupted)

    bootstrap_entry.main()
