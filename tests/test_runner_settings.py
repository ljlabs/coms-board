"""Saved runner settings take precedence over Docker bootstrap defaults."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coms import api, db
from coms.runner import Runner
from coms.server import r_runner_settings


class RunnerSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.env = patch.dict(os.environ, {
            'COMS_DB': str(Path(self.tmp.name) / 'board.db'),
            'COMS_IN_DOCKER': '1',
            'COMS_LLAMACPP_BASE_URL': 'http://host.docker.internal:12345/v1',
            'COMS_LLAMACPP_MODEL': 'bootstrap.gguf',
        })
        self.env.start()
        db._local.__dict__.clear()
        self.conn = db.connect()
        self.runner = Runner()
        self.policy = patch('coms.workspace.workspace_path', side_effect=lambda values, **kw: values['workspace'])
        self.policy.start()

    def tearDown(self):
        self.runner.stop()
        self.conn.close()
        db._local.__dict__.clear()
        self.policy.stop()
        self.env.stop()
        self.tmp.cleanup()

    def save(self, values):
        self.conn.executemany('INSERT OR REPLACE INTO runner_settings VALUES(?,?)', values.items())
        self.conn.commit()

    def read_http(self):
        with patch('coms.server.workspace_path', side_effect=lambda values, **kw: values['workspace']):
            return r_runner_settings(self.conn, 'GET', None, {}, {}, 'admin')

    def test_saved_hosted_model_endpoint_and_key_survive_docker_reads(self):
        saved = {'model_opencode': 'opencode/spark', 'opencode_api_base_url': 'https://example.test/v1',
                 'opencode_api_model': 'manual-id', 'opencode_api_key': 'test-key', 'workspace': '/workspace/project'}
        self.save(saved)
        settings = self.runner.settings()
        for key, value in saved.items():
            self.assertEqual(settings[key], value, key)
        http = self.read_http()
        for key in ('model_opencode', 'opencode_api_base_url', 'opencode_api_model', 'workspace'):
            self.assertEqual(http[key], settings[key], key)
        self.assertTrue(http['opencode_api_key_set'])
        self.assertNotIn('opencode_api_key', http)

    def test_fresh_docker_defaults_do_not_write_the_database(self):
        settings = self.runner.settings()
        self.assertEqual(settings['workspace'], '/workspace')
        self.assertEqual(settings['opencode_api_base_url'], 'http://host.docker.internal:12345/v1')
        self.assertEqual(settings['opencode_api_model'], 'bootstrap.gguf')
        self.assertEqual(settings['model_opencode'], 'coms-local/bootstrap.gguf')
        self.assertEqual(self.read_http()['model_opencode'], settings['model_opencode'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM runner_settings').fetchone()[0], 0)

    def test_explicit_empty_model_and_cleared_key_remain_empty(self):
        self.save({'model_opencode': '', 'opencode_api_model': '', 'opencode_api_key': ''})
        self.assertEqual(self.runner.settings()['model_opencode'], '')
        http = self.read_http()
        self.assertEqual(http['model_opencode'], '')
        self.assertEqual(http['opencode_api_model'], '')
        self.assertFalse(http['opencode_api_key_set'])

    def test_invalid_imported_workspace_still_uses_container_workspace(self):
        self.save({'workspace': 'invalid-imported-path', 'model_opencode': 'opencode/spark'})
        with patch('coms.workspace.workspace_path', side_effect=ValueError('invalid workspace')):
            self.assertEqual(self.runner.settings()['workspace'], '/workspace')
            self.assertEqual(self.read_http()['workspace'], '/workspace')

    def test_saved_model_survives_http_save_and_new_runner_instance(self):
        policy = lambda values, **kw: values.get('workspace', '/workspace')
        with patch('coms.runner.RUNNER', self.runner), patch('coms.server.workspace_path', side_effect=policy):
            r_runner_settings(self.conn, 'PUT', None, {}, {'model_opencode': 'opencode/spark'}, 'admin')
        self.assertEqual(self.read_http()['model_opencode'], 'opencode/spark')
        self.assertEqual(Runner().settings()['model_opencode'], 'opencode/spark')

    def test_settings_change_still_rejects_active_workers(self):
        self.runner.active[1] = True
        with patch('coms.runner.RUNNER', self.runner), self.assertRaisesRegex(api.BadRequest, 'active agents'):
            r_runner_settings(self.conn, 'PUT', None, {}, {'model_opencode': 'opencode/spark'}, 'admin')
        self.assertIsNone(self.conn.execute("SELECT value FROM runner_settings WHERE key='model_opencode'").fetchone())
