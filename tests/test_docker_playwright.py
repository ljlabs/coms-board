"""The Docker image keeps the agent's Playwright runtime self-contained."""

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DockerPlaywrightRuntimeContractTests(unittest.TestCase):
    def test_image_bakes_in_pinned_cli_and_its_chromium_browser(self):
        dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
        normalized = re.sub(r'\s+', ' ', dockerfile)

        self.assertRegex(normalized, r'@playwright/cli@\d+\.\d+\.\d+')
        self.assertIn('playwright@1.64.0-alpha-1790635538000', normalized)
        self.assertIn('playwright install --with-deps chromium', normalized)
        self.assertIn('PLAYWRIGHT_BROWSERS_PATH=/ms-playwright', normalized)
        self.assertIn('PLAYWRIGHT_MCP_CONFIG=/app/playwright-cli.config.json', normalized)

    def test_cli_config_writes_output_to_the_persistent_home_volume(self):
        config = json.loads((ROOT / 'docker/playwright-cli.config.json').read_text(encoding='utf-8'))

        self.assertEqual(config['browser']['browserName'], 'chromium')
        self.assertEqual(config['outputDir'], '/home/board/playwright-output')

    def test_compose_keeps_cli_state_writable_and_allocates_chromium_shared_memory(self):
        compose = (ROOT / 'compose.yaml').read_text(encoding='utf-8')
        board_service = compose.split('\n  board:\n', 1)[1].split('\nvolumes:\n', 1)[0]
        entrypoint = (ROOT / 'docker/board-entrypoint.sh').read_text(encoding='utf-8')

        self.assertIn('agent-state:/home/board', board_service)
        self.assertRegex(board_service, r'(?m)^\s*shm_size:\s*512m\s*$')
        self.assertIn('read_only: true', board_service)
        self.assertIn('PLAYWRIGHT_BROWSERS_PATH: /ms-playwright', board_service)
        self.assertIn('PLAYWRIGHT_MCP_CONFIG: /app/playwright-cli.config.json', board_service)
        self.assertIn('mkdir -p "$HOME/playwright-output"', entrypoint)
        self.assertIn('exec "$@"', entrypoint)

    def test_shared_skill_uses_the_baked_cli_without_runtime_npx_install(self):
        skill = (ROOT / 'skills/playwright/SKILL.md').read_text(encoding='utf-8')

        self.assertIn('playwright-cli', skill)
        self.assertIn('/home/board/playwright-output', skill)
        self.assertNotIn('npx --package', skill)
        self.assertNotIn('npm install -g @playwright/cli', skill)


if __name__ == '__main__':
    unittest.main()
