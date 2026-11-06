"""Additional coverage for crawler/fetcher.py (currently 68%)"""
import contextlib
import os
import unittest
from unittest.mock import Mock, patch

from nexus_search.crawler.fetcher import (
    Fetcher, FetchResult, FetchBlocked, TooLarge, _render_js_enabled,
    RETRY_STATUS, REDIRECT_STATUS, MAX_REDIRECTS, MAX_BYTES, MAX_BACKOFF
)

try:
    import requests
except ImportError:
    requests = None


class TestFetcherCoverage(unittest.TestCase):
    """Targeted tests for uncovered lines in fetcher.py"""

    def setUp(self):
        self.fetcher = Fetcher()

    def tearDown(self):
        self.fetcher.close()

    # --- Constructor edge cases (lines 78-96) ---

    def test_render_js_disabled_without_env(self):
        """Test render_js is disabled when NEXUS_RENDER_JS not set (lines 78-83)"""
        with patch.dict(os.environ, {"NEXUS_RENDER_JS": ""}, clear=True):
            fetcher = Fetcher(render_js=True)
            self.assertFalse(fetcher.render_js)

    def test_render_js_refused_without_validator(self):
        """Test render_js refused without url_validator (lines 89-96)"""
        with patch.dict(os.environ, {"NEXUS_RENDER_JS": "1"}, clear=True):
            fetcher = Fetcher(render_js=True, url_validator=None)
            self.assertFalse(fetcher.render_js)

    def test_render_js_allowed_with_allow_private(self):
        """Test render_js allowed with render_js_allow_private=True (line 96)"""
        with patch.dict(os.environ, {"NEXUS_RENDER_JS": "1"}, clear=True):
            fetcher = Fetcher(render_js=True, url_validator=None, render_js_allow_private=True)
            self.assertTrue(fetcher.render_js)

    # --- _render_with_browser (lines 100-210) ---

    def test_render_with_browser_not_enabled(self):
        """Test _render_with_browser returns None when not enabled (lines 138-139)"""
        fetcher = Fetcher(render_js=False)
        result = fetcher._render_with_browser("https://example.com")
        self.assertIsNone(result)

    def test_render_with_browser_playwright_not_installed(self):
        """Test _render_with_browser returns None when playwright not installed (lines 141-146)"""
        # Skip if playwright is actually installed (test is for missing playwright)
        try:
            import playwright
            self.skipTest("playwright is installed, cannot test ImportError path")
        except ImportError:
            pass  # Good, playwright not installed
        
        with patch.dict(os.environ, {"NEXUS_RENDER_JS": "1"}, clear=True):
            fetcher = Fetcher(render_js=True, url_validator=lambda x: True)
            with patch('nexus_search.crawler.fetcher._render_js_enabled', return_value=True):
                result = fetcher._render_with_browser("https://example.com")
                self.assertIsNone(result)

    # --- _get redirect handling (lines 214-234) ---

    def test_get_redirect_without_location(self):
        """Test _get raises FetchBlocked for redirect without Location (lines 227-228)"""
        response = Mock()
        response.status_code = 301
        response.headers = {}  # No Location
        response.close = Mock()

        self.fetcher.session.get = Mock(return_value=response)

        with self.assertRaises(FetchBlocked) as cm:
            self.fetcher._get("https://example.com", {})
        self.assertIn("redirect without Location header", str(cm.exception))

    def test_get_redirect_to_disallowed(self):
        """Test _get raises FetchBlocked for redirect to disallowed URL (lines 230-231)"""
        def validator(url):
            return "allowed.com" in url

        fetcher = Fetcher(url_validator=validator)

        response = Mock()
        response.status_code = 302
        response.headers = {"Location": "https://blocked.com/page"}
        response.close = Mock()

        fetcher.session.get = Mock(return_value=response)

        with self.assertRaises(FetchBlocked) as cm:
            fetcher._get("https://allowed.com", {})
        self.assertIn("redirect to disallowed URL", str(cm.exception))

    def test_get_too_many_redirects(self):
        """Test _get raises FetchBlocked after too many redirects (line 234)"""
        def validator(url):
            return True

        fetcher = Fetcher(url_validator=validator, max_retries=0)

        response = Mock()
        response.status_code = 302
        response.headers = {"Location": "https://example.com/next"}
        response.close = Mock()

        fetcher.session.get = Mock(return_value=response)

        with self.assertRaises(FetchBlocked) as cm:
            fetcher._get("https://example.com", {})
        self.assertIn("too many redirects", str(cm.exception))

    # --- _read_bytes (lines 236-251) ---

    def test_read_bytes_declared_content_length_exceeds(self):
        """Test _read_bytes raises TooLarge for declared content-length (lines 237-239)"""
        response = Mock()
        response.headers = {"content-length": str(MAX_BYTES + 1)}
        response.close = Mock()

        with self.assertRaises(TooLarge) as cm:
            self.fetcher._read_bytes(response)
        self.assertIn("content-length", str(cm.exception))

    def test_read_bytes_stream_exceeds(self):
        """Test _read_bytes raises TooLarge for streaming body (lines 242-245)"""
        # Create chunks that exceed MAX_BYTES
        chunk_size = 65536
        num_chunks = (MAX_BYTES // chunk_size) + 2
        chunks = [b"x" * chunk_size for _ in range(num_chunks)]

        response = Mock()
        response.headers = {}
        response.iter_content = Mock(return_value=chunks)
        response.close = Mock()

        with self.assertRaises(TooLarge) as cm:
            self.fetcher._read_bytes(response)
        self.assertIn("body exceeds", str(cm.exception))

    def test_read_bytes_type_error_fallback(self):
        """Test _read_bytes handles TypeError (test doubles with .text only) (lines 247-248)"""
        response = Mock()
        response.headers = {}
        # No iter_content - will raise TypeError
        response.iter_content = Mock(side_effect=TypeError("no iter_content"))
        response.text = "fallback text"
        response.close = Mock()

        result = self.fetcher._read_bytes(response)
        self.assertEqual(result, b"fallback text")

    # --- _read_text charset handling (lines 253-262) ---

    def test_read_text_charset_from_content_type(self):
        """Test _read_text extracts charset from content-type (lines 255-257)"""
        response = Mock()
        response.headers = {"content-type": "text/html; charset=iso-8859-1"}
        response.close = Mock()
        response.iter_content = Mock(return_value=[b"hello"])
        
        with patch.object(self.fetcher, '_read_bytes', return_value=b"hello"):
            result = self.fetcher._read_text(response)
        self.assertEqual(result, "hello")

    def test_read_text_charset_lookup_error(self):
        """Test _read_text falls back to utf-8 on LookupError (lines 261-262)"""
        response = Mock()
        response.headers = {"content-type": "text/html; charset=invalid-charset-name"}
        response.close = Mock()
        
        with patch.object(self.fetcher, '_read_bytes', return_value=b"hello"):
            result = self.fetcher._read_text(response)
        self.assertEqual(result, "hello")

    # --- fetch_bytes (lines 264-275) ---

    def test_fetch_bytes_success(self):
        """Test fetch_bytes returns bytes on 200 (lines 268-270)"""
        response = Mock()
        response.status_code = 200
        response.close = Mock()
        self.fetcher.session.get = Mock(return_value=response)
        
        with patch.object(self.fetcher, '_read_bytes', return_value=b"content"):
            result = self.fetcher.fetch_bytes("https://example.com")
        self.assertEqual(result, b"content")

    def test_fetch_bytes_non_200(self):
        """Test fetch_bytes returns b'' on non-200 (lines 271)"""
        response = Mock()
        response.status_code = 404
        response.close = Mock()
        self.fetcher.session.get = Mock(return_value=response)
        
        result = self.fetcher.fetch_bytes("https://example.com")
        self.assertEqual(result, b"")

    def test_fetch_bytes_exceptions(self):
        """Test fetch_bytes returns b'' on exceptions (lines 272-274)"""
        self.fetcher.session.get = Mock(side_effect=FetchBlocked("blocked"))
        result = self.fetcher.fetch_bytes("https://example.com")
        self.assertEqual(result, b"")
        
        self.fetcher.session.get = Mock(side_effect=TooLarge("too large"))
        result = self.fetcher.fetch_bytes("https://example.com")
        self.assertEqual(result, b"")
        
        if requests:
            self.fetcher.session.get = Mock(side_effect=requests.RequestException("network"))
            result = self.fetcher.fetch_bytes("https://example.com")
            self.assertEqual(result, b"")

    # --- _backoff (lines 277-281) ---

    def test_backoff_with_retry_after(self):
        """Test _backoff respects Retry-After header (lines 279-280)"""
        fetcher = Fetcher()
        wait = fetcher._backoff(0, retry_after="5")
        self.assertEqual(wait, 5.0)
        
        wait = fetcher._backoff(0, retry_after="100")
        self.assertEqual(wait, MAX_BACKOFF)  # capped at MAX_BACKOFF

    def test_backoff_exponential(self):
        """Test _backoff exponential backoff (line 281)"""
        fetcher = Fetcher()
        wait = fetcher._backoff(0)
        self.assertEqual(wait, 1.0)  # 2^0 = 1
        
        wait = fetcher._backoff(1)
        self.assertEqual(wait, 2.0)  # 2^1 = 2
        
        wait = fetcher._backoff(5)
        self.assertEqual(wait, MAX_BACKOFF)  # capped

    def test_backoff_non_digit_retry_after(self):
        """Test _backoff ignores non-digit Retry-After (line 279)"""
        fetcher = Fetcher()
        wait = fetcher._backoff(0, retry_after="invalid")
        self.assertEqual(wait, 1.0)  # falls back to exponential

    # --- fetch retry logic (lines 297-359) ---

    def test_fetch_retry_on_retry_status(self):
        """Test fetch retries on RETRY_STATUS codes (lines 317-322)"""
        responses = [
            Mock(status_code=500, close=Mock(), headers={}),
            Mock(status_code=200, close=Mock(), headers={}, text="<html>ok</html>")
        ]
        self.fetcher.session.get = Mock(side_effect=responses)
        
        with patch.object(self.fetcher, '_read_text', return_value="<html>ok</html>"):
            with patch('time.sleep') as mock_sleep:
                result = self.fetcher.fetch("https://example.com")
        
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.fetcher.session.get.call_count, 2)
        mock_sleep.assert_called()

    def test_fetch_retry_exhausted(self):
        """Test fetch gives up after max_retries on network errors (lines 353-359)"""
        # Network errors (RequestException) return status_code=None after retries
        if requests:
            self.fetcher.session.get = Mock(side_effect=requests.RequestException("network"))
            
            with patch('time.sleep'):
                result = self.fetcher.fetch("https://example.com")
            
            self.assertEqual(result.status_code, None)  # final failure on network error
            self.assertEqual(self.fetcher.session.get.call_count, 3)  # max_retries + 1
        else:
            self.skipTest("requests not available")

    def test_fetch_retry_exhausted_http_error(self):
        """Test fetch returns error status after max_retries on HTTP 5xx (lines 324-327)"""
        # HTTP 500 on last attempt returns the error status, not None
        response = Mock(status_code=500, close=Mock(), headers={})
        self.fetcher.session.get = Mock(return_value=response)
        
        with patch('time.sleep'):
            result = self.fetcher.fetch("https://example.com")
        
        self.assertEqual(result.status_code, 500)  # HTTP error status returned
        self.assertIn("HTTP 500", result.error)

    def test_fetch_error_status_no_retry(self):
        """Test fetch returns error result for 4xx (lines 324-327)"""
        response = Mock(status_code=404, close=Mock(), headers={})
        self.fetcher.session.get = Mock(return_value=response)
        
        result = self.fetcher.fetch("https://example.com")
        
        self.assertEqual(result.status_code, 404)
        self.assertIn("HTTP 404", result.error)

    def test_fetch_non_html_content_type(self):
        """Test fetch returns error for non-HTML content-type (lines 329-336)"""
        response = Mock(status_code=200, close=Mock(), headers={"content-type": "application/json"})
        self.fetcher.session.get = Mock(return_value=response)
        
        result = self.fetcher.fetch("https://example.com")
        
        self.assertEqual(result.status_code, 200)
        self.assertIn("non-HTML content-type", result.error)
        self.assertIsNone(result.html)

    def test_fetch_js_render_attempt(self):
        """Test fetch attempts JS render when enabled (lines 338-344)"""
        with patch.dict(os.environ, {"NEXUS_RENDER_JS": "1"}, clear=True):
            fetcher = Fetcher(render_js=True, url_validator=lambda x: True)
            response = Mock(status_code=200, close=Mock(), headers={"content-type": "text/html"})
            fetcher.session.get = Mock(return_value=response)
            
            with patch.object(fetcher, '_read_text', return_value="<html>initial</html>"):
                with patch.object(fetcher, '_render_with_browser', return_value="<html>rendered</html>") as mock_render:
                    result = fetcher.fetch("https://example.com")
            
            mock_render.assert_called_once()
            self.assertEqual(result.html, "<html>rendered</html>")

    def test_fetch_js_render_size_limit(self):
        """Test fetch rejects JS render if too large (line 343)"""
        with patch.dict(os.environ, {"NEXUS_RENDER_JS": "1"}, clear=True):
            fetcher = Fetcher(render_js=True, url_validator=lambda x: True, max_bytes=10)
            response = Mock(status_code=200, close=Mock(), headers={"content-type": "text/html"})
            fetcher.session.get = Mock(return_value=response)
            
            with patch.object(fetcher, '_read_text', return_value="<html>initial</html>"):
                with patch.object(fetcher, '_render_with_browser', return_value="x" * 100) as mock_render:
                    result = fetcher.fetch("https://example.com")
            
            # Rendered content too large, should use initial HTML
            self.assertEqual(result.html, "<html>initial</html>")

    # --- fetch_robots (lines 361-372) ---

    def test_fetch_robots_200(self):
        """Test fetch_robots returns text on 200 (lines 366-367)"""
        response = Mock(status_code=200, close=Mock())
        self.fetcher.session.get = Mock(return_value=response)
        
        with patch.object(self.fetcher, '_read_text', return_value="User-agent: *"):
            result = self.fetcher.fetch_robots("https://example.com/robots.txt")
        self.assertEqual(result, "User-agent: *")

    def test_fetch_robots_4xx(self):
        """Test fetch_robots returns '' on 4xx (line 369)"""
        response = Mock(status_code=404, close=Mock())
        self.fetcher.session.get = Mock(return_value=response)
        
        result = self.fetcher.fetch_robots("https://example.com/robots.txt")
        self.assertEqual(result, "")

    def test_fetch_robots_5xx(self):
        """Test fetch_robots returns None on 5xx (line 369)"""
        response = Mock(status_code=500, close=Mock())
        self.fetcher.session.get = Mock(return_value=response)
        
        result = self.fetcher.fetch_robots("https://example.com/robots.txt")
        self.assertIsNone(result)

    def test_fetch_robots_exceptions(self):
        """Test fetch_robots returns None on exceptions (lines 370-371)"""
        self.fetcher.session.get = Mock(side_effect=FetchBlocked("blocked"))
        result = self.fetcher.fetch_robots("https://example.com/robots.txt")
        self.assertIsNone(result)
        
        self.fetcher.session.get = Mock(side_effect=TooLarge("too large"))
        result = self.fetcher.fetch_robots("https://example.com/robots.txt")
        self.assertIsNone(result)
        
        if requests:
            self.fetcher.session.get = Mock(side_effect=requests.RequestException("network"))
            result = self.fetcher.fetch_robots("https://example.com/robots.txt")
            self.assertIsNone(result)

    # --- fetch_text (lines 374-384) ---

    def test_fetch_text_200(self):
        """Test fetch_text returns text on 200 (lines 378-379)"""
        response = Mock(status_code=200, close=Mock())
        self.fetcher.session.get = Mock(return_value=response)
        
        with patch.object(self.fetcher, '_read_text', return_value="text content"):
            result = self.fetcher.fetch_text("https://example.com/sitemap.xml")
        self.assertEqual(result, "text content")

    def test_fetch_text_non_200(self):
        """Test fetch_text returns '' on non-200 (lines 380)"""
        response = Mock(status_code=404, close=Mock())
        self.fetcher.session.get = Mock(return_value=response)
        
        result = self.fetcher.fetch_text("https://example.com/sitemap.xml")
        self.assertEqual(result, "")

    def test_fetch_text_exceptions(self):
        """Test fetch_text returns '' on exceptions (lines 381-383)"""
        self.fetcher.session.get = Mock(side_effect=FetchBlocked("blocked"))
        result = self.fetcher.fetch_text("https://example.com")
        self.assertEqual(result, "")
        
        self.fetcher.session.get = Mock(side_effect=TooLarge("too large"))
        result = self.fetcher.fetch_text("https://example.com")
        self.assertEqual(result, "")

    # --- Constants and helpers ---

    def test_constants(self):
        """Test module constants are correct"""
        self.assertEqual(RETRY_STATUS, {429, 500, 502, 503, 504})
        self.assertEqual(REDIRECT_STATUS, {301, 302, 303, 307, 308})
        self.assertEqual(MAX_REDIRECTS, 5)
        self.assertEqual(MAX_BYTES, 5 * 1024 * 1024)
        self.assertEqual(MAX_BACKOFF, 30.0)

    def test_render_js_enabled(self):
        """Test _render_js_enabled helper"""
        with patch.dict(os.environ, {"NEXUS_RENDER_JS": "1"}, clear=True):
            self.assertTrue(_render_js_enabled())
        with patch.dict(os.environ, {"NEXUS_RENDER_JS": "true"}, clear=True):
            self.assertTrue(_render_js_enabled())
        with patch.dict(os.environ, {"NEXUS_RENDER_JS": "yes"}, clear=True):
            self.assertTrue(_render_js_enabled())
        with patch.dict(os.environ, {"NEXUS_RENDER_JS": "0"}, clear=True):
            self.assertFalse(_render_js_enabled())
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_render_js_enabled())

    # --- Exception classes ---

    def test_fetch_blocked_exception(self):
        """Test FetchBlocked exception"""
        exc = FetchBlocked("test message")
        self.assertEqual(str(exc), "test message")

    def test_too_large_exception(self):
        """Test TooLarge exception"""
        exc = TooLarge("too big")
        self.assertEqual(str(exc), "too big")


class TestFetchResult(unittest.TestCase):
    """Tests for FetchResult dataclass"""

    def test_fetch_result_creation(self):
        """Test FetchResult can be created with all fields"""
        result = FetchResult(
            url="https://example.com",
            status_code=200,
            html="<html>test</html>",
            error=None,
            elapsed=0.5,
            etag='"abc"',
            last_modified="Wed, 01 Jan 2025 00:00:00 GMT",
            not_modified=False,
            final_url="https://example.com/"
        )
        self.assertEqual(result.url, "https://example.com")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.html, "<html>test</html>")
        self.assertIsNone(result.error)
        self.assertEqual(result.elapsed, 0.5)
        self.assertEqual(result.etag, '"abc"')
        self.assertEqual(result.last_modified, "Wed, 01 Jan 2025 00:00:00 GMT")
        self.assertFalse(result.not_modified)
        self.assertEqual(result.final_url, "https://example.com/")

    def test_fetch_result_defaults(self):
        """Test FetchResult with minimal fields"""
        result = FetchResult(
            url="https://example.com",
            status_code=200,
            html=None,
            error="error",
            elapsed=0.1
        )
        self.assertIsNone(result.etag)
        self.assertIsNone(result.last_modified)
        self.assertFalse(result.not_modified)
        self.assertIsNone(result.final_url)


if __name__ == "__main__":
    unittest.main()