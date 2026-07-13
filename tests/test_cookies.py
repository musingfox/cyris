"""Tests for cookie loading utilities."""

import sqlite3
from unittest.mock import MagicMock, patch

from cyris.adapters.cookies import load_browser_cookies


class TestCookieLoader:
    def test_empty_domains(self):
        assert load_browser_cookies("chrome", []) == {}

    @patch("cyris.adapters.cookies.browser_cookie3", create=True)
    def test_loads_matching_cookies(self, mock_bc3):
        cookie = MagicMock()
        cookie.domain = ".example.com"
        cookie.name = "session_id"
        cookie.value = "abc123"
        mock_bc3.chrome.return_value = [cookie]

        with patch.dict("sys.modules", {"browser_cookie3": mock_bc3}):
            result = load_browser_cookies("chrome", ["example.com"])
        assert result == {"session_id": "abc123"}

    def test_invalid_browser(self):
        result = load_browser_cookies("invalid", ["example.com"])
        assert result == {}


class TestZenBrowserSupport:
    def test_zen_browser_loads_cookies(self, tmp_path):
        """Test loading cookies from mocked Zen Browser profile."""
        # Create the full expected path structure
        zen_base = tmp_path / "Library" / "Application Support" / "zen" / "Profiles"
        profile_dir = zen_base / "abc123.default"
        profile_dir.mkdir(parents=True)

        # Create mock cookies database
        cookies_db = profile_dir / "cookies.sqlite"
        conn = sqlite3.connect(str(cookies_db))
        cursor = conn.cursor()

        # Create Firefox-compatible moz_cookies table
        cursor.execute("""
            CREATE TABLE moz_cookies (
                host TEXT,
                name TEXT,
                value TEXT
            )
        """)
        cursor.execute(
            "INSERT INTO moz_cookies (host, name, value) VALUES (?, ?, ?)",
            (".example.com", "session_id", "xyz789"),
        )
        cursor.execute(
            "INSERT INTO moz_cookies (host, name, value) VALUES (?, ?, ?)",
            (".other.com", "other_cookie", "ignored"),
        )
        conn.commit()
        conn.close()

        # Patch Path.home() to return our tmp_path
        with patch("cyris.adapters.cookies.Path.home") as mock_home:
            mock_home.return_value = tmp_path
            result = load_browser_cookies("zen", ["example.com"])

        assert result == {"session_id": "xyz789"}

    def test_zen_browser_no_profile(self, tmp_path):
        """Test handling of missing Zen Browser profile."""
        with patch("cyris.adapters.cookies.Path.home") as mock_home:
            mock_home.return_value = tmp_path
            result = load_browser_cookies("zen", ["example.com"])

        assert result == {}

    def test_zen_browser_no_cookies_db(self, tmp_path):
        """Test handling of Zen profile without cookies.sqlite."""
        zen_profiles = tmp_path / "Library" / "Application Support" / "zen" / "Profiles"
        profile_dir = zen_profiles / "abc123.default"
        profile_dir.mkdir(parents=True)
        # No cookies.sqlite created

        with patch("cyris.adapters.cookies.Path.home") as mock_home:
            mock_home.return_value = tmp_path
            result = load_browser_cookies("zen", ["example.com"])

        assert result == {}
