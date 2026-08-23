"""The server address, checked before a token is sent to it.

An http address is not refused: whether an unencrypted hop is acceptable depends on the network it
crosses, which the person setting this up knows and the code does not. What IS refused is an
address no request could be made to.
"""
import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "base_url",
    Path(__file__).resolve().parents[1] / "custom_components" / "progresscove" / "base_url.py",
)
_base_url = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base_url)
validate = _base_url.validate


class HttpsTest(unittest.TestCase):
    def test_https_is_accepted_without_a_warning(self):
        url, error, insecure = validate("https://api.progresscove.com")
        self.assertIsNone(error)
        self.assertFalse(insecure)
        self.assertEqual(url, "https://api.progresscove.com")

    def test_a_trailing_slash_and_spaces_are_normalised_away(self):
        url, error, _ = validate("  https://api.progresscove.com/  ")
        self.assertIsNone(error)
        self.assertEqual(url, "https://api.progresscove.com")


class InsecureWarningTest(unittest.TestCase):
    """http is usable, and flagged. Every one of these reaches the confirm step, not an error."""

    def test_http_is_usable_but_flagged(self):
        for probe in (
            "http://192.168.1.50:8000",
            "http://10.0.0.5",
            "http://taskbox.local:8000",
            "http://api.progresscove.com",
            "http://8.8.8.8",
            "http://localhost:8000",
        ):
            with self.subTest(probe):
                _, error, insecure = validate(probe)
                self.assertIsNone(error, f"{probe} should be usable, not an error")
                self.assertTrue(insecure, f"{probe} should be flagged as unencrypted")


class UnusableAddressTest(unittest.TestCase):
    """Refused because no request could be made, not because of what is at the other end."""

    def test_a_missing_scheme_is_refused_rather_than_treated_as_a_path(self):
        """urlsplit puts a bare host in `path`, so this would otherwise be requested relative to
        nothing and fail somewhere far less legible."""
        self.assertEqual(validate("api.progresscove.com")[1], "invalid_url")

    def test_other_schemes_are_refused(self):
        for probe in ("file:///etc/passwd", "ftp://host", "javascript:alert(1)", "ws://host"):
            with self.subTest(probe):
                self.assertEqual(validate(probe)[1], "invalid_url")

    def test_a_scheme_with_no_host_is_refused(self):
        for probe in ("https://", "http:///path"):
            with self.subTest(probe):
                self.assertEqual(validate(probe)[1], "invalid_url")

    def test_empty_and_whitespace_are_refused(self):
        for probe in ("", "   ", None):
            with self.subTest(probe):
                self.assertEqual(validate(probe)[1], "invalid_url")


if __name__ == "__main__":
    unittest.main()


class FlowUsesTheWarningTest(unittest.TestCase):
    """The flag is only worth anything if the flow acts on it.

    Asserted against the source rather than by driving the flow, which would need most of Home
    Assistant: what matters is that BOTH entry points route an insecure address to a confirm step
    before `_async_load_projects`, which is the first call that carries the token.
    """

    def _flow(self):
        path = (Path(__file__).resolve().parents[1] / "custom_components" / "progresscove"
                / "config_flow.py")
        return path.read_text()

    def test_both_entry_points_confirm_before_sending_the_token(self):
        source = self._flow()
        for step in ("async_step_insecure", "async_step_reconfigure_insecure"):
            with self.subTest(step):
                self.assertIn(f"return await self.{step}()", source)
                self.assertIn(f"async def {step}(", source)

    def test_the_confirm_names_the_address_it_is_asking_about(self):
        """A warning that does not say WHICH address is a warning people click through."""
        source = self._flow()
        self.assertEqual(source.count('description_placeholders={"url": self._base_url}'), 2)


class TokenChangeTest(unittest.TestCase):
    """Every token is minted dormant, so a replacement authenticates nothing until it is paired.

    Reauth and reconfigure both took a token and neither paired it, so every replacement was
    rejected as invalid: the one thing those flows exist to do. Reauth now pairs. Reconfigure no
    longer takes a token at all, because a DIFFERENT account's token is a delete-and-re-add and a
    rotation belongs in reauth.
    """

    def _flow(self):
        return (Path(__file__).resolve().parents[1] / "custom_components" / "progresscove"
                / "config_flow.py").read_text()

    def test_reauth_routes_an_unpaired_token_to_pairing(self):
        source = self._flow()
        start = source.index("async def async_step_reauth_confirm")
        reauth = source[start:source.index("def _save_reauth_token")]
        self.assertIn('self._after_pairing = "reauth"', reauth)
        self.assertIn("return await self.async_step_pair()", reauth)

    def test_pairing_reuses_an_existing_installation_id(self):
        """A new id would bind the token to a stranger and strand the entry."""
        source = self._flow()
        self.assertIn("self._device_id = self._device_id or secrets.token_urlsafe(16)", source)

    def test_reauth_stores_the_id_the_token_was_bound_to(self):
        source = self._flow()
        start = source.index("def _save_reauth_token")
        saver = source[start:source.index("async def _async_check_token")]
        self.assertIn("CONF_DEVICE_ID: self._device_id", saver)

    def test_reconfigure_does_not_take_a_token(self):
        source = self._flow()
        step = source[source.index("async def async_step_reconfigure("):
                      source.index("async def async_step_reconfigure_insecure")]
        self.assertNotIn("CONF_TOKEN): str", step, "reconfigure still offers a token field")


class PairingErrorTest(unittest.TestCase):
    def test_only_an_auth_failure_becomes_not_paired_yet(self):
        """The pairing screen used to overwrite every error with "not paired yet", so an outage
        left someone re-entering a code at an unreachable server."""
        source = (Path(__file__).resolve().parents[1] / "custom_components" / "progresscove"
                  / "config_flow.py").read_text()
        start = source.index("async def async_step_pair")
        pair = source[start:source.index("async def async_step_projects")]
        self.assertIn('if errors.get("base") == "invalid_auth":\n                errors = '
                      '{"base": "not_paired_yet"}', pair)


class DocstringDriftTest(unittest.TestCase):
    """Copy that describes a flow the code no longer has.

    Every one of these was true once: reconfigure took a token, the options flow chose lists, and
    there were two subentry types rather than four.
    """

    def _flow(self):
        return (Path(__file__).resolve().parents[1] / "custom_components" / "progresscove"
                / "config_flow.py").read_text()

    def test_nothing_claims_reconfigure_re_enters_the_token(self):
        for phrase in ("token re-entered", "Paste the API token"):
            with self.subTest(phrase):
                self.assertNotIn(phrase, self._flow())

    def test_nothing_claims_the_options_flow_chooses_lists(self):
        source = self._flow()
        start = source.index("class ProgressCoveOptionsFlow")
        options = source[start:source.index("class _NodePickerSubentryFlow")]
        self.assertNotIn("which projects are shown", options)

    def test_the_subentry_docstring_does_not_count_them(self):
        """It said "the two things" while returning four."""
        self.assertNotIn("The two things a user ADDS", self._flow())


class ReconfigureScopeTest(unittest.TestCase):
    """Reconfigure changes the address and nothing else."""

    def _flow(self):
        return (Path(__file__).resolve().parents[1] / "custom_components" / "progresscove"
                / "config_flow.py").read_text()

    def test_it_carries_the_existing_entry_data_forward(self):
        source = self._flow()
        start = source.index("def _save_credentials")
        saver = source[start:source.index("async def async_step_reauth")]
        self.assertIn("**entry.data", saver, "reconfigure must preserve what it does not change")

    def test_it_writes_only_the_address_and_the_token(self):
        source = self._flow()
        start = source.index("def _save_credentials")
        saver = source[start:source.index("async def async_step_reauth")]
        for key in ("CONF_DEVICE_ID", "CONF_PROJECTS"):
            with self.subTest(key):
                self.assertNotIn(f"{key}:", saver, f"reconfigure should not rewrite {key}")

    def test_there_is_no_project_step_left_on_this_route(self):
        self.assertNotIn("async_step_reconfigure_projects", self._flow())
