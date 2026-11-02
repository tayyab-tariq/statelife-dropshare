class MacAutostart:
    """Development-grade: the Mac build is never installed, so there is nothing to start."""

    def available(self) -> bool:
        return False

    def is_enabled(self) -> bool:
        return False

    def set_enabled(self, enabled: bool) -> None:
        return None
