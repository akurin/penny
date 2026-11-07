import os
import unittest
from unittest.mock import patch

from penny.service import DEFAULT_MODEL, DEFAULT_MODEL_BASE_URL, Model, model_settings


class ModelSettingsTest(unittest.TestCase):
    def test_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(model_settings(),
                             ("", DEFAULT_MODEL, DEFAULT_MODEL_BASE_URL, ""))

    def test_new_names(self):
        env = {"MODEL_API_KEY": "new", "MODEL_NAME": "glm-4.7-flash",
               "MODEL_BASE_URL": "https://api.z.ai/api/paas/v4/",
               "MODEL_PROXY": "socks5h://127.0.0.1:1080"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(model_settings(),
                             ("new", "glm-4.7-flash",
                              "https://api.z.ai/api/paas/v4",
                              "socks5h://127.0.0.1:1080"))

    def test_model_uses_configured_base_url(self):
        model = Model("key", "glm-4.7-flash", "https://api.z.ai/api/paas/v4/")
        self.assertEqual(model.base_url, "https://api.z.ai/api/paas/v4")

    def test_empty_proxy_is_none(self):
        self.assertIsNone(Model("key", "model", proxy="").proxy)
        self.assertEqual(Model("key", "model", proxy="socks5://p:1080").proxy,
                         "socks5://p:1080")

    def test_native_provider_disables_thinking(self):
        model = Model("key", "glm-4.7-flash", "https://api.z.ai/api/paas/v4")
        payload = model._payload("instruction", {"message": "x"}, 400, None)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning", payload)
        self.assertNotIn("provider", payload)

    def test_openrouter_uses_normalized_reasoning_field(self):
        model = Model("key", "z-ai/glm-4.7-flash", "https://openrouter.ai/api/v1")
        payload = model._payload("instruction", {"message": "x"}, 400, None)
        self.assertTrue(model.openrouter)
        self.assertEqual(payload["reasoning"], {"enabled": False})
        self.assertEqual(payload["provider"], {"require_parameters": True})
        self.assertNotIn("thinking", payload)

    def test_openrouter_detection_needs_the_host(self):
        model = Model("key", "z-ai/glm-4.7-flash", "https://openrouter.ai.example/api/v1")
        self.assertFalse(model.openrouter)


if __name__ == "__main__":
    unittest.main()
