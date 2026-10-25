import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).parents[1] / "configure_cloudflare_tunnel.py"
spec = importlib.util.spec_from_file_location("cloudflare_config", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

SAMPLE = '''# synthetic fixture
tunnel: abc-123
credentials-file: /not-read/fixture.json
warp-routing:
  enabled: false
ingress:
  - hostname: old.example.test
    service: http://127.0.0.1:4200
  - service: http_status:404
'''

class ConfigTests(unittest.TestCase):
    def test_insert_preserves_other_routes_settings_and_fallback(self):
        result, changed = module.configure(SAMPLE, "abc-123", "new.example.test", 3000)
        self.assertTrue(changed)
        self.assertIn("credentials-file: /not-read/fixture.json", result)
        self.assertIn("hostname: old.example.test", result)
        self.assertIn("hostname: new.example.test\n    service: http://127.0.0.1:3000", result)
        self.assertLess(result.index("new.example.test"), result.index("http_status:404"))

    def test_idempotence_and_conflict(self):
        result, _ = module.configure(SAMPLE, "abc-123", "new.example.test", 3000)
        self.assertEqual(module.configure(result, "abc-123", "new.example.test", 3000), (result, False))
        with self.assertRaises(ValueError):
            module.configure(result, "abc-123", "new.example.test", 4200)
        self.assertEqual(module.configure(result, "abc-123", "NEW.EXAMPLE.TEST", 3000), (result, False))
        wildcard = SAMPLE.replace("old.example.test", "*.example.test")
        with self.assertRaises(ValueError):
            module.configure(wildcard, "abc-123", "new.example.test", 3000)

    def test_rejects_invalid_or_unsupported_config(self):
        for host in ["bad/path", "localhost", "-bad.example.test"]:
            with self.assertRaises(ValueError):
                module.configure(SAMPLE, "abc-123", host, 3000)
        with self.assertRaises(ValueError): module.configure(SAMPLE, "other", "new.example.test", 3000)
        for bad in (
            SAMPLE.replace("http_status:404", "http://example.test"),
            SAMPLE.replace("    service: http://127.0.0.1:4200", "    service: http://127.0.0.1:4200\n    originRequest:\n      connectTimeout: 30s"),
            SAMPLE.replace("  - service: http_status:404", "  - service: http_status:404\n  - service: http_status:404"),
        ):
            with self.assertRaises(ValueError): module.configure(bad, "abc-123", "new.example.test", 3000)

    def test_preserves_crlf_and_fails_closed_on_comments_or_complex_rules(self):
        crlf = SAMPLE.replace("\n", "\r\n")
        result, changed = module.configure(crlf, "abc-123", "new.example.test", 3000)
        self.assertTrue(changed)
        self.assertIn("new.example.test\r\n    service: http://127.0.0.1:3000\r\n", result)
        with self.assertRaises(ValueError):
            module.configure(SAMPLE.replace("  - hostname: old.example.test", "  - hostname: old.example.test\n    path: /api/*"), "abc-123", "new.example.test", 3000)

    def test_cli_review_apply_backup_validation_and_dns_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); config=root/"fixture.yml"; config.write_text(SAMPLE); config.chmod(0o640)
            log=root/"calls"; fake=root/"cloudflared"
            fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALL_LOG\"\nexit \"${FAKE_STATUS:-0}\"\n")
            fake.chmod(0o700)
            env=dict(os.environ,CALL_LOG=str(log))
            base=[sys.executable,str(SCRIPT),"--tunnel","abc-123","--hostname","new.example.test","--port","3000","--config",str(config),"--cloudflared",str(fake)]
            run=lambda *extra: subprocess.run(base+list(extra),env=env,capture_output=True,text=True)
            self.assertEqual(run().returncode,0)
            self.assertEqual(config.read_text(),SAMPLE)
            self.assertEqual(run("--create-dns").returncode,2)
            self.assertEqual(run("--apply").returncode,0)
            self.assertEqual(config.stat().st_mode & 0o777,0o640)
            backup=config.with_name("fixture.yml.bak")
            self.assertEqual(backup.read_text(),SAMPLE); self.assertEqual(backup.stat().st_mode & 0o777,0o600)
            no_overwrite=run("--apply","--hostname","another.example.test")
            self.assertEqual(no_overwrite.returncode,2)
            self.assertIn("new.example.test",config.read_text())
            self.assertEqual(backup.read_text(),SAMPLE)
            self.assertEqual(run("--apply","--create-dns","--confirm-dns").returncode,0)
            calls=log.read_text()
            self.assertIn("tunnel ingress validate --config",calls)
            self.assertIn("tunnel route dns abc-123 new.example.test",calls)
            self.assertNotIn("--overwrite",calls)
            self.assertNotIn("--overwrite-dns",calls)
            failed=dict(env,FAKE_STATUS="1")
            rejected=subprocess.run(base+["--apply","--hostname","another.example.test"],env=failed,capture_output=True,text=True)
            self.assertEqual(rejected.returncode,2)
            self.assertNotIn("another.example.test",config.read_text())
            symlink=root/"linked.yml"; symlink.symlink_to(config)
            rejected_link=subprocess.run(base[:base.index("--config")]+["--config",str(symlink),"--cloudflared",str(fake)],env=env,capture_output=True,text=True)
            self.assertEqual(rejected_link.returncode,2)
            self.assertIn("symlink",rejected_link.stderr)
            dns_fake=root/"dns-failure"
            dns_fake.write_text("#!/bin/sh\nif [ \"$2\" = route ]; then exit \"${FAKE_DNS_STATUS:-0}\"; fi\nexit 0\n")
            dns_fake.chmod(0o700)
            dns_config=root/"dns-fixture.yml"; dns_config.write_text(SAMPLE)
            dns_base=base.copy(); dns_base[dns_base.index(str(fake))]=str(dns_fake); dns_base[dns_base.index(str(config))]=str(dns_config)
            dns_failed=subprocess.run(dns_base+["--apply","--create-dns","--confirm-dns","--hostname","dnsfail.example.test"],env=dict(env,FAKE_DNS_STATUS="1"),capture_output=True,text=True)
            self.assertEqual(dns_failed.returncode,2, dns_failed.stderr)
            self.assertIn("dnsfail.example.test",dns_config.read_text(), dns_failed.stdout + dns_failed.stderr)
            self.assertEqual(dns_config.with_name("dns-fixture.yml.bak").read_text(),SAMPLE)
            self.assertIn("not rolled back",dns_failed.stderr)

if __name__ == "__main__": unittest.main()
