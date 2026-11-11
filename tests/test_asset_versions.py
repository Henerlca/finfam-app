from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.main import service_worker
import app.services.assets as assets


class AssetVersionTests(unittest.TestCase):
    def test_asset_url_changes_when_mocked_file_content_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            static_root = Path(directory)
            css = static_root / "css" / "app.css"
            css.parent.mkdir(parents=True)
            css.write_text("body { color: red; }", encoding="utf-8")
            with patch.object(assets, "STATIC_ROOT", static_root):
                first = assets.asset_url("/static/css/app.css?v=old")
                css.write_text("body { color: blue; }", encoding="utf-8")
                second = assets.asset_url("/static/css/app.css")

        self.assertRegex(first, r"^/static/css/app\.css\?v=[0-9a-f]{12}$")
        self.assertRegex(second, r"^/static/css/app\.css\?v=[0-9a-f]{12}$")
        self.assertNotEqual(first, second)

    def test_service_worker_urls_and_cache_name_match_helpers(self):
        public = assets.public_assets()
        source = assets.service_worker_source()
        expected_json = json.dumps(public, ensure_ascii=False, separators=(",", ":"))

        self.assertTrue(any(url.startswith("/static/css/finance-visuals.css?v=") for url in public))
        self.assertIn(f'const CACHE_NAME = "{assets.shell_cache_name()}";', source)
        self.assertIn(f"const PUBLIC_ASSETS = {expected_json};", source)
        self.assertEqual(re.findall(r"/static/[^\"?]+\?v=[0-9a-f]{12}", source), [
            url for url in public if url.startswith("/static/")
        ])
        self.assertIn('new Request(url, { cache: "reload" })', source)
        self.assertIn('key.startsWith("finfam-shell-")', source)
        self.assertFalse(any("offline" in url.casefold() for url in public))
        self.assertNotIn("offlineUrl", source)

    def test_service_worker_keeps_versioned_request_as_exact_cache_key(self):
        source = assets.service_worker_source()
        script = r'''
const vm = require("node:vm");
const source = require("node:fs").readFileSync(0, "utf8");
const listeners = {};
const matchKeys = [];
const self = {
  location: { origin: "https://example.test" },
  addEventListener: (name, handler) => { listeners[name] = handler; },
  clients: { claim: () => Promise.resolve() },
};
const context = {
  URL,
  Promise,
  Request: class Request {},
  self,
  caches: {
    match: (key) => { matchKeys.push(key); return Promise.resolve({}); },
    open: () => Promise.resolve({ addAll: () => Promise.resolve() }),
    keys: () => Promise.resolve([]),
  },
  fetch: () => Promise.resolve({}),
};
vm.runInNewContext(source, context);
const request = {
  method: "GET",
  mode: "no-cors",
  url: "https://example.test/static/css/app.css?v=brand-new",
};
const event = { request, respondWith: (response) => { event.response = response; } };
listeners.fetch(event);
event.response.then(() => {
  if (matchKeys[0] !== request) process.exit(1);
});
'''
        result = subprocess.run(["node", "-e", script], input=source, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sw_handler_uses_the_same_dynamic_payload(self):
        response = service_worker()
        source = bytes(response.body).decode("utf-8")

        self.assertEqual(response.headers["cache-control"], "no-cache, no-store, must-revalidate")
        self.assertIn(f'const CACHE_NAME = "{assets.shell_cache_name()}";', source)
        self.assertIn(json.dumps(assets.public_assets(), separators=(",", ":")), source)

    def test_clean_default_select_does_not_block_update_and_dirty_fields_do(self):
        source = (assets.STATIC_ROOT / "js" / "app.js").read_text()
        function_source = source[source.index("const hasDirtyForm ="):source.index("const showUpdateNotice =")]
        script = r'''
const vm = require("node:vm");
const fn = require("node:fs").readFileSync(0, "utf8");
const field = { tagName: "SELECT", type: "select-one", selectedIndex: 0, options: [{defaultSelected:false}, {defaultSelected:false}] };
const form = {elements:[field]};
const context = {document:{querySelectorAll:()=>[form]}};
vm.runInNewContext(fn + "globalThis.check = hasDirtyForm;", context);
if (context.check()) process.exit(1);
field.selectedIndex = 1;
if (!context.check()) process.exit(2);
form.elements = [{tagName:"INPUT",type:"text",value:"rascunho",defaultValue:""}];
if (!context.check()) process.exit(3);
form.elements[0].value = "";
if (context.check()) process.exit(4);
'''
        result = subprocess.run(["node", "-e", script], input=function_source, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
